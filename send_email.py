import base64
import json
import os
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def main():
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    to_email = os.getenv("BRIEF_TO_EMAIL", "").strip()
    from_email = os.getenv("BRIEF_FROM_EMAIL", "").strip()
    if not api_key or not to_email or not from_email:
        print("Brevo email secrets are incomplete; briefing files were created but email was not sent.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    html_path = OUTPUT / f"brief-{today}.html"
    text_path = OUTPUT / f"brief-{today}.txt"
    if not html_path.exists():
        raise RuntimeError("HTML brief not found. Run daily_digest.py first.")

    payload = {
        "sender": {
            "name": os.getenv("BRIEF_FROM_NAME", "JAM Morning Brief"),
            "email": from_email,
        },
        "to": [{"email": to_email, "name": os.getenv("BRIEF_TO_NAME", "")}],
        "subject": f"JAM Morning Brief — {datetime.now().strftime('%A, %B %-d')}",
        "htmlContent": html_path.read_text(encoding="utf-8"),
    }

    audio_path = OUTPUT / f"brief-{today}.mp3"
    if audio_path.exists():
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        if size_mb <= 15:
            payload["attachment"] = [{
                "name": audio_path.name,
                "content": base64.b64encode(audio_path.read_bytes()).decode("ascii"),
            }]
        else:
            print(f"Audio is {size_mb:.1f} MB; not attaching to email.")

    r = requests.post(
        BREVO_URL,
        headers={"api-key": api_key, "accept": "application/json", "content-type": "application/json"},
        data=json.dumps(payload),
        timeout=90,
    )
    r.raise_for_status()
    print(f"Brevo sent briefing: {r.json()}")


if __name__ == "__main__":
    main()
