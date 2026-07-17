"""
Agent 1 — Ingestion Agent
4-option fallback: RSS Feed → Playwright → Email IMAP → Manual Upload
No LLM used. Pure Python automation.
"""

import hashlib
import io
import logging
import email as email_lib
import imaplib
import traceback
from pathlib import Path
from typing import Optional

import requests
import pdfplumber
from bs4 import BeautifulSoup

import db

logger = logging.getLogger("agents.ingestion")

DATA_DIR = Path("./data")
INCOMING_PDF_DIR = DATA_DIR / "incoming_pdfs"
INCOMING_PDF_DIR.mkdir(parents=True, exist_ok=True)

# ── RSS feed URLs ────────────────────────────────────────────────
RSS_FEEDS = {
    "RBI": "https://www.rbi.org.in/scripts/rss.aspx",
    "SEBI": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&type=1",
    "IRDAI": "https://www.irdai.gov.in/ADMINCMS/cms/Circulars_Layout.aspx",
}

# ── Circular index pages (for Playwright fallback) ──────────────
INDEX_PAGES = {
    "RBI": "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx",
    "SEBI": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=1&smession=No",
    "IRDAI": "https://www.irdai.gov.in/ADMINCMS/cms/Circulars_Layout.aspx",
}

# ── Email domains that signify official regulator mail ───────────
OFFICIAL_DOMAINS = ["rbi.org.in", "sebi.gov.in", "irdai.gov.in"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


# =====================================================================
# PDF processing helpers
# =====================================================================
def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _extract_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def _hash_exists(doc_hash: str) -> bool:
    row = db.fetch_one("SELECT id FROM regulations WHERE doc_hash = ?", (doc_hash,))
    return row is not None


def _save_regulation(
    title: str,
    source: str,
    url: str,
    raw_text: str,
    doc_hash: str,
) -> str:
    """Insert a new regulation and write an audit entry. Returns the regulation id."""
    reg_id = db.new_id()
    db.execute(
        """INSERT INTO regulations (id, title, source, url, raw_text, doc_hash, ingested_at, status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (reg_id, title, source, url, raw_text, doc_hash, db.now_iso(), "new"),
    )
    db.insert_audit_entry(
        agent="ingestion",
        action="circular ingested",
        detail={"source": source, "title": title, "method": "auto", "url": url},
        regulation_id=reg_id,
    )
    logger.info("Ingested: %s [%s]", title[:80], source)
    return reg_id


def _download_pdf(url: str, timeout: int = 60) -> Optional[bytes]:
    """Download PDF bytes from a URL. Returns None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
            return resp.content
        # Sometimes the link returns HTML — skip those
        if resp.content[:5] == b"%PDF-":
            return resp.content
        return None
    except Exception as exc:
        logger.warning("PDF download failed for %s: %s", url, exc)
        return None


# =====================================================================
# OPTION 1 — RSS FEED
# =====================================================================
def _ingest_via_rss() -> int:
    """Parse RSS/XML feeds from RBI, SEBI, IRDAI. Returns count of new regulations."""
    import feedparser  # lazy import — only needed for this path

    total = 0
    for source, feed_url in RSS_FEEDS.items():
        try:
            logger.info("Fetching RSS feed: %s (%s)", source, feed_url)
            resp = requests.get(feed_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            if not resp.text.strip():
                logger.warning("Empty RSS response from %s", source)
                continue

            feed = feedparser.parse(resp.text)
            if not feed.entries:
                # Try parsing raw HTML for links if feedparser finds nothing
                total += _parse_html_for_pdfs(resp.text, source, feed_url)
                continue

            for entry in feed.entries:
                title = entry.get("title", "Untitled Circular")
                link = entry.get("link", "")
                # Try to find a PDF link in enclosures or the link itself
                pdf_url = ""
                for enc in entry.get("enclosures", []):
                    if "pdf" in enc.get("type", "").lower() or enc.get("href", "").lower().endswith(".pdf"):
                        pdf_url = enc["href"]
                        break
                if not pdf_url and link.lower().endswith(".pdf"):
                    pdf_url = link
                if not pdf_url:
                    # Try to find PDF in the entry content/summary
                    summary = entry.get("summary", "") + entry.get("content", [{}])[0].get("value", "") if entry.get("content") else entry.get("summary", "")
                    soup = BeautifulSoup(summary, "html.parser")
                    for a_tag in soup.find_all("a", href=True):
                        if a_tag["href"].lower().endswith(".pdf"):
                            pdf_url = a_tag["href"]
                            break
                if not pdf_url:
                    continue

                # Make absolute URL
                if pdf_url.startswith("/"):
                    from urllib.parse import urljoin
                    pdf_url = urljoin(feed_url, pdf_url)

                pdf_bytes = _download_pdf(pdf_url)
                if pdf_bytes is None:
                    continue
                doc_hash = _md5(pdf_bytes)
                if _hash_exists(doc_hash):
                    continue
                raw_text = _extract_text(pdf_bytes)
                if not raw_text.strip():
                    continue
                _save_regulation(title, source, pdf_url, raw_text, doc_hash)
                total += 1

        except Exception as exc:
            logger.warning("RSS feed failed for %s: %s", source, exc)
            continue

    return total


def _parse_html_for_pdfs(html: str, source: str, base_url: str) -> int:
    """Fallback parser within RSS: if feedparser returns nothing, scrape HTML for PDF links."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    count = 0
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not href.lower().endswith(".pdf"):
            continue
        pdf_url = urljoin(base_url, href)
        title = a_tag.get_text(strip=True) or "Untitled Circular"
        pdf_bytes = _download_pdf(pdf_url)
        if pdf_bytes is None:
            continue
        doc_hash = _md5(pdf_bytes)
        if _hash_exists(doc_hash):
            continue
        raw_text = _extract_text(pdf_bytes)
        if not raw_text.strip():
            continue
        _save_regulation(title, source, pdf_url, raw_text, doc_hash)
        count += 1
    return count


# =====================================================================
# OPTION 2 — PLAYWRIGHT BROWSER AUTOMATION
# =====================================================================
def _ingest_via_playwright() -> int:
    """Headless Chromium scraper for circular index pages."""
    from urllib.parse import urljoin

    total = 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed — skipping Option 2")
        return 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers(HEADERS)

            for source, index_url in INDEX_PAGES.items():
                try:
                    logger.info("Playwright: navigating to %s (%s)", source, index_url)
                    page.goto(index_url, timeout=45000, wait_until="networkidle")
                    # Simulate human scroll
                    page.evaluate("window.scrollBy(0, 300)")
                    page.wait_for_timeout(1500)

                    # Find all PDF links
                    links = page.eval_on_selector_all(
                        "a[href]",
                        """els => els
                            .filter(e => e.href && e.href.toLowerCase().endsWith('.pdf'))
                            .map(e => ({href: e.href, text: e.innerText}))
                        """,
                    )
                    for link_info in links:
                        pdf_url = link_info["href"]
                        title = link_info.get("text", "").strip() or "Untitled Circular"
                        if pdf_url.startswith("/"):
                            pdf_url = urljoin(index_url, pdf_url)
                        pdf_bytes = _download_pdf(pdf_url)
                        if pdf_bytes is None:
                            continue
                        doc_hash = _md5(pdf_bytes)
                        if _hash_exists(doc_hash):
                            continue
                        raw_text = _extract_text(pdf_bytes)
                        if not raw_text.strip():
                            continue
                        _save_regulation(title, source, pdf_url, raw_text, doc_hash)
                        total += 1
                except Exception as exc:
                    logger.warning("Playwright failed for %s: %s", source, exc)
                    continue

            browser.close()
    except Exception as exc:
        logger.error("Playwright engine failure: %s", exc)

    return total


# =====================================================================
# OPTION 3 — EMAIL IMAP INGESTION
# =====================================================================
def _ingest_via_email(
    host: str, port: int, user: str, password: str, folder: str = "INBOX"
) -> int:
    """Connect to IMAP inbox, download PDF attachments from official domains."""
    total = 0
    try:
        logger.info("Connecting to IMAP: %s@%s:%s", user, host, port)
        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(user, password)
        mail.select(folder)
        _, data = mail.search(None, "UNSEEN")
        if not data or not data[0]:
            mail.logout()
            return 0

        for msg_id in data[0].split():
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw_email)

            # Check sender domain
            from_addr = msg.get("From", "")
            if not any(domain in from_addr.lower() for domain in OFFICIAL_DOMAINS):
                continue

            # Determine source regulator from sender
            source = "RBI"
            if "sebi" in from_addr.lower():
                source = "SEBI"
            elif "irdai" in from_addr.lower():
                source = "IRDAI"

            subject = msg.get("Subject", "Untitled Circular")

            for part in msg.walk():
                content_type = part.get_content_type()
                filename = part.get_filename()
                if filename and (
                    content_type == "application/pdf"
                    or (filename and filename.lower().endswith(".pdf"))
                ):
                    pdf_bytes = part.get_payload(decode=True)
                    if pdf_bytes is None:
                        continue
                    doc_hash = _md5(pdf_bytes)
                    if _hash_exists(doc_hash):
                        continue
                    raw_text = _extract_text(pdf_bytes)
                    if not raw_text.strip():
                        continue
                    _save_regulation(
                        title=f"{subject} — {filename}",
                        source=source,
                        url=f"email://{from_addr}/{filename}",
                        raw_text=raw_text,
                        doc_hash=doc_hash,
                    )
                    total += 1

            # Mark as read
            mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as exc:
        logger.error("Email ingestion failed: %s\n%s", exc, traceback.format_exc())
    return total


# =====================================================================
# OPTION 4 — MANUAL UPLOAD (called from FastAPI endpoint)
# =====================================================================
def ingest_manual_upload(
    pdf_bytes: bytes,
    source: str,
    title: str,
    url: str = "",
) -> dict:
    """Process a manually uploaded PDF. Returns regulation dict or error."""
    doc_hash = _md5(pdf_bytes)
    if _hash_exists(doc_hash):
        return {"error": "Duplicate circular — already ingested", "doc_hash": doc_hash}

    raw_text = _extract_text(pdf_bytes)
    if not raw_text.strip():
        return {"error": "Could not extract text from PDF"}

    reg_id = db.new_id()
    db.execute(
        """INSERT INTO regulations (id, title, source, url, raw_text, doc_hash, ingested_at, status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (reg_id, title, source, url, raw_text, doc_hash, db.now_iso(), "new"),
    )
    db.insert_audit_entry(
        agent="ingestion",
        action="manual upload",
        detail={"source": source, "title": title, "url": url},
        regulation_id=reg_id,
    )
    logger.info("Manual upload ingested: %s [%s]", title[:80], source)
    return {
        "regulation_id": reg_id,
        "title": title,
        "source": source,
        "doc_hash": doc_hash,
    }


# =====================================================================
# LOCAL FOLDER WATCHER — scans data/incoming_pdfs/
# =====================================================================
def _ingest_local_folder() -> int:
    """Scan incoming_pdfs/ for any new PDF files."""
    total = 0
    for pdf_path in INCOMING_PDF_DIR.glob("*.pdf"):
        try:
            pdf_bytes = pdf_path.read_bytes()
            doc_hash = _md5(pdf_bytes)
            if _hash_exists(doc_hash):
                # Move to processed folder
                processed = INCOMING_PDF_DIR / "processed"
                processed.mkdir(exist_ok=True)
                pdf_path.rename(processed / pdf_path.name)
                continue
            raw_text = _extract_text(pdf_bytes)
            if not raw_text.strip():
                continue
            # Default to RBI for local files; source can be corrected later
            _save_regulation(
                title=pdf_path.stem.replace("_", " "),
                source="RBI",
                url=f"file://{pdf_path.as_posix()}",
                raw_text=raw_text,
                doc_hash=doc_hash,
            )
            # Move to processed
            processed = INCOMING_PDF_DIR / "processed"
            processed.mkdir(exist_ok=True)
            pdf_path.rename(processed / pdf_path.name)
            total += 1
        except Exception as exc:
            logger.warning("Failed to process local file %s: %s", pdf_path.name, exc)
    return total



def _scrape_web_listings() -> int:
    """Scrape the web listing pages of RBI, SEBI, and IRDAI for new PDF circulars."""
    from urllib.parse import urljoin
    total = 0
    
    # 1. RBI Master Directions Page
    rbi_md_url = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx"
    try:
        logger.info("Scraping RBI Master Directions page: %s", rbi_md_url)
        resp = requests.get(rbi_md_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            pdf_url = ""
            if "pdf" in href.lower() or href.lower().endswith(".pdf"):
                pdf_url = urljoin(rbi_md_url, href)
            elif "notificationuser.aspx" in href.lower() or "viewmasdirections.aspx" in href.lower():
                pdf_url = urljoin(rbi_md_url, href)
                
            if not pdf_url:
                continue
                
            title = a_tag.get_text(strip=True) or "RBI Master Direction Circular"
            if len(title) < 10 and a_tag.parent:
                title = a_tag.parent.get_text(strip=True) or title
            
            pdf_bytes = _download_pdf(pdf_url)
            if pdf_bytes is None:
                try:
                    detail_resp = requests.get(pdf_url, headers=HEADERS, timeout=15)
                    if "text/html" in detail_resp.headers.get("Content-Type", ""):
                        detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                        for d_a in detail_soup.find_all("a", href=True):
                            d_href = d_a["href"]
                            if "pdf" in d_href.lower() or d_href.lower().endswith(".pdf"):
                                inner_pdf_url = urljoin(pdf_url, d_href)
                                pdf_bytes = _download_pdf(inner_pdf_url)
                                if pdf_bytes and pdf_bytes[:5] == b"%PDF-":
                                    pdf_url = inner_pdf_url
                                    break
                except Exception:
                    pass
                    
            if pdf_bytes is None or pdf_bytes[:5] != b"%PDF-":
                continue
                
            doc_hash = _md5(pdf_bytes)
            if _hash_exists(doc_hash):
                continue
                
            raw_text = _extract_text(pdf_bytes)
            if not raw_text.strip():
                continue
                
            _save_regulation(title, "RBI", pdf_url, raw_text, doc_hash)
            total += 1
    except Exception as exc:
        logger.warning("Scraping RBI Master Directions page failed: %s", exc)

    # 2. SEBI Circulars Listing Page
    sebi_url = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&type=1"
    try:
        logger.info("Scraping SEBI Circulars page: %s", sebi_url)
        resp = requests.get(sebi_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            pdf_url = ""
            if "pdf" in href.lower() or href.lower().endswith(".pdf"):
                pdf_url = urljoin(sebi_url, href)
            elif "circulars" in href.lower() or "doListing" in href.lower():
                pdf_url = urljoin(sebi_url, href)
                
            if not pdf_url:
                continue
                
            title = a_tag.get_text(strip=True) or "SEBI Circular"
            if len(title) < 10 and a_tag.parent:
                title = a_tag.parent.get_text(strip=True) or title
                
            pdf_bytes = _download_pdf(pdf_url)
            if pdf_bytes is None:
                try:
                    detail_resp = requests.get(pdf_url, headers=HEADERS, timeout=15)
                    if "text/html" in detail_resp.headers.get("Content-Type", ""):
                        detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                        for d_a in detail_soup.find_all("a", href=True):
                            d_href = d_a["href"]
                            if "pdf" in d_href.lower() or d_href.lower().endswith(".pdf"):
                                inner_pdf_url = urljoin(pdf_url, d_href)
                                pdf_bytes = _download_pdf(inner_pdf_url)
                                if pdf_bytes and pdf_bytes[:5] == b"%PDF-":
                                    pdf_url = inner_pdf_url
                                    break
                except Exception:
                    pass
                    
            if pdf_bytes is None or pdf_bytes[:5] != b"%PDF-":
                continue
                
            doc_hash = _md5(pdf_bytes)
            if _hash_exists(doc_hash):
                continue
                
            raw_text = _extract_text(pdf_bytes)
            if not raw_text.strip():
                continue
                
            _save_regulation(title, "SEBI", pdf_url, raw_text, doc_hash)
            total += 1
    except Exception as exc:
        logger.warning("Scraping SEBI Circulars page failed: %s", exc)

    # 3. IRDAI Circulars Page
    irdai_url = "https://www.irdai.gov.in/ADMINCMS/cms/Circulars_Layout.aspx"
    try:
        logger.info("Scraping IRDAI Circulars page: %s", irdai_url)
        resp = requests.get(irdai_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            pdf_url = ""
            if "pdf" in href.lower() or href.lower().endswith(".pdf"):
                pdf_url = urljoin(irdai_url, href)
            elif "circulars" in href.lower() or "Circulars_Layout" in href.lower():
                pdf_url = urljoin(irdai_url, href)
                
            if not pdf_url:
                continue
                
            title = a_tag.get_text(strip=True) or "IRDAI Circular"
            if len(title) < 10 and a_tag.parent:
                title = a_tag.parent.get_text(strip=True) or title
                
            pdf_bytes = _download_pdf(pdf_url)
            if pdf_bytes is None:
                try:
                    detail_resp = requests.get(pdf_url, headers=HEADERS, timeout=15)
                    if "text/html" in detail_resp.headers.get("Content-Type", ""):
                        detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                        for d_a in detail_soup.find_all("a", href=True):
                            d_href = d_a["href"]
                            if "pdf" in d_href.lower() or d_href.lower().endswith(".pdf"):
                                inner_pdf_url = urljoin(pdf_url, d_href)
                                pdf_bytes = _download_pdf(inner_pdf_url)
                                if pdf_bytes and pdf_bytes[:5] == b"%PDF-":
                                    pdf_url = inner_pdf_url
                                    break
                except Exception:
                    pass
                    
            if pdf_bytes is None or pdf_bytes[:5] != b"%PDF-":
                continue
                
            doc_hash = _md5(pdf_bytes)
            if _hash_exists(doc_hash):
                continue
                
            raw_text = _extract_text(pdf_bytes)
            if not raw_text.strip():
                continue
                
            _save_regulation(title, "IRDAI", pdf_url, raw_text, doc_hash)
            total += 1
    except Exception as exc:
        logger.warning("Scraping IRDAI Circulars page failed: %s", exc)

    return total


# =====================================================================
# MAIN RUNNER — Cascade fallback with audit logging
# =====================================================================
def run_ingestion(
    email_host: str = "",
    email_port: int = 993,
    email_user: str = "",
    email_password: str = "",
    email_folder: str = "INBOX",
) -> dict:
    """Execute the 4-option ingestion cascade.
    Returns a summary dict with counts and which options were used.
    """
    summary: dict = {"total": 0, "methods_tried": [], "methods_succeeded": [], "errors": []}

    # Always check local folder first (independent of cascade)
    local_count = _ingest_local_folder()
    if local_count:
        summary["total"] += local_count
        summary["methods_succeeded"].append("local_folder")

    # ── Option 1: RSS & Website Scraping ──
    summary["methods_tried"].append("rss")
    try:
        rss_count = _ingest_via_rss()
        if rss_count > 0:
            summary["total"] += rss_count
            summary["methods_succeeded"].append("rss")
            logger.info("RSS ingestion succeeded: %d new circulars", rss_count)
    except Exception as exc:
        err = f"RSS failed: {exc}"
        summary["errors"].append(err)
        logger.warning(err)
        db.insert_audit_entry(
            agent="ingestion",
            action="fallback_step",
            detail={"method": "rss", "reason": str(exc)},
        )

    # ── Direct Web Listing Scraper ──
    summary["methods_tried"].append("web_listings")
    try:
        scrape_count = _scrape_web_listings()
        if scrape_count > 0:
            summary["total"] += scrape_count
            summary["methods_succeeded"].append("web_listings")
            logger.info("Web listings scraping succeeded: %d new circulars", scrape_count)
    except Exception as exc:
        err = f"Web listing scraping failed: {exc}"
        summary["errors"].append(err)
        logger.warning(err)
        db.insert_audit_entry(
            agent="ingestion",
            action="fallback_step",
            detail={"method": "web_listings", "reason": str(exc)},
        )

    # ── Option 2: Playwright ──
    summary["methods_tried"].append("playwright")
    try:
        pw_count = _ingest_via_playwright()
        if pw_count > 0:
            summary["total"] += pw_count
            summary["methods_succeeded"].append("playwright")
            logger.info("Playwright ingestion succeeded: %d new circulars", pw_count)
        else:
            logger.info("Playwright returned 0 — falling through to Option 3")
            db.insert_audit_entry(
                agent="ingestion",
                action="fallback",
                detail={"from": "playwright", "to": "email", "reason": "zero results"},
            )
    except Exception as exc:
        err = f"Playwright failed: {exc}"
        summary["errors"].append(err)
        logger.warning(err)
        db.insert_audit_entry(
            agent="ingestion",
            action="fallback",
            detail={"from": "playwright", "to": "email", "reason": str(exc)},
        )

    # ── Option 3: Email IMAP ──
    if email_host and email_user and email_password:
        summary["methods_tried"].append("email")
        try:
            em_count = _ingest_via_email(email_host, email_port, email_user, email_password, email_folder)
            if em_count > 0:
                summary["total"] += em_count
                summary["methods_succeeded"].append("email")
                logger.info("Email ingestion succeeded: %d new circulars", em_count)
            else:
                logger.info("Email ingestion returned 0")
        except Exception as exc:
            err = f"Email ingestion failed: {exc}"
            summary["errors"].append(err)
            logger.warning(err)
            db.insert_audit_entry(
                agent="ingestion",
                action="fallback",
                detail={"from": "email", "to": "manual_only", "reason": str(exc)},
            )
    else:
        logger.info("Email credentials not configured — skipping Option 3")

    # Option 4 (manual upload) is always available via the dashboard REST endpoint
    if summary["total"] == 0:
        logger.info(
            "All automatic ingestion options exhausted. "
            "Manual upload is available via the dashboard."
        )
        db.insert_audit_entry(
            agent="ingestion",
            action="all_auto_failed",
            detail={
                "methods_tried": summary["methods_tried"],
                "errors": summary["errors"],
            },
        )
    else:
        logger.info("Ingestion complete — %d new circulars found", summary["total"])
        db.insert_audit_entry(
            agent="ingestion",
            action="ingestion complete",
            detail={
                "total": summary["total"],
                "methods_succeeded": summary["methods_succeeded"],
            },
        )

    return summary

