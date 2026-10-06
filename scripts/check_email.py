#!/usr/bin/env python3
"""Check that outgoing email works, by sending a real message.

  python scripts/check_email.py you@example.com

Reports which transport is configured (HTTPS API or SMTP) and prints the exact
failure if the send is refused, instead of leaving it in the server log.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app import mailer  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Send a test email and report what happened")
    p.add_argument("to", help="address to send the test message to")
    a = p.parse_args(argv)

    mode = mailer.transport()
    name, sender = mailer._from_address()
    print(f"Transport : {mode}")
    print(f"From      : {name} <{sender or '(not set)'}>")

    if mode == "log":
        print("\nNo transport configured. Set MAIL_FROM plus either BREVO_API_KEY (HTTPS, works "
              "everywhere) or SMTP_HOST (blocked on many hosts).", file=sys.stderr)
        return 2
    if mode == "smtp":
        print(f"SMTP host : {os.environ.get('SMTP_HOST')}:{os.environ.get('SMTP_PORT', 587)}")
        print("Note: Railway Free/Trial/Hobby block outbound SMTP. If this times out, that's why.")

    try:
        mailer._send_now(a.to, "BrandBatch test email",
                         "If you're reading this, outgoing email works.\n")
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1

    print(f"\nOK        : message accepted for {a.to}. Check the inbox, and spam.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
