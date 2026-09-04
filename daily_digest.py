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
from rapidfuzz.fuzz import ratio

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "news.json"
PROFILE_FILE = ROOT / "editorial_profile.txt"
OUTPUT = ROOT / "output"
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

    # 2. Dedicated legal feeds/searches: only keep items with a genuine employment/labor signal.
    dedicated = [
        i for i in items
        if i.get("source") not in TRUSTED_LEGAL_SOURCES
        and i.get("category_hint") in LEGAL_CATEGORIES
        and is_strong_employment_legal(i)
    ]
    for item in sorted(dedicated, key=item_rank, reverse=True):
        if len(selected) >= max_items:
            break
        unique_append(selected, seen, item)

    # 3. General-news sources may enter only for unmistakable employment/labor legal developments.
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
Your reader is an employment attorney. This is a professional practice update, NOT a general court-news section.

EDITORIAL RULES:
{profile}

LEGAL CANDIDATES:
{json.dumps(stories, ensure_ascii=False)}

Lexology Daily Newsfeed and ELINfonet Daily Employment Law Update are PRIMARY specialist sources. Review their supplied items carefully.
Other legal items are included only because they appear potentially employment/labor related.

Return VALID JSON ONLY:
{{
  "legal_notes": [
    {{
      "heading": "actual case name + court when supplied, or concise legal development title",
      "jurisdiction_topic": "e.g. Federal — EEOC; California — Wage & Hour; Eighth Circuit — ADA",
      "development": "concise, legally precise development/holding/rule/guidance",
      "employer_takeaway": "specific practice/compliance significance; empty if source too thin",
      "court": "",
      "case": "",
      "date": "",
      "effective_date": "",
      "source": "...",
      "url": "..."
    }}
  ],
  "specialist_source_review": [
    {{
      "source": "Lexology Daily Newsfeed or ELINfonet Daily Employment Law Update",
      "title": "candidate title",
      "decision": "included | duplicate | outside_scope | stale | too_thin",
      "reason": "brief reason"
    }}
  ]
}}

SELECTION RULE:
- Usually select 4–8 meaningful notes on an active day; fewer is preferable to filler.
- EVERY supplied Lexology and ELINfonet item must be reviewed and represented in specialist_source_review.
- A material employment/labor-law item from Lexology or ELINfonet should ordinarily be INCLUDED unless duplicative, stale, or too thin to state accurately.
- If specialist-source items contain meaningful employment/labor developments, DO NOT return an empty legal_notes array.
- EXCLUDE criminal, eminent-domain, tax-procedure, generic public-pension, unrelated constitutional, unrelated commercial, and unrelated land-use cases.
- EXCLUDE generic HR/career advice and promotional pieces without a real legal development.
- Do not include a case just because it came from an Eighth or Ninth Circuit source.
- Include only matters substantially related to employment, labor, workplace regulation, employer compliance, employee rights, labor relations, or a genuinely consequential Supreme Court/administrative-law issue affecting employment practice.
- California state employment law receives very high priority.
- Federal agencies and federal employment/labor developments receive high priority.
- D. Minnesota/Eighth Circuit and Ninth Circuit/California federal employment cases receive high priority.

ACCURACY:
- One development per note.
- Never invent case names, holdings, deadlines or obligations.
- Never call a district-court ruling precedent.
- Never describe a circuit decision as binding nationwide.
- Never manufacture an employer takeaway from an incidental employment connection.
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
    notes = legal_digest.get("legal_notes", [])
    specialist_candidates = [
        c for c in legal_candidates if c.get("source") in TRUSTED_LEGAL_SOURCES
    ]
    review = legal_digest.get("specialist_source_review", [])

    if specialist_candidates and not notes:
        errors.append(
            f"legal_notes is empty despite {len(specialist_candidates)} specialist-source candidates"
        )

    reviewed_titles = {normalize_title(r.get("title", "")) for r in review if r.get("title")}
    missing = [
        c.get("title", "")
        for c in specialist_candidates
        if normalize_title(c.get("title", "")) not in reviewed_titles
    ]
    if missing:
        errors.append(
            "specialist_source_review omitted specialist candidates: "
            + " | ".join(missing[:12])
        )
    return errors


def repair_legal_if_needed(profile, candidates, legal_digest, errors):
    if not errors:
        return legal_digest

    prompt = build_legal_prompt(profile, candidates)
    prompt += "\n\nYOUR PRIOR LEGAL OUTPUT FAILED VALIDATION:\n- " + "\n- ".join(errors)
    prompt += """
Re-review EVERY Lexology and ELINfonet item first.
If any specialist-source item is a material employment/labor-law development, include it.
Do not fill the section with unrelated court news.
Return the COMPLETE corrected JSON, including specialist_source_review.
"""
    raw = call_openrouter(
        prompt,
        "You are a senior employment-and-labor-law editor. The legal practice update is mandatory when meaningful specialist-source items exist. Return valid JSON only.",
        temperature=0.03,
    )
    return parse_json_response(raw)

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
    body += section_html("Employment & Labor Law Notes", d.get("legal_notes", []), render_legal_note)
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

    if d.get("legal_notes"):
        lines.extend(["", "EMPLOYMENT & LABOR LAW NOTES"])
        for n in d["legal_notes"]:
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

    add_general("Tech & AI", d.get("tech_news", []))
    add_general("Entertainment & Culture", d.get("entertainment", []))
    add_general("Good News", d.get("good_news", []))
    return "\n".join(lines).strip() + "\n"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    items = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    items = [normalize_item(i) for i in items]

    lookback = int(os.getenv("LOOKBACK_HOURS", "30"))
    items = [i for i in items if within_lookback(i, lookback)]
    items = dedupe_near(dedupe_exact(items), threshold=87)

    general_max = int(os.getenv("GENERAL_MAX_STORIES_FOR_AI", "150"))
    legal_max = int(os.getenv("LEGAL_MAX_STORIES_FOR_AI", "120"))

    general_items = select_general_candidates(items, general_max)
    legal_items = select_legal_candidates(items, legal_max)

    if not general_items:
        raise RuntimeError("No recent general-news stories found. Run scraper.py first.")

    profile = PROFILE_FILE.read_text(encoding="utf-8")

    general_compact = [compact_story(i, idx + 1) for idx, i in enumerate(general_items)]
    legal_compact = [compact_story(i, idx + 1) for idx, i in enumerate(legal_items)]

    # Diagnostics are intentionally saved in the artifact so source-selection problems are visible.
    source_counts_all = Counter(i.get("source", "") for i in items)
    diagnostics = {
        "recent_total_items": len(items),
        "general_candidate_count": len(general_items),
        "legal_candidate_count": len(legal_items),
        "lexology_recent_items": source_counts_all.get("Lexology Daily Newsfeed", 0),
        "elinfonet_recent_items": source_counts_all.get("ELINfonet Daily Employment Law Update", 0),
        "legal_candidate_sources": dict(Counter(i.get("source", "") for i in legal_items)),
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

    # LEGAL PIPELINE — separate, mandatory professional review
    legal_notes = []
    specialist_review = []
    legal_validation_errors = []

    if legal_compact:
        legal_raw = call_openrouter(
            build_legal_prompt(profile, legal_compact),
            "You are a senior employment-and-labor-law briefing editor. Review every specialist-source item and exclude unrelated court news. Return valid JSON only.",
            temperature=0.04,
        )
        legal_digest = parse_json_response(legal_raw)

        legal_validation_errors = validate_legal(legal_digest, legal_compact)
        if legal_validation_errors:
            print("Legal editor validation errors; requesting repair:", legal_validation_errors)
            legal_digest = repair_legal_if_needed(
                profile, legal_compact, legal_digest, legal_validation_errors
            )
            legal_validation_errors = validate_legal(legal_digest, legal_compact)

        legal_notes = [clean_legal_note(n) for n in legal_digest.get("legal_notes", [])]
        specialist_review = legal_digest.get("specialist_source_review", [])

        if legal_validation_errors:
            print("WARNING: legal validation still has issues:", legal_validation_errors)
    else:
        print("WARNING: No legal candidates available for this run.")

    legal_diagnostics = {
        "legal_candidate_count": len(legal_compact),
        "specialist_candidate_count": sum(
            1 for c in legal_compact if c.get("source") in TRUSTED_LEGAL_SOURCES
        ),
        "specialist_candidates": [
            {"source": c.get("source"), "title": c.get("title"), "url": c.get("url")}
            for c in legal_compact if c.get("source") in TRUSTED_LEGAL_SOURCES
        ],
        "specialist_source_review": specialist_review,
        "legal_note_count": len(legal_notes),
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

    print(
        "Created digest:",
        f"{len(digest['national_headlines'])} national,",
        f"{len(digest['global_headlines'])} global,",
        f"{len(digest['minnesota'])} Minnesota,",
        f"{len(digest['legal_notes'])} legal notes,",
        f"{len(digest['tech_news'])} tech,",
        f"{len(digest['entertainment'])} entertainment,",
        f"{len(digest['good_news'])} good news.",
    )


if __name__ == "__main__":
    main()
