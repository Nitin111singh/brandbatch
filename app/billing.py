"""Razorpay Subscriptions integration.

Flow: user picks a plan -> we create a Razorpay subscription (API) -> Checkout opens ->
on success the browser posts payment_id/subscription_id/signature to /billing/verify (HMAC checked)
-> plan activated. Webhooks keep status in sync (renewals, cancellations, failures).
"""
from __future__ import annotations

import hashlib
import hmac
import os

import requests

API = "https://api.razorpay.com/v1"


def key_id() -> str | None:
    return os.environ.get("RAZORPAY_KEY_ID") or None


def key_secret() -> str | None:
    return os.environ.get("RAZORPAY_KEY_SECRET") or None


def webhook_secret() -> str | None:
    return os.environ.get("RAZORPAY_WEBHOOK_SECRET") or None


def configured() -> bool:
    return bool(key_id() and key_secret())


class BillingError(Exception):
    pass


class AuthError(BillingError):
    """Razorpay rejected the API keys."""


def _hmac(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_payment_signature(payment_id: str, subscription_id: str, signature: str) -> bool:
    secret = key_secret()
    if not (secret and payment_id and subscription_id and signature):
        return False
    expected = _hmac(secret, f"{payment_id}|{subscription_id}".encode())
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = webhook_secret()
    if not (secret and signature):
        return False
    return hmac.compare_digest(_hmac(secret, body), signature)


def create_order(amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    """Create a one-time order (Standard Checkout). Amount is in paise; Razorpay's minimum is 100."""
    if amount_paise < 100:
        raise BillingError("Amount must be at least 100 paise (Rs 1).")
    try:
        r = requests.post(f"{API}/orders", auth=(key_id(), key_secret()), timeout=20, json={
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": receipt[:40],
            "notes": notes or {},
        })
    except requests.RequestException as exc:
        raise BillingError(f"Could not reach Razorpay: {exc}")
    if r.status_code in (401, 403):
        raise AuthError("Razorpay rejected the API credentials.")
    if r.status_code >= 300:
        raise BillingError(f"Razorpay error: {r.text[:300]}")
    return r.json()


def verify_order_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Standard Checkout: HMAC-SHA256(order_id + '|' + payment_id) with the key secret."""
    secret = key_secret()
    if not (secret and order_id and payment_id and signature):
        return False
    expected = _hmac(secret, f"{order_id}|{payment_id}".encode())
    return hmac.compare_digest(expected, signature)


def create_subscription(plan_id: str, user_id: str, plan_key: str, email: str) -> dict:
    try:
        r = requests.post(f"{API}/subscriptions", auth=(key_id(), key_secret()), timeout=20, json={
            "plan_id": plan_id,
            "total_count": 120,          # monthly for up to 10 years; user can cancel anytime
            "customer_notify": 1,
            "notes": {"user_id": user_id, "plan": plan_key, "email": email},
        })
    except requests.RequestException as exc:
        raise BillingError(f"Could not reach Razorpay: {exc}")
    if r.status_code >= 300:
        raise BillingError(f"Razorpay error: {r.text[:300]}")
    return r.json()


def cancel_subscription(subscription_id: str, at_cycle_end: bool = True) -> dict:
    try:
        r = requests.post(f"{API}/subscriptions/{subscription_id}/cancel", auth=(key_id(), key_secret()),
                          timeout=20, json={"cancel_at_cycle_end": 1 if at_cycle_end else 0})
    except requests.RequestException as exc:
        raise BillingError(f"Could not reach Razorpay: {exc}")
    if r.status_code >= 300:
        raise BillingError(f"Razorpay error: {r.text[:300]}")
    return r.json()
