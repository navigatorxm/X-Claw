"""
XClaw Communication Tools — email sending via stdlib SMTP (no extra deps).

Tools:
  send_email   — send email via SMTP (Gmail, Outlook, custom SMTP)
  draft_email  — create a formatted email draft (no sending)

Configuration (via .env):
  SMTP_HOST    — e.g. smtp.gmail.com
  SMTP_PORT    — e.g. 587
  SMTP_USER    — your email address
  SMTP_PASS    — app password (Gmail: generate at myaccount.google.com/apppasswords)
  SMTP_FROM    — display name + address, e.g. "XClaw <you@gmail.com>"
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _smtp_cfg() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
        "from_addr": os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
    }


async def send_email(to: str, subject: str, body: str, html: bool = False) -> str:
    """
    Send an email. Requires SMTP_HOST, SMTP_USER, SMTP_PASS in environment.
    to: recipient email address. body: plain text or HTML content.
    Returns confirmation or error message.
    """
    cfg = _smtp_cfg()
    if not cfg["user"] or not cfg["password"]:
        return (
            "Email not configured. Set SMTP_USER and SMTP_PASS in your .env file.\n"
            "For Gmail: use an App Password from myaccount.google.com/apppasswords"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"] or cfg["user"]
    msg["To"] = to

    mime_type = "html" if html else "plain"
    msg.attach(MIMEText(body, mime_type, "utf-8"))

    try:
        import asyncio
        loop = asyncio.get_event_loop()

        def _send() -> str:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["user"], cfg["password"])
                server.sendmail(cfg["user"], [to], msg.as_string())
            return f"Email sent to {to} — Subject: {subject!r}"

        result = await loop.run_in_executor(None, _send)
        logger.info("[email] sent to %s", to)
        return result

    except smtplib.SMTPAuthenticationError:
        return (
            "SMTP authentication failed. Check SMTP_USER and SMTP_PASS.\n"
            "Gmail users: use an App Password, not your regular password."
        )
    except smtplib.SMTPException as exc:
        return f"SMTP error sending to {to}: {exc}"
    except Exception as exc:
        return f"Email send failed: {exc}"


async def draft_email(to: str, subject: str, body: str) -> str:
    """
    Create a formatted email draft (does NOT send). Returns the full email as text.
    Useful for reviewing before sending, or when SMTP is not configured.
    """
    cfg = _smtp_cfg()
    from_addr = cfg["from_addr"] or cfg["user"] or "you@example.com"
    draft = (
        f"📧 **Email Draft**\n\n"
        f"**From:** {from_addr}\n"
        f"**To:** {to}\n"
        f"**Subject:** {subject}\n"
        f"{'─' * 40}\n\n"
        f"{body}"
    )
    return draft
