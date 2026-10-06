"""Transactional email.

Two transports, picked automatically in this order:

  1. HTTPS API  - set BREVO_API_KEY. Works everywhere, including hosts that firewall
     outbound SMTP (Railway Free/Trial/Hobby block ports 25/465/587/2525).
  2. SMTP       - set SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD.

Both need a sender address in MAIL_FROM (or the older SMTP_FROM), e.g.
"BrandBatch <no-reply@yourdomain.com>". With neither transport configured the message is
written to the log instead, so the app still runs locally and in tests; `transport()` lets
the UI and scripts say which path is live.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import parseaddr

import requests

log = logging.getLogger("brandbatch.mail")

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


def _from_address() -> tuple[str, str]:
    """('Display Name', 'user@host') from MAIL_FROM / SMTP_FROM; email is '' if unset."""
    name, email = parseaddr(os.environ.get("MAIL_FROM") or os.environ.get("SMTP_FROM", ""))
    return name or "BrandBatch", email


def _api_key() -> str | None:
    return os.environ.get("BREVO_API_KEY") or None


def transport() -> str:
    """What send() will actually do: 'api', 'smtp' or 'log'."""
    if not _from_address()[1]:
        return "log"
    if _api_key():
        return "api"
    if os.environ.get("SMTP_HOST"):
        return "smtp"
    return "log"


def configured() -> bool:
    return transport() != "log"


def _send_via_api(to: str, subject: str, body: str):
    name, email = _from_address()
    r = requests.post(
        BREVO_ENDPOINT,
        headers={"api-key": _api_key() or "", "accept": "application/json",
                 "content-type": "application/json"},
        json={"sender": {"name": name, "email": email},
              "to": [{"email": to}],
              "subject": subject,
              "textContent": body},
        timeout=20,
    )
    if r.status_code == 401:
        raise RuntimeError("Brevo rejected the API key (401). Check BREVO_API_KEY.")
    if r.status_code >= 300:
        # Brevo explains refusals in the body; the most common is an unverified sender.
        raise RuntimeError(f"Brevo API returned {r.status_code}: {r.text[:300]}")


def _send_via_smtp(to: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_FROM"]
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


def _send_now(to: str, subject: str, body: str):
    if transport() == "api":
        _send_via_api(to, subject, body)
    else:
        _send_via_smtp(to, subject, body)


def send(to: str, subject: str, body: str, background: bool = True) -> bool:
    """Returns True if the mail was handed to a transport, False if it was only logged."""
    mode = transport()
    if mode == "log":
        log.warning("email not configured; message to %s not sent. Subject: %s\n%s", to, subject, body)
        return False

    def run():
        try:
            _send_now(to, subject, body)
            log.info("sent %r to %s via %s", subject, to, mode)
        except (TimeoutError, OSError) as exc:
            if mode == "smtp":
                log.error("could not send email to %s over SMTP: %s. Many hosts (Railway "
                          "Free/Trial/Hobby among them) block outbound SMTP ports - set "
                          "BREVO_API_KEY to send over HTTPS instead.", to, exc)
            else:
                log.error("could not send email to %s: %s", to, exc)
        except Exception as exc:
            log.error("could not send email to %s: %s", to, exc)

    if background:
        threading.Thread(target=run, daemon=True, name="mailer").start()
    else:
        run()
    return True
