"""The team inbox (spec WS10): who owns a chat, who can see it, who answers when nobody is there.

A conversation is in one state:

    active       the customer is chatting and nobody has taken over (a bot may answer)
    requesting   the customer (or a bot) needs a person; waiting to be picked up
    intervened   a person owns it; the bot stays out of it
    closed       resolved; a new message from the customer reopens it as active

(`new` is the legacy starting state and `human_takeover` the legacy name for intervened.)

Ownership and visibility. Each Prism seat is an agent with a role. Owners and
managers see every chat. An agent sees the chats assigned to them and the
unassigned ones that match their rules — and the database enforces the first
half (migration 0010: an agent's session cannot read another agent's chat), so a
bug in a screen cannot leak it.

Routing picks who gets a chat that needs a person: only agents who are
available and inside their working hours, whose rules (segment filters, e.g.
"tag is builder") match the contact; then least busy, or round robin.

When nobody is there: an auto-reply says so (once per contact per N hours), and a
welcome message greets someone new during opening hours. Idle chats close
themselves after N hours (default 24).
"""
from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from prism_wa import chat_send, media, plans, segments, send
from prism_wa.jobs import utcnow

OPEN = ("new", "active", "requesting", "intervened", "human_takeover")
OWNED = ("intervened", "human_takeover")
ROLES = ("owner", "manager", "agent")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ROUTING = ("least_busy", "round_robin", "manual")
SHORTCUT_RE = re.compile(r"^[a-z0-9_-]{1,30}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULTS = {
    "routing": "least_busy", "auto_resolve_hours": 24, "sla_minutes": 15, "business_hours": None, "holidays": [],
    "welcome": {"enabled": False, "text": "Hello! Thanks for messaging us. How can we help you today?", "once_per_hours": 24,
                "asset_id": None, "template": None},
    "off_hours": {"enabled": False, "once_per_hours": 12, "asset_id": None, "template": None,
                  "text": "Thanks for your message. We are closed right now and will reply as soon as we open."},
}


class InboxError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# ── working hours ────────────────────────────────────────────────────────────

def validate_hours(hours) -> None:
    if hours is None:
        return
    if not isinstance(hours, dict) or not isinstance(hours.get("days"), dict):
        raise InboxError("bad_hours", "working hours look like {tz, days: {mon: [[09:00, 18:00]]}}")
    try:
        ZoneInfo(hours.get("tz") or "Asia/Kolkata")
    except Exception:                                       # noqa: BLE001
        raise InboxError("bad_hours", "unknown time zone %r" % hours.get("tz")) from None
    for day, ranges in hours["days"].items():
        if day not in DAYS or not isinstance(ranges, list):
            raise InboxError("bad_hours", "days are mon..sun, each with a list of [start, end] times")
        for r in ranges:
            if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) and TIME_RE.match(x) for x in r)) \
                    or r[0] >= r[1]:
                raise InboxError("bad_hours", "each range is [start, end] as HH:MM with start before end")


def in_hours(hours, now: datetime) -> bool:
    """Is `now` inside these working hours? None means always open."""
    if not hours:
        return True
    local = now.astimezone(ZoneInfo(hours.get("tz") or "Asia/Kolkata"))
    at = local.strftime("%H:%M")
    return any(start <= at < end for start, end in (hours.get("days") or {}).get(DAYS[local.weekday()], []))


# ── settings ─────────────────────────────────────────────────────────────────

def settings(cur, tenant_id) -> dict:
    cur.execute("select settings->'inbox' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    stored = (row[0] if row else None) or {}
    out = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}
    for key in ("welcome", "off_hours"):
        out[key] = {**DEFAULTS[key], **(stored.get(key) or {})}
    return out


MAX_HOLIDAYS = 60


def clean_holidays(value) -> list:
    """The days the business is closed: [{"date": "2026-11-12", "name": "Diwali"}], sorted, each once. A flow can ask "is today a holiday?"."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_HOLIDAYS:
        raise InboxError("bad_settings", "holidays is a list of up to %d days" % MAX_HOLIDAYS)
    seen, out = set(), []
    for h in value:
        day = str((h or {}).get("date") if isinstance(h, dict) else h or "").strip()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise InboxError("bad_settings", "a holiday needs a date like 2026-11-12 (got %r)" % day[:20]) from None
        name = str(h.get("name") or "").strip()[:60] if isinstance(h, dict) else ""
        if day not in seen:
            seen.add(day)
            out.append({"date": day, "name": name} if name else {"date": day})
    return sorted(out, key=lambda x: x["date"])


def save_settings(cur, tenant_id, patch: dict) -> dict:
    new = settings(cur, tenant_id)
    for key, value in (patch or {}).items():
        if key in ("welcome", "off_hours"):
            new[key] = {**new[key], **(value or {})}
        elif key in DEFAULTS:
            new[key] = value
    if new["routing"] not in ROUTING:
        raise InboxError("bad_settings", "routing must be one of: %s" % ", ".join(ROUTING))
    if new["routing"] != "manual":
        plans.require(cur, tenant_id, "smart_routing", "Automatic routing")
    for key, lo, hi in (("auto_resolve_hours", 0, 24 * 30), ("sla_minutes", 1, 24 * 60)):
        if not (isinstance(new[key], int) and not isinstance(new[key], bool) and lo <= new[key] <= hi):
            raise InboxError("bad_settings", "%s must be a whole number from %d to %d" % (key, lo, hi))
    validate_hours(new["business_hours"])
    new["holidays"] = clean_holidays(new.get("holidays"))
    for key in ("welcome", "off_hours"):
        block = new[key]
        tpl = block.get("template")
        if tpl is not None and not (isinstance(tpl, dict) and (tpl.get("name") or "").strip()):
            raise InboxError("bad_settings", "%s: choose a template, or none" % key.replace("_", " "))
        asset = block.get("asset_id")
        if asset:
            try:
                media.get(cur, tenant_id, asset)
            except media.MediaError as exc:
                raise InboxError("bad_settings", "%s: %s" % (key.replace("_", " "), exc)) from None
        text = block.get("text")
        if not isinstance(text, str) or len(text.strip()) > 1000 or not (text.strip() or asset or tpl):
            raise InboxError("bad_settings", "%s needs a message of up to 1000 characters, a file or a template" % key.replace("_", " "))
        if not (isinstance(block.get("once_per_hours"), int) and 1 <= block["once_per_hours"] <= 24 * 30):
            raise InboxError("bad_settings", "%s: once_per_hours must be 1 to 720" % key)
        block["enabled"] = bool(block.get("enabled"))
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, %s)"
                " on conflict (tenant_id) do update set settings = tenant_settings.settings || excluded.settings,"
                " updated_at = now()", (tenant_id, Json({"inbox": {k: new[k] for k in DEFAULTS}})))
    return new


# ── agents ───────────────────────────────────────────────────────────────────

AGENT_COLS = ("id", "seat_key", "name", "role", "active", "available", "masked", "working_hours", "rules",
              "last_assigned_at", "created_at", "email")


def _agent(cur, tenant_id, agent_id) -> dict:
    cur.execute("select %s from agents where id = %%s and tenant_id = %%s" % ", ".join(AGENT_COLS), (agent_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise InboxError("unknown_agent", "that person is not in this workspace")
    return dict(zip(AGENT_COLS, row))


def ensure_agent(cur, tenant_id, seat_key: str, name: str) -> dict:
    """The agent for a Prism seat, created on first use. The first person in a workspace is its owner."""
    cur.execute("select %s from agents where tenant_id = %%s and seat_key = %%s" % ", ".join(AGENT_COLS), (tenant_id, seat_key))
    row = cur.fetchone()
    if row:
        return dict(zip(AGENT_COLS, row))
    cur.execute("select count(*) from agents where tenant_id = %s and active", (tenant_id,))
    have = cur.fetchone()[0]
    plans.check_count(cur, tenant_id, "agents", have, 1, "team members (the owner included)")
    role = "owner" if have == 0 else "agent"
    cur.execute("insert into agents (tenant_id, seat_key, name, role) values (%s, %s, %s, %s) returning id",
                (tenant_id, seat_key, (name or "Agent").strip()[:80] or "Agent", role))
    return _agent(cur, tenant_id, cur.fetchone()[0])


def list_agents(cur, tenant_id) -> list:
    cur.execute("select %s from agents where tenant_id = %%s and active order by created_at" % ", ".join(AGENT_COLS), (tenant_id,))
    return [dict(zip(AGENT_COLS, r)) for r in cur.fetchall()]


def update_agent(cur, tenant_id, agent_id, **changes) -> dict:
    agent = _agent(cur, tenant_id, agent_id)
    sets, params = [], []
    for key, value in changes.items():
        if key == "role":
            if value not in ROLES:
                raise InboxError("bad_role", "role must be one of: %s" % ", ".join(ROLES))
            if value != agent["role"]:
                plans.require(cur, tenant_id, "access_control", "Roles and permissions")
            if agent["role"] == "owner" and value != "owner" and agent["active"]:
                cur.execute("select count(*) from agents where tenant_id = %s and active and role = 'owner'", (tenant_id,))
                if cur.fetchone()[0] <= 1:
                    raise InboxError("last_owner", "a workspace needs at least one owner")
        elif key == "active" and value is False and agent["role"] == "owner":
            cur.execute("select count(*) from agents where tenant_id = %s and active and role = 'owner'", (tenant_id,))
            if cur.fetchone()[0] <= 1:
                raise InboxError("last_owner", "a workspace needs at least one owner")
        elif key == "working_hours":
            validate_hours(value)
            value = Json(value) if value is not None else None
        elif key == "rules":
            if not isinstance(value, list):
                raise InboxError("bad_rules", "rules are a list of segment filters")
            if value:
                plans.require(cur, tenant_id, "agent_rules", "Agent rules")
            for rule in value:
                problem = segments.validate(cur, tenant_id, rule)
                if problem:
                    raise InboxError("bad_rules", problem)
            value = Json(value)
        elif key in ("available", "masked", "active"):
            value = bool(value)
            if key == "masked" and value:
                plans.require(cur, tenant_id, "number_masking", "Number masking")
        elif key == "name":
            value = (value or "").strip()[:80]
            if not value:
                raise InboxError("bad_name", "a name is required")
        elif key == "email":                                         # the company address single sign-on matches a sign-in to
            value = (value or "").strip().lower()[:200] or None
            if value:
                if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
                    raise InboxError("bad_email", "that e-mail address is not valid")
                cur.execute("select 1 from agents where tenant_id = %s and active and lower(email) = %s and id <> %s", (tenant_id, value, agent_id))
                if cur.fetchone():
                    raise InboxError("email_taken", "another team member already has that e-mail address")
        else:
            raise InboxError("bad_field", "cannot change %r" % key)
        sets.append("%s = %%s" % key)
        params.append(value)
    if sets:
        cur.execute("update agents set %s where id = %%s" % ", ".join(sets), params + [agent_id])
    return _agent(cur, tenant_id, agent_id)


# ── conversations ────────────────────────────────────────────────────────────

def _conversation(cur, tenant_id, conversation_id, *, lock: bool = False) -> dict:
    cur.execute("select id, contact_id, status, assignee_id from conversations where id = %s and tenant_id = %s"
                + (" for update" if lock else ""), (conversation_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise InboxError("unknown_conversation", "that chat is not in this workspace")
    return dict(zip(("id", "contact_id", "status", "assignee_id"), row))


def _differ(a, b) -> bool:
    """Ids arrive as UUIDs from the database and as strings from JSON; compare what they say, not their type."""
    return str(a) != str(b)


def _event(cur, tenant_id, conversation_id, kind: str, actor=None, detail=None, now=None) -> None:
    cur.execute("insert into conversation_events (tenant_id, conversation_id, kind, actor_id, detail, at)"
                " values (%s, %s, %s, %s, %s, coalesce(%s, now()))",
                (tenant_id, conversation_id, kind, actor, Json(detail) if detail else None, now))


def contact_matches(cur, tenant_id, filter_, contact_id) -> bool:
    compiled = segments.compile_filter(filter_, defs=_defs(cur, tenant_id))
    cur.execute("select exists (select 1 from contacts c where c.tenant_id = %s and c.id = %s and (" + compiled.sql + "))",
                [tenant_id, contact_id] + compiled.params)
    return cur.fetchone()[0]


def _defs(cur, tenant_id) -> dict:
    from prism_wa import contacts
    return contacts.attribute_defs(cur, tenant_id)


def _eligible(cur, tenant_id, agent: dict, contact_id, now: datetime) -> bool:
    if not (agent["active"] and agent["available"] and in_hours(agent["working_hours"], now)):
        return False
    rules = agent["rules"] or []
    return not rules or any(contact_matches(cur, tenant_id, r, contact_id) for r in rules)


def pick_agent(cur, tenant_id, contact_id, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Who should get this chat, or None (nobody is available, or routing is manual)."""
    now = now or utcnow()
    strategy = settings(cur, tenant_id)["routing"]
    if strategy == "manual" or not plans.has(plans.plan_of(cur, tenant_id), "smart_routing"):
        return None                                         # chats wait for someone to pick them up
    for roles in (("agent",), ("manager", "owner")):
        cur.execute("select %s, (select count(*) from conversations v where v.assignee_id = agents.id"
                    "   and v.status in ('active', 'requesting', 'intervened', 'human_takeover')) as busy"
                    " from agents where tenant_id = %%s and active and role = any(%%s)" % ", ".join(AGENT_COLS),
                    (tenant_id, list(roles)))
        candidates = []
        for row in cur.fetchall():
            agent = dict(zip(AGENT_COLS, row[:-1]))
            agent["busy"] = row[-1]
            if _eligible(cur, tenant_id, agent, contact_id, now):
                candidates.append(agent)
        if candidates:
            never = datetime.min.replace(tzinfo=timezone.utc)
            if strategy == "round_robin":
                key = lambda a: (a["last_assigned_at"] or never, a["created_at"], str(a["id"]))                   # noqa: E731
            else:
                key = lambda a: (a["busy"], a["last_assigned_at"] or never, a["created_at"], str(a["id"]))        # noqa: E731
            return min(candidates, key=key)
    return None


def _assign(cur, tenant_id, conversation_id, agent_id, now, kind: str, actor=None, detail=None) -> None:
    cur.execute("update conversations set assignee_id = %s, assigned_at = %s where id = %s",
                (agent_id, now, conversation_id))
    if agent_id:
        cur.execute("update agents set last_assigned_at = %s where id = %s", (now, agent_id))
    _event(cur, tenant_id, conversation_id, kind, actor, {"to": str(agent_id) if agent_id else None, **(detail or {})}, now)


def request_human(cur, tenant_id, conversation_id, reason: str = "", *, now: Optional[datetime] = None) -> Optional[dict]:
    """The customer (or a bot) needs a person. Marks the chat requesting and routes it. -> the agent it went to, or None."""
    now = now or utcnow()
    conv = _conversation(cur, tenant_id, conversation_id, lock=True)
    if conv["status"] in OWNED:
        return None                                         # a person already has it
    cur.execute("update conversations set status = 'requesting', requested_at = %s, bot_paused = true where id = %s",
                (now, conversation_id))
    _event(cur, tenant_id, conversation_id, "requested", None, {"reason": reason}, now)
    agent = pick_agent(cur, tenant_id, conv["contact_id"], now=now)
    if agent:
        _assign(cur, tenant_id, conversation_id, agent["id"], now, "assigned", None, {"by": "routing"})
    from prism_wa import webhooks_out
    webhooks_out.emit(cur, tenant_id, "chat.handoff", lambda: {
        "conversation_id": str(conversation_id), "contact_id": str(conv["contact_id"]), "reason": reason or None,
        "assigned_to": agent["name"] if agent else None})
    return agent


def intervene(cur, tenant_id, conversation_id, agent_id, *, now: Optional[datetime] = None) -> None:
    """A person takes the chat. Refused if another agent owns it, unless the taker is a manager or owner."""
    now = now or utcnow()
    agent = _agent(cur, tenant_id, agent_id)
    conv = _conversation(cur, tenant_id, conversation_id, lock=True)
    holder = conv["assignee_id"]
    if conv["status"] in OWNED and holder and _differ(holder, agent_id) and agent["role"] == "agent":
        raise InboxError("taken", "another agent is already handling this chat")
    cur.execute("update conversations set status = 'intervened', bot_paused = true where id = %s", (conversation_id,))
    if _differ(holder, agent_id):
        _assign(cur, tenant_id, conversation_id, agent_id, now, "intervened", agent_id)
    elif conv["status"] not in OWNED:
        _event(cur, tenant_id, conversation_id, "intervened", agent_id, None, now)


def transfer(cur, tenant_id, conversation_id, to_agent_id, by_agent_id, *, now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    by = _agent(cur, tenant_id, by_agent_id)
    target = _agent(cur, tenant_id, to_agent_id)
    conv = _conversation(cur, tenant_id, conversation_id, lock=True)
    if by["role"] == "agent" and _differ(conv["assignee_id"], by_agent_id):
        raise InboxError("not_yours", "you can only transfer a chat that is yours")
    if not target["active"]:
        raise InboxError("unknown_agent", "that person is no longer in the workspace")
    cur.execute("update conversations set status = case when status in ('closed', 'new', 'active') then 'requesting'"
                " else status end where id = %s", (conversation_id,))
    _assign(cur, tenant_id, conversation_id, to_agent_id, now, "transferred", by_agent_id,
            {"from": str(conv["assignee_id"]) if conv["assignee_id"] else None})


def hand_back(cur, tenant_id, conversation_id, agent_id, *, now: Optional[datetime] = None) -> None:
    """Give the chat back to the bot: active, unassigned."""
    now = now or utcnow()
    conv = _conversation(cur, tenant_id, conversation_id, lock=True)
    agent = _agent(cur, tenant_id, agent_id)
    if agent["role"] == "agent" and conv["assignee_id"] is not None and _differ(conv["assignee_id"], agent_id):
        raise InboxError("not_yours", "this chat belongs to another agent")
    cur.execute("update conversations set status = 'active', assignee_id = null, bot_paused = false where id = %s", (conversation_id,))
    _event(cur, tenant_id, conversation_id, "handed_back", agent_id, None, now)


def resolve(cur, tenant_id, conversation_id, agent_id=None, *, now: Optional[datetime] = None, auto: bool = False) -> None:
    now = now or utcnow()
    conv = _conversation(cur, tenant_id, conversation_id, lock=True)
    if agent_id:
        agent = _agent(cur, tenant_id, agent_id)
        if agent["role"] == "agent" and conv["assignee_id"] is not None and _differ(conv["assignee_id"], agent_id):
            raise InboxError("not_yours", "this chat belongs to another agent")
    cur.execute("update conversations set status = 'closed', resolved_at = %s, bot_paused = false where id = %s",
                (now, conversation_id))
    _event(cur, tenant_id, conversation_id, "resolved", agent_id, {"auto": True} if auto else None, now)


def on_customer_message(cur, tenant_id, conversation_id, now) -> None:
    """A message arrived: a closed chat reopens as active, with nobody assigned (the bot resumes)."""
    cur.execute("update conversations set status = 'active', assignee_id = null, bot_paused = false, resolved_at = null"
                " where id = %s and status = 'closed' returning id", (conversation_id,))
    if cur.fetchone():
        _event(cur, tenant_id, conversation_id, "reopened", None, None, now)


def note_reply_by(cur, tenant_id, contact_id, agent_id, *, now: Optional[datetime] = None) -> None:
    """An agent is replying to a contact: that is taking the chat (unless someone else has it)."""
    cur.execute("select id from conversations where tenant_id = %s and contact_id = %s and channel = 'whatsapp'",
                (tenant_id, contact_id))
    row = cur.fetchone()
    if row:
        intervene(cur, tenant_id, row[0], agent_id, now=now)


# ── notes and canned replies ─────────────────────────────────────────────────

def add_note(cur, tenant_id, conversation_id, agent_id, text: str) -> None:
    if not (text or "").strip() or len(text) > 4000:
        raise InboxError("bad_note", "a note needs 1 to 4000 characters")
    _conversation(cur, tenant_id, conversation_id)
    cur.execute("insert into conversation_notes (tenant_id, conversation_id, agent_id, text) values (%s, %s, %s, %s)",
                (tenant_id, conversation_id, agent_id, text.strip()))


def list_notes(cur, tenant_id, conversation_id) -> list:
    cur.execute("select n.id, n.text, n.at, a.name from conversation_notes n left join agents a on a.id = n.agent_id"
                " where n.tenant_id = %s and n.conversation_id = %s order by n.at", (tenant_id, conversation_id))
    return [{"id": r[0], "text": r[1], "at": r[2], "agent": r[3]} for r in cur.fetchall()]


def save_canned(cur, tenant_id, shortcut: str, body: str, agent_id=None, asset_id=None) -> None:
    shortcut = (shortcut or "").strip().lower().lstrip("/")
    if not SHORTCUT_RE.match(shortcut):
        raise InboxError("bad_shortcut", "a shortcut uses lowercase letters, numbers, - and _ (30 at most)")
    body = (body or "").strip()
    if len(body) > 1000 or not (body or asset_id):
        raise InboxError("bad_canned", "a saved reply needs 1 to 1000 characters, or a file")
    if asset_id:
        try:
            media.get(cur, tenant_id, asset_id)
        except media.MediaError as exc:
            raise InboxError("bad_canned", str(exc)) from None
    cur.execute("insert into canned_messages (tenant_id, shortcut, body, created_by, asset_id) values (%s, %s, %s, %s, %s)"
                " on conflict (tenant_id, shortcut) do update set body = excluded.body, asset_id = excluded.asset_id",
                (tenant_id, shortcut, body, agent_id, asset_id))


def delete_canned(cur, tenant_id, shortcut: str) -> bool:
    cur.execute("delete from canned_messages where tenant_id = %s and shortcut = %s", (tenant_id, shortcut.strip().lower().lstrip("/")))
    return cur.rowcount > 0


def list_canned(cur, tenant_id) -> list:
    cur.execute("select c.shortcut, c.body, c.asset_id, a.kind, a.filename from canned_messages c left join media_assets a on a.id = c.asset_id"
                " where c.tenant_id = %s order by c.shortcut", (tenant_id,))
    return [{"shortcut": r[0], "body": r[1], "asset_id": r[2], "asset_kind": r[3], "asset_name": r[4]} for r in cur.fetchall()]


def expand(cur, tenant_id, shortcut: str, contact_id) -> str:
    """A saved reply with {{name}}, {{first_name}}, {{last_name}} and {{mobile}} filled in for this contact."""
    return expand_full(cur, tenant_id, shortcut, contact_id)["text"]


def expand_full(cur, tenant_id, shortcut: str, contact_id) -> dict:
    """-> {"text": the words filled in, "asset": the file it carries or None}."""
    cur.execute("select body, asset_id from canned_messages where tenant_id = %s and shortcut = %s",
                (tenant_id, shortcut.strip().lower().lstrip("/")))
    row = cur.fetchone()
    if row is None:
        raise InboxError("unknown_shortcut", "there is no saved reply called /%s" % shortcut)
    cur.execute("select name, phone from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    c = cur.fetchone()
    name, phone = (c or ("", ""))
    parts = (name or "").split()
    values = {"name": name or "", "first_name": parts[0] if parts else "", "last_name": parts[-1] if len(parts) > 1 else "",
              "mobile": phone or ""}
    text = re.sub(r"\{\{\s*(name|first_name|last_name|mobile)\s*\}\}", lambda m: values[m.group(1)], row[0])
    asset = media.get(cur, tenant_id, row[1]) if row[1] else None
    return {"text": text, "asset": asset}


# ── what an agent may see ────────────────────────────────────────────────────

def mask_phone(phone) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return ("*" * max(len(digits) - 4, 0) + digits[-4:]) if digits else ""


def visible_conversations(cur, tenant_id, agent: dict, *, status: Optional[str] = None, limit: int = 100) -> list:
    """The chats this person may see, newest first. Phone numbers are masked for an agent who is set to masked."""
    sql = ("select v.id, v.contact_id, v.status, v.assignee_id, v.last_active_at, c.name, c.phone"
           " from conversations v join contacts c on c.id = v.contact_id where v.tenant_id = %s")
    params = [tenant_id]
    if agent["role"] == "agent":
        sql += " and (v.assignee_id = %s or v.assignee_id is null)"
        params.append(agent["id"])
    if status:
        sql += " and v.status = %s"
        params.append(status)
    cur.execute(sql + " order by v.last_active_at desc nulls last limit %s", params + [limit * 3 if agent["role"] == "agent" else limit])
    rows = []
    rules = agent["rules"] or []
    for conv_id, contact_id, st, assignee, last, name, phone in cur.fetchall():
        if agent["role"] == "agent" and assignee is None and rules \
                and not any(contact_matches(cur, tenant_id, r, contact_id) for r in rules):
            continue
        rows.append({"id": conv_id, "contact_id": contact_id, "status": st, "assignee_id": assignee,
                     "last_active_at": last, "contact_name": name,
                     "contact_phone": mask_phone(phone) if agent["masked"] and agent["role"] == "agent" else phone})
        if len(rows) >= limit:
            break
    return rows


# ── auto-replies ─────────────────────────────────────────────────────────────

def maybe_auto_reply(cur, tenant_id, contact_id, conversation_id, when: datetime, *, wamid: str = "") -> Optional[str]:
    """Send the off-hours or welcome message if one is due. -> "off_hours", "welcome" or None."""
    cfg = settings(cur, tenant_id)
    cur.execute("select status, last_welcome_at, last_offhours_at from conversations where id = %s", (conversation_id,))
    status, last_welcome, last_off = cur.fetchone()
    if status in OWNED:
        return None                                         # a person is on it
    open_now = in_hours(cfg["business_hours"], when)
    kind, block, last = ("welcome", cfg["welcome"], last_welcome) if open_now else ("off_hours", cfg["off_hours"], last_off)
    if not block["enabled"]:
        return None
    if last is not None and when - last < timedelta(hours=block["once_per_hours"]):
        return None
    key = "auto:%s:%s" % (kind, wamid or conversation_id)
    try:
        if block.get("template"):
            tpl = block["template"]
            chat_send.send_template(cur, tenant_id, contact_id, name=tpl["name"], language=tpl.get("language") or "en_US", body=tpl.get("body") or (),
                                    sender="autoreply", now=when)
        elif block.get("asset_id"):
            asset = media.get(cur, tenant_id, block["asset_id"])
            send.enqueue_media(cur, tenant_id, contact_id=contact_id, kind=asset["kind"], asset_id=asset["id"], caption=block["text"].strip(),
                               filename=asset["filename"], idem_key=key, sender="autoreply", now=when)
        else:
            send.enqueue_text(cur, tenant_id, contact_id=contact_id, text=block["text"], sender="autoreply", now=when, idem_key=key)
    except (send.SendRefused, chat_send.ChatSendError, media.MediaError):
        return None
    cur.execute("update conversations set %s = %%s where id = %%s" % ("last_welcome_at" if kind == "welcome" else "last_offhours_at"),
                (when, conversation_id))
    return kind


# ── the manager's view ───────────────────────────────────────────────────────

def monitor(cur, tenant_id, *, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    cfg = settings(cur, tenant_id)
    cur.execute("select status, count(*) from conversations where tenant_id = %s group by 1", (tenant_id,))
    by_state = dict(cur.fetchall())
    cur.execute("select count(*) from conversations where tenant_id = %s and status = 'requesting' and assignee_id is null", (tenant_id,))
    unassigned = cur.fetchone()[0]
    cur.execute("select a.id, a.name, a.role, a.available, count(v.id) filter (where v.status in"
                " ('active', 'requesting', 'intervened', 'human_takeover'))"
                " from agents a left join conversations v on v.assignee_id = a.id"
                " where a.tenant_id = %s and a.active group by a.id order by a.name", (tenant_id,))
    per_agent = [{"id": r[0], "name": r[1], "role": r[2], "available": r[3], "open": r[4]} for r in cur.fetchall()]
    cur.execute("select count(*) from conversations v where v.tenant_id = %s and v.status = 'requesting' and v.requested_at < %s"
                " and not exists (select 1 from messages m where m.conversation_id = v.id and m.direction = 'out'"
                "   and m.sender = 'agent' and m.created_at > v.requested_at)",
                (tenant_id, now - timedelta(minutes=cfg["sla_minutes"])))
    breached = cur.fetchone()[0]
    cur.execute(
        "with turns as (select m.conversation_id, m.created_at, m.direction,"
        "   lag(m.direction) over (partition by m.conversation_id order by m.created_at) as prev"
        "   from messages m where m.tenant_id = %s and m.created_at > %s),"
        " starts as (select conversation_id, created_at from turns where direction = 'in' and (prev is null or prev = 'out')),"
        " replies as (select s.created_at as t0, (select min(r.created_at) from messages r where r.conversation_id = s.conversation_id"
        "   and r.direction = 'out' and r.sender = 'agent' and r.created_at > s.created_at) as t1 from starts s)"
        " select avg(extract(epoch from (t1 - t0))), count(*) from replies where t1 is not null",
        (tenant_id, now - timedelta(days=7)))
    avg, answered = cur.fetchone()
    return {"by_state": by_state, "unassigned_requesting": unassigned, "agents": per_agent, "sla_minutes": cfg["sla_minutes"],
            "sla_breached": breached, "first_response_seconds": round(float(avg), 1) if avg is not None else None,
            "answered_last_7_days": answered}


# ── housekeeping ─────────────────────────────────────────────────────────────

def auto_resolve(connect, *, now: Optional[datetime] = None) -> int:
    """Close chats that have been idle longer than each workspace's setting (default 24 hours)."""
    now = now or utcnow()
    conn = connect()
    closed = 0
    try:
        with conn.cursor() as cur:
            cur.execute("select distinct v.tenant_id, coalesce((ts.settings->'inbox'->>'auto_resolve_hours')::int, %s)"
                        " from conversations v left join tenant_settings ts on ts.tenant_id = v.tenant_id"
                        " where v.status in ('active', 'requesting', 'intervened', 'human_takeover')",
                        (DEFAULTS["auto_resolve_hours"],))
            for tenant_id, hours in cur.fetchall():
                if not hours:
                    continue
                cur.execute("update conversations set status = 'closed', resolved_at = %s, bot_paused = false"
                            " where tenant_id = %s and status in ('active', 'requesting', 'intervened', 'human_takeover')"
                            " and coalesce(last_active_at, created_at) < %s returning id",
                            (now, tenant_id, now - timedelta(hours=hours)))
                for (cid,) in cur.fetchall():
                    _event(cur, tenant_id, cid, "resolved", None, {"auto": True, "idle_hours": hours}, now)
                    closed += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return closed
