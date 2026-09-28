"""Transactional email over SMTP.

Configured with SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM.
When SMTP isn't configured the message is written to the log instead, so the app still runs
locally and in tests; `configured()` lets the UI tell the user what to expect.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger("brandbatch.mail")


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def _send_now(to: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        if port != 465:
            try:
                server.starttls()
            except smtplib.SMTPException:
                log.warning("SMTP server did not accept STARTTLS")
        if user:
            server.login(user, password or "")
        server.send_message(msg)


def send(to: str, subject: str, body: str, background: bool = True) -> bool:
    """Returns True if the mail was handed to SMTP, False if it was only logged."""
    if not configured():
        log.warning("SMTP not configured; email to %s not sent. Subject: %s\n%s", to, subject, body)
        return False

    def run():
        try:
            _send_now(to, subject, body)
            log.info("sent %r to %s", subject, to)
        except Exception as exc:
            log.error("could not send email to %s: %s", to, exc)

    if background:
        threading.Thread(target=run, daemon=True, name="mailer").start()
    else:
        run()
    return True
