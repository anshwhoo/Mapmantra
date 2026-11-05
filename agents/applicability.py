"""
Applicability screening (part of the Ingestion agent).
Decides whether a new circular applies to this bank before any actions are extracted,
using the bank profile (bank type + activities):
  1. Source check   - e.g. PCI DSS only matters if the bank issues cards.
  2. Addressee check - who the circular is addressed to, read from the top of the
                       document ("All Scheduled Commercial Banks (excluding RRBs)").
  3. Title check    - titles that name only other bank types ("... - Small Finance Banks").
  4. AI check       - only when the rules above cannot tell.
When nothing decides, the circular is kept: missing a real obligation costs more than
reading an extra circular. Compliance can override any decision from the Regulation Inbox.
"""

import json
import logging
import os
import re

from langchain_core.messages import SystemMessage, HumanMessage

import db
from agents.bank_profile import BANK_TYPES, BANK_TYPE_NAMES, ACTIVITY_NAMES, get_profile, describe
from agents.llm import chat_llm

logger = logging.getLogger("agents.applicability")

COOP = r"co-?\s?operative"

# ── Who a circular can be addressed to ───────────────────────────
# Bank types (codes match agents.bank_profile.BANK_TYPES; foreign banks are recognised
# so circulars meant only for them are filtered out, but are not a profile choice)
BANK_AUDIENCES = {
    "public_sector": r"\bpublic\s+sector\s+banks?\b|\bPSBs?\b",
    "private_sector": r"\bprivate\s+sector\s+banks?\b",
    "foreign": r"\bforeign\s+banks?\b",
    "small_finance": r"\bsmall\s+finance\s+banks?\b|\bSFBs?\b",
    "payments": r"\bpayments?\s+banks?\b",
    "regional_rural": r"\bregional\s+rural\s+banks?\b|\bRRBs?\b",
    "local_area": r"\blocal\s+area\s+banks?\b",
    "urban_cooperative": rf"\burban\)?\s+{COOP}\s+banks?\b|\bUCBs?\b",
    "rural_cooperative": rf"\b(?:state|district|central)\s+(?:central\s+)?{COOP}\s+banks?\b|\bStCBs?\b|\bDCCBs?\b|\brural\s+{COOP}",
}
SCHEDULED_COMMERCIAL = r"\b(?:scheduled\s+)?commercial\s+banks?\b|\bSCBs?\b"
ANY_COOPERATIVE = rf"\b{COOP}\s+banks?\b"
ALL_BANKS = r"\ball\s+(?:the\s+)?banks\b|\bevery\s+bank\b"
ALL_ENTITIES = r"\bregulated\s+entities\b|\breporting\s+entities\b|\ball\s+entities\b"

# Activities: a circular addressed to these applies only if the bank does that business
ACTIVITY_AUDIENCES = {
    "forex_dealer": (r"\bauthori[sz]ed\s+dealer|\bAD\s+category|\bAD\s+banks?\b", "Authorised dealer banks"),
    "card_issuer": (r"\bcard[\s-]+issu|\bissuers?\s+of\s+(?:credit|debit)\s+cards", "Card issuers"),
    "d_sib": (r"\bsystemically\s+important\s+banks?\b|\bD-SIBs?\b", "Systemically important banks"),
    "listed": (r"\blisted\s+(?:entities|companies)\b", "Listed companies"),
    "upi_member": (r"\bUPI\b|\bmember\s+banks\b|\bNPCI\s+members?\b", "UPI / NPCI member banks"),
    "corporate_agent": (r"\bcorporate\s+agents?\b|\bbancassurance\b|\binsurance\s+intermediar", "Insurance corporate agents"),
    "pension_pop": (r"\bpoints?\s+of\s+presence\b", "NPS Points of Presence"),
    "sebi_intermediary": (r"\bdepository\s+participants?\b|\bcustodians?\b|\bbankers?\s+to\s+(?:an?\s+)?issues?\b|\bdebenture\s+trustees?\b",
                          "SEBI-registered intermediaries"),
    "ifsc_unit": (r"\bIFSC\s+banking\s+units?\b|\bIBUs?\b", "IFSC banking units"),
    "swift_member": (r"\bSWIFT\s+users?\b", "SWIFT users"),
}

# Entities that are not banks
NON_BANKS = {
    "NBFCs": r"\bnon-?\s?banking\s+financial\s+compan|\bNBFCs?\b",
    "Housing finance companies": r"\bhousing\s+finance\s+compan|\bHFCs?\b",
    "All India financial institutions": r"\ball\s+india\s+financial\s+institutions\b|\bAIFIs?\b",
    "Asset reconstruction companies": r"\basset\s+reconstruction\s+compan|\bARCs?\b",
    "Credit information companies": r"\bcredit\s+information\s+compan",
    "Money changers": r"\bmoney\s+changers?\b|\bFFMCs?\b",
    "Insurers": r"\binsurers\b|\binsurance\s+compan",
    "Stock exchanges and clearing corporations": r"\bstock\s+exchanges?\b|\bclearing\s+corporations?\b|\bdepositories\b",
    "Mutual funds and asset managers": r"\bmutual\s+funds?\b|\basset\s+management\s+compan",
    "Stock brokers": r"\bstock\s+brokers?\b|\btrading\s+members?\b",
    "Pension funds": r"\bpension\s+funds?\b",
    "Credit rating agencies": r"\bcredit\s+rating\s+agenc",
}

# Sources that only concern banks doing a particular business
SOURCE_ACTIVITY = {
    "PCI DSS": "card_issuer",
    "SWIFT CSP": "swift_member",
    "IFSCA": "ifsc_unit",
    "IRDAI": "corporate_agent",
    "PFRDA": "pension_pop",
    "NPCI": "upi_member",
}

# The addressee block of a letter sits just above the salutation ("Madam / Dear Sir") or
# follows a "To" line, and ends at the subject line or the first numbered paragraph
_SALUTATION = re.compile(r"^\s*(?:dear\s+)?(?:madam|sir)\b", re.I | re.M)
_BODY_START = re.compile(r"^\s*(?:sub(?:ject)?\s*[:.\-]|1\.\s)", re.I | re.M)
_TO_LINE = re.compile(r"^[ \t]*to[ \t]*(?:[:,][ \t]*(?=\S)|[:,]?[ \t]*$)", re.I | re.M)  # "To:", "To," or "To" alone
ADDRESSEE_BLOCK_LINES = 12
# RBI's consolidated directions name the regulated entity in the title:
# "Reserve Bank of India (Small Finance Banks – Internal Ombudsman) Directions, 2025"
_RBI_DIRECTION_TITLE = re.compile(r"reserve\s+bank\s+of\s+india\s*\(([^()]*?)\s*[–—\-�]\s", re.I)
_EXCLUSION = re.compile(r"\(?\s*\b(?:excluding|other\s+than|except(?:\s+for)?)\b([^)]*)\)?", re.I)

AI_SYSTEM_PROMPT = (
    "You check whether an Indian regulatory circular or standard applies to one specific bank. "
    "Read who the circular is addressed to and what it covers. It applies if this bank must act on it. "
    "Circulars addressed only to other kinds of entities (for example only urban co-operative banks, "
    "only NBFCs, only insurers or only stock brokers) do not apply. If you are not sure, answer \"unsure\". "
    'Return ONLY JSON: {"applies": "yes" | "no" | "unsure", '
    '"addressed_to": "<who it is addressed to>", "reason": "<one short sentence>"}'
)


# ── Reading the audience ─────────────────────────────────────────
def _audiences(fragment: str) -> set[str]:
    found = {code for code, pattern in BANK_AUDIENCES.items() if re.search(pattern, fragment, re.I)}
    if re.search(ANY_COOPERATIVE, fragment, re.I) and not found & {"urban_cooperative", "rural_cooperative"}:
        found |= {"urban_cooperative", "rural_cooperative"}
    if re.search(SCHEDULED_COMMERCIAL, fragment, re.I):
        found.add("scheduled_commercial")
    if re.search(ALL_BANKS, fragment, re.I):
        found.add("all_banks")
    if re.search(ALL_ENTITIES, fragment, re.I):
        found.add("all_entities")
    found |= {f"activity:{code}" for code, (pattern, _) in ACTIVITY_AUDIENCES.items() if re.search(pattern, fragment, re.I)}
    found |= {f"nonbank:{name}" for name, pattern in NON_BANKS.items() if re.search(pattern, fragment, re.I)}
    return found


def _addressee_lines(text: str) -> list[str]:
    """Lines like 'All Scheduled Commercial Banks (excluding RRBs)' from the letter's addressee
    block. Documents that are not letters (no salutation and no 'To' line) have none, so text
    from their body is never mistaken for an addressee. A line with an unclosed bracket
    continues onto the next one."""
    head = text[:3000]
    salutation = _SALUTATION.search(head)
    to_line = _TO_LINE.search(head)
    if not salutation and not to_line:
        return []
    ends = [m.start() for m in (salutation, _BODY_START.search(head)) if m]
    block = head[:min(ends)] if ends else head
    if to_line and to_line.start() < len(block):
        block = block[to_line.end():]
    lines, current = [], ""
    for line in block.splitlines()[-ADDRESSEE_BLOCK_LINES:]:
        current = f"{current} {line.strip()}".strip()
        if current.count("(") <= current.count(")"):
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return [line for line in lines if line]


def _read_addressees(text: str) -> tuple[set[str], set[str], list[str]]:
    """Audiences the circular is addressed to, audiences it explicitly excludes, and the raw lines."""
    included, excluded = set(), set()
    lines = _addressee_lines(text)
    for line in lines:
        for m in _EXCLUSION.finditer(line):
            excluded |= _audiences(m.group(1))
        included |= _audiences(_EXCLUSION.sub(" ", line))
    return included - excluded, excluded, lines


def _label(audience: str) -> str:
    if audience in BANK_TYPE_NAMES:
        return BANK_TYPE_NAMES[audience] + "s"
    if audience.startswith("activity:"):
        return ACTIVITY_AUDIENCES[audience.split(":", 1)[1]][1]
    if audience.startswith("nonbank:"):
        return audience.split(":", 1)[1]
    return {"foreign": "Foreign banks", "scheduled_commercial": "Scheduled commercial banks",
            "all_banks": "All banks", "all_entities": "All regulated entities"}.get(audience, audience)


def _labels(audiences: set[str]) -> str:
    return ", ".join(sorted(_label(a) for a in audiences))


def _decide(included: set[str], excluded: set[str], profile: dict, where: str) -> dict | None:
    """Rule decision from the audiences found; None when they say nothing about this bank."""
    bank_type = profile["bank_type"]
    type_name = BANK_TYPE_NAMES[bank_type].lower()
    a_type = ("an " if type_name[0] in "aeiou" else "a ") + type_name
    is_scheduled_commercial = next(t for t in BANK_TYPES if t["code"] == bank_type)["scheduled_commercial"]
    activities = set(profile.get("activities", []))
    addressed_to = _labels(included)

    def result(applicable: bool, reason: str) -> dict:
        return {"applicable": applicable, "method": "rules", "reason": reason, "addressed_to": addressed_to}

    if bank_type in excluded:
        return result(False, f"{where}: {addressed_to or 'banks'}, but {type_name}s are excluded.")
    if bank_type in included:
        return result(True, f"{where}: {addressed_to}. This includes {type_name}s.")
    if "scheduled_commercial" in included and is_scheduled_commercial:
        return result(True, f"{where}: {addressed_to}. {a_type.capitalize()} is a scheduled commercial bank.")
    if included & {"all_banks", "all_entities"}:
        return result(True, f"{where}: {addressed_to}. This covers every bank.")
    held = [a for a in included if a.startswith("activity:") and a.split(":", 1)[1] in activities]
    if held:
        return result(True, f"{where}: {addressed_to}. This bank is one of the {_labels(set(held)).lower()}.")
    if included:
        return result(False, f"{where}: {addressed_to}. This bank ({a_type}) is not one of them.")
    return None


def _ai_check(reg: dict, profile: dict) -> dict:
    llm = chat_llm(num_predict=300)
    reply = llm.invoke([
        SystemMessage(content=AI_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"Bank: {describe(profile)}\n"
            f"Source: {reg.get('source')}\n"
            f"Title: {reg.get('title')}\n\n"
            f"Start of the circular:\n{(reg.get('raw_text') or '')[:3000]}"
        )),
    ]).content
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", reply, flags=re.S), re.S)
    answer = json.loads(match.group(0)) if match else {}
    applies = str(answer.get("applies", "unsure")).strip().lower()
    addressed_to = str(answer.get("addressed_to") or "")[:200]
    reason = str(answer.get("reason") or "")[:300]
    if applies == "no":
        return {"applicable": False, "method": "ai", "reason": reason or "AI found it is not meant for this bank.",
                "addressed_to": addressed_to}
    if applies == "yes":
        return {"applicable": True, "method": "ai", "reason": reason or "AI found it applies to this bank.",
                "addressed_to": addressed_to}
    return {"applicable": True, "method": "default", "addressed_to": addressed_to,
            "reason": "The AI was not sure, so the circular is kept to make sure nothing is missed."}


# ── Public API ───────────────────────────────────────────────────
def screen(reg: dict, profile: dict | None = None, use_ai: bool | None = None) -> dict:
    """Decide whether a regulation applies to the bank. Returns
    {applicable, method ('rules' | 'ai' | 'default'), reason, addressed_to}."""
    profile = profile or get_profile()
    if use_ai is None:
        use_ai = os.getenv("APPLICABILITY_AI_CHECK", "true").lower() == "true"
    text = reg.get("raw_text") or ""

    # 1. Source check
    needed = SOURCE_ACTIVITY.get(reg.get("source"))
    if needed and needed not in profile.get("activities", []):
        return {"applicable": False, "method": "rules", "addressed_to": reg.get("source"),
                "reason": f"{reg.get('source')} only applies to banks that do this business: "
                          f"{ACTIVITY_NAMES[needed].lower()}. This bank's profile does not include it."}

    # 2. Addressee check
    included, excluded, _ = _read_addressees(text)
    decision = _decide(included, excluded, profile, "Addressed to")
    if decision:
        return decision

    # 3. Title check. RBI directions name their regulated entity in the title; otherwise only
    # bank types count (a title naming NBFCs may still be about bank lending to them)
    direction = _RBI_DIRECTION_TITLE.search(reg.get("title") or "")
    if direction:
        decision = _decide(_audiences(direction.group(1)), set(), profile, "Directions for")
        if decision:
            return decision
    title = re.sub(r"reserve\s+bank\s+of\s+india", " ", reg.get("title") or "", flags=re.I)
    title_types = {a for a in _audiences(title) if a in BANK_AUDIENCES or a == "scheduled_commercial"}
    decision = _decide(title_types, set(), profile, "Title mentions")
    if decision:
        return decision

    # 4. AI check
    if use_ai:
        try:
            return _ai_check(reg, profile)
        except Exception as exc:
            logger.warning("AI applicability check failed for %s: %s", reg.get("id"), exc)

    return {"applicable": True, "method": "default", "addressed_to": "",
            "reason": "No addressee found, so the circular is kept to make sure nothing is missed."}


def screen_and_store(reg_id: str) -> bool:
    """Screen a 'new' regulation, store the decision and park it as 'not_applicable' when it
    does not apply. Returns True when the circular should go on to extraction."""
    reg = db.fetch_one("SELECT * FROM regulations WHERE id = ?", (reg_id,))
    if not reg:
        return False
    profile = get_profile()
    decision = screen(reg, profile)
    db.execute(
        """UPDATE regulations SET applicability = ?, applicability_method = ?, applicability_reason = ?,
               applicability_audience = ?, screened_at = ?, status = ?
           WHERE id = ? AND status = 'new'""",
        ("applicable" if decision["applicable"] else "not_applicable", decision["method"],
         decision["reason"], decision["addressed_to"], db.now_iso(),
         "new" if decision["applicable"] else "not_applicable", reg_id),
    )
    db.insert_audit_entry(
        agent="ingestion",
        action="applicability checked",
        detail={**decision, "bank_profile": profile["name"], "bank_type": profile["bank_type"]},
        regulation_id=reg_id,
    )
    logger.info("Applicability [%s, %s]: %s - %s", "applies" if decision["applicable"] else "not applicable",
                decision["method"], (reg.get("title") or "")[:60], decision["reason"])
    return decision["applicable"]
