"""
Agent 5 — Threat Intelligence Mapper
Uses ChatOllama (qwen3:8b) to map compliance actions to MITRE ATT&CK techniques.
"""

import json
import logging
import re

from agents.llm import chat_llm
from langchain_core.messages import SystemMessage, HumanMessage

import db

logger = logging.getLogger("agents.threat_mapper")

SYSTEM_PROMPT = (
    "You are a cybersecurity expert specializing in MITRE ATT&CK. "
    "Read this banking compliance action and identify which ATT&CK technique IDs "
    "it defends against if implemented. "
    'Return JSON: {"attack_techniques": ["T1078", "T1190"], '
    '"technique_names": ["Valid Accounts", "Exploit Public-Facing Application"], '
    '"explanation": "one sentence"}. '
    "Return empty array if none genuinely apply."
)


def _normalize_threat_result(parsed) -> dict:
    """Models sometimes answer with a bare list (e.g. `[]` when no technique applies,
    or `["T1078"]`) instead of the requested object; treat it as the technique list."""
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        techniques, names = [], []
        for item in parsed:
            if isinstance(item, str):
                techniques.append(item)
            elif isinstance(item, dict):
                tid = item.get("technique_id") or item.get("id") or item.get("attack_technique")
                if tid:
                    techniques.append(str(tid))
                if item.get("technique_name") or item.get("name"):
                    names.append(item.get("technique_name") or item.get("name"))
        return {"attack_techniques": techniques, "technique_names": names, "explanation": ""}
    return {}


def _parse_threat_response(raw: str) -> dict:
    """Parse threat mapping from LLM output."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"```", "", cleaned)
    cleaned = cleaned.strip()

    try:
        return _normalize_threat_result(json.loads(cleaned))
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}


def run_threat_mapper(map_ids: list = None) -> dict:
    """Map in-progress MAPs (without attack_techniques) to MITRE ATT&CK."""
    if map_ids is not None:
        if not map_ids:
            logger.info("No MAPs specified for threat mapper")
            return {"mapped": 0}
        placeholders = ",".join(["?"] * len(map_ids))
        maps_to_process = db.fetch_all(
            f"""SELECT * FROM maps
               WHERE status = 'in_progress'
               AND (attack_techniques IS NULL OR attack_techniques = '')
               AND id IN ({placeholders})""",
            tuple(map_ids)
        )
    else:
        maps_to_process = db.fetch_all(
            """SELECT * FROM maps
               WHERE status = 'in_progress'
               AND (attack_techniques IS NULL OR attack_techniques = '')"""
        )

    if not maps_to_process:
        logger.info("No MAPs to map to MITRE ATT&CK")
        return {"mapped": 0}

    llm = chat_llm()
    mapped = 0

    for map_row in maps_to_process:
        map_id = map_row["id"]
        action_desc = map_row["action_description"]

        try:
            response = llm.invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=action_desc),
            ])

            result = _parse_threat_response(response.content)
            techniques = result.get("attack_techniques", [])
            technique_names = result.get("technique_names", [])
            explanation = result.get("explanation", "")

            # Store as comma-separated string
            techniques_str = ",".join(techniques) if techniques else ""

            db.execute(
                "UPDATE maps SET attack_techniques = ? WHERE id = ?",
                (techniques_str, map_id),
            )

            db.insert_audit_entry(
                agent="threat_mapper",
                action="ATT&CK mapped",
                detail={
                    "techniques": techniques,
                    "technique_names": technique_names,
                    "explanation": explanation,
                },
                regulation_id=map_row["regulation_id"],
                map_id=map_id,
            )
            mapped += 1

            logger.info(
                "MAP %s → ATT&CK: %s",
                map_id[:8],
                techniques_str or "(none)",
            )

        except Exception as exc:
            logger.error("Threat mapping failed for MAP %s: %s", map_id, exc)
            db.insert_audit_entry(
                agent="threat_mapper",
                action="threat_mapping_failed",
                detail={"error": str(exc)},
                map_id=map_id,
            )

    summary = {"mapped": mapped}
    logger.info("Threat mapper complete: %s", summary)
    return summary
