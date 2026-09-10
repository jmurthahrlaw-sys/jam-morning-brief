import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from dedupe import dedupe_exact, dedupe_near, normalize_title, canonical_url
from rapidfuzz.fuzz import ratio, token_set_ratio

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "news.json"
PROFILE_FILE = ROOT / "editorial_profile.txt"
OUTPUT = ROOT / "output"
HISTORY_FILE = ROOT / "data" / "brief_history.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TRUSTED_LEGAL_SOURCES = {
    "Lexology Daily Newsfeed",
    "ELINfonet Daily Employment Law Update",
}

LEGAL_CATEGORIES = {
    "U.S. Supreme Court",
    "Federal Courts",
    "Minnesota Law",
    "California Law",
    "Employment Law",
    "Legal — Unsorted",
}

EMPLOYMENT_TERMS = [
    "employment", "employee", "employer", "labor", "labour", "eeoc", "nlrb",
    "department of labor", "wage", "hour", "overtime", "minimum wage",
    "discrimination", "harassment", "retaliation", "accommodation", "ada",
    "fmla", "leave", "pregnan", "lactation", "union", "collective bargaining",
    "worker classification", "independent contractor", "noncompete",
    "restrictive covenant", "paga", "cal/osha", "dlse", "civil rights department",
    "workplace", "hiring", "termination", "layoff", "pay transparency",
    "paid sick", "personnel", "human resources", "worker", "workers",
]

GENERAL_QUOTAS = {
    "Top News": 40,
    "Politics & Government": 18,
    "Business & Economy": 16,
    "Minnesota": 18,
    "Health & Science": 10,
    "Education & Higher Education": 8,
    "Tech & AI": 16,
    "Entertainment & Culture": 16,
    "Good News": 16,
}


def fix_text_encoding(value):
    text = html.unescape(str(value or ""))
    # Common UTF-8 bytes accidentally decoded as Windows-1252/Latin-1.
    markers = ("â", "Â", "Ã", "ðŸ")
    if any(m in text for m in markers):
        for enc in ("cp1252", "latin-1"):
            try:
                repaired = text.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            old_score = sum(text.count(m) for m in markers)
            new_score = sum(repaired.count(m) for m in markers)
            if new_score < old_score:
                text = repaired
                break
    replacements = {
        "â€™": "’", "â€˜": "‘", "â€œ": "“", "â€": "”",
        "â€“": "–", "â€”": "—", "Â ": " ", "Â": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalize_item(item):
    out = dict(item)
    for key in ("title", "summary", "source", "category_hint", "origin"):
        out[key] = fix_text_encoding(out.get(key, ""))
    return out


def within_lookback(item, hours):
    try:
        dt = dateparser.parse(item.get("published_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        return True


def legal_story_key(item):
    url = canonical_url(item.get("url", ""))
    if url:
        return url
    return normalize_title(item.get("title", "") or item.get("heading", ""))


def load_legal_history(days=21):
    if not HISTORY_FILE.exists():
        return []
    try:
        payload = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        entries = payload.get("legal", []) if isinstance(payload, dict) else []
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for e in entries:
        try:
            dt = dateparser.parse(e.get("used_at", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(e)
        except Exception:
            continue
    return kept


def previously_used_legal(item, history):
    key = legal_story_key(item)
    title = normalize_title(item.get("title", ""))
    for entry in history:
        if key and key == entry.get("key"):
            return True
        old_title = normalize_title(entry.get("title", ""))
        if title and old_title and token_set_ratio(title, old_title) >= 88:
            return True
    return False


def save_legal_history(notes, existing_history):
    now = datetime.now(timezone.utc).isoformat()
    entries = list(existing_history)
    for note in notes:
        title = note.get("heading", "")
        url = note.get("url", "")
        key = canonical_url(url) or normalize_title(title)
        if not key:
            continue
        entries.append({"key": key, "title": title, "url": url, "used_at": now})
    # Exact key de-dupe, newest wins.
    deduped = {}
    for entry in entries:
        deduped[entry.get("key", "")] = entry
    final = [e for k, e in deduped.items() if k]
    final.sort(key=lambda e: e.get("used_at", ""), reverse=True)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps({"legal": final[:250]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def is_employment_relevant(item):
    text = " ".join([
        item.get("title", ""),
        item.get("summary", ""),
        item.get("category_hint", ""),
    ]).lower()
    return any(term in text for term in EMPLOYMENT_TERMS)


STRONG_EMPLOYMENT_TERMS = [
    "eeoc", "nlrb", "department of labor", "dol ", "wage", "overtime",
    "minimum wage", "discrimination", "harassment", "retaliation",
    "ada", "fmla", "paid leave", "sick leave", "pregnan", "lactation",
    "union", "collective bargaining", "worker classification",
    "independent contractor", "noncompete", "restrictive covenant",
    "paga", "cal/osha", "dlse", "civil rights department",
    "employment law", "labor law", "workplace law", "pay transparency",
]

LEGAL_SIGNAL_TERMS = [
    "court", "circuit", "supreme court", "district court", "lawsuit", "sued",
    "ruling", "decision", "holding", "opinion", "statute", "legislation",
    "bill", "law", "regulation", "rule", "guidance", "agency", "enforcement",
    "settlement", "injunction", "appeal", "administrative",
]


def is_strong_employment_legal(item):
    text = " ".join([
        item.get("title", ""),
        item.get("summary", ""),
        item.get("category_hint", ""),
    ]).lower()

    if any(term in text for term in STRONG_EMPLOYMENT_TERMS):
        return True

    employment_signal = any(term in text for term in [
        "employment", "employee", "employer", "labor", "workplace",
        "worker", "workers", "hiring", "termination", "layoff",
        "human resources", "personnel",
    ])
    legal_signal = any(term in text for term in LEGAL_SIGNAL_TERMS)
    return employment_signal and legal_signal



CALIFORNIA_MARKERS = [
    "california", "ninth circuit", "9th circuit",
    "northern district of california", "eastern district of california",
    "central district of california", "southern district of california",
    "n.d. cal", "e.d. cal", "c.d. cal", "s.d. cal",
    "cal/osha", "dlse", "department of industrial relations",
    "civil rights department", "feha", "cfra", "paga",
    "private attorneys general act", "california labor code",
    "california supreme court", "california court of appeal",
]


def is_california_employment(item):
    text = " ".join([
        item.get("title", ""),
        item.get("summary", ""),
        item.get("category_hint", ""),
        item.get("origin", ""),
        item.get("source", ""),
    ]).lower()

    california_signal = (
        item.get("category_hint") == "California Law"
        or any(marker in text for marker in CALIFORNIA_MARKERS)
    )
    return california_signal and is_strong_employment_legal(item)


def item_rank(item):
    return (
        2 if item.get("source") in TRUSTED_LEGAL_SOURCES else 0,
        1 if is_employment_relevant(item) else 0,
        int(item.get("priority", 0)),
        item.get("published_at", ""),
    )


def unique_append(selected, seen, item):
    key = canonical_url(item.get("url", "")) or normalize_title(item.get("title", ""))
    if not key or key in seen:
        return
    selected.append(item)
    seen.add(key)


def select_general_candidates(items, max_items):
    eligible = [
        i for i in items
        if i.get("source") not in TRUSTED_LEGAL_SOURCES
        and i.get("category_hint") not in LEGAL_CATEGORIES
    ]

    buckets = defaultdict(list)
    for item in sorted(eligible, key=lambda x: (int(x.get("priority", 0)), x.get("published_at", "")), reverse=True):
        buckets[item.get("category_hint", "Top News")].append(item)

    selected, seen = [], set()
    for category, quota in GENERAL_QUOTAS.items():
        for item in buckets.get(category, [])[:quota]:
            unique_append(selected, seen, item)

    for item in sorted(eligible, key=lambda x: (int(x.get("priority", 0)), x.get("published_at", "")), reverse=True):
        if len(selected) >= max_items:
            break
        unique_append(selected, seen, item)
    return selected[:max_items]


def select_legal_candidates(items, max_items):
    selected, seen = [], set()

    # 1. Specialist sources are always reviewed broadly.
    for source_name in ("Lexology Daily Newsfeed", "ELINfonet Daily Employment Law Update"):
        source_items = [i for i in items if i.get("source") == source_name]
        for item in sorted(source_items, key=item_rank, reverse=True)[:80]:
            unique_append(selected, seen, item)

    # 2. CALIFORNIA FIRST: dedicated California state + Ninth Circuit/CA federal employment material.
    california_items = [
        i for i in items
        if i.get("source") not in TRUSTED_LEGAL_SOURCES
        and is_california_employment(i)
    ]
    for item in sorted(california_items, key=item_rank, reverse=True)[:70]:
        if len(selected) >= max_items:
            break
        unique_append(selected, seen, item)

    # 3. Other dedicated employment/labor legal feeds.
    dedicated = [
        i for i in items
        if i.get("source") not in TRUSTED_LEGAL_SOURCES
        and i.get("category_hint") in LEGAL_CATEGORIES
        and is_strong_employment_legal(i)
        and not is_california_employment(i)
    ]
    for item in sorted(dedicated, key=item_rank, reverse=True):
        if len(selected) >= max_items:
            break
        unique_append(selected, seen, item)

    # 4. General-news spillover only for unmistakable employment/labor legal developments.
    spillover = [
        i for i in items
        if i.get("source") not in TRUSTED_LEGAL_SOURCES
        and i.get("category_hint") not in LEGAL_CATEGORIES
        and is_strong_employment_legal(i)
        and any(term in (" " + i.get("title", "") + " " + i.get("summary", "")).lower()
                for term in ("eeoc", "nlrb", "department of labor", "employment law",
                             "labor law", "wage", "overtime", "discrimination",
                             "retaliation", "fmla", "ada ", "paga", "cal/osha", "dlse"))
    ]
    for item in sorted(spillover, key=item_rank, reverse=True):
        if len(selected) >= max_items:
            break
        unique_append(selected, seen, item)

    return selected[:max_items]


def _norm_for_quote(value):
    return re.sub(r"\s+", " ", fix_text_encoding(value or "")).strip()


def _quote_supported(quote, evidence):
    quote_n = _norm_for_quote(quote)
    evidence_n = _norm_for_quote(evidence)
    return bool(quote_n and len(quote_n) >= 10 and quote_n.lower() in evidence_n.lower())


def _extract_article_text(html_text):
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    candidates = []
    for selector in ("article", "main", "[role='main']"):
        for node in soup.select(selector):
            text = _norm_for_quote(node.get_text(" ", strip=True))
            if len(text) >= 300:
                candidates.append(text)
    if not candidates:
        body = soup.body or soup
        candidates.append(_norm_for_quote(body.get_text(" ", strip=True)))
    text = max(candidates, key=len) if candidates else ""
    return text[:12000]


def enrich_item_with_source(item):
    """Add best-effort source-page evidence. RSS/newsletter text remains the fallback."""
    out = dict(item)
    title = fix_text_encoding(out.get("title", ""))
    summary = fix_text_encoding(out.get("summary", ""))
    evidence_parts = [f"HEADLINE: {title}"]
    if summary:
        evidence_parts.append(f"RSS/NEWSLETTER TEXT: {summary}")

    url = (out.get("url") or "").strip()
    fetched_text = ""
    resolved_url = url
    fetch_status = "not_attempted"
    if url.startswith(("http://", "https://")):
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 JAM-Morning-Brief/2.0",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=12,
                allow_redirects=True,
            )
            resolved_url = r.url or url
            ctype = (r.headers.get("content-type") or "").lower()
            if r.ok and ("html" in ctype or not ctype):
                fetched_text = _extract_article_text(r.text)
                # Google News article wrappers are often mostly navigation; retain only if substantive.
                if len(fetched_text) >= 350:
                    fetch_status = "fetched"
                    evidence_parts.append(f"SOURCE PAGE TEXT: {fetched_text}")
                else:
                    fetch_status = "thin_page"
            else:
                fetch_status = f"http_{r.status_code}"
        except Exception as exc:
            fetch_status = f"error:{type(exc).__name__}"

    out["resolved_url"] = resolved_url
    out["source_page_text"] = fetched_text
    out["fetch_status"] = fetch_status
    out["evidence"] = "\n".join(evidence_parts)[:14000]
    return out


def enrich_items(items, max_workers=8):
    if not items:
        return []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(enrich_item_with_source, item): idx for idx, item in enumerate(items)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception:
                fallback = dict(items[idx])
                fallback["fetch_status"] = "worker_error"
                fallback["evidence"] = f"HEADLINE: {fallback.get('title','')}\nRSS/NEWSLETTER TEXT: {fallback.get('summary','')}"
                results[idx] = fallback
    return results


def assign_candidate_ids(items, prefix):
    out = []
    for idx, item in enumerate(items, 1):
        x = dict(item)
        x["candidate_id"] = f"{prefix}{idx:03d}"
        out.append(x)
    return out


def selector_story(item):
    return {
        "candidate_id": item.get("candidate_id"),
        "title": fix_text_encoding(item.get("title", ""))[:320],
        "summary": fix_text_encoding(item.get("summary", ""))[:550],
        "source": fix_text_encoding(item.get("source", "")),
        "category_hint": fix_text_encoding(item.get("category_hint", "")),
        "published_at": item.get("published_at", ""),
        "california_employment_hint": is_california_employment(item),
    }


def compact_story(item, idx=None):
    return {
        "candidate_id": item.get("candidate_id") or str(idx or ""),
        "title": fix_text_encoding(item.get("title", ""))[:320],
        "summary": fix_text_encoding(item.get("summary", ""))[:1000],
        "source": fix_text_encoding(item.get("source", "")),
        "url": item.get("url", ""),
        "resolved_url": item.get("resolved_url", ""),
        "category_hint": fix_text_encoding(item.get("category_hint", "")),
        "priority_hint": item.get("priority", 5),
        "published_at": item.get("published_at", ""),
        "origin": item.get("origin", ""),
        "fetch_status": item.get("fetch_status", ""),
        "evidence": fix_text_encoding(item.get("evidence", ""))[:14000],
        "trusted_legal_source": item.get("source") in TRUSTED_LEGAL_SOURCES,
        "california_employment_hint": is_california_employment(item),
    }


def choose_finalists(items, kind, max_finalists):
    """Headline/snippet-only preselection keeps source-page fetching focused and fast."""
    payload = [selector_story(i) for i in items]
    if kind == "general":
        prompt = f"""You are selecting candidates for a daily general-news briefing. Do NOT summarize or rewrite facts.
From the candidates below, return up to {max_finalists} candidate IDs that provide enough strong choices to later fill exactly 3 U.S. national, 3 global, 2 Minnesota, 1-2 Tech/AI, 1-2 Entertainment/Culture, and 1-2 genuinely uplifting Good News stories.
Prioritize consequence, recency, source quality, and section fit. Exclude obvious duplicates and weak filler.
Return JSON only: {{\"candidate_ids\":[\"G001\", ...]}}
CANDIDATES:\n{json.dumps(payload, ensure_ascii=False)}"""
        system = "You are a news assignment editor. Select candidate IDs only; do not invent facts. Return valid JSON only."
    else:
        prompt = f"""You are selecting candidates for a DAILY employment/labor-law briefing for an attorney whose PRIMARY practice is California.
From the candidates below, return up to {max_finalists} candidate IDs worth source-page verification. Prioritize fresh California statutes/bills, appellate cases, CRD/DIR/DLSE/Cal-OSHA, PAGA, wage/hour, FEHA, leave, restrictive covenants, privacy/AI, then major federal EEOC/NLRB/DOL/OSHA and meaningful Minnesota/Eighth Circuit matters. Review specialist newsletter candidates seriously but exclude obvious benefits filler, marketing, generic HR advice, stale background, and non-U.S. material.
Return JSON only: {{\"candidate_ids\":[\"L001\", ...]}}
CANDIDATES:\n{json.dumps(payload, ensure_ascii=False)}"""
        system = "You are a California-first employment-law assignment editor. Select candidate IDs only; do not invent facts. Return valid JSON only."
    try:
        raw = call_openrouter(prompt, system, temperature=0.02)
        data = parse_json_response(raw)
        ids = [str(x) for x in data.get("candidate_ids", [])][:max_finalists]
        by_id = {i.get("candidate_id"): i for i in items}
        selected = [by_id[x] for x in ids if x in by_id]
    except Exception as exc:
        print(f"WARNING: {kind} finalist preselection failed: {exc}")
        selected = []
    if len(selected) < min(12, len(items)):
        seen = {i.get("candidate_id") for i in selected}
        for item in items:
            if item.get("candidate_id") not in seen:
                selected.append(item)
                seen.add(item.get("candidate_id"))
            if len(selected) >= max_finalists:
                break
    return selected[:max_finalists]


def call_openrouter(prompt, system, temperature=0.12):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    model = os.getenv("OPENROUTER_MODEL", "").strip() or "openai/gpt-4.1-mini"
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/jmurthahrlaw-sys/jam-morning-brief",
            "X-Title": "JAM Morning Brief",
        },
        json={
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json_response(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def build_general_prompt(profile, stories):
    return f"""You are the SOURCE-GROUNDED GENERAL-NEWS editor for JAM Morning Brief.
The professional legal section is produced separately.

Relevant editorial rules:
{profile}

VERIFIED CANDIDATE EVIDENCE:
{json.dumps(stories, ensure_ascii=False)}

Your job is not to write a generic explanation of a headline. Every factual statement must be traceable to the EVIDENCE field of the candidate you use.

Return VALID JSON ONLY with exactly:
{{
  "date": "Month D, YYYY",
  "intro": "1-2 sentences describing the edition",
  "national_headlines": [
    {{
      "candidate_id":"G000",
      "headline":"faithful headline; may lightly shorten source title without changing meaning",
      "summary":"2-4 sentences closely paraphrasing the source evidence and containing at least 3 concrete source-supported facts when the evidence allows",
      "why_it_matters":"ONLY an implication/consequence explicitly supported by the source evidence; otherwise empty string",
      "evidence_quotes":["2-3 short exact excerpts copied from candidate evidence that together support the summary"],
      "why_support_quote":"short exact source excerpt supporting why_it_matters, or empty if why_it_matters is empty"
    }}
  ],
  "global_headlines": [same shape],
  "minnesota": [same shape],
  "tech_news": [same shape],
  "entertainment": [same shape],
  "good_news": [same shape]
}}

STRICT COUNTS:
- national_headlines exactly 3
- global_headlines exactly 3
- minnesota exactly 2
- tech_news 1 or 2
- entertainment 1 or 2
- good_news 1 or 2

GROUNDING RULES — NONNEGOTIABLE:
- Use only supplied candidate evidence. Do NOT fill gaps from memory or general knowledge.
- evidence_quotes and why_support_quote are INTERNAL audit fields. Copy them exactly from the selected candidate's EVIDENCE. Keep each excerpt short.
- Do not change a person's current title/status from what the source says. If the evidence says "President," do not rewrite it as "former President," and vice versa.
- Do not characterize a proposal, allegation, claim, investigation, or pending action as a completed fact.
- Attribute claims when the source attributes them (e.g., "Iran said...", "the administration said...").
- Preserve numbers, dates, institutional names, causal statements, and procedural posture exactly enough to avoid changing meaning.
- "Why it matters" is NOT a generic filler field. Use it only when the source itself provides a concrete consequence, stakes, impact, next step, or context. Otherwise return "".
- No unsupported predictions about diplomacy, markets, social cohesion, public confidence, regulation, or other downstream effects.
- A one-sentence restatement of the headline is NOT an acceptable summary.
- Prefer candidates with substantive SOURCE PAGE TEXT. RSS-only candidates may be used only when the RSS/newsletter text itself contains enough concrete detail.
- A normal summary should answer, when supported: who/what happened, the key action or result, and at least one material detail such as timing, scope, numbers, location, procedural posture, or consequence.
- Do not use vague filler such as "sparking debate," "raising concerns," "signaling changes," or "highlighting issues" unless the source evidence specifically supports that characterization.
- If the evidence is too thin to supply at least 2 distinct concrete facts beyond the headline, choose another candidate.

SECTION RULES:
- A story may appear in only one section.
- Top National must be genuinely U.S. national news. U.S. elections, Congress, federal courts/agencies, state redistricting with national electoral significance, and national policy belong here, not Global.
- Top Global must be primarily international/foreign affairs. A U.S. state political or court story may not be placed in Global merely because it is consequential.
- Minnesota must concern Minnesota or a Minnesota-specific development.
- Tech must actually be technology/AI.
- Entertainment must actually be entertainment/culture.
- Good News must be genuinely uplifting. Abuse, crisis, disaster, conflict, layoffs, scandal, warnings, or controversy are not Good News merely because someone responds constructively.
- No What to Watch section.
"""


def build_legal_prompt(profile, stories):
    return f"""You are the SOURCE-GROUNDED EMPLOYMENT & LABOR LAW editor for JAM Morning Brief.
Your reader is an employment attorney whose PRIMARY practice is California. This is a daily professional practice update.

EDITORIAL RULES:
{profile}

VERIFIED LEGAL CANDIDATE EVIDENCE:
{json.dumps(stories, ensure_ascii=False)}

Return VALID JSON ONLY:
{{
  "california_notes": [
    {{
      "candidate_id":"L000",
      "heading":"actual case/development title supported by source",
      "jurisdiction_topic":"e.g. California — Wage & Hour",
      "development":"2-4 sentences closely paraphrasing the actual source and stating the concrete rule/holding/action plus material scope or procedural detail",
      "employer_takeaway":"narrow practical implication explicitly supported by the source; empty if source does not support one",
      "source_language":"one short exact quote of no more than 20 words that captures the operative rule/holding/action, or empty if no useful exact language is available",
      "court":"only if supplied",
      "case":"only if supplied",
      "effective_date":"only if supplied",
      "evidence_quotes":["1-3 short exact excerpts copied from candidate evidence supporting development"],
      "takeaway_support_quote":"short exact excerpt supporting employer_takeaway, or empty if takeaway is empty"
    }}
  ],
  "other_legal_notes": [same shape],
  "specialist_source_review": [
    {{"source":"...","title":"...","decision":"included | duplicate | outside_scope | stale | too_thin","reason":"brief reason"}}
  ],
  "california_candidate_review": [
    {{"title":"...","source":"...","decision":"included | duplicate | outside_scope | stale | too_thin","reason":"brief reason"}}
  ]
}}

FRESHNESS / PRIORITY:
- Daily briefing: prefer genuinely new developments from the last 24-48 hours.
- California first; then major federal; then meaningful Minnesota/Eighth Circuit.
- Target roughly 6-10 total when the day warrants it; never pad to hit a number.
- One legal development gets one note even if several sources cover it. Collapse multiple reports of the same case, rule, agency action, bill, or enforcement development into ONE note.
- Prefer the candidate with the strongest evidence and highest authority for a duplicated development: primary court/agency/statute first, then specialist legal source, then established legal publication, then general press.

SOURCE-GROUNDING RULES — NONNEGOTIABLE:
- Every factual statement in development must be entailed by the selected candidate's EVIDENCE.
- evidence_quotes and takeaway_support_quote are INTERNAL audit fields copied exactly from candidate EVIDENCE. Keep excerpts short.
- Do not infer legal duties from a headline or generic article title.
- Distinguish enacted law, signed bill, pending bill, proposed rule, final rule, agency guidance, enforcement action, court holding, allegation, settlement, and commentary.
- For cases, state court and procedural posture exactly as supplied; never call a district-court ruling precedent and never describe a circuit decision as binding nationwide.
- Do not invent case names, holdings, dates, penalties, deadlines, remedies, coverage thresholds, or effective dates.
- employer_takeaway must be a narrow practice implication the source supports. If the source does not actually say or establish the claimed employer obligation, leave it blank.
- Advocacy for a bill is not law. A bill awaiting signature is not an enacted employer obligation.
- A headline restatement is not a legal update. The development should normally identify at least THREE concrete items supported by the evidence: (1) what authority acted, (2) what it actually held/issued/proposed/changed, and (3) a material detail such as scope, standard, procedure, effective date, remedy, vote, covered conduct, or next step.
- For a case, identify the actual holding or procedural disposition; saying an article "discusses" or "analyzes" a case is insufficient.
- For legislation/regulation, identify the bill/rule and what the operative provision would do. Do not say merely that it "creates new requirements."
- For agency letters/guidance, state the agency's actual conclusion on the issue, not merely that guidance was issued.
- source_language must be copied exactly from EVIDENCE, no more than 20 words, and should capture operative language rather than promotional prose.
- If source evidence is thin or inaccessible, OMIT the note instead of extrapolating.
- When a primary agency/court source and commentary cover the same event, prefer the primary source when it contains enough detail.

EXCLUDE:
- generic worker-rights awareness pieces without a new legal development
- annual reports without a discrete new rule/action
- webinars/marketing/evergreen explainers
- ordinary allegations with no material ruling or agency action
- employee-benefits filler unless unusually consequential
- state-specific developments outside California/Minnesota without national significance
"""


def clean_general_story(story):
    for key in ("headline", "summary", "why_it_matters", "source", "why_support_quote"):
        story[key] = fix_text_encoding(story.get(key, ""))
    return story


def clean_legal_note(note):
    for key in (
        "heading", "jurisdiction_topic", "development", "employer_takeaway",
        "court", "case", "date", "effective_date", "source", "takeaway_support_quote", "source_language",
    ):
        note[key] = fix_text_encoding(note.get(key, ""))
    return note


def _note_date_is_stale(note, lookback_hours):
    value = (note.get("date") or "").strip()
    if not value:
        return False
    try:
        dt = dateparser.parse(value)
        if not dt:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    except Exception:
        return False


def _legal_topic_signature(note):
    text = " ".join([
        note.get("heading", ""), note.get("jurisdiction_topic", ""),
        note.get("development", ""),
    ]).lower()
    is_ca = "california" in text or "ninth circuit" in text
    if "temporary protected status" in text or re.search(r"\btps\b", text):
        return "ca:tps" if is_ca else "tps"
    if is_ca and ("workplace ai" in text or "artificial intelligence" in text or "automated decision" in text or re.search(r"\bai\b", text)):
        return "ca:workplace-ai"
    if is_ca and "labor commissioner" in text:
        return "ca:labor-commissioner"
    if is_ca and "holiday" in text and ("dir" in text or "holiday pay" in text):
        return "ca:holiday-pay"
    if "eeo-1" in text:
        return "federal:eeo-1"
    if "electronic delivery" in text or "e-delivery" in text:
        return "federal:e-delivery"
    if ("nlrb" in text or "national labor relations board" in text) and (
        "withdrawal of recognition" in text or "union ouster" in text or
        ("withdrawal" in text and "recognition" in text)
    ):
        return "federal:nlrb-withdrawal-recognition"
    if "starbucks" in text and ("fifth circuit" in text or "5th circ" in text) and "nlrb" in text:
        return "federal:fifth-circuit-starbucks-nlrb"
    if is_ca and "bills" in text and ("newsom" in text or "key measures" in text):
        return "ca:legislative-roundup"
    return ""


def postprocess_legal_notes(notes, lookback_hours, max_notes):
    out = []
    seen_urls = set()
    seen_topics = set()
    for note in notes:
        if _note_date_is_stale(note, lookback_hours):
            continue
        url = canonical_url(note.get("url", ""))
        if url and url in seen_urls:
            continue
        topic = _legal_topic_signature(note)
        if topic and topic in seen_topics:
            continue
        heading = normalize_title(note.get("heading", ""))
        duplicate = False
        for existing in out:
            old = normalize_title(existing.get("heading", ""))
            if heading and old and token_set_ratio(heading, old) >= 86:
                duplicate = True
                break
        if duplicate:
            continue
        out.append(note)
        if url:
            seen_urls.add(url)
        if topic:
            seen_topics.add(topic)
        if len(out) >= max_notes:
            break
    return out


def story_key(story):
    url = canonical_url(story.get("url", ""))
    if url:
        return url
    return normalize_title(story.get("headline", ""))


def bind_general_to_candidates(d, candidates):
    by_id = {c.get("candidate_id"): c for c in candidates}
    sections = ("national_headlines", "global_headlines", "minnesota", "tech_news", "entertainment", "good_news")
    for section in sections:
        for story in d.get(section, []):
            cid = str(story.get("candidate_id", ""))
            c = by_id.get(cid)
            if not c:
                continue
            story["source"] = c.get("source", "")
            story["url"] = c.get("url", "")
    return d


def bind_legal_to_candidates(notes, candidates):
    by_id = {c.get("candidate_id"): c for c in candidates}
    for note in notes:
        cid = str(note.get("candidate_id", ""))
        c = by_id.get(cid)
        if not c:
            continue
        note["source"] = c.get("source", "")
        note["url"] = c.get("url", "")
        # The displayed date is the source/article date, not a guessed legal effective date.
        pub = (c.get("published_at") or "")[:10]
        note["date"] = pub
    return notes


def validate_grounded_general(d, candidates):
    errors = []
    by_id = {c.get("candidate_id"): c for c in candidates}
    for section in ("national_headlines", "global_headlines", "minnesota", "tech_news", "entertainment", "good_news"):
        for story in d.get(section, []):
            cid = str(story.get("candidate_id", ""))
            c = by_id.get(cid)
            if not c:
                errors.append(f"{section}: unknown candidate_id {cid}")
                continue
            evidence = c.get("evidence", "")
            quotes = story.get("evidence_quotes", []) or []
            if not quotes:
                errors.append(f"{section}: {cid} has no evidence_quotes")
            for q in quotes[:3]:
                if not _quote_supported(q, evidence):
                    errors.append(f"{section}: {cid} evidence quote not found in source evidence")
                    break
            why = (story.get("why_it_matters") or "").strip()
            why_q = (story.get("why_support_quote") or "").strip()
            if why and not _quote_supported(why_q, evidence):
                errors.append(f"{section}: {cid} why_it_matters lacks exact source support")
            summary = _norm_for_quote(story.get("summary", ""))
            summary_words = len(summary.split())
            if summary_words < 28:
                errors.append(f"{section}: {cid} summary is too thin ({summary_words} words); require a substantive 2-4 sentence account")
            if len(quotes) < 2:
                errors.append(f"{section}: {cid} needs at least 2 evidence quotes supporting distinct facts")
            # Do not accept headline-only evidence for a final story.
            non_headline = re.sub(r"^HEADLINE:\s*.*?(?:\n|$)", "", evidence, count=1, flags=re.I)
            if len(_norm_for_quote(non_headline)) < 120:
                errors.append(f"{section}: {cid} source evidence is too thin for reliable briefing")
    return errors


def validate_grounded_legal(legal_digest, candidates):
    errors = []
    by_id = {c.get("candidate_id"): c for c in candidates}
    for section in ("california_notes", "other_legal_notes"):
        for note in legal_digest.get(section, []):
            cid = str(note.get("candidate_id", ""))
            c = by_id.get(cid)
            if not c:
                errors.append(f"{section}: unknown candidate_id {cid}")
                continue
            evidence = c.get("evidence", "")
            quotes = note.get("evidence_quotes", []) or []
            if not quotes:
                errors.append(f"{section}: {cid} has no evidence_quotes")
            for q in quotes[:4]:
                if not _quote_supported(q, evidence):
                    errors.append(f"{section}: {cid} evidence quote not found in source evidence")
                    break
            takeaway = (note.get("employer_takeaway") or "").strip()
            take_q = (note.get("takeaway_support_quote") or "").strip()
            if takeaway and not _quote_supported(take_q, evidence):
                errors.append(f"{section}: {cid} employer_takeaway lacks exact source support")
            development = _norm_for_quote(note.get("development", ""))
            dev_words = len(development.split())
            if dev_words < 38:
                errors.append(f"{section}: {cid} legal development is too thin ({dev_words} words)")
            if len(quotes) < 2:
                errors.append(f"{section}: {cid} needs at least 2 evidence quotes supporting the legal development")
            source_language = _norm_for_quote(note.get("source_language", ""))
            if source_language:
                if len(source_language.split()) > 20:
                    errors.append(f"{section}: {cid} source_language exceeds 20 words")
                elif not _quote_supported(source_language, evidence):
                    errors.append(f"{section}: {cid} source_language not found in source evidence")
            non_headline = re.sub(r"^HEADLINE:\s*.*?(?:\n|$)", "", evidence, count=1, flags=re.I)
            if len(_norm_for_quote(non_headline)) < 160:
                errors.append(f"{section}: {cid} source evidence is too thin for an attorney-facing legal note")
    return errors


def validate_general(d):
    errors = []
    expected = {
        "national_headlines": 3,
        "global_headlines": 3,
        "minnesota": 2,
    }
    for key, count in expected.items():
        if len(d.get(key, [])) != count:
            errors.append(f"{key} must have {count}, found {len(d.get(key, []))}")
    for key in ("tech_news", "entertainment", "good_news"):
        if len(d.get(key, [])) not in (1, 2):
            errors.append(f"{key} must have 1 or 2, found {len(d.get(key, []))}")

    all_stories = []
    seen_exact = {}
    for section in ("national_headlines", "global_headlines", "minnesota", "tech_news", "entertainment", "good_news"):
        for s in d.get(section, []):
            k = story_key(s)
            if k and k in seen_exact:
                errors.append(f"duplicate story across {seen_exact[k]} and {section}: {s.get('headline','')}")
            if k:
                seen_exact[k] = section
            all_stories.append((section, s))

    # Catch obvious section-fit mistakes.
    for s in d.get("global_headlines", []):
        txt = " ".join([s.get("headline",""), s.get("summary","")]).lower()
        if any(term in txt for term in ("missouri", "congressional district", "u.s. senate", "united states senate", "white house", "congress ")) and not any(term in txt for term in ("foreign", "international", "ukraine", "israel", "china", "russia", "iran", "united nations", "u.n.")):
            errors.append(f"global_headlines probable U.S.-domestic misclassification: {s.get('headline','')}")
    for s in d.get("good_news", []):
        txt = " ".join([s.get("headline",""), s.get("summary","")]).lower()
        if any(term in txt for term in ("abuse", "crisis", "killed", "death", "dead", "lawsuit", "scandal", "warning", "layoff", "war ", "attack")):
            errors.append(f"good_news probable negative-story misclassification: {s.get('headline','')}")

    # Catch same event chosen from different outlets.
    for i in range(len(all_stories)):
        sec_a, a = all_stories[i]
        title_a = normalize_title(a.get("headline", ""))
        for j in range(i + 1, len(all_stories)):
            sec_b, b = all_stories[j]
            if sec_a == sec_b:
                continue
            title_b = normalize_title(b.get("headline", ""))
            if title_a and title_b and ratio(title_a, title_b) >= 72:
                errors.append(
                    f"probable same-event duplicate across {sec_a} and {sec_b}: "
                    f"{a.get('headline','')} / {b.get('headline','')}"
                )
    return errors


def repair_general_if_needed(profile, candidates, digest, errors):
    if not errors:
        return digest
    prompt = build_general_prompt(profile, candidates)
    prompt += "\n\nYOUR PRIOR OUTPUT VIOLATED THESE RULES:\n- " + "\n- ".join(errors)
    prompt += "\nCorrect all violations. Return the complete corrected JSON only."
    raw = call_openrouter(
        prompt,
        "You are a source-grounded senior general-news editor. Correct only using supplied evidence. Obey counts and deduplication. Return valid JSON only.",
        temperature=0.08,
    )
    return parse_json_response(raw)



def validate_legal(legal_digest, legal_candidates):
    errors = []
    ca_notes = legal_digest.get("california_notes", [])
    other_notes = legal_digest.get("other_legal_notes", [])
    specialist_candidates = [
        c for c in legal_candidates if c.get("source") in TRUSTED_LEGAL_SOURCES
    ]
    california_candidates = [
        c for c in legal_candidates if c.get("california_employment_hint")
    ]

    specialist_review = legal_digest.get("specialist_source_review", [])
    ca_review = legal_digest.get("california_candidate_review", [])

    if len(ca_notes) > 6:
        errors.append(f"california_notes hard maximum is 6, found {len(ca_notes)}")
    if len(other_notes) > 4:
        errors.append(f"other_legal_notes hard maximum is 4, found {len(other_notes)}")
    if len(ca_notes) + len(other_notes) > 10:
        errors.append(f"total legal notes hard maximum is 10, found {len(ca_notes) + len(other_notes)}")

    if specialist_candidates and not (ca_notes or other_notes):
        errors.append(
            f"all legal notes are empty despite {len(specialist_candidates)} specialist-source candidates"
        )

    if california_candidates and not ca_notes:
        errors.append(
            f"california_notes is empty despite {len(california_candidates)} California employment candidates"
        )

    reviewed_specialist_titles = {
        normalize_title(r.get("title", "")) for r in specialist_review if r.get("title")
    }
    missing_specialist = [
        c.get("title", "")
        for c in specialist_candidates
        if normalize_title(c.get("title", "")) not in reviewed_specialist_titles
    ]
    if missing_specialist:
        errors.append(
            "specialist_source_review omitted candidates: "
            + " | ".join(missing_specialist[:12])
        )

    reviewed_ca_titles = {
        normalize_title(r.get("title", "")) for r in ca_review if r.get("title")
    }
    missing_ca = [
        c.get("title", "")
        for c in california_candidates[:30]
        if normalize_title(c.get("title", "")) not in reviewed_ca_titles
    ]
    if missing_ca:
        errors.append(
            "california_candidate_review omitted candidates: "
            + " | ".join(missing_ca[:12])
        )

    return errors


def repair_legal_if_needed(profile, candidates, legal_digest, errors):
    if not errors:
        return legal_digest

    prompt = build_legal_prompt(profile, candidates)
    prompt += "\n\nYOUR PRIOR LEGAL OUTPUT FAILED VALIDATION:\n- " + "\n- ".join(errors)
    prompt += """
Re-review California employment candidates FIRST.
The user practices primarily in California.
If meaningful California employment material exists, include it before routine federal items.
Re-review EVERY Lexology and ELINfonet item.
Return the COMPLETE corrected JSON with california_notes, other_legal_notes,
specialist_source_review, and california_candidate_review.
"""
    raw = call_openrouter(
        prompt,
        "You are a source-grounded California-focused employment-and-labor-law editor. Use only supplied evidence and exact audit quotes. Return valid JSON only.",
        temperature=0.02,
    )
    return parse_json_response(raw)


def legal_section_html(d):
    ca = d.get("california_legal_notes", [])
    other = d.get("other_legal_notes", [])
    if not ca and not other:
        return ""

    parts = [
        '<div style="font-size:13px;letter-spacing:.09em;text-transform:uppercase;font-weight:800;color:#5b6770;margin-top:32px;margin-bottom:2px">Employment &amp; Labor Law Notes</div>'
    ]
    if ca:
        parts.append(
            '<div style="font-size:14px;font-weight:800;color:#7a5d2c;margin-top:16px;margin-bottom:0">CALIFORNIA EMPLOYMENT — PRIMARY PRACTICE</div>'
        )
        parts.extend(render_legal_note(n) for n in ca)

    if other:
        parts.append(
            '<div style="font-size:14px;font-weight:800;color:#5b6770;margin-top:20px;margin-bottom:0">FEDERAL / MINNESOTA / OTHER EMPLOYMENT</div>'
        )
        parts.extend(render_legal_note(n) for n in other)

    return "".join(parts)


def render_general_story(s):
    headline = html.escape(s.get("headline", ""))
    summary = html.escape(s.get("summary", ""))
    why = html.escape(s.get("why_it_matters", ""))
    source = html.escape(s.get("source", ""))
    url = html.escape(s.get("url", ""), quote=True)
    why_html = f'<div style="margin-top:8px"><strong>Why it matters:</strong> {why}</div>' if why else ""
    src = f'<a href="{url}" style="color:#315d74;text-decoration:none">{source or "Read source"}</a>' if url else source
    return f"""
    <div style="padding:16px 0;border-bottom:1px solid #e7e4de">
      <div style="font-size:19px;line-height:1.25;font-weight:700;color:#17232b">{headline}</div>
      <div style="font-size:15px;line-height:1.55;margin-top:7px;color:#303b42">{summary}</div>
      {why_html}
      <div style="font-size:13px;margin-top:9px;color:#6b747a">{src}</div>
    </div>
    """


def render_legal_note(n):
    heading = html.escape(n.get("heading", ""))
    jurisdiction = html.escape(n.get("jurisdiction_topic", ""))
    development = html.escape(n.get("development", ""))
    takeaway = html.escape(n.get("employer_takeaway", ""))
    source_language = html.escape(n.get("source_language", ""))
    source = html.escape(n.get("source", ""))
    url = html.escape(n.get("url", ""), quote=True)
    meta = []
    for label, key in (("Court","court"),("Case","case"),("Date","date"),("Effective date","effective_date")):
        if n.get(key):
            meta.append(f"<span style='margin-right:14px'><strong>{label}:</strong> {html.escape(n[key])}</span>")
    src = f'<a href="{url}" style="color:#315d74;text-decoration:none">{source or "Read source"}</a>' if url else source
    return f"""
    <div style="padding:17px 0;border-bottom:1px solid #e7e4de">
      <div style="font-size:13px;font-weight:800;color:#7a5d2c;text-transform:uppercase;letter-spacing:.04em">{jurisdiction}</div>
      <div style="font-size:18px;line-height:1.3;font-weight:700;color:#17232b;margin-top:4px">{heading}</div>
      <div style="font-size:15px;line-height:1.55;margin-top:8px;color:#303b42"><strong>Development:</strong> {development}</div>
      {f'<div style="font-size:15px;line-height:1.55;margin-top:7px;color:#303b42"><strong>Employer takeaway:</strong> {takeaway}</div>' if takeaway else ''}
      {f'<div style="font-size:14px;line-height:1.5;margin-top:7px;color:#4c5960"><strong>Source language:</strong> “{source_language}”</div>' if source_language else ''}
      <div style="font-size:12px;line-height:1.5;margin-top:8px;color:#69737a">{' '.join(meta)}</div>
      <div style="font-size:13px;margin-top:8px;color:#6b747a">{src}</div>
    </div>
    """


def section_html(title, stories, renderer=render_general_story):
    if not stories:
        return ""
    return f"""
    <div style="font-size:13px;letter-spacing:.09em;text-transform:uppercase;font-weight:800;color:#5b6770;margin-top:32px;margin-bottom:2px">{html.escape(title)}</div>
    {''.join(renderer(s) for s in stories)}
    """


def render_html(d):
    body = ""
    body += section_html("Top National", d.get("national_headlines", []))
    body += section_html("Top Global", d.get("global_headlines", []))
    body += section_html("Minnesota", d.get("minnesota", []))
    body += legal_section_html(d)
    body += section_html("Tech & AI", d.get("tech_news", []))
    body += section_html("Entertainment & Culture", d.get("entertainment", []))
    body += section_html("Good News", d.get("good_news", []))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f3f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#26333a">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f1ec;padding:24px 10px"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background:#ffffff;border-radius:14px;overflow:hidden">
<tr><td style="padding:34px 36px 28px 36px">
  <div style="font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#65757f;font-weight:800">JAM Morning Brief</div>
  <div style="font-family:Georgia,'Times New Roman',serif;font-size:34px;line-height:1.1;margin-top:8px;color:#17232b">{html.escape(d.get('date',''))}</div>
  <div style="font-size:16px;line-height:1.55;margin-top:12px;color:#4c5960">{html.escape(d.get('intro',''))}</div>
  {body}
  <div style="font-size:11px;color:#8a9297;margin-top:30px">Personal briefing generated from selected sources. Follow source links for full reporting and primary legal materials.</div>
</td></tr></table>
</td></tr></table>
</body></html>"""


def render_text(d):
    lines = ["JAM MORNING BRIEF", d.get("date", ""), "", d.get("intro", "")]

    def add_general(title, stories):
        if not stories:
            return
        lines.extend(["", title.upper()])
        for s in stories:
            lines.extend(["", s.get("headline", ""), s.get("summary", "")])
            if s.get("why_it_matters"):
                lines.append(f"Why it matters: {s['why_it_matters']}")
            if s.get("source"):
                lines.append(f"Source: {s['source']} {s.get('url','')}")

    add_general("Top National", d.get("national_headlines", []))
    add_general("Top Global", d.get("global_headlines", []))
    add_general("Minnesota", d.get("minnesota", []))

    def add_legal_notes(title, notes):
        if not notes:
            return
        lines.extend(["", title.upper()])
        for n in notes:
            lines.extend(["", n.get("heading", "")])
            if n.get("jurisdiction_topic"):
                lines.append(n["jurisdiction_topic"])
            lines.append(f"Development: {n.get('development','')}")
            if n.get("employer_takeaway"):
                lines.append(f"Employer takeaway: {n['employer_takeaway']}")
            for label, key in (("Court","court"),("Case","case"),("Date","date"),("Effective date","effective_date")):
                if n.get(key):
                    lines.append(f"{label}: {n[key]}")
            if n.get("source"):
                lines.append(f"Source: {n['source']} {n.get('url','')}")

    if d.get("california_legal_notes") or d.get("other_legal_notes"):
        lines.extend(["", "EMPLOYMENT & LABOR LAW NOTES"])
        add_legal_notes("California Employment — Primary Practice", d.get("california_legal_notes", []))
        add_legal_notes("Federal / Minnesota / Other Employment", d.get("other_legal_notes", []))

    add_general("Tech & AI", d.get("tech_news", []))
    add_general("Entertainment & Culture", d.get("entertainment", []))
    add_general("Good News", d.get("good_news", []))
    return "\n".join(lines).strip() + "\n"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    items = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    items = [normalize_item(i) for i in items]

    general_lookback = int(os.getenv("LOOKBACK_HOURS", "30"))
    legal_lookback = int(os.getenv("LEGAL_LOOKBACK_HOURS", "48"))

    items = dedupe_near(dedupe_exact(items), threshold=87)
    general_recent = [i for i in items if within_lookback(i, general_lookback)]
    legal_history = load_legal_history()
    legal_recent_before_history = [i for i in items if within_lookback(i, legal_lookback)]
    legal_recent = [i for i in legal_recent_before_history if not previously_used_legal(i, legal_history)]

    general_max = int(os.getenv("GENERAL_MAX_STORIES_FOR_AI", "150"))
    legal_max = int(os.getenv("LEGAL_MAX_STORIES_FOR_AI", "100"))

    general_items = assign_candidate_ids(select_general_candidates(general_recent, general_max), "G")
    legal_items = assign_candidate_ids(select_legal_candidates(legal_recent, legal_max), "L")

    if not general_items:
        raise RuntimeError("No recent general-news stories found. Run scraper.py first.")

    profile = PROFILE_FILE.read_text(encoding="utf-8")

    # Two-stage pipeline: select finalists first, then fetch source pages and write only from evidence.
    general_finalists = choose_finalists(general_items, "general", int(os.getenv("GENERAL_FINALISTS", "32")))
    legal_finalists = choose_finalists(legal_items, "legal", int(os.getenv("LEGAL_FINALISTS", "36"))) if legal_items else []
    general_enriched = enrich_items(general_finalists)
    legal_enriched = enrich_items(legal_finalists)
    general_compact = [compact_story(i) for i in general_enriched]
    legal_compact = [compact_story(i) for i in legal_enriched]

    # Diagnostics are intentionally saved in the artifact so source-selection problems are visible.
    source_counts_legal = Counter(i.get("source", "") for i in legal_recent)
    california_recent = [i for i in legal_recent if is_california_employment(i)]
    diagnostics = {
        "recent_total_items_general_lookback": len(general_recent),
        "recent_total_items_legal_lookback": len(legal_recent_before_history),
        "legal_items_after_history_filter": len(legal_recent),
        "previously_used_legal_filtered": len(legal_recent_before_history) - len(legal_recent),
        "general_lookback_hours": general_lookback,
        "legal_lookback_hours": legal_lookback,
        "general_candidate_count": len(general_items),
        "legal_candidate_count": len(legal_items),
        "general_finalist_count": len(general_compact),
        "legal_finalist_count": len(legal_compact),
        "general_source_pages_fetched": sum(1 for c in general_compact if c.get("fetch_status") == "fetched"),
        "legal_source_pages_fetched": sum(1 for c in legal_compact if c.get("fetch_status") == "fetched"),
        "general_finalists_with_substantive_evidence": sum(1 for c in general_compact if len(_norm_for_quote(re.sub(r"^HEADLINE:\\s*.*?(?:\\n|$)", "", c.get("evidence",""), count=1, flags=re.I))) >= 120),
        "legal_finalists_with_substantive_evidence": sum(1 for c in legal_compact if len(_norm_for_quote(re.sub(r"^HEADLINE:\\s*.*?(?:\\n|$)", "", c.get("evidence",""), count=1, flags=re.I))) >= 160),
        "california_employment_recent_count": len(california_recent),
        "california_employment_candidate_count": sum(1 for i in legal_items if is_california_employment(i)),
        "lexology_recent_items": source_counts_legal.get("Lexology Daily Newsfeed", 0),
        "elinfonet_recent_items": source_counts_legal.get("ELINfonet Daily Employment Law Update", 0),
        "legal_candidate_sources": dict(Counter(i.get("source", "") for i in legal_items)),
        "legal_candidate_categories": dict(Counter(i.get("category_hint", "") for i in legal_items)),
        "general_candidate_categories": dict(Counter(i.get("category_hint", "") for i in general_items)),
    }
    (OUTPUT / "source_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("Source diagnostics:", json.dumps(diagnostics, ensure_ascii=False))

    # GENERAL NEWS PIPELINE
    general_raw = call_openrouter(
        build_general_prompt(profile, general_compact),
        "You are a precise senior general-news editor. Do not apply an employment-law lens to ordinary news. Return valid JSON only.",
        temperature=0.10,
    )
    general_digest = bind_general_to_candidates(parse_json_response(general_raw), general_compact)
    general_digest = {
        **general_digest,
        "national_headlines": [clean_general_story(s) for s in general_digest.get("national_headlines", [])],
        "global_headlines": [clean_general_story(s) for s in general_digest.get("global_headlines", [])],
        "minnesota": [clean_general_story(s) for s in general_digest.get("minnesota", [])],
        "tech_news": [clean_general_story(s) for s in general_digest.get("tech_news", [])],
        "entertainment": [clean_general_story(s) for s in general_digest.get("entertainment", [])],
        "good_news": [clean_general_story(s) for s in general_digest.get("good_news", [])],
    }
    general_errors = validate_general(general_digest) + validate_grounded_general(general_digest, general_compact)
    if general_errors:
        print("General editor validation errors; requesting repair:", general_errors)
        general_digest = bind_general_to_candidates(
            repair_general_if_needed(profile, general_compact, general_digest, general_errors),
            general_compact,
        )
        general_digest = {
            **general_digest,
            "national_headlines": [clean_general_story(s) for s in general_digest.get("national_headlines", [])],
            "global_headlines": [clean_general_story(s) for s in general_digest.get("global_headlines", [])],
            "minnesota": [clean_general_story(s) for s in general_digest.get("minnesota", [])],
            "tech_news": [clean_general_story(s) for s in general_digest.get("tech_news", [])],
            "entertainment": [clean_general_story(s) for s in general_digest.get("entertainment", [])],
            "good_news": [clean_general_story(s) for s in general_digest.get("good_news", [])],
        }
        remaining_general_grounding = validate_grounded_general(general_digest, general_compact)
        if remaining_general_grounding:
            print("WARNING: general grounding still has issues:", remaining_general_grounding)

    # LEGAL PIPELINE — California-first professional review
    california_legal_notes = []
    other_legal_notes = []
    specialist_review = []
    california_candidate_review = []
    legal_validation_errors = []

    if legal_compact:
        legal_raw = call_openrouter(
            build_legal_prompt(profile, legal_compact),
            "You are a senior California-focused employment-and-labor-law briefing editor. California practice updates receive first priority. Return valid JSON only.",
            temperature=0.03,
        )
        legal_digest = parse_json_response(legal_raw)

        legal_validation_errors = validate_legal(legal_digest, legal_compact) + validate_grounded_legal(legal_digest, legal_compact)
        if legal_validation_errors:
            print("Legal editor validation errors; requesting repair:", legal_validation_errors)
            legal_digest = repair_legal_if_needed(
                profile, legal_compact, legal_digest, legal_validation_errors
            )
            legal_validation_errors = validate_legal(legal_digest, legal_compact) + validate_grounded_legal(legal_digest, legal_compact)

        california_legal_notes = [
            clean_legal_note(n) for n in legal_digest.get("california_notes", [])
        ]
        other_legal_notes = [
            clean_legal_note(n) for n in legal_digest.get("other_legal_notes", [])
        ]
        california_legal_notes = bind_legal_to_candidates(california_legal_notes, legal_compact)
        other_legal_notes = bind_legal_to_candidates(other_legal_notes, legal_compact)
        # Deterministic safety net: stale-dated notes, obvious topical duplicates,
        # and over-long outputs are removed even if the model over-selects.
        california_legal_notes = postprocess_legal_notes(
            california_legal_notes, legal_lookback, 6
        )
        other_legal_notes = postprocess_legal_notes(
            other_legal_notes, legal_lookback, 4
        )
        specialist_review = legal_digest.get("specialist_source_review", [])
        california_candidate_review = legal_digest.get("california_candidate_review", [])

        if legal_validation_errors:
            print("WARNING: legal validation still has issues:", legal_validation_errors)
    else:
        print("WARNING: No legal candidates available for this run.")

    legal_notes = california_legal_notes + other_legal_notes

    legal_diagnostics = {
        "legal_lookback_hours": legal_lookback,
        "legal_candidate_count": len(legal_compact),
        "california_candidate_count": sum(
            1 for c in legal_compact if c.get("california_employment_hint")
        ),
        "california_candidates": [
            {"source": c.get("source"), "title": c.get("title"), "url": c.get("url")}
            for c in legal_compact if c.get("california_employment_hint")
        ][:40],
        "specialist_candidate_count": sum(
            1 for c in legal_compact if c.get("source") in TRUSTED_LEGAL_SOURCES
        ),
        "specialist_candidates": [
            {"source": c.get("source"), "title": c.get("title"), "url": c.get("url")}
            for c in legal_compact if c.get("source") in TRUSTED_LEGAL_SOURCES
        ],
        "california_note_count": len(california_legal_notes),
        "other_legal_note_count": len(other_legal_notes),
        "specialist_source_review": specialist_review,
        "california_candidate_review": california_candidate_review,
        "validation_errors": legal_validation_errors,
    }
    (OUTPUT / "legal_diagnostics.json").write_text(
        json.dumps(legal_diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("Legal diagnostics:", json.dumps(legal_diagnostics, ensure_ascii=False))

    grounding_diagnostics = {
        "general_grounding_errors": validate_grounded_general(general_digest, general_compact),
        "legal_grounding_errors": validate_grounded_legal(legal_digest, legal_compact) if legal_compact else [],
        "general_finalists": [
            {"candidate_id": c.get("candidate_id"), "source": c.get("source"), "title": c.get("title"), "fetch_status": c.get("fetch_status")}
            for c in general_compact
        ],
        "legal_finalists": [
            {"candidate_id": c.get("candidate_id"), "source": c.get("source"), "title": c.get("title"), "fetch_status": c.get("fetch_status")}
            for c in legal_compact
        ],
    }
    (OUTPUT / "grounding_diagnostics.json").write_text(
        json.dumps(grounding_diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    digest = {
        "date": fix_text_encoding(general_digest.get("date", datetime.now().strftime("%B %d, %Y").replace(" 0", " "))),
        "intro": fix_text_encoding(general_digest.get("intro", "")),
        "national_headlines": general_digest.get("national_headlines", []),
        "global_headlines": general_digest.get("global_headlines", []),
        "minnesota": general_digest.get("minnesota", []),
        "california_legal_notes": california_legal_notes,
        "other_legal_notes": other_legal_notes,
        "legal_notes": legal_notes,
        "tech_news": general_digest.get("tech_news", []),
        "entertainment": general_digest.get("entertainment", []),
        "good_news": general_digest.get("good_news", []),
    }

    today = datetime.now().strftime("%Y-%m-%d")
    (OUTPUT / "latest_digest.json").write_text(
        json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUTPUT / f"brief-{today}.html").write_text(render_html(digest), encoding="utf-8")
    (OUTPUT / f"brief-{today}.txt").write_text(render_text(digest), encoding="utf-8")

    save_legal_history(legal_notes, legal_history)

    print(
        "Created digest:",
        f"{len(digest['national_headlines'])} national,",
        f"{len(digest['global_headlines'])} global,",
        f"{len(digest['minnesota'])} Minnesota,",
        f"{len(digest['california_legal_notes'])} California legal, " + f"{len(digest['other_legal_notes'])} other legal,",
        f"{len(digest['tech_news'])} tech,",
        f"{len(digest['entertainment'])} entertainment,",
        f"{len(digest['good_news'])} good news.",
    )


if __name__ == "__main__":
    main()
