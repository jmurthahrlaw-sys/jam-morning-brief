import email
import hashlib
import imaplib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
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

# ACC Newsstand contains several non-employment sections. Employment & Labor is
# the core section for JAM; strong employment-law items elsewhere may still pass
# the keyword screen.
ACC_RELEVANT_SECTIONS = {
    "employment & labor",
}

# Track the country heading in ACC Newsstand so the U.S. practice feed does not
# spend candidate budget on Canada/Europe/other international employment items.
ACC_COUNTRY_HEADINGS = {
    "usa", "united states", "canada", "global", "australia", "brazil", "china",
    "czech republic", "france", "germany", "hungary", "india", "ireland", "italy",
    "japan", "mexico", "netherlands", "new zealand", "north macedonia", "poland",
    "portugal", "singapore", "south africa", "south korea", "spain", "sweden",
    "switzerland", "taiwan", "thailand", "turkey", "united kingdom", "uk",
}
US_COUNTRY_HEADINGS = {"usa", "united states"}

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


def _jsonld_dates(obj):
    """Yield datePublished/dateModified values from nested JSON-LD."""
    if isinstance(obj, dict):
        for key in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
            value = obj.get(key)
            if value:
                yield value
        for value in obj.values():
            yield from _jsonld_dates(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _jsonld_dates(value)


def _plausible_article_date(value):
    if not value:
        return ""
    try:
        dt = dateparser.parse(str(value))
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        # Reject obviously bogus metadata and dates far in the future.
        if dt.year < 2018 or dt > datetime.now(timezone.utc) + timedelta(days=2):
            return ""
        return dt.isoformat()
    except Exception:
        return ""


def fetch_article_metadata(url):
    """Best-effort fetch of an article's own publication date and description.

    Newsletter issue dates are not reliable article publication dates.  This
    function follows the article link and checks standard metadata/JSON-LD so
    stale stories can be filtered deterministically later.  Failures are safe:
    the newsletter issue date remains the fallback.
    """
    try:
        r = requests.get(
            url,
            timeout=float(os.getenv("ARTICLE_METADATA_TIMEOUT", "10")),
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code >= 400 or "html" not in (r.headers.get("Content-Type", "").lower()):
            return {}
        soup = BeautifulSoup(r.text, "html.parser")

        date_values = []
        for attrs in (
            {"property": "article:published_time"},
            {"property": "og:published_time"},
            {"name": "date"},
            {"name": "datePublished"},
            {"name": "publish-date"},
            {"name": "publication_date"},
            {"itemprop": "datePublished"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                date_values.append(tag.get("content"))

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or script.get_text() or "")
            except Exception:
                continue
            date_values.extend(_jsonld_dates(payload))

        for t in soup.find_all("time", datetime=True)[:8]:
            date_values.append(t.get("datetime"))

        published_at = ""
        for value in date_values:
            published_at = _plausible_article_date(value)
            if published_at:
                break

        description = ""
        for attrs in (
            {"property": "og:description"},
            {"name": "description"},
            {"name": "twitter:description"},
        ):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                description = clean_text(tag.get("content"))
                if len(description) >= 60:
                    break

        return {
            "article_published_at": published_at,
            "article_description": description[:1400],
            "resolved_url": r.url,
        }
    except Exception:
        return {}


def enrich_newsletter_items(items):
    if not items:
        return items, {"dates_resolved": 0, "pages_fetched": 0}
    max_workers = max(1, min(int(os.getenv("ARTICLE_METADATA_WORKERS", "8")), 12))
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(fetch_article_metadata, item.get("url", "")): idx for idx, item in enumerate(items)}
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result() or {}
            except Exception:
                results[idx] = {}

    dates_resolved = 0
    pages_fetched = 0
    enriched = []
    for idx, original in enumerate(items):
        item = dict(original)
        meta = results.get(idx, {})
        if meta:
            pages_fetched += 1
        if meta.get("article_published_at"):
            item["newsletter_issue_at"] = item.get("published_at", "")
            item["published_at"] = meta["article_published_at"]
            item["date_basis"] = "article_metadata"
            dates_resolved += 1
        else:
            item["date_basis"] = "newsletter_issue_fallback"
        if meta.get("article_description"):
            existing = item.get("summary", "")
            item["summary"] = clean_text(f"{existing} Source page summary: {meta['article_description']}")[:2200]
        if meta.get("resolved_url"):
            item["resolved_url"] = meta["resolved_url"]
        enriched.append(item)
    return enriched, {"dates_resolved": dates_resolved, "pages_fetched": pages_fetched}


def parse_acc_newsstand(msg):
    """Extract substantive U.S. Employment & Labor items from ACC Newsstand."""
    html, text = extract_html_from_message(msg)
    lines = html_to_link_lines(html, text)
    out = []
    current_section = ""
    current_country = ""
    message_date = msg.get("Date", "")

    for idx, line in enumerate(lines):
        low = line.lower().strip()
        if low in ACC_COUNTRY_HEADINGS:
            current_country = low
            continue
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

        # ACC is global.  JAM's specialist use is U.S. employment law, with
        # California first.  Once the issue moves to another country, do not
        # spend candidate budget on that jurisdiction.
        if current_country and current_country not in US_COUNTRY_HEADINGS:
            continue

        in_relevant_section = current_section in ACC_RELEVANT_SECTIONS
        if not in_relevant_section and not is_employment_relevant(combined):
            continue

        # Employee benefits/pension pieces are not core employment/labor notes
        # unless the title/summary contains a stronger workplace-law signal.
        if current_section == "employee benefits & pensions" and not any(
            term in combined.lower() for term in (
                "employment", "employer", "eeoc", "nlrb", "wage", "leave",
                "discrimination", "harassment", "labor", "workplace", "worker",
            )
        ):
            continue

        low_combined = combined.lower()
        if any(x in low_combined for x in [
            "webinar", "register now", "event registration", "conference",
            "award ceremony", "podcast episode", "video series",
        ]):
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

    return dedupe_exact(out)[:70]

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


def _message_sort_time(msg, msg_id):
    try:
        dt = dateparser.parse(msg.get("Date", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromtimestamp(int(msg_id), tz=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


def collect_newsletter_email_items():
    """Collect the newest ACC Newsstand + ELINfonet issue from the JAM mailbox.

    The IMAP search can look back several days so weekends/holidays are safe,
    but only the newest issue of each newsletter is parsed.  Old newsletter
    issues therefore do not keep flooding the candidate pool.
    """
    host = os.getenv("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com"
    user = os.getenv("IMAP_USER", "").strip()
    password = re.sub(r"\s+", "", os.getenv("IMAP_APP_PASSWORD", ""))
    lookback_days = int(os.getenv("NEWSLETTER_LOOKBACK_DAYS", "7"))
    issues_per_source = max(1, int(os.getenv("NEWSLETTER_ISSUES_PER_SOURCE", "1")))

    diagnostics = {
        "imap_configured": bool(user and password),
        "imap_user": user if user else "",
        "lookback_days": lookback_days,
        "issues_per_source": issues_per_source,
        "mailbox": "",
        "matched_message_count": 0,
        "selected_issue_count": 0,
        "acc_message_count": 0,
        "elinfonet_message_count": 0,
        "acc_article_count": 0,
        "elinfonet_article_count": 0,
        "article_pages_fetched": 0,
        "article_dates_resolved": 0,
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
        ids = search_message_ids(mail, since)[-30:]
        diagnostics["matched_message_count"] = len(ids)

        records = []
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
            records.append((kind, _message_sort_time(msg, msg_id), int(msg_id), msg))

        selected_records = []
        for kind in ("acc", "elinfonet"):
            kind_records = [r for r in records if r[0] == kind]
            kind_records.sort(key=lambda r: (r[1], r[2]), reverse=True)
            selected_records.extend(kind_records[:issues_per_source])

        diagnostics["selected_issue_count"] = len(selected_records)

        for kind, _, _, msg in sorted(selected_records, key=lambda r: r[1]):
            subject = decode_mime_header(msg.get("Subject", ""))
            sender = decode_mime_header(msg.get("From", ""))
            date = msg.get("Date", "")

            if kind == "acc":
                batch = parse_acc_newsstand(msg)
                diagnostics["acc_message_count"] += 1
            else:
                batch = parse_elinfonet(msg)
                diagnostics["elinfonet_message_count"] += 1

            batch, meta_diag = enrich_newsletter_items(batch)
            diagnostics["article_pages_fetched"] += meta_diag.get("pages_fetched", 0)
            diagnostics["article_dates_resolved"] += meta_diag.get("dates_resolved", 0)

            if kind == "acc":
                diagnostics["acc_article_count"] += len(batch)
            else:
                diagnostics["elinfonet_article_count"] += len(batch)

            diagnostics["messages"].append({
                "kind": kind,
                "subject": subject,
                "from": sender,
                "date": date,
                "articles_extracted": len(batch),
                "article_dates_resolved": meta_diag.get("dates_resolved", 0),
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
