"""
Agent 4 — Conflict Detector
Uses ChatOllama + OllamaEmbeddings + ChromaDB to find regulatory conflicts.
Types: A (superseded), B (contradiction), C (overlap/merge), none.
"""

import json
import logging
import re
from pathlib import Path

from langchain_ollama import ChatOllama, OllamaEmbeddings
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
    embeddings = OllamaEmbeddings(model="nomic-embed-text")

    # We use chromadb's native collection with a custom embedding function wrapper
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return collection, embeddings


def _embed_text(text: str, embeddings: OllamaEmbeddings) -> list[float]:
    """Generate embedding vector for a text string."""
    return embeddings.embed_query(text)


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
    llm = ChatOllama(model="qwen3:8b", temperature=0.1, timeout=120)

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

                    if conflict_type == "A":
                        # MAP B supersedes MAP A — archive old MAP
                        db.execute(
                            "UPDATE maps SET status = 'superseded', conflict_flag = 1, "
                            "conflict_type = 'A', conflict_detail = ? WHERE id = ?",
                            (explanation, existing_map_id),
                        )
                        db.insert_audit_entry(
                            agent="conflict_detector",
                            action="MAP superseded",
                            detail={
                                "old_map": existing_map_id,
                                "new_map": map_id,
                                "explanation": explanation,
                            },
                            map_id=existing_map_id,
                        )
                        conflicts_found += 1

                    elif conflict_type == "B":
                        # Direct contradiction — flag both
                        db.execute(
                            "UPDATE maps SET status = 'conflict', conflict_flag = 1, "
                            "conflict_type = 'B', conflict_detail = ? WHERE id = ?",
                            (explanation, map_id),
                        )
                        db.execute(
                            "UPDATE maps SET status = 'conflict', conflict_flag = 1, "
                            "conflict_type = 'B', conflict_detail = ? WHERE id = ?",
                            (explanation, existing_map_id),
                        )
                        # Write to conflicts table
                        conflict_id = db.new_id()
                        db.execute(
                            """INSERT INTO conflicts
                               (id, map_id_1, map_id_2, conflict_type, description,
                                resolution, created_at)
                               VALUES (?,?,?,?,?,?,?)""",
                            (
                                conflict_id, existing_map_id, map_id, "B",
                                f"{explanation} | {recommendation}",
                                "pending_legal", db.now_iso(),
                            ),
                        )
                        db.insert_audit_entry(
                            agent="conflict_detector",
                            action="contradiction detected",
                            detail={
                                "map_1": existing_map_id,
                                "map_2": map_id,
                                "explanation": explanation,
                            },
                            map_id=map_id,
                        )
                        conflicts_found += 1

                    elif conflict_type == "C":
                        # Overlap — merge into one unified MAP
                        merge_text = (
                            f"Action 1: {doc}\n\nAction 2: {action_desc}"
                        )
                        merge_resp = llm.invoke([
                            SystemMessage(content=MERGE_PROMPT),
                            HumanMessage(content=merge_text),
                        ])
                        merged = _parse_conflict_response(merge_resp.content)

                        if merged and "action_description" in merged:
                            merged_id = db.new_id()
                            now = db.now_iso()
                            
                            # Clean and validate department to prevent CHECK constraint failures
                            dept = str(merged.get("department", "")).strip()
                            valid_depts = {"IT", "Compliance", "Legal", "HR", "Risk", "Operations"}
                            if dept not in valid_depts:
                                matched_dept = None
                                for valid_d in valid_depts:
                                    if valid_d.lower() in dept.lower() or dept.lower() in valid_d.lower():
                                        matched_dept = valid_d
                                        break
                                dept = matched_dept or map_row["department"]

                            # Clean and validate priority
                            priority = str(merged.get("priority", "")).strip().lower()
                            if priority not in {"high", "medium", "low"}:
                                if "high" in priority:
                                    priority = "high"
                                elif "med" in priority:
                                    priority = "medium"
                                elif "low" in priority:
                                    priority = "low"
                                else:
                                    priority = map_row["priority"]

                            # Clean and validate deadline_days
                            deadline_days = merged.get("deadline_days", map_row["deadline_days"])
                            try:
                                deadline_days = int(str(deadline_days).strip())
                                if deadline_days <= 0:
                                    deadline_days = 90
                            except Exception:
                                deadline_days = map_row["deadline_days"] or 90

                            from datetime import datetime, timedelta, timezone
                            deadline_date = (
                                datetime.now(timezone.utc) + timedelta(days=deadline_days)
                            ).isoformat()

                            db.execute(
                                """INSERT INTO maps
                                   (id, regulation_id, action_description, department,
                                    priority, deadline_days, deadline_date,
                                    compliance_metric, verification_method,
                                    status, conflict_detail, created_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    merged_id, map_row["regulation_id"],
                                    merged["action_description"],
                                    dept, priority, deadline_days, deadline_date,
                                    merged.get("compliance_metric", map_row["compliance_metric"]),
                                    merged.get("verification_method", map_row["verification_method"]),
                                    "pending",
                                    f"Merged from {existing_map_id} and {map_id}",
                                    now,
                                ),
                            )
                            # Archive both originals
                            db.execute(
                                "UPDATE maps SET status = 'superseded', conflict_flag = 1, "
                                "conflict_type = 'C' WHERE id = ?",
                                (existing_map_id,),
                            )
                            db.execute(
                                "UPDATE maps SET status = 'superseded', conflict_flag = 1, "
                                "conflict_type = 'C' WHERE id = ?",
                                (map_id,),
                            )

                            # Record in conflicts table
                            conflict_id = db.new_id()
                            db.execute(
                                """INSERT INTO conflicts
                                   (id, map_id_1, map_id_2, conflict_type, description,
                                    resolution, resolved_at, created_at)
                                   VALUES (?,?,?,?,?,?,?,?)""",
                                (
                                    conflict_id, existing_map_id, map_id, "C",
                                    explanation, "merged", now, now,
                                ),
                            )
                            db.insert_audit_entry(
                                agent="conflict_detector",
                                action="MAPs merged",
                                detail={
                                    "merged_from": [existing_map_id, map_id],
                                    "merged_id": merged_id,
                                },
                                map_id=merged_id,
                            )
                        conflicts_found += 1

                    # else: "none" — no conflict, unrelated

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
