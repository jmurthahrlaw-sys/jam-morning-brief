import html
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dateutil import parser as dateparser

from dedupe import dedupe_exact, dedupe_near

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "news.json"
PROFILE_FILE = ROOT / "editorial_profile.txt"
OUTPUT = ROOT / "output"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TRUSTED_LEGAL_SOURCES = {
    "Lexology Daily Newsfeed",
    "ELINfonet Daily Employment Law Update",
}

# These are candidate-pool minimums, not output quotas.
CANDIDATE_MINIMUMS = {
    "Top News": 22,
    "Minnesota": 12,
    "U.S. Supreme Court": 8,
    "Federal Courts": 12,
    "Minnesota Law": 8,
    "California Law": 12,
    "Employment Law": 16,
    "Legal — Unsorted": 12,
    "Politics & Government": 8,
    "Business & Economy": 8,
    "Health & Science": 6,
    "Education & Higher Education": 4,
    "Entertainment & Culture": 10,
    "Tech & AI": 10,
    "Good News": 6,
}


def fix_text_encoding(value):
    text = html.unescape(str(value or ""))
    bad_markers = ("â€", "â€™", "â€œ", "â€˜", "Ã", "Â")
    if any(marker in text for marker in bad_markers):
        try:
            repaired = text.encode("latin-1").decode("utf-8")
            old_bad = sum(text.count(m) for m in bad_markers)
            new_bad = sum(repaired.count(m) for m in bad_markers)
            if new_bad < old_bad:
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    replacements = {
        "â€™": "’", "â€˜": "‘", "â€œ": "“", "â€": "”",
        "â€“": "–", "â€”": "—", "Â ": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def within_lookback(item, hours):
    try:
        dt = dateparser.parse(item.get("published_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        return True


def is_employment_legal(item):
    text = " ".join([
        item.get("title", ""),
        item.get("summary", ""),
        item.get("category_hint", ""),
    ]).lower()
    terms = [
        "employment", "employee", "employer", "labor", "labour", "eeoc", "nlrb",
        "department of labor", "wage", "hour", "overtime", "discrimination",
        "harassment", "retaliation", "accommodation", "ada", "fmla", "leave",
        "pregnan", "union", "collective bargaining", "worker classification",
        "independent contractor", "noncompete", "restrictive covenant", "paga",
        "cal/osha", "dlse", "civil rights department", "workplace", "hiring",
        "termination", "layoff", "pay transparency", "paid sick", "minimum wage",
    ]
    return any(term in text for term in terms)


def item_rank(item):
    trusted_legal = 2 if item.get("source") in TRUSTED_LEGAL_SOURCES else 0
    employment_bonus = 1 if is_employment_legal(item) else 0
    return (
        trusted_legal,
        employment_bonus,
        int(item.get("priority", 0)),
        item.get("published_at", ""),
    )


def select_candidates(items, max_items):
    buckets = defaultdict(list)
    for item in sorted(items, key=item_rank, reverse=True):
        buckets[item.get("category_hint", "Worth Knowing")].append(item)

    selected, seen = [], set()

    def add(item):
        key = item.get("id") or item.get("url") or item.get("title")
        if not key or key in seen:
            return
        selected.append(item)
        seen.add(key)

    # First: guarantee substantial review of the two specialist legal feeds.
    for source_name in TRUSTED_LEGAL_SOURCES:
        source_items = [i for i in items if i.get("source") == source_name]
        relevant = [i for i in source_items if is_employment_legal(i)]
        for item in sorted(relevant, key=item_rank, reverse=True)[:24]:
            add(item)
        # Also allow a few items the keyword filter may miss.
        for item in sorted(source_items, key=item_rank, reverse=True)[:8]:
            add(item)

    # Second: guarantee subject diversity.
    for category, minimum in CANDIDATE_MINIMUMS.items():
        for item in buckets.get(category, [])[:minimum]:
            add(item)

    # Third: fill remaining capacity by overall rank.
    for item in sorted(items, key=item_rank, reverse=True):
        if len(selected) >= max_items:
            break
        add(item)

    return selected[:max_items]


def compact_story(item, idx):
    source = item.get("source", "")
    return {
        "id": idx,
        "title": fix_text_encoding(item.get("title", ""))[:300],
        "summary": fix_text_encoding(item.get("summary", ""))[:850],
        "source": source,
        "url": item.get("url", ""),
        "category_hint": item.get("category_hint", ""),
        "priority_hint": item.get("priority", 5),
        "published_at": item.get("published_at", ""),
        "origin": item.get("origin", ""),
        "trusted_legal_source": source in TRUSTED_LEGAL_SOURCES,
        "employment_legal_hint": is_employment_legal(item),
    }


def build_prompt(profile, stories):
    return f"""You are producing today's JAM Morning Brief. Follow the editorial profile exactly.

EDITORIAL PROFILE:
{profile}

AVAILABLE STORIES FROM THE LAST ~24 HOURS:
{json.dumps(stories, ensure_ascii=False)}

TASK:
Choose the best available items for the FIXED editorial assignments below.
Do not simply rank all stories together. Fill each assignment on its own merits.
Merge duplicate coverage into one item and prefer the strongest available source.
Do not invent facts beyond supplied material.

FIXED OUTPUT:
- national_headlines: EXACTLY 3
- global_headlines: EXACTLY 3
- minnesota: EXACTLY 2
- legal_notes: flexible, usually 4–8 meaningful EMPLOYMENT/LABOR notes
- tech_news: 1 or 2
- entertainment: 1 or 2
- good_news: 1 or 2
- NO “What to Watch Today”

LEGAL SECTION:
Lexology Daily Newsfeed and ELINfonet Daily Employment Law Update are primary specialist inputs.
The legal section is NOT a general “interesting court cases” section. Its focus is employment and labor law:
federal agencies/law, U.S. Supreme Court employment matters, D. Minn./8th Circuit employment matters,
Ninth Circuit/California federal district employment matters, Minnesota state employment law, and California
state employment law. California employment developments receive very high priority.

Do not use a criminal case, tax case, general constitutional case, or unrelated civil case merely because it is legal.
Include non-employment Supreme Court/legal matters only when unusually consequential to employer regulation,
administrative law, civil rights, or the user's practice.

Return VALID JSON ONLY with exactly this top-level shape:
{{
  "date": "Month D, YYYY",
  "intro": "1-2 sentences accurately describing this edition",
  "national_headlines": [
    {{
      "headline": "...",
      "summary": "2-4 sentences",
      "why_it_matters": "1-2 concrete sentences",
      "source": "outlet/source",
      "url": "https://..."
    }}
  ],
  "global_headlines": [same general-story shape],
  "minnesota": [same general-story shape],
  "legal_notes": [
    {{
      "heading": "case name and court, or concise legal development title",
      "jurisdiction_topic": "e.g. Federal — EEOC; California — Wage & Hour; Eighth Circuit — ADA",
      "development": "concise description of holding, rule, agency action, legislation, or guidance",
      "employer_takeaway": "concrete practice/compliance significance; empty string if source material is too thin",
      "court": "optional; empty string if not applicable",
      "case": "optional; empty string if not supplied",
      "date": "optional; empty string if not supplied",
      "effective_date": "optional; empty string if not supplied",
      "source": "outlet/source",
      "url": "https://..."
    }}
  ],
  "tech_news": [same general-story shape],
  "entertainment": [same general-story shape],
  "good_news": [same general-story shape]
}}

STRICT COUNTS:
- national_headlines MUST contain 3 items.
- global_headlines MUST contain 3 items.
- minnesota MUST contain 2 items.
- tech_news, entertainment, good_news must each contain 1 or 2 items.
- legal_notes should contain only meaningful employment/labor items; fewer than 4 is acceptable if the source material does not support more.
- Never create fake filler to meet a count. If there truly are not enough meaningful items, use the best available and do not invent.

LEGAL ACCURACY:
- One case/development per legal note.
- Never combine unrelated decisions.
- Never call a district-court ruling precedent.
- Never imply a circuit ruling is binding nationwide.
- Never invent a case name, holding, effective date, or employer obligation.
- Distinguish commentary from authority.
- If material is too thin, be cautious.
"""


def parse_json_response(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def call_openrouter(prompt):
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
            "temperature": 0.12,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise senior news editor and employment-law briefing editor. Return valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=150,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def general_story(item):
    return {
        "headline": fix_text_encoding(item.get("title", "")),
        "summary": fix_text_encoding(item.get("summary", ""))[:500],
        "why_it_matters": "",
        "source": item.get("source", ""),
        "url": item.get("url", ""),
    }


def fallback_digest(items):
    ranked = sorted(items, key=item_rank, reverse=True)
    return {
        "date": datetime.now().strftime("%B %-d, %Y"),
        "intro": "AI summarization was unavailable, so this edition contains a basic selection of collected stories.",
        "national_headlines": [general_story(i) for i in ranked[:3]],
        "global_headlines": [general_story(i) for i in ranked[3:6]],
        "minnesota": [general_story(i) for i in ranked if i.get("category_hint") == "Minnesota"][:2],
        "legal_notes": [],
        "tech_news": [general_story(i) for i in ranked if i.get("category_hint") == "Tech & AI"][:1],
        "entertainment": [general_story(i) for i in ranked if i.get("category_hint") == "Entertainment & Culture"][:1],
        "good_news": [],
    }


def clean_general_story(story):
    for key in ["headline", "summary", "why_it_matters", "source"]:
        story[key] = fix_text_encoding(story.get(key, ""))
    return story


def clean_digest_text(digest):
    digest["date"] = fix_text_encoding(digest.get("date", ""))
    digest["intro"] = fix_text_encoding(digest.get("intro", ""))
    for key in ["national_headlines", "global_headlines", "minnesota", "tech_news", "entertainment", "good_news"]:
        digest[key] = [clean_general_story(s) for s in digest.get(key, [])]
    cleaned_legal = []
    for note in digest.get("legal_notes", []):
        for key in [
            "heading", "jurisdiction_topic", "development", "employer_takeaway",
            "court", "case", "date", "effective_date", "source",
        ]:
            note[key] = fix_text_encoding(note.get(key, ""))
        cleaned_legal.append(note)
    digest["legal_notes"] = cleaned_legal
    return digest


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
    for label, key in [
        ("Court", "court"),
        ("Case", "case"),
        ("Date", "date"),
        ("Effective date", "effective_date"),
    ]:
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
<html><body style="margin:0;background:#f3f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#26333a">
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
            for label, key in [("Court","court"),("Case","case"),("Date","date"),("Effective date","effective_date")]:
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
    lookback = int(os.getenv("LOOKBACK_HOURS", "30"))
    items = [i for i in items if within_lookback(i, lookback)]
    items = dedupe_near(dedupe_exact(items), threshold=87)
    items.sort(key=item_rank, reverse=True)

    max_items = int(os.getenv("MAX_STORIES_FOR_AI", "150"))
    candidates = select_candidates(items, max_items)
    if not candidates:
        raise RuntimeError("No recent stories found. Run scraper.py first.")

    category_counts = Counter(i.get("category_hint", "") for i in candidates)
    source_counts = Counter(i.get("source", "") for i in candidates)
    print(f"Sending {len(candidates)} candidate stories to AI.")
    print("Candidate categories:", dict(category_counts))
    print("Trusted legal candidates:", {s: source_counts.get(s, 0) for s in sorted(TRUSTED_LEGAL_SOURCES)})
    print("Employment/legal-hint candidates:", sum(1 for i in candidates if is_employment_legal(i)))

    profile = PROFILE_FILE.read_text(encoding="utf-8")
    compact = [compact_story(item, idx + 1) for idx, item in enumerate(candidates)]

    try:
        raw = call_openrouter(build_prompt(profile, compact))
        digest = clean_digest_text(parse_json_response(raw))
    except Exception as exc:
        print(f"WARNING: AI digest failed; using fallback: {exc}")
        digest = fallback_digest(candidates)

    today = datetime.now().strftime("%Y-%m-%d")
    (OUTPUT / "latest_digest.json").write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUTPUT / f"brief-{today}.html").write_text(render_html(digest), encoding="utf-8")
    (OUTPUT / f"brief-{today}.txt").write_text(render_text(digest), encoding="utf-8")
    print(
        "Created digest:",
        f"{len(digest.get('national_headlines', []))} national,",
        f"{len(digest.get('global_headlines', []))} global,",
        f"{len(digest.get('minnesota', []))} Minnesota,",
        f"{len(digest.get('legal_notes', []))} legal notes,",
        f"{len(digest.get('tech_news', []))} tech,",
        f"{len(digest.get('entertainment', []))} entertainment,",
        f"{len(digest.get('good_news', []))} good news.",
    )


if __name__ == "__main__":
    main()
