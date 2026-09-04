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

SECTION_ORDER = [
    "Top News",
    "Minnesota",
    "U.S. Supreme Court",
    "Federal Courts — Minnesota / Eighth Circuit",
    "Federal Courts — California / Ninth Circuit",
    "Minnesota Law",
    "California Law",
    "Federal Employment & Labor",
    "California Employment & Labor",
    "Politics & Government",
    "Business & Economy",
    "Health & Science",
    "Education & Higher Education",
    "Entertainment & Culture",
    "Worth Knowing",
]

# Minimum representation in the AI candidate pool. These are not output quotas.
CANDIDATE_MINIMUMS = {
    "Top News": 12,
    "Minnesota": 10,
    "U.S. Supreme Court": 8,
    "Federal Courts": 10,
    "Minnesota Law": 8,
    "California Law": 10,
    "Employment Law": 8,
    "Legal — Unsorted": 10,
    "Politics & Government": 6,
    "Business & Economy": 6,
    "Health & Science": 5,
    "Education & Higher Education": 4,
    "Entertainment & Culture": 7,
}

TRUSTED_LEGAL_SOURCES = {
    "Lexology Daily Newsfeed",
    "ELINfonet Daily Employment Law Update",
}


def fix_text_encoding(value):
    """Repair common UTF-8/Windows-1252 mojibake without changing normal text."""
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
        "â€™": "’",
        "â€˜": "‘",
        "â€œ": "“",
        "â€": "”",
        "â€“": "–",
        "â€”": "—",
        "Â ": " ",
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


def item_rank(item):
    trusted = 1 if item.get("source") in TRUSTED_LEGAL_SOURCES else 0
    return (trusted, int(item.get("priority", 0)), item.get("published_at", ""))


def select_candidates(items, max_items):
    """Preserve subject diversity before sending candidates to the model."""
    buckets = defaultdict(list)
    for item in sorted(items, key=item_rank, reverse=True):
        buckets[item.get("category_hint", "Worth Knowing")].append(item)

    selected = []
    seen = set()

    def add(item):
        key = item.get("id") or item.get("url") or item.get("title")
        if not key or key in seen:
            return
        selected.append(item)
        seen.add(key)

    # Give every important category a guaranteed look before globally filling.
    for category, minimum in CANDIDATE_MINIMUMS.items():
        for item in buckets.get(category, [])[:minimum]:
            add(item)

    # Explicitly guarantee review of a reasonable number of trusted legal-source items.
    for source_name in TRUSTED_LEGAL_SOURCES:
        source_items = [i for i in items if i.get("source") == source_name]
        for item in sorted(source_items, key=item_rank, reverse=True)[:15]:
            add(item)

    # Fill remaining capacity by overall rank.
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
        "summary": fix_text_encoding(item.get("summary", ""))[:700],
        "source": source,
        "url": item.get("url", ""),
        "category_hint": item.get("category_hint", ""),
        "priority_hint": item.get("priority", 5),
        "published_at": item.get("published_at", ""),
        "origin": item.get("origin", ""),
        "trusted_legal_source": source in TRUSTED_LEGAL_SOURCES,
    }


def build_prompt(profile, stories):
    return f"""You are producing today's JAM Morning Brief. Follow the editorial profile exactly.

EDITORIAL PROFILE:
{profile}

AVAILABLE STORIES FROM THE LAST ~24 HOURS:
{json.dumps(stories, ensure_ascii=False)}

TASK:
Select and synthesize only the stories worth including. Merge duplicate coverage into a single item. Prefer the most authoritative or primary source available. Do not invent facts beyond the supplied material. If an item is thin, be cautious rather than filling gaps.

IMPORTANT SELECTION RULES:
- Top Stories: 5 to 7 items, across the whole news agenda.
- Ordinarily no more than TWO Top Stories should be primarily legal.
- Do not repeat the same story in Top Stories and a lower section.
- One legal case/development per story. Never combine unrelated court decisions.
- Review Lexology Daily Newsfeed and ELINfonet items carefully when supplied. Include a material employment-law development when warranted, but do not force routine/promotional material.
- For federal court items, distinguish Minnesota/Eighth Circuit from California/Ninth Circuit where the source material permits.
- Put California employment/labor developments in "California Employment & Labor" when that is the principal significance.
- Put federal agency/employment/labor developments in "Federal Employment & Labor" when appropriate.
- If a requested section has no meaningful item, OMIT the section rather than filling it with weak material.
- Entertainment should usually contain 2 to 3 genuinely worthwhile items, not filler.
- The intro must summarize the stories actually selected.
- "What to Watch Today" must concern plausible developments in the next 24 hours, not vague future events.
- Never say the Supreme Court is about to decide a case merely because certiorari was granted.
- Never describe a circuit ruling as binding nationwide.
- Never call a district-court ruling precedent.

Return VALID JSON ONLY, no markdown fences, with exactly this top-level shape:
{{
  "date": "Month D, YYYY",
  "intro": "1-2 sentence overview of the actual selected stories",
  "top_stories": [
    {{
      "headline": "...",
      "summary": "2-4 sentences",
      "why_it_matters": "1-2 concrete sentences",
      "source": "outlet/source",
      "url": "https://...",
      "court": "optional; empty string if not legal",
      "case": "optional; empty string if not legal or not supplied",
      "case_date": "optional; empty string if not supplied",
      "holding_or_development": "optional; empty string if not legal",
      "practical_effect": "optional; empty string if not legal"
    }}
  ],
  "sections": [
    {{
      "name": "one of: {', '.join(SECTION_ORDER[1:])}",
      "stories": [same story object shape]
    }}
  ],
  "what_to_watch": ["item 1", "item 2", "item 3"]
}}

Rules for size:
- 5 to 7 top stories.
- Most sections: 1 to 4 stories; omit empty sections.
- Entertainment: usually 2 to 3 items.
- Keep the full written digest near a 5–7 minute read.
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
    payload = {
        "model": model,
        "temperature": 0.15,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise senior news editor with unusually careful legal judgment. Return valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/jmurthahrlaw-sys/jam-morning-brief",
            "X-Title": "JAM Morning Brief",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def fallback_digest(items):
    top = sorted(items, key=lambda x: int(x.get("priority", 0)), reverse=True)[:7]

    def story(i):
        return {
            "headline": fix_text_encoding(i.get("title", "")),
            "summary": fix_text_encoding(i.get("summary", ""))[:450],
            "why_it_matters": "",
            "source": i.get("source", ""),
            "url": i.get("url", ""),
            "court": "",
            "case": "",
            "case_date": "",
            "holding_or_development": "",
            "practical_effect": "",
        }

    grouped = {}
    for i in items[7:]:
        grouped.setdefault(i.get("category_hint", "Worth Knowing"), []).append(story(i))
    return {
        "date": datetime.now().strftime("%B %-d, %Y"),
        "intro": "AI summarization was unavailable, so this edition lists the highest-priority collected stories.",
        "top_stories": [story(i) for i in top],
        "sections": [
            {"name": k if k in SECTION_ORDER else "Worth Knowing", "stories": v[:4]}
            for k, v in grouped.items()
            if v
        ],
        "what_to_watch": [],
    }


def clean_digest_text(digest):
    def clean_story(story):
        for key in [
            "headline",
            "summary",
            "why_it_matters",
            "source",
            "court",
            "case",
            "case_date",
            "holding_or_development",
            "practical_effect",
        ]:
            if key in story:
                story[key] = fix_text_encoding(story.get(key, ""))
        return story

    digest["date"] = fix_text_encoding(digest.get("date", ""))
    digest["intro"] = fix_text_encoding(digest.get("intro", ""))
    digest["top_stories"] = [clean_story(s) for s in digest.get("top_stories", [])]
    for section in digest.get("sections", []):
        section["name"] = fix_text_encoding(section.get("name", ""))
        section["stories"] = [clean_story(s) for s in section.get("stories", [])]
    digest["what_to_watch"] = [fix_text_encoding(x) for x in digest.get("what_to_watch", [])]
    return digest


def render_story(s):
    headline = html.escape(fix_text_encoding(s.get("headline", "")))
    summary = html.escape(fix_text_encoding(s.get("summary", "")))
    why = html.escape(fix_text_encoding(s.get("why_it_matters", "")))
    source = html.escape(fix_text_encoding(s.get("source", "")))
    url = html.escape(s.get("url", ""), quote=True)
    legal_bits = []
    for label, key in [
        ("Court", "court"),
        ("Case", "case"),
        ("Date", "case_date"),
        ("Holding / development", "holding_or_development"),
        ("Practical effect", "practical_effect"),
    ]:
        value = fix_text_encoding((s.get(key) or "").strip())
        if value:
            legal_bits.append(
                f'<div style="margin-top:6px"><strong>{label}:</strong> {html.escape(value)}</div>'
            )
    why_html = (
        f'<div style="margin-top:9px"><strong>Why it matters:</strong> {why}</div>' if why else ""
    )
    source_html = (
        f'<a href="{url}" style="color:#315d74;text-decoration:none">{source or "Read source"}</a>'
        if url
        else source
    )
    return f"""
    <div style="padding:18px 0;border-bottom:1px solid #e7e4de">
      <div style="font-size:19px;line-height:1.25;font-weight:700;color:#17232b">{headline}</div>
      <div style="font-size:15px;line-height:1.55;margin-top:8px;color:#303b42">{summary}</div>
      {why_html}
      {''.join(legal_bits)}
      <div style="font-size:13px;margin-top:10px;color:#6b747a">{source_html}</div>
    </div>
    """


def render_html(digest):
    sections_html = ""
    for section in digest.get("sections", []):
        stories = section.get("stories") or []
        if not stories:
            continue
        sections_html += f"""
        <div style="font-size:13px;letter-spacing:.09em;text-transform:uppercase;font-weight:800;color:#5b6770;margin-top:34px;margin-bottom:2px">{html.escape(section.get('name',''))}</div>
        {''.join(render_story(s) for s in stories)}
        """

    watch = digest.get("what_to_watch") or []
    watch_html = "".join(
        f"<li style='margin:7px 0'>{html.escape(fix_text_encoding(x))}</li>" for x in watch
    )
    top_html = "".join(render_story(s) for s in digest.get("top_stories", []))
    return f"""<!doctype html>
<html><body style="margin:0;background:#f3f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#26333a">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f1ec;padding:24px 10px"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:720px;background:#ffffff;border-radius:14px;overflow:hidden">
<tr><td style="padding:34px 36px 18px 36px">
  <div style="font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#65757f;font-weight:800">JAM Morning Brief</div>
  <div style="font-family:Georgia,'Times New Roman',serif;font-size:34px;line-height:1.1;margin-top:8px;color:#17232b">{html.escape(digest.get('date',''))}</div>
  <div style="font-size:16px;line-height:1.55;margin-top:12px;color:#4c5960">{html.escape(digest.get('intro',''))}</div>

  <div style="font-size:13px;letter-spacing:.09em;text-transform:uppercase;font-weight:800;color:#5b6770;margin-top:32px">Top Stories</div>
  {top_html}
  {sections_html}

  <div style="margin-top:34px;background:#f4f6f5;border-radius:10px;padding:18px 20px">
    <div style="font-size:13px;letter-spacing:.09em;text-transform:uppercase;font-weight:800;color:#5b6770">What to Watch Today</div>
    <ol style="padding-left:20px;margin:10px 0 0 0;font-size:15px;line-height:1.5">{watch_html}</ol>
  </div>
  <div style="font-size:11px;color:#8a9297;margin-top:30px">Personal briefing generated from selected sources. Follow source links for full reporting and primary legal materials.</div>
</td></tr></table>
</td></tr></table>
</body></html>"""


def render_text(digest):
    lines = [
        "JAM MORNING BRIEF",
        digest.get("date", ""),
        "",
        digest.get("intro", ""),
        "",
        "TOP STORIES",
    ]

    def append_story(s):
        lines.extend([f"\n{s.get('headline','')}", s.get("summary", "")])
        if s.get("why_it_matters"):
            lines.append(f"Why it matters: {s['why_it_matters']}")
        for label, key in [
            ("Court", "court"),
            ("Case", "case"),
            ("Date", "case_date"),
            ("Holding / development", "holding_or_development"),
            ("Practical effect", "practical_effect"),
        ]:
            if s.get(key):
                lines.append(f"{label}: {s[key]}")
        if s.get("source"):
            lines.append(f"Source: {s.get('source')} {s.get('url','')}")

    for s in digest.get("top_stories", []):
        append_story(s)

    for section in digest.get("sections", []):
        lines.append(f"\n{section.get('name','').upper()}")
        for s in section.get("stories", []):
            append_story(s)

    lines.append("\nWHAT TO WATCH TODAY")
    for x in digest.get("what_to_watch", []):
        lines.append(f"- {x}")
    return "\n".join(lines).strip() + "\n"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    items = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    lookback = int(os.getenv("LOOKBACK_HOURS", "30"))
    items = [i for i in items if within_lookback(i, lookback)]
    items = dedupe_near(dedupe_exact(items), threshold=87)
    items.sort(key=item_rank, reverse=True)

    max_items = int(os.getenv("MAX_STORIES_FOR_AI", "120"))
    candidates = select_candidates(items, max_items)

    if not candidates:
        raise RuntimeError("No recent stories found. Run scraper.py first.")

    category_counts = Counter(i.get("category_hint", "") for i in candidates)
    source_counts = Counter(i.get("source", "") for i in candidates)
    print(f"Sending {len(candidates)} candidate stories to AI.")
    print("Candidate categories:", dict(category_counts))
    print(
        "Trusted legal candidates:",
        {
            source: source_counts.get(source, 0)
            for source in sorted(TRUSTED_LEGAL_SOURCES)
        },
    )

    profile = PROFILE_FILE.read_text(encoding="utf-8")
    compact = [compact_story(item, idx + 1) for idx, item in enumerate(candidates)]
    try:
        raw = call_openrouter(build_prompt(profile, compact))
        digest = clean_digest_text(parse_json_response(raw))
    except Exception as exc:
        print(f"WARNING: AI digest failed; using fallback: {exc}")
        digest = fallback_digest(candidates)

    today = datetime.now().strftime("%Y-%m-%d")
    (OUTPUT / "latest_digest.json").write_text(
        json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUTPUT / f"brief-{today}.html").write_text(render_html(digest), encoding="utf-8")
    (OUTPUT / f"brief-{today}.txt").write_text(render_text(digest), encoding="utf-8")
    print(f"Created daily digest with {len(digest.get('top_stories', []))} top stories.")


if __name__ == "__main__":
    main()
