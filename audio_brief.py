import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from pydub import AudioSegment

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"


def call_openrouter_for_script(digest):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("OPENROUTER_MODEL", "").strip() or "openai/gpt-4.1-mini"

    prompt = f"""Turn this written JAM Morning Brief into a polished spoken morning rundown for someone listening while getting ready for the day.

TARGET:
- Approximately 9–13 minutes.
- Natural, calm, intelligent, conversational.
- It should sound like a personal morning-news briefing, not like an article being read aloud.
- Do not say you are an AI.
- Do not read URLs, labels such as “source,” or email formatting aloud.
- Preserve factual uncertainty and legal precision.

ORDER — KEEP THIS EXACT:
1. Three top U.S. national headlines
2. Three top global headlines
3. Two Minnesota headlines
4. Employment & Labor Law Notes
5. One or two Tech & AI stories
6. One or two Entertainment & Culture stories
7. One or two Good News stories

LEGAL AUDIO STYLE:
- The legal section is for an employment attorney whose PRIMARY practice is California.
- Begin the legal segment with California Employment — Primary Practice.
- Give California state employment law, California agencies, Ninth Circuit employment cases, and California federal district employment cases the most attention.
- Then cover the strongest federal employment/labor developments.
- Minnesota/Eighth Circuit employment matters are secondary unless especially significant.
- Keep the legal content somewhat denser than the general-news sections, but still easy to follow by ear.
- Name the court or agency when relevant.
- State the development/holding, then give the practical employer or practice takeaway.
- Do not turn unrelated legal cases into a general court-news roundup.

TRANSITIONS:
Use short natural transitions such as “In Minnesota,” “Turning to employment law,” and “On the tech side.”
Do not announce numbered lists unless it sounds natural.

ENDING:
- Do NOT include a “What to Watch Today” segment.
- End on the Good News story with a warm but restrained closing sentence.
- Do not add facts that are not in the briefing.

Output the spoken script only.

BRIEFING JSON:
{json.dumps(digest, ensure_ascii=False)}
"""

    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "JAM Morning Brief Audio",
        },
        json={
            "model": model,
            "temperature": 0.30,
            "messages": [
                {"role": "system", "content": "You are a polished morning-news audio producer with excellent legal accuracy."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=150,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def split_for_tts(text, max_chars=3400):
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        candidate = (current + "\n\n" + p).strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = p
        while len(current) > max_chars:
            cut = current.rfind(". ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            extra = 2 if current[cut:cut+2] == ". " else 0
            chunks.append(current[:cut + extra].strip())
            current = current[cut + extra:].strip()
    if current:
        chunks.append(current)
    return chunks


def synthesize(text):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY not set; skipping audio generation.")
        return None

    model = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
    voice = os.getenv("TTS_VOICE", "marin")
    combined = AudioSegment.empty()

    for idx, chunk in enumerate(split_for_tts(text), start=1):
        payload = {
            "model": model,
            "voice": voice,
            "input": chunk,
            "response_format": "mp3",
        }
        if model.startswith("gpt-4o"):
            payload["instructions"] = (
                "Speak like a polished, warm morning-news host. "
                "Clear, measured and conversational. Legal items should sound precise but not stiff. "
                "Use subtle changes of pacing between hard news and lighter closing items."
            )

        r = requests.post(
            OPENAI_SPEECH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        )
        r.raise_for_status()
        segment = AudioSegment.from_file(io.BytesIO(r.content), format="mp3")
        if len(combined):
            combined += AudioSegment.silent(duration=350)
        combined += segment
        print(f"Synthesized audio chunk {idx}")

    out = OUTPUT / f"brief-{datetime.now().strftime('%Y-%m-%d')}.mp3"
    combined.export(out, format="mp3", bitrate="96k")
    return out


def main():
    digest_path = OUTPUT / "latest_digest.json"
    if not digest_path.exists():
        raise RuntimeError("latest_digest.json not found. Run daily_digest.py first.")

    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    script = call_openrouter_for_script(digest)
    if not script:
        print("No OpenRouter key; skipping audio script.")
        return

    (OUTPUT / "latest_audio_script.txt").write_text(script, encoding="utf-8")
    out = synthesize(script)
    if out:
        print(f"Created {out}")


if __name__ == "__main__":
    main()
