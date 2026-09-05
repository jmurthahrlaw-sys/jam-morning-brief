import mimetypes
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"


def main():
    smtp_user = os.getenv("IMAP_USER", "").strip()
    smtp_password = os.getenv("IMAP_APP_PASSWORD", "").replace(" ", "").strip()
    to_email = os.getenv("BRIEF_TO_EMAIL", "").strip()
    to_name = os.getenv("BRIEF_TO_NAME", "").strip()
    from_name = os.getenv("BRIEF_FROM_NAME", "JAM Morning Brief").strip() or "JAM Morning Brief"

    if not smtp_user or not smtp_password or not to_email:
        print("Gmail email settings are incomplete; briefing files were created but email was not sent.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    html_path = OUTPUT / f"brief-{today}.html"
    text_path = OUTPUT / f"brief-{today}.txt"
    if not html_path.exists():
        raise RuntimeError("HTML brief not found. Run daily_digest.py first.")

    msg = EmailMessage()
    msg["Subject"] = f"JAM Morning Brief — {datetime.now().strftime('%A, %B %-d')}"
    msg["From"] = f"{from_name} <{smtp_user}>"
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email

    if text_path.exists():
        msg.set_content(text_path.read_text(encoding="utf-8"))
    else:
        msg.set_content("Your JAM Morning Brief is included in the HTML version of this message.")

    msg.add_alternative(html_path.read_text(encoding="utf-8"), subtype="html")

    # If audio has been generated, attach it when reasonably sized for email.
    audio_path = OUTPUT / f"brief-{today}.mp3"
    if audio_path.exists():
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        if size_mb <= 18:
            ctype, _ = mimetypes.guess_type(audio_path.name)
            maintype, subtype = (ctype or "audio/mpeg").split("/", 1)
            msg.add_attachment(
                audio_path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=audio_path.name,
            )
            print(f"Attached audio ({size_mb:.1f} MB).")
        else:
            print(f"Audio is {size_mb:.1f} MB; not attaching to email.")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=90) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    print(f"Gmail sent JAM Morning Brief to {to_email} from {smtp_user}.")


if __name__ == "__main__":
    main()
