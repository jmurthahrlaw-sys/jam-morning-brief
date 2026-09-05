import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
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


def compact_story(item, idx):
    return {
        "id": idx,
        "title": fix_text_encoding(item.get("title", ""))[:320],
        "summary": fix_text_encoding(item.get("summary", ""))[:1000],
        "source": fix_text_encoding(item.get("source", "")),
        "url": item.get("url", ""),
        "category_hint": fix_text_encoding(item.get("category_hint", "")),
        "priority_hint": item.get("priority", 5),
        "published_at": item.get("published_at", ""),
        "origin": item.get("origin", ""),
        "trusted_legal_source": item.get("source") in TRUSTED_LEGAL_SOURCES,
        "california_employment_hint": is_california_employment(item),
    }


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
    return f"""You are the GENERAL-NEWS editor for JAM Morning Brief.
The professional legal section is being produced by a different editor. Do not perform employment-law analysis here.

Relevant editorial rules:
{profile}

GENERAL-NEWS CANDIDATES:
{json.dumps(stories, ensure_ascii=False)}

Select the best available stories and return VALID JSON ONLY with exactly:
{{
  "date": "Month D, YYYY",
  "intro": "1-2 sentences accurately describing the overall edition without claiming coverage that is absent",
  "national_headlines": [
    {{"headline":"...","summary":"2-4 sentences","why_it_matters":"ordinary public significance, not an artificial employer angle","source":"...","url":"..."}}
  ],
  "global_headlines": [same shape],
  "minnesota": [same shape],
  "tech_news": [same shape],
  "entertainment": [same shape],
  "good_news": [same shape]
}}

STRICT COUNTS:
- national_headlines: exactly 3 U.S. stories
- global_headlines: exactly 3 international stories
- minnesota: exactly 2 Minnesota stories
- tech_news: 1 or 2
- entertainment: 1 or 2
- good_news: 1 or 2

HARD EDITORIAL RULES:
- A story may appear in only ONE section, including when different outlets use different headlines for the same underlying event.
- Never reuse a National, Global, Minnesota, Tech, or Entertainment story as Good News.
- Exclude roundup/newsletter items that combine multiple unrelated stories into one candidate; never merge unrelated events into one headline.
- Top National must be genuinely nationally consequential. Do not use a primarily local criminal case merely because it is high-profile.
- Do not put foreign-company restructuring into National merely because jobs are involved.
- Do not add employer/compliance implications to ordinary news.
- Tech should actually be technology/AI news.
- Entertainment may be fun but should not be trivial clickbait.
- Good News must be genuinely uplifting, not merely technically interesting or mildly positive.
- No What to Watch section.
- Use only facts supported by the supplied candidates.
"""


def build_legal_prompt(profile, stories):
    return f"""You are the EMPLOYMENT & LABOR LAW editor for JAM Morning Brief.
Your reader practices PRIMARILY CALIFORNIA employment law. This is a DAILY professional practice update, not a general court-news section.

EDITORIAL RULES:
{profile}

LEGAL CANDIDATES:
{json.dumps(stories, ensure_ascii=False)}

FRESHNESS IS CRITICAL:
- This brief runs every day. Prefer genuinely new developments from the last 24–48 hours.
- The candidate published_at field is the best available article/development date. Treat it as a freshness signal.
- A newsletter issue date is NOT proof that the underlying article is new. If a title/summary reveals an older development, mark it stale.
- Do not recycle background explainers, annual reports, old proposals, or articles merely because they appeared in today's newsletter.
- A story already used in a prior JAM brief is filtered before you see it; do not recreate it from a duplicate source.

PRIMARY PRACTICE PRIORITY:
1. California state employment/labor law and California employer compliance.
2. Ninth Circuit and California federal district employment/labor cases.
3. Federal employment/labor agencies and nationally applicable employment law.
4. Minnesota/Eighth Circuit employment matters when meaningful.

SOURCE PRIORITY:
- Lexology/ACC Newsstand and ELINfonet are mandatory specialist INPUTS and should be reviewed carefully, but they do not receive automatic inclusion.
- When multiple sources cover the same development, choose ONE note and prefer the most authoritative source: primary authority/agency first, then strong specialist analysis.
- Do not use a weaker duplicate merely to increase the count.

LENGTH / SELECTION:
- Target 6–10 TOTAL legal notes on an active day; fewer is acceptable on a quiet day.
- Usually 4–6 California notes and 2–4 federal/Minnesota notes when that much genuinely strong fresh material exists.
- HARD MAXIMUM: 10 total legal notes, 6 California notes, 4 other notes.
- The user prefers more useful coverage rather than an artificially tiny section, but NEVER pad with stale, repetitive, promotional, or low-value material.
- One underlying legal development gets ONE note. Consolidate duplicate coverage of the same bill, guidance, case, rule, or agency action.

Return VALID JSON ONLY:
{{
  "california_notes": [
    {{
      "heading": "actual case name + court when supplied, or concise California legal development title",
      "jurisdiction_topic": "e.g. California — Wage & Hour; Ninth Circuit — FEHA/ADA",
      "development": "concise, legally precise development/holding/rule/guidance",
      "employer_takeaway": "specific California/employer practice significance; empty if source too thin",
      "court": "",
      "case": "",
      "date": "",
      "effective_date": "",
      "source": "...",
      "url": "..."
    }}
  ],
  "other_legal_notes": [same shape],
  "specialist_source_review": [
    {{
      "source": "Lexology Daily Newsfeed or ELINfonet Daily Employment Law Update",
      "title": "candidate title",
      "decision": "included | duplicate | outside_scope | stale | too_thin",
      "reason": "brief reason"
    }}
  ],
  "california_candidate_review": [
    {{
      "title": "California candidate title",
      "source": "...",
      "decision": "included | duplicate | outside_scope | stale | too_thin",
      "reason": "brief reason"
    }}
  ]
}}

CALIFORNIA RULES:
- California notes appear FIRST.
- Prefer enacted/pending employer obligations, appellate decisions, CRD/DIR/DLSE/Cal-OSHA actions, PAGA, wage/hour, FEHA, leave/accommodation, restrictive covenants, privacy, workplace AI, and meaningful Ninth Circuit/California federal decisions.
- Do not include generic worker-rights awareness articles, ordinary allegations, annual reports, webinars, marketing, or evergreen explainers unless they contain a genuine new legal development.
- Multiple articles about the same California AI bill are ONE development, not several notes.
- Multiple articles about the same TPS guidance are ONE development, not several notes.

OTHER LEGAL NOTES:
- Prefer consequential EEOC, NLRB, DOL, OSHA, Supreme Court, federal statute/regulation, and meaningful Minnesota/Eighth Circuit developments.
- Exclude state-specific developments outside California/Minnesota unless they have clear national significance.
- Employee benefits/pension items are secondary and should be included only when unusually significant to employer compliance.

ACCURACY:
- One development per note.
- Never invent case names, holdings, deadlines, effective dates, obligations, penalties, or remedies.
- Never call a district-court ruling precedent.
- Never describe a circuit decision as binding nationwide.
- Advocacy for a pending bill is NOT an enacted rule; phrase the takeaway conditionally.
- Do not infer a reasonable-accommodation duty, termination restriction, or other specific obligation unless the supplied source supports it.
- Federal-sector EEOC procedures do not automatically apply to federal contractors or private employers.
- If source material is thin, omit the unsupported detail or omit the note.
"""


def clean_general_story(story):
    for key in ("headline", "summary", "why_it_matters", "source"):
        story[key] = fix_text_encoding(story.get(key, ""))
    return story


def clean_legal_note(note):
    for key in (
        "heading", "jurisdiction_topic", "development", "employer_takeaway",
        "court", "case", "date", "effective_date", "source",
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
        "You are a precise senior general-news editor. Obey section counts and cross-section deduplication. Return valid JSON only.",
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
        "You are a senior California-focused employment-and-labor-law editor. California practice updates receive first priority. Return valid JSON only.",
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

    general_items = select_general_candidates(general_recent, general_max)
    legal_items = select_legal_candidates(legal_recent, legal_max)

    if not general_items:
        raise RuntimeError("No recent general-news stories found. Run scraper.py first.")

    profile = PROFILE_FILE.read_text(encoding="utf-8")

    general_compact = [compact_story(i, idx + 1) for idx, i in enumerate(general_items)]
    legal_compact = [compact_story(i, idx + 1) for idx, i in enumerate(legal_items)]

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
    general_digest = parse_json_response(general_raw)
    general_digest = {
        **general_digest,
        "national_headlines": [clean_general_story(s) for s in general_digest.get("national_headlines", [])],
        "global_headlines": [clean_general_story(s) for s in general_digest.get("global_headlines", [])],
        "minnesota": [clean_general_story(s) for s in general_digest.get("minnesota", [])],
        "tech_news": [clean_general_story(s) for s in general_digest.get("tech_news", [])],
        "entertainment": [clean_general_story(s) for s in general_digest.get("entertainment", [])],
        "good_news": [clean_general_story(s) for s in general_digest.get("good_news", [])],
    }
    general_errors = validate_general(general_digest)
    if general_errors:
        print("General editor validation errors; requesting repair:", general_errors)
        general_digest = repair_general_if_needed(profile, general_compact, general_digest, general_errors)
        general_digest = {
            **general_digest,
            "national_headlines": [clean_general_story(s) for s in general_digest.get("national_headlines", [])],
            "global_headlines": [clean_general_story(s) for s in general_digest.get("global_headlines", [])],
            "minnesota": [clean_general_story(s) for s in general_digest.get("minnesota", [])],
            "tech_news": [clean_general_story(s) for s in general_digest.get("tech_news", [])],
            "entertainment": [clean_general_story(s) for s in general_digest.get("entertainment", [])],
            "good_news": [clean_general_story(s) for s in general_digest.get("good_news", [])],
        }

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

        legal_validation_errors = validate_legal(legal_digest, legal_compact)
        if legal_validation_errors:
            print("Legal editor validation errors; requesting repair:", legal_validation_errors)
            legal_digest = repair_legal_if_needed(
                profile, legal_compact, legal_digest, legal_validation_errors
            )
            legal_validation_errors = validate_legal(legal_digest, legal_compact)

        california_legal_notes = [
            clean_legal_note(n) for n in legal_digest.get("california_notes", [])
        ]
        other_legal_notes = [
            clean_legal_note(n) for n in legal_digest.get("other_legal_notes", [])
        ]
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
