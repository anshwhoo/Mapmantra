"""
main.py — FastAPI Backend Server
Hosts all REST endpoints, the concurrent pipeline workers, and CORS setup.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Body, UploadFile, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

import db
from agents.ingestion import ingest_manual_upload
from agents.validator import validate_evidence
from agents.risk_score import compute_risk_scores
from agents.router import run_router
from agents import workers

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle: every agent runs as its own concurrent worker."""
    db.init_db()
    logger.info("Database initialized")
    workers.start_workers()

    yield

    workers.stop_workers()
    logger.info("Workers stopped")


from fastapi.responses import HTMLResponse

app = FastAPI(
    title="MapMantra — AI Compliance System",
    description="Multi-agent regulatory compliance intelligence for Indian banks",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
def read_index():
    """Serve the premium React glassmorphism dashboard."""
    index_path = Path(__file__).parent / "frontend" / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>MapMantra UI index.html not found</h1>", status_code=404)



# =====================================================================
# DASHBOARD ENDPOINTS
# =====================================================================
@app.get("/api/dashboard")
def get_dashboard():
    """Aggregated dashboard summary."""
    risk = compute_risk_scores()

    total_maps = db.fetch_one("SELECT COUNT(*) as cnt FROM maps")["cnt"]
    pending = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='pending'")["cnt"]
    assigned = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='assigned'")["cnt"]
    in_progress = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='in_progress'")["cnt"]
    completed = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='completed'")["cnt"]
    overdue = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='overdue'")["cnt"]
    partial = db.fetch_one("SELECT COUNT(*) as cnt FROM maps WHERE status='partial'")["cnt"]
    conflicts = db.fetch_one("SELECT COUNT(*) as cnt FROM conflicts WHERE resolution='pending_legal'")["cnt"]

    # Circulars this week
    from datetime import datetime, timedelta, timezone
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    circulars_week = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM regulations WHERE ingested_at >= ?", (week_ago,)
    )["cnt"]
    new_circulars = db.fetch_one("SELECT COUNT(*) as cnt FROM regulations WHERE status='new'")["cnt"]

    # Recent activity (last 10 audit entries)
    recent = db.fetch_all(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 10"
    )

    return {
        "risk": risk,
        "maps": {
            "total": total_maps,
            "pending": pending,
            "assigned": assigned,
            "in_progress": in_progress,
            "completed": completed,
            "overdue": overdue,
            "partial": partial,
        },
        "conflicts": conflicts,
        "circulars_this_week": circulars_week,
        "new_circulars": new_circulars,
        "recent_activity": recent,
    }


# =====================================================================
# REGULATIONS ENDPOINTS
# =====================================================================
@app.get("/api/regulations")
def list_regulations(
    source: str = Query(None, description="Filter by source: RBI/SEBI/IRDAI"),
    status: str = Query(None, description="Filter by status"),
    search: str = Query(None, description="Search in title"),
):
    """List all regulations with optional filters."""
    query = "SELECT id, title, source, url, doc_hash, ingested_at, status FROM regulations WHERE 1=1"
    params = []
    if source:
        query += " AND source = ?"
        params.append(source)
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND title LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY ingested_at DESC"
    return db.fetch_all(query, tuple(params))


@app.get("/api/regulations/{regulation_id}")
def get_regulation(regulation_id: str):
    """Get full regulation details including its MAPs."""
    reg = db.fetch_one("SELECT * FROM regulations WHERE id = ?", (regulation_id,))
    if not reg:
        raise HTTPException(status_code=404, detail="Regulation not found")

    maps = db.fetch_all(
        "SELECT * FROM maps WHERE regulation_id = ? ORDER BY created_at",
        (regulation_id,),
    )
    return {"regulation": reg, "maps": maps}


@app.post("/api/regulations/upload")
async def upload_regulation(
    file: UploadFile = File(...),
    source: str = Form(...),
    title: str = Form(...),
    url: str = Form(""),
):
    """Manual PDF upload (Option 4). Triggers full pipeline after ingestion."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    pdf_bytes = await file.read()
    result = ingest_manual_upload(pdf_bytes, source, title, url)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    if result.get("regulation_id"):
        workers.prioritize_regulation(result["regulation_id"])
        result["pipeline"] = {"status": "queued", "regulation_id": result["regulation_id"]}

    return result


# =====================================================================
# MAPS ENDPOINTS
# =====================================================================
@app.get("/api/maps")
def list_maps(
    department: str = Query(None),
    priority: str = Query(None),
    status: str = Query(None),
    search: str = Query(None),
):
    """List MAPs with optional filters."""
    query = "SELECT * FROM maps WHERE 1=1"
    params = []
    if department:
        query += " AND department = ?"
        params.append(department)
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND action_description LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY created_at DESC"
    return db.fetch_all(query, tuple(params))


@app.get("/api/maps/{map_id}")
def get_map_detail(map_id: str):
    """Get full MAP detail with evidence history and siblings."""
    map_row = db.fetch_one("SELECT * FROM maps WHERE id = ?", (map_id,))
    if not map_row:
        raise HTTPException(status_code=404, detail="MAP not found")

    evidence = db.fetch_all(
        "SELECT * FROM evidence WHERE map_id = ? ORDER BY submitted_at DESC",
        (map_id,),
    )

    # Get sibling MAPs
    parent_id = map_row.get("parent_map_id") or map_id
    siblings = db.fetch_all(
        """SELECT * FROM maps
           WHERE (parent_map_id = ? OR id = ?)
           AND id != ?
           ORDER BY department""",
        (parent_id, parent_id, map_id),
    )

    # Get source regulation
    regulation = None
    if map_row.get("regulation_id"):
        regulation = db.fetch_one(
            "SELECT id, title, source, url, ingested_at FROM regulations WHERE id = ?",
            (map_row["regulation_id"],),
        )

    return {
        "map": map_row,
        "evidence_history": evidence,
        "siblings": siblings,
        "regulation": regulation,
    }


@app.post("/api/maps/{map_id}/submit-evidence")
async def submit_evidence(
    request: Request,
    map_id: str,
):
    """Submit evidence for a MAP — triggers Validation Agent.
    Accepts multipart/form-data with optional file upload, or JSON body.
    Fields: evidence_text (str), submitted_by (str), evidence_file (UploadFile, optional)."""
    evidence_text = None
    submitted_by = "Dashboard User"
    evidence_file_path = None

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            evidence_text = body.get("evidence_text")
            submitted_by = body.get("submitted_by", "Dashboard User")
        except Exception:
            pass
    else:
        try:
            form_data = await request.form()
            evidence_text = form_data.get("evidence_text", "")
            submitted_by = form_data.get("submitted_by", "Dashboard User")

            # Handle file upload
            evidence_file = form_data.get("evidence_file")
            if evidence_file and hasattr(evidence_file, 'read'):
                try:
                    import uuid as _uuid
                    evidence_dir = Path("./data/evidence")
                    evidence_dir.mkdir(parents=True, exist_ok=True)

                    # Sanitize filename and add unique prefix
                    original_name = getattr(evidence_file, 'filename', 'upload')
                    safe_name = "".join(c for c in original_name if c.isalnum() or c in "._- ")
                    unique_name = f"{_uuid.uuid4().hex[:8]}_{safe_name}"
                    file_path = evidence_dir / unique_name

                    file_bytes = await evidence_file.read()
                    with open(file_path, "wb") as f:
                        f.write(file_bytes)

                    evidence_file_path = str(file_path)
                    logger.info("Evidence file saved: %s (%d bytes)", evidence_file_path, len(file_bytes))
                except Exception as file_exc:
                    logger.error("Failed to save evidence file: %s", file_exc)
        except Exception:
            pass

    # Allow submission with just a file (no text required if file is present)
    if not evidence_text and not evidence_file_path:
        raise HTTPException(status_code=422, detail="evidence_text or evidence_file is required")

    if not evidence_text:
        evidence_text = "[Evidence submitted as file upload — see attached document]"

    map_row = db.fetch_one("SELECT id FROM maps WHERE id = ?", (map_id,))
    if not map_row:
        raise HTTPException(status_code=404, detail="MAP not found")

    from fastapi.concurrency import run_in_threadpool
    result = await run_in_threadpool(
        validate_evidence, map_id, evidence_text, submitted_by, evidence_file_path
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# =====================================================================
# CONFLICTS ENDPOINTS
# =====================================================================
@app.get("/api/conflicts")
@app.get("/conflicts")
def list_conflicts(resolution: str = Query(None)):
    """List all conflicts, optionally filtered by resolution status."""
    query = """
        SELECT c.*,
               m1.action_description as map_1_action,
               m1.department as map_1_dept,
               m2.action_description as map_2_action,
               m2.department as map_2_dept
        FROM conflicts c
        JOIN maps m1 ON c.map_id_1 = m1.id
        JOIN maps m2 ON c.map_id_2 = m2.id
        WHERE 1=1
    """
    params = []
    if resolution:
        query += " AND c.resolution = ?"
        params.append(resolution)
    query += " ORDER BY c.created_at DESC"
    return db.fetch_all(query, tuple(params))


@app.post("/api/conflicts/{conflict_id}/assign-legal")
@app.post("/conflicts/{conflict_id}/assign-legal")
def assign_conflict_to_legal(conflict_id: str):
    """Assign a conflict to the legal team for resolution."""
    conflict = db.fetch_one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    db.execute(
        """UPDATE conflicts
           SET resolution = 'pending_legal', legal_assigned_at = ?, resolved_at = NULL
           WHERE id = ?""",
        (db.now_iso(), conflict_id),
    )
    db.insert_audit_entry(
        agent="supervisor",
        action="conflict assigned to legal",
        detail={"conflict_id": conflict_id},
    )
    return {"status": "assigned", "conflict_id": conflict_id}


@app.post("/api/conflicts/{conflict_id}/resolve")
@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str):
    """Mark a conflict as resolved."""
    conflict = db.fetch_one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))
    if not conflict:
        raise HTTPException(status_code=404, detail="Conflict not found")

    if conflict["conflict_type"] == "B" and not conflict.get("legal_assigned_at"):
        raise HTTPException(
            status_code=409,
            detail="Type B conflicts require legal assignment before resolution.",
        )

    db.execute(
        "UPDATE conflicts SET resolution = 'resolved', resolved_at = ? WHERE id = ?",
        (db.now_iso(), conflict_id),
    )

    affected_map_ids = [conflict["map_id_1"], conflict["map_id_2"]]
    if conflict["conflict_type"] == "B":
        for mid in affected_map_ids:
            db.execute(
                """UPDATE maps
                   SET status = 'pending',
                       conflict_flag = 0,
                       conflict_type = NULL,
                       conflict_detail = 'Resolved by human/legal review; returned to router'
                   WHERE id = ?""",
                (mid,),
            )
        router_summary = run_router(map_ids=affected_map_ids)
    else:
        router_summary = None
        for mid in affected_map_ids:
            current = db.fetch_one("SELECT status FROM maps WHERE id = ?", (mid,))
            if current and current["status"] == "conflict":
                db.execute(
                    "UPDATE maps SET status = 'in_progress', conflict_flag = 0 WHERE id = ?",
                    (mid,),
                )

    db.insert_audit_entry(
        agent="supervisor",
        action="conflict resolved",
        detail={
            "conflict_id": conflict_id,
            "routed_after_human_review": conflict["conflict_type"] == "B",
            "router_summary": router_summary,
        },
    )
    return {"status": "resolved", "router": router_summary}


# =====================================================================
# THREAT INTEL ENDPOINTS
# =====================================================================
@app.get("/api/threat-intel")
def get_threat_intel():
    """Get MITRE ATT&CK technique coverage summary."""
    maps_with_techniques = db.fetch_all(
        "SELECT id, action_description, department, status, attack_techniques, deadline_date "
        "FROM maps WHERE attack_techniques IS NOT NULL AND attack_techniques != ''"
    )

    technique_map: dict = {}
    for m in maps_with_techniques:
        for tech_id in m["attack_techniques"].split(","):
            tech_id = tech_id.strip()
            if not tech_id:
                continue
            if tech_id not in technique_map:
                technique_map[tech_id] = {"open": 0, "completed": 0, "maps": []}
            if m["status"] == "completed":
                technique_map[tech_id]["completed"] += 1
            else:
                technique_map[tech_id]["open"] += 1
            technique_map[tech_id]["maps"].append({
                "id": m["id"],
                "action": m["action_description"][:100],
                "department": m["department"],
                "status": m["status"],
                "deadline_date": m["deadline_date"],
            })

    techniques = []
    for tech_id, data in sorted(technique_map.items()):
        total = data["open"] + data["completed"]
        coverage = data["completed"] / total if total > 0 else 0
        techniques.append({
            "technique_id": tech_id,
            "open_maps": data["open"],
            "completed_maps": data["completed"],
            "coverage": round(coverage, 2),
            "maps": data["maps"],
        })

    # Find most exposed and unmitigated
    most_exposed = max(techniques, key=lambda x: x["open_maps"]) if techniques else None
    unmitigated = sum(1 for t in techniques if t["completed_maps"] == 0)

    return {
        "techniques": techniques,
        "most_exposed": most_exposed,
        "unmitigated_count": unmitigated,
        "total_techniques": len(techniques),
    }


# =====================================================================
# AUDIT TRAIL ENDPOINTS
# =====================================================================
@app.get("/api/audit/logs")
def get_audit_logs(
    agent: str = Query(None),
    regulation_id: str = Query(None),
    limit: int = Query(100, le=1000),
):
    """Get audit log entries with optional filters."""
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if agent:
        query += " AND agent = ?"
        params.append(agent)
    if regulation_id:
        query += " AND regulation_id = ?"
        params.append(regulation_id)
    query += f" ORDER BY timestamp DESC LIMIT {limit}"
    return db.fetch_all(query, tuple(params))


@app.get("/api/audit/verify")
def verify_audit_chain():
    """Verify cryptographic integrity of the audit log chain."""
    return db.verify_audit_chain()


# =====================================================================
# RISK REPORT ENDPOINT
# =====================================================================
@app.get("/api/risk-report")
def get_risk_report():
    """Full risk report with breakdown, department scores, and financial exposure."""
    return compute_risk_scores()


# =====================================================================
# PIPELINE TRIGGER
# =====================================================================
def _trigger_ingestion_now() -> dict:
    """Run an ingestion pass now; the other workers are always running and pick up its output."""
    if workers.is_busy("ingestion"):
        return {"status": "already_running", "message": "Ingestion is already running"}
    workers.wake("ingestion")
    return {"status": "started", "message": "Ingestion started; all pipeline workers are running concurrently"}


@app.post("/api/pipeline/trigger")
def trigger_pipeline():
    """Trigger an ingestion pass now."""
    return _trigger_ingestion_now()


@app.get("/api/pipeline/status")
def get_pipeline_status():
    """Live state of every pipeline worker plus queue depths.
    `running` / `last_error` describe the ingestion worker (used by the dashboard's Run Pipeline button)."""
    status = workers.worker_status()
    ingestion = status.get("ingestion", {})
    return {
        "running": ingestion.get("state") == "busy",
        "last_result": {"finished": ingestion.get("last_finished")},
        "last_error": ingestion.get("last_error"),
        "workers": status,
        "queues": workers.queue_depths(),
    }


# =====================================================================
# INGESTION TRIGGER
# =====================================================================
@app.post("/api/ingestion/trigger")
def trigger_ingestion():
    """Manually trigger an ingestion pass now."""
    return _trigger_ingestion_now()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
