"""
main.py — FastAPI Backend Server
Hosts all REST endpoints, APScheduler jobs, and CORS setup.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Body, UploadFile, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

import db
from agents.ingestion import run_ingestion, ingest_manual_upload
from agents.extractor import run_extractor
from agents.validator import validate_evidence
from agents.supervisor import run_pipeline, scheduled_supervisor_run
from agents.risk_score import compute_risk_scores
from agents.conflict import run_conflict_detector
from agents.router import run_router
from agents.threat_mapper import run_threat_mapper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("main")

# ── APScheduler ──────────────────────────────────────────────────
scheduler = BackgroundScheduler()


def execute_pipeline_for_regulations(regulation_ids: list):
    """Orchestrates the pipeline chain for each regulation:
    For each regulation:
      extractor -> router -> conflict_detector -> threat_mapper.
    Router runs BEFORE conflict detector so MAPs move to in_progress quickly.
    Conflict detection is slow (embedding + LLM per MAP) and should not block routing.
    Catches errors at each stage, logs them to the audit trail, and continues.
    """
    if not regulation_ids:
        return
    logger.info("Starting pipeline chain for %d regulations", len(regulation_ids))

    for reg_id in regulation_ids:
        logger.info("Processing regulation ID: %s", reg_id)
        
        # 1. Extractor — extract MAPs from the regulation text
        regulation = db.fetch_one("SELECT id, status FROM regulations WHERE id = ?", (reg_id,))
        if not regulation:
            logger.warning("Regulation %s not found, skipping", reg_id)
            continue

        if regulation["status"] == "new":
            try:
                extractor_summary = run_extractor(regulation_ids=[reg_id])
                logger.info("Extractor complete for regulation %s: %s", reg_id, extractor_summary)
            except Exception as exc:
                logger.error("Extractor failed for regulation %s: %s", reg_id, exc)
                try:
                    db.insert_audit_entry(
                        agent="extractor",
                        action="extractor failed",
                        detail={"error": str(exc)},
                        regulation_id=reg_id,
                    )
                except Exception as audit_exc:
                    logger.error("Failed to write extractor error to audit log: %s", audit_exc)
                continue
        else:
            logger.info(
                "Regulation %s is %s; skipping extraction and routing existing pending MAPs",
                reg_id,
                regulation["status"],
            )

        # 2. Retrieve newly extracted MAPs for this regulation
        new_map_ids = []
        try:
            new_maps = db.fetch_all(
                "SELECT id FROM maps WHERE regulation_id = ? AND status = 'pending'",
                (reg_id,)
            )
            new_map_ids = [m["id"] for m in new_maps]
        except Exception as exc:
            logger.error("Failed to query newly extracted MAPs for regulation %s: %s", reg_id, exc)
            try:
                db.insert_audit_entry(
                    agent="supervisor",
                    action="chain_error",
                    detail={"error": f"Failed to query newly extracted MAPs: {exc}"},
                    regulation_id=reg_id,
                )
            except Exception as audit_exc:
                logger.error("Failed to write query error to audit log: %s", audit_exc)

        if not new_map_ids:
            logger.info("No pending MAPs to route for regulation %s, skipping", reg_id)
            continue

        logger.info("Regulation %s: %d pending MAPs found, starting router catch-up...", reg_id, len(new_map_ids))

        # 3. Router FIRST — assign departments and move to in_progress immediately
        #    This runs before conflict detection so MAPs don't sit in pending for hours.
        try:
            router_summary = run_router()
            logger.info("Router complete for regulation %s: %s", reg_id, router_summary)
        except Exception as exc:
            logger.error("Router failed for regulation %s: %s", reg_id, exc)
            try:
                db.insert_audit_entry(
                    agent="router",
                    action="router failed",
                    detail={"error": str(exc)},
                    regulation_id=reg_id,
                )
            except Exception as audit_exc:
                logger.error("Failed to write router error to audit log: %s", audit_exc)

        # 4. Conflict Detector — check for regulatory conflicts among routed MAPs
        #    Uses embeddings + LLM comparisons (slow, 3-8 min per MAP).
        #    Now queries all MAPs for this regulation regardless of status.
        all_reg_map_ids = []
        try:
            all_reg_maps = db.fetch_all(
                "SELECT id FROM maps WHERE regulation_id = ? AND status NOT IN ('superseded', 'conflict')",
                (reg_id,)
            )
            all_reg_map_ids = [m["id"] for m in all_reg_maps]
        except Exception as exc:
            logger.error("Failed to query MAPs for conflict detection for regulation %s: %s", reg_id, exc)

        if all_reg_map_ids:
            try:
                conflict_summary = run_conflict_detector(map_ids=all_reg_map_ids)
                logger.info("Conflict detector complete for regulation %s: %s", reg_id, conflict_summary)
            except Exception as exc:
                logger.error("Conflict detector failed for regulation %s: %s", reg_id, exc)
                try:
                    db.insert_audit_entry(
                        agent="conflict_detector",
                        action="conflict detector failed",
                        detail={"error": str(exc)},
                        regulation_id=reg_id,
                    )
                except Exception as audit_exc:
                    logger.error("Failed to write conflict detector error to audit log: %s", audit_exc)

        # 5. Threat Mapper on the routed MAPs (status = 'in_progress', empty attack_techniques)
        assigned_map_ids = []
        try:
            assigned_maps = db.fetch_all(
                """SELECT id FROM maps 
                   WHERE regulation_id = ? 
                   AND status = 'in_progress' 
                   AND (attack_techniques IS NULL OR attack_techniques = '')""",
                (reg_id,)
            )
            assigned_map_ids = [m["id"] for m in assigned_maps]
        except Exception as exc:
            logger.error("Failed to query MAPs for threat mapping for regulation %s: %s", reg_id, exc)
            try:
                db.insert_audit_entry(
                    agent="supervisor",
                    action="chain_error",
                    detail={"error": f"Failed to query assigned MAPs: {exc}"},
                    regulation_id=reg_id,
                )
            except Exception as audit_exc:
                logger.error("Failed to write query error to audit log: %s", audit_exc)

        if assigned_map_ids:
            try:
                threat_summary = run_threat_mapper(map_ids=assigned_map_ids)
                logger.info("Threat mapper complete for regulation %s: %s", reg_id, threat_summary)
            except Exception as exc:
                logger.error("Threat mapper failed for regulation %s: %s", reg_id, exc)
                try:
                    db.insert_audit_entry(
                        agent="threat_mapper",
                        action="threat mapper failed",
                        detail={"error": str(exc)},
                        regulation_id=reg_id,
                    )
                except Exception as audit_exc:
                    logger.error("Failed to write threat mapper error to audit log: %s", audit_exc)

        # Update regulation status to 'processed' since the pipeline completed
        try:
            db.execute("UPDATE regulations SET status = 'processed' WHERE id = ?", (reg_id,))
            db.insert_audit_entry(
                agent="supervisor",
                action="pipeline complete",
                detail={"status": "processed"},
                regulation_id=reg_id,
            )
            logger.info("Regulation %s status updated to 'processed'", reg_id)
        except Exception as exc:
            logger.error("Failed to update regulation %s status to processed: %s", reg_id, exc)

    logger.info("Pipeline chain complete for %d regulations", len(regulation_ids))


def run_ingestion_chain(run_fetch: bool = True):
    """Runs ingestion and triggers the downstream pipeline if new circulars are found or existing ones are new."""
    logger.info("Ingestion Chain starting (run_fetch=%s)", run_fetch)
    
    if run_fetch:
        try:
            summary = run_ingestion(
                email_host=os.getenv("EMAIL_HOST", ""),
                email_port=int(os.getenv("EMAIL_PORT", "993")),
                email_user=os.getenv("EMAIL_USER", ""),
                email_password=os.getenv("EMAIL_PASSWORD", ""),
                email_folder=os.getenv("EMAIL_FOLDER", "INBOX"),
            )
            total = summary.get("total", 0)
            logger.info("Ingestion agent completed. Found %d new regulations.", total)
        except Exception as exc:
            logger.error("Ingestion agent failed: %s", exc)
            try:
                db.insert_audit_entry(
                    agent="ingestion",
                    action="ingestion failed",
                    detail={"error": str(exc)},
                )
            except Exception as audit_exc:
                logger.error("Failed to write ingestion error to audit log: %s", audit_exc)

    # Always process new regulations and catch up regulations that still have pending MAPs.
    try:
        new_regs = db.fetch_all(
            """SELECT DISTINCT r.id
               FROM regulations r
               LEFT JOIN maps m ON m.regulation_id = r.id AND m.status = 'pending'
               WHERE r.status = 'new' OR m.id IS NOT NULL"""
        )
        new_reg_ids = [r["id"] for r in new_regs]
    except Exception as exc:
        logger.error("Failed to query regulations needing processing: %s", exc)
        new_reg_ids = []

    if new_reg_ids:
        logger.info("Found %d regulations needing pipeline processing. Triggering pipeline chain.", len(new_reg_ids))
        execute_pipeline_for_regulations(new_reg_ids)
    else:
        logger.info("No new regulations or pending MAPs found.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    db.init_db()
    logger.info("Database initialized")

    # Ingestion interval configurable, defaults to 24 hours (1440 minutes)
    ingestion_interval = int(os.getenv("INGESTION_INTERVAL_MINUTES", "1440"))
    # Supervisor interval defaults to 30 minutes
    supervisor_interval = int(os.getenv("SUPERVISOR_INTERVAL_MINUTES", "30"))

    # Schedule the jobs using APScheduler
    scheduler.add_job(run_ingestion_chain, "interval", minutes=ingestion_interval, id="ingestion_job")
    scheduler.add_job(scheduled_supervisor_run, "interval", minutes=supervisor_interval, id="supervisor_job")
    scheduler.start()
    logger.info("Scheduler started — ingestion every %dm, supervisor every %dm", ingestion_interval, supervisor_interval)

    # Run once immediately on startup in a background thread
    import threading
    def startup_run():
        # 1. Process any regulations stuck in 'processing' with pending MAPs
        #    (These had extraction done but router/conflict/threat never ran)
        try:
            stuck_regs = db.fetch_all(
                """SELECT DISTINCT r.id FROM regulations r
                   JOIN maps m ON m.regulation_id = r.id
                   WHERE r.status = 'processing' AND m.status = 'pending'"""
            )
            stuck_ids = [r["id"] for r in stuck_regs]
            if stuck_ids:
                logger.info("Startup recovery: found %d stuck regulations with pending MAPs, resuming pipeline...", len(stuck_ids))
                # For stuck regs, MAPs are already extracted — run router + conflict + threat only
                for reg_id in stuck_ids:
                    logger.info("Recovering regulation %s — routing pending MAPs...", reg_id)
                    pending = db.fetch_all(
                        "SELECT id FROM maps WHERE regulation_id = ? AND status = 'pending'",
                        (reg_id,)
                    )
                    pending_ids = [m["id"] for m in pending]
                    if not pending_ids:
                        continue

                    # Router
                    try:
                        rsummary = run_router(map_ids=pending_ids)
                        logger.info("Recovery router for %s: %s", reg_id, rsummary)
                    except Exception as exc:
                        logger.error("Recovery router failed for %s: %s", reg_id, exc)

                    # Conflict detector
                    try:
                        all_maps = db.fetch_all(
                            "SELECT id FROM maps WHERE regulation_id = ? AND status NOT IN ('superseded','conflict')",
                            (reg_id,)
                        )
                        if all_maps:
                            csummary = run_conflict_detector(map_ids=[m["id"] for m in all_maps])
                            logger.info("Recovery conflict detector for %s: %s", reg_id, csummary)
                    except Exception as exc:
                        logger.error("Recovery conflict detector failed for %s: %s", reg_id, exc)

                    # Threat mapper
                    try:
                        routed = db.fetch_all(
                            """SELECT id FROM maps WHERE regulation_id = ?
                               AND status = 'in_progress'
                               AND (attack_techniques IS NULL OR attack_techniques = '')""",
                            (reg_id,)
                        )
                        if routed:
                            tsummary = run_threat_mapper(map_ids=[m["id"] for m in routed])
                            logger.info("Recovery threat mapper for %s: %s", reg_id, tsummary)
                    except Exception as exc:
                        logger.error("Recovery threat mapper failed for %s: %s", reg_id, exc)

                    # Mark as processed
                    try:
                        db.execute("UPDATE regulations SET status = 'processed' WHERE id = ?", (reg_id,))
                        logger.info("Recovery: regulation %s marked as processed", reg_id)
                    except Exception as exc:
                        logger.error("Failed to update regulation %s status: %s", reg_id, exc)

                logger.info("Startup recovery complete for %d stuck regulations", len(stuck_ids))
        except Exception as exc:
            logger.error("Startup recovery failed: %s", exc)

        # 2. Process regulations with status = 'new'
        logger.info("Immediate startup run: processing existing new regulations...")
        run_ingestion_chain(run_fetch=False)
        logger.info("Immediate startup run: running ingestion fetch...")
        run_ingestion_chain(run_fetch=True)
        logger.info("Immediate startup run complete.")

    threading.Thread(target=startup_run, daemon=True).start()
    threading.Thread(target=scheduled_supervisor_run, daemon=True).start()
    logger.info("Triggered initial runs of ingestion chain and supervisor in background threads")

    yield

    scheduler.shutdown()
    logger.info("Scheduler stopped")


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

    reg_id = result.get("regulation_id")
    if reg_id:
        try:
            execute_pipeline_for_regulations([reg_id])
            result["pipeline"] = {"status": "success", "regulation_id": reg_id}
        except Exception as exc:
            logger.error("Pipeline failed after upload: %s", exc)
            result["pipeline_error"] = str(exc)

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
import threading

_pipeline_status = {"running": False, "last_result": None, "last_error": None}

def _run_pipeline_background():
    """Run ingestion and compliance chain in background thread for 10 minutes."""
    import time
    start_time = time.time()
    end_time = start_time + (10 * 60)  # 10 minutes

    try:
        while time.time() < end_time:
            logger.info("Running pipeline chain (time remaining: %.1f mins)", (end_time - time.time()) / 60.0)
            run_ingestion_chain()
            
            # Sleep briefly before next ingestion check to avoid IMAP spam
            # We break early if we exceed the 10 minute mark during sleep
            for _ in range(30):
                if time.time() >= end_time:
                    break
                time.sleep(1)
                
        _pipeline_status["last_result"] = {
            "status": "completed",
            "message": "Ingestion and compliance pipeline chain finished successfully after 10 minutes."
        }
        _pipeline_status["last_error"] = None
        logger.info("Background pipeline chain completed successfully")
    except Exception as exc:
        logger.error("Background pipeline chain failed: %s", exc)
        _pipeline_status["last_result"] = None
        _pipeline_status["last_error"] = str(exc)
    finally:
        _pipeline_status["running"] = False


@app.post("/api/pipeline/trigger")
def trigger_pipeline():
    """Trigger the ingestion and compliance chain in background."""
    if _pipeline_status["running"]:
        return {"status": "already_running", "message": "Pipeline is already running in background"}

    _pipeline_status["running"] = True
    _pipeline_status["last_result"] = None
    _pipeline_status["last_error"] = None
    thread = threading.Thread(target=_run_pipeline_background, daemon=True)
    thread.start()
    return {"status": "started", "message": "Pipeline chain started in background"}


@app.get("/api/pipeline/status")
def get_pipeline_status():
    """Check pipeline execution status."""
    return _pipeline_status


# =====================================================================
# INGESTION TRIGGER
# =====================================================================
@app.post("/api/ingestion/trigger")
def trigger_ingestion():
    """Manually trigger the ingestion and compliance chain in background."""
    if _pipeline_status["running"]:
        return {"status": "already_running", "message": "Pipeline/ingestion is already running in background"}

    _pipeline_status["running"] = True
    _pipeline_status["last_result"] = None
    _pipeline_status["last_error"] = None
    thread = threading.Thread(target=_run_pipeline_background, daemon=True)
    thread.start()
    return {"status": "started", "message": "Ingestion and compliance chain started in background"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
