"""
db.py — SQLite Database Manager with Cryptographic Audit Chain
Manages all 5 tables: regulations, maps, evidence, conflicts, audit_log.
Provides thread-safe connection access and blockchain-like hash integrity.
"""

import sqlite3
import uuid
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

DB_PATH = Path("./data/compliance.db")
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection with WAL mode for concurrency."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Module-level shared connection (protected by lock for writes)
# ---------------------------------------------------------------------------
_conn: Optional[sqlite3.Connection] = None


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


def init_db():
    """Create all tables if they do not exist."""
    conn = get_connection()
    with _lock:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS regulations (
            id TEXT PRIMARY KEY,
            title TEXT,
            source TEXT CHECK(source IN ('RBI','SEBI','IRDAI')),
            url TEXT,
            raw_text TEXT,
            doc_hash TEXT UNIQUE,
            ingested_at TEXT,
            status TEXT DEFAULT 'new' CHECK(status IN ('new','processing','processed','resolved'))
        );

        CREATE TABLE IF NOT EXISTS maps (
            id TEXT PRIMARY KEY,
            regulation_id TEXT REFERENCES regulations(id),
            action_description TEXT,
            department TEXT CHECK(department IN ('IT','Compliance','Legal','HR','Risk','Operations')),
            priority TEXT CHECK(priority IN ('high','medium','low')),
            deadline_days INTEGER,
            deadline_date TEXT,
            compliance_metric TEXT,
            verification_method TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN (
                'pending','assigned','in_progress','partial','completed',
                'failed','overdue','conflict','superseded'
            )),
            attack_techniques TEXT,
            conflict_flag INTEGER DEFAULT 0,
            conflict_type TEXT CHECK(conflict_type IN ('A','B','C') OR conflict_type IS NULL),
            conflict_detail TEXT,
            routing_reason TEXT,
            parent_map_id TEXT REFERENCES maps(id),
            assigned_at TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY,
            map_id TEXT REFERENCES maps(id),
            submitted_by TEXT,
            evidence_text TEXT,
            evidence_file_path TEXT,
            submitted_at TEXT,
            validation_status TEXT DEFAULT 'pending' CHECK(validation_status IN ('pending','pass','partial','fail')),
            validation_verdict TEXT,
            criteria_scores TEXT,
            gap_analysis TEXT,
            validation_confidence TEXT DEFAULT 'medium',
            pre_analysis TEXT
        );

        CREATE TABLE IF NOT EXISTS conflicts (
            id TEXT PRIMARY KEY,
            map_id_1 TEXT REFERENCES maps(id),
            map_id_2 TEXT REFERENCES maps(id),
            conflict_type TEXT CHECK(conflict_type IN ('A','B','C')),
            description TEXT,
            resolution TEXT DEFAULT 'pending_legal' CHECK(resolution IN ('auto_resolved','merged','pending_legal','resolved')),
            legal_assigned_at TEXT,
            resolved_at TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            regulation_id TEXT,
            map_id TEXT,
            agent TEXT CHECK(agent IN (
                'ingestion','extractor','router','validator','supervisor',
                'conflict_detector','threat_mapper','risk_engine'
            )),
            action TEXT,
            detail TEXT,
            prev_hash TEXT,
            entry_hash TEXT,
            timestamp TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_regulations_status ON regulations(status);
        CREATE INDEX IF NOT EXISTS idx_maps_status ON maps(status);
        CREATE INDEX IF NOT EXISTS idx_maps_regulation_id ON maps(regulation_id);
        CREATE INDEX IF NOT EXISTS idx_maps_parent_map_id ON maps(parent_map_id);
        CREATE INDEX IF NOT EXISTS idx_maps_department ON maps(department);
        CREATE INDEX IF NOT EXISTS idx_evidence_map_id ON evidence(map_id);
        CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
        """)
        conn.commit()

        # Schema migration: Add validation_confidence to evidence table if not present
        try:
            conn.execute("ALTER TABLE evidence ADD COLUMN validation_confidence TEXT DEFAULT 'medium'")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Schema migration: track explicit human/legal intervention for Type B conflicts
        try:
            conn.execute("ALTER TABLE conflicts ADD COLUMN legal_assigned_at TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Schema migration: Add evidence_file_path for file-based evidence uploads
        try:
            conn.execute("ALTER TABLE evidence ADD COLUMN evidence_file_path TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Schema migration: Add pre_analysis for structured validation pre-analysis
        try:
            conn.execute("ALTER TABLE evidence ADD COLUMN pre_analysis TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# UUID helper
# ---------------------------------------------------------------------------
def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Audit Log — Blockchain-style hash chaining
# ---------------------------------------------------------------------------
def _compute_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _get_latest_audit_hash(conn: sqlite3.Connection) -> str:
    """Get the entry_hash of the most recent audit log row (genesis = '0'*64)."""
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row["entry_hash"] if row else "0" * 64


def insert_audit_entry(
    agent: str,
    action: str,
    detail: Any = None,
    regulation_id: Optional[str] = None,
    map_id: Optional[str] = None,
) -> str:
    """Insert an audit log entry with cryptographic hash chain link."""
    conn = get_connection()
    with _lock:
        entry_id = new_id()
        ts = now_iso()
        detail_json = json.dumps(detail) if detail else "{}"
        prev_hash = _get_latest_audit_hash(conn)
        raw = f"{entry_id}|{regulation_id}|{map_id}|{agent}|{action}|{detail_json}|{ts}|{prev_hash}"
        entry_hash = _compute_hash(raw)
        conn.execute(
            """INSERT INTO audit_log
               (id, regulation_id, map_id, agent, action, detail,
                prev_hash, entry_hash, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (entry_id, regulation_id, map_id, agent, action, detail_json,
             prev_hash, entry_hash, ts),
        )
        conn.commit()
    return entry_id


def verify_audit_chain() -> dict:
    """Walk the full audit log and verify SHA-256 chain integrity.
    Returns {'valid': bool, 'total': int, 'broken_at': int | None}."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY timestamp ASC"
    ).fetchall()
    if not rows:
        return {"valid": True, "total": 0, "broken_at": None}

    expected_prev = "0" * 64
    for idx, row in enumerate(rows):
        if row["prev_hash"] != expected_prev:
            return {"valid": False, "total": len(rows), "broken_at": idx}
        raw = (
            f"{row['id']}|{row['regulation_id']}|{row['map_id']}|"
            f"{row['agent']}|{row['action']}|{row['detail']}|"
            f"{row['timestamp']}|{row['prev_hash']}"
        )
        recomputed = _compute_hash(raw)
        if recomputed != row["entry_hash"]:
            return {"valid": False, "total": len(rows), "broken_at": idx}
        expected_prev = row["entry_hash"]
    return {"valid": True, "total": len(rows), "broken_at": None}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def fetch_all(query: str, params: tuple = ()) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def fetch_one(query: str, params: tuple = ()) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def execute(query: str, params: tuple = ()):
    conn = get_connection()
    with _lock:
        conn.execute(query, params)
        conn.commit()


def execute_many(query: str, params_list: list[tuple]):
    conn = get_connection()
    with _lock:
        conn.executemany(query, params_list)
        conn.commit()
