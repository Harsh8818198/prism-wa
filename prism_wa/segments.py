"""Audience segments (spec WS6): a filter language, compiled to parameterised SQL.

A filter is JSON. A group combines filters; a leaf tests one thing about a contact:

    {"all": [                                       # every one of these
        {"field": "tags",  "op": "has_any", "value": ["vip", "builder"]},
        {"any": [                                   # at least one of these
            {"field": "attr:City", "op": "eq", "value": "Vadodara"},
            {"field": "last_seen", "op": "within_days", "value": 30}]},
        {"not": {"field": "opted_out", "op": "is", "value": true}}]}

Fields and operators
    name, email        eq ne contains starts_with is_set is_not_set
    mobile             eq starts_with contains          (compared as digits)
    tags               has has_any has_all has_none
    source             eq in
    first_msg_tag      eq contains is_set
    opted_in opted_out blocked    is  (true / false)
    created, last_seen before after within_days older_than_days
    conversation_status eq in
    attr:<Key>         eq ne contains starts_with in is_set is_not_set,
                       and gt gte lt lte between for number and date attributes
    campaign           sent delivered read replied clicked failed
                       (with "campaign": <id>) — retargeting: who did what in an earlier campaign
    segment            in  (value: another segment's id — exclusion lists are `not` of this)

Old saved segments are just `{"tags": [...]}` and still work (any of those tags).

Nothing is ever interpolated into SQL: values are bound parameters, field and
operator names are looked up in fixed tables, and attribute keys are validated.
A reference implementation in Python (`matches`) exists so tests can prove the
compiler and the plain-English meaning agree on random data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from prism_wa import contacts as contacts_mod
from prism_wa import plans

MAX_DEPTH = 6
MAX_NODES = 200
REPLY_WINDOW_HOURS = 24
TEXT_FIELDS = {"name": "c.name", "email": "c.email", "first_msg_tag": "c.first_msg_tag"}
FLAG_FIELDS = {"opted_in": "c.opted_in", "opted_out": "c.opt_out", "blocked": "c.blocked"}
TIME_FIELDS = {"created": "c.created_at", "last_seen": "c.last_seen"}
CAMPAIGN_OPS = ("sent", "delivered", "read", "replied", "clicked", "failed", "button")


class FilterError(ValueError):
    """The filter is not valid. The message says which part and why."""


def normalise(filter_: Any) -> dict:
    """Accept the legacy `{"tags": [...]}` shape and empty filters."""
    if not filter_:
        return {"all": []}
    if isinstance(filter_, dict) and set(filter_) <= {"tags", "name"} and "tags" in filter_:
        return {"field": "tags", "op": "has_any", "value": list(filter_["tags"] or [])} if filter_["tags"] else {"all": []}
    return filter_


@dataclass
class Compiled:
    sql: str                      # a boolean expression over contacts aliased `c`
    params: list


def _guard(op: str, allowed: tuple, field: str) -> None:
    if op not in allowed:
        raise FilterError("%s cannot use %r (allowed: %s)" % (field, op, ", ".join(allowed)))


def _list(value, field: str) -> list:
    if isinstance(value, (list, tuple)) and value:
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        return [value]
    raise FilterError("%s needs a value" % field)


def _when(value, field: str) -> datetime:
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise FilterError("%s needs a date like 2026-10-17" % field) from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days(value, field: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise FilterError("%s needs a number of days" % field) from None
    if not 0 <= n <= 36500:
        raise FilterError("%s needs a number of days between 0 and 36500" % field)
    return n


def compile_filter(filter_: Any, *, defs: dict, now: Optional[datetime] = None,
                   segment_loader=None) -> Compiled:
    """-> Compiled. `defs` is attribute_defs(); `segment_loader(id) -> (filter, dynamic)` resolves
    segment references (omitted, a `segment` leaf is an error)."""
    now = now or datetime.now(timezone.utc)
    params: list = []
    count = [0]

    def node(f, depth: int, seen: tuple) -> str:
        count[0] += 1
        if count[0] > MAX_NODES:
            raise FilterError("this filter is too large (at most %d conditions)" % MAX_NODES)
        if depth > MAX_DEPTH:
            raise FilterError("filters can be nested at most %d levels deep" % MAX_DEPTH)
        if not isinstance(f, dict):
            raise FilterError("each condition must be an object")
        if "all" in f or "any" in f:
            kind = "all" if "all" in f else "any"
            parts = f[kind]
            if not isinstance(parts, list):
                raise FilterError("%r must hold a list of conditions" % kind)
            if not parts:
                return "true" if kind == "all" else "false"
            joiner = " and " if kind == "all" else " or "
            return "(" + joiner.join(node(p, depth + 1, seen) for p in parts) + ")"
        if "not" in f:
            return "(not %s)" % node(f["not"], depth + 1, seen)
        # SQL's NULL is neither true nor false, and `not NULL` is NULL: a contact with no source
        # would drop out of BOTH "source is X" and "not source is X". Every leaf answers true or false.
        return "coalesce(%s, false)" % leaf(f, seen)

    def p(value) -> str:
        params.append(value)
        return "%s"

    def leaf(f: dict, seen: tuple) -> str:
        field, op, value = f.get("field"), f.get("op"), f.get("value")
        if not isinstance(field, str) or not isinstance(op, str):
            raise FilterError("a condition needs a field and an op")
        if field in TEXT_FIELDS:
            col = TEXT_FIELDS[field]
            _guard(op, ("eq", "ne", "contains", "starts_with", "is_set", "is_not_set"), field)
            return text_op(col, op, value, field)
        if field == "mobile":
            _guard(op, ("eq", "starts_with", "contains"), field)
            digits = re.sub(r"\D", "", str(value or ""))
            if not digits:
                raise FilterError("mobile needs digits")
            if op == "eq":
                digits = contacts_mod.normalise_phone(digits) or digits
                return "(wa_norm_phone(c.phone) = %s)" % p(digits)
            if op == "starts_with":
                return "(left(coalesce(wa_norm_phone(c.phone), ''), length(%s)) = %s)" % (p(digits), p(digits))
            return "(position(%s in coalesce(wa_norm_phone(c.phone), '')) > 0)" % p(digits)
        if field == "tags":
            _guard(op, ("has", "has_any", "has_all", "has_none"), field)
            tags = _list(value, field)
            if op == "has":
                return "(%s = any(c.tags))" % p(tags[0])
            if op == "has_any":
                return "(c.tags && %s::text[])" % p(tags)
            if op == "has_all":
                return "(c.tags @> %s::text[])" % p(tags)
            return "(not (c.tags && %s::text[]))" % p(tags)
        if field == "source":
            _guard(op, ("eq", "in"), field)
            return ("(c.source_first_seen = %s)" % p(_list(value, field)[0])) if op == "eq" else \
                   ("(c.source_first_seen = any(%s))" % p(_list(value, field)))
        if field in FLAG_FIELDS:
            _guard(op, ("is",), field)
            if not isinstance(value, bool):
                raise FilterError("%s needs true or false" % field)
            return "(%s = %s)" % (FLAG_FIELDS[field], p(value))
        if field in TIME_FIELDS:
            _guard(op, ("before", "after", "within_days", "older_than_days"), field)
            col = TIME_FIELDS[field]
            if op in ("before", "after"):
                return "(%s %s %s::timestamptz)" % (col, "<" if op == "before" else ">=", p(_when(value, field)))
            cutoff = now - timedelta(days=_days(value, field))
            if op == "within_days":
                return "(%s >= %s::timestamptz)" % (col, p(cutoff))
            return "(%s < %s::timestamptz or %s is null)" % (col, p(cutoff), col)
        if field == "conversation_status":
            _guard(op, ("eq", "in"), field)
            return "exists (select 1 from conversations v where v.contact_id = c.id and v.status = any(%s))" % \
                   p(_list(value, field))
        if field.startswith("attr:"):
            return attribute(field[5:], op, value)
        if field == "campaign":
            return campaign(f, op)
        if field == "segment":
            _guard(op, ("in",), field)
            return segment(str(value or ""), seen)
        raise FilterError("unknown field %r" % field)

    def text_op(col: str, op: str, value, field: str) -> str:
        if op == "is_set":
            return "(coalesce(%s, '') <> '')" % col
        if op == "is_not_set":
            return "(coalesce(%s, '') = '')" % col
        text = str(value or "").strip()
        if not text:
            raise FilterError("%s needs a value" % field)
        if op == "eq":
            return "(lower(coalesce(%s, '')) = lower(%s))" % (col, p(text))
        if op == "ne":
            return "(lower(coalesce(%s, '')) <> lower(%s))" % (col, p(text))
        if op == "contains":
            return "(position(lower(%s) in lower(coalesce(%s, ''))) > 0)" % (p(text), col)
        return "(left(lower(coalesce(%s, '')), length(%s)) = lower(%s))" % (col, p(text), p(text))

    def attribute(key: str, op: str, value) -> str:
        if not contacts_mod.ATTRIBUTE_KEY_RE.match(key) or key not in defs:
            raise FilterError("there is no attribute called %r" % key)
        kind, options = defs[key]["type"], defs[key]["options"]
        has = "exists (select 1 from contact_attributes a where a.contact_id = c.id and a.key = %s" % p(key)
        if op == "is_set":
            return "(%s))" % has
        if op == "is_not_set":
            return "(not %s))" % has
        cast = {"number": "::numeric", "date": "::date", "bool": "::boolean"}.get(kind, "")
        raw = "(a.value #>> '{}')%s" % cast
        ordered = kind in ("number", "date")

        def coerced(v):
            try:
                return contacts_mod.coerce(kind, v, options)
            except ValueError as exc:
                raise FilterError("%s: %s" % (key, exc)) from None

        if op in ("eq", "ne"):
            v = coerced(value)
            test = "lower(a.value #>> '{}') = lower(%s)" % p(str(v)) if kind in ("string", "enum") else \
                   "%s = %s%s" % (raw, p(v if kind != "date" else str(v)), cast if kind == "date" else "")
            return ("(%s and %s))" % (has, test)) if op == "eq" else ("(not (%s and %s)))" % (has, test))
        if op == "in":
            vals = [str(coerced(v)) for v in (value if isinstance(value, list) else [value])]
            return "(%s and lower(a.value #>> '{}') = any(%s)))" % (has, p([v.lower() for v in vals]))
        if op in ("contains", "starts_with") and kind == "string":
            text = str(value or "").strip().lower()
            if not text:
                raise FilterError("%s needs a value" % key)
            if op == "contains":
                return "(%s and position(%s in lower(a.value #>> '{}')) > 0))" % (has, p(text))
            return "(%s and left(lower(a.value #>> '{}'), length(%s)) = %s))" % (has, p(text), p(text))
        if op in ("gt", "gte", "lt", "lte") and ordered:
            sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            v = coerced(value)
            return "(%s and %s %s %s%s))" % (has, raw, sym, p(v if kind == "number" else str(v)),
                                             "::date" if kind == "date" else "")
        if op == "between" and ordered:
            if not (isinstance(value, list) and len(value) == 2):
                raise FilterError("%s: between needs two values" % key)
            lo, hi = coerced(value[0]), coerced(value[1])
            tail = "::date" if kind == "date" else ""
            return "(%s and %s between %s%s and %s%s))" % (has, raw, p(lo if kind == "number" else str(lo)), tail,
                                                           p(hi if kind == "number" else str(hi)), tail)
        raise FilterError("%s (%s) cannot use %r" % (key, kind, op))

    def campaign(f: dict, op: str) -> str:
        _guard(op, CAMPAIGN_OPS, "campaign")
        cid = str(f.get("campaign") or "")
        if not re.match(r"^[0-9a-fA-F-]{36}$", cid):
            raise FilterError("campaign needs a campaign id")
        if op == "clicked":
            return "(exists (select 1 from click_events k where k.contact_id = c.id and k.campaign_id = %s::uuid))" % p(cid)
        if op == "button":                      # pressed one particular quick-reply button (index 0, 1, 2) of that campaign's message
            try:
                index = int(f.get("button"))
            except (TypeError, ValueError):
                raise FilterError("button needs the number of the button (0, 1 or 2)") from None
            return ("(exists (select 1 from click_events k where k.contact_id = c.id and k.campaign_id = %s::uuid and k.kind = 'button'"
                    " and k.card = %s))" % (p(cid), p("button:%d" % index)))
        sent = ("exists (select 1 from outbox o join messages m on m.id = o.message_id"
                " join conversations v on v.id = m.conversation_id where v.contact_id = c.id and o.campaign_id = %s::uuid" % p(cid))
        if op == "sent":
            return "(%s and o.state = 'sent'))" % sent
        if op == "delivered":
            return "(%s and m.status in ('delivered', 'read')))" % sent
        if op == "read":
            return "(%s and m.status = 'read'))" % sent
        if op == "failed":
            return "(%s and (o.state = 'failed' or m.status = 'failed')))" % sent
        if op == "replied":
            return ("(%s and exists (select 1 from messages r where r.conversation_id = v.id and r.direction = 'in'"
                    " and r.created_at > o.updated_at and r.created_at <= o.updated_at + interval '%d hours')))"
                    % (sent, REPLY_WINDOW_HOURS))
        raise AssertionError(op)

    def segment(seg_id: str, seen: tuple) -> str:
        if segment_loader is None:
            raise FilterError("segments cannot be used here")
        if not re.match(r"^[0-9a-fA-F-]{36}$", seg_id):
            raise FilterError("segment needs a segment id")
        if seg_id in seen:
            raise FilterError("segments cannot refer to themselves")
        loaded = segment_loader(seg_id)
        if loaded is None:
            raise FilterError("that segment does not exist")
        inner, dynamic = loaded
        if not dynamic:
            return "(c.id in (select contact_id from segment_members where segment_id = %s::uuid))" % p(seg_id)
        return node(normalise(inner), 0, seen + (seg_id,))

    sql = node(normalise(filter_), 0, ())
    return Compiled(sql, params)


def resolve(cur, tenant_id, filter_: Any, *, limit: Optional[int] = None, now: Optional[datetime] = None,
            opted_in_only: bool = False) -> list:
    """The contact ids that match, for this workspace only (never opted-out ones when `opted_in_only`)."""
    defs = contacts_mod.attribute_defs(cur, tenant_id)

    def loader(seg_id):
        cur.execute("select filter, dynamic from segments where id = %s and tenant_id = %s", (seg_id, tenant_id))
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    compiled = compile_filter(filter_, defs=defs, now=now, segment_loader=loader)
    sql = "select c.id from contacts c where c.tenant_id = %s and (" + compiled.sql + ")"
    params = [tenant_id] + compiled.params
    if opted_in_only:
        sql += " and c.opt_out = false and c.blocked = false and (c.phone is not null or c.bsuid is not null)"
    sql += " order by c.id"
    if limit:
        sql += " limit %s"
        params.append(limit)
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def count(cur, tenant_id, filter_: Any, **kw) -> int:
    return len(resolve(cur, tenant_id, filter_, **kw))


def validate(cur, tenant_id, filter_: Any) -> Optional[str]:
    """None when the filter is valid, else the message to show."""
    try:
        resolve(cur, tenant_id, filter_, limit=1)
        return None
    except FilterError as exc:
        return str(exc)


# ── the plain-English meaning, in Python (what the tests compare the SQL against) ──

def matches(row: dict, filter_: Any, *, now: Optional[datetime] = None, defs: Optional[dict] = None) -> bool:
    """Does this contact satisfy the filter? `row` carries name, phone, email, tags, source, first_msg_tag,
    opted_in, opt_out, blocked, created_at, last_seen, conversation_statuses, attrs. Campaign and segment
    conditions are exercised against the database, not here."""
    now = now or datetime.now(timezone.utc)
    defs = defs or {}

    def go(f) -> bool:
        if "all" in f:
            return all(go(x) for x in f["all"])
        if "any" in f:
            return any(go(x) for x in f["any"])
        if "not" in f:
            return not go(f["not"])
        field, op, value = f["field"], f["op"], f.get("value")
        if field in TEXT_FIELDS:
            have = (row.get({"name": "name", "email": "email", "first_msg_tag": "first_msg_tag"}[field]) or "")
            return text(have, op, value)
        if field == "mobile":
            have = re.sub(r"\D", "", row.get("phone") or "")
            digits = re.sub(r"\D", "", str(value))
            if op == "eq":
                return have == (contacts_mod.normalise_phone(digits) or digits)
            return have.startswith(digits) if op == "starts_with" else digits in have
        if field == "tags":
            tags, want = set(row.get("tags") or []), _list(value, field)
            return {"has": want[0] in tags, "has_any": bool(tags & set(want)), "has_all": set(want) <= tags,
                    "has_none": not (tags & set(want))}[op]
        if field == "source":
            have = row.get("source")
            return have == _list(value, field)[0] if op == "eq" else have in _list(value, field)
        if field in FLAG_FIELDS:
            return bool(row.get({"opted_in": "opted_in", "opted_out": "opt_out", "blocked": "blocked"}[field])) is value
        if field in TIME_FIELDS:
            have = row.get({"created": "created_at", "last_seen": "last_seen"}[field])
            if op == "before":
                return have is not None and have < _when(value, field)
            if op == "after":
                return have is not None and have >= _when(value, field)
            cutoff = now - timedelta(days=_days(value, field))
            if op == "within_days":
                return have is not None and have >= cutoff
            return have is None or have < cutoff
        if field == "conversation_status":
            return bool(set(row.get("conversation_statuses") or []) & set(_list(value, field)))
        if field.startswith("attr:"):
            return attr(field[5:], op, value)
        raise AssertionError(field)

    def text(have: str, op: str, value) -> bool:
        if op == "is_set":
            return have != ""
        if op == "is_not_set":
            return have == ""
        want = str(value).strip().lower()
        have = have.lower()
        return {"eq": have == want, "ne": have != want, "contains": want in have, "starts_with": have.startswith(want)}[op]

    def attr(key: str, op: str, value) -> bool:
        kind, options = defs[key]["type"], defs[key]["options"]
        attrs = row.get("attrs") or {}
        if op == "is_set":
            return key in attrs
        if op == "is_not_set":
            return key not in attrs
        have = attrs.get(key)
        if op == "ne":
            return have is None or not _same(kind, have, contacts_mod.coerce(kind, value, options))
        if have is None:
            return False
        if op == "eq":
            return _same(kind, have, contacts_mod.coerce(kind, value, options))
        if op == "in":
            return any(_same(kind, have, contacts_mod.coerce(kind, v, options)) for v in (value if isinstance(value, list) else [value]))
        if op == "contains":
            return str(value).strip().lower() in str(have).lower()
        if op == "starts_with":
            return str(have).lower().startswith(str(value).strip().lower())
        want = contacts_mod.coerce(kind, value[0] if op == "between" else value, options)
        if op == "between":
            hi = contacts_mod.coerce(kind, value[1], options)
            return want <= have <= hi
        return {"gt": have > want, "gte": have >= want, "lt": have < want, "lte": have <= want}[op]

    def _same(kind, have, want) -> bool:
        return str(have).lower() == str(want).lower() if kind in ("string", "enum") else have == want

    return go(normalise(filter_))


# ── saving segments (the plan caps how many) ─────────────────────────────────

def save(cur, tenant_id, name: str, filter_: Any, *, dynamic: bool = True):
    """Create or replace a saved segment by name. A static one is a frozen list of who matches right now."""
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise FilterError("a segment needs a name of 1 to 80 characters")
    ids = resolve(cur, tenant_id, filter_)                    # raises FilterError for a bad filter
    cur.execute("select id from segments where tenant_id = %s and lower(name) = lower(%s)", (tenant_id, name))
    row = cur.fetchone()
    if row is None:
        cur.execute("select count(*) from segments where tenant_id = %s", (tenant_id,))
        plans.check_count(cur, tenant_id, "segments", cur.fetchone()[0], 1, "saved segments")
        cur.execute("insert into segments (tenant_id, name, filter, dynamic) values (%s, %s, %s, %s) returning id",
                    (tenant_id, name, Json(filter_ or {}), dynamic))
        seg_id = cur.fetchone()[0]
    else:
        seg_id = row[0]
        cur.execute("update segments set filter = %s, dynamic = %s where id = %s", (Json(filter_ or {}), dynamic, seg_id))
        cur.execute("delete from segment_members where segment_id = %s", (seg_id,))
    if not dynamic and ids:
        cur.execute("insert into segment_members (segment_id, contact_id, tenant_id) select %s, c, %s from unnest(%s::uuid[]) c",
                    (seg_id, tenant_id, [str(i) for i in ids]))
    return seg_id


def delete(cur, tenant_id, segment_id) -> bool:
    cur.execute("delete from segments where id = %s and tenant_id = %s", (segment_id, tenant_id))
    return cur.rowcount > 0


def list_segments(cur, tenant_id) -> list:
    cur.execute("select id, name, filter, dynamic, created_at from segments where tenant_id = %s order by lower(name)", (tenant_id,))
    out = []
    for seg_id, name, filter_, dynamic, created in cur.fetchall():
        try:
            size = len(resolve(cur, tenant_id, filter_)) if dynamic else None
        except FilterError:
            size = None
        if size is None and not dynamic:
            cur.execute("select count(*) from segment_members where segment_id = %s", (seg_id,))
            size = cur.fetchone()[0]
        out.append({"id": seg_id, "name": name, "filter": filter_, "dynamic": dynamic, "size": size, "created_at": created})
    return out
