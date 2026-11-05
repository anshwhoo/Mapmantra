"""
Regulators and standards MapMantra tracks.
One list used by the upload form, upload validation, the risk engine and agent prompts.
Penalty ranges (in Crores, per overdue MAP) are set only where the risk engine has
configured figures; sources without one are counted but not priced in financial exposure.
"""

REGULATORS = [
    # Indian regulators and authorities
    {"code": "RBI", "name": "Reserve Bank of India", "kind": "regulator", "penalty": {"min_cr": 2, "max_cr": 50}},
    {"code": "SEBI", "name": "Securities and Exchange Board of India", "kind": "regulator", "penalty": {"min_cr": 1, "max_cr": 25}},
    {"code": "IRDAI", "name": "Insurance Regulatory and Development Authority of India", "kind": "regulator", "penalty": {"min_cr": 0.5, "max_cr": 5}},
    {"code": "CERT-In", "name": "Indian Computer Emergency Response Team", "kind": "regulator", "penalty": None},
    {"code": "NPCI", "name": "National Payments Corporation of India", "kind": "regulator", "penalty": None},
    {"code": "FIU-IND", "name": "Financial Intelligence Unit - India", "kind": "regulator", "penalty": None},
    {"code": "PFRDA", "name": "Pension Fund Regulatory and Development Authority", "kind": "regulator", "penalty": None},
    {"code": "IFSCA", "name": "International Financial Services Centres Authority", "kind": "regulator", "penalty": None},
    {"code": "MeitY", "name": "Ministry of Electronics and IT (DPDP Act)", "kind": "regulator", "penalty": None},
    # Security and industry standards
    {"code": "ISO 27001", "name": "ISO/IEC 27001 Information Security Management", "kind": "standard", "penalty": None},
    {"code": "PCI DSS", "name": "Payment Card Industry Data Security Standard", "kind": "standard", "penalty": None},
    {"code": "NIST CSF", "name": "NIST Cybersecurity Framework", "kind": "standard", "penalty": None},
    {"code": "SWIFT CSP", "name": "SWIFT Customer Security Programme", "kind": "standard", "penalty": None},
]

REGULATOR_CODES = [r["code"] for r in REGULATORS]
PENALTY_RANGES = {r["code"]: r["penalty"] for r in REGULATORS if r["penalty"]}
