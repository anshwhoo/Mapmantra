"""
run_demo.py — Start MapMantra on a small, seeded demo database.

Runs from ./demo (its own data/ folder), so the real ./data is never touched.
Demo defaults (override by setting these environment variables before running;
they take precedence over .env):
  OLLAMA_MODEL=qwen2.5:7b       ~2x faster than qwen3:8b on a 6 GB GPU
  INGESTION_ON_STARTUP=false    start quiet; click "Run Pipeline" to show live ingestion

Usage:
  python run_demo.py           start (seeds the demo DB on first run)
  python run_demo.py --reset   wipe and re-seed the demo DB, then start

Demo flow: open http://localhost:8001, upload demo/sample_circular.pdf from the
Regulation Inbox and watch the Pipeline Agents panel on the Dashboard.
Then upload demo/conflict_circular.pdf (SEBI): its offshore-replication rule
contradicts the sample's data-localisation rule, so the Conflict Detector raises
a Type B conflict that needs human/legal review. Evidence files to submit are
generated in demo/evidence_files/ with today's date.
"""

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "demo"

SAMPLE_CIRCULAR = [
    "RESERVE BANK OF INDIA - SAMPLE CIRCULAR (MapMantra demo)",
    "RBI/DEMO/2026-27  DoS.CO.CSITE.SEC.No.DEMO/31.01.015/2026-27",
    "To: All Scheduled Commercial Banks",
    "Subject: Strengthening Cyber Resilience - Privileged Access and Incident Reporting",
    "1. All banks shall implement multi-factor authentication (MFA) for all privileged",
    "   user accounts on core banking, payment and treasury systems within 90 days.",
    "2. Banks shall report every cyber security incident to the Reserve Bank (CSITE cell)",
    "   within 6 hours of detection using the prescribed incident reporting format.",
    "3. The Board of each bank shall approve an updated Cyber Crisis Management Plan",
    "   (CCMP) within 60 days and review it at least annually thereafter.",
    "4. Banks shall conduct disaster recovery (DR) drills for critical systems every",
    "   quarter and document the results, including RTO and RPO achieved.",
    "5. Banks shall ensure that all customer payment data is stored only in systems",
    "   located in India. No copy of such data shall be stored or processed outside India.",
    "Chief General Manager-in-Charge",
]

# Contradicts item 5 above: triggers a Type B conflict (human/legal review)
CONFLICT_CIRCULAR = [
    "SECURITIES AND EXCHANGE BOARD OF INDIA - SAMPLE CIRCULAR (MapMantra demo)",
    "SEBI/HO/DEMO/CIR/2026/77",
    "To: All Regulated Entities",
    "Subject: Business Continuity - Offshore Disaster Recovery for Customer Data",
    "1. Regulated entities shall replicate all customer payment and transaction data in",
    "   real time to a disaster recovery site located outside India, in a different",
    "   geographic region from the primary data centre.",
    "Chief General Manager",
]


def _evidence_files(today: date) -> dict[str, list[str]]:
    """Evidence documents dated today, so they post-date the actions they support."""
    d = today.strftime("%d %B %Y")
    nxt = today.replace(year=today.year + 1).strftime("%B %Y")
    return {
        "ccmp_board_approval.pdf": [
            "BOARD RESOLUTION No. BR/2026/142 - Cyber Crisis Management Plan",
            f"Date of meeting: {d}",
            "RESOLVED that the updated Cyber Crisis Management Plan (CCMP v3.0, ref CCMP/2026/03)",
            "is approved. The CCMP has been approved by the Board of Directors and circulated",
            f"to all departments on {d}.",
            "Annual review: the Board has fixed an annual review of the CCMP;",
            f"the next review is due in {nxt}.",
            "Certified extract of Board minutes and the signed CCMP document are enclosed.",
            f"Approved by Ramesh Iyer, Chairman. Certified by Company Secretary on {d}.",
        ],
        "ccmp_draft_note.pdf": [
            "Internal note - Cyber Crisis Management Plan",
            "We are preparing the updated CCMP. The draft is planned to be presented to the",
            "Board next quarter. Work in progress.",
        ],
        "data_localisation_report.pdf": [
            f"DATA LOCALISATION COMPLIANCE REPORT DL/2026/07 - {d}",
            "All customer payment data has been verified to be stored only in the Mumbai",
            "(primary) and Chennai (disaster recovery) data centres, both located in India.",
            "No copy of customer payment data is stored or processed outside India.",
            f"Internal Audit Report IA/2026/19 dated {d} confirms the above after a",
            "review of storage locations, backup targets and replication jobs.",
            f"Signed: Chief Information Security Officer, {d}.",
        ],
    }


def _make_pdf(lines: list[str]) -> bytes:
    """Minimal single-page PDF with Helvetica text (no extra dependencies)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 50 800 Td 13 TL\n" + "".join(f"({esc(l)}) '\n" for l in lines) + "ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def main():
    if "--reset" in sys.argv and DEMO.exists():
        shutil.rmtree(DEMO)
    if not (DEMO / "data" / "compliance.db").exists():
        DEMO.mkdir(exist_ok=True)
        print("Seeding demo database in", DEMO)
        subprocess.run([sys.executable, str(ROOT / "seed.py")], cwd=DEMO, check=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    (DEMO / "sample_circular.pdf").write_bytes(_make_pdf(SAMPLE_CIRCULAR))
    (DEMO / "conflict_circular.pdf").write_bytes(_make_pdf(CONFLICT_CIRCULAR))
    evidence_dir = DEMO / "evidence_files"
    evidence_dir.mkdir(exist_ok=True)
    for name, lines in _evidence_files(date.today()).items():
        (evidence_dir / name).write_bytes(_make_pdf(lines))

    os.environ.setdefault("OLLAMA_MODEL", "qwen2.5:7b")
    os.environ.setdefault("INGESTION_ON_STARTUP", "false")
    os.chdir(DEMO)
    sys.path.insert(0, str(ROOT))
    print(f"Demo running at http://localhost:8001 (model {os.environ['OLLAMA_MODEL']})")
    print(f"Upload {DEMO / 'sample_circular.pdf'} from the Regulation Inbox to watch the agents,")
    print(f"then {DEMO / 'conflict_circular.pdf'} to trigger a Type B conflict.")
    print(f"Evidence files to submit: {evidence_dir}")

    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
