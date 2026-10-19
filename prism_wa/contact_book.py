"""The contact book: everything the Contacts and Inbox screens need to treat a person as a CRM record.

    list_contacts   a filtered, sorted, paged table (with the columns AiSensy shows: source, window, state, attributes...)
    get_profile     one person: counts, campaigns received, consent history, attributes
    journey         what happened to them, newest first (tags, chats, campaigns, consent)
    create / update add one person without a CSV; change a name, email, company
    tags            a registry (a tag can exist before anyone has it), rename, delete, bulk add/remove
    attributes      typed custom fields: list with fill counts, define, delete, a value for the whole workspace
    block / erase   stop a person reaching you; remove them everywhere

Every function takes a cursor and the workspace id, and never reaches outside that workspace.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from prism_wa import consent, contacts, plans, segments

MAX_PAGE = 200
MAX_TAG = 60
SORTS = {"name": "lower(c.name)", "created": "c.created_at", "last_seen": "c.last_seen", "source": "c.source_first_seen",
         "phone": "c.phone", "tags": "cardinality(c.tags)"}
WINDOW_HOURS = 24


class BookError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _filter_sql(cur, tenant_id, filter_) -> segments.Compiled:
    defs = contacts.attribute_defs(cur, tenant_id)

    def loader(seg_id):
        cur.execute("select filter, dynamic from segments where id = %s and tenant_id = %s", (seg_id, tenant_id))
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    return segments.compile_filter(filter_ or {"all": []}, defs=defs, segment_loader=loader)


def _tag(raw) -> str:
    tag = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not tag or len(tag) > MAX_TAG or re.search(r"[|;,]", tag):
        raise BookError("bad_tag", "A tag is 1 to %d characters and cannot contain a comma, | or ;." % MAX_TAG)
    return tag


def _event(cur, tenant_id, contact_id, kind: str, actor: Optional[dict] = None, detail: Any = None) -> None:
    cur.execute("insert into contact_events (tenant_id, contact_id, kind, actor_id, actor_name, detail) values (%s,%s,%s,%s,%s,%s)",
                (tenant_id, contact_id, kind, (actor or {}).get("id"), (actor or {}).get("name"), Json(detail) if detail else None))


def _mask(phone, masked: bool):
    if not masked or not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    return "*" * max(len(digits) - 4, 0) + digits[-4:]


# ── the table ────────────────────────────────────────────────────────────────

def list_contacts(cur, tenant_id, *, filter_=None, search: str = "", sort: str = "created", direction: str = "desc",
                  limit: int = 50, offset: int = 0, masked: bool = False) -> dict:
    """-> {"contacts": [...], "total": n, "attribute_keys": [...]}."""
    compiled = _filter_sql(cur, tenant_id, filter_)
    where = ["c.tenant_id = %s", "(" + compiled.sql + ")"]
    params: list = [tenant_id] + list(compiled.params)
    text = (search or "").strip()
    if text:
        digits = re.sub(r"\D", "", text)
        clause = "(c.name ilike %s or c.email ilike %s or c.company ilike %s"
        params += ["%" + text + "%"] * 3
        if len(digits) >= 3:
            clause += " or c.phone like %s"
            params.append("%" + digits + "%")
        where.append(clause + ")")
    sql_where = " and ".join(where)
    cur.execute("select count(*) from contacts c where " + sql_where, params)
    total = cur.fetchone()[0]

    order = SORTS.get(sort, SORTS["created"])
    way = "asc" if str(direction).lower() == "asc" else "desc"
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    cur.execute(
        "select c.id, c.name, c.phone, c.email, c.company, c.tags, c.source_first_seen, c.opted_in, c.opt_out, c.blocked,"
        " c.first_msg_tag, c.last_seen, c.created_at,"
        " (select v.status from conversations v where v.contact_id = c.id order by v.last_active_at desc nulls last limit 1)"
        " from contacts c where " + sql_where + " order by " + order + " " + way + " nulls last, c.id limit %s offset %s",
        params + [limit, max(int(offset or 0), 0)])
    cols = ("id", "name", "phone", "email", "company", "tags", "source", "opted_in", "opted_out", "blocked", "first_msg_tag",
            "last_seen", "created_at", "state")
    now = _now()
    rows = []
    for raw in cur.fetchall():
        r = dict(zip(cols, raw))
        r["phone"] = _mask(r["phone"], masked)
        r["tags"] = list(r["tags"] or [])
        r["window_open"] = bool(r["last_seen"] and now - r["last_seen"] < timedelta(hours=WINDOW_HOURS))
        r["attrs"] = {}
        rows.append(r)
    keys = sorted(contacts.attribute_defs(cur, tenant_id))
    if rows and keys:
        cur.execute("select contact_id, key, value from contact_attributes where tenant_id = %s and contact_id = any(%s::uuid[])",
                    (tenant_id, [r["id"] for r in rows]))
        by_id = {r["id"]: r for r in rows}
        for contact_id, key, value in cur.fetchall():
            by_id[contact_id]["attrs"][key] = value
    return {"contacts": rows, "total": total, "attribute_keys": keys}


def ids_for(cur, tenant_id, *, ids=None, filter_=None, limit: int = 100000) -> list:
    """The contacts an action is aimed at: an explicit list, or everyone matching a filter."""
    if ids:
        cur.execute("select id from contacts where tenant_id = %s and id = any(%s::uuid[])", (tenant_id, [str(i) for i in ids]))
        return [r[0] for r in cur.fetchall()]
    return segments.resolve(cur, tenant_id, filter_ or {"all": []}, limit=limit)


# ── one person ───────────────────────────────────────────────────────────────

def _contact(cur, tenant_id, contact_id) -> dict:
    cur.execute("select id, name, phone, email, company, tags, source_first_seen, opted_in, opt_out, blocked, first_msg_tag,"
                " last_seen, created_at from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise BookError("unknown_contact", "That person is not in this workspace.")
    cols = ("id", "name", "phone", "email", "company", "tags", "source", "opted_in", "opted_out", "blocked", "first_msg_tag",
            "last_seen", "created_at")
    out = dict(zip(cols, row))
    out["tags"] = list(out["tags"] or [])
    return out


def get_profile(cur, tenant_id, contact_id, *, masked: bool = False) -> dict:
    """Everything the chat profile shows (AiSensy's "Chat profile"), in one call."""
    c = _contact(cur, tenant_id, contact_id)
    c["phone"] = _mask(c["phone"], masked)
    c["window_open"] = bool(c["last_seen"] and _now() - c["last_seen"] < timedelta(hours=WINDOW_HOURS))
    cur.execute("select key, value from contact_attributes where contact_id = %s and tenant_id = %s order by key", (contact_id, tenant_id))
    c["attrs"] = {k: v for k, v in cur.fetchall()}
    cur.execute("select v.status, v.assignee_id, (select name from agents a where a.id = v.assignee_id)"
                " from conversations v where v.contact_id = %s and v.tenant_id = %s order by v.last_active_at desc nulls last limit 1",
                (contact_id, tenant_id))
    conv = cur.fetchone()
    c["state"], c["assignee_id"], c["assignee_name"] = conv if conv else (None, None, None)
    cur.execute("select count(*) filter (where o.kind = 'template'), count(*) filter (where o.kind is distinct from 'template')"
                " from messages m join conversations v on v.id = m.conversation_id left join outbox o on o.message_id = m.id"
                " where v.contact_id = %s and v.tenant_id = %s", (contact_id, tenant_id))
    templates_sent, session = cur.fetchone()
    cur.execute("select count(*) from conversation_events e join conversations v on v.id = e.conversation_id"
                " where v.contact_id = %s and v.tenant_id = %s and e.kind = 'requested'", (contact_id, tenant_id))
    c["counts"] = {"template_messages": templates_sent or 0, "session_messages": session or 0, "unresolved_queries": cur.fetchone()[0]}
    cur.execute("select k.id, k.name, k.template_name, r.status, r.created_at from campaign_recipients r"
                " join campaigns k on k.id = r.campaign_id where r.contact_id = %s and r.tenant_id = %s"
                " order by r.created_at desc limit 20", (contact_id, tenant_id))
    c["campaigns"] = [dict(zip(("id", "name", "template", "status", "at"), r)) for r in cur.fetchall()]
    cur.execute("select action, source, evidence, at from consent_ledger where contact_id = %s order by at desc limit 10", (contact_id,))
    c["consent"] = [dict(zip(("action", "source", "evidence", "at"), r)) for r in cur.fetchall()]
    return c


def journey(cur, tenant_id, contact_id, *, limit: int = 100) -> list:
    """What happened to this person, newest first: [{kind, at, who, detail}]."""
    _contact(cur, tenant_id, contact_id)
    out = []
    cur.execute("select kind, actor_name, detail, at from contact_events where contact_id = %s and tenant_id = %s", (contact_id, tenant_id))
    out += [{"kind": k, "who": who, "detail": d or {}, "at": at} for k, who, d, at in cur.fetchall()]
    cur.execute("select e.kind, a.name, e.detail, e.at from conversation_events e join conversations v on v.id = e.conversation_id"
                " left join agents a on a.id = e.actor_id where v.contact_id = %s and v.tenant_id = %s", (contact_id, tenant_id))
    out += [{"kind": "chat_" + k, "who": who, "detail": d or {}, "at": at} for k, who, d, at in cur.fetchall()]
    cur.execute("select k.name, r.status, r.created_at from campaign_recipients r join campaigns k on k.id = r.campaign_id"
                " where r.contact_id = %s and r.tenant_id = %s", (contact_id, tenant_id))
    out += [{"kind": "campaign_sent", "who": None, "detail": {"campaign": n, "status": s}, "at": at} for n, s, at in cur.fetchall()]
    cur.execute("select action, source, evidence, at from consent_ledger where contact_id = %s", (contact_id,))
    out += [{"kind": action, "who": None, "detail": {"source": s, "evidence": e}, "at": at} for action, s, e, at in cur.fetchall()]
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:max(1, min(int(limit), 500))]


def create_contact(cur, tenant_id, *, name: str = "", phone: str = "", email: str = "", tags=(), attributes: Optional[dict] = None,
                   source: str = "MANUAL", attest_consent: bool = False, evidence: str = "", actor: Optional[dict] = None) -> dict:
    """Add one person. A number that is already here updates that person instead (-> `created` says which)."""
    normal = contacts.normalise_phone(phone)
    if normal is None:
        raise BookError("bad_phone", "%r is not a valid phone number. Use the country code, e.g. 919812345678." % (phone,))
    cur.execute("select contact_id, created from wa_resolve_contact(%s, %s, null, null, null, %s, %s)",
                (tenant_id, normal, (name or "").strip() or None, source))
    contact_id, created = cur.fetchone()
    if (email or "").strip():
        cur.execute("savepoint e")
        try:
            cur.execute("update contacts set email = %s where id = %s and tenant_id = %s", (email.strip(), contact_id, tenant_id))
            cur.execute("release savepoint e")
        except Exception:                                                  # noqa: BLE001
            cur.execute("rollback to savepoint e")
            raise BookError("email_taken", "Another contact already uses that email address.") from None
    if tags:
        set_tags(cur, tenant_id, [contact_id], add=list(tags), actor=actor)
    defs = contacts.attribute_defs(cur, tenant_id)
    for key, value in (attributes or {}).items():
        if value not in (None, ""):
            try:
                contacts.set_attribute(cur, tenant_id, contact_id, key, value, defs)
            except ValueError as exc:
                raise BookError("bad_attribute", "%s: %s" % (key, exc)) from None
    if attest_consent:
        consent.record(cur, tenant_id, contact_id, "opt_in", "manual", evidence or "added by hand and confirmed", by_person=False)
    _event(cur, tenant_id, contact_id, "created" if created else "updated", actor, {"source": source})
    return {"contact_id": str(contact_id), "created": bool(created)}


def update_contact(cur, tenant_id, contact_id, *, changes: dict, actor: Optional[dict] = None) -> dict:
    _contact(cur, tenant_id, contact_id)
    sets, params = [], []
    for key, column in (("name", "name"), ("email", "email"), ("company", "company")):
        if key in changes:
            value = (changes[key] or "").strip() or None
            if key == "name" and not value:
                raise BookError("bad_name", "A contact needs a name.")
            sets.append("%s = %%s" % column)
            params.append(value)
    if sets:
        cur.execute("savepoint u")
        try:
            cur.execute("update contacts set %s, updated_at = now() where id = %%s and tenant_id = %%s" % ", ".join(sets),
                        params + [contact_id, tenant_id])
            cur.execute("release savepoint u")
        except Exception:                                                  # noqa: BLE001
            cur.execute("rollback to savepoint u")
            raise BookError("email_taken", "Another contact already uses that email address.") from None
    defs = contacts.attribute_defs(cur, tenant_id)
    for key, value in (changes.get("attributes") or {}).items():
        if value in (None, ""):
            cur.execute("delete from contact_attributes where contact_id = %s and key = %s", (contact_id, key))
            continue
        try:
            contacts.set_attribute(cur, tenant_id, contact_id, key, value, defs)
        except ValueError as exc:
            raise BookError("bad_attribute", "%s: %s" % (key, exc)) from None
        _event(cur, tenant_id, contact_id, "attribute_set", actor, {"key": key, "value": value})
    return _contact(cur, tenant_id, contact_id)


def set_blocked(cur, tenant_id, contact_ids, blocked: bool, actor: Optional[dict] = None) -> int:
    n = 0
    for cid in contact_ids:
        cur.execute("update contacts set blocked = %s where id = %s and tenant_id = %s and blocked is distinct from %s",
                    (bool(blocked), cid, tenant_id, bool(blocked)))
        if cur.rowcount:
            n += 1
            _event(cur, tenant_id, cid, "blocked" if blocked else "unblocked", actor)
    return n


# ── tags ─────────────────────────────────────────────────────────────────────

def list_tags(cur, tenant_id) -> list:
    """[{name, color, contacts, registered}] — tags in use plus tags made on purpose."""
    cur.execute("select t, count(*) from contacts c, unnest(c.tags) t where c.tenant_id = %s group by t", (tenant_id,))
    used = dict(cur.fetchall())
    cur.execute("select name, color from tag_defs where tenant_id = %s", (tenant_id,))
    reg = {n: c for n, c in cur.fetchall()}
    names = sorted(set(used) | set(reg), key=str.lower)
    return [{"name": n, "color": reg.get(n), "contacts": used.get(n, 0), "registered": n in reg} for n in names]


def _known_tags(cur, tenant_id) -> set:
    return {t["name"] for t in list_tags(cur, tenant_id)}


def create_tag(cur, tenant_id, name: str, color: Optional[str] = None) -> str:
    tag = _tag(name)
    cur.execute("select 1 from tag_defs where tenant_id = %s and name = %s", (tenant_id, tag))
    if cur.fetchone():
        raise BookError("tag_exists", "There is already a tag called %r." % tag)
    known = _known_tags(cur, tenant_id)
    if tag not in known:                                          # a tag people already carry costs nothing more to register
        plans.check_count(cur, tenant_id, "tags", len(known), 1, "tags")
    cur.execute("insert into tag_defs (tenant_id, name, color) values (%s, %s, %s)", (tenant_id, tag, color))
    return tag


def rename_tag(cur, tenant_id, old: str, new: str) -> int:
    new = _tag(new)
    if new == old:
        return 0
    known = _known_tags(cur, tenant_id)
    if old not in known:
        raise BookError("unknown_tag", "There is no tag called %r." % old)
    if new in known:
        raise BookError("tag_exists", "There is already a tag called %r. Delete or merge that one first." % new)
    cur.execute("update contacts set tags = array_replace(tags, %s, %s) where tenant_id = %s and %s = any(tags)", (old, new, tenant_id, old))
    n = cur.rowcount
    cur.execute("update tag_defs set name = %s where tenant_id = %s and name = %s", (new, tenant_id, old))
    return n


def delete_tag(cur, tenant_id, name: str) -> int:
    cur.execute("update contacts set tags = array_remove(tags, %s) where tenant_id = %s and %s = any(tags)", (name, tenant_id, name))
    n = cur.rowcount
    cur.execute("delete from tag_defs where tenant_id = %s and name = %s", (tenant_id, name))
    return n


def set_tags(cur, tenant_id, contact_ids, *, add=(), remove=(), actor: Optional[dict] = None) -> dict:
    """Add and remove tags on many people at once. A new tag counts against the plan; past it, nothing is changed."""
    add = [_tag(t) for t in add]
    remove = [_tag(t) for t in remove]
    ids = [str(i) for i in contact_ids]
    if not ids:
        return {"changed": 0}
    if add:
        known = _known_tags(cur, tenant_id)
        fresh = [t for t in dict.fromkeys(add) if t not in known]
        if fresh:
            plans.check_count(cur, tenant_id, "tags", len(known), len(fresh), "tags")
    changed = 0
    if add:
        cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t)"
                    " where tenant_id = %s and id = any(%s::uuid[]) and not (coalesce(tags, '{}') @> %s::text[])", (add, tenant_id, ids, add))
        changed += cur.rowcount
        for cid in ids:
            _event(cur, tenant_id, cid, "tag_added", actor, {"tags": add})
    if remove:
        cur.execute("update contacts set tags = array(select t from unnest(coalesce(tags, '{}')) t where not (t = any(%s::text[])))"
                    " where tenant_id = %s and id = any(%s::uuid[]) and tags && %s::text[]", (remove, tenant_id, ids, remove))
        changed += cur.rowcount
        for cid in ids:
            _event(cur, tenant_id, cid, "tag_removed", actor, {"tags": remove})
    return {"changed": changed}


# ── attributes ───────────────────────────────────────────────────────────────

def list_attributes(cur, tenant_id) -> list:
    cur.execute("select d.key, d.type, d.is_global, d.options, (select count(*) from contact_attributes a where a.tenant_id = d.tenant_id"
                " and a.key = d.key) from attribute_defs d where d.tenant_id = %s order by d.key", (tenant_id,))
    rows = cur.fetchall()                                          # before the next query reuses the cursor
    glob = _globals(cur, tenant_id)
    return [{"key": k, "type": t, "global": g, "options": o, "filled": n, "value": glob.get(k)} for k, t, g, o, n in rows]


def _globals(cur, tenant_id) -> dict:
    cur.execute("select settings->'global_attributes' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return (row[0] if row and row[0] else {}) or {}


def define_attribute(cur, tenant_id, key: str, kind: str = "string", *, is_global: bool = False, options=None) -> dict:
    try:
        contacts.define_attribute(cur, tenant_id, key, kind, is_global=is_global, options=options)
    except ValueError as exc:
        raise BookError("bad_attribute", str(exc)) from None
    return next(a for a in list_attributes(cur, tenant_id) if a["key"] == key)


def delete_attribute(cur, tenant_id, key: str) -> bool:
    cur.execute("delete from attribute_defs where tenant_id = %s and key = %s", (tenant_id, key))
    deleted = cur.rowcount > 0
    if deleted:
        cur.execute("update tenant_settings set settings = settings #- %s::text[] where tenant_id = %s", (["global_attributes", key], tenant_id))
    return deleted


def set_global_value(cur, tenant_id, key: str, value) -> None:
    """One value for the whole workspace (a company name, a website) — usable wherever a per-person attribute is."""
    cur.execute("select type, options, is_global from attribute_defs where tenant_id = %s and key = %s", (tenant_id, key))
    row = cur.fetchone()
    if row is None:
        raise BookError("unknown_attribute", "There is no attribute called %r." % key)
    if not row[2]:
        raise BookError("not_global", "%r is a per-person attribute. Make it a workspace-wide one first." % key)
    try:
        stored = contacts.coerce(row[0], value, row[1])
    except ValueError as exc:
        raise BookError("bad_attribute", "%s: %s" % (key, exc)) from None
    payload = json.dumps(stored)
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, jsonb_build_object('global_attributes', jsonb_build_object(%s::text, %s::jsonb)))"
                " on conflict (tenant_id) do update set settings = jsonb_set(tenant_settings.settings, '{global_attributes}',"
                " coalesce(tenant_settings.settings->'global_attributes', '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb), true),"
                " updated_at = now()", (tenant_id, key, payload, key, payload))


def global_values(cur, tenant_id) -> dict:
    return _globals(cur, tenant_id)


# ── bulk and erase ───────────────────────────────────────────────────────────

def erase(cur, tenant_id, contact_ids) -> int:
    n = 0
    for cid in contact_ids:
        if contacts.erase_contact(cur, tenant_id, cid):
            n += 1
    return n


# ── import log ───────────────────────────────────────────────────────────────

def log_import(cur, tenant_id, report: dict, *, file_name: str = "", source: str = "", by_name: str = "") -> str:
    cur.execute("insert into import_log (tenant_id, file_name, source, by_name, rows, created, updated, invalid, report)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (tenant_id, (file_name or "")[:200] or None, source or None, by_name or None, int(report.get("rows") or 0),
                 int(report.get("created") or 0), int(report.get("updated") or 0), int(report.get("invalid") or 0), Json(report)))
    return str(cur.fetchone()[0])


def list_imports(cur, tenant_id, limit: int = 50) -> list:
    cur.execute("select id, file_name, source, by_name, rows, created, updated, invalid, at from import_log where tenant_id = %s"
                " order by at desc limit %s", (tenant_id, max(1, min(int(limit), 200))))
    return [dict(zip(("id", "file_name", "source", "by_name", "rows", "created", "updated", "invalid", "at"), r)) for r in cur.fetchall()]


def import_report(cur, tenant_id, import_id) -> dict:
    cur.execute("select report, file_name, at from import_log where id = %s and tenant_id = %s", (import_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise BookError("unknown_import", "That import is not in this workspace.")
    return {"report": row[0], "file_name": row[1], "at": row[2]}


def failures_csv(report: dict) -> str:
    """The rows that did not import, as a file to fix and send again."""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Row", "Why it did not import"])
    for e in report.get("errors") or []:
        w.writerow([e.get("row"), e.get("error")])
    return out.getvalue()
