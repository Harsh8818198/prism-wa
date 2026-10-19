"""The live side of a form: what Meta asks Prism while a customer is filling one in.

Meta sends each request encrypted — an AES key sealed with the workspace's public key (RSA-OAEP, SHA-256), and the body sealed with that AES key (AES-GCM) — and expects the
answer sealed with the same AES key and the initial vector with every bit flipped. A request that cannot be decrypted is answered 421, which tells Meta to fetch the public key
again. Every request also carries X-Hub-Signature-256 (the app secret), which is checked first: the public key is public, so anyone could encrypt to it.

What the form asks us (the `action`):
  ping           "are you alive?"  -> {"data": {"status": "active"}}
  INIT           the form opened   -> the first screen, with the days a person may pick
  data_exchange  a day was picked  -> the same screen with the times still free that day
                 the form was sent -> checked against the free times, kept, and the thank-you screen
  BACK           (not used)        -> the screen again
An error Meta reports back (`data.error`) is acknowledged and logged.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from prism_wa import forms
from prism_wa.jobs import utcnow
from prism_wa.signature import verify_signature
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.form_endpoint")


class EndpointError(Exception):
    """`status` is what Meta is told: 421 = decrypt failed (refetch the key), 432 = bad signature, 400/404 otherwise."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status, self.reason = status, reason


# ── the crypto ───────────────────────────────────────────────────────────────

def decrypt_request(private_pem: str, body: dict) -> tuple:
    """-> (request dict, aes key, initial vector). Raises EndpointError(421) when anything about the envelope is wrong."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    try:
        key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
        aes_key = key.decrypt(base64.b64decode(body["encrypted_aes_key"]), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        iv = base64.b64decode(body["initial_vector"])
        sealed = base64.b64decode(body["encrypted_flow_data"])
        if len(sealed) < 17 or len(iv) < 12:
            raise ValueError("too short")
        decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv, sealed[-16:])).decryptor()
        plain = decryptor.update(sealed[:-16]) + decryptor.finalize()
        return json.loads(plain.decode("utf-8")), aes_key, iv
    except Exception as why:                                          # noqa: BLE001  (any failure to open the envelope is the same answer)
        raise EndpointError(421, "decrypt_failed", "the request could not be decrypted: %s" % why.__class__.__name__) from None


def encrypt_response(response: dict, aes_key: bytes, iv: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    flipped = bytes(b ^ 0xFF for b in iv)
    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(flipped)).encryptor()
    sealed = encryptor.update(json.dumps(response).encode("utf-8")) + encryptor.finalize() + encryptor.tag
    return base64.b64encode(sealed).decode("ascii")


# ── free times ───────────────────────────────────────────────────────────────

def _on_date(value) -> Optional[date]:
    """A picked day arrives as "2026-10-21" (or, from older clients, as milliseconds since 1970)."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text) / (1000 if len(text) >= 13 else 1), tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _clock(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def window(a: dict, now: Optional[datetime] = None) -> tuple:
    """(first day, last day) a person may pick, as ISO strings, in the form's own time zone."""
    now = (now or utcnow()).astimezone(ZoneInfo(a["tz"]))
    first = (now + timedelta(hours=a["min_notice_hours"])).date()
    return first.isoformat(), (first + timedelta(days=a["window_days"])).isoformat()


def slots_for(cur, tenant_id, form: dict, day, *, now: Optional[datetime] = None) -> list:
    """The times still free on `day`: [{"id": "10:00", "title": "10:00 AM"}]. Empty on a day the business is closed, in the past, or full."""
    a = (form["settings"] or {}).get("availability")
    d = _on_date(day)
    if not a or d is None:
        return []
    tz = ZoneInfo(a["tz"])
    now = (now or utcnow()).astimezone(tz)
    earliest = now + timedelta(hours=a["min_notice_hours"])
    if d < earliest.date() or d > (earliest + timedelta(days=a["window_days"])).date() or d.weekday() not in a["weekdays"]:
        return []
    cur.execute("select r.answers->>%s, count(*) from form_responses r where r.form_id = %s and r.tenant_id = %s and r.answers->>%s = %s group by 1",
                (a["slot_field"], form["id"], tenant_id, a["date_field"], d.isoformat()))
    taken = {slot: n for slot, n in cur.fetchall()}
    out = []
    start = datetime.combine(d, datetime.min.time(), tzinfo=tz).replace(hour=int(a["start"][:2]), minute=int(a["start"][3:5]))
    end = start.replace(hour=int(a["end"][:2]), minute=int(a["end"][3:5]))
    t = start
    while t + timedelta(minutes=a["slot_minutes"]) <= end:
        key = t.strftime("%H:%M")
        if t >= earliest and taken.get(key, 0) < a["capacity"]:
            out.append({"id": key, "title": _clock(key)})
        t += timedelta(minutes=a["slot_minutes"])
    return out[:200]


# ── answering ────────────────────────────────────────────────────────────────

def _first_screen(form: dict, now) -> dict:
    s = form["screens"][0]
    data = {}
    a = (form["settings"] or {}).get("availability")
    if any(f.get("live") == "date" for f in s["fields"]) and a:
        data["min_date"], data["max_date"] = window(a, now)
    if any(f.get("live") == "slots" for f in s["fields"]):
        data["slots"] = []
    data.update({"has_error": False, "error_message": ""})
    return {"screen": s["id"], "data": data}


def respond(cur, tenant_id, form: dict, request: dict, *, now: Optional[datetime] = None) -> dict:
    """The answer to one decrypted request for one form."""
    now = now or utcnow()
    action = str(request.get("action") or "")
    data = request.get("data") if isinstance(request.get("data"), dict) else {}
    if action == "ping":
        return {"data": {"status": "active"}}
    if data.get("error"):                                             # Meta tells us something broke on the phone
        log.warning("form %s reported an error: %s %s", form["id"], data.get("error"), data.get("error_message"))
        return {"data": {"acknowledged": True}}
    if action in ("INIT", "BACK"):
        return _first_screen(form, now)
    if action != "data_exchange":
        raise EndpointError(400, "bad_action", "unknown action %r" % action)
    a = (form["settings"] or {}).get("availability")
    current = str(request.get("screen") or form["screens"][0]["id"])
    step = str(data.get("step") or "")
    if step == "date":
        base = _first_screen(form, now)
        base["screen"] = current
        base["data"]["slots"] = slots_for(cur, tenant_id, form, data.get("date"), now=now)
        if not base["data"]["slots"]:
            base["data"]["has_error"], base["data"]["error_message"] = True, "No times are free on that day. Please pick another day."
        return base
    # the form was sent
    answers = {k: v for k, v in data.items() if k != "step"}
    missing = [f["label"] for s in form["screens"] for f in s["fields"] if f["required"] and answers.get(f["name"]) in (None, "", [])]
    again = _first_screen(form, now)
    again["screen"] = current
    if missing:
        again["data"]["has_error"], again["data"]["error_message"] = True, "Please fill in: %s." % ", ".join(missing[:4])
        return again
    if a:
        day, slot = answers.get(a["date_field"]), answers.get(a["slot_field"])
        free = {s["id"] for s in slots_for(cur, tenant_id, form, day, now=now)}
        d = _on_date(day)
        if d is not None:
            answers[a["date_field"]] = d.isoformat()
        if str(slot) not in free:
            again["data"]["slots"] = slots_for(cur, tenant_id, form, day, now=now)
            again["data"]["has_error"], again["data"]["error_message"] = True, "Sorry, that time was just taken. Please choose another."
            return again
    token = str(request.get("flow_token") or "")
    form_id, contact_id = forms.read_token(token)
    cur.execute("select c.id from conversations c where c.contact_id = %s and c.tenant_id = %s and c.channel = 'whatsapp'", (contact_id, tenant_id)) if contact_id else None
    conv = cur.fetchone() if contact_id else None
    forms.record_response(cur, tenant_id, form_id=form["id"], contact_id=contact_id, conversation_id=conv[0] if conv else None, flow_token=token, answers=answers, now=now)
    params = {"flow_token": token}
    params.update({k: (v if isinstance(v, (str, int, float, bool)) else json.dumps(v)) for k, v in answers.items()})
    return {"screen": "SUCCESS", "data": {"extension_message_response": {"params": params}}}


def handle(cur, tenant_id, form_id, vault: Vault, raw_body: bytes, signature: str, app_secrets, *, now: Optional[datetime] = None) -> str:
    """One call from Meta -> the encrypted answer to send back (text). Raises EndpointError with the status Meta must be given."""
    if not verify_signature(app_secrets, raw_body, signature):
        raise EndpointError(432, "bad_signature", "the signature does not match")
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except ValueError:
        raise EndpointError(400, "bad_body", "the request is not JSON") from None
    pem = forms.private_key(cur, tenant_id, vault)
    if pem is None:
        raise EndpointError(421, "no_key", "this workspace has no form key yet")
    request, aes_key, iv = decrypt_request(pem, body if isinstance(body, dict) else {})
    try:
        form = forms.get_form(cur, tenant_id, form_id)
    except forms.FormError:
        if request.get("action") == "ping":                           # Meta's health check does not belong to one form
            return encrypt_response({"data": {"status": "active"}}, aes_key, iv)
        raise EndpointError(404, "unknown_form", "that form is not in this workspace") from None
    return encrypt_response(respond(cur, tenant_id, form, request, now=now), aes_key, iv)
