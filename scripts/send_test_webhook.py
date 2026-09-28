#!/usr/bin/env python3
"""Send a correctly signed Razorpay-style webhook to your own app.

Razorpay can't reach http://127.0.0.1, so this lets you test webhook handling locally:
it signs the payload exactly the way Razorpay does and posts it to your app.

  python scripts/send_test_webhook.py --user-email you@example.com --event subscription.activated --plan agency

Options:
  --url     app base URL (default http://127.0.0.1:8000)
  --secret  webhook secret (default: RAZORPAY_WEBHOOK_SECRET from the environment)
  --sub     subscription id to use (default sub_test_local)
  --bad-signature   send a wrong signature; the app must answer 400
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

EVENTS = ["subscription.activated", "subscription.charged", "subscription.pending",
          "subscription.halted", "subscription.cancelled", "subscription.completed"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Post a signed test webhook to BrandBatch")
    p.add_argument("--url", default=os.environ.get("APP_URL", "http://127.0.0.1:8000"))
    p.add_argument("--secret", default=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""))
    p.add_argument("--event", default="subscription.activated", choices=EVENTS)
    p.add_argument("--plan", default="agency", help="plan key from app/plans.py")
    p.add_argument("--sub", default="sub_test_local")
    p.add_argument("--user-id", help="user id (32 hex chars)")
    p.add_argument("--user-email", help="look the user id up in the database by email")
    p.add_argument("--bad-signature", action="store_true")
    a = p.parse_args(argv)

    if not a.secret:
        print("No webhook secret. Pass --secret or set RAZORPAY_WEBHOOK_SECRET.", file=sys.stderr)
        return 2

    user_id = a.user_id
    if not user_id and a.user_email:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from sqlalchemy import select
        from app.models import SessionLocal, User, init_engine
        init_engine(os.environ.get("DATABASE_URL", "sqlite:///brandbatch.db"))
        user = SessionLocal().scalar(select(User).where(User.email == a.user_email.strip().lower()))
        if not user:
            print(f"No user with email {a.user_email}", file=sys.stderr)
            return 1
        user_id = user.id
    if not user_id:
        print("Pass --user-id or --user-email.", file=sys.stderr)
        return 2

    body = json.dumps({
        "event": a.event,
        "created_at": int(time.time()),
        "payload": {"subscription": {"entity": {
            "id": a.sub, "status": "active",
            "notes": {"user_id": user_id, "plan": a.plan},
        }}},
    }).encode()
    signature = hmac.new(a.secret.encode(), body, hashlib.sha256).hexdigest()
    if a.bad_signature:
        signature = "0" * len(signature)

    req = urllib.request.Request(
        a.url.rstrip("/") + "/billing/webhook", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature,
                 "X-Razorpay-Event-Id": f"test-{a.event}-{int(time.time() * 1000)}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"{resp.status} {resp.read().decode()[:200]}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"{exc.code} {exc.read().decode()[:200]}")
        return 0 if a.bad_signature and exc.code == 400 else 1
    except urllib.error.URLError as exc:
        print(f"Could not reach {a.url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
