import email
import hashlib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from dedupe import canonical_url, dedupe_exact

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "news.json"
SOURCES_FILE = ROOT / "sources.json"
USER_AGENT = "JAM-Morning-Brief/1.0 (+personal news aggregator)"
TIMEOUT = 25


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
        "summary": clean_text(summary)[:1800],
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

        # Google News RSS usually exposes the actual publisher in entry.source.title.
        # Use it when available so the digest can distinguish MPR, Reuters, a law firm, etc.
        entry_source = entry.get("source") or {}
        if hasattr(entry_source, "get"):
            actual_source = clean_text(entry_source.get("title", ""))
        else:
            actual_source = ""
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


def dated_lexology_url(raw_url):
    """Preserve the user's private feed id but replace d= with today's date."""
    parsed = urlparse(raw_url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["d"] = datetime.now().strftime("%Y-%m-%d")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q), parsed.fragment))


def collect_lexology():
    raw_url = os.getenv("LEXOLOGY_NEWSFEED_URL", "").strip()
    if not raw_url:
        return []
    url = dated_lexology_url(raw_url)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    skip = re.compile(r"^(home|sign in|register|privacy|terms|unsubscribe|view|manage|contact)$", re.I)
    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text(" ", strip=True))
        href = a.get("href", "").strip()
        if len(title) < 18 or skip.match(title):
            continue
        if href.startswith("/"):
            href = "https://www.lexology.com" + href
        if not href.startswith("http"):
            continue
        parent_text = clean_text(a.parent.get_text(" ", strip=True)) if a.parent else ""
        items.append(
            make_item(
                title,
                href,
                parent_text,
                "Lexology Daily Newsfeed",
                "Legal — Unsorted",
                10,
                now_iso(),
                origin="lexology",
            )
        )
    return dedupe_exact(items)[:100]


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


def collect_elinfonet_email():
    """Optional IMAP ingestion. Does nothing until IMAP secrets are configured."""
    host = os.getenv("IMAP_HOST", "imap.gmail.com")
    user = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_APP_PASSWORD", "").strip()
    sender = os.getenv("ELINFONET_SENDER", "update@elinfonet.com").strip()
    if not user or not password:
        return []

    mail = imaplib.IMAP4_SSL(host)
    try:
        mail.login(user, password)
        mail.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(FROM "{sender}" SINCE "{since}")')
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-3:]
        out = []
        for msg_id in ids:
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", "ELINfonet Daily Employment Law Update"))
            message_date = msg.get("Date", "")
            html, text = extract_html_from_message(msg)
            soup = BeautifulSoup(html or text, "html.parser")
            for a in soup.find_all("a", href=True):
                title = clean_text(a.get_text(" ", strip=True))
                href = a.get("href", "").strip()
                if len(title) < 20 or not href.startswith("http"):
                    continue
                # Skip newsletter-management and ad links.
                low = title.lower()
                if any(x in low for x in ["unsubscribe", "privacy policy", "update profile", "try it free", "ask an hr ai", "policy drafter"]):
                    continue
                parent_text = clean_text(a.parent.get_text(" ", strip=True)) if a.parent else subject
                out.append(
                    make_item(
                        title,
                        href,
                        parent_text,
                        "ELINfonet Daily Employment Law Update",
                        "Employment Law",
                        10,
                        message_date,
                        origin="email",
                    )
                )
        return dedupe_exact(out)[:100]
    finally:
        try:
            mail.logout()
        except Exception:
            pass


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
    DATA_FILE.write_text(json.dumps(kept[:2500], indent=2, ensure_ascii=False), encoding="utf-8")


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

    try:
        batch = collect_lexology()
        print(f"Lexology: {len(batch)} items")
        new_items.extend(batch)
    except Exception as exc:
        print(f"WARNING: Lexology failed: {exc}")

    try:
        batch = collect_elinfonet_email()
        print(f"ELINfonet email: {len(batch)} items")
        new_items.extend(batch)
    except Exception as exc:
        print(f"WARNING: ELINfonet email failed: {exc}")

    existing = load_existing()
    save_items(new_items + existing)
    print(f"Saved {len(load_existing())} total items to {DATA_FILE}")


if __name__ == "__main__":
    main()
