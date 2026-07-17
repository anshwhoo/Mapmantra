"""
Risk Score Engine — Pure arithmetic, no LLM.
Calculates organization-wide and per-department risk scores,
plus financial penalty exposure ranges.
"""

import logging
from datetime import datetime, timezone

import db

logger = logging.getLogger("agents.risk_score")

# ── Penalty ranges per regulator (in Crores) ─────────────────────
PENALTY_RANGES = {
    "RBI":  {"min_cr": 2,    "max_cr": 50},
    "SEBI": {"min_cr": 1,    "max_cr": 25},
    "IRDAI": {"min_cr": 0.5, "max_cr": 5},
}

DEPARTMENTS = ["IT", "Compliance", "Legal", "HR", "Risk", "Operations"]


def _count(query: str, params: tuple = ()) -> int:
    row = db.fetch_one(f"SELECT COUNT(*) as cnt FROM ({query})", params)
    return row["cnt"] if row else 0


def _calculate_score(
    overdue_high: int,
    overdue_med: int,
    overdue_low: int,
    conflicts_unresolved: int,
    pending_high: int,
    completed_this_month: int,
) -> int:
    """Apply the exact formula and clamp between 0 and 100."""
    score = (
        overdue_high * 12
        + overdue_med * 6
        + overdue_low * 3
        + conflicts_unresolved * 8
        + pending_high * 3
        - completed_this_month * 2
    )
    return max(0, min(100, score))


def compute_risk_scores(write_audit: bool = False) -> dict:
    """Compute global and per-department risk scores plus financial exposure.

    Returns:
        {
            "global_score": int,
            "departments": {"IT": {"score": int, "overdue": int, ...}, ...},
            "financial_exposure": {"min_cr": float, "max_cr": float},
            "breakdown": { ... raw counts ... },
        }
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    # Current month boundaries
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    # ── Global counts ────────────────────────────────────────────
    overdue_high = _count(
        "SELECT id FROM maps WHERE status = 'overdue' AND priority = 'high'"
    )
    overdue_med = _count(
        "SELECT id FROM maps WHERE status = 'overdue' AND priority = 'medium'"
    )
    overdue_low = _count(
        "SELECT id FROM maps WHERE status = 'overdue' AND priority = 'low'"
    )
    conflicts_unresolved = _count(
        "SELECT id FROM conflicts WHERE resolution = 'pending_legal'"
    )
    pending_high = _count(
        "SELECT id FROM maps WHERE status IN ('pending','assigned','in_progress') AND priority = 'high'"
    )
    completed_this_month = _count(
        "SELECT id FROM maps WHERE status = 'completed' AND assigned_at >= ?",
        (month_start,),
    )

    global_score = _calculate_score(
        overdue_high, overdue_med, overdue_low,
        conflicts_unresolved, pending_high, completed_this_month,
    )

    breakdown = {
        "overdue_high": overdue_high,
        "overdue_medium": overdue_med,
        "overdue_low": overdue_low,
        "conflicts_unresolved": conflicts_unresolved,
        "pending_high": pending_high,
        "completed_this_month": completed_this_month,
    }

    # ── Per-department scores ────────────────────────────────────
    dept_scores = {}
    for dept in DEPARTMENTS:
        d_oh = _count(
            "SELECT id FROM maps WHERE status='overdue' AND priority='high' AND department=?",
            (dept,),
        )
        d_om = _count(
            "SELECT id FROM maps WHERE status='overdue' AND priority='medium' AND department=?",
            (dept,),
        )
        d_ol = _count(
            "SELECT id FROM maps WHERE status='overdue' AND priority='low' AND department=?",
            (dept,),
        )
        d_ph = _count(
            "SELECT id FROM maps WHERE status IN ('pending','assigned','in_progress') AND priority='high' AND department=?",
            (dept,),
        )
        d_cm = _count(
            "SELECT id FROM maps WHERE status='completed' AND department=? AND assigned_at >= ?",
            (dept, month_start),
        )
        # Department-level conflicts: count conflicts where either MAP belongs to this dept
        d_conflicts = _count(
            """SELECT c.id FROM conflicts c
               JOIN maps m1 ON c.map_id_1 = m1.id
               JOIN maps m2 ON c.map_id_2 = m2.id
               WHERE c.resolution = 'pending_legal'
               AND (m1.department = ? OR m2.department = ?)""",
            (dept, dept),
        )

        d_score = _calculate_score(d_oh, d_om, d_ol, d_conflicts, d_ph, d_cm)

        # Additional stats for the department
        d_total = _count("SELECT id FROM maps WHERE department = ?", (dept,))
        d_overdue_total = _count(
            "SELECT id FROM maps WHERE department = ? AND status = 'overdue'", (dept,)
        )
        d_in_progress = _count(
            "SELECT id FROM maps WHERE department = ? AND status = 'in_progress'", (dept,)
        )
        d_completed = _count(
            "SELECT id FROM maps WHERE department = ? AND status = 'completed'", (dept,)
        )

        dept_scores[dept] = {
            "score": d_score,
            "total": d_total,
            "overdue": d_overdue_total,
            "in_progress": d_in_progress,
            "completed": d_completed,
            "completed_this_month": d_cm,
        }

    # ── Financial exposure ───────────────────────────────────────
    total_min_cr = 0.0
    total_max_cr = 0.0

    overdue_maps = db.fetch_all(
        """SELECT m.id, r.source
           FROM maps m
           JOIN regulations r ON m.regulation_id = r.id
           WHERE m.status = 'overdue'"""
    )
    for om in overdue_maps:
        source = om.get("source", "RBI")
        ranges = PENALTY_RANGES.get(source, PENALTY_RANGES["RBI"])
        total_min_cr += ranges["min_cr"]
        total_max_cr += ranges["max_cr"]

    financial_exposure = {
        "min_cr": round(total_min_cr, 2),
        "max_cr": round(total_max_cr, 2),
        "overdue_count": len(overdue_maps),
    }

    # Write audit entry
    if write_audit:
        db.insert_audit_entry(
            agent="risk_engine",
            action="risk score calculated",
            detail={
                "global_score": global_score,
                "department_scores": {k: v["score"] for k, v in dept_scores.items()},
                "financial_exposure": financial_exposure,
            },
        )

    result = {
        "global_score": global_score,
        "departments": dept_scores,
        "financial_exposure": financial_exposure,
        "breakdown": breakdown,
    }
    logger.info("Risk score calculated: global=%d", global_score)
    return result
