import email
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from dedupe import canonical_url, dedupe_exact

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "news.json"
NEWSLETTER_DIAGNOSTICS_FILE = ROOT / "data" / "newsletter_diagnostics.json"
SOURCES_FILE = ROOT / "sources.json"
USER_AGENT = "JAM-Morning-Brief/1.0 (+personal news aggregator)"

LEXOLOGY_SOURCE = "Lexology Daily Newsfeed"
ELINFONET_SOURCE = "ELINfonet Daily Employment Law Update"

# ACC Newsstand contains several non-employment sections. These two sections are
# directly relevant to the user's employment practice; strong employment-law
# items in other sections are also allowed through by keyword screening.
ACC_RELEVANT_SECTIONS = {
    "employment & labor",
    "employee benefits & pensions",
}

ACC_SECTION_HEADINGS = {
    "employment & labor",
    "employee benefits & pensions",
    "legal practice",
    "legal tech",
    "acc resources",
    "articles",
    "other top stories",
    "international developments",
    "banking",
    "competition & antitrust",
    "corporate & commercial",
    "dispute resolution",
    "environment & climate change",
    "financial services",
    "insurance",
    "intellectual property",
    "life sciences",
    "privacy & data protection",
    "public",
    "real estate",
    "tax",
    "technology",
}

BOILERPLATE_PHRASES = {
    "view in browser",
    "my account",
    "about",
    "search archive",
    "follow on linkedin",
    "unsubscribe",
    "disclaimer",
    "privacy policy",
    "contact lexology",
    "about lexology",
    "centellic",
    "research methodology",
    "client choice",
    "contracts & clauses",
    "lexology talent management",
    "lexology index awards",
    "not an acc member",
    "try acc resources free",
    "advertise",
    "sponsor",
    "preferences",
    "update profile",
    "manage preferences",
}

EMPLOYMENT_TERMS = [
    "employment", "employee", "employer", "labor", "labour", "worker", "workplace",
    "eeoc", "nlrb", "department of labor", "dol", "osha", "wage", "overtime",
    "minimum wage", "discrimination", "harassment", "retaliation", "accommodation",
    "ada", "fmla", "leave", "pregnan", "lactation", "union", "collective bargaining",
    "classification", "independent contractor", "noncompete", "restrictive covenant",
    "paga", "cal/osha", "dlse", "civil rights department", "feha", "cfra", "erisa",
    "benefits", "pension", "pay transparency", "paid sick", "hiring", "termination",
    "layoff", "arbitration", "personnel", "human resources",
]


# ---------- Core helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    if not value:
        return now_iso()
    try:
        dt = dateparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return now_iso()


def item_id(title, url):
    raw = f"{canonical_url(url)}|{clean_text(title).lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def make_item(title, url, summary, source_name, category, priority, published_at=None, origin="web"):
    title = clean_text(title)
    url = (url or "").strip()
    return {
        "id": item_id(title, url),
        "title": title,
        "url": url,
        "summary": clean_text(summary)[:2200],
        "source": source_name,
        "category_hint": category,
        "priority": int(priority or 5),
        "published_at": parse_date(published_at),
        "collected_at": now_iso(),
        "origin": origin,
    }


def google_news_url(query):
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


def collect_feed(source):
    url = source.get("url")
    if source.get("type") == "google_news":
        url = google_news_url(source["query"])

    parsed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    items = []
    for entry in parsed.entries[:40]:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary") or entry.get("description") or ""
        published = entry.get("published") or entry.get("updated") or ""

        # Google News exposes the underlying publisher in entry.source.title.
        entry_source = entry.get("source") or {}
        actual_source = clean_text(entry_source.get("title", "")) if hasattr(entry_source, "get") else ""
        source_name = actual_source or source["name"]

        items.append(
            make_item(
                title,
                link,
                summary,
                source_name,
                source["category"],
                source.get("priority", 5),
                published,
                origin=source.get("type", "rss"),
            )
        )
    return items


# ---------- Email / IMAP ingestion ----------

def decode_mime_header(value):
    parts = []
    for chunk, enc in decode_header(value or ""):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def extract_html_from_message(msg):
    html = ""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/html":
                html += decoded
            elif ctype == "text/plain":
                text += decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                text = decoded
    return html, text


def html_to_link_lines(html, fallback_text=""):
    """Convert HTML into ordered text lines while preserving link titles + URLs."""
    if not html:
        return [line.strip() for line in (fallback_text or "").splitlines() if line.strip()]

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text(" ", strip=True))
        href = (a.get("href") or "").strip()
        # A rare title may contain our separator; normalize it.
        title = title.replace("|||", " | ")
        a.replace_with(f"\n__JAM_LINK__|||{title}|||{href}\n")

    raw = soup.get_text("\n")
    return [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines() if line.strip()]


def is_http_url(url):
    return (url or "").lower().startswith(("http://", "https://"))


def is_boilerplate(title, url):
    low = clean_text(title).lower()
    if not low or len(low) < 12:
        return True
    if any(phrase in low for phrase in BOILERPLATE_PHRASES):
        return True
    host = urlparse(url or "").netloc.lower()
    if any(site in host for site in ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com")):
        return True
    return False


def is_employment_relevant(text):
    low = (text or "").lower()
    return any(term in low for term in EMPLOYMENT_TERMS)


def category_for_legal_text(text):
    low = (text or "").lower()
    if any(x in low for x in [
        "california", "ninth circuit", "9th circuit", "cal/osha", "dlse", "feha", "cfra", "paga",
        "northern district of california", "eastern district of california",
        "central district of california", "southern district of california",
    ]):
        return "California Law", 12
    if any(x in low for x in ["minnesota", "eighth circuit", "8th circuit", "district of minnesota"]):
        return "Minnesota Law", 11
    if any(x in low for x in ["supreme court", "circuit", "district court", "eeoc", "nlrb", "department of labor", "osha", "federal"]):
        return "Employment Law", 11
    return "Employment Law", 10


def lookahead_summary(lines, start_index, max_lines=5):
    pieces = []
    for line in lines[start_index + 1:start_index + 10]:
        low = line.lower()
        if line.startswith("__JAM_LINK__|||"):
            break
        if low in ACC_SECTION_HEADINGS or low in {"usa", "canada", "global", "north america", "europe"}:
            if pieces:
                break
            continue
        if any(phrase in low for phrase in BOILERPLATE_PHRASES):
            continue
        if len(line) <= 1:
            continue
        pieces.append(line)
        if len(pieces) >= max_lines:
            break
    return " ".join(pieces)


def parse_acc_newsstand(msg):
    """Extract substantive Employment & Labor / Benefits items from ACC Newsstand email."""
    html, text = extract_html_from_message(msg)
    lines = html_to_link_lines(html, text)
    out = []
    current_section = ""
    message_date = msg.get("Date", "")

    for idx, line in enumerate(lines):
        low = line.lower().strip()
        if low in ACC_SECTION_HEADINGS:
            current_section = low
            continue

        if not line.startswith("__JAM_LINK__|||"):
            continue

        try:
            _, title, href = line.split("|||", 2)
        except ValueError:
            continue

        title = clean_text(title)
        href = href.strip()
        if not is_http_url(href) or is_boilerplate(title, href):
            continue

        summary = lookahead_summary(lines, idx)
        combined = f"{title} {summary}"
        in_relevant_section = current_section in ACC_RELEVANT_SECTIONS
        if not in_relevant_section and not is_employment_relevant(combined):
            continue

        # Skip obvious non-article event/promotional links even inside a legal section.
        low_combined = combined.lower()
        if any(x in low_combined for x in ["webinar", "register now", "event registration", "conference", "award ceremony"]):
            continue

        category, priority = category_for_legal_text(combined)
        section_label = current_section.title() if current_section else "Employment / Labor"
        enriched_summary = f"ACC Newsstand section: {section_label}. {summary}".strip()
        out.append(
            make_item(
                title,
                href,
                enriched_summary,
                LEXOLOGY_SOURCE,
                category,
                priority,
                message_date,
                origin="email:acc-newsstand",
            )
        )

    return dedupe_exact(out)[:80]


def parse_elinfonet(msg):
    """Extract substantive article links from the ELINfonet daily employment-law email."""
    html, text = extract_html_from_message(msg)
    lines = html_to_link_lines(html, text)
    out = []
    message_date = msg.get("Date", "")
    current_section = "Employment Law"

    likely_section_words = {
        "federal law", "state law", "human resources", "labor relations", "california",
        "employment law", "benefits", "wage and hour", "immigration", "osha", "workplace safety",
    }

    for idx, line in enumerate(lines):
        low = line.lower().strip()
        if low in likely_section_words or (len(line) < 60 and low.endswith(" law")):
            current_section = line
            continue

        if not line.startswith("__JAM_LINK__|||"):
            continue

        try:
            _, title, href = line.split("|||", 2)
        except ValueError:
            continue

        title = clean_text(title)
        href = href.strip()
        if not is_http_url(href) or is_boilerplate(title, href):
            continue
        if len(title) < 18:
            continue

        summary = lookahead_summary(lines, idx)
        combined = f"{title} {summary} {current_section}"

        # ELINfonet is an employment-law newsletter, but strip clear ads/events/utility links.
        low_combined = combined.lower()
        if any(x in low_combined for x in [
            "webinar", "register now", "conference", "sponsored", "advertisement",
            "download our", "subscribe", "unsubscribe", "privacy", "contact us",
        ]):
            continue

        category, priority = category_for_legal_text(combined)
        enriched_summary = f"ELINfonet section: {current_section}. {summary}".strip()
        out.append(
            make_item(
                title,
                href,
                enriched_summary,
                ELINFONET_SOURCE,
                category,
                priority,
                message_date,
                origin="email:elinfonet",
            )
        )

    return dedupe_exact(out)[:100]


def select_all_mail(mail):
    # Gmail's All Mail folder is preferable because category rules or user actions may archive a newsletter.
    for mailbox in ('"[Gmail]/All Mail"', "INBOX"):
        status, _ = mail.select(mailbox, readonly=True)
        if status == "OK":
            return mailbox
    raise RuntimeError("Could not select Gmail All Mail or INBOX")


def search_message_ids(mail, since):
    searches = [
        f'(FROM "noreply.acc@lexology.com" SINCE "{since}")',
        f'(SUBJECT "ACC Newsstand" SINCE "{since}")',
        f'(FROM "update@elinfonet.com" SINCE "{since}")',
        f'(SUBJECT "EMPLOYMENT LAW UPDATE" SINCE "{since}")',
        f'(SUBJECT "EMPLOYMENT LAW INFORMATION NETWORK" SINCE "{since}")',
    ]
    found = set()
    for criteria in searches:
        try:
            status, data = mail.search(None, criteria)
            if status == "OK" and data and data[0]:
                found.update(data[0].split())
        except Exception as exc:
            print(f"WARNING: IMAP search failed for {criteria}: {exc}")
    return sorted(found, key=lambda x: int(x))


def classify_newsletter(msg, preview_text=""):
    sender = decode_mime_header(msg.get("From", "")).lower()
    subject = decode_mime_header(msg.get("Subject", "")).lower()
    preview = (preview_text or "").lower()[:5000]

    if "noreply.acc@lexology.com" in sender or "acc newsstand" in subject:
        return "acc"
    if (
        "update@elinfonet.com" in sender
        or "employment law update" in subject
        or "employment law information network" in subject
        or "employment law information network" in preview
    ):
        return "elinfonet"
    return ""


def collect_newsletter_email_items():
    """Collect ACC Newsstand + forwarded ELINfonet from one personal Gmail mailbox."""
    host = os.getenv("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com"
    user = os.getenv("IMAP_USER", "").strip()
    password = re.sub(r"\s+", "", os.getenv("IMAP_APP_PASSWORD", ""))
    lookback_days = int(os.getenv("NEWSLETTER_LOOKBACK_DAYS", "7"))

    diagnostics = {
        "imap_configured": bool(user and password),
        "imap_user": user if user else "",
        "lookback_days": lookback_days,
        "mailbox": "",
        "matched_message_count": 0,
        "acc_message_count": 0,
        "elinfonet_message_count": 0,
        "acc_article_count": 0,
        "elinfonet_article_count": 0,
        "messages": [],
        "collected_at": now_iso(),
    }

    if not user or not password:
        diagnostics["status"] = "IMAP secrets not configured; newsletter email ingestion skipped."
        return [], diagnostics

    mail = imaplib.IMAP4_SSL(host)
    out = []
    try:
        mail.login(user, password)
        diagnostics["mailbox"] = select_all_mail(mail)
        since = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        ids = search_message_ids(mail, since)[-20:]
        diagnostics["matched_message_count"] = len(ids)

        for msg_id in ids:
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            html, text = extract_html_from_message(msg)
            preview = clean_text(text or html)[:5000]
            kind = classify_newsletter(msg, preview)
            if not kind:
                continue

            subject = decode_mime_header(msg.get("Subject", ""))
            sender = decode_mime_header(msg.get("From", ""))
            date = msg.get("Date", "")

            if kind == "acc":
                batch = parse_acc_newsstand(msg)
                diagnostics["acc_message_count"] += 1
                diagnostics["acc_article_count"] += len(batch)
            else:
                batch = parse_elinfonet(msg)
                diagnostics["elinfonet_message_count"] += 1
                diagnostics["elinfonet_article_count"] += len(batch)

            diagnostics["messages"].append({
                "kind": kind,
                "subject": subject,
                "from": sender,
                "date": date,
                "articles_extracted": len(batch),
            })
            out.extend(batch)

        diagnostics["status"] = "ok"
        return dedupe_exact(out), diagnostics
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# ---------- Persistence ----------

def load_existing():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_items(items):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    kept = []
    for item in dedupe_exact(items):
        try:
            dt = dateparser.parse(item.get("published_at", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        if dt >= cutoff:
            kept.append(item)
    kept.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    DATA_FILE.write_text(json.dumps(kept[:3000], indent=2, ensure_ascii=False), encoding="utf-8")


def save_newsletter_diagnostics(diagnostics):
    NEWSLETTER_DIAGNOSTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    NEWSLETTER_DIAGNOSTICS_FILE.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main():
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    new_items = []

    for source in sources:
        try:
            batch = collect_feed(source)
            print(f"{source['name']}: {len(batch)} items")
            new_items.extend(batch)
        except Exception as exc:
            print(f"WARNING: {source['name']} failed: {exc}")

    # v7: Lexology web scraping is intentionally disabled. The ACC Newsstand email is
    # the authoritative Lexology input, and forwarded ELINfonet is read from the same mailbox.
    try:
        batch, diagnostics = collect_newsletter_email_items()
        print(
            "Newsletter email ingestion:",
            f"ACC {diagnostics.get('acc_message_count', 0)} messages / {diagnostics.get('acc_article_count', 0)} articles;",
            f"ELINfonet {diagnostics.get('elinfonet_message_count', 0)} messages / {diagnostics.get('elinfonet_article_count', 0)} articles."
        )
        print("Newsletter diagnostics:", json.dumps(diagnostics, ensure_ascii=False))
        new_items.extend(batch)
        save_newsletter_diagnostics(diagnostics)
    except Exception as exc:
        diagnostics = {
            "status": "error",
            "error": str(exc),
            "collected_at": now_iso(),
        }
        save_newsletter_diagnostics(diagnostics)
        print(f"WARNING: Newsletter email ingestion failed: {exc}")

    existing = load_existing()
    save_items(new_items + existing)
    print(f"Saved {len(load_existing())} total items to {DATA_FILE}")


if __name__ == "__main__":
    main()
