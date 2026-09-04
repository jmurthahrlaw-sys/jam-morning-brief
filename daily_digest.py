import html
import json
import os
import re
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
    "Federal Courts",
    "Minnesota Law",
    "California Law",
    "Politics & Government",
    "Business & Economy",
    "Health & Science",
    "Education & Higher Education",
    "Entertainment & Culture",
    "Worth Knowing",
]


def within_lookback(item, hours):
    try:
        dt = dateparser.parse(item.get("published_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        return True


def compact_story(item, idx):
    return {
        "id": idx,
        "title": item.get("title", "")[:300],
        "summary": item.get("summary", "")[:700],
        "source": item.get("source", ""),
        "url": item.get("url", ""),
        "category_hint": item.get("category_hint", ""),
        "priority_hint": item.get("priority", 5),
        "published_at": item.get("published_at", ""),
    }


def build_prompt(profile, stories):
    return f"""You are producing today's JAM Morning Brief. Follow the editorial profile exactly.

EDITORIAL PROFILE:
{profile}

AVAILABLE STORIES FROM THE LAST ~24 HOURS:
{json.dumps(stories, ensure_ascii=False)}

TASK:
Select and synthesize only the stories worth including. Merge duplicate coverage into a single item. Prefer the most authoritative/primary source available. Do not invent facts beyond the supplied material. If an item is thin, be appropriately cautious.

Return VALID JSON ONLY, no markdown fences, with exactly this top-level shape:
{{
  "date": "Month D, YYYY",
  "intro": "1-2 sentence overview of the morning",
  "top_stories": [
    {{
      "headline": "...",
      "summary": "2-4 sentences",
      "why_it_matters": "1-2 sentences",
      "source": "outlet/source",
      "url": "https://...",
      "court": "optional; empty string if not legal",
      "case": "optional; empty string if not legal",
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
- 5 to 8 top stories maximum.
- Most sections: 1 to 4 stories; omit empty sections.
- Entertainment: usually 2 to 4 items.
- Keep the full written digest near a 5-minute read.
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
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are a precise senior news editor. Return valid JSON only."},
            {"role": "user", "content": prompt},
        ],
    }
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/jmurthahrlaw-sys/jam_editorial_profile.txt",
            "X-Title": "JAM Morning Brief",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def fallback_digest(items):
    top = sorted(items, key=lambda x: int(x.get("priority", 0)), reverse=True)[:8]
    def story(i):
        return {
            "headline": i.get("title", ""),
            "summary": i.get("summary", "")[:450],
            "why_it_matters": "",
            "source": i.get("source", ""),
            "url": i.get("url", ""),
            "court": "",
            "case": "",
            "holding_or_development": "",
            "practical_effect": "",
        }
    grouped = {}
    for i in items[8:]:
        grouped.setdefault(i.get("category_hint", "Worth Knowing"), []).append(story(i))
    return {
        "date": datetime.now().strftime("%B %-d, %Y"),
        "intro": "AI summarization was unavailable, so this edition lists the highest-priority collected stories.",
        "top_stories": [story(i) for i in top],
        "sections": [
            {"name": k if k in SECTION_ORDER else "Worth Knowing", "stories": v[:4]}
            for k, v in grouped.items() if v
        ],
        "what_to_watch": [],
    }


def render_story(s):
    headline = html.escape(s.get("headline", ""))
    summary = html.escape(s.get("summary", ""))
    why = html.escape(s.get("why_it_matters", ""))
    source = html.escape(s.get("source", ""))
    url = html.escape(s.get("url", ""), quote=True)
    legal_bits = []
    for label, key in [
        ("Court", "court"),
        ("Case", "case"),
        ("Holding / development", "holding_or_development"),
        ("Practical effect", "practical_effect"),
    ]:
        value = (s.get(key) or "").strip()
        if value:
            legal_bits.append(f'<div style="margin-top:6px"><strong>{label}:</strong> {html.escape(value)}</div>')
    why_html = f'<div style="margin-top:9px"><strong>Why it matters:</strong> {why}</div>' if why else ""
    source_html = f'<a href="{url}" style="color:#315d74;text-decoration:none">{source or "Read source"}</a>' if url else source
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
    watch_html = "".join(f"<li style='margin:7px 0'>{html.escape(x)}</li>" for x in watch)
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
    lines = ["JAM MORNING BRIEF", digest.get("date", ""), "", digest.get("intro", ""), "", "TOP STORIES"]
    for s in digest.get("top_stories", []):
        lines.extend([f"\n{s.get('headline','')}", s.get("summary", "")])
        if s.get("why_it_matters"):
            lines.append(f"Why it matters: {s['why_it_matters']}")
        if s.get("source"):
            lines.append(f"Source: {s.get('source')} {s.get('url','')}")
    for section in digest.get("sections", []):
        lines.append(f"\n{section.get('name','').upper()}")
        for s in section.get("stories", []):
            lines.extend([f"\n{s.get('headline','')}", s.get("summary", "")])
            if s.get("why_it_matters"):
                lines.append(f"Why it matters: {s['why_it_matters']}")
            if s.get("source"):
                lines.append(f"Source: {s.get('source')} {s.get('url','')}")
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
    items.sort(key=lambda x: (int(x.get("priority", 0)), x.get("published_at", "")), reverse=True)
    max_items = int(os.getenv("MAX_STORIES_FOR_AI", "120"))
    items = items[:max_items]

    if not items:
        raise RuntimeError("No recent stories found. Run scraper.py first.")

    profile = PROFILE_FILE.read_text(encoding="utf-8")
    compact = [compact_story(item, idx + 1) for idx, item in enumerate(items)]
    try:
        raw = call_openrouter(build_prompt(profile, compact))
        digest = parse_json_response(raw)
    except Exception as exc:
        print(f"WARNING: AI digest failed; using fallback: {exc}")
        digest = fallback_digest(items)

    today = datetime.now().strftime("%Y-%m-%d")
    (OUTPUT / "latest_digest.json").write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUTPUT / f"brief-{today}.html").write_text(render_html(digest), encoding="utf-8")
    (OUTPUT / f"brief-{today}.txt").write_text(render_text(digest), encoding="utf-8")
    print(f"Created daily digest with {len(digest.get('top_stories', []))} top stories.")


if __name__ == "__main__":
    main()
