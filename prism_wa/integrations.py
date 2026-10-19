"""Shopify and WooCommerce: a store event becomes a WhatsApp message (spec WS14).

A connected store sends webhooks to `/hooks/<kind>/<token>`, signed with a secret only the store and
this service know. Each is checked, recorded once (a retry from the store is a no-op), and — if the
workspace set that event up — turned into a template message:

    order_created      "thanks, your order #1001 is confirmed"
    order_fulfilled    "your order shipped, tracking ..."   (WooCommerce: an order marked completed)
    abandoned_cart     a checkout started but never finished, reminded after a delay — unless an
                       order for it arrives first                                      (Shopify)

Variables are filled from a small normalised view of the order, by dotted path:

    customer.first_name  customer.last_name  customer.name  customer.phone  customer.email
    order.id  order.name  order.total  order.currency  order.status_url
    tracking.number  tracking.url  tracking.company
    checkout.id  checkout.url  checkout.total

(`"=text"` is a fixed string. A missing value becomes "-", because Meta rejects an empty parameter.)

Consent. A store event is business-initiated messaging, so the contact must be opted in. A workspace
that collects consent at checkout turns on `consent_attested`; each message then records an opt-in in the
consent ledger with the order or checkout as evidence. Without it, only contacts already opted in are
messaged. An opted-out contact is never messaged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Optional

from psycopg2.extras import Json

from prism_wa import consent, contacts, jobs, plans, send
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

KINDS = ("shopify", "woocommerce")
EVENTS = ("order_created", "order_fulfilled", "abandoned_cart")
DEFAULT_DELAY_MINUTES = 60
SECRET_MIN = 8


class IntegrationError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# ── configuration ────────────────────────────────────────────────────────────

def validate_config(config) -> dict:
    if not isinstance(config, dict):
        raise IntegrationError("bad_config", "the configuration must be an object")
    events = config.get("events") or {}
    if not isinstance(events, dict):
        raise IntegrationError("bad_config", "`events` must be an object")
    clean = {}
    for key, spec in events.items():
        if key not in EVENTS:
            raise IntegrationError("bad_config", "unknown event %r (use %s)" % (key, ", ".join(EVENTS)))
        if not isinstance(spec, dict) or not str(spec.get("template") or "").strip():
            raise IntegrationError("bad_config", "%s needs a template name" % key)
        variables = spec.get("variables") or {}
        if not isinstance(variables, dict) or not all(str(k).isdigit() and int(k) >= 1 and isinstance(v, str) and v.strip()
                                                      for k, v in variables.items()):
            raise IntegrationError("bad_config", "%s: variables map {{1}}, {{2}}... to a field like customer.first_name" % key)
        entry = {"template": spec["template"].strip()[:512], "language": str(spec.get("language") or "en_US"),
                 "variables": {str(k): v.strip() for k, v in variables.items()}, "enabled": bool(spec.get("enabled", True))}
        if key == "abandoned_cart":
            delay = spec.get("delay_minutes", DEFAULT_DELAY_MINUTES)
            if not (isinstance(delay, int) and not isinstance(delay, bool) and 5 <= delay <= 24 * 60):
                raise IntegrationError("bad_config", "abandoned_cart: the delay is 5 minutes to 24 hours")
            entry["delay_minutes"] = delay
        clean[key] = entry
    country = str(config.get("default_country") or "91")
    if not re.match(r"^\d{1,3}$", country):
        raise IntegrationError("bad_config", "default_country is a calling code like 91")
    return {"events": clean, "consent_attested": bool(config.get("consent_attested")), "default_country": country}


def create(cur, tenant_id, vault: Vault, kind: str, name: str, secret: str, config: dict) -> dict:
    if kind not in KINDS:
        raise IntegrationError("bad_kind", "kind must be one of: %s" % ", ".join(KINDS))
    plans.require(cur, tenant_id, "integrations", "Store integrations")
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise IntegrationError("bad_name", "give the connection a name of 1 to 80 characters")
    if len((secret or "").strip()) < SECRET_MIN:
        raise IntegrationError("bad_secret", "paste the store's webhook signing secret")
    config = validate_config(config or {})
    token = pysecrets.token_urlsafe(24)
    blob, ver = vault.encrypt(secret.strip(), context=token)
    cur.execute("insert into integrations (tenant_id, kind, name, token, secret_enc, key_ver, config)"
                " values (%s, %s, %s, %s, %s, %s, %s) returning id", (tenant_id, kind, name, token, blob, ver, Json(config)))
    return {"id": str(cur.fetchone()[0]), "token": token, "path": "/hooks/%s/%s" % (kind, token), "config": config}


def update(cur, tenant_id, vault: Vault, integration_id, *, name=None, config=None, enabled=None, secret=None) -> None:
    cur.execute("select token from integrations where id = %s and tenant_id = %s for update", (integration_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise IntegrationError("unknown_integration", "that connection is not in this workspace")
    if name is not None:
        cur.execute("update integrations set name = %s where id = %s", (name.strip()[:80], integration_id))
    if config is not None:
        cur.execute("update integrations set config = %s where id = %s", (Json(validate_config(config)), integration_id))
    if enabled is not None:
        cur.execute("update integrations set enabled = %s where id = %s", (bool(enabled), integration_id))
    if secret is not None:
        if len(secret.strip()) < SECRET_MIN:
            raise IntegrationError("bad_secret", "paste the store's webhook signing secret")
        blob, ver = vault.encrypt(secret.strip(), context=row[0])
        cur.execute("update integrations set secret_enc = %s, key_ver = %s where id = %s", (blob, ver, integration_id))


def delete(cur, tenant_id, integration_id) -> bool:
    cur.execute("delete from integrations where id = %s and tenant_id = %s", (integration_id, tenant_id))
    return cur.rowcount > 0


def list_integrations(cur, tenant_id) -> list:
    cur.execute("select id, kind, name, token, config, enabled, created_at, last_event_at from integrations"
                " where tenant_id = %s order by created_at", (tenant_id,))
    cols = ("id", "kind", "name", "token", "config", "enabled", "created_at", "last_event_at")
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["path"] = "/hooks/%s/%s" % (d["kind"], d["token"])
        out.append(d)
    return out


# ── signatures ───────────────────────────────────────────────────────────────

def signature_ok(secret: str, body: bytes, header: Optional[str]) -> bool:
    """Both stores sign the raw body with HMAC-SHA256 and send it base64-encoded."""
    if not secret or not header:
        return False
    expected = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, header.strip().encode("utf-8", "replace"))


def load(cur, kind: str, token: str, vault: Vault) -> Optional[dict]:
    """The enabled integration behind this webhook address, with its decrypted secret."""
    cur.execute("select id, tenant_id, kind, name, secret_enc, config from integrations"
                " where token = %s and kind = %s and enabled", (token, kind))
    row = cur.fetchone()
    if row is None:
        return None
    integration = dict(zip(("id", "tenant_id", "kind", "name", "secret_enc", "config"), row))
    integration["secret"] = vault.decrypt(integration.pop("secret_enc"), context=token)
    return integration


# ── what the store said ──────────────────────────────────────────────────────

def _first(*values) -> str:
    return next((str(v).strip() for v in values if v not in (None, "")), "")


def _name(first, last) -> str:
    return " ".join(x for x in (first, last) if x)


def normalise(kind: str, topic: str, payload: dict) -> dict:
    """A store's payload as the small fixed view variables refer to."""
    p = payload if isinstance(payload, dict) else {}
    if kind == "shopify":
        cust = p.get("customer") or {}
        bill, ship = p.get("billing_address") or {}, p.get("shipping_address") or {}
        first = _first(cust.get("first_name"), bill.get("first_name"), ship.get("first_name"))
        last = _first(cust.get("last_name"), bill.get("last_name"), ship.get("last_name"))
        ful = (p.get("fulfillments") or [p])[0] if topic.startswith("orders/") or topic.startswith("fulfillments/") else {}
        ful = ful if isinstance(ful, dict) else {}
        is_checkout = topic.startswith("checkouts/")
        return {
            "customer": {"first_name": first, "last_name": last, "name": _name(first, last),
                         "phone": _first(p.get("phone"), cust.get("phone"), ship.get("phone"), bill.get("phone")),
                         "email": _first(p.get("email"), cust.get("email"))},
            "order": {"id": _first(p.get("id")) if not is_checkout else "", "name": _first(p.get("name"), p.get("order_number")),
                      "total": _first(p.get("total_price")), "currency": _first(p.get("currency")),
                      "status_url": _first(p.get("order_status_url"))},
            "tracking": {"number": _first(ful.get("tracking_number")), "url": _first(ful.get("tracking_url")),
                         "company": _first(ful.get("tracking_company"))},
            "checkout": {"id": _first(p.get("checkout_id"), p.get("id") if is_checkout else ""),
                         "url": _first(p.get("abandoned_checkout_url")), "total": _first(p.get("total_price"))},
        }
    bill, ship = p.get("billing") or {}, p.get("shipping") or {}
    first, last = _first(bill.get("first_name"), ship.get("first_name")), _first(bill.get("last_name"), ship.get("last_name"))
    return {
        "customer": {"first_name": first, "last_name": last, "name": _name(first, last),
                     "phone": _first(bill.get("phone"), ship.get("phone")), "email": _first(bill.get("email"))},
        "order": {"id": _first(p.get("id")), "name": _first(p.get("number"), p.get("id")), "total": _first(p.get("total")),
                  "currency": _first(p.get("currency")), "status_url": ""},
        "tracking": {"number": "", "url": "", "company": ""},
        "checkout": {"id": "", "url": "", "total": ""},
    }


def event_for(kind: str, topic: str, payload: dict) -> Optional[str]:
    topic = (topic or "").lower()
    if kind == "shopify":
        return {"orders/create": "order_created", "orders/fulfilled": "order_fulfilled", "fulfillments/create": "order_fulfilled",
                "checkouts/create": "abandoned_cart", "checkouts/update": "abandoned_cart"}.get(topic)
    status = (payload or {}).get("status")
    if topic == "order.created":
        return "order_created"
    if topic == "order.updated" and status == "completed":
        return "order_fulfilled"
    return None


def resolve_variable(spec: str, data: dict) -> str:
    if spec.startswith("="):
        return spec[1:] or "-"
    node = data
    for part in spec.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return str(node).strip() if node not in (None, "") else "-"


def components_for(variables: dict, data: dict) -> list:
    if not variables:
        return []
    ordered = [variables[k] for k in sorted(variables, key=int)]
    return [{"type": "body", "parameters": [{"type": "text", "text": resolve_variable(v, data)} for v in ordered]}]


def external_id(kind: str, topic: str, payload: dict, header_id: str = "") -> str:
    if header_id:
        return header_id
    p = payload if isinstance(payload, dict) else {}
    stamp = _first(p.get("updated_at"), p.get("date_modified_gmt"), p.get("status"))
    return "%s:%s" % (_first(p.get("id"), p.get("token")), stamp) if stamp else _first(p.get("id"), p.get("token")) or "-"


# ── handling an event ────────────────────────────────────────────────────────

def _contact_for(cur, integration: dict, data: dict, config: dict, evidence: str):
    """-> (contact_id, None) when this person may be messaged, else (None, reason)."""
    phone = contacts.normalise_phone(data["customer"]["phone"], config.get("default_country", "91"))
    if not phone:
        return None, "no_phone"
    cur.execute("select contact_id from wa_resolve_contact(%s, %s, null, null, null, %s, %s)",
                (integration["tenant_id"], phone, data["customer"]["name"] or None, integration["kind"]))
    contact_id = cur.fetchone()[0]
    cur.execute("select opt_out, opted_in from contacts where id = %s", (contact_id,))
    opted_out, opted_in = cur.fetchone()
    if opted_out:
        return None, "opted_out"
    if not opted_in:
        if not config.get("consent_attested"):
            return None, "no_consent"
        consent.record(cur, integration["tenant_id"], contact_id, "opt_in", integration["kind"], evidence, by_person=False)
    return contact_id, None


def handle_event(cur, integration: dict, topic: str, payload: dict, ext_id: str, *, now: Optional[datetime] = None) -> str:
    """Record and act on one webhook. -> duplicate | ignored | skipped:<why> | queued | converted."""
    now = now or utcnow()
    tenant_id, kind, config = integration["tenant_id"], integration["kind"], integration["config"] or {}
    cur.execute("insert into integration_events (tenant_id, integration_id, topic, external_id) values (%s, %s, %s, %s)"
                " on conflict (integration_id, topic, external_id) do nothing returning id",
                (tenant_id, integration["id"], topic, ext_id))
    row = cur.fetchone()
    if row is None:
        return "duplicate"
    cur.execute("update integrations set last_event_at = %s where id = %s", (now, integration["id"]))

    def done(status: str, **detail) -> str:
        cur.execute("update integration_events set status = %s, detail = %s where id = %s",
                    (status.split(":")[0], Json(detail or None), row[0]))
        return status

    data = normalise(kind, topic, payload)
    # An order for a checkout we were about to remind someone about cancels the reminder.
    if event_for(kind, topic, payload) == "order_created":
        _convert_carts(cur, integration, data, payload)
    key = event_for(kind, topic, payload)
    spec = (config.get("events") or {}).get(key) if key else None
    if not spec or not spec.get("enabled", True):
        return done("ignored", reason="not configured" if key else "unhandled topic")

    evidence = "%s %s %s" % (kind, topic, ext_id)
    contact_id, why = _contact_for(cur, integration, data, config, evidence)
    if why:
        return done("skipped:" + why, reason=why)

    if key == "abandoned_cart":
        checkout = data["checkout"]["id"] or ext_id
        due = now + timedelta(minutes=spec.get("delay_minutes", DEFAULT_DELAY_MINUTES))
        cur.execute("insert into cart_checkouts (tenant_id, integration_id, checkout_id, contact_id, payload, due_at)"
                    " values (%s, %s, %s, %s, %s, %s)"
                    " on conflict (integration_id, checkout_id) do update set payload = excluded.payload,"
                    "   contact_id = excluded.contact_id, due_at = case when cart_checkouts.state = 'waiting'"
                    "   then cart_checkouts.due_at else excluded.due_at end"
                    " where cart_checkouts.state in ('waiting') returning id, due_at",
                    (tenant_id, integration["id"], checkout, contact_id, Json(payload), due))
        cart = cur.fetchone()
        if cart is None:
            return done("ignored", reason="checkout already handled")
        jobs.enqueue(cur, tenant_id, "abandoned_cart", {"cart_id": str(cart[0])}, "cart:%s:%s" % (cart[0], cart[1].isoformat()),
                     run_at=cart[1])
        return done("queued", cart=str(cart[0]))

    try:
        send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=spec["template"], language=spec["language"],
                              components=components_for(spec.get("variables") or {}, data) or None,
                              idem_key="int:%s:%s:%s" % (integration["id"], topic, ext_id), sender=kind, now=now)
    except send.SendRefused as refused:
        return done("skipped:" + refused.reason, reason=refused.reason)
    return done("queued")


def _convert_carts(cur, integration: dict, data: dict, payload: dict) -> None:
    ids = {x for x in (data["checkout"]["id"], str((payload or {}).get("checkout_id") or ""), str((payload or {}).get("cart_token") or "")) if x}
    phone = contacts.normalise_phone(data["customer"]["phone"], (integration["config"] or {}).get("default_country", "91"))
    cur.execute("update cart_checkouts set state = 'converted' where integration_id = %s and state = 'waiting'"
                " and (checkout_id = any(%s) or contact_id in (select id from contacts where tenant_id = %s and wa_norm_phone(phone) = %s))",
                (integration["id"], list(ids) or [""], integration["tenant_id"], phone or ""))


def handle_cart_job(cur, tenant_id, payload: dict) -> None:
    """The reminder is due: send it if the checkout is still unfinished and the person is still reachable."""
    cur.execute("select c.id, c.state, c.contact_id, c.payload, c.checkout_id, i.id, i.kind, i.config, i.enabled"
                " from cart_checkouts c join integrations i on i.id = c.integration_id where c.id = %s and c.tenant_id = %s for update of c",
                (payload.get("cart_id"), tenant_id))
    row = cur.fetchone()
    if row is None or row[1] != "waiting":
        return
    cart_id, _, contact_id, cart_payload, checkout_id, integ_id, kind, config, enabled = row
    spec = ((config or {}).get("events") or {}).get("abandoned_cart")
    if not enabled or not spec or not spec.get("enabled", True):
        cur.execute("update cart_checkouts set state = 'skipped' where id = %s", (cart_id,))
        return
    data = normalise(kind, "checkouts/update", cart_payload)
    integration = {"id": integ_id, "tenant_id": tenant_id, "kind": kind, "config": config}
    contact_id, why = _contact_for(cur, integration, data, config or {}, "%s abandoned checkout %s" % (kind, checkout_id))
    if why:
        cur.execute("update cart_checkouts set state = 'skipped' where id = %s", (cart_id,))
        return
    try:
        send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=spec["template"], language=spec["language"],
                              components=components_for(spec.get("variables") or {}, data) or None,
                              idem_key="cart:%s" % cart_id, sender=kind)
        cur.execute("update cart_checkouts set state = 'sent' where id = %s", (cart_id,))
    except send.SendRefused:
        cur.execute("update cart_checkouts set state = 'skipped' where id = %s", (cart_id,))
