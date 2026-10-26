"""
Concurrent Pipeline Workers
Every agent runs in its own thread and pulls work from the database by status,
so ingestion, extraction, routing, conflict detection, threat mapping and
supervision all progress at the same time on different regulations and MAPs.

The extractor takes manual uploads first, then the newest circulars; later
stages work oldest-first so nothing already extracted is starved. Per-item
stage order is kept by the queue queries:
  regulation 'new' → extractor → MAP 'pending' → router → MAP 'in_progress'
  → conflict detector ('conflict_checked') → threat mapper ('ATT&CK mapped')
  → finalizer marks the regulation 'processed'.
Ingestion and the supervisor run on fixed intervals.
"""

import logging
import os
import threading
import time
from typing import Callable

import db
from agents.ingestion import run_ingestion
from agents.extractor import run_extractor
from agents.router import run_router
from agents.conflict import run_conflict_detector
from agents.threat_mapper import run_threat_mapper
from agents.supervisor import scheduled_supervisor_run

logger = logging.getLogger("agents.workers")

QUEUE_POLL_SECONDS = 10
FINALIZER_POLL_SECONDS = 15
RETRY_AFTER_SECONDS = 3600  # items that fail are retried after an hour, not in a hot loop

CONFLICT_CHECKED_IDS = """SELECT map_id FROM audit_log
    WHERE agent = 'conflict_detector' AND action = 'conflict_checked' AND map_id IS NOT NULL"""
THREAT_MAPPED_IDS = """SELECT map_id FROM audit_log
    WHERE agent = 'threat_mapper' AND action = 'ATT&CK mapped' AND map_id IS NOT NULL"""


# ── Manual uploads jump the extraction queue ─────────────────────
_priority_regulations: list[str] = []
_priority_lock = threading.Lock()


def prioritize_regulation(regulation_id: str):
    with _priority_lock:
        _priority_regulations.append(regulation_id)
    wake("extractor")


def _next_priority_regulation() -> str | None:
    with _priority_lock:
        while _priority_regulations:
            reg_id = _priority_regulations.pop(0)
            reg = db.fetch_one("SELECT status FROM regulations WHERE id = ?", (reg_id,))
            if reg and reg["status"] == "new":
                return reg_id
    return None


# ── Retry backoff for items an agent could not finish ────────────
_backoff: dict[str, float] = {}
_backoff_lock = threading.Lock()


def _defer(item_id: str):
    with _backoff_lock:
        _backoff[item_id] = time.time() + RETRY_AFTER_SECONDS


def _deferred_ids() -> list[str]:
    now = time.time()
    with _backoff_lock:
        for item_id in [i for i, t in _backoff.items() if t <= now]:
            del _backoff[item_id]
        return list(_backoff)


def _not_deferred_clause(column: str = "id") -> tuple[str, tuple]:
    ids = _deferred_ids()
    if not ids:
        return "", ()
    return f" AND {column} NOT IN ({','.join('?' * len(ids))})", tuple(ids)


# ── Worker thread ────────────────────────────────────────────────
class Worker(threading.Thread):
    """Runs `step` in a loop. A queue worker repeats immediately while `step`
    reports work done and sleeps `idle_seconds` when its queue is empty; an
    interval worker always sleeps `idle_seconds` between runs. `wake()` cuts
    the sleep short. With run_at_start=False the first run waits for a wake()
    or the first interval."""

    def __init__(self, key: str, step: Callable[[], int], idle_seconds: float,
                 interval: bool = False, run_at_start: bool = True):
        super().__init__(name=f"worker-{key}", daemon=True)
        self.key = key
        self.step = step
        self.idle_seconds = idle_seconds
        self.interval = interval
        self.run_at_start = run_at_start
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self.status = {
            "state": "starting",
            "items_processed": 0,
            "runs": 0,
            "last_started": None,
            "last_finished": None,
            "last_error": None,
        }

    def run(self):
        if not self.run_at_start:
            self.status["state"] = "idle"
            self._wake_event.wait(self.idle_seconds)
            self._wake_event.clear()
        while not self._stop_event.is_set():
            self.status.update(state="busy", last_started=db.now_iso())
            done = 0
            try:
                done = self.step() or 0
                self.status["last_error"] = None
            except Exception as exc:
                logger.exception("Worker %s failed", self.key)
                self.status["last_error"] = str(exc)
            self.status["items_processed"] += done
            self.status["runs"] += 1
            self.status.update(state="idle", last_finished=db.now_iso())

            if done and not self.interval:
                continue
            self._wake_event.wait(self.idle_seconds)
            self._wake_event.clear()
        self.status["state"] = "stopped"

    def wake(self):
        self._wake_event.set()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()


# ── Steps (each returns the number of items it handled) ──────────
def _ingest_step() -> int:
    summary = run_ingestion(
        email_host=os.getenv("EMAIL_HOST", ""),
        email_port=int(os.getenv("EMAIL_PORT", "993")),
        email_user=os.getenv("EMAIL_USER", ""),
        email_password=os.getenv("EMAIL_PASSWORD", ""),
        email_folder=os.getenv("EMAIL_FOLDER", "INBOX"),
    )
    total = summary.get("total", 0)
    logger.info("Ingestion run complete: %d new regulations", total)
    if total:
        wake("extractor")
    return total


def _extract_step() -> int:
    """Extract MAPs from one 'new' regulation: manual uploads first, then newest circulars."""
    reg_id = _next_priority_regulation()
    if not reg_id:
        clause, params = _not_deferred_clause()
        reg = db.fetch_one(
            f"SELECT id FROM regulations WHERE status = 'new'{clause} ORDER BY ingested_at DESC LIMIT 1",
            params,
        )
        if not reg:
            return 0
        reg_id = reg["id"]
    run_extractor(regulation_ids=[reg_id])
    after = db.fetch_one("SELECT status FROM regulations WHERE id = ?", (reg_id,))
    if after and after["status"] == "new":
        _defer(reg_id)  # extraction failed or text too short
    else:
        wake("router")
    return 1


def _route_step() -> int:
    clause, params = _not_deferred_clause()
    rows = db.fetch_all(
        f"SELECT id FROM maps WHERE status = 'pending'{clause} ORDER BY created_at LIMIT 5",
        params,
    )
    if not rows:
        return 0
    map_ids = [r["id"] for r in rows]
    run_router(map_ids=map_ids)
    for r in db.fetch_all(
        f"SELECT id FROM maps WHERE status = 'pending' AND id IN ({','.join('?' * len(map_ids))})",
        tuple(map_ids),
    ):
        _defer(r["id"])
    wake("conflict")
    return len(map_ids)


def _conflict_step() -> int:
    """Conflict-check one routed MAP (slowest stage: embedding + LLM per match).
    Department siblings share their parent's action text, so only parents are checked."""
    clause, params = _not_deferred_clause()
    row = db.fetch_one(
        f"""SELECT id FROM maps
            WHERE status = 'in_progress' AND conflict_flag = 0 AND parent_map_id IS NULL
            AND id NOT IN ({CONFLICT_CHECKED_IDS}){clause}
            ORDER BY created_at LIMIT 1""",
        params,
    )
    if not row:
        return 0
    run_conflict_detector(map_ids=[row["id"]])
    checked = db.fetch_one(
        f"SELECT 1 AS ok FROM ({CONFLICT_CHECKED_IDS}) WHERE map_id = ?", (row["id"],)
    )
    if not checked:
        _defer(row["id"])
    wake("threat")
    return 1


def _threat_step() -> int:
    clause, params = _not_deferred_clause()
    rows = db.fetch_all(
        f"""SELECT id FROM maps
            WHERE status = 'in_progress'
            AND (attack_techniques IS NULL OR attack_techniques = '')
            AND (conflict_flag = 1 OR id IN ({CONFLICT_CHECKED_IDS})
                 OR parent_map_id IN ({CONFLICT_CHECKED_IDS}))
            AND id NOT IN ({THREAT_MAPPED_IDS}){clause}
            ORDER BY created_at LIMIT 3""",
        params,
    )
    if not rows:
        return 0
    map_ids = [r["id"] for r in rows]
    run_threat_mapper(map_ids=map_ids)
    mapped = {
        r["map_id"] for r in db.fetch_all(
            f"SELECT map_id FROM ({THREAT_MAPPED_IDS}) WHERE map_id IN ({','.join('?' * len(map_ids))})",
            tuple(map_ids),
        )
    }
    for map_id in map_ids:
        if map_id not in mapped:
            _defer(map_id)
    return len(map_ids)


def _finalize_step() -> int:
    """Mark regulations 'processed' once none of their MAPs is still in the pipeline."""
    regs = db.fetch_all(
        f"""SELECT r.id FROM regulations r
            WHERE r.status = 'processing'
            AND NOT EXISTS (
                SELECT 1 FROM maps m WHERE m.regulation_id = r.id AND (
                    m.status = 'pending'
                    OR (m.status = 'in_progress' AND m.conflict_flag = 0
                        AND m.parent_map_id IS NULL AND m.id NOT IN ({CONFLICT_CHECKED_IDS}))
                    OR (m.status = 'in_progress'
                        AND (m.attack_techniques IS NULL OR m.attack_techniques = '')
                        AND m.id NOT IN ({THREAT_MAPPED_IDS}))
                )
            )"""
    )
    for reg in regs:
        db.execute("UPDATE regulations SET status = 'processed' WHERE id = ?", (reg["id"],))
        db.insert_audit_entry(
            agent="supervisor",
            action="pipeline complete",
            detail={"status": "processed"},
            regulation_id=reg["id"],
        )
        logger.info("Regulation %s fully processed", reg["id"])
    return 0  # cheap SQL poll; always wait between runs


def _supervise_step() -> int:
    scheduled_supervisor_run()
    return 0


# ── Worker registry ──────────────────────────────────────────────
_workers: dict[str, Worker] = {}


def start_workers():
    if _workers:
        return
    ingestion_minutes = int(os.getenv("INGESTION_INTERVAL_MINUTES", "1440"))
    supervisor_minutes = int(os.getenv("SUPERVISOR_INTERVAL_MINUTES", "30"))
    ingest_at_start = os.getenv("INGESTION_ON_STARTUP", "true").lower() == "true"
    for worker in (
        Worker("ingestion", _ingest_step, ingestion_minutes * 60, interval=True, run_at_start=ingest_at_start),
        Worker("extractor", _extract_step, QUEUE_POLL_SECONDS),
        Worker("router", _route_step, QUEUE_POLL_SECONDS),
        Worker("conflict", _conflict_step, QUEUE_POLL_SECONDS),
        Worker("threat", _threat_step, QUEUE_POLL_SECONDS),
        Worker("finalizer", _finalize_step, FINALIZER_POLL_SECONDS, interval=True),
        Worker("supervisor", _supervise_step, supervisor_minutes * 60, interval=True),
    ):
        _workers[worker.key] = worker
        worker.start()
    logger.info(
        "Started %d concurrent workers (ingestion every %dm, supervisor every %dm)",
        len(_workers), ingestion_minutes, supervisor_minutes,
    )


def stop_workers():
    for worker in _workers.values():
        worker.stop()


def wake(key: str):
    worker = _workers.get(key)
    if worker:
        worker.wake()


def is_busy(key: str) -> bool:
    worker = _workers.get(key)
    return bool(worker and worker.status["state"] == "busy")


def queue_depths() -> dict:
    count = lambda q: db.fetch_one(f"SELECT COUNT(*) AS c FROM ({q})")["c"]
    return {
        "regulations_new": count("SELECT id FROM regulations WHERE status = 'new'"),
        "maps_pending_routing": count("SELECT id FROM maps WHERE status = 'pending'"),
        "maps_pending_conflict_check": count(
            f"""SELECT id FROM maps WHERE status = 'in_progress' AND conflict_flag = 0
                AND parent_map_id IS NULL AND id NOT IN ({CONFLICT_CHECKED_IDS})"""
        ),
        "maps_pending_threat_mapping": count(
            f"""SELECT id FROM maps WHERE status = 'in_progress'
                AND (attack_techniques IS NULL OR attack_techniques = '')
                AND id NOT IN ({THREAT_MAPPED_IDS})"""
        ),
        "regulations_processing": count("SELECT id FROM regulations WHERE status = 'processing'"),
        "deferred_for_retry": len(_deferred_ids()),
    }


def worker_status() -> dict:
    return {key: dict(worker.status) for key, worker in _workers.items()}
