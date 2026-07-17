"""
Agent 6 — Validation Agent (Strengthened)
Multi-stage validation pipeline for MAP compliance evidence.

Pipeline stages:
  1. Evidence Parsing — Extract text from uploaded files (PDF, DOCX, images)
  2. Artifact Detection — Scan for concrete artifacts (dates, IDs, reference numbers)
  3. Compliance Metric Cross-Matching — Compare evidence against MAP requirements
  4. Temporal Validation — Verify dates are consistent with regulation timeline
  5. Red Flag Detection — Flag future-tense language, copy-paste, vague evidence
  6. LLM Deep Analysis — Enhanced 8-criteria evaluation with pre-analysis context

Triggered ONLY when a department submits evidence. Never runs automatically.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage

import db

logger = logging.getLogger("agents.validator")

# ── Evidence file storage ──────────────────────────────────────────
EVIDENCE_DIR = Path("./data/evidence")
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# ── Stage 1: Evidence Text Extraction ──────────────────────────────

def extract_text_from_file(file_path: str) -> str:
    """Extract text content from uploaded evidence files (PDF, DOCX, TXT, images)."""
    if not file_path or not os.path.exists(file_path):
        return ""

    ext = Path(file_path).suffix.lower()
    extracted = ""

    try:
        if ext == ".pdf":
            extracted = _extract_pdf_text(file_path)
        elif ext in (".doc", ".docx"):
            extracted = _extract_docx_text(file_path)
        elif ext in (".txt", ".csv", ".log", ".md"):
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                extracted = f.read()
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff"):
            extracted = _extract_image_text(file_path)
        else:
            # Try reading as plain text
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    extracted = f.read()
            except Exception:
                extracted = f"[Binary file uploaded: {Path(file_path).name}]"
    except Exception as exc:
        logger.warning("Failed to extract text from %s: %s", file_path, exc)
        extracted = f"[File uploaded but text extraction failed: {Path(file_path).name}]"

    return extracted.strip()


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                # Also extract tables if present
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            text_parts.append(" | ".join(str(cell or "") for cell in row))
        return "\n".join(text_parts)
    except ImportError:
        logger.warning("pdfplumber not installed, falling back to basic extraction")
        return f"[PDF file uploaded: {Path(file_path).name}]"


def _extract_docx_text(file_path: str) -> str:
    """Extract text from DOCX files."""
    try:
        from docx import Document
        doc = Document(file_path)
        return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
    except ImportError:
        logger.warning("python-docx not installed")
        return f"[DOCX file uploaded: {Path(file_path).name}]"
    except Exception:
        return f"[DOCX file uploaded but extraction failed: {Path(file_path).name}]"


def _extract_image_text(file_path: str) -> str:
    """Describe image evidence (OCR would require additional dependencies)."""
    try:
        from PIL import Image
        img = Image.open(file_path)
        w, h = img.size
        return (
            f"[Image evidence uploaded: {Path(file_path).name}, "
            f"dimensions: {w}x{h}px, format: {img.format}. "
            f"Image appears to contain documentary evidence. "
            f"Manual visual verification recommended.]"
        )
    except ImportError:
        return f"[Image file uploaded: {Path(file_path).name}]"
    except Exception:
        return f"[Image file uploaded but processing failed: {Path(file_path).name}]"


# ── Stage 2: Artifact Detection ────────────────────────────────────

# Patterns for detecting concrete evidence artifacts
ARTIFACT_PATTERNS = {
    "dates": re.compile(
        r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
        r"|(?:\d{4}[/-]\d{1,2}[/-]\d{1,2})"
        r"|(?:\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"
        r"|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})",
        re.IGNORECASE
    ),
    "reference_numbers": re.compile(
        r"(?:(?:Ref|Reference|Circular|No|Number|ID|File|Order|Notification|Circular\s*No)[.\s:#-]*\s*[A-Z0-9][A-Z0-9./-]{3,30})"
        r"|(?:RBI/\d{4}[-/]\d{2,4}/\d+)"
        r"|(?:SEBI/[A-Z]+/\d+)"
        r"|(?:IRDAI/[A-Z]+/\d+)"
        r"|(?:[A-Z]{2,6}[-/]\d{4,}[-/][A-Z0-9]+)",
        re.IGNORECASE
    ),
    "transaction_ids": re.compile(
        r"(?:(?:Transaction|TXN|Txn|TR|Trans)[.\s:#-]*\s*[A-Z0-9]{6,20})"
        r"|(?:(?:Ticket|Case|Incident|SR|Service\s*Request)[.\s:#-]*\s*[A-Z0-9]{4,15})",
        re.IGNORECASE
    ),
    "approval_signatures": re.compile(
        r"(?:(?:Approved|Signed|Authorized|Certified|Verified|Endorsed)\s+by\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)"
        r"|(?:(?:Chief|Head|Director|VP|AVP|Manager|Officer|Auditor|CISO|CRO|CTO|CFO|CEO|MD)\s*[-:,]\s*[A-Z][a-z]+)"
        r"|(?:(?:Digital\s*Signature|e-?Sign|DSC)\s*(?:of|by)?)",
        re.IGNORECASE
    ),
    "system_paths": re.compile(
        r"(?:(?:https?://)[^\s<>\"']+)"
        r"|(?:[A-Z]:\\(?:[^\s<>\"'\\]+\\)*[^\s<>\"'\\]+)"
        r"|(?:/(?:home|opt|var|usr|etc|data|srv)/[^\s<>\"']+)",
        re.IGNORECASE
    ),
    "monetary_amounts": re.compile(
        r"(?:(?:INR|Rs\.?)\s*[\d,]+(?:\.\d{2})?)"
        r"|(?:[\d,]+(?:\.\d{2})?\s*(?:Crores?|Lakhs?|Cr|L))",
        re.IGNORECASE
    ),
    "audit_reports": re.compile(
        r"(?:(?:Audit|Assessment|Review|Inspection|Examination)\s+(?:Report|Finding|Result|Summary|Certificate|Observation))"
        r"|(?:(?:ISO|SOC|PCI[\s-]DSS|NIST|GDPR|DPDP)\s*[\d.-]*)"
        r"|(?:(?:Internal|External|Statutory|Concurrent)\s+Audit)",
        re.IGNORECASE
    ),
    "metrics_numbers": re.compile(
        r"(?:\d+(?:\.\d+)?%?\s*(?:of|out\s+of|from|across|over))"
        r"|(?:(?:100|[1-9]\d?)%\s*(?:compliance|coverage|completion|adoption|migration))"
        r"|(?:\d+\s*(?:servers?|systems?|applications?|users?|employees?|branches?|departments?)\s+(?:migrated|upgraded|updated|configured|deployed|trained))",
        re.IGNORECASE
    ),
}

# Keywords that indicate future/planned actions (red flags)
FUTURE_TENSE_PATTERNS = re.compile(
    r"(?:we\s+will\s+(?:implement|deploy|configure|migrate|install|update|establish))"
    r"|(?:(?:will\s+be|to\s+be|yet\s+to\s+be)\s+(?:implemented|deployed|configured|completed|done|established|rolled\s+out))"
    r"|(?:(?:plan|planned|planning|intend|proposal|proposed|roadmap|timeline)\s+(?:to|for))"
    r"|(?:(?:expected|estimated|tentative|approximate|target)\s+(?:date|timeline|completion))"
    r"|(?:(?:in\s+progress|under\s+development|work\s+in\s+progress|WIP|TBD|TBA))"
    r"|(?:(?:phase\s*[2-9]|next\s+phase|upcoming|future|forthcoming))",
    re.IGNORECASE
)

# Keywords that indicate action was actually done (positive indicators)
COMPLETION_PATTERNS = re.compile(
    r"(?:(?:successfully|fully)\s+(?:implemented|deployed|configured|migrated|completed|established|installed|updated|upgraded))"
    r"|(?:has\s+been\s+(?:implemented|deployed|configured|completed|done|established|rolled\s+out|approved|certified))"
    r"|(?:(?:completed|finished|done|implemented|deployed|operational|live|active)\s+(?:on|as\s+of|since|from))"
    r"|(?:(?:go[\s-]live|launched|activated|enabled|enforced)\s+(?:on|since|from|date))"
    r"|(?:audit\s+(?:cleared|passed|confirmed|verified|certified))",
    re.IGNORECASE
)


def detect_artifacts(text: str) -> dict:
    """Scan evidence text for concrete artifacts and return structured results."""
    results = {}
    for artifact_type, pattern in ARTIFACT_PATTERNS.items():
        matches = pattern.findall(text)
        # Deduplicate and clean
        cleaned = list(set(m.strip() for m in matches if m.strip()))
        results[artifact_type] = cleaned

    # Count total artifacts found
    total = sum(len(v) for v in results.values())
    results["total_artifacts"] = total

    return results


# ── Stage 3: Compliance Metric Cross-Matching ──────────────────────

def cross_match_compliance(evidence_text: str, map_row: dict) -> dict:
    """Check if evidence content actually addresses the MAP's compliance metric and verification method."""
    metric = (map_row.get("compliance_metric") or "").lower()
    verification = (map_row.get("verification_method") or "").lower()
    action = (map_row.get("action_description") or "").lower()
    evidence_lower = evidence_text.lower()

    # Extract key terms from metric and verification method
    def extract_keywords(text):
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "shall", "should",
            "may", "might", "can", "could", "would", "must", "to", "of", "in",
            "for", "on", "with", "at", "by", "from", "as", "into", "through",
            "during", "before", "after", "above", "below", "between", "and",
            "or", "but", "not", "no", "all", "each", "every", "both", "this",
            "that", "these", "those", "such", "than", "so", "if", "when",
            "ensure", "required", "requirement", "compliance", "compliant",
        }
        words = re.findall(r'\b[a-z]{3,}\b', text)
        return [w for w in words if w not in stop_words]

    metric_keywords = extract_keywords(metric)
    verification_keywords = extract_keywords(verification)
    action_keywords = extract_keywords(action)

    # Count how many keywords from the metric/verification appear in evidence
    metric_matches = [kw for kw in metric_keywords if kw in evidence_lower]
    verification_matches = [kw for kw in verification_keywords if kw in evidence_lower]
    action_matches = [kw for kw in action_keywords if kw in evidence_lower]

    metric_coverage = len(metric_matches) / max(len(metric_keywords), 1)
    verification_coverage = len(verification_matches) / max(len(verification_keywords), 1)
    action_coverage = len(action_matches) / max(len(action_keywords), 1)

    return {
        "metric_keyword_matches": metric_matches,
        "metric_coverage_pct": round(metric_coverage * 100, 1),
        "verification_keyword_matches": verification_matches,
        "verification_coverage_pct": round(verification_coverage * 100, 1),
        "action_keyword_matches": action_matches,
        "action_coverage_pct": round(action_coverage * 100, 1),
        "overall_relevance": round(
            (metric_coverage * 0.4 + verification_coverage * 0.3 + action_coverage * 0.3) * 100, 1
        ),
    }


# ── Stage 4: Temporal Validation ───────────────────────────────────

def validate_temporal(evidence_text: str, map_row: dict) -> dict:
    """Verify dates in evidence are consistent with regulation timeline."""
    deadline_str = map_row.get("deadline_date", "")
    created_str = map_row.get("created_at", "")

    # Parse all dates found in evidence
    date_pattern = re.compile(r'\d{4}-\d{2}-\d{2}')
    evidence_dates = date_pattern.findall(evidence_text)

    # Also try DD/MM/YYYY and MM/DD/YYYY patterns
    alt_dates = re.findall(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', evidence_text)
    for d, m, y in alt_dates:
        try:
            evidence_dates.append(f"{y}-{m.zfill(2)}-{d.zfill(2)}")
        except Exception:
            pass

    result = {
        "dates_found_in_evidence": evidence_dates,
        "deadline": deadline_str,
        "regulation_created": created_str,
        "temporal_issues": [],
        "temporal_ok": True,
    }

    if not evidence_dates:
        result["temporal_issues"].append("No specific dates found in evidence")
        return result

    try:
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00")) if created_str else None
    except Exception:
        created = None

    for date_str in evidence_dates:
        try:
            ev_date = datetime.fromisoformat(date_str)
            # Check if evidence date is in the future (suspicious)
            if ev_date > datetime.now():
                result["temporal_issues"].append(
                    f"Date {date_str} is in the future - evidence may describe planned actions"
                )
                result["temporal_ok"] = False

            # Check if action was done after regulation was issued
            if created and ev_date < created.replace(tzinfo=None):
                result["temporal_issues"].append(
                    f"Date {date_str} is before the regulation was issued ({created_str[:10]}) - suspicious"
                )
                result["temporal_ok"] = False
        except Exception:
            pass

    return result


# ── Stage 5: Red Flag Detection ───────────────────────────────────

def detect_red_flags(evidence_text: str, map_row: dict) -> list:
    """Detect common red flags in evidence submissions."""
    flags = []
    text = evidence_text.strip()

    # 1. Evidence is too short (less than 50 characters)
    if len(text) < 50:
        flags.append({
            "flag": "EVIDENCE_TOO_SHORT",
            "severity": "high",
            "detail": f"Evidence is only {len(text)} characters. Genuine compliance evidence typically requires detailed documentation."
        })

    # 2. Future tense language
    future_matches = FUTURE_TENSE_PATTERNS.findall(text)
    if future_matches:
        flags.append({
            "flag": "FUTURE_TENSE_LANGUAGE",
            "severity": "high",
            "detail": f"Found {len(future_matches)} future-tense phrases suggesting actions are planned, not completed: {', '.join(future_matches[:3])}"
        })

    # 3. Evidence copies the requirement text
    action = (map_row.get("action_description") or "").strip()
    if action and len(action) > 30:
        action_words = set(action.lower().split())
        evidence_words = set(text.lower().split())
        overlap = len(action_words & evidence_words) / max(len(action_words), 1)
        if overlap > 0.7:
            flags.append({
                "flag": "COPY_PASTE_REQUIREMENT",
                "severity": "high",
                "detail": f"Evidence appears to restate the requirement itself ({round(overlap*100)}% word overlap). Evidence should describe what was DONE, not what needs to be done."
            })

    # 4. No completion indicators
    completion_matches = COMPLETION_PATTERNS.findall(text)
    if not completion_matches and len(text) > 100:
        flags.append({
            "flag": "NO_COMPLETION_INDICATORS",
            "severity": "medium",
            "detail": "No phrases indicating completed actions found (e.g., 'successfully implemented', 'has been deployed', 'completed on')."
        })

    # 5. Generic/vague language
    vague_patterns = re.findall(
        r'(?:appropriate|adequate|suitable|proper|necessary|relevant|required|various|several)\s+(?:measures|steps|actions|controls|procedures|processes)',
        text, re.IGNORECASE
    )
    if len(vague_patterns) >= 2:
        flags.append({
            "flag": "VAGUE_LANGUAGE",
            "severity": "medium",
            "detail": f"Evidence uses {len(vague_patterns)} vague phrases like '{vague_patterns[0]}'. Specific, concrete details are needed."
        })

    # 6. No reference numbers or document IDs
    ref_pattern = ARTIFACT_PATTERNS["reference_numbers"]
    if not ref_pattern.findall(text) and len(text) > 200:
        flags.append({
            "flag": "NO_REFERENCE_NUMBERS",
            "severity": "low",
            "detail": "No document reference numbers, circular IDs, or filing numbers found. Official compliance evidence typically includes references."
        })

    return flags


# ── Stage 6: Compute Rule-Based Confidence ─────────────────────────

def compute_confidence(
    artifacts: dict,
    cross_match: dict,
    temporal: dict,
    red_flags: list,
    evidence_length: int,
) -> str:
    """Compute a rule-based confidence score combining all pre-analysis signals."""
    score = 50  # Start at neutral

    # Artifacts boost confidence
    total_artifacts = artifacts.get("total_artifacts", 0)
    if total_artifacts >= 5:
        score += 20
    elif total_artifacts >= 3:
        score += 12
    elif total_artifacts >= 1:
        score += 5

    # Specific artifact types have different weights
    if artifacts.get("reference_numbers"):
        score += 10
    if artifacts.get("approval_signatures"):
        score += 8
    if artifacts.get("audit_reports"):
        score += 10
    if artifacts.get("dates"):
        score += 5
    if artifacts.get("metrics_numbers"):
        score += 5

    # Cross-match relevance
    relevance = cross_match.get("overall_relevance", 0)
    if relevance >= 60:
        score += 15
    elif relevance >= 30:
        score += 5
    else:
        score -= 10

    # Temporal consistency
    if temporal.get("temporal_ok", True):
        score += 5
    else:
        score -= 15

    # Red flags reduce confidence
    for flag in red_flags:
        if flag["severity"] == "high":
            score -= 20
        elif flag["severity"] == "medium":
            score -= 10
        elif flag["severity"] == "low":
            score -= 5

    # Evidence length
    if evidence_length > 500:
        score += 5
    elif evidence_length < 50:
        score -= 15

    # Clamp and map to levels
    score = max(0, min(100, score))
    if score >= 70:
        return "high"
    elif score >= 40:
        return "medium"
    else:
        return "low"


# ── LLM Prompt (Enhanced 8-Criteria) ──────────────────────────────

SYSTEM_PROMPT = (
    "You are a senior RBI compliance auditor with 25+ years of banking regulation experience. "
    "You are evaluating whether submitted evidence genuinely satisfies a regulatory compliance requirement (MAP). "
    "You have been given a pre-analysis summary from automated scanners - use it to ground your evaluation.\n\n"
    "Evaluate the evidence against these 8 criteria:\n"
    "  C1 - ACTION COMPLETION: Was the action actually done (not just planned or in-progress)? "
    "Look for past-tense completion language vs future-tense planning language.\n"
    "  C2 - SUB-REQUIREMENT COVERAGE: Are ALL sub-requirements and aspects of the mandate addressed? "
    "No partial compliance counts as full.\n"
    "  C3 - TIMELINESS: Was the action completed by or before the deadline date?\n"
    "  C4 - EVIDENCE SPECIFICITY: Is the evidence specific and verifiable? "
    "Does it include dates, reference numbers, system IDs, names?\n"
    "  C5 - DOCUMENT AUTHENTICITY: Does the evidence reference official documents, approvals, "
    "audit reports, or regulatory filings with proper identifiers?\n"
    "  C6 - METRIC ALIGNMENT: Does the evidence directly address the compliance metric specified? "
    "The metric defines HOW compliance is measured - evidence must match this.\n"
    "  C7 - VERIFICATION METHOD MATCH: Does the evidence match the expected verification method? "
    "If verification requires an audit report, evidence should include or reference one.\n"
    "  C8 - COMPLETENESS: Is the evidence self-contained? No gaps, no missing pieces, "
    "no 'refer to attachment' without actual content?\n\n"
    "Score each criterion: 'pass', 'partial', or 'fail'.\n"
    "Verdict rules:\n"
    "  - All pass = 'satisfied'\n"
    "  - Any partial, no fail = 'partial'\n"
    "  - Any fail = 'failed'\n\n"
    "Also assign confidence:\n"
    "  - 'high': Specific, verifiable artifacts with concrete references\n"
    "  - 'medium': Plausible but generic, partial gaps\n"
    "  - 'low': Vague, future plans, contradictions, speculative\n\n"
    "Return ONLY valid JSON (no markdown, no explanation):\n"
    '{"criterion_1":"pass/partial/fail","criterion_2":"pass/partial/fail",'
    '"criterion_3":"pass/partial/fail","criterion_4":"pass/partial/fail",'
    '"criterion_5":"pass/partial/fail","criterion_6":"pass/partial/fail",'
    '"criterion_7":"pass/partial/fail","criterion_8":"pass/partial/fail",'
    '"verdict":"satisfied/partial/failed",'
    '"gap_analysis":"exact description of what is missing or inadequate",'
    '"confidence":"high/medium/low",'
    '"reasoning":"brief explanation of key factors in your decision"}'
)


def _parse_validation_response(raw: str) -> dict:
    """Parse validation verdict from LLM output."""
    # Remove thinking tags from qwen models
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


# ── Main Validation Entry Point ────────────────────────────────────

def validate_evidence(
    map_id: str,
    evidence_text: str,
    submitted_by: str,
    evidence_file_path: str = None,
) -> dict:
    """Validate submitted evidence for a specific MAP using multi-stage pipeline.

    Args:
        map_id: The MAP row ID to validate against.
        evidence_text: The raw text evidence submitted by the department.
        submitted_by: Identifier of who submitted (department name or user).
        evidence_file_path: Optional path to an uploaded evidence file.

    Returns:
        Dict with verdict details or error.
    """
    # Fetch the full MAP row
    map_row = db.fetch_one("SELECT * FROM maps WHERE id = ?", (map_id,))
    if not map_row:
        return {"error": f"MAP {map_id} not found"}

    # Fetch regulation for context
    regulation = None
    if map_row.get("regulation_id"):
        regulation = db.fetch_one(
            "SELECT id, title, source, url, ingested_at FROM regulations WHERE id = ?",
            (map_row["regulation_id"],),
        )

    # ── Stage 1: Evidence Parsing ──────────────────────────────
    file_content = ""
    if evidence_file_path:
        file_content = extract_text_from_file(evidence_file_path)
        logger.info("Extracted %d chars from uploaded file: %s", len(file_content), evidence_file_path)

    # Combine text evidence with file content
    combined_evidence = evidence_text.strip()
    if file_content:
        combined_evidence += f"\n\n--- UPLOADED DOCUMENT CONTENT ---\n{file_content}"

    # Create evidence record
    evidence_id = db.new_id()
    submitted_at = db.now_iso()
    db.execute(
        """INSERT INTO evidence
           (id, map_id, submitted_by, evidence_text, evidence_file_path, submitted_at,
            validation_status, validation_verdict, criteria_scores, gap_analysis, validation_confidence, pre_analysis)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (evidence_id, map_id, submitted_by, evidence_text, evidence_file_path, submitted_at,
         "pending", None, None, None, "medium", None),
    )

    # ── Stage 2: Artifact Detection ────────────────────────────
    artifacts = detect_artifacts(combined_evidence)
    logger.info(
        "Artifact detection for MAP %s: %d total artifacts found",
        map_id[:8], artifacts["total_artifacts"]
    )

    # ── Stage 3: Compliance Metric Cross-Matching ──────────────
    cross_match = cross_match_compliance(combined_evidence, map_row)
    logger.info(
        "Cross-match for MAP %s: relevance=%.1f%%",
        map_id[:8], cross_match["overall_relevance"]
    )

    # ── Stage 4: Temporal Validation ───────────────────────────
    temporal = validate_temporal(combined_evidence, map_row)
    if temporal["temporal_issues"]:
        logger.warning(
            "Temporal issues for MAP %s: %s",
            map_id[:8], "; ".join(temporal["temporal_issues"])
        )

    # ── Stage 5: Red Flag Detection ────────────────────────────
    red_flags = detect_red_flags(combined_evidence, map_row)
    if red_flags:
        logger.warning(
            "Red flags for MAP %s: %s",
            map_id[:8], ", ".join(f["flag"] for f in red_flags)
        )

    # ── Stage 6: Compute Rule-Based Confidence ─────────────────
    rule_confidence = compute_confidence(
        artifacts, cross_match, temporal, red_flags, len(combined_evidence)
    )

    # Store pre-analysis results
    pre_analysis = {
        "artifacts": {k: v for k, v in artifacts.items() if k != "total_artifacts"},
        "total_artifacts": artifacts["total_artifacts"],
        "cross_match": cross_match,
        "temporal": temporal,
        "red_flags": red_flags,
        "rule_confidence": rule_confidence,
        "file_extracted": bool(file_content),
        "combined_evidence_length": len(combined_evidence),
    }

    # ── Stage 7: LLM Deep Analysis ─────────────────────────────
    pre_analysis_summary = _build_pre_analysis_summary(pre_analysis, map_row)

    user_prompt = (
        f"=== MAP REQUIREMENT ===\n"
        f"Action: {map_row['action_description']}\n"
        f"Department: {map_row.get('department', 'N/A')}\n"
        f"Compliance Metric: {map_row['compliance_metric']}\n"
        f"Verification Method: {map_row['verification_method']}\n"
        f"Deadline: {map_row['deadline_date']}\n"
        f"Priority: {map_row.get('priority', 'N/A')}\n"
        f"Source Regulation: {regulation['title'] if regulation else 'N/A'}\n\n"
        f"=== EVIDENCE SUBMITTED ===\n"
        f"Submitted by: {submitted_by}\n"
        f"Submitted at: {submitted_at}\n"
        f"Evidence text:\n{combined_evidence}\n\n"
        f"=== AUTOMATED PRE-ANALYSIS ===\n"
        f"{pre_analysis_summary}\n\n"
        f"Based on the above requirement, evidence, and pre-analysis, evaluate all 8 criteria and return your JSON verdict."
    )

    try:
        llm = ChatOllama(model="qwen3:8b", temperature=0.1, timeout=180)
        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])

        result = _parse_validation_response(response.content)

        if not result or "verdict" not in result:
            logger.error("Invalid validation response for MAP %s", map_id)
            result = _rule_based_fallback(pre_analysis)

        verdict = result.get("verdict", "failed")
        gap_analysis = result.get("gap_analysis", "")
        reasoning = result.get("reasoning", "")
        llm_confidence = result.get("confidence", "medium")

        # Combine LLM confidence with rule-based confidence (rule-based takes priority if lower)
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        final_confidence = min(
            confidence_rank.get(llm_confidence, 1),
            confidence_rank.get(rule_confidence, 1)
        )
        confidence = ["low", "medium", "high"][final_confidence]

        criteria_scores = {
            "criterion_1_action_completion": result.get("criterion_1", "fail"),
            "criterion_2_subreq_coverage": result.get("criterion_2", "fail"),
            "criterion_3_timeliness": result.get("criterion_3", "fail"),
            "criterion_4_evidence_specificity": result.get("criterion_4", "fail"),
            "criterion_5_document_authenticity": result.get("criterion_5", "fail"),
            "criterion_6_metric_alignment": result.get("criterion_6", "fail"),
            "criterion_7_verification_match": result.get("criterion_7", "fail"),
            "criterion_8_completeness": result.get("criterion_8", "fail"),
        }

        # Override verdict if red flags are severe
        high_severity_flags = [f for f in red_flags if f["severity"] == "high"]
        if verdict == "satisfied" and len(high_severity_flags) >= 2:
            verdict = "partial"
            gap_analysis += " [Auto-downgraded: Multiple high-severity red flags detected by pre-analysis]"
            logger.info("Verdict downgraded from satisfied to partial due to %d red flags", len(high_severity_flags))

        # Determine validation_status from verdict
        validation_status_map = {
            "satisfied": "pass",
            "partial": "partial",
            "failed": "fail",
        }
        validation_status = validation_status_map.get(verdict, "fail")

        # Build rich gap analysis with red flags
        enriched_gap = gap_analysis
        if red_flags:
            flag_summary = "; ".join(f["detail"] for f in red_flags[:3])
            enriched_gap += f"\n\nAutomated Red Flags: {flag_summary}"
        if reasoning:
            enriched_gap += f"\n\nReasoning: {reasoning}"

        # Update evidence record
        db.execute(
            """UPDATE evidence
               SET validation_status = ?, validation_verdict = ?,
                   criteria_scores = ?, gap_analysis = ?, validation_confidence = ?,
                   pre_analysis = ?
               WHERE id = ?""",
            (
                validation_status,
                verdict,
                json.dumps(criteria_scores),
                enriched_gap,
                confidence,
                json.dumps(pre_analysis),
                evidence_id,
            ),
        )

        # Update MAP status based on verdict
        if verdict == "satisfied":
            new_map_status = "completed"
        elif verdict == "partial":
            new_map_status = "partial"
        else:
            if map_row["status"] in ("overdue", "partial"):
                new_map_status = map_row["status"]
            else:
                new_map_status = "in_progress"

        db.execute(
            "UPDATE maps SET status = ? WHERE id = ?",
            (new_map_status, map_id),
        )

        # Write detailed audit entry
        db.insert_audit_entry(
            agent="validator",
            action="evidence validated",
            detail={
                "evidence_id": evidence_id,
                "verdict": verdict,
                "confidence": confidence,
                "rule_confidence": rule_confidence,
                "criteria_scores": criteria_scores,
                "gap_analysis": gap_analysis,
                "red_flags_count": len(red_flags),
                "artifacts_found": artifacts["total_artifacts"],
                "relevance_score": cross_match["overall_relevance"],
                "submitted_by": submitted_by,
                "map_status_updated_to": new_map_status,
                "file_evidence": bool(evidence_file_path),
            },
            regulation_id=map_row["regulation_id"],
            map_id=map_id,
        )

        logger.info(
            "Validation complete for MAP %s: verdict=%s, confidence=%s, "
            "artifacts=%d, relevance=%.1f%%, red_flags=%d",
            map_id[:8], verdict, confidence,
            artifacts["total_artifacts"], cross_match["overall_relevance"], len(red_flags),
        )

        return {
            "evidence_id": evidence_id,
            "map_id": map_id,
            "verdict": verdict,
            "confidence": confidence,
            "criteria_scores": criteria_scores,
            "gap_analysis": enriched_gap,
            "map_status": new_map_status,
            "pre_analysis": {
                "artifacts_found": artifacts["total_artifacts"],
                "relevance_score": cross_match["overall_relevance"],
                "red_flags": [f["flag"] for f in red_flags],
                "temporal_ok": temporal.get("temporal_ok", True),
                "rule_confidence": rule_confidence,
            },
        }

    except Exception as exc:
        logger.error("Validation failed for MAP %s: %s", map_id, exc)
        fallback = _rule_based_fallback(pre_analysis)
        fallback_verdict = fallback.get("verdict", "failed")
        fallback_gap = fallback.get("gap_analysis", "Automated validation only - LLM unavailable.")

        try:
            db.execute(
                """UPDATE evidence
                   SET validation_status = ?,
                       validation_verdict = ?,
                       gap_analysis = ?,
                       validation_confidence = ?,
                       pre_analysis = ?
                   WHERE id = ?""",
                (
                    "fail" if fallback_verdict == "failed" else "partial",
                    fallback_verdict,
                    f"LLM Error: {str(exc)}\n\nRule-based assessment: {fallback_gap}",
                    rule_confidence,
                    json.dumps(pre_analysis),
                    evidence_id,
                ),
            )
        except Exception as db_exc:
            logger.error("Failed to update evidence status on error: %s", db_exc)

        db.insert_audit_entry(
            agent="validator",
            action="validation failed (rule-based fallback used)",
            detail={
                "error": str(exc),
                "evidence_id": evidence_id,
                "fallback_verdict": fallback_verdict,
                "rule_confidence": rule_confidence,
            },
            map_id=map_id,
        )
        return {
            "error": str(exc),
            "evidence_id": evidence_id,
            "fallback_verdict": fallback_verdict,
            "rule_confidence": rule_confidence,
        }


def _build_pre_analysis_summary(pre_analysis: dict, map_row: dict) -> str:
    """Build a human-readable summary of pre-analysis for LLM context."""
    lines = []

    # Artifacts
    artifacts = pre_analysis.get("artifacts", {})
    total = pre_analysis.get("total_artifacts", 0)
    lines.append(f"ARTIFACTS DETECTED: {total} total")
    for atype, values in artifacts.items():
        if values:
            lines.append(f"  - {atype}: {', '.join(str(v) for v in values[:5])}")

    # Cross-match
    cm = pre_analysis.get("cross_match", {})
    lines.append(f"\nRELEVANCE ANALYSIS:")
    lines.append(f"  - Metric keyword coverage: {cm.get('metric_coverage_pct', 0)}%")
    lines.append(f"  - Verification method coverage: {cm.get('verification_coverage_pct', 0)}%")
    lines.append(f"  - Overall relevance to requirement: {cm.get('overall_relevance', 0)}%")

    # Temporal
    temporal = pre_analysis.get("temporal", {})
    if temporal.get("temporal_issues"):
        lines.append(f"\nTEMPORAL ISSUES:")
        for issue in temporal["temporal_issues"]:
            lines.append(f"  WARNING: {issue}")
    else:
        lines.append(f"\nTEMPORAL CHECK: No issues detected")

    # Red flags
    red_flags = pre_analysis.get("red_flags", [])
    if red_flags:
        lines.append(f"\nRED FLAGS ({len(red_flags)}):")
        for flag in red_flags:
            lines.append(f"  [{flag['severity'].upper()}] {flag['flag']}: {flag['detail']}")
    else:
        lines.append(f"\nRED FLAGS: None detected")

    # Rule-based confidence
    lines.append(f"\nRULE-BASED CONFIDENCE: {pre_analysis.get('rule_confidence', 'medium')}")

    return "\n".join(lines)


def _rule_based_fallback(pre_analysis: dict) -> dict:
    """Generate a verdict using only rule-based analysis when LLM is unavailable."""
    total_artifacts = pre_analysis.get("total_artifacts", 0)
    relevance = pre_analysis.get("cross_match", {}).get("overall_relevance", 0)
    red_flags = pre_analysis.get("red_flags", [])
    temporal_ok = pre_analysis.get("temporal", {}).get("temporal_ok", True)
    confidence = pre_analysis.get("rule_confidence", "medium")

    high_flags = [f for f in red_flags if f["severity"] == "high"]
    evidence_length = pre_analysis.get("combined_evidence_length", 0)

    # Simple heuristic verdict
    if len(high_flags) >= 2 or evidence_length < 30:
        verdict = "failed"
    elif total_artifacts >= 3 and relevance >= 50 and temporal_ok and not high_flags:
        verdict = "satisfied"
    elif total_artifacts >= 1 or relevance >= 30:
        verdict = "partial"
    else:
        verdict = "failed"

    # Build gap analysis
    gaps = []
    if not temporal_ok:
        gaps.append("Temporal inconsistencies detected in evidence dates")
    if relevance < 30:
        gaps.append(f"Low relevance to compliance metric ({relevance}% keyword match)")
    if total_artifacts == 0:
        gaps.append("No concrete artifacts (dates, reference numbers, IDs) found")
    for flag in high_flags:
        gaps.append(flag["detail"])

    return {
        "verdict": verdict,
        "confidence": confidence,
        "gap_analysis": "; ".join(gaps) if gaps else "Rule-based assessment only. Please review manually.",
    }
