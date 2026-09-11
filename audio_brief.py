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
    prompt = f"""Turn the following written morning briefing into an intelligent, natural 9–11 minute spoken morning-news briefing.

Requirements:
- Preserve factual accuracy and uncertainty.
- Do not add facts that are not in the written briefing.
- Do not read URLs, source-language labels, or formatting aloud.
- Use smooth, concise transitions.
- Employment & Labor Law is the professional centerpiece. Cover California employment developments first and with the most useful detail available.
- Then cover the strongest federal and Minnesota/Eighth Circuit employment developments.
- For legal stories, clearly distinguish proposals, allegations, agency guidance, and holdings. Do not invent a holding or employer obligation when the written briefing does not supply one.
- Cover national, global, and Minnesota news efficiently.
- Keep Tech & AI, Entertainment & Culture, and Good News lighter and shorter near the end.
- Do NOT add a 'what to watch today' section.
- Do not say you are an AI.
- Output the spoken script only.

BRIEFING JSON:
{json.dumps(digest, ensure_ascii=False)}
"""
    r = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "JAM Morning Brief Audio"},
        json={
            "model": model,
            "temperature": 0.35,
            "messages": [
                {"role": "system", "content": "You are a polished morning-news audio producer creating a concise, engaging personal podcast."},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def split_for_tts(text, max_chars=3400):
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks = []
    current = ""
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
            chunks.append(current[: cut + (2 if current[cut:cut+2] == '. ' else 0)].strip())
            current = current[cut + (2 if current[cut:cut+2] == '. ' else 0):].strip()
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
    speed = float(os.getenv("TTS_SPEED", "1.08"))
    style = os.getenv("TTS_STYLE", "fitness_instructor").strip().lower()

    style_instructions = {
        "fitness_instructor": (
            "Voice: High-energy, upbeat, and encouraging. "
            "Delivery: brisk, dynamic, articulate, and engaging, with purposeful pauses. "
            "Tone: positive, energetic, and confident. "
            "Adapt the energy to a professional morning-news podcast: lively but not theatrical, "
            "not shouty, and especially clear and composed during legal stories."
        ),
        "news": (
            "Speak like a polished, warm morning-news host. "
            "Brisk, articulate, conversational, and confident; never theatrical."
        ),
    }
    instructions = style_instructions.get(style, style_instructions["news"])
    combined = AudioSegment.empty()
    for idx, chunk in enumerate(split_for_tts(text), start=1):
        payload = {
            "model": model,
            "voice": voice,
            "input": chunk,
            "response_format": "mp3",
            "speed": speed,
        }
        if model.startswith("gpt-4o"):
            payload["instructions"] = instructions
        r = requests.post(
            OPENAI_SPEECH_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
    print(f"TTS voice={voice}, style={style}, speed={speed}")
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
