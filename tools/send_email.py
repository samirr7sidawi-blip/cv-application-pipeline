"""
Send the tailored CV PDF and cover letter to the user.

Two backends, picked by env vars:
- RESEND_API_KEY set         → send via Resend HTTP API (works on hosts that
                               block outbound SMTP, e.g. DigitalOcean droplets)
- otherwise, GMAIL_EMAIL +    → send via Gmail SMTP (local Mac fallback)
  GMAIL_APP_PASSWORD

Usage:
    python3 tools/send_email.py

Requires (one of):
    RESEND_API_KEY in .env (preferred for cloud)
    GMAIL_EMAIL + GMAIL_APP_PASSWORD in .env (local SMTP)

Optional:
    RESEND_FROM      sender address. Defaults to onboarding@resend.dev
                     (Resend's sandbox sender — works without domain verification
                     but you can only send to your own verified address)
    EMAIL_RECIPIENT  recipient. Defaults to GMAIL_EMAIL.

Input:
    .tmp/tailored_cv.pdf
    .tmp/cover_letter_final.txt
    .tmp/job_description.json
"""

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from dotenv import load_dotenv

load_dotenv()

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "onboarding@resend.dev")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", GMAIL_EMAIL)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def send_via_resend(subject: str, body: str, recipient: str, attachments: list[tuple[str, bytes]]) -> None:
    import resend
    resend.api_key = RESEND_API_KEY
    params = {
        "from": RESEND_FROM,
        "to": [recipient],
        "subject": subject,
        "text": body,
        "attachments": [
            {"filename": name, "content": list(content)}
            for name, content in attachments
        ],
    }
    resend.Emails.send(params)


def send_via_smtp(subject: str, body: str, recipient: str, attachments: list[tuple[str, bytes]]) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = GMAIL_EMAIL
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for name, content in attachments:
        part = MIMEApplication(content, Name=name)
        part["Content-Disposition"] = f'attachment; filename="{name}"'
        msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    if not RESEND_API_KEY and (not GMAIL_EMAIL or not GMAIL_APP_PASSWORD):
        print("Error: set RESEND_API_KEY (preferred) or GMAIL_EMAIL + GMAIL_APP_PASSWORD in .env")
        raise SystemExit(1)

    if not EMAIL_RECIPIENT:
        print("Error: EMAIL_RECIPIENT not set (GMAIL_EMAIL missing as fallback)")
        raise SystemExit(1)

    if not os.path.exists(".tmp/tailored_cv.pdf"):
        print("Error: .tmp/tailored_cv.pdf not found. Run update_cv_flowcv.py first.")
        raise SystemExit(1)

    if not os.path.exists(".tmp/cover_letter_final.txt"):
        print("Error: .tmp/cover_letter_final.txt not found. Run generate_cover_letter.py first.")
        raise SystemExit(1)

    job = load_json(".tmp/job_description.json")
    cover_letter = load_text(".tmp/cover_letter_final.txt")

    casual_path = ".tmp/cover_letter_casual.txt"
    casual_letter = load_text(casual_path) if os.path.exists(casual_path) else ""

    title = job.get("title", "Unknown Role")
    company = job.get("company", "Unknown Company")
    apply_url = job.get("apply_url", "")

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()
    pdf_filename = f"CV_{safe_title.replace(' ', '_')}.pdf"

    subject = f"Application: {title} at {company}"

    body_parts = [
        "=== FORMAL COVER LETTER ===",
        "",
        cover_letter,
    ]
    if casual_letter:
        body_parts += [
            "",
            "",
            "=== CASUAL COVER LETTER (alternative) ===",
            "",
            casual_letter,
        ]
    body_parts += [
        "",
        "---",
        f"Job: {title} at {company}",
        f"Apply URL: {apply_url}",
        "",
    ]
    body = "\n".join(body_parts)

    with open(".tmp/tailored_cv.pdf", "rb") as f:
        pdf_bytes = f.read()
    attachments = [
        (pdf_filename, pdf_bytes),
        ("cover_letter_formal.txt", cover_letter.encode("utf-8")),
    ]
    if casual_letter:
        attachments.append(("cover_letter_casual.txt", casual_letter.encode("utf-8")))

    if RESEND_API_KEY:
        print(f"Sending email to {EMAIL_RECIPIENT} via Resend (from {RESEND_FROM})...")
        send_via_resend(subject, body, EMAIL_RECIPIENT, attachments)
    else:
        print(f"Sending email to {EMAIL_RECIPIENT} via Gmail SMTP (from {GMAIL_EMAIL})...")
        send_via_smtp(subject, body, EMAIL_RECIPIENT, attachments)

    print(f"Email sent: '{subject}'")
    print(f"  To:         {EMAIL_RECIPIENT}")
    print(f"  Attachment: {pdf_filename}")


if __name__ == "__main__":
    main()
