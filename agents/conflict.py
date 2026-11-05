"""
Agent 4 — Conflict Detector
Uses ChatOllama + OllamaEmbeddings + ChromaDB to find regulatory conflicts.
Types: A (superseded), B (contradiction), C (overlap/merge), none.
Supersede and overlap are applied automatically (staff can undo them);
contradictions hold both MAPs until Legal decides.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_ollama import OllamaEmbeddings
from agents.llm import chat_llm, embeddings as ollama_embeddings
from langchain_core.messages import SystemMessage, HumanMessage

import db

logger = logging.getLogger("agents.conflict")

CHROMA_DIR = str(Path("./data/chroma_db"))
COLLECTION_NAME = "maps"
SIMILARITY_THRESHOLD = 0.75

COMPARE_PROMPT = (
    "Compare these two compliance actions from different regulatory circulars. "
    'Classify as: "A" (MAP B supersedes MAP A — amendment), '
    '"B" (direct contradiction — both cannot be satisfied simultaneously), '
    '"C" (overlap — same requirement, can be merged), '
    '"none" (unrelated). '
    'Return JSON: {"conflict_type": "A/B/C/none", "explanation": "one sentence", '
    '"recommendation": "what to do"}'
)

MERGE_PROMPT = (
    "These two compliance actions overlap significantly. Merge them into a single "
    "unified compliance action that covers all requirements from both. "
    "Return JSON with exactly these 6 fields: "
    "action_description, department, priority (use the higher priority), "
    "deadline_days (use the shorter deadline), compliance_metric, verification_method."
)


def _get_chroma_collection():
    """Initialize or retrieve the ChromaDB collection."""
    import chromadb

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    embeddings = ollama_embeddings()

    # We use chromadb's native collection with a custom embedding function wrapper
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return collection, embeddings


def _embed_text(text: str, embeddings: OllamaEmbeddings) -> list[float]:
    """Generate embedding vector for a text string."""
    return embeddings.embed_query(text)


def index_map(map_id: str):
    """Add or refresh one MAP in the similarity index, so later circulars are compared against it.
    Used for MAPs that Legal sends back to departments after resolving a contradiction."""
    map_row = db.fetch_one("SELECT * FROM maps WHERE id = ?", (map_id,))
    if not map_row or map_row["status"] in ("superseded", "conflict"):
        return
    collection, embeddings = _get_chroma_collection()
    collection.upsert(
        ids=[map_id],
        documents=[map_row["action_description"]],
        metadatas=[{
            "map_id": map_id,
            "regulation_id": map_row["regulation_id"] or "",
            "department": map_row["department"] or "",
        }],
        embeddings=[_embed_text(map_row["action_description"], embeddings)],
    )


def _parse_conflict_response(raw: str) -> dict:
    """Parse conflict classification from LLM output."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"```", "", cleaned)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}


def _propose_merge(llm, existing_text: str, new_text: str, map_row: dict) -> dict | None:
    """Ask the model for one combined action covering two overlapping ones.
    Returned as a proposal for Legal; nothing is created until Legal accepts it."""
    merge_resp = llm.invoke([
        SystemMessage(content=MERGE_PROMPT),
        HumanMessage(content=f"Action 1: {existing_text}\n\nAction 2: {new_text}"),
    ])
    merged = _parse_conflict_response(merge_resp.content)
    if not merged or "action_description" not in merged:
        return None

    # Clean and validate department to prevent CHECK constraint failures
    dept = str(merged.get("department", "")).strip()
    valid_depts = {"IT", "Compliance", "Legal", "HR", "Risk", "Operations"}
    if dept not in valid_depts:
        dept = next((d for d in valid_depts if d.lower() in dept.lower() or dept.lower() in d.lower()),
                    map_row["department"])

    # Clean and validate priority
    priority = str(merged.get("priority", "")).strip().lower()
    if priority not in {"high", "medium", "low"}:
        priority = next((p for p in ("high", "medium", "low") if p[:3] in priority), map_row["priority"])

    # Clean and validate deadline_days
    try:
        deadline_days = int(str(merged.get("deadline_days", map_row["deadline_days"])).strip())
        if deadline_days <= 0:
            deadline_days = 90
    except Exception:
        deadline_days = map_row["deadline_days"] or 90

    return {
        "action_description": str(merged["action_description"]),
        "department": dept,
        "priority": priority,
        "deadline_days": deadline_days,
        "compliance_metric": merged.get("compliance_metric", map_row["compliance_metric"]),
        "verification_method": merged.get("verification_method", map_row["verification_method"]),
        "regulation_id": map_row["regulation_id"],
    }


def run_conflict_detector(map_ids: list = None) -> dict:
    """Detect conflicts for newly extracted MAPs (status='pending' or just created)."""
    # Get MAPs that need conflict checking — recently created, not yet in ChromaDB
    if map_ids is not None:
        if not map_ids:
            logger.info("No MAPs specified for conflict detector")
            return {"checked": 0, "conflicts_found": 0}
        placeholders = ",".join(["?"] * len(map_ids))
        new_maps = db.fetch_all(
            f"""SELECT * FROM maps
               WHERE status IN ('pending', 'in_progress')
               AND conflict_flag = 0
               AND id IN ({placeholders})
               AND id NOT IN (
                   SELECT map_id FROM audit_log
                   WHERE agent = 'conflict_detector' AND action = 'conflict_checked'
                   AND map_id IS NOT NULL
               )""",
            tuple(map_ids)
        )
    else:
        new_maps = db.fetch_all(
            """SELECT * FROM maps
               WHERE status IN ('pending', 'in_progress')
               AND conflict_flag = 0
               AND id NOT IN (
                   SELECT map_id FROM audit_log
                   WHERE agent = 'conflict_detector' AND action = 'conflict_checked'
                   AND map_id IS NOT NULL
               )"""
        )

    if not new_maps:
        logger.info("No new MAPs to check for conflicts")
        return {"checked": 0, "conflicts_found": 0}

    collection, embeddings = _get_chroma_collection()
    llm = chat_llm()

    checked = 0
    conflicts_found = 0

    for map_row in new_maps:
        map_id = map_row["id"]
        action_desc = map_row["action_description"]

        try:
            # Embed the current MAP
            vector = _embed_text(action_desc, embeddings)

            # Search for similar existing MAPs in ChromaDB
            existing_count = collection.count()
            if existing_count > 0:
                n_results = min(5, existing_count)
                results = collection.query(
                    query_embeddings=[vector],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )

                distances = results["distances"][0] if results["distances"] else []
                documents = results["documents"][0] if results["documents"] else []
                metadatas = results["metadatas"][0] if results["metadatas"] else []

                for dist, doc, meta in zip(distances, documents, metadatas):
                    # ChromaDB cosine distance: 0 = identical, 2 = opposite
                    # Convert to similarity: sim = 1 - (dist / 2)
                    similarity = 1.0 - (dist / 2.0)
                    if similarity < SIMILARITY_THRESHOLD:
                        continue

                    existing_map_id = meta.get("map_id", "")
                    if existing_map_id == map_id:
                        continue

                    existing_regulation_id = meta.get("regulation_id", "")
                    if existing_regulation_id == map_row["regulation_id"]:
                        continue

                    # Archived MAPs leave the index for good, otherwise the same old MAP gets
                    # merged again and again. MAPs on hold for Legal are skipped but stay
                    # indexed: they come back once Legal decides.
                    existing = db.fetch_one("SELECT status FROM maps WHERE id = ?", (existing_map_id,))
                    if not existing or existing["status"] == "superseded":
                        collection.delete(ids=[existing_map_id])
                        continue
                    if existing["status"] == "conflict":
                        continue

                    # Ask LLM to classify the conflict
                    compare_text = (
                        f"MAP A (existing): {doc}\n\n"
                        f"MAP B (new): {action_desc}"
                    )
                    response = llm.invoke([
                        SystemMessage(content=COMPARE_PROMPT),
                        HumanMessage(content=compare_text),
                    ])

                    classification = _parse_conflict_response(response.content)
                    conflict_type = classification.get("conflict_type", "none")
                    explanation = classification.get("explanation", "")
                    recommendation = classification.get("recommendation", "")

                    description = f"{explanation} | {recommendation}" if recommendation else explanation

                    if conflict_type == "A":
                        # MAP B supersedes MAP A: archive the old MAP and its department copies
                        # automatically (completed ones keep their status). Staff can undo it.
                        db.execute(
                            "UPDATE maps SET status = 'superseded', conflict_flag = 1, "
                            "conflict_type = 'A', conflict_detail = ? "
                            "WHERE (id = ? OR parent_map_id = ?) AND status != 'completed'",
                            (explanation, existing_map_id, existing_map_id),
                        )
                        collection.delete(ids=[existing_map_id])
                        conflict_id = db.new_id()
                        db.execute(
                            """INSERT INTO conflicts
                               (id, map_id_1, map_id_2, conflict_type, description,
                                resolution, resolved_at, created_at)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (conflict_id, existing_map_id, map_id, "A", description,
                             "auto_resolved", db.now_iso(), db.now_iso()),
                        )
                        db.insert_audit_entry(
                            agent="conflict_detector",
                            action="MAP superseded",
                            detail={"old_map": existing_map_id, "new_map": map_id, "explanation": explanation},
                            map_id=existing_map_id,
                        )
                        conflicts_found += 1

                    elif conflict_type == "B":
                        # Contradiction: hold both MAPs and their department copies for Legal
                        for held_id in (map_id, existing_map_id):
                            db.execute(
                                "UPDATE maps SET status = 'conflict', conflict_flag = 1, "
                                "conflict_type = 'B', conflict_detail = ? "
                                "WHERE (id = ? OR parent_map_id = ?) AND status != 'completed'",
                                (explanation, held_id, held_id),
                            )
                        db.execute(
                            """INSERT INTO conflicts
                               (id, map_id_1, map_id_2, conflict_type, description, resolution, created_at)
                               VALUES (?,?,?,?,?,?,?)""",
                            (db.new_id(), existing_map_id, map_id, "B", description, "pending_legal", db.now_iso()),
                        )
                        db.insert_audit_entry(
                            agent="conflict_detector",
                            action="contradiction detected",
                            detail={"map_1": existing_map_id, "map_2": map_id, "explanation": explanation},
                            map_id=map_id,
                        )
                        conflicts_found += 1

                    elif conflict_type == "C":
                        # Overlap: merge into one combined MAP automatically and archive both
                        # originals with their department copies. Staff can undo it.
                        proposal = _propose_merge(llm, doc, action_desc, map_row)
                        if proposal:
                            merged_id = db.new_id()
                            now = db.now_iso()
                            db.execute(
                                """INSERT INTO maps
                                   (id, regulation_id, action_description, department, priority,
                                    deadline_days, deadline_date, compliance_metric, verification_method,
                                    status, conflict_detail, created_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    merged_id, proposal["regulation_id"], proposal["action_description"],
                                    proposal["department"], proposal["priority"], proposal["deadline_days"],
                                    (datetime.now(timezone.utc) + timedelta(days=proposal["deadline_days"])).isoformat(),
                                    proposal["compliance_metric"], proposal["verification_method"],
                                    "pending", f"Merged from {existing_map_id} and {map_id}", now,
                                ),
                            )
                            for archived_id in (existing_map_id, map_id):
                                db.execute(
                                    "UPDATE maps SET status = 'superseded', conflict_flag = 1, "
                                    "conflict_type = 'C', conflict_detail = ? "
                                    "WHERE (id = ? OR parent_map_id = ?) AND status != 'completed'",
                                    ("Merged automatically into one combined action.", archived_id, archived_id),
                                )
                            collection.delete(ids=[existing_map_id])
                            db.execute(
                                """INSERT INTO conflicts
                                   (id, map_id_1, map_id_2, conflict_type, description,
                                    resolution, resolved_at, merged_map_id, created_at)
                                   VALUES (?,?,?,?,?,?,?,?,?)""",
                                (db.new_id(), existing_map_id, map_id, "C", description,
                                 "merged", now, merged_id, now),
                            )
                            db.insert_audit_entry(
                                agent="conflict_detector",
                                action="MAPs merged",
                                detail={"merged_from": [existing_map_id, map_id], "merged_id": merged_id},
                                map_id=merged_id,
                            )
                        conflicts_found += 1

                    # else: "none" — no conflict, unrelated

                    current = db.fetch_one("SELECT status FROM maps WHERE id = ?", (map_id,))
                    if current and current["status"] in ("superseded", "conflict"):
                        break

            # Add new MAP to ChromaDB for future searches (if not superseded/conflicted)
            current_status = db.fetch_one("SELECT status FROM maps WHERE id = ?", (map_id,))
            if current_status and current_status["status"] not in ("superseded", "conflict"):
                collection.upsert(
                    ids=[map_id],
                    documents=[action_desc],
                    metadatas=[{
                        "map_id": map_id,
                        "regulation_id": map_row["regulation_id"] or "",
                        "department": map_row["department"] or "",
                    }],
                    embeddings=[vector],
                )

            # Mark as conflict-checked
            db.insert_audit_entry(
                agent="conflict_detector",
                action="conflict_checked",
                detail={"conflicts_found_for_map": conflicts_found},
                map_id=map_id,
            )
            checked += 1

        except Exception as exc:
            logger.error("Conflict detection failed for MAP %s: %s", map_id, exc)
            db.insert_audit_entry(
                agent="conflict_detector",
                action="conflict_check_failed",
                detail={"error": str(exc)},
                map_id=map_id,
            )

    summary = {"checked": checked, "conflicts_found": conflicts_found}
    logger.info("Conflict detector complete: %s", summary)
    return summary
