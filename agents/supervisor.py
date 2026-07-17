"""
Agent 7 — Supervisor Agent
Uses LangGraph StateGraph to orchestrate the full pipeline:
  extract → conflict_detect → route → threat_map → supervise → risk_score
Also performs periodic maintenance checks (overdue, stuck partial, unresolved conflicts).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import StateGraph, END

import db
from agents.extractor import run_extractor
from agents.conflict import run_conflict_detector
from agents.router import run_router
from agents.threat_mapper import run_threat_mapper
from agents.risk_score import compute_risk_scores

logger = logging.getLogger("agents.supervisor")


# ── LangGraph State ──────────────────────────────────────────────
class PipelineState(TypedDict):
    extraction_result: dict
    conflict_result: dict
    routing_result: dict
    threat_result: dict
    supervision_result: dict
    risk_result: dict


# ── Node functions ───────────────────────────────────────────────
def extract_node(state: PipelineState) -> PipelineState:
    """Run MAP extraction on all new regulations."""
    logger.info("Pipeline node: extract")
    result = run_extractor()
    state["extraction_result"] = result
    return state


def conflict_detect_node(state: PipelineState) -> PipelineState:
    """Run conflict detection on newly extracted MAPs."""
    logger.info("Pipeline node: conflict_detect")
    result = run_conflict_detector()
    state["conflict_result"] = result
    return state


def route_node(state: PipelineState) -> PipelineState:
    """Route pending MAPs to departments."""
    logger.info("Pipeline node: route")
    result = run_router()
    state["routing_result"] = result
    return state


def threat_map_node(state: PipelineState) -> PipelineState:
    """Map assigned MAPs to MITRE ATT&CK techniques."""
    logger.info("Pipeline node: threat_map")
    result = run_threat_mapper()
    state["threat_result"] = result
    return state


def supervise_node(state: PipelineState) -> PipelineState:
    """Run maintenance supervision checks."""
    logger.info("Pipeline node: supervise")
    result = _run_supervision_checks()
    state["supervision_result"] = result
    return state


def risk_score_node(state: PipelineState) -> PipelineState:
    """Calculate risk scores after supervision."""
    logger.info("Pipeline node: risk_score")
    result = compute_risk_scores(write_audit=True)
    state["risk_result"] = result
    return state


# ── Build the LangGraph pipeline ────────────────────────────────
def build_pipeline() -> StateGraph:
    """Construct the LangGraph StateGraph for the full compliance pipeline."""
    workflow = StateGraph(PipelineState)

    workflow.add_node("extract", extract_node)
    workflow.add_node("conflict_detect", conflict_detect_node)
    workflow.add_node("route", route_node)
    workflow.add_node("threat_map", threat_map_node)
    workflow.add_node("supervise", supervise_node)
    workflow.add_node("risk_score", risk_score_node)

    workflow.set_entry_point("extract")
    workflow.add_edge("extract", "conflict_detect")
    workflow.add_edge("conflict_detect", "route")
    workflow.add_edge("route", "threat_map")
    workflow.add_edge("threat_map", "supervise")
    workflow.add_edge("supervise", "risk_score")
    workflow.add_edge("risk_score", END)

    return workflow.compile()


def run_pipeline() -> dict:
    """Execute the full LangGraph pipeline end-to-end."""
    logger.info("Starting full compliance pipeline")
    pipeline = build_pipeline()

    initial_state: PipelineState = {
        "extraction_result": {},
        "conflict_result": {},
        "routing_result": {},
        "threat_result": {},
        "supervision_result": {},
        "risk_result": {},
    }

    final_state = pipeline.invoke(initial_state)

    db.insert_audit_entry(
        agent="supervisor",
        action="pipeline completed",
        detail={
            "extraction": final_state.get("extraction_result", {}),
            "conflicts": final_state.get("conflict_result", {}),
            "routing": final_state.get("routing_result", {}),
            "threats": final_state.get("threat_result", {}),
            "supervision": final_state.get("supervision_result", {}),
            "risk": final_state.get("risk_result", {}).get("global_score"),
        },
    )

    logger.info("Pipeline completed successfully")
    return final_state


# ── Supervision maintenance checks ──────────────────────────────
def _run_supervision_checks() -> dict:
    """Perform periodic maintenance checks:
    - Mark overdue MAPs
    - Flag stuck partial MAPs (>7 days)
    - Escalate unresolved conflicts (>14 days)
    - Resolve fully completed regulations
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    summary = {
        "overdue_marked": 0,
        "partial_stuck": 0,
        "conflicts_escalated": 0,
        "regulations_resolved": 0,
    }

    # ── 1. Mark overdue MAPs ─────────────────────────────────
    overdue_candidates = db.fetch_all(
        """SELECT id, deadline_date, priority FROM maps
           WHERE status NOT IN ('completed', 'superseded', 'overdue', 'conflict')
           AND deadline_date IS NOT NULL
           AND deadline_date < ?""",
        (now_iso,),
    )
    for m in overdue_candidates:
        db.execute("UPDATE maps SET status = 'overdue' WHERE id = ?", (m["id"],))
        db.insert_audit_entry(
            agent="supervisor",
            action="MAP marked overdue",
            detail={
                "map_id": m["id"],
                "deadline_date": m["deadline_date"],
                "priority": m["priority"],
            },
            map_id=m["id"],
        )
        summary["overdue_marked"] += 1

    # ── 2. Flag stuck partial MAPs (>7 days) ─────────────────
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    stuck_partials = db.fetch_all(
        """SELECT m.id FROM maps m
           JOIN evidence e ON e.map_id = m.id
           WHERE m.status = 'partial'
           AND e.submitted_at < ?
           GROUP BY m.id""",
        (seven_days_ago,),
    )
    for sp in stuck_partials:
        db.insert_audit_entry(
            agent="supervisor",
            action="partial MAP reminder",
            detail={"map_id": sp["id"], "stuck_since": seven_days_ago},
            map_id=sp["id"],
        )
        summary["partial_stuck"] += 1

    # ── 3. Escalate unresolved conflicts (>14 days) ──────────
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()
    old_conflicts = db.fetch_all(
        """SELECT id, map_id_1, map_id_2 FROM conflicts
           WHERE resolution = 'pending_legal'
           AND created_at < ?""",
        (fourteen_days_ago,),
    )
    for oc in old_conflicts:
        db.insert_audit_entry(
            agent="supervisor",
            action="conflict escalation",
            detail={
                "conflict_id": oc["id"],
                "map_id_1": oc["map_id_1"],
                "map_id_2": oc["map_id_2"],
                "age_days": 14,
            },
        )
        summary["conflicts_escalated"] += 1

    # ── 4. Resolve fully completed regulations ───────────────
    # A regulation is resolved when ALL its MAPs (including siblings) are completed
    regulations_in_progress = db.fetch_all(
        "SELECT id FROM regulations WHERE status IN ('processing', 'processed')"
    )
    for reg in regulations_in_progress:
        reg_id = reg["id"]
        total = db.fetch_one(
            "SELECT COUNT(*) as cnt FROM maps WHERE regulation_id = ? AND status != 'superseded'",
            (reg_id,),
        )
        completed = db.fetch_one(
            "SELECT COUNT(*) as cnt FROM maps WHERE regulation_id = ? AND status = 'completed'",
            (reg_id,),
        )
        if total and completed:
            if total["cnt"] == 0 or (total["cnt"] > 0 and total["cnt"] == completed["cnt"]):
                db.execute(
                    "UPDATE regulations SET status = 'resolved' WHERE id = ?",
                    (reg_id,),
                )
                db.insert_audit_entry(
                    agent="supervisor",
                    action="regulation resolved",
                    detail={"regulation_id": reg_id, "total_maps": total["cnt"]},
                    regulation_id=reg_id,
                )
                summary["regulations_resolved"] += 1

    logger.info("Supervision checks complete: %s", summary)
    return summary


# ── Scheduled runner (called by APScheduler) ─────────────────────
def scheduled_supervisor_run():
    """Entry point for APScheduler — runs supervision + risk score only."""
    logger.info("Scheduled supervisor run starting")
    try:
        _run_supervision_checks()
    except Exception as exc:
        logger.error("Supervisor checks failed: %s", exc)
        try:
            db.insert_audit_entry(
                agent="supervisor",
                action="supervision failed",
                detail={"error": str(exc)},
            )
        except Exception as audit_exc:
            logger.error("Failed to write supervisor error to audit log: %s", audit_exc)

    try:
        compute_risk_scores(write_audit=True)
    except Exception as exc:
        logger.error("Risk score calculation failed: %s", exc)
        try:
            db.insert_audit_entry(
                agent="risk_score",
                action="risk calculation failed",
                detail={"error": str(exc)},
            )
        except Exception as audit_exc:
            logger.error("Failed to write risk score error to audit log: %s", audit_exc)
    logger.info("Scheduled supervisor run complete")
