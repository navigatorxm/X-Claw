"""
XClaw Gmail Tools — read, search, and send email via IMAP/SMTP.

This is intentionally lightweight: uses only Python stdlib (imaplib, smtplib).
No OAuth needed — just an App Password.

Setup (Gmail):
  1. Enable 2-step verification: myaccount.google.com/security
  2. Create App Password:        myaccount.google.com/apppasswords
  3. Set in .env:
       SMTP_USER=you@gmail.com
       SMTP_PASS=your_16_char_app_password
       IMAP_HOST=imap.gmail.com   (default)
       SMTP_HOST=smtp.gmail.com   (default)

The LLM can then:
  - gmail_list_inbox(limit)            → recent emails
  - gmail_search(query, limit)         → search inbox
  - gmail_read_email(uid)              → full email body
  - gmail_send(to, subject, body)      → send email
  - gmail_get_identity()               → returns configured email address

Combined with MCP servers, XClaw can use this identity to register/auth
on external services when the user explicitly authorises it.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header

logger = logging.getLogger(__name__)


def _imap_cfg() -> dict:
    return {
        "host": os.getenv("IMAP_HOST", "imap.gmail.com"),
        "port": int(os.getenv("IMAP_PORT", "993")),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
    }


def _smtp_cfg() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
    }


def _decode_header_value(value: str) -> str:
    parts = decode_header(value or "")
    result = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(raw)
    return "".join(result)


def _connect_imap() -> imaplib.IMAP4_SSL:
    cfg = _imap_cfg()
    if not cfg["user"] or not cfg["password"]:
        raise ValueError("SMTP_USER and SMTP_PASS not configured. Run bash scripts/setup.sh")
    conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    conn.login(cfg["user"], cfg["password"])
    return conn


async def gmail_get_identity() -> str:
    """Return the configured Gmail address (or instructions if not set)."""
    user = os.getenv("SMTP_USER", "")
    if user:
        return f"XClaw is operating as: {user}"
    return "No email identity configured. Add SMTP_USER and SMTP_PASS to .env"


async def gmail_list_inbox(limit: int = 10, folder: str = "INBOX") -> str:
    """
    List recent emails from inbox. Returns sender, subject, date for each.
    limit: max emails to return (default 10).
    """
    cfg = _imap_cfg()
    if not cfg["user"]:
        return "Email not configured. Set SMTP_USER and SMTP_PASS in .env"

    def _fetch() -> str:
        conn = _connect_imap()
        conn.select(folder)
        _, data = conn.search(None, "ALL")
        ids = data[0].split()
        if not ids:
            return "Inbox is empty."
        recent = ids[-limit:][::-1]  # newest first
        rows = []
        for uid in recent:
            _, msg_data = conn.fetch(uid, "(ENVELOPE)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1].decode(errors="replace") if isinstance(msg_data[0][1], bytes) else str(msg_data[0][1])
            # Quick parse of envelope
            subject = ""
            sender = ""
            date = ""
            for line in raw.split("\n"):
                ll = line.strip().lower()
                if "subject" in ll:
                    subject = line.strip()[:80]
                elif "from" in ll:
                    sender = line.strip()[:60]
                elif "date" in ll:
                    date = line.strip()[:40]
            rows.append(f"[{uid.decode()}] {date} | {sender} | {subject}")
        conn.logout()
        return "\n".join(rows) if rows else "No emails found."

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        return f"IMAP error: {exc}"


async def gmail_search(query: str, limit: int = 10) -> str:
    """
    Search Gmail inbox. query: Gmail search syntax e.g. 'from:boss@company.com subject:invoice'.
    """
    cfg = _imap_cfg()
    if not cfg["user"]:
        return "Email not configured."

    def _search() -> str:
        conn = _connect_imap()
        conn.select("INBOX")
        # Convert to IMAP search criteria (simplified)
        criteria = "TEXT " + query if not any(k in query for k in ["from:", "subject:", "to:"]) else query.upper().replace(":", " ")
        try:
            _, data = conn.search(None, criteria)
        except Exception:
            _, data = conn.search(None, "TEXT", query)
        ids = data[0].split()
        if not ids:
            conn.logout()
            return f"No emails matching: {query!r}"
        rows = []
        for uid in ids[-limit:][::-1]:
            _, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                continue
            raw = msg_data[0][1].decode(errors="replace")
            rows.append(f"[{uid.decode()}] " + " | ".join(raw.split("\r\n")[:3]))
        conn.logout()
        return "\n".join(rows) if rows else "No matches."

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _search)
    except Exception as exc:
        return f"Search failed: {exc}"


async def gmail_read_email(uid: str) -> str:
    """
    Read the full body of an email by UID. Use gmail_list_inbox() first to get UIDs.
    """
    cfg = _imap_cfg()
    if not cfg["user"]:
        return "Email not configured."

    def _read() -> str:
        conn = _connect_imap()
        conn.select("INBOX")
        _, data = conn.fetch(uid.encode(), "(RFC822)")
        if not data or not data[0]:
            conn.logout()
            return f"Email {uid} not found."
        raw = data[0][1]
        msg = email_lib.message_from_bytes(raw)
        subject = _decode_header_value(msg.get("Subject", ""))
        from_ = _decode_header_value(msg.get("From", ""))
        date = msg.get("Date", "")
        # Get text body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="replace")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="replace")
        conn.logout()
        return f"From: {from_}\nDate: {date}\nSubject: {subject}\n\n{body[:3000]}"

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read)
    except Exception as exc:
        return f"Could not read email {uid}: {exc}"


async def gmail_send(to: str, subject: str, body: str) -> str:
    """
    Send an email from the configured Gmail account.
    to: recipient address. subject: email subject. body: plain text body.
    """
    cfg = _smtp_cfg()
    if not cfg["user"] or not cfg["password"]:
        return "Email not configured. Set SMTP_USER and SMTP_PASS in .env"

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    def _send() -> str:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], [to], msg.as_string())
        return f"Email sent to {to} — Subject: {subject!r}"

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _send)
    except smtplib.SMTPAuthenticationError:
        return "Auth failed. Use an App Password: myaccount.google.com/apppasswords"
    except Exception as exc:
        return f"Send failed: {exc}"
