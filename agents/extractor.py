"""
Agent 2 — MAP Extraction Agent
Uses ChatOllama (qwen3:8b) to extract compliance actions from regulatory text.
Validates with Pydantic. Saves MAPs with status='pending'.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, field_validator
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage

import db

logger = logging.getLogger("agents.extractor")

VALID_DEPARTMENTS = {"IT", "Compliance", "Legal", "HR", "Risk", "Operations"}
VALID_PRIORITIES = {"high", "medium", "low"}

SYSTEM_PROMPT = (
    "You are a senior banking compliance expert with 20 years of experience "
    "reading RBI, SEBI, and IRDAI regulatory circulars in India. Your job is "
    "to read a regulatory circular and extract every action the bank must take. "
    "For each action return a JSON object with exactly these 6 fields: "
    "action_description (specific concrete task), "
    "department (one of: IT/Compliance/Legal/HR/Risk/Operations), "
    "priority (high/medium/low), "
    "deadline_days (integer, estimate if not stated: high=30 medium=60 low=90), "
    "compliance_metric (how to measure if done), "
    "verification_method (what proof is required). "
    "Return ONLY a valid JSON array. No explanation. No markdown. No extra text."
)


# ── Pydantic model for validating each extracted MAP ─────────────
class ComplianceAction(BaseModel):
    action_description: str
    department: str
    priority: str
    deadline_days: int
    compliance_metric: str
    verification_method: str

    @field_validator("action_description")
    @classmethod
    def action_min_length(cls, v: str) -> str:
        if len(v.strip()) < 20:
            raise ValueError("action_description must be at least 20 characters")
        return v.strip()

    @field_validator("department")
    @classmethod
    def valid_department(cls, v: str) -> str:
        if v not in VALID_DEPARTMENTS:
            raise ValueError(f"department must be one of {VALID_DEPARTMENTS}")
        return v

    @field_validator("priority")
    @classmethod
    def valid_priority(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {VALID_PRIORITIES}")
        return v

    @field_validator("deadline_days")
    @classmethod
    def positive_deadline(cls, v: int) -> int:
        if v <= 0:
            return 90  # default fallback
        return v


def _parse_llm_json(raw: str) -> list[dict]:
    """Robustly extract a JSON array from LLM output, handling markdown fences and thinking tags."""
    # Remove <think>...</think> blocks that qwen3 may emit
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Remove markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"```", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # Sometimes the LLM wraps in {"actions": [...]}
            for key in ("actions", "data", "results", "items"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Try to find embedded JSON array
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.error("Could not parse LLM output as JSON array: %s", cleaned[:500])
    return []


def run_extractor(regulation_ids: list = None, batch_size: int = 5) -> dict:
    """Process regulations with status='new' (or specific IDs) in batches. Returns summary."""
    if regulation_ids is not None:
        if not regulation_ids:
            logger.info("No regulations specified for extractor")
            return {"processed": 0, "maps_created": 0, "skipped": 0, "remaining": 0}
        placeholders = ",".join(["?"] * len(regulation_ids))
        regulations = db.fetch_all(
            f"SELECT * FROM regulations WHERE id IN ({placeholders})",
            tuple(regulation_ids),
        )
    else:
        regulations = db.fetch_all(
            "SELECT * FROM regulations WHERE status = 'new' LIMIT ?",
            (batch_size,),
        )
    if not regulations:
        logger.info("No new regulations to process")
        return {"processed": 0, "maps_created": 0, "skipped": 0}

    # 120-second timeout prevents Ollama from hanging indefinitely
    llm = ChatOllama(model="qwen3:8b", temperature=0.1, timeout=120)

    total_maps = 0
    processed_count = 0
    skipped_count = 0

    for idx, reg in enumerate(regulations):
        reg_id = reg["id"]
        raw_text = reg["raw_text"] or ""

        # Skip regulations with very short text (non-regulatory content)
        if len(raw_text.strip()) < 100:
            logger.warning(
                "Skipping regulation %s (text too short: %d chars): %s",
                reg_id[:8], len(raw_text), reg["title"][:60],
            )
            skipped_count += 1
            continue

        # Truncate very long texts to avoid context window overflow
        if len(raw_text) > 12000:
            raw_text = raw_text[:12000] + "\n\n[TEXT TRUNCATED]"

        try:
            logger.info(
                "[%d/%d] Extracting MAPs from regulation: %s",
                idx + 1, len(regulations), reg["title"][:60],
            )
            response = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=raw_text),
            ])

            items = _parse_llm_json(response.content)
            valid_count = 0

            for item in items:
                try:
                    action = ComplianceAction(**item)
                except Exception as exc:
                    logger.warning("Validation failed for item: %s — %s", item, exc)
                    continue

                map_id = db.new_id()
                now = db.now_iso()
                deadline_date = (
                    datetime.now(timezone.utc) + timedelta(days=action.deadline_days)
                ).isoformat()

                db.execute(
                    """INSERT INTO maps
                       (id, regulation_id, action_description, department, priority,
                        deadline_days, deadline_date, compliance_metric, verification_method,
                        status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        map_id, reg_id, action.action_description, action.department,
                        action.priority, action.deadline_days, deadline_date,
                        action.compliance_metric, action.verification_method,
                        "pending", now,
                    ),
                )
                db.insert_audit_entry(
                    agent="extractor",
                    action="MAP created",
                    detail={
                        "action": action.action_description[:100],
                        "department": action.department,
                        "priority": action.priority,
                    },
                    regulation_id=reg_id,
                    map_id=map_id,
                )
                valid_count += 1

            total_maps += valid_count

            # Update regulation status
            new_status = "processing" if valid_count > 0 else "processed"
            db.execute(
                "UPDATE regulations SET status = ? WHERE id = ?",
                (new_status, reg_id),
            )
            db.insert_audit_entry(
                agent="extractor",
                action="extraction complete",
                detail={"maps_extracted": valid_count, "total_items": len(items)},
                regulation_id=reg_id,
            )
            processed_count += 1
            logger.info(
                "[%d/%d] Done — %d MAPs extracted from: %s",
                idx + 1, len(regulations), valid_count, reg["title"][:60],
            )

        except Exception as exc:
            logger.error("Extraction failed for regulation %s: %s", reg_id, exc)
            db.insert_audit_entry(
                agent="extractor",
                action="extraction failed",
                detail={"error": str(exc)},
                regulation_id=reg_id,
            )

    remaining = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM regulations WHERE status = 'new'"
    )
    remaining_count = remaining["cnt"] if remaining else 0

    summary = {
        "processed": processed_count,
        "maps_created": total_maps,
        "skipped": skipped_count,
        "remaining": remaining_count,
    }
    logger.info("Extractor complete: %s", summary)
    return summary
