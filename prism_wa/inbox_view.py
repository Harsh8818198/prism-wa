"""What the Inbox screen shows: the list of chats (with the states a team works from), one chat's messages, and a typed reply.

A person only ever sees the chats they may: an owner or manager sees every chat; an agent sees the chats assigned to them and
the unassigned ones that match their rules (the same rule `inbox.visible_conversations` applies — here as one query, so a list
with filters and counts costs one round trip).

    list_threads   the chats, filtered by state (requesting / mine / unassigned / bot / with an agent / resolved) and by whether
                   the 24-hour window is open (History = closed), by search and by any segment filter; plus the counts for the tabs
    thread         one chat's messages, oldest first, with delivery state and media
    send_text      a typed reply: one free-form message, sent from the number the chat is on
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from prism_wa import contacts, inbox, segments, send
from prism_wa.inbox import OWNED, mask_phone

STATES = ("requesting", "mine", "unassigned", "bot", "intervened", "closed")
WINDOW = timedelta(hours=24)
MAX_LIMIT = 300


def _visibility(cur, tenant_id, agent: dict, defs: dict) -> tuple:
    """(sql, params) limiting `v` (conversations) and `c` (contacts) to what this agent may see."""
    if agent["role"] != "agent":
        return "true", []
    rules = []
    params = [agent["id"]]
    for rule in agent["rules"] or []:
        compiled = segments.compile_filter(rule, defs=defs)
        rules.append("(" + compiled.sql + ")")
        params += compiled.params
    unassigned = "v.assignee_id is null" + (" and (" + " or ".join(rules) + ")" if rules else "")
    return "(v.assignee_id = %s or (" + unassigned + "))", params


def _state_sql(state: Optional[str], agent: dict) -> tuple:
    if not state:
        return "true", []
    if state == "requesting":
        return "v.status = 'requesting'", []
    if state == "mine":
        return "(v.assignee_id = %s and v.status <> 'closed')", [agent["id"]]
    if state == "unassigned":
        return "(v.assignee_id is null and v.status <> 'closed')", []
    if state == "bot":
        return "(v.status in ('new', 'active', 'open'))", []
    if state == "intervened":
        return "v.status in ('intervened', 'human_takeover')", []
    if state == "closed":
        return "v.status = 'closed'", []
    raise ValueError("unknown state %r" % (state,))


def _window_sql(window: Optional[str], now) -> tuple:
    """open / closed are the raw 24-hour window. `live` is what a team works from (the window is open, or a person is wanted or
    already on it); `history` is everything else: quiet chats nobody is on."""
    cutoff = now - WINDOW
    if window == "open":
        return "(v.last_inbound_at is not null and v.last_inbound_at > %s)", [cutoff]
    if window == "closed":
        return "(v.last_inbound_at is null or v.last_inbound_at <= %s)", [cutoff]
    if window == "live":
        return "((v.last_inbound_at is not null and v.last_inbound_at > %s) or v.status in ('requesting', 'intervened', 'human_takeover'))", [cutoff]
    if window == "history":
        return "((v.last_inbound_at is null or v.last_inbound_at <= %s) and v.status not in ('requesting', 'intervened', 'human_takeover'))", [cutoff]
    return "true", []


def list_threads(cur, tenant_id, agent: dict, *, state: Optional[str] = None, window: Optional[str] = None, search: str = "",
                 filter_=None, limit: int = 100, offset: int = 0) -> dict:
    """-> {"conversations": [...], "counts": {...}, "total": n}. Counts ignore `state` (they are the tabs) but obey the rest."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    defs = contacts.attribute_defs(cur, tenant_id)
    vis_sql, vis_params = _visibility(cur, tenant_id, agent, defs)
    win_sql, win_params = _window_sql(window, now)
    base = ["v.tenant_id = %s", "v.channel = 'whatsapp'", vis_sql, win_sql]
    base_params: list = [tenant_id] + vis_params + win_params
    if filter_:
        compiled = segments.compile_filter(filter_, defs=defs)
        base.append("(" + compiled.sql + ")")
        base_params += compiled.params
    text = (search or "").strip()
    if text:
        digits = re.sub(r"\D", "", text)
        clause = "(c.name ilike %s or exists (select 1 from messages q where q.conversation_id = v.id and q.text ilike %s)"
        base_params += ["%" + text + "%", "%" + text + "%"]
        if len(digits) >= 3:
            clause += " or c.phone like %s"
            base_params.append("%" + digits + "%")
        base.append(clause + ")")
    from_sql = " from conversations v join contacts c on c.id = v.contact_id where " + " and ".join(base)

    cur.execute("select count(*),"
                " count(*) filter (where v.status = 'requesting'),"
                " count(*) filter (where v.assignee_id = %s and v.status <> 'closed'),"
                " count(*) filter (where v.assignee_id is null and v.status <> 'closed'),"
                " count(*) filter (where v.status in ('new', 'active', 'open')),"
                " count(*) filter (where v.status in ('intervened', 'human_takeover')),"
                " count(*) filter (where v.status = 'closed')" + from_sql, [agent["id"]] + base_params)
    row = cur.fetchone()
    counts = dict(zip(("all",) + STATES, row))

    st_sql, st_params = _state_sql(state, agent)
    cur.execute("select count(*)" + from_sql + " and " + st_sql, base_params + st_params)
    total = cur.fetchone()[0]

    limit = max(1, min(int(limit or 100), MAX_LIMIT))
    cur.execute(
        "select v.id, v.contact_id, v.status, v.assignee_id, (select a.name from agents a where a.id = v.assignee_id), v.last_active_at,"
        " v.last_inbound_at, v.requested_at, c.name, c.phone, c.opt_out, c.blocked, c.tags, lm.text, lm.direction, lm.msg_type"
        " from conversations v join contacts c on c.id = v.contact_id"
        " left join lateral (select m.text, m.direction, m.msg_type from messages m where m.conversation_id = v.id"
        "   order by m.created_at desc limit 1) lm on true"
        " where " + " and ".join(base) + " and " + st_sql +
        " order by v.last_active_at desc nulls last, v.id limit %s offset %s",
        base_params + st_params + [limit, max(int(offset or 0), 0)])
    masked = bool(agent["masked"] and agent["role"] == "agent")
    out = []
    for (cid, contact_id, status, assignee, assignee_name, last_active, last_in, requested, name, phone, opt_out, blocked, tags,
         last_text, last_dir, last_type) in cur.fetchall():
        out.append({"id": cid, "contact_id": contact_id, "status": status, "assignee_id": assignee, "assignee_name": assignee_name,
                    "last_active_at": last_active, "last_inbound_at": last_in, "requested_at": requested, "contact_name": name,
                    "contact_phone": mask_phone(phone) if masked else phone, "opt_out": bool(opt_out), "blocked": bool(blocked),
                    "tags": list(tags or []), "last_text": last_text or "", "last_direction": last_dir or "", "last_type": last_type or "text",
                    "window_open": bool(last_in and now - last_in < WINDOW), "channel": "whatsapp"})
    return {"conversations": out, "counts": counts, "total": total}


def _conversation_row(cur, tenant_id, agent: dict, conversation_id) -> dict:
    cur.execute("select v.id, v.contact_id, v.status, v.assignee_id, v.last_inbound_at, v.last_active_at, c.name, c.phone, c.opt_out, c.blocked"
                " from conversations v join contacts c on c.id = v.contact_id where v.id = %s and v.tenant_id = %s", (conversation_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise inbox.InboxError("unknown_conversation", "that chat is not in this workspace")
    cols = ("id", "contact_id", "status", "assignee_id", "last_inbound_at", "last_active_at", "contact_name", "contact_phone", "opt_out", "blocked")
    conv = dict(zip(cols, row))
    if agent["role"] == "agent" and conv["assignee_id"] is not None and str(conv["assignee_id"]) != str(agent["id"]):
        raise inbox.InboxError("not_yours", "this chat belongs to another agent")
    if agent["masked"] and agent["role"] == "agent":
        conv["contact_phone"] = mask_phone(conv["contact_phone"])
    return conv


def thread(cur, tenant_id, agent: dict, conversation_id, *, limit: int = 200) -> dict:
    """-> {"conversation": {...}, "messages": [...]} (oldest first)."""
    from datetime import datetime, timezone
    conv = _conversation_row(cur, tenant_id, agent, conversation_id)
    conv["window_open"] = bool(conv["last_inbound_at"] and datetime.now(timezone.utc) - conv["last_inbound_at"] < WINDOW)
    limit = max(1, min(int(limit or 200), 1000))
    cur.execute("select * from (select m.id, m.direction, m.sender, m.text, m.created_at, m.status, m.msg_type, m.media_id, m.mime_type,"
                " m.error, m.reply_to, m.media_asset_id, a.filename, a.size_bytes from messages m left join media_assets a on a.id = m.media_asset_id"
                " where m.conversation_id = %s order by m.created_at desc, m.id limit %s) t order by created_at, id", (conversation_id, limit))
    cols = ("id", "direction", "sender", "text", "created_at", "status", "msg_type", "media_id", "mime_type", "error", "reply_to",
            "asset_id", "filename", "size")
    msgs = []
    for raw in cur.fetchall():
        m = dict(zip(cols, raw))
        has = m["media_id"] or m["asset_id"]
        m["media"] = {"id": m["asset_id"], "wa_id": m["media_id"], "mime_type": m["mime_type"], "kind": m["msg_type"], "filename": m["filename"],
                      "size": m["size"], "ready": m["asset_id"] is not None} if has else None
        for k in ("media_id", "mime_type", "asset_id", "filename", "size"):
            del m[k]
        msgs.append(m)
    return {"conversation": conv, "messages": msgs}


def send_media(cur, tenant_id, agent: dict, *, conversation_id=None, contact_id=None, asset_id, caption: str = "", client_ref: str = "") -> dict:
    """A file from the Media Library, typed into a chat (inside the 24-hour window, like any reply)."""
    from prism_wa import media
    if conversation_id:
        conv = _conversation_row(cur, tenant_id, agent, conversation_id)
        contact_id = conv["contact_id"]
    if not contact_id:
        raise ValueError("say which chat or contact the file is for")
    asset = media.get(cur, tenant_id, asset_id)
    import uuid
    key = "ui:%s" % (client_ref or uuid.uuid4())
    sent = send.enqueue_media(cur, tenant_id, contact_id=contact_id, kind=asset["kind"], asset_id=asset["id"], caption=caption,
                              filename=asset["filename"], idem_key=key, sender="agent")
    send.attribute(cur, sent, agent["id"])
    inbox.note_reply_by(cur, tenant_id, contact_id, agent["id"])
    return {"queued": True, "outbox_id": str(sent)}


def send_text(cur, tenant_id, agent: dict, *, conversation_id=None, contact_id=None, text: str, client_ref: str = "") -> dict:
    """A reply typed by a person. Free-form, so only inside the 24-hour window (the send engine refuses otherwise, in words)."""
    if conversation_id:
        conv = _conversation_row(cur, tenant_id, agent, conversation_id)
        contact_id = conv["contact_id"]
    if not contact_id:
        raise ValueError("say which chat or contact the reply is for")
    key = "ui:%s" % (client_ref or "")
    if not client_ref:
        import uuid
        key = "ui:%s" % uuid.uuid4()
    sent = send.enqueue_text(cur, tenant_id, contact_id=contact_id, text=text, idem_key=key, sender="agent")
    send.attribute(cur, sent, agent["id"])
    inbox.note_reply_by(cur, tenant_id, contact_id, agent["id"])
    return {"queued": True, "outbox_id": str(sent)}
