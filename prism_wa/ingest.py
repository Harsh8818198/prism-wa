"""The ingest worker: stored webhook events -> contacts, conversations, messages.

The gateway only records what Meta sent (store.py). This turns each recorded
event into the rows the Prism inbox already reads — through the same `contacts`
/ `conversations` / `messages` tables the licence server's /v1/whatsapp/* serves,
so real messages appear in the existing panel with no change to it.

  message  -> wa_resolve_contact() (phone or business-scoped id; never keyed on a
              username), the conversation for that contact, an inbound message row.
              Redelivery is harmless: messages are unique on (tenant, wamid).
  status   -> the outbound message that carries that wamid: queued < sent <
              delivered < read, never moving backwards, failures recorded with
              Meta's error, pricing kept. A "recipient has opted out" failure
              (131050) flips the contact's opt-out flag.
  template_* -> our copy of the template (templates.py): status, quality, category.
  other    -> nothing yet (phone / account events); the job completes and the
              event stays stored.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from psycopg2.extras import Json

from prism_wa import ai_agent, ai_knowledge, campaigns, coexistence, commerce, consent, flow_engine, forms, inbox, integrations, jobs, media, payments, send, webhooks_out
from prism_wa.jobs import Defer, utcnow

log = logging.getLogger("prism_wa.ingest")

# A status can overtake the message it is about (we record a send's wamid just
# after Meta accepts it). Wait a little before concluding the message is not ours.
STATUS_PATIENCE = timedelta(minutes=2)
DEFER_SECONDS = 15

STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
OPTED_OUT_CODE = 131050


class Skip(Exception):
    """An event this worker cannot act on (and never will). Not a failure."""


def next_status(current: Optional[str], new: str) -> Optional[str]:
    """The status a message should have after `new` arrives. Never goes backwards,
    and a failure only counts while the message had not been delivered."""
    if current in ("failed", "cancelled"):
        return current
    if new == "failed":
        return "failed" if current in (None, "queued", "sent", "unknown") else current
    if new not in STATUS_ORDER:
        return current
    if current is None or STATUS_ORDER[new] > STATUS_ORDER.get(current, 0):
        return new
    return current


def handle_event_job(cur, tenant_id, payload: dict) -> None:
    """The `ingest_event` job: load the stored event and act on it."""
    cur.execute("select id, kind, canonical, raw, waba_id, phone_number_id, occurred_at, received_at"
                " from wa_events where id = %s and tenant_id = %s",
                (payload.get("event_id"), tenant_id))
    row = cur.fetchone()
    if row is None:
        return                                          # the event is gone; nothing to do
    event = dict(zip(("id", "kind", "canonical", "raw", "waba_id", "phone_number_id", "occurred_at",
                      "received_at"), row))
    handler = EVENT_HANDLERS.get(event["kind"])
    if handler is None:
        return
    try:
        handler(cur, tenant_id, event)
    except Skip as why:
        log.info("skipped event %s: %s", event["id"], why)


# ── inbound messages ─────────────────────────────────────────────────────────

def on_message(cur, tenant_id, event: dict) -> None:
    c = event["canonical"]
    phone, bsuid, parent = (c.get("wa_id") or None), (c.get("bsuid") or None), (c.get("parent_bsuid") or None)
    if not (phone or bsuid or parent):
        raise Skip("message carries no phone number or business-scoped id")

    source = "whatsapp_ad" if (c.get("referral") or {}).get("ctwa_clid") else "whatsapp"
    cur.execute("select contact_id from wa_resolve_contact(%s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, phone, bsuid, parent, c.get("username") or None,
                 c.get("profile_name") or None, source))
    contact_id = cur.fetchone()[0]

    cur.execute("select id from phone_numbers where tenant_id = %s and phone_number_id = %s",
                (tenant_id, event["phone_number_id"]))
    number = cur.fetchone()
    number_id = number[0] if number else None

    when = event["occurred_at"] or event["received_at"] or utcnow()
    cur.execute(
        "insert into conversations (tenant_id, contact_id, channel, last_active_at, last_inbound_at, number_id)"
        " values (%s, %s, 'whatsapp', %s, %s, %s)"
        " on conflict (contact_id, channel) do update set"
        "   last_active_at  = greatest(conversations.last_active_at, excluded.last_active_at),"
        "   last_inbound_at = greatest(conversations.last_inbound_at, excluded.last_inbound_at),"
        "   number_id       = coalesce(excluded.number_id, conversations.number_id)"
        " returning id", (tenant_id, contact_id, when, when, number_id))
    conversation_id = cur.fetchone()[0]

    msg_type = c.get("msg_type") or "text"
    text = c.get("text") or "[%s]" % msg_type
    wamid = c.get("wamid") or None
    cur.execute(
        "insert into messages (tenant_id, conversation_id, direction, sender, text, channel, wamid,"
        " msg_type, media_id, mime_type, reply_to, raw_payload, message_hash, created_at)"
        " values (%s, %s, 'in', 'customer', %s, 'whatsapp', %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (tenant_id, wamid) where wamid is not null do nothing returning id",
        (tenant_id, conversation_id, text, wamid, msg_type, c.get("media_id") or None,
         c.get("mime_type") or None, c.get("reply_to") or None, Json(event["raw"]), wamid, when))
    stored = cur.fetchone()
    if stored is None:
        return                                          # redelivery of a message we already hold

    if c.get("media_id") and number_id:                 # Meta's link for it expires in minutes: fetch our copy now
        jobs.enqueue(cur, tenant_id, "media_fetch", {"message_id": str(stored[0]), "media_id": c["media_id"], "number_id": str(number_id)},
                     "media_fetch:%s" % stored[0])
    cur.execute("update conversations set message_count = message_count + 1,"
                " substantive_count = substantive_count + %s where id = %s",
                (1 if c.get("text") else 0, conversation_id))
    webhooks_out.emit(cur, tenant_id, "message.received", lambda: {
        "message_id": str(stored[0]), "conversation_id": str(conversation_id), "contact_id": str(contact_id), "wamid": wamid, "type": msg_type,
        "text": (c.get("text") or "")[:1000], "media_id": c.get("media_id"), "received_at": when.isoformat()})
    first_tag = ("ad:%s" % c["referral"]["source_id"]) if (c.get("referral") or {}).get("source_id")         else (c.get("text") or "")[:40] or None
    cur.execute("update contacts set source_first_seen = coalesce(source_first_seen, %s),"
                " first_msg_tag = coalesce(first_msg_tag, %s),"
                " last_seen = greatest(last_seen, %s) where id = %s",
                (source, first_tag, when, contact_id))  # a contact made some other way keeps its own source
    clid = (c.get("referral") or {}).get("ctwa_clid")
    if clid:                                                               # the ad click behind this person: the Conversions API credits it
        cur.execute("update contacts set ctwa_clid = coalesce(ctwa_clid, %s) where id = %s", (str(clid)[:200], contact_id))
    inbox.on_customer_message(cur, tenant_id, conversation_id, when)       # a closed chat reopens
    if msg_type == "flow_reply":                                           # a form was filled in: keep the answers, update the contact
        _safely(cur, "form", lambda: forms.on_reply(cur, tenant_id, contact_id=contact_id, conversation_id=conversation_id, event_raw=event["raw"], wamid=wamid or ""))
    if msg_type == "order":                                                # a cart from the catalogue becomes a draft order
        _safely(cur, "cart", lambda: commerce.on_cart(cur, tenant_id, contact_id=contact_id, conversation_id=conversation_id, number_id=number_id,
                                                     event_raw=event["raw"], wamid=wamid or "", canonical=c))
    keyword = _honour_keywords(cur, tenant_id, contact_id, c, wamid, when)
    campaigns.handle_button_payload(cur, tenant_id, c)       # a campaign's quick-reply button counts as a click
    if keyword is None and msg_type != "reaction":
        # a flow answers first, then an AI agent; the welcome / off-hours message is only for chats a bot did not take
        if not _bots_answer(cur, tenant_id, contact_id, conversation_id, c, when):
            inbox.maybe_auto_reply(cur, tenant_id, contact_id, conversation_id, when, wamid=wamid or "")


def _safely(cur, name: str, work) -> None:
    """Run a side job of a message (a cart, a form answer) so that a bug in it never costs the customer's message, which is already stored."""
    cur.execute("savepoint side_%s" % name)
    try:
        work()
    except Exception:                                          # noqa: BLE001
        log.exception("could not handle a %s", name)
        cur.execute("rollback to savepoint side_%s" % name)
    cur.execute("release savepoint side_%s" % name)


def _bots_answer(cur, tenant_id, contact_id, conversation_id, canonical: dict, when) -> bool:
    """Flows, then the AI agent. Run in a savepoint: a bug or a bad flow must never cost the customer's message
    (the message is already stored; if the bot fails the chat just goes unanswered by it)."""
    cur.execute("select count(*) from messages where conversation_id = %s and direction = 'in'", (conversation_id,))
    first = cur.fetchone()[0] <= 1
    cur.execute("savepoint bots")
    try:
        handled = flow_engine.on_inbound(cur, tenant_id, contact_id=contact_id, conversation_id=conversation_id,
                                         canonical=canonical, when=when, first_message=first)
        if not handled:
            handled = ai_agent.on_inbound(cur, tenant_id, contact_id=contact_id, conversation_id=conversation_id,
                                          canonical=canonical, when=when)
    except Exception:                                          # noqa: BLE001  (see the docstring)
        log.exception("a bot failed on a message for conversation %s", conversation_id)
        cur.execute("rollback to savepoint bots")
        cur.execute("release savepoint bots")
        return False
    cur.execute("release savepoint bots")
    return handled


def _honour_keywords(cur, tenant_id, contact_id, c: dict, wamid, when) -> Optional[str]:
    """STOP and START (and a template's opt-out button) change consent at once, are
    recorded with the exact words, and are answered with a short confirmation."""
    cfg = consent.settings(cur, tenant_id)
    action = consent.classify(c.get("msg_type") or "", c.get("text") or "", c.get("payload_id") or "", cfg)
    if action is None:
        return None
    source = "button" if c.get("msg_type") == "button" else "keyword"
    consent.record(cur, tenant_id, contact_id, action, source, c.get("text") or c.get("payload_id") or "", when)
    if not cfg["confirm"]:
        return action
    try:
        send.enqueue_text(cur, tenant_id, contact_id=contact_id, idem_key="consent:%s" % (wamid or contact_id),
                          text=cfg["opt_out_reply" if action == "opt_out" else "opt_in_reply"],
                          sender="system", now=when)
    except send.SendRefused as why:                      # e.g. the workspace has no number yet
        log.info("no confirmation sent to contact %s: %s", contact_id, why)
    return action


# ── delivery statuses ────────────────────────────────────────────────────────

def on_status(cur, tenant_id, event: dict) -> None:
    c = event["canonical"]
    if ((event.get("raw") or {}).get("status") or {}).get("type") == "payment":           # the result of a payment, not of a message
        payments.on_meta_status(cur, tenant_id, event)
        return
    wamid, new = c.get("wamid"), c.get("status")
    if not wamid or not new:
        raise Skip("status without a message id or state")

    cur.execute("select m.id, m.status, k.id from messages m"
                " join conversations v on v.id = m.conversation_id"
                " join contacts k on k.id = v.contact_id"
                " where m.tenant_id = %s and m.wamid = %s for update of m", (tenant_id, wamid))
    row = cur.fetchone()
    if row is None:
        received = event["received_at"] or utcnow()
        if utcnow() - received < STATUS_PATIENCE:
            raise Defer(DEFER_SECONDS)                  # probably racing the send that made it
        raise Skip("status for a message this platform did not send")
    message_id, current, contact_id = row

    status = next_status(current, new)
    if status == current:
        return
    errors = c.get("errors") or []
    when = event["occurred_at"] or utcnow()
    webhooks_out.emit(cur, tenant_id, "message.status", lambda: {
        "message_id": str(message_id), "contact_id": str(contact_id), "wamid": wamid, "status": status, "at": when.isoformat(),
        "error": (errors[0].get("message") if errors and isinstance(errors[0], dict) else None), "pricing": c.get("pricing")})
    cur.execute(
        "update messages set status = %s, delivered = %s,"
        " read_at = case when %s = 'read' then coalesce(read_at, %s) else read_at end,"
        " error = case when %s = 'failed' then %s else error end,"
        " pricing = coalesce(%s, pricing) where id = %s",
        (status, status in ("delivered", "read"), status, when, status,
         Json(errors[0]) if errors else None, Json(c["pricing"]) if c.get("pricing") else None, message_id))

    if status == "failed":
        if any(int(e.get("code", 0) or 0) == OPTED_OUT_CODE for e in errors):
            consent.record(cur, tenant_id, contact_id, "opt_out", "meta_131050",
                           "Meta reported the recipient has opted out of business messages", when)
        cur.execute("select campaign_id from outbox where tenant_id = %s and wamid = %s", (tenant_id, wamid))
        sent = cur.fetchone()
        if sent and sent[0]:
            cur.execute("update campaigns set sent = greatest(sent - 1, 0), failed = failed + 1"
                        " where id = %s", (sent[0],))
            cur.execute("update campaign_recipients set status = 'failed' where outbox_id ="
                        " (select id from outbox where tenant_id = %s and wamid = %s)", (tenant_id, wamid))


EVENT_HANDLERS = {"message": on_message, "status": on_status}
JOB_HANDLERS = {"ingest_event": handle_event_job, "launch_campaign": campaigns.handle_launch_job,
                "abandoned_cart": integrations.handle_cart_job, "flow_timeout": flow_engine.handle_timeout_job,
                "ai_reply": ai_agent.handle_reply_job, "media_fetch": media.handle_fetch_job, "ai_crawl": ai_knowledge.handle_crawl_job,
                "ai_refresh": ai_knowledge.handle_refresh_job, "ai_embed": ai_knowledge.handle_embed_job}

from prism_wa import templates as _templates                    # noqa: E402  (after the registry it extends)
EVENT_HANDLERS.update(_templates.EVENT_HANDLERS)
EVENT_HANDLERS.update(coexistence.EVENT_HANDLERS)
