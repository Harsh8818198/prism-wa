"""Razorpay, as far as taking a payment from a WhatsApp chat needs it: a payment link, its state, a refund, and the check on a webhook.

Why Razorpay links: they work today with only a Razorpay account (no Meta payments approval), and the order, the payment record and the refund are the same either way. The
gateway keeps the money and the card or UPI details; Prism sees an amount, a state and an id. Amounts here are whole paise, which is what Razorpay takes.

The transport is the same shape as Graph's — `(method, url, body_bytes, headers) -> (status, bytes)` — so a test hands in a fake and nothing opens a socket.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Callable, Optional

from prism_wa.graph import urllib_transport

BASE = "https://api.razorpay.com/v1"
LINK_STATES = {"created": "pending", "partially_paid": "pending", "paid": "captured", "cancelled": "cancelled", "expired": "expired"}


class RazorpayError(Exception):
    def __init__(self, message: str, *, code: str = "", http_status: Optional[int] = None):
        super().__init__(message)
        self.code, self.http_status = code, http_status


class Razorpay:
    def __init__(self, key_id: str, key_secret: str, transport: Optional[Callable] = None, base: str = BASE):
        self.key_id, self.key_secret = key_id, key_secret
        self.transport = transport or urllib_transport
        self.base = base.rstrip("/")

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        auth = base64.b64encode(("%s:%s" % (self.key_id, self.key_secret)).encode("utf-8")).decode("ascii")
        headers = {"Accept": "application/json", "Authorization": "Basic " + auth}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, "%s/%s" % (self.base, path.lstrip("/")), data, headers)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {}
        if status >= 400 or (isinstance(parsed, dict) and isinstance(parsed.get("error"), dict)):
            err = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else {}
            raise RazorpayError(str(err.get("description") or "Razorpay answered HTTP %d" % status), code=str(err.get("code") or ""), http_status=status)
        return parsed if isinstance(parsed, dict) else {}

    def create_link(self, *, amount_paise: int, currency: str, reference_id: str, description: str, name: str = "", phone: str = "",
                    expire_by: Optional[int] = None, callback_url: str = "") -> dict:
        body = {"amount": int(amount_paise), "currency": currency, "accept_partial": False, "reference_id": reference_id[:40], "description": description[:2000],
                "reminder_enable": True, "notify": {"sms": False, "email": False}, "notes": {"prism_reference": reference_id}}
        customer = {k: v for k, v in (("name", name[:100]), ("contact", ("+" + phone.lstrip("+")) if phone else "")) if v}
        if customer:
            body["customer"] = customer
        if expire_by:
            body["expire_by"] = int(expire_by)
        if callback_url:
            body["callback_url"], body["callback_method"] = callback_url, "get"
        out = self._call("POST", "payment_links", body)
        if not out.get("id") or not out.get("short_url"):
            raise RazorpayError("Razorpay did not return a payment link")
        return out

    def get_link(self, link_id: str) -> dict:
        return self._call("GET", "payment_links/%s" % link_id)

    def cancel_link(self, link_id: str) -> dict:
        return self._call("POST", "payment_links/%s/cancel" % link_id, {})

    def refund(self, payment_id: str, amount_paise: int, speed: str = "normal", notes: Optional[dict] = None) -> dict:
        """`speed` is 'normal' or 'instant' (Razorpay calls the second one 'optimum': it sends instantly when it can)."""
        return self._call("POST", "payments/%s/refund" % payment_id, {"amount": int(amount_paise), "speed": "optimum" if speed == "instant" else "normal", "notes": notes or {}})

    def get_payment(self, payment_id: str) -> dict:
        return self._call("GET", "payments/%s" % payment_id)


def link_state(link: dict) -> str:
    return LINK_STATES.get(str(link.get("status") or ""), "pending")


def payment_id_of(link: dict) -> str:
    """The payment that paid a link, when it is paid."""
    for p in link.get("payments") or []:
        if isinstance(p, dict) and p.get("payment_id") and str(p.get("status")) in ("captured", "authorized", "paid"):
            return str(p["payment_id"])
    for p in link.get("payments") or []:
        if isinstance(p, dict) and p.get("payment_id"):
            return str(p["payment_id"])
    return ""


def verify_signature(secret: str, body: bytes, header: Optional[str]) -> bool:
    """Razorpay signs a webhook's raw body with HMAC-SHA256 and the webhook secret, in X-Razorpay-Signature (hex)."""
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())
