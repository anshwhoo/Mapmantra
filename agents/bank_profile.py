"""
Bank profile used to decide which circulars apply to this bank.
The profile is the bank's type (private sector, urban co-operative, ...) plus the
business it does (cards, forex, SWIFT, ...). It is stored in the settings table and
edited from the Regulation Inbox; the ingestion screening reads it for every circular.
"""

import json

import db

# Indian bank types the RBI addresses circulars to (foreign banks are out of scope: the
# filter treats circulars meant only for them as not applicable). `scheduled_commercial`
# marks the types covered when a circular is addressed to "All Scheduled Commercial Banks".
BANK_TYPES = [
    {"code": "public_sector", "name": "Public sector bank", "examples": "SBI, Bank of Baroda, PNB", "scheduled_commercial": True},
    {"code": "private_sector", "name": "Private sector bank", "examples": "ICICI Bank, HDFC Bank, Axis Bank", "scheduled_commercial": True},
    {"code": "small_finance", "name": "Small finance bank", "examples": "AU SFB, Ujjivan SFB", "scheduled_commercial": True},
    {"code": "payments", "name": "Payments bank", "examples": "Airtel Payments Bank, India Post Payments Bank", "scheduled_commercial": True},
    {"code": "regional_rural", "name": "Regional rural bank", "examples": "Kerala Gramin Bank, Baroda UP Bank", "scheduled_commercial": True},
    {"code": "local_area", "name": "Local area bank", "examples": "Coastal Local Area Bank", "scheduled_commercial": True},
    {"code": "urban_cooperative", "name": "Urban co-operative bank", "examples": "Saraswat Bank, Cosmos Bank", "scheduled_commercial": False},
    {"code": "rural_cooperative", "name": "State / district co-operative bank", "examples": "Maharashtra State Co-op Bank, Pune DCCB", "scheduled_commercial": False},
]
BANK_TYPE_CODES = [t["code"] for t in BANK_TYPES]
BANK_TYPE_NAMES = {t["code"]: t["name"] for t in BANK_TYPES}

# Business the bank does. A circular addressed only to, say, card issuers applies
# only when the bank has that activity.
ACTIVITIES = [
    {"code": "listed", "name": "Listed on stock exchanges"},
    {"code": "d_sib", "name": "Domestic systemically important bank (D-SIB)"},
    {"code": "forex_dealer", "name": "Authorised Dealer Category-I (forex)"},
    {"code": "card_issuer", "name": "Issues credit or debit cards"},
    {"code": "upi_member", "name": "Member of UPI / NPCI payment systems"},
    {"code": "swift_member", "name": "Uses SWIFT for cross-border payments"},
    {"code": "ifsc_unit", "name": "Has a GIFT City (IFSC) banking unit"},
    {"code": "corporate_agent", "name": "Sells insurance as a corporate agent"},
    {"code": "pension_pop", "name": "NPS Point of Presence (pension)"},
    {"code": "sebi_intermediary", "name": "SEBI-registered role (depository participant, custodian, banker to an issue)"},
]
ACTIVITY_CODES = [a["code"] for a in ACTIVITIES]
ACTIVITY_NAMES = {a["code"]: a["name"] for a in ACTIVITIES}

# Default: a large private sector bank, set up like ICICI Bank
DEFAULT_PROFILE = {
    "name": "Demo Bank (ICICI-style)",
    "bank_type": "private_sector",
    "activities": list(ACTIVITY_CODES),
}

SETTINGS_KEY = "bank_profile"


def get_profile() -> dict:
    row = db.fetch_one("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,))
    if not row:
        return dict(DEFAULT_PROFILE)
    try:
        profile = {**DEFAULT_PROFILE, **json.loads(row["value"])}
    except (TypeError, ValueError):
        return dict(DEFAULT_PROFILE)
    return profile if profile["bank_type"] in BANK_TYPE_CODES else dict(DEFAULT_PROFILE)


def save_profile(name: str, bank_type: str, activities: list[str]) -> dict:
    if bank_type not in BANK_TYPE_CODES:
        raise ValueError(f"Unknown bank type '{bank_type}'. Choose one of: {', '.join(BANK_TYPE_CODES)}")
    unknown = [a for a in activities if a not in ACTIVITY_CODES]
    if unknown:
        raise ValueError(f"Unknown activities: {', '.join(unknown)}")
    profile = {
        "name": name.strip() or DEFAULT_PROFILE["name"],
        "bank_type": bank_type,
        "activities": [a for a in ACTIVITY_CODES if a in activities],
    }
    db.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (SETTINGS_KEY, json.dumps(profile), db.now_iso()),
    )
    return profile


def describe(profile: dict) -> str:
    """One plain-language line about the bank, for the AI check and the audit trail."""
    activities = [ACTIVITY_NAMES[a] for a in profile.get("activities", []) if a in ACTIVITY_NAMES]
    text = f"{profile.get('name')}: a {BANK_TYPE_NAMES.get(profile.get('bank_type'), 'bank').lower()} in India"
    if activities:
        text += ". Activities: " + "; ".join(activities)
    return text + "."
