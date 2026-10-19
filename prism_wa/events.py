"""Meta's webhook payload -> canonical events (spec WS2 "normaliser").

One POST from Meta can carry many things: several inbound messages, delivery
statuses, template approvals, a quality change. `normalise` turns a payload
into a flat list of `Event`s, each with a `dedupe_key` so a redelivery of the
same thing is recognised, and `raw` holding exactly what Meta sent for it.

Identity. Since 2026 a sender may be identified only by a business-scoped user
id (`from_user_id` / `contacts[].user_id`, format `IN.<alnum>`), and once a user
adopts a username the phone number (`from` / `wa_id`) is omitted. So `wa_id`
and `bsuid` are both optional here and a consumer must cope with either, or
both, being empty. A username is a display attribute, never a key.

Tolerance. The payload shapes below follow Meta's documentation and the spec
(§1.2). Nothing here may raise on an unexpected shape: one odd message must not
cost the other messages in the same delivery. Fields Meta adds later land in
`raw` untouched. Spec §9 item 4 (exact status shape under the 2026 pricing
model) is still to be checked against Meta's own sample payloads.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

OBJECT = "whatsapp_business_account"

# A change's `field` -> the kind of event it becomes. Anything not listed is
# kept as kind "other" (stored, never dropped).
_FIELD_KINDS = {
    "message_template_status_update": "template_status",
    "message_template_quality_update": "template_quality",
    "template_category_update": "template_category",
    "phone_number_quality_update": "phone_quality",
    "phone_number_name_update": "phone_name",
    "account_update": "account_update",
    "account_alerts": "account_update",
    "account_review_update": "account_update",
    "smb_message_echoes": "echo",                  # coexistence: a message typed in the WhatsApp Business app on the phone
    "history": "history",                          # coexistence: past chats, in chunks
    "smb_app_state_sync": "app_state",             # coexistence: contacts saved on the phone
}

_MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


@dataclass(frozen=True)
class Event:
    kind: str                       # message | status | template_status | ... | other
    dedupe_key: str
    waba_id: str
    phone_number_id: str
    occurred_at: Optional[datetime]
    raw: dict
    wamid: str = ""
    # who it concerns
    wa_id: str = ""                 # phone number, digits only; absent once a user adopts a username
    bsuid: str = ""                 # business-scoped user id
    parent_bsuid: str = ""
    username: str = ""              # display only
    profile_name: str = ""
    # an inbound message
    msg_type: str = ""              # text image video audio document sticker location reaction
                                    # button_reply list_reply flow_reply button order ...
    text: str = ""                  # the words, or the reply's title, or a caption
    payload_id: str = ""            # button / list / quick-reply id
    media_id: str = ""
    mime_type: str = ""
    location: dict = field(default_factory=dict)    # a shared location: latitude, longitude, name, address
    reply_to: str = ""              # wamid this answers or reacts to
    referral: dict = field(default_factory=dict)    # click-to-WhatsApp ad: ctwa_clid, source_id, ...
    # a delivery status (and template / phone events, whose `event` goes here)
    status: str = ""
    errors: tuple = ()
    pricing: dict = field(default_factory=dict)
    conversation: dict = field(default_factory=dict)

    def canonical(self) -> dict:
        """The normalised fields as plain JSON (everything except `raw`)."""
        out = {}
        for key in self.__dataclass_fields__:
            if key == "raw":
                continue
            value = getattr(self, key)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = list(value)
            out[key] = value
        return out


# ── small tolerant helpers ───────────────────────────────────────────────────

def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def _when(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def digest(obj: Any) -> str:
    """A stable short hash of any JSON-ish value (dedupe keys for changes)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ── the payload ──────────────────────────────────────────────────────────────

def normalise(payload: Any) -> list:
    """A webhook payload -> [Event]. Never raises; a payload that is not a
    WhatsApp Business Account notification yields []."""
    payload = _dict(payload)
    if payload.get("object") != OBJECT:
        return []
    events = []
    for entry in _list(payload.get("entry")):
        entry = _dict(entry)
        waba = _str(entry.get("id"))
        for change in _list(entry.get("changes")):
            change = _dict(change)
            name = _str(change.get("field"))
            value = _dict(change.get("value"))
            if name == "messages":
                events.extend(_from_messages(waba, value))
            else:
                events.append(_from_account_change(waba, name, value, entry.get("time")))
    return events


def _from_messages(waba: str, value: dict) -> list:
    meta = _dict(value.get("metadata"))
    pnid = _str(meta.get("phone_number_id"))
    contacts = [_dict(c) for c in _list(value.get("contacts"))]
    out = []
    for m in _list(value.get("messages")):
        out.append(_message(waba, pnid, meta, contacts, _dict(m)))
    for s in _list(value.get("statuses")):
        out.append(_status(waba, pnid, meta, _dict(s)))
    return out


def _contact_for(contacts: list, wa_id: str, bsuid: str) -> dict:
    for c in contacts:
        if (wa_id and _str(c.get("wa_id")) == wa_id) or (bsuid and _str(c.get("user_id")) == bsuid):
            return c
    return contacts[0] if len(contacts) == 1 else {}


def _message(waba: str, pnid: str, meta: dict, contacts: list, m: dict) -> Event:
    wamid = _str(m.get("id"))
    frm = _str(m.get("from"))
    uid = _str(m.get("from_user_id"))
    contact = _contact_for(contacts, frm, uid)
    profile = _dict(contact.get("profile"))

    mtype = _str(m.get("type"))
    body = _dict(m.get(mtype))
    text = payload_id = media_id = mime = reply_to = ""
    where: dict = {}

    if mtype == "text":
        text = _str(body.get("body"))
    elif mtype == "interactive":
        sub = _str(body.get("type"))
        reply = _dict(body.get(sub))
        if sub in ("button_reply", "list_reply"):
            mtype = sub
            payload_id, text = _str(reply.get("id")), _str(reply.get("title"))
        elif sub == "nfm_reply":                    # a WhatsApp Flow's answers (JSON stays in raw)
            mtype = "flow_reply"
            payload_id, text = _str(reply.get("name")), _str(reply.get("body"))
    elif mtype == "button":                         # a template quick-reply button
        payload_id, text = _str(body.get("payload")), _str(body.get("text"))
    elif mtype in _MEDIA_TYPES:
        media_id, mime = _str(body.get("id")), _str(body.get("mime_type"))
        text = _str(body.get("caption") or body.get("filename"))
    elif mtype == "location":
        text = " ".join(x for x in (_str(body.get("name")), _str(body.get("address"))) if x) \
            or "%s,%s" % (_str(body.get("latitude")), _str(body.get("longitude")))
        where = {"latitude": body.get("latitude"), "longitude": body.get("longitude"), "name": _str(body.get("name")), "address": _str(body.get("address"))}
    elif mtype == "reaction":
        text, reply_to = _str(body.get("emoji")), _str(body.get("message_id"))
    elif mtype == "order":
        text = _str(body.get("text"))
    reply_to = reply_to or _str(_dict(m.get("context")).get("id"))

    return Event(
        kind="message",
        dedupe_key="msg:" + (wamid or digest(m)),
        waba_id=waba, phone_number_id=pnid,
        occurred_at=_when(m.get("timestamp")),
        raw={"field": "messages", "metadata": meta, "contacts": contacts, "message": m},
        wamid=wamid,
        wa_id=frm or _str(contact.get("wa_id")),
        bsuid=uid or _str(contact.get("user_id")),
        parent_bsuid=_str(contact.get("parent_user_id")),
        username=_str(profile.get("username")),
        profile_name=_str(profile.get("name")),
        msg_type=mtype, text=text, payload_id=payload_id, media_id=media_id,
        mime_type=mime, reply_to=reply_to, location=where,
        referral=_dict(m.get("referral")),
    )


def _status(waba: str, pnid: str, meta: dict, s: dict) -> Event:
    wamid = _str(s.get("id"))
    status = _str(s.get("status"))
    stamp = _str(s.get("timestamp"))
    return Event(
        kind="status",
        # the same message legitimately moves sent -> delivered -> read, so the
        # status and its time are part of the identity
        dedupe_key="st:%s:%s:%s" % (wamid or digest(s), status, stamp),
        waba_id=waba, phone_number_id=pnid,
        occurred_at=_when(s.get("timestamp")),
        raw={"field": "messages", "metadata": meta, "status": s},
        wamid=wamid,
        wa_id=_str(s.get("recipient_id")),
        bsuid=_str(s.get("recipient_user_id")),
        parent_bsuid=_str(s.get("parent_recipient_user_id")),
        status=status,
        errors=tuple(_dict(e) for e in _list(s.get("errors"))),
        pricing=_dict(s.get("pricing")),
        conversation=_dict(s.get("conversation")),
    )


def _from_account_change(waba: str, name: str, value: dict, entry_time: Any) -> Event:
    kind = _FIELD_KINDS.get(name, "other")
    raw = {"field": name, "value": value, "entry_time": entry_time}
    pnid = _str(value.get("phone_number_id") or _dict(value.get("metadata")).get("phone_number_id"))
    return Event(
        kind=kind,
        dedupe_key="ch:%s:%s" % (name, digest(raw)),
        waba_id=waba, phone_number_id=pnid,
        occurred_at=_when(entry_time),
        raw=raw,
        status=_str(value.get("event")),
    )
