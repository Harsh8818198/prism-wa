"""Telling a workspace's own systems when something happens: outbound webhooks.

A workspace registers a URL and the events it wants. When one happens, `emit()` queues a delivery for each endpoint that asked for it,
in the same transaction as the thing that happened, so an event is never lost and never sent for something that rolled back. The
worker's `deliver_due()` sends them: a POST of JSON, signed so the receiver can tell it is ours, retried with a growing wait if the
receiver is down, and recorded either way (the delivery log, and a replay button).

    X-Prism-Event       the event name
    X-Prism-Delivery    this delivery's id (the same on every retry, so a receiver can drop a repeat)
    X-Prism-Timestamp   when it was sent (unix seconds)
    X-Prism-Signature   v1=<hex HMAC-SHA256 of "<timestamp>.<body>" with the endpoint's secret>

The URL must be https (or http to this computer), and must not point at a private or internal address: the service would otherwise be
a way to reach things inside its own network. That is checked when the endpoint is saved and again at every delivery.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Callable, Optional
from urllib.parse import urlparse

from psycopg2.extras import Json

from prism_wa import plans
from prism_wa.jobs import utcnow

EVENTS = {
    "message.received": "A customer sent a message",
    "message.status": "A message you sent was delivered, read or failed",
    "chat.handoff": "A chat needs a person",
    "campaign.completed": "A campaign finished sending",
    "contact.opt_in": "A contact opted in",
    "contact.opt_out": "A contact opted out",
    "template.status": "Meta approved, rejected or changed a template",
    "flow.response": "A customer finished a form or flow",
    "payment.status": "A payment was made, failed or refunded",
    "order.received": "A customer sent a cart from the catalogue",
}
BACKOFF = (60, 300, 1800, 7200, 21600)                  # seconds to wait after the 1st, 2nd... failed try
MAX_ATTEMPTS = len(BACKOFF) + 1
SWITCH_OFF_AFTER = 20                                   # deliveries in a row that ran out of tries
TIMEOUT = 10
BODY_MAX = 256 * 1024


class WebhookError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


Resolver = Callable[[str], list]


def _resolve(host: str) -> list:
    try:
        return [i[4][0] for i in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        return []


def check_url(url: str, resolver: Optional[Resolver] = None) -> None:
    """Raise unless this is a web address a delivery may go to."""
    resolver = resolver or _resolve
    u = urlparse((url or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname or len(url) > 500:
        raise WebhookError("bad_url", "That is not a web address. It must start with https://")
    if u.username or u.password:
        raise WebhookError("bad_url", "Leave the user name and password out of the address.")
    host = u.hostname
    local = host in ("localhost", "127.0.0.1", "::1")
    if u.scheme == "http" and not local:
        raise WebhookError("insecure_url", "The address must use https (plain http is only allowed to this computer).")
    if local:
        return
    addrs = resolver(host)
    if not addrs:
        raise WebhookError("unknown_host", "That address cannot be found. Check the spelling.")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise WebhookError("private_url", "That address points inside a private network, which webhooks may not reach.")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


# ── endpoints ────────────────────────────────────────────────────────────────

COLS = ("id", "url", "description", "events", "enabled", "failure_count", "last_status", "last_error", "last_delivery_at", "disabled_reason", "created_at")


def _row(cur, tenant_id, endpoint_id) -> dict:
    cur.execute("select %s from webhook_endpoints where id = %%s and tenant_id = %%s" % ", ".join(COLS), (endpoint_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise WebhookError("unknown_endpoint", "That webhook is not in this workspace.")
    return dict(zip(COLS, row))


def _clean_events(events) -> list:
    out = []
    for e in events or []:
        if e not in EVENTS:
            raise WebhookError("bad_event", "%r is not an event. Choose from: %s." % (e, ", ".join(EVENTS)))
        if e not in out:
            out.append(e)
    if not out:
        raise WebhookError("no_events", "Choose at least one thing to be told about.")
    return out


def list_endpoints(cur, tenant_id) -> list:
    cur.execute("select %s from webhook_endpoints where tenant_id = %%s order by created_at" % ", ".join(COLS), (tenant_id,))
    return [dict(zip(COLS, r)) for r in cur.fetchall()]


def create(cur, tenant_id, *, url: str, events: list, description: str = "", agent_id=None, resolver: Optional[Resolver] = None) -> dict:
    """-> the endpoint, with `secret` (shown this once)."""
    plans.require(cur, tenant_id, "webhooks", "Webhooks")
    cur.execute("select count(*) from webhook_endpoints where tenant_id = %s", (tenant_id,))
    plans.check_count(cur, tenant_id, "webhooks", cur.fetchone()[0], 1, "webhooks")
    check_url(url, resolver)
    events = _clean_events(events)
    secret = "whsec_" + secrets.token_urlsafe(24)
    cur.execute("insert into webhook_endpoints (tenant_id, url, description, secret, events, created_by) values (%s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, url.strip(), (description or "").strip()[:200] or None, secret, events, agent_id))
    return {**_row(cur, tenant_id, cur.fetchone()[0]), "secret": secret}


def update(cur, tenant_id, endpoint_id, changes: dict, *, resolver: Optional[Resolver] = None) -> dict:
    _row(cur, tenant_id, endpoint_id)
    sets, params = [], []
    if "url" in changes:
        check_url(changes["url"], resolver)
        sets.append("url = %s")
        params.append(changes["url"].strip())
    if "events" in changes:
        sets.append("events = %s")
        params.append(_clean_events(changes["events"]))
    if "description" in changes:
        sets.append("description = %s")
        params.append((changes["description"] or "").strip()[:200] or None)
    if "enabled" in changes:
        sets.append("enabled = %s")
        params.append(bool(changes["enabled"]))
        if changes["enabled"]:
            sets += ["failure_count = 0", "disabled_reason = null"]                  # turning it back on gives it a clean start
    if not sets:
        raise WebhookError("nothing_to_change", "There is nothing to change.")
    cur.execute("update webhook_endpoints set %s where id = %%s and tenant_id = %%s" % ", ".join(sets), params + [endpoint_id, tenant_id])
    return _row(cur, tenant_id, endpoint_id)


def delete(cur, tenant_id, endpoint_id) -> bool:
    cur.execute("delete from webhook_endpoints where id = %s and tenant_id = %s", (endpoint_id, tenant_id))
    return cur.rowcount > 0


def rotate_secret(cur, tenant_id, endpoint_id) -> str:
    _row(cur, tenant_id, endpoint_id)
    secret = "whsec_" + secrets.token_urlsafe(24)
    cur.execute("update webhook_endpoints set secret = %s where id = %s and tenant_id = %s", (secret, endpoint_id, tenant_id))
    return secret


# ── queueing ─────────────────────────────────────────────────────────────────

def emit(cur, tenant_id, event: str, data) -> int:
    """Queue `event` for every enabled endpoint of the workspace that asked for it. `data` is a dict, or a function that makes one (it
    is only called when somebody is listening, so a workspace with no webhooks costs one small query). Never raises: telling the world
    must not break the thing that happened."""
    if event not in EVENTS:
        return 0
    try:
        cur.execute("savepoint wh_emit")
        cur.execute("select exists (select 1 from webhook_endpoints where tenant_id = %s and enabled and %s = any(events))", (tenant_id, event))
        if not cur.fetchone()[0]:
            cur.execute("release savepoint wh_emit")
            return 0
        body = data() if callable(data) else data
        cur.execute("insert into webhook_deliveries (tenant_id, endpoint_id, event, payload)"
                    " select tenant_id, id, %s, %s from webhook_endpoints where tenant_id = %s and enabled and %s = any(events)",
                    (event, Json(body or {}), tenant_id, event))
        n = cur.rowcount
        cur.execute("release savepoint wh_emit")
        return n
    except Exception:                                     # noqa: BLE001
        try:
            cur.execute("rollback to savepoint wh_emit")
        except Exception:                                 # noqa: BLE001
            pass
        return 0


def send_test(cur, tenant_id, endpoint_id) -> str:
    """Queue a `ping` to one endpoint, whatever events it chose, so a person can see the whole path work."""
    _row(cur, tenant_id, endpoint_id)
    cur.execute("insert into webhook_deliveries (tenant_id, endpoint_id, event, payload) values (%s, %s, 'ping', %s) returning id",
                (tenant_id, endpoint_id, Json({"message": "This is a test from Prism."})))
    return str(cur.fetchone()[0])


def deliveries(cur, tenant_id, endpoint_id=None, limit: int = 50) -> list:
    sql = ("select d.id, d.endpoint_id, e.url, d.event, d.state, d.attempts, d.response_status, d.error, d.created_at, d.delivered_at, d.next_at"
           " from webhook_deliveries d join webhook_endpoints e on e.id = d.endpoint_id where d.tenant_id = %s")
    params = [tenant_id]
    if endpoint_id:
        sql += " and d.endpoint_id = %s"
        params.append(endpoint_id)
    cur.execute(sql + " order by d.created_at desc limit %s", params + [max(1, min(int(limit or 50), 200))])
    cols = ("id", "endpoint_id", "url", "event", "state", "attempts", "response_status", "error", "created_at", "delivered_at", "next_at")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def replay(cur, tenant_id, delivery_id) -> str:
    """Send a past delivery again, as a new one."""
    cur.execute("insert into webhook_deliveries (tenant_id, endpoint_id, event, payload) select tenant_id, endpoint_id, event, payload"
                " from webhook_deliveries where id = %s and tenant_id = %s returning id", (delivery_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise WebhookError("unknown_delivery", "That delivery is not in this workspace.")
    return str(row[0])


# ── sending ──────────────────────────────────────────────────────────────────

def urllib_transport(method: str, url: str, data: bytes, headers: dict, timeout: int = TIMEOUT) -> tuple:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:                  # noqa: S310  (the address was checked)
            return resp.status, resp.read(2048)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(exc).encode("utf-8", "replace")


def deliver_due(connect: Callable, *, transport: Optional[Callable] = None, resolver: Optional[Resolver] = None, limit: int = 25,
                now=None, clock: Callable = time.time) -> dict:
    """Send what is due. -> {"sent": n, "retry": n, "failed": n}. One endpoint down never holds up another."""
    now = now or utcnow()
    transport = transport or urllib_transport
    stats = {"sent": 0, "retry": 0, "failed": 0}
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("select d.id, d.tenant_id, d.endpoint_id, d.event, d.payload, d.attempts, d.created_at, e.url, e.secret"
                        " from webhook_deliveries d join webhook_endpoints e on e.id = d.endpoint_id"
                        " where d.state = 'pending' and d.next_at <= %s and e.enabled order by d.next_at limit %s for update of d skip locked", (now, limit))
            due = cur.fetchall()
            for did, tenant_id, endpoint_id, event, payload, attempts, created, url, secret in due:
                body = json.dumps({"id": str(did), "event": event, "created_at": created.isoformat(), "data": payload}, separators=(",", ":"),
                                  ensure_ascii=False).encode("utf-8")
                status, error = 0, None
                try:
                    check_url(url, resolver)
                    if len(body) > BODY_MAX:
                        raise WebhookError("too_big", "The event is too big to send.")
                    ts = str(int(clock()))
                    status, raw = transport("POST", url, body, {"Content-Type": "application/json", "User-Agent": "Prism-Webhooks/1",
                                                               "X-Prism-Event": event, "X-Prism-Delivery": str(did), "X-Prism-Timestamp": ts,
                                                               "X-Prism-Signature": sign(secret, ts, body)}, TIMEOUT)
                    if not 200 <= status < 300:
                        error = ("HTTP %d" % status if status else "could not connect") + (": " + raw.decode("utf-8", "replace")[:200] if raw else "")
                except WebhookError as exc:
                    error = str(exc)
                attempts += 1
                if error is None:
                    cur.execute("update webhook_deliveries set state = 'delivered', attempts = %s, response_status = %s, error = null, delivered_at = %s where id = %s",
                                (attempts, status, now, did))
                    cur.execute("update webhook_endpoints set failure_count = 0, last_status = %s, last_error = null, last_delivery_at = %s where id = %s",
                                (status, now, endpoint_id))
                    stats["sent"] += 1
                elif attempts >= MAX_ATTEMPTS:
                    cur.execute("update webhook_deliveries set state = 'failed', attempts = %s, response_status = %s, error = %s where id = %s",
                                (attempts, status or None, error, did))
                    cur.execute("update webhook_endpoints set failure_count = failure_count + 1, last_status = %s, last_error = %s, last_delivery_at = %s where id = %s",
                                (status or None, error, now, endpoint_id))
                    cur.execute("update webhook_endpoints set enabled = false, disabled_reason = %s where id = %s and failure_count >= %s",
                                ("Switched off after %d deliveries in a row failed. Fix the address, then turn it back on." % SWITCH_OFF_AFTER, endpoint_id, SWITCH_OFF_AFTER))
                    stats["failed"] += 1
                else:
                    cur.execute("update webhook_deliveries set attempts = %s, response_status = %s, error = %s, next_at = %s where id = %s",
                                (attempts, status or None, error, now + timedelta(seconds=BACKOFF[attempts - 1]), did))
                    cur.execute("update webhook_endpoints set last_status = %s, last_error = %s, last_delivery_at = %s where id = %s",
                                (status or None, error, now, endpoint_id))
                    stats["retry"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats
