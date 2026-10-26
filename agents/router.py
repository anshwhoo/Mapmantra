"""
Agent 3 — Routing Agent
Uses ChatOllama (qwen3:8b) to determine department ownership.
Implements sibling MAP logic with parent_map_id.
"""

import json
import logging
import re

from agents.llm import chat_llm
from langchain_core.messages import SystemMessage, HumanMessage

import db

logger = logging.getLogger("agents.router")

VALID_DEPARTMENTS = {"IT", "Compliance", "Legal", "HR", "Risk", "Operations"}

SYSTEM_PROMPT = (
    "You are a banking operations expert. Read the compliance action and determine "
    "which bank department(s) should own it. "
    "Departments: IT, Compliance, Legal, HR, Risk, Operations. "
    "Assign to ALL departments with real responsibility. "
    'Return JSON: {"departments": ["IT", "Compliance"], '
    '"routing_reason": {"IT": "one sentence why", "Compliance": "one sentence why"}}'
)


def _parse_routing_response(raw: str) -> dict:
    """Parse the LLM routing response robustly."""
    # Remove <think>...</think> blocks
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"```", "", cleaned)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "departments" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    # Try to find embedded JSON object
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if "departments" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    logger.error("Could not parse routing response: %s", cleaned[:300])
    return {}


def _route_to_existing_department(map_row: dict, reason: str) -> bool:
    """Fallback route when extraction already assigned a valid owner."""
    department = map_row.get("department")
    if department not in VALID_DEPARTMENTS:
        return False

    now = db.now_iso()
    routing_reason = map_row.get("routing_reason") or reason
    db.execute(
        """UPDATE maps
           SET status = 'in_progress', assigned_at = ?, routing_reason = ?
           WHERE id = ? AND status = 'pending'""",
        (now, routing_reason, map_row["id"]),
    )
    db.insert_audit_entry(
        agent="router",
        action="MAP routed by fallback",
        detail={
            "department": department,
            "reason": routing_reason,
        },
        regulation_id=map_row.get("regulation_id"),
        map_id=map_row["id"],
    )
    return True


def run_router(map_ids: list = None) -> dict:
    """Route all MAPs with status='pending' (or specific IDs) to departments using sibling logic."""
    if map_ids is not None:
        if not map_ids:
            logger.info("No MAPs specified for router")
            return {"routed": 0, "siblings_created": 0}
        placeholders = ",".join(["?"] * len(map_ids))
        pending_maps = db.fetch_all(
            f"SELECT * FROM maps WHERE status = 'pending' AND id IN ({placeholders})",
            tuple(map_ids)
        )
    else:
        pending_maps = db.fetch_all("SELECT * FROM maps WHERE status = 'pending'")
    if not pending_maps:
        logger.info("No pending MAPs to route")
        return {"routed": 0, "siblings_created": 0}

    llm = chat_llm()
    routed = 0
    siblings_created = 0
    fallback_routed = 0

    for map_row in pending_maps:
        map_id = map_row["id"]
        action_desc = map_row["action_description"]

        try:
            response = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=action_desc),
            ])

            result = _parse_routing_response(response.content)
            if not result or "departments" not in result:
                logger.warning("No valid departments returned for MAP %s", map_id)
                if _route_to_existing_department(
                    map_row,
                    "Routed to the department assigned during extraction because router returned no valid departments.",
                ):
                    routed += 1
                    fallback_routed += 1
                continue

            departments = [
                d for d in result["departments"]
                if d in VALID_DEPARTMENTS
            ]
            if not departments:
                logger.warning("No valid departments after filtering for MAP %s", map_id)
                if _route_to_existing_department(
                    map_row,
                    "Routed to the department assigned during extraction because router returned invalid departments.",
                ):
                    routed += 1
                    fallback_routed += 1
                continue

            routing_reason = result.get("routing_reason", {})
            now = db.now_iso()

            # ── Sibling MAP Logic ──
            # First department: update the original MAP in place
            primary_dept = departments[0]
            primary_reason = routing_reason.get(primary_dept, "")

            db.execute(
                """UPDATE maps
                   SET department = ?, status = 'in_progress', assigned_at = ?,
                       routing_reason = ?
                   WHERE id = ?""",
                (primary_dept, now, primary_reason, map_id),
            )

            db.insert_audit_entry(
                agent="router",
                action="MAP routed",
                detail={
                    "primary_department": primary_dept,
                    "all_departments": departments,
                    "routing_reason": routing_reason,
                },
                regulation_id=map_row["regulation_id"],
                map_id=map_id,
            )
            routed += 1

            # Remaining departments: create sibling MAPs with parent_map_id
            for dept in departments[1:]:
                sibling_id = db.new_id()
                dept_reason = routing_reason.get(dept, "")

                db.execute(
                    """INSERT INTO maps
                       (id, regulation_id, action_description, department, priority,
                        deadline_days, deadline_date, compliance_metric, verification_method,
                        status, attack_techniques, conflict_flag, conflict_type,
                        conflict_detail, routing_reason, parent_map_id, assigned_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sibling_id,
                        map_row["regulation_id"],
                        map_row["action_description"],
                        dept,
                        map_row["priority"],
                        map_row["deadline_days"],
                        map_row["deadline_date"],
                        map_row["compliance_metric"],
                        map_row["verification_method"],
                        "in_progress",
                        map_row["attack_techniques"],
                        map_row["conflict_flag"],
                        map_row["conflict_type"],
                        map_row["conflict_detail"],
                        dept_reason,
                        map_id,  # parent_map_id points to original
                        now,
                        db.now_iso(),
                    ),
                )

                db.insert_audit_entry(
                    agent="router",
                    action="sibling MAP created",
                    detail={
                        "sibling_id": sibling_id,
                        "parent_map_id": map_id,
                        "department": dept,
                        "reason": dept_reason,
                    },
                    regulation_id=map_row["regulation_id"],
                    map_id=sibling_id,
                )
                siblings_created += 1

            logger.info(
                "Routed MAP %s → %s (+ %d siblings)",
                map_id[:8], primary_dept, len(departments) - 1,
            )

        except Exception as exc:
            logger.error("Routing failed for MAP %s: %s", map_id, exc)
            if _route_to_existing_department(
                map_row,
                f"Routed to the department assigned during extraction after router error: {exc}",
            ):
                routed += 1
                fallback_routed += 1
                continue
            db.insert_audit_entry(
                agent="router",
                action="routing failed",
                detail={"error": str(exc)},
                map_id=map_id,
            )

    summary = {
        "routed": routed,
        "siblings_created": siblings_created,
        "fallback_routed": fallback_routed,
    }
    logger.info("Router complete: %s", summary)
    return summary
