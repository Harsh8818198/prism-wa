"""The send engine (spec WS3): everything outbound goes through `outbox`.

Enqueue is the only way a message leaves the platform — a human reply, a
broadcast, a bot, the API. It checks the rules (24-hour window, opt-out,
which number), writes the message the inbox will show, and queues the send.
A worker (`send_due`) then delivers, at the number's rate, and records exactly
what Meta answered.

At-most-once. An HTTP call cannot be rolled back, so a row is marked `sending`
and committed BEFORE the call. If the process dies in between, the row stays
`sending` and `reap_stuck` closes it as `unknown_outcome` instead of resending:
the message may well have gone, and sending it twice to a customer is worse than
asking a person to look. The status webhook can still advance such a message.

Errors are classified (Meta's table, spec WS3):
  retry           backoff, up to MAX_ATTEMPTS (network, 5xx, throughput,
                  pair-rate, "ecosystem engagement" waits 24 h)
  needs_template  131047 — the 24-hour window closed; a template is required
  opted_out       131050 — the customer opted out; the contact is flagged
  fix_setup       a template / number / token problem only an admin can fix
  fatal           anything else Meta refused for good
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from psycopg2.extras import Json

from prism_wa import consent, plans, templates
from prism_wa import media
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault, VaultError

log = logging.getLogger("prism_wa.send")

WINDOW = timedelta(hours=24)
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 30, 120, 600, 3600)

RETRY_CODES = {1, 2, 4, 80007, 130429, 131000, 131016, 131049, 131056, 131057, 133004, 2494100}
NEEDS_TEMPLATE_CODES = {131047}
OPTED_OUT_CODES = {131050}
FIX_SETUP_CODES = set(range(132000, 132017)) | {130403, 190, 368}
# Waits that are not the default ladder: pair-rate is short, throughput shorter,
# and Meta's "healthy ecosystem" refusal is not worth trying again for a day.
SPECIAL_BACKOFF = {130429: 5, 131056: 60, 131049: 86400}


class SendRefused(Exception):
    """A message that must not be queued. `reason` is machine-readable so a UI can react."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def classify(error: GraphError) -> str:
    code = error.code
    if code in NEEDS_TEMPLATE_CODES:
        return "needs_template"
    if code in OPTED_OUT_CODES:
        return "opted_out"
    if code in FIX_SETUP_CODES:
        return "fix_setup"
    if code in RETRY_CODES or error.http_status == 0 or error.http_status >= 500 or error.http_status == 429:
        return "retry"
    return "fatal"


def backoff_seconds(error: GraphError, attempts: int) -> float:
    if error.code in SPECIAL_BACKOFF:
        return SPECIAL_BACKOFF[error.code]
    return BACKOFF_SECONDS[min(max(attempts, 1) - 1, len(BACKOFF_SECONDS) - 1)]


# ── enqueue ──────────────────────────────────────────────────────────────────

def _recipient(cur, tenant_id, contact_id) -> dict:
    cur.execute("select id, phone, bsuid, opt_out from contacts where id = %s and tenant_id = %s",
                (contact_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise SendRefused("unknown_contact", "that contact is not in this workspace")
    c = dict(zip(("id", "phone", "bsuid", "opt_out"), row))
    digits = "".join(ch for ch in (c["phone"] or "") if ch.isdigit())
    if not digits and not c["bsuid"]:
        raise SendRefused("no_recipient", "this contact has no phone number or WhatsApp id")
    c["digits"] = digits
    return c


def _number_for(cur, tenant_id, conversation, number_id):
    if number_id:
        cur.execute("select id from phone_numbers where id = %s and tenant_id = %s", (number_id, tenant_id))
        if cur.fetchone() is None:
            raise SendRefused("no_number", "that number is not in this workspace")
        return number_id
    if conversation and conversation["number_id"]:
        return conversation["number_id"]
    cur.execute("select id from phone_numbers where tenant_id = %s limit 2", (tenant_id,))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0][0]
    raise SendRefused("no_number", "this workspace has no WhatsApp number" if not rows
                      else "this workspace has several numbers; say which one to send from")


def _enqueue(cur, tenant_id, *, contact_id, kind, payload, idem_key, preview, free_form,
             number_id=None, campaign_id=None, sender="agent", now=None, precheck=None, run_at=None):
    now = now or utcnow()
    cur.execute("select id, message_id from outbox where tenant_id = %s and idem_key = %s", (tenant_id, idem_key))
    existing = cur.fetchone()
    if existing:
        return existing[0]                                   # the same request again: nothing new

    contact = _recipient(cur, tenant_id, contact_id)
    cur.execute("select id, last_inbound_at, number_id from conversations"
                " where tenant_id = %s and contact_id = %s and channel = 'whatsapp'", (tenant_id, contact_id))
    row = cur.fetchone()
    conversation = dict(zip(("id", "last_inbound_at", "number_id"), row)) if row else None

    if free_form:
        last = conversation["last_inbound_at"] if conversation else None
        if last is None or now - last >= WINDOW:
            raise SendRefused("window_closed", "the 24-hour window is closed; send an approved template")
    elif contact["opt_out"]:
        raise SendRefused("opted_out", "this contact has opted out of business-initiated messages")

    number = _number_for(cur, tenant_id, conversation, number_id)
    if precheck:
        precheck(cur, number)

    cur.execute(
        "insert into conversations (tenant_id, contact_id, channel, last_active_at, number_id)"
        " values (%s, %s, 'whatsapp', %s, %s)"
        " on conflict (contact_id, channel) do update set"
        "   last_active_at = greatest(conversations.last_active_at, excluded.last_active_at),"
        "   number_id = coalesce(conversations.number_id, excluded.number_id)"
        " returning id", (tenant_id, contact_id, now, number))
    conversation_id = cur.fetchone()[0]
    cur.execute(
        "insert into messages (tenant_id, conversation_id, direction, sender, text, channel, msg_type,"
        " status, client_ref, created_at) values (%s, %s, 'out', %s, %s, 'whatsapp', %s, 'queued', %s, %s)"
        " returning id", (tenant_id, conversation_id, sender, preview, payload.get("type", "text"), idem_key, now))
    message_id = cur.fetchone()[0]
    if kind == "media":                                      # the thread shows the file this message carries
        carried = payload.get(payload.get("type")) or {}
        cur.execute("update messages set media_asset_id = %s, media_id = %s where id = %s",
                    (carried.get("asset_id"), carried.get("id"), message_id))
    cur.execute("update conversations set message_count = message_count + 1 where id = %s", (conversation_id,))
    cur.execute(
        "insert into outbox (tenant_id, number_id, to_phone, to_bsuid, kind, payload, idem_key, message_id,"
        " campaign_id, run_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (tenant_id, number, contact["digits"] or None, None if contact["digits"] else contact["bsuid"],
         kind, Json(payload), idem_key, message_id, campaign_id, run_at or now))
    return cur.fetchone()[0]


def enqueue_text(cur, tenant_id, *, contact_id, text: str, idem_key: str, number_id=None,
                 sender: str = "agent", now: Optional[datetime] = None):
    """A free-form reply. Only allowed inside the 24 hours after the customer last wrote."""
    if not (text or "").strip():
        raise SendRefused("empty", "there is nothing to send")
    return _enqueue(cur, tenant_id, contact_id=contact_id, kind="text",
                    payload={"type": "text", "text": {"body": text}}, idem_key=idem_key,
                    preview=text, free_form=True, number_id=number_id, sender=sender, now=now)


def enqueue_interactive(cur, tenant_id, *, contact_id, interactive: dict, preview: str, idem_key: str, number_id=None,
                        sender: str = "bot", now: Optional[datetime] = None):
    """Reply buttons or a list. Like free text, only inside the 24 hours after the customer last wrote."""
    return _enqueue(cur, tenant_id, contact_id=contact_id, kind="interactive",
                    payload={"type": "interactive", "interactive": interactive}, idem_key=idem_key,
                    preview=preview, free_form=True, number_id=number_id, sender=sender, now=now)


def enqueue_template(cur, tenant_id, *, contact_id, name: str, language: str = "en_US",
                     components: Optional[list] = None, idem_key: str, number_id=None,
                     campaign_id=None, sender: str = "agent", now: Optional[datetime] = None,
                     run_at: Optional[datetime] = None):
    """An approved template; the only thing that may start a conversation. `run_at` holds it until then."""
    if not (name or "").strip():
        raise SendRefused("empty", "a template needs a name")
    template = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components

    def approved(cur, number):
        # Known and not approved (pending, rejected, paused, disabled) -> refuse here, not at Meta.
        # A template we have never seen is let through: it may predate our copy, and Meta decides.
        status = templates.sendable_status(cur, number, name, language)
        if status is not None and status != "APPROVED":
            raise SendRefused("template_not_approved",
                              "the template \"%s\" is %s, so it cannot be sent" % (name, status.lower()))

    return _enqueue(cur, tenant_id, contact_id=contact_id, kind="template",
                    payload={"type": "template", "template": template}, idem_key=idem_key,
                    preview="[template] %s" % name, free_form=False, number_id=number_id,
                    campaign_id=campaign_id, sender=sender, now=now, precheck=approved, run_at=run_at)


def enqueue_location(cur, tenant_id, *, contact_id, latitude, longitude, name: str = "", address: str = "", idem_key: str, number_id=None,
                     sender: str = "bot", now: Optional[datetime] = None):
    """A pin on a map, inside the 24-hour window (it is sent as a media-kind message: nothing to upload, nothing to store)."""
    loc = {"latitude": float(latitude), "longitude": float(longitude)}
    if name:
        loc["name"] = name[:100]
    if address:
        loc["address"] = address[:200]
    return _enqueue(cur, tenant_id, contact_id=contact_id, kind="media", payload={"type": "location", "location": loc}, idem_key=idem_key,
                    preview="[location] %s" % (name or address or "%s,%s" % (latitude, longitude)), free_form=True, number_id=number_id, sender=sender, now=now)


def attribute(cur, outbox_id, agent_id) -> None:
    """Record which teammate sent a message, so the team report can count it."""
    if agent_id:
        cur.execute("update messages set sent_by = %s where id = (select message_id from outbox where id = %s)", (agent_id, outbox_id))


MEDIA_KINDS = ("image", "video", "audio", "document", "sticker")


def enqueue_media(cur, tenant_id, *, contact_id, kind: str, idem_key: str, asset_id=None, wa_media_id: str = "", link: str = "", caption: str = "",
                  filename: str = "", number_id=None, sender: str = "agent", now: Optional[datetime] = None):
    """A picture, video, voice note or document, inside the 24-hour window. Name the file one of three ways: `asset_id` (our Media
    Library: uploaded to Meta when it goes out), `wa_media_id` (Meta already has it) or `link` (a public address)."""
    if kind not in MEDIA_KINDS:
        raise SendRefused("bad_kind", "a media message is one of: %s" % ", ".join(MEDIA_KINDS))
    ref = ({"asset_id": str(asset_id)} if asset_id else {"id": wa_media_id} if wa_media_id else {"link": link} if link else None)
    if ref is None:
        raise SendRefused("empty", "say which file to send")
    body = dict(ref)
    if caption and kind in ("image", "video", "document"):
        body["caption"] = caption[:1024]
    if kind == "document" and filename:
        body["filename"] = filename[:240]
    return _enqueue(cur, tenant_id, contact_id=contact_id, kind="media", payload={"type": kind, kind: body}, idem_key=idem_key,
                    preview=("[%s] %s" % (kind, caption)).strip(), free_form=True, number_id=number_id, sender=sender, now=now)


# ── rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """A token bucket per number: `rate` messages a second, a one-second burst."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets = {}

    def allow(self, key, rate: float) -> bool:
        rate = max(float(rate or 0), 1.0)
        now = self._clock()
        tokens, stamp = self._buckets.get(key, (rate, now))
        tokens = min(rate, tokens + (now - stamp) * rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False


# ── delivery ─────────────────────────────────────────────────────────────────

@dataclass
class SendStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    throttled: int = 0


def _body(item: dict) -> dict:
    body = {"messaging_product": "whatsapp", "recipient_type": "individual"}
    if item["to_phone"]:
        body["to"] = item["to_phone"]
    else:
        body["recipient"] = item["to_bsuid"]
    body.update(item["payload"])
    return body


def send_due(connect: Callable, *, graph: Graph, vault: Vault, limiter: Optional[RateLimiter] = None,
             now: Optional[datetime] = None, batch: int = 50, concurrency: int = 8) -> SendStats:
    """Deliver due outbox rows. Claiming and marking `sending` is one committed
    transaction; the HTTP calls and their results are recorded afterwards."""
    now = now or utcnow()
    limiter = limiter or RateLimiter()
    stats = SendStats()

    conn = connect()
    items = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select o.id, o.tenant_id, o.number_id, o.to_phone, o.to_bsuid, o.payload, o.attempts,"
                " o.message_id, o.campaign_id, p.phone_number_id, p.mps_limit, w.waba_id, w.token_enc, k.retry,"
                " (select ts.settings->>'plan' from tenant_settings ts where ts.tenant_id = o.tenant_id) as plan"
                " from outbox o join phone_numbers p on p.id = o.number_id join wabas w on w.id = p.waba_id"
                " left join campaigns k on k.id = o.campaign_id"
                " where o.state = 'queued' and o.run_at <= %s"
                # a paused or cancelled campaign's messages are left alone: that is how pause and cancel take
                # effect within one batch
                " and (o.campaign_id is null or k.status not in ('paused', 'cancelled'))"
                " order by o.run_at, o.created_at limit %s for update of o skip locked", (now, batch))
            cols = ("id", "tenant_id", "number_id", "to_phone", "to_bsuid", "payload", "attempts",
                    "message_id", "campaign_id", "phone_number_id", "mps_limit", "waba_id", "token_enc", "retry", "plan")
            for row in cur.fetchall():
                item = dict(zip(cols, row))
                ceiling = plans.LIMITS[plans.normalise(item["plan"])]["mps"]
                rate = min(item["mps_limit"] or 80, ceiling) if ceiling else item["mps_limit"]
                if not limiter.allow(item["number_id"], rate):
                    stats.throttled += 1
                    continue
                items.append(item)
            if items:
                cur.execute("update outbox set state = 'sending', attempts = attempts + 1, updated_at = %s"
                            " where id = any(%s::uuid[])", (now, [str(i["id"]) for i in items]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    stats.claimed = len(items)
    if not items:
        return stats

    def deliver(item: dict) -> str:
        attempts = item["attempts"] + 1
        try:
            token = vault.decrypt(item["token_enc"], context=item["waba_id"])
        except VaultError as exc:
            return _record_failure(connect, item, attempts, now,
                                   GraphError("no usable token for this number: %s" % exc, code=190), "fix_setup")
        try:
            if media.has_assets(item["payload"]):                  # a file of ours: give Meta a copy first (once per number)
                item = dict(item, payload=media.resolve_payload(connect, item, token, graph=graph))
            reply = graph.send_message(token, item["phone_number_id"], _body(item))
        except GraphError as exc:
            return _record_failure(connect, item, attempts, now, exc, classify(exc))
        wamid = ((reply.get("messages") or [{}])[0] or {}).get("id")
        if not wamid:
            return _record_failure(connect, item, attempts, now,
                                   GraphError("Meta accepted the call but returned no message id"), "fatal")
        _record_sent(connect, item, wamid, now)
        return "sent"

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(items)))) as pool:
        for outcome in pool.map(deliver, items):
            if outcome == "sent":
                stats.sent += 1
            elif outcome == "retry":
                stats.retried += 1
            else:
                stats.failed += 1
    return stats


def _record_sent(connect, item: dict, wamid: str, now: datetime) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("update outbox set state = 'sent', wamid = %s, error = null, error_class = null,"
                        " updated_at = %s where id = %s", (wamid, now, item["id"]))
            cur.execute("update messages set wamid = %s, status = case when status in ('delivered', 'read')"
                        " then status else 'sent' end where id = %s", (wamid, item["message_id"]))
            if item["campaign_id"]:
                cur.execute("update campaign_recipients set status = 'sent' where outbox_id = %s", (item["id"],))
                cur.execute("update campaigns set sent = sent + 1 where id = %s", (item["campaign_id"],))
                _close_campaign_if_done(cur, item["campaign_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _retry_policy(item: dict) -> tuple:
    """(attempts allowed in all, delay in seconds or None for the default ladder). A campaign chooses
    its own: up to three retries, each after a wait of up to a day."""
    policy = item.get("retry") if item.get("campaign_id") else None
    if not isinstance(policy, dict):
        return MAX_ATTEMPTS, None
    retries = max(0, min(int(policy.get("max", MAX_ATTEMPTS - 1)), 3))
    delay = max(1, min(int(policy.get("delay_minutes", 30)), 24 * 60)) * 60
    return 1 + retries, delay


def _record_failure(connect, item: dict, attempts: int, now: datetime, error: GraphError, error_class: str) -> str:
    allowed, delay = _retry_policy(item)
    retry = error_class == "retry" and attempts < allowed
    conn = connect()
    try:
        with conn.cursor() as cur:
            if retry:
                cur.execute("update outbox set state = 'queued', error_class = 'retry', error = %s, run_at = %s,"
                            " updated_at = %s where id = %s",
                            (Json(error.as_dict()),
                             now + timedelta(seconds=delay if delay and error.code not in SPECIAL_BACKOFF
                                             else backoff_seconds(error, attempts)),
                             now, item["id"]))
            else:
                cur.execute("update outbox set state = 'failed', error_class = %s, error = %s, updated_at = %s"
                            " where id = %s", (error_class, Json(error.as_dict()), now, item["id"]))
                cur.execute("update messages set status = 'failed', error = %s where id = %s",
                            (Json(error.as_dict()), item["message_id"]))
                if error_class == "opted_out":
                    cur.execute("select v.contact_id from messages m join conversations v on v.id = m.conversation_id"
                                " where m.id = %s", (item["message_id"],))
                    row = cur.fetchone()
                    if row:
                        consent.record(cur, item["tenant_id"], row[0], "opt_out", "meta_131050",
                                       "Meta refused the send: the recipient has opted out", now)
                if item["campaign_id"]:
                    cur.execute("update campaign_recipients set status = 'failed' where outbox_id = %s", (item["id"],))
                    cur.execute("update campaigns set failed = failed + 1 where id = %s", (item["campaign_id"],))
                    _close_campaign_if_done(cur, item["campaign_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info("send %s %s: %s", item["id"], "will retry" if retry else "failed", error.message)
    return "retry" if retry else "failed"


def _close_campaign_if_done(cur, campaign_id) -> None:
    cur.execute("update campaigns set status = 'done', finished_at = now() where id = %s and status = 'sending' and kind = 'broadcast'"
                " and sent + failed >= total returning tenant_id, name, total, sent, failed", (campaign_id,))
    row = cur.fetchone()
    if row:
        from prism_wa import webhooks_out
        webhooks_out.emit(cur, row[0], "campaign.completed", {"campaign_id": str(campaign_id), "name": row[1], "total": row[2], "sent": row[3], "failed": row[4]})


def reap_stuck(connect: Callable, *, older_than: timedelta = timedelta(minutes=10),
               now: Optional[datetime] = None) -> int:
    """Close rows left `sending` by a crashed worker. They are NOT resent."""
    now = now or utcnow()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("update outbox set state = 'failed', error_class = 'unknown_outcome',"
                        " error = %s, updated_at = %s where state = 'sending' and updated_at < %s"
                        " returning message_id",
                        (Json({"message": "the worker stopped mid-send; the message may or may not have gone"}),
                         now, now - older_than))
            rows = cur.fetchall()
            ids = [r[0] for r in rows if r[0]]
            if ids:
                cur.execute("update messages set status = 'unknown',"
                            " error = %s where id = any(%s::uuid[]) and status = 'queued'",
                            (Json({"message": "outcome unknown"}), [str(i) for i in ids]))
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
