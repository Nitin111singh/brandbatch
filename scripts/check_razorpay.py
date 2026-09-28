#!/usr/bin/env python3
"""Check that the Razorpay keys in .env work, by creating a real test-mode order.

  python scripts/check_razorpay.py            # creates a Rs 299 test order
  python scripts/check_razorpay.py --amount 100

Test-mode orders cost nothing and expire on their own.
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

from app import billing  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify Razorpay credentials")
    p.add_argument("--amount", type=int, default=29900, help="amount in paise (default 29900 = Rs 299)")
    a = p.parse_args(argv)

    key = billing.key_id() or ""
    if not billing.configured():
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. Check your .env file.", file=sys.stderr)
        return 2
    print(f"Key id : {key}  ({'TEST mode' if key.startswith('rzp_test') else 'LIVE mode'})")

    try:
        order = billing.create_order(a.amount, receipt="credential-check", notes={"purpose": "check"})
    except billing.AuthError:
        print("FAILED: Razorpay rejected these credentials (401). Re-copy the key id and secret.", file=sys.stderr)
        return 1
    except billing.BillingError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"OK     : order {order['id']} for {order['amount'] / 100:.2f} {order.get('currency', 'INR')} "
          f"(status {order.get('status')})")
    print("Credentials work. The order appears in your Razorpay dashboard under Transactions > Orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
