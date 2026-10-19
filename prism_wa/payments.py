"""Asking for money in a chat, knowing it arrived, and giving it back.

Two ways to collect, one record. A payment is an attempt to collect one order:

  meta      an `order_details` message: the customer taps Review and Pay and pays by UPI inside WhatsApp. Meta tells us the result in a status webhook
            (type "payment"); the gateway behind it is the one set up in WhatsApp Manager.
  razorpay  a payment link sent as a button. Razorpay tells us in its own webhook; the same link can be looked up at any time.

A webhook can be dropped, so `reconcile` asks the gateway about every payment still open and applies the answer through the same function the webhook uses (`apply_state`):
whichever arrives first wins and the other changes nothing. A payment that was captured never goes back to pending or failed; a late payment on a link we had marked expired
still counts, because the money is real.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from psycopg2.extras import Json

from prism_wa import commerce, razorpay, send, webhooks_out
from prism_wa.commerce import CommerceError, money_text
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.payments")

TRANSPORT: Optional[Callable] = None               # a test hands in a fake Razorpay; None = the real network
VAULT: Optional[Vault] = None                      # set once at start-up (configure); a chatbot flow that asks for payment has no other way to reach the gateway's secret


def configure(vault: Optional[Vault]) -> None:
    global VAULT
    VAULT = vault
OPEN = ("created", "pending")
RANK = {"created": 0, "pending": 1, "cancelled": 2, "expired": 2, "failed": 2, "captured": 3, "partially_refunded": 4, "refunded": 5}
PAYMENT_COLS = ("id", "order_id", "reference_id", "provider", "status", "amount_paise", "refunded_paise", "currency", "method", "txn_id", "provider_ref", "link_url", "wamid",
                "last_error", "checked_at", "paid_at", "created_at", "updated_at")


def _client(cur, tenant_id, vault: Vault) -> razorpay.Razorpay:
    creds = commerce.gateway_secrets(cur, tenant_id, vault)
    if not (creds["key_id"] and creds["secret"]):
        raise CommerceError("no_gateway", "add your Razorpay key id and secret first (Commerce → Settings)")
    return razorpay.Razorpay(creds["key_id"], creds["secret"], transport=TRANSPORT)


def get_payment(cur, tenant_id, payment_id) -> dict:
    cur.execute("select " + ", ".join("p." + c for c in PAYMENT_COLS) + ", o.contact_id, o.number_id, o.total_paise from payments p join orders o on o.id = p.order_id"
                " where p.id = %s and p.tenant_id = %s", (payment_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CommerceError("unknown_payment", "that payment is not in this workspace")
    p = dict(zip(PAYMENT_COLS + ("contact_id", "number_id", "order_total_paise"), row))
    cur.execute("select id, amount_paise, speed, status, reason, error, created_at from refunds where payment_id = %s order by created_at", (payment_id,))
    p["refunds"] = [dict(zip(("id", "amount_paise", "speed", "status", "reason", "error", "created_at"), r)) for r in cur.fetchall()]
    return p


def list_payments(cur, tenant_id, *, status: str = "", q: str = "", since=None, until=None, limit: int = 100, offset: int = 0) -> dict:
    where, params = ["p.tenant_id = %s"], [tenant_id]
    if status:
        where.append("p.status = %s")
        params.append(status)
    if since:
        where.append("p.created_at >= %s")
        params.append(since)
    if until:
        where.append("p.created_at < %s")
        params.append(until)
    if q:
        where.append("(p.reference_id ilike %s or c.name ilike %s or c.phone ilike %s)")
        params += ["%" + q + "%"] * 3
    base = " from payments p join orders o on o.id = p.order_id left join contacts c on c.id = o.contact_id where " + " and ".join(where)
    cur.execute("select count(*)" + base, params)
    total = cur.fetchone()[0]
    cur.execute("select " + ", ".join("p." + c for c in PAYMENT_COLS) + ", c.name, c.phone" + base + " order by p.created_at desc limit %s offset %s",
                params + [max(1, min(int(limit), 500)), max(0, int(offset))])
    return {"payments": [dict(zip(PAYMENT_COLS + ("contact_name", "contact_phone"), r)) for r in cur.fetchall()], "total": total}


# ── asking ───────────────────────────────────────────────────────────────────

def build_order_details(order: dict, cfg: dict, expires_at: datetime) -> dict:
    """The Meta `order_details` message for an order. Meta refuses one whose parts do not add up, so this checks first."""
    if order["subtotal_paise"] - order["discount_paise"] + order["tax_paise"] + order["shipping_paise"] != order["total_paise"]:
        raise CommerceError("bad_total", "the order's total does not equal its items, discount, tax and shipping")

    def amount(v: int) -> dict:
        return {"value": int(v), "offset": 100}

    items = []
    for i in order["items"]:
        items.append({"retailer_id": i.get("retailer_id") or i["name"][:40], "name": i["name"][:60], "amount": amount(i["unit_paise"]), "quantity": int(i["quantity"])})
    parts = {"status": "pending", "items": items, "subtotal": amount(order["subtotal_paise"]), "tax": {**amount(order["tax_paise"]), "description": "Tax"},
             "expiration": {"timestamp": str(int(expires_at.timestamp())), "description": "This payment request is open until it expires."}}
    if order["shipping_paise"]:
        parts["shipping"] = {**amount(order["shipping_paise"]), "description": "Shipping"}
    if order["discount_paise"]:
        parts["discount"] = {**amount(order["discount_paise"]), "description": "Discount"}
    physical = bool(order["shipping_paise"] or order.get("address"))
    n = sum(int(i["quantity"]) for i in order["items"])
    return {"type": "order_details", "body": {"text": ("Order %s — %d item%s, total %s. Tap below to review and pay." % (order["reference_id"], n, "" if n == 1 else "s",
                                                                                                          money_text(order["total_paise"], order["currency"])))[:1024]},
            "footer": {"text": "Secure payment through WhatsApp"},
            "action": {"name": "review_and_pay", "parameters": {"reference_id": order["reference_id"], "type": "physical-goods" if physical else "digital-goods",
                                                               "payment_configuration": cfg["payment_configuration"], "currency": order["currency"],
                                                               "total_amount": amount(order["total_paise"]), "order": parts}}}


def request_payment(cur, tenant_id, vault: Optional[Vault], *, order_id, agent_id=None, send_message: bool = True, now: Optional[datetime] = None) -> dict:
    """Ask the customer to pay an order. A new request replaces any open one for the same order. With `send_message=False` only the payment link is made (Razorpay)."""
    now = now or utcnow()
    cfg = commerce.get_config(cur, tenant_id)
    why = commerce.ready_reason(cfg)
    if why:
        raise CommerceError("not_ready", why)
    order = commerce.get_order(cur, tenant_id, order_id)
    if order["status"] not in ("draft", "awaiting_payment", "failed"):
        raise CommerceError("order_locked", "this order is %s, so it cannot be paid for" % order["status"].replace("_", " "))
    if order["total_paise"] <= 0:
        raise CommerceError("no_total", "the order has nothing to pay")
    if not order["contact_id"]:
        raise CommerceError("no_contact", "choose which customer this order is for")
    for open_ in [p for p in order["payments"] if p["status"] in OPEN]:
        close_open(cur, tenant_id, open_["id"], vault, None, "cancelled")
    expires = now + timedelta(hours=cfg["order_expiry_hours"])
    ref = order["reference_id"]
    key = "pay:%s:%d" % (order_id, int(now.timestamp() * 1000))

    cur.execute("select count(*) from payments where order_id = %s", (order_id,))
    attempt = cur.fetchone()[0]
    link_url = link_id = None
    interactive = None
    if cfg["gateway"] == "meta":
        interactive = build_order_details(order, cfg, expires)
        provider = "meta"
    else:
        rz = _client(cur, tenant_id, vault)
        cur.execute("select name, phone from contacts where id = %s", (order["contact_id"],))
        name, phone = cur.fetchone()
        link_ref = ref if attempt == 0 else "%s-%d" % (ref, attempt + 1)
        link = rz.create_link(amount_paise=order["total_paise"], currency=order["currency"], reference_id=link_ref, description="Order %s" % ref, name=name or "",
                              phone="".join(ch for ch in (phone or "") if ch.isdigit()), expire_by=int(expires.timestamp()))
        link_url, link_id, provider = link["short_url"], link["id"], "razorpay"
        interactive = {"type": "cta_url", "body": {"text": ("Order %s — total %s. Pay securely with the button below." % (ref, money_text(order["total_paise"], order["currency"])))[:1024]},
                       "footer": {"text": "Secure payment by Razorpay"},
                       "action": {"name": "cta_url", "parameters": {"display_text": ("Pay " + money_text(order["total_paise"], order["currency"]))[:20], "url": link_url}}}
    outbox_id = None
    if send_message:
        outbox_id = send.enqueue_interactive(cur, tenant_id, contact_id=order["contact_id"], interactive=interactive,
                                             preview="[payment request] %s %s" % (ref, money_text(order["total_paise"], order["currency"])), idem_key=key,
                                             number_id=order["number_id"], sender=("agent" if agent_id else "bot"), now=now)
        if agent_id:
            send.attribute(cur, outbox_id, agent_id)
    cur.execute("insert into payments (tenant_id, order_id, reference_id, provider, status, amount_paise, currency, provider_ref, link_url) values"
                " (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, order_id, ref, provider, "pending" if send_message or link_url else "created", order["total_paise"], order["currency"], link_id, link_url))
    payment_id = cur.fetchone()[0]
    cur.execute("update orders set status = 'awaiting_payment', expires_at = %s, updated_at = now() where id = %s", (expires, order_id))
    _tag(cur, order["contact_id"], add=["payment-pending"])
    return get_payment(cur, tenant_id, payment_id)


def close_open(cur, tenant_id, payment_id, vault, http, state: str) -> None:
    """Stop waiting on a payment (a Razorpay link is cancelled at Razorpay too, when we can reach it)."""
    p = get_payment(cur, tenant_id, payment_id)
    if p["status"] not in OPEN:
        return
    if p["provider"] == "razorpay" and p["provider_ref"] and vault is not None:
        try:
            _client(cur, tenant_id, vault).cancel_link(p["provider_ref"])
        except (razorpay.RazorpayError, CommerceError) as why:
            log.info("could not cancel the payment link %s: %s", p["provider_ref"], why)
    cur.execute("update payments set status = %s, updated_at = now() where id = %s", (state, payment_id))


# ── knowing ──────────────────────────────────────────────────────────────────

def _tag(cur, contact_id, add=(), remove=()) -> None:
    if not contact_id:
        return
    if add:
        cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t) where id = %s", (list(add), contact_id))
    if remove:
        cur.execute("update contacts set tags = array(select t from unnest(coalesce(tags, '{}')) t where t <> all(%s::text[])) where id = %s", (list(remove), contact_id))


def apply_state(cur, tenant_id, payment_id, state: str, *, method: Optional[str] = None, txn_id: Optional[str] = None, error: Optional[str] = None, raw=None,
                now: Optional[datetime] = None, notify: bool = True) -> bool:
    """Move a payment to what the gateway reports. Returns whether anything changed. Safe to call twice with the same news, and with news in any order."""
    now = now or utcnow()
    cur.execute("select p.status, p.order_id, o.contact_id, p.amount_paise, p.reference_id, o.status, o.currency, p.method, p.txn_id from payments p join orders o on o.id = p.order_id"
                " where p.id = %s and p.tenant_id = %s for update of p", (payment_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        return False
    current, order_id, contact_id, amount, ref, order_status, currency, had_method, had_txn = row
    if state not in RANK:
        return False
    upgrade = RANK[state] > RANK[current] or (state == "captured" and current in ("failed", "expired", "cancelled"))
    details_changed = (method and method != had_method) or (txn_id and txn_id != had_txn)
    if not upgrade and not details_changed:
        return False
    new = state if upgrade else current
    cur.execute("update payments set status = %s, method = coalesce(%s, method), txn_id = coalesce(%s, txn_id), last_error = %s,"
                " raw = coalesce(%s, raw), paid_at = case when %s = 'captured' then coalesce(paid_at, %s) else paid_at end, checked_at = %s, updated_at = %s where id = %s",
                (new, method, txn_id, error if new in ("failed", "expired") else None, Json(raw) if raw is not None else None, new, now, now, now, payment_id))
    if not upgrade:
        return True
    if new == "captured":
        if order_status in ("draft", "awaiting_payment", "failed"):
            cur.execute("update orders set status = 'paid', updated_at = %s where id = %s", (now, order_id))
        _tag(cur, contact_id, add=["customer"], remove=["payment-failed", "payment-pending", "cart"])
        if notify and contact_id:
            _tell(cur, tenant_id, contact_id, "Thank you — we received %s for order %s." % (money_text(amount, currency), ref), "paid:%s" % payment_id, now)
    elif new in ("failed", "expired"):
        if order_status == "awaiting_payment":
            cur.execute("update orders set status = 'failed', updated_at = %s where id = %s", (now, order_id))
        _tag(cur, contact_id, add=["payment-failed"], remove=["payment-pending"])
    elif new == "cancelled":
        _tag(cur, contact_id, remove=["payment-pending"])
    webhooks_out.emit(cur, tenant_id, "payment.status", lambda: {"payment_id": str(payment_id), "order_id": str(order_id), "reference_id": ref, "status": new,
                                                                 "amount_paise": amount, "currency": currency, "method": method, "contact_id": str(contact_id) if contact_id else None})
    return True


def _tell(cur, tenant_id, contact_id, text: str, key: str, now) -> None:
    try:
        cur.execute("savepoint tell")
        send.enqueue_text(cur, tenant_id, contact_id=contact_id, text=text, idem_key=key, sender="system", now=now)
        cur.execute("release savepoint tell")
    except send.SendRefused as why:                                  # the chat's 24 hours closed, or no number: the order still moved on
        cur.execute("rollback to savepoint tell")
        cur.execute("release savepoint tell")
        log.info("customer %s not told (%s): %s", contact_id, key, why)


STATUS_WORDS = {"processing": "is being prepared", "shipped": "has been shipped", "completed": "is complete — thank you", "cancelled": "was cancelled"}
META_STATUS = {"processing": "processing", "shipped": "shipped", "completed": "completed", "cancelled": "canceled"}


def tell_customer_status(cur, tenant_id, order: dict, status: str, note: str, *, sender: str = "system") -> None:
    text = "Your order %s %s.%s" % (order["reference_id"], STATUS_WORDS[status], (" " + note) if note else "")
    paid_with_meta = any(p["provider"] == "meta" and p["status"] in ("captured", "partially_refunded") for p in order["payments"])
    key = "orderstatus:%s:%s" % (order["id"], status)
    if paid_with_meta:
        try:
            cur.execute("savepoint tell")
            send.enqueue_interactive(cur, tenant_id, contact_id=order["contact_id"], idem_key=key, sender=sender, number_id=order["number_id"],
                                     preview="[order status] %s" % status,
                                     interactive={"type": "order_status", "body": {"text": text[:1024]},
                                                  "action": {"name": "review_order", "parameters": {"reference_id": order["reference_id"],
                                                                                                   "order": {"status": META_STATUS[status], "description": (note or text)[:200]}}}})
            cur.execute("release savepoint tell")
            return
        except send.SendRefused:
            cur.execute("rollback to savepoint tell")
            cur.execute("release savepoint tell")
            return
    _tell(cur, tenant_id, order["contact_id"], text, key, utcnow())


def meta_state(status_obj: dict) -> tuple:
    """-> (state, method, transaction id, error) from the status block of Meta's payment webhook. Reads loosely: Meta's shape varies by country and has changed."""
    payment = status_obj.get("payment") if isinstance(status_obj.get("payment"), dict) else {}
    tx = payment.get("transaction") if isinstance(payment.get("transaction"), dict) else {}
    top = str(status_obj.get("status") or "").lower()
    inner = str(tx.get("status") or "").lower()
    if top in ("captured", "success", "paid", "completed") or inner in ("success", "captured", "paid"):
        state = "captured"
    elif top in ("failed", "canceled", "cancelled", "declined") or inner in ("failed", "declined", "canceled", "cancelled"):
        state = "failed"
    elif top in ("refunded",):
        state = "refunded"
    else:
        state = "pending"
    method = tx.get("method")
    if isinstance(method, dict):
        method = method.get("type")
    error = None
    if state == "failed":
        errors = status_obj.get("errors") or tx.get("error") or []
        err = errors[0] if isinstance(errors, list) and errors else errors
        error = (err.get("message") or err.get("title")) if isinstance(err, dict) else (str(err) if err else "The payment did not go through.")
    return state, (str(method).lower() if method else None), (str(tx.get("pg_transaction_id") or tx.get("id") or "") or None), error


def on_meta_status(cur, tenant_id, event: dict) -> bool:
    """A status webhook of type "payment" -> the payment it is about. Returns whether we knew the payment."""
    s = (event.get("raw") or {}).get("status") or {}
    ref = ((s.get("payment") or {}).get("reference_id")) or ""
    if not ref:
        return False
    cur.execute("select id from payments where tenant_id = %s and reference_id = %s and provider = 'meta' order by created_at desc limit 1", (tenant_id, ref))
    row = cur.fetchone()
    if row is None:
        return False
    state, method, txn, error = meta_state(s)
    if state == "refunded":
        return False                                                # refunds are recorded by the refund call, which knows the amount
    apply_state(cur, tenant_id, row[0], state, method=method, txn_id=txn, error=error, raw=s)
    if s.get("id"):
        cur.execute("update payments set wamid = coalesce(wamid, %s) where id = %s", (str(s["id"]), row[0]))
    return True


def razorpay_webhook(cur, tenant_id, vault: Vault, body: bytes, signature: str) -> dict:
    """Razorpay's webhook for a workspace. Refused unless the signature matches the workspace's webhook secret."""
    creds = commerce.gateway_secrets(cur, tenant_id, vault)
    if not creds["webhook_secret"]:
        raise CommerceError("no_webhook_secret", "add the webhook secret from your Razorpay dashboard first")
    if not razorpay.verify_signature(creds["webhook_secret"], body, signature):
        raise CommerceError("bad_signature", "that did not come from Razorpay (the signature does not match)")
    try:
        event = json.loads(body.decode("utf-8"))
    except ValueError:
        raise CommerceError("bad_body", "the notification could not be read") from None
    name = str(event.get("event") or "")
    payload = event.get("payload") or {}
    link = (payload.get("payment_link") or {}).get("entity") or {}
    pay = (payload.get("payment") or {}).get("entity") or {}
    link_id = link.get("id") or ""
    cur.execute("select id, status from payments where tenant_id = %s and provider = 'razorpay' and (provider_ref = %s or txn_id = %s) order by created_at desc limit 1",
                (tenant_id, link_id or "-", pay.get("id") or "-"))
    row = cur.fetchone()
    if row is None:
        return {"handled": False, "event": name}
    if name == "payment_link.paid" or name == "payment.captured":
        apply_state(cur, tenant_id, row[0], "captured", method=pay.get("method"), txn_id=pay.get("id") or razorpay.payment_id_of(link) or None, raw=event)
    elif name == "payment_link.expired":
        apply_state(cur, tenant_id, row[0], "expired", raw=event)
    elif name == "payment_link.cancelled":
        apply_state(cur, tenant_id, row[0], "cancelled", raw=event)
    elif name == "payment.failed":
        apply_state(cur, tenant_id, row[0], "failed", method=pay.get("method"), txn_id=pay.get("id"), error=str(pay.get("error_description") or "The payment did not go through."), raw=event)
    else:
        return {"handled": False, "event": name}
    return {"handled": True, "event": name, "payment_id": str(row[0])}


def expire_old(cur, now: Optional[datetime] = None) -> int:
    """Payment requests whose time ran out. Each goes through apply_state, so the order, the customer's tags and the webhooks all hear about it."""
    now = now or utcnow()
    cur.execute("select p.id, p.tenant_id from payments p join orders o on o.id = p.order_id where p.status in ('created', 'pending')"
                " and o.status = 'awaiting_payment' and o.expires_at is not null and o.expires_at <= %s", (now,))
    rows = cur.fetchall()
    for pid, tenant_id in rows:
        apply_state(cur, tenant_id, pid, "expired", error="The payment request ran out of time.", now=now, notify=False)
    return len(rows)


# ── catching up with a webhook that never came ───────────────────────────────

def check(cur, tenant_id, vault: Vault, graph, payment_id, *, now: Optional[datetime] = None) -> dict:
    """Ask the gateway where one payment stands and apply the answer. Returns the payment as it is now."""
    now = now or utcnow()
    p = get_payment(cur, tenant_id, payment_id)
    try:
        if p["provider"] == "razorpay" and p["provider_ref"]:
            link = _client(cur, tenant_id, vault).get_link(p["provider_ref"])
            state = razorpay.link_state(link)
            apply_state(cur, tenant_id, payment_id, state, txn_id=razorpay.payment_id_of(link) or None, raw=link, now=now)
        elif p["provider"] == "meta":
            token, _waba, pnid = _meta_token(cur, tenant_id, vault, p["number_id"])
            reply = graph.payment_status(token, pnid, p["reference_id"])
            block = reply
            if isinstance(reply.get("data"), list) and reply["data"]:
                block = reply["data"][0]
            if isinstance(block.get("payment"), dict) or block.get("status"):
                state, method, txn, error = meta_state(block)
                if state in RANK:
                    apply_state(cur, tenant_id, payment_id, state, method=method, txn_id=txn, error=error, raw=block, now=now)
        cur.execute("update payments set checked_at = %s, last_error = case when status in ('created', 'pending') then null else last_error end where id = %s", (now, payment_id))
    except Exception as why:                                          # noqa: BLE001  (a gateway outage must not stop the others being checked)
        cur.execute("update payments set checked_at = %s, last_error = %s where id = %s", (now, str(why)[:300], payment_id))
        if not isinstance(why, (razorpay.RazorpayError, CommerceError)) and not why.__class__.__name__ == "GraphError":
            raise
    return get_payment(cur, tenant_id, payment_id)


def _meta_token(cur, tenant_id, vault: Vault, number_id=None) -> tuple:
    if number_id:
        cur.execute("select w.token_enc, w.waba_id, p.phone_number_id from phone_numbers p join wabas w on w.id = p.waba_id where p.id = %s and p.tenant_id = %s", (number_id, tenant_id))
        row = cur.fetchone()
        if row:
            return vault.decrypt(row[0], context=row[1]), row[1], row[2]
    return commerce._token_for(cur, tenant_id, vault)


def reconcile(cur, *, graph, vault: Vault, now: Optional[datetime] = None, limit: int = 25, quiet_for: timedelta = timedelta(minutes=2),
              recheck_after: timedelta = timedelta(minutes=5)) -> int:
    """For every payment still open (every workspace): expire the ones whose time ran out, then ask the gateway about the rest. Returns how many changed."""
    now = now or utcnow()
    expire_old(cur, now)
    cur.execute("select id, tenant_id, status from payments where status in ('created', 'pending') and created_at <= %s and (checked_at is null or checked_at <= %s)"
                " order by coalesce(checked_at, created_at) limit %s", (now - quiet_for, now - recheck_after, limit))
    changed = 0
    for pid, tenant_id, before in cur.fetchall():
        cur.execute("savepoint recon")
        try:
            after = check(cur, tenant_id, vault, graph, pid, now=now)
            changed += 1 if after["status"] != before else 0
            cur.execute("release savepoint recon")
        except Exception:                                              # noqa: BLE001
            log.exception("could not check payment %s", pid)
            cur.execute("rollback to savepoint recon")
            cur.execute("release savepoint recon")
    return changed


# ── giving it back ───────────────────────────────────────────────────────────

def refund(cur, tenant_id, vault: Vault, *, payment_id, amount_paise: Optional[int] = None, speed: str = "normal", reason: str = "", agent_id=None,
           record_only: bool = False, now: Optional[datetime] = None) -> dict:
    """Send money back through Razorpay, or (record_only) note a refund made somewhere else. Never more than was paid."""
    now = now or utcnow()
    if speed not in ("normal", "instant"):
        raise CommerceError("bad_speed", "a refund is normal or instant")
    p = get_payment(cur, tenant_id, payment_id)
    if p["status"] not in ("captured", "partially_refunded"):
        raise CommerceError("not_refundable", "only a payment that was received can be refunded")
    left = p["amount_paise"] - p["refunded_paise"]
    amount = left if amount_paise in (None, 0) else int(amount_paise)
    if amount <= 0 or amount > left:
        raise CommerceError("bad_amount", "the refund must be between 0.01 and %s (what is left of this payment)" % commerce.rupees(left))
    status, ref, error = "recorded", None, None
    if not record_only:
        creds = commerce.gateway_secrets(cur, tenant_id, vault)
        if not (creds["key_id"] and creds["secret"] and p["txn_id"]):
            raise CommerceError("no_gateway_refund", "Prism cannot send this refund (no Razorpay credentials, or the gateway's payment id is not known yet). "
                                                   "Refund it in your gateway dashboard, then record it here.")
        try:
            reply = razorpay.Razorpay(creds["key_id"], creds["secret"], transport=TRANSPORT).refund(p["txn_id"], amount, speed, {"reference": p["reference_id"], "reason": reason[:200]})
        except razorpay.RazorpayError as why:
            raise CommerceError("gateway_refused", "Razorpay refused the refund: %s" % why) from why
        ref, status = reply.get("id"), ("processed" if str(reply.get("status")) == "processed" else "pending")
    cur.execute("insert into refunds (tenant_id, payment_id, amount_paise, speed, status, provider_ref, reason, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, payment_id, amount, speed, status, ref, reason[:300] or None, agent_id))
    done = p["refunded_paise"] + amount
    new = "refunded" if done >= p["amount_paise"] else "partially_refunded"
    cur.execute("update payments set refunded_paise = %s, status = %s, updated_at = %s where id = %s", (done, new, now, payment_id))
    if new == "refunded":
        cur.execute("update orders set status = 'refunded', updated_at = %s where id = %s and status in ('paid', 'processing', 'shipped', 'completed')", (now, p["order_id"]))
    webhooks_out.emit(cur, tenant_id, "payment.status", lambda: {"payment_id": str(payment_id), "order_id": str(p["order_id"]), "reference_id": p["reference_id"], "status": new,
                                                                 "amount_paise": p["amount_paise"], "refunded_paise": done, "currency": p["currency"]})
    return get_payment(cur, tenant_id, payment_id)


# ── looking at it all ────────────────────────────────────────────────────────

def dashboard(cur, tenant_id, since=None, until=None, *, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    until = until or now + timedelta(days=1)
    since = since or now - timedelta(days=30)
    cur.execute("select status, count(*), coalesce(sum(amount_paise), 0) from payments where tenant_id = %s and created_at >= %s and created_at < %s group by status", (tenant_id, since, until))
    by = {s: {"count": c, "paise": int(a)} for s, c, a in cur.fetchall()}
    got = [by.get(s, {"count": 0, "paise": 0}) for s in ("captured", "partially_refunded", "refunded")]
    received_count, received = sum(g["count"] for g in got), sum(g["paise"] for g in got)
    cur.execute("select coalesce(sum(r.amount_paise), 0) from refunds r where r.tenant_id = %s and r.created_at >= %s and r.created_at < %s and r.status <> 'failed'", (tenant_id, since, until))
    refunded = int(cur.fetchone()[0])
    settled = received_count + by.get("failed", {"count": 0})["count"] + by.get("expired", {"count": 0})["count"] + by.get("cancelled", {"count": 0})["count"]
    cur.execute("select date_trunc('day', paid_at)::date, count(*), sum(amount_paise) from payments where tenant_id = %s and paid_at >= %s and paid_at < %s"
                " group by 1 order by 1", (tenant_id, since, until))
    days = [{"day": d.isoformat(), "count": c, "paise": int(a)} for d, c, a in cur.fetchall()]
    cur.execute("select coalesce(method, 'unknown'), count(*), sum(amount_paise) from payments where tenant_id = %s and paid_at >= %s and paid_at < %s group by 1 order by 3 desc",
                (tenant_id, since, until))
    methods = [{"method": m, "count": c, "paise": int(a)} for m, c, a in cur.fetchall()]
    cur.execute("select count(*) from payments where tenant_id = %s and status in ('created', 'pending') and created_at < %s", (tenant_id, now - timedelta(hours=1)))
    abandoned = cur.fetchone()[0]
    cur.execute("select count(*), coalesce(sum(total_paise), 0) from orders where tenant_id = %s and created_at >= %s and created_at < %s", (tenant_id, since, until))
    orders, ordered = cur.fetchone()
    return {"received_paise": received, "received_count": received_count, "refunded_paise": refunded, "net_paise": received - refunded,
            "average_paise": int(received / received_count) if received_count else 0, "pending": by.get("pending", {"count": 0, "paise": 0}),
            "failed": by.get("failed", {"count": 0, "paise": 0}), "expired": by.get("expired", {"count": 0, "paise": 0}), "abandoned": abandoned,
            "conversion_percent": round(100.0 * received_count / settled, 1) if settled else 0.0, "orders": orders, "ordered_paise": int(ordered), "days": days, "methods": methods,
            "since": since.isoformat(), "until": until.isoformat()}


def retarget(cur, tenant_id, *, older_than_minutes: int = 60) -> list:
    """People whose latest payment on an order failed, expired or was left open, and who have not paid that order since: who to remind."""
    cur.execute(
        "select o.contact_id, c.name, c.phone, o.id, o.reference_id, o.total_paise, o.currency, p.status, p.updated_at from payments p join orders o on o.id = p.order_id"
        " join contacts c on c.id = o.contact_id where p.tenant_id = %s and o.status in ('failed', 'awaiting_payment')"
        " and p.status in ('failed', 'expired', 'cancelled', 'pending', 'created') and p.updated_at <= now() - make_interval(mins => %s)"
        " and p.id = (select p2.id from payments p2 where p2.order_id = o.id order by p2.created_at desc limit 1)"
        " and not exists (select 1 from payments p3 where p3.order_id = o.id and p3.status in ('captured', 'partially_refunded', 'refunded'))"
        " and coalesce(c.opt_out, false) = false order by p.updated_at desc limit 500", (tenant_id, older_than_minutes))
    cols = ("contact_id", "name", "phone", "order_id", "reference_id", "total_paise", "currency", "payment_status", "last_try")
    return [dict(zip(cols, r)) for r in cur.fetchall()]
