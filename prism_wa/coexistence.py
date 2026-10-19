"""Coexistence: one number used both in the WhatsApp Business app on the owner's phone and through Prism.

What changes for the workspace
  - the number is connected without being re-registered (it already lives on the phone), by choosing "Keep using the WhatsApp Business app" when connecting;
  - a message the team types in the phone app arrives here as an *echo* and appears in the chat as sent from the phone;
  - within 24 hours of connecting, Meta can send the phone's contacts and up to six months of chats; they are filed quietly (no bots, no auto-replies, no campaign counting);
  - what Meta sends comes in three webhook fields: smb_message_echoes, history and smb_app_state_sync.

The shapes below follow Meta's coexistence documentation as read in October 2026. They have not been seen on a live number.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import Json

from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.coexistence")

HISTORY_STATUS = {"DELIVERED": "delivered", "READ": "read", "PLAYED": "read", "SENT": "sent", "ERROR": "failed", "PENDING": "queued"}


class CoexistenceError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def numbers(cur, tenant_id) -> list:
    cur.execute("select id, phone_number_id, display, status, coexistence, coexistence_state from phone_numbers where tenant_id = %s order by created_at", (tenant_id,))
    return [dict(zip(("id", "phone_number_id", "display", "status", "coexistence", "state"), r)) for r in cur.fetchall()]


def mark(cur, tenant_id, number_id, enabled: bool = True) -> None:
    cur.execute("update phone_numbers set coexistence = %s, coexistence_state = case when %s then coalesce(coexistence_state, '{}'::jsonb) else null end where id = %s and tenant_id = %s",
                (enabled, enabled, number_id, tenant_id))
    if cur.rowcount == 0:
        raise CoexistenceError("unknown_number", "that number is not in this workspace")


def _state(cur, number_id, **changes) -> None:
    cur.execute("update phone_numbers set coexistence_state = coalesce(coexistence_state, '{}'::jsonb) || %s where id = %s", (Json(changes), number_id))


def request_sync(cur, tenant_id, graph, vault: Vault, number_id) -> dict:
    """Ask Meta for the phone's contacts and chats. Only works in the 24 hours after connecting; Meta's refusal is passed on in words."""
    cur.execute("select w.token_enc, w.waba_id, p.phone_number_id, p.coexistence from phone_numbers p join wabas w on w.id = p.waba_id where p.id = %s and p.tenant_id = %s", (number_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CoexistenceError("unknown_number", "that number is not in this workspace")
    if not row[3]:
        raise CoexistenceError("not_coexistence", "this number was not connected alongside the WhatsApp Business app")
    token = vault.decrypt(row[0], context=row[1])
    out, now = {}, utcnow().isoformat()
    for kind, key in (("smb_app_state_sync", "contacts"), ("history", "history")):
        try:
            graph.request_app_sync(token, row[2], kind)
            out[key] = "requested"
        except Exception as why:                                          # noqa: BLE001
            out[key] = "refused: %s" % (getattr(why, "details", "") or getattr(why, "message", "") or why)[:200]
    _state(cur, number_id, requested_at=now, contacts=out["contacts"], history_request=out["history"])
    return out


def _number(cur, tenant_id, phone_number_id: str):
    cur.execute("select id from phone_numbers where tenant_id = %s and phone_number_id = %s", (tenant_id, phone_number_id))
    row = cur.fetchone()
    return row[0] if row else None


def _contact(cur, tenant_id, wa_id: str, name: Optional[str] = None):
    cur.execute("select contact_id from wa_resolve_contact(%s, %s, %s, %s, %s, %s, %s)", (tenant_id, wa_id, None, None, None, name, "whatsapp_app"))
    return cur.fetchone()[0]


def _conversation(cur, tenant_id, contact_id, number_id, when, inbound: bool):
    cur.execute("insert into conversations (tenant_id, contact_id, channel, last_active_at, last_inbound_at, number_id) values (%s, %s, 'whatsapp', %s, %s, %s)"
                " on conflict (contact_id, channel) do update set last_active_at = greatest(conversations.last_active_at, excluded.last_active_at),"
                " last_inbound_at = greatest(conversations.last_inbound_at, excluded.last_inbound_at), number_id = coalesce(conversations.number_id, excluded.number_id) returning id",
                (tenant_id, contact_id, when, when if inbound else None, number_id))
    return cur.fetchone()[0]


def _when(ts) -> datetime:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return utcnow()


def _words(msg: dict) -> tuple:
    """(type, text) of a message in an echo or a history chunk."""
    kind = str(msg.get("type") or "text")
    body = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
    if kind == "text":
        return kind, str(body.get("body") or "")
    return kind, str(body.get("caption") or body.get("filename") or "[%s]" % kind)


def _store(cur, tenant_id, conversation_id, *, direction: str, sender: str, msg: dict, status: str, when) -> bool:
    kind, text = _words(msg)
    cur.execute("insert into messages (tenant_id, conversation_id, direction, sender, text, channel, wamid, msg_type, status, delivered, created_at) values"
                " (%s, %s, %s, %s, %s, 'whatsapp', %s, %s, %s, %s, %s) on conflict (tenant_id, wamid) where wamid is not null do nothing returning id",
                (tenant_id, conversation_id, direction, sender, text or "[%s]" % kind, msg.get("id"), kind, status, status in ("delivered", "read"), when))
    if cur.fetchone() is None:
        return False
    cur.execute("update conversations set message_count = message_count + 1 where id = %s", (conversation_id,))
    return True


# ── Meta's three webhook fields ──────────────────────────────────────────────

def on_echo(cur, tenant_id, event: dict) -> None:
    """Messages the team sent from the phone app."""
    value = (event["raw"] or {}).get("value") or {}
    number_id = _number(cur, tenant_id, event["phone_number_id"])
    for echo in value.get("message_echoes") or []:
        if not isinstance(echo, dict) or not echo.get("to"):
            continue
        when = _when(echo.get("timestamp"))
        contact = _contact(cur, tenant_id, str(echo["to"]))
        convo = _conversation(cur, tenant_id, contact, number_id, when, inbound=False)
        _store(cur, tenant_id, convo, direction="out", sender="phone", msg=echo, status="sent", when=when)
    if number_id:
        _state(cur, number_id, last_echo_at=utcnow().isoformat())


def on_history(cur, tenant_id, event: dict) -> None:
    """A chunk of the phone's past chats. Filed as history: nothing is answered, counted or sent because of it."""
    value = (event["raw"] or {}).get("value") or {}
    number_id = _number(cur, tenant_id, event["phone_number_id"])
    stored = chats = 0
    progress, phase, declined = None, None, False
    for chunk in value.get("history") or []:
        if not isinstance(chunk, dict):
            continue
        for err in chunk.get("errors") or []:
            declined = True
            log.info("history sync for %s not available: %s", event["phone_number_id"], (err or {}).get("message") if isinstance(err, dict) else err)
        meta = chunk.get("metadata") or {}
        progress, phase = meta.get("progress", progress), meta.get("phase", phase)
        for thread in chunk.get("threads") or []:
            who = str((thread or {}).get("id") or "")
            if not who:
                continue
            contact = _contact(cur, tenant_id, who)
            convo = None
            chats += 1
            for msg in thread.get("messages") or []:
                if not isinstance(msg, dict) or not msg.get("id"):
                    continue
                incoming = str(msg.get("from") or "") == who
                when = _when(msg.get("timestamp"))
                convo = _conversation(cur, tenant_id, contact, number_id, when, inbound=incoming)
                status = HISTORY_STATUS.get(str((msg.get("history_context") or {}).get("status") or "").upper(), "sent")
                stored += 1 if _store(cur, tenant_id, convo, direction="in" if incoming else "out", sender="customer" if incoming else "phone", msg=msg, status=None if incoming else status,
                                      when=when) else 0
    if number_id:
        cur.execute("select coexistence_state from phone_numbers where id = %s", (number_id,))
        was = (cur.fetchone()[0] or {}).get("history_messages", 0)
        _state(cur, number_id, history="declined" if declined and not stored else "importing" if (progress or 0) < 100 else "done", history_progress=progress, history_phase=phase,
               history_messages=int(was) + stored, history_updated_at=utcnow().isoformat())


def on_app_state(cur, tenant_id, event: dict) -> None:
    """Contacts saved on the phone."""
    value = (event["raw"] or {}).get("value") or {}
    number_id = _number(cur, tenant_id, event["phone_number_id"])
    n = 0
    for item in value.get("state_sync") or []:
        if not isinstance(item, dict) or item.get("type") != "contact" or item.get("action") not in ("add", "edit"):
            continue
        c = item.get("contact") or {}
        phone = "".join(ch for ch in str(c.get("phone_number") or "") if ch.isdigit())
        if not phone:
            continue
        name = str(c.get("full_name") or c.get("first_name") or "").strip()[:200] or None
        contact = _contact(cur, tenant_id, phone, name)
        if name:
            cur.execute("update contacts set name = %s where id = %s and (name is null or name = '' or name = phone)", (name, contact))
        n += 1
    if number_id and n:
        cur.execute("select coexistence_state from phone_numbers where id = %s", (number_id,))
        was = int((cur.fetchone()[0] or {}).get("contacts_imported", 0))
        _state(cur, number_id, contacts="done", contacts_imported=was + n)


EVENT_HANDLERS = {"echo": on_echo, "history": on_history, "app_state": on_app_state}
