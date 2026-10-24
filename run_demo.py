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
"""

import os
import shutil
import subprocess
import sys
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
    "Chief General Manager-in-Charge",
]


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

    os.environ.setdefault("OLLAMA_MODEL", "qwen2.5:7b")
    os.environ.setdefault("INGESTION_ON_STARTUP", "false")
    os.chdir(DEMO)
    sys.path.insert(0, str(ROOT))
    print(f"Demo running at http://localhost:8001 (model {os.environ['OLLAMA_MODEL']})")
    print(f"Upload {DEMO / 'sample_circular.pdf'} from the Regulation Inbox to watch the agents.")

    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
