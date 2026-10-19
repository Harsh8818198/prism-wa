"""Automatic messages: a template sent because of a date a contact has, or a short series sent after something happens to them.

    date   "Happy birthday" on the day, "your appointment is tomorrow", "your payment was due 3 days ago" — a template sent on, before or after a date held in a
           contact attribute. Birthdays and anniversaries come back every year; an appointment or a due date happens once.
    drip   a series — hello today, a reminder in 2 days, an offer in 7 — started for a contact when they get a tag, opt in, or when the owner adds them. It stops for
           anyone who replies, gets a tag you name, or opts out, so nobody is chased after they answered.

Both only ever send an APPROVED template (the only thing that may start a conversation), only to people who have not opted out, and each message at most once per
person per occasion (the log is unique on automatic message, person and occasion). Sending goes through the same queue as everything else, so it is rate-limited,
retried and shown in the person's chat like any other message.

A date message is checked once a day at the time the owner chooses, in the owner's time zone. If the worker was down, up to the last two missed days are caught up;
nobody gets the same birthday message twice. Switching a drip on does NOT enrol everyone who already has the tag — only people who get it afterwards — unless the
owner says to include them.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from prism_wa import campaigns, plans, schedules, send
from prism_wa.jobs import utcnow

log = logging.getLogger("prism_wa.autos")

KINDS = ("date", "drip")
MAX_AUTOS = 30
MAX_STEPS = 10
MAX_DELAY_MINUTES = 90 * 24 * 60
MAX_OFFSET_DAYS = 60
BATCH = 500
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_VALUE = r"^\d{4}-\d{2}-\d{2}$"
FEATURE = "scheduler"
COLS = ("id", "kind", "name", "enabled", "config", "next_run_at", "last_run_at", "runs", "last_error", "created_at", "updated_at")


class AutoError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# ── what the owner may ask for ───────────────────────────────────────────────

def _template(cur, tenant_id, spec) -> dict:
    spec = spec if isinstance(spec, dict) else {}
    name = str(spec.get("name") or "").strip()
    if not name:
        raise AutoError("bad_template", "Choose the template to send.")
    for key, v in (spec.get("variables") or {}).items():
        problem = campaigns._mapping_problem(v)
        if problem:
            raise AutoError("bad_template", "Variable %s: %s" % (key, problem))
        if isinstance(v, dict) and v.get("source") == "tracked_link":
            raise AutoError("bad_template", "A tracked link needs a campaign. Use a plain web address here.")
    return {"name": name, "language": str(spec.get("language") or "en_US"), "variables": spec.get("variables") or {}}


def _time_zone(config) -> tuple:
    time_, tz = str(config.get("time") or "10:00"), str(config.get("tz") or "Asia/Kolkata")
    if not TIME_RE.match(time_):
        raise AutoError("bad_time", "The time must look like 10:00.")
    try:
        ZoneInfo(tz)
    except Exception:                                                  # noqa: BLE001
        raise AutoError("bad_time", "That time zone is not known.") from None
    return time_, tz


def clean_config(cur, tenant_id, kind: str, config) -> dict:
    if kind not in KINDS:
        raise AutoError("bad_kind", "An automatic message is a date message or a drip.")
    config = config if isinstance(config, dict) else {}
    if kind == "date":
        attribute = str(config.get("attribute") or "").strip()
        cur.execute("select type from attribute_defs where tenant_id = %s and key = %s", (tenant_id, attribute))
        row = cur.fetchone()
        if row is None or row[0] != "date":
            raise AutoError("bad_attribute", "Choose a contact detail that holds a date (add one in Contacts, Attributes, of the type Date).")
        match = config.get("match") or "yearly"
        if match not in ("yearly", "once"):
            raise AutoError("bad_match", "A date comes round every year, or happens once.")
        offset = config.get("offset_days", 0)
        if not (isinstance(offset, int) and not isinstance(offset, bool) and -MAX_OFFSET_DAYS <= offset <= MAX_OFFSET_DAYS):
            raise AutoError("bad_offset", "Send it on the day, or up to %d days before or after." % MAX_OFFSET_DAYS)
        time_, tz = _time_zone(config)
        tags = config.get("tags") or []
        if not (isinstance(tags, list) and len(tags) <= 5 and all(isinstance(t, str) and 1 <= len(t.strip()) <= 40 for t in tags)):
            raise AutoError("bad_tags", "Limit it to up to 5 tags, each up to 40 characters.")
        return {"attribute": attribute, "match": match, "offset_days": offset, "time": time_, "tz": tz, "template": _template(cur, tenant_id, config.get("template")),
                "tags": [t.strip() for t in tags]}
    trigger = config.get("trigger") if isinstance(config.get("trigger"), dict) else {}
    ttype = trigger.get("type") or "manual"
    if ttype not in ("tag", "opt_in", "manual"):
        raise AutoError("bad_trigger", "A drip starts when someone gets a tag, opts in, or when you add them.")
    tag = str(trigger.get("tag") or "").strip()
    if ttype == "tag" and not 1 <= len(tag) <= 40:
        raise AutoError("bad_trigger", "Name the tag that starts the drip.")
    steps = config.get("steps")
    if not (isinstance(steps, list) and 1 <= len(steps) <= MAX_STEPS):
        raise AutoError("bad_steps", "A drip has 1 to %d messages." % MAX_STEPS)
    clean_steps = []
    for s in steps:
        s = s if isinstance(s, dict) else {}
        delay = s.get("delay_minutes", 0)
        if not (isinstance(delay, int) and not isinstance(delay, bool) and 0 <= delay <= MAX_DELAY_MINUTES):
            raise AutoError("bad_delay", "Wait from 0 minutes to 90 days between messages.")
        clean_steps.append({"delay_minutes": delay, "template": _template(cur, tenant_id, s.get("template"))})
    stop = config.get("stop") if isinstance(config.get("stop"), dict) else {}
    stop_tags = stop.get("tags") or []
    if not (isinstance(stop_tags, list) and len(stop_tags) <= 5 and all(isinstance(t, str) and 1 <= len(t.strip()) <= 40 for t in stop_tags)):
        raise AutoError("bad_stop", "Stop on up to 5 tags, each up to 40 characters.")
    quiet = config.get("quiet") if isinstance(config.get("quiet"), dict) else None
    if quiet and not (TIME_RE.match(str(quiet.get("from") or "")) and TIME_RE.match(str(quiet.get("to") or "")) and quiet["from"] != quiet["to"]):
        raise AutoError("bad_quiet", "Send between two different times, like 09:00 and 20:00.")
    _, tz = _time_zone({"tz": config.get("tz")})
    out = {"trigger": {"type": ttype, **({"tag": tag} if ttype == "tag" else {})}, "steps": clean_steps,
           "stop": {"on_reply": bool(stop.get("on_reply", True)), "tags": [t.strip() for t in stop_tags]}, "tz": tz,
           "include_existing": bool(config.get("include_existing"))}
    if quiet:
        out["quiet"] = {"from": quiet["from"], "to": quiet["to"]}
    if config.get("since"):
        out["since"] = str(config["since"])
    return out


# ── looking after them ───────────────────────────────────────────────────────

def _row(cur, tenant_id, auto_id, *, lock: bool = False) -> dict:
    cur.execute("select %s from autos where id = %%s and tenant_id = %%s" % ", ".join(COLS) + (" for update" if lock else ""), (auto_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise AutoError("unknown_auto", "that automatic message is not in this workspace")
    return dict(zip(COLS, row))


def _name(name) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise AutoError("bad_name", "Give it a name (up to 80 characters).")
    return name


def create(cur, tenant_id, kind: str, name: str, config: dict, *, created_by=None) -> str:
    plans.require(cur, tenant_id, FEATURE, "Automatic messages")
    name = _name(name)
    cfg = clean_config(cur, tenant_id, kind, config)
    cur.execute("select count(*) from autos where tenant_id = %s", (tenant_id,))
    if cur.fetchone()[0] >= MAX_AUTOS:
        raise AutoError("too_many", "A workspace can have up to %d automatic messages." % MAX_AUTOS)
    cur.execute("insert into autos (tenant_id, kind, name, config, created_by) values (%s, %s, %s, %s, %s) returning id", (tenant_id, kind, name, Json(cfg), created_by))
    return cur.fetchone()[0]


def update(cur, tenant_id, auto_id, *, name=None, config=None) -> dict:
    a = _row(cur, tenant_id, auto_id, lock=True)
    new_name = _name(name) if name is not None else a["name"]
    new_cfg = clean_config(cur, tenant_id, a["kind"], config) if config is not None else a["config"]
    if config is not None and a["kind"] == "drip" and a["config"].get("since"):
        new_cfg["since"] = a["config"]["since"]
    nxt = _first_run(new_cfg, utcnow()) if a["kind"] == "date" and a["enabled"] else a["next_run_at"]
    cur.execute("update autos set name = %s, config = %s, next_run_at = %s, updated_at = now() where id = %s", (new_name, Json(new_cfg), nxt, auto_id))
    return _row(cur, tenant_id, auto_id)


def delete(cur, tenant_id, auto_id) -> bool:
    cur.execute("delete from autos where id = %s and tenant_id = %s", (auto_id, tenant_id))
    return cur.rowcount == 1


def _first_run(cfg: dict, now: datetime) -> Optional[datetime]:
    return schedules.next_occurrence({"freq": "daily", "time": cfg["time"], "tz": cfg["tz"]}, now)


def _approved(cur, tenant_id, spec: dict) -> None:
    cur.execute("select status from templates where tenant_id = %s and name = %s and language = %s limit 1", (tenant_id, spec["name"], spec["language"]))
    row = cur.fetchone()
    if row is None:
        raise AutoError("template_unknown", "The template “%s” is not known for this account. Sync your templates first." % spec["name"])
    if row[0] != "APPROVED":
        raise AutoError("template_not_approved", "The template “%s” is %s, so it cannot be sent yet." % (spec["name"], str(row[0]).lower()))


def set_enabled(cur, tenant_id, auto_id, enabled: bool, *, now: Optional[datetime] = None) -> dict:
    a = _row(cur, tenant_id, auto_id, lock=True)
    now = now or utcnow()
    if not enabled:
        cur.execute("update autos set enabled = false, next_run_at = null, updated_at = now() where id = %s", (auto_id,))
        return _row(cur, tenant_id, auto_id)
    plans.require(cur, tenant_id, FEATURE, "Automatic messages")
    cfg = a["config"]
    for spec in ([cfg["template"]] if a["kind"] == "date" else [s["template"] for s in cfg["steps"]]):
        _approved(cur, tenant_id, spec)
    if a["kind"] == "date":
        cur.execute("update autos set enabled = true, next_run_at = %s, last_error = null, updated_at = now() where id = %s", (_first_run(cfg, now), auto_id))
    else:
        cfg = {**cfg, "since": now.isoformat()}
        if cfg["trigger"]["type"] == "tag" and not cfg.get("include_existing"):                 # people who already have the tag are not chased
            cur.execute("insert into auto_enrolments (auto_id, contact_id, tenant_id, state, step, finished_at, reason)"
                        " select %s, c.id, %s, 'stopped', 0, %s, %s from contacts c where c.tenant_id = %s and %s = any(coalesce(c.tags, '{}'))"
                        " on conflict do nothing", (auto_id, tenant_id, now, "had the tag before this was switched on", tenant_id, cfg["trigger"]["tag"]))
        cur.execute("update autos set enabled = true, config = %s, last_error = null, updated_at = now() where id = %s", (Json(cfg), auto_id))
    return _row(cur, tenant_id, auto_id)


def list_autos(cur, tenant_id) -> list:
    cur.execute("select %s from autos where tenant_id = %%s order by created_at" % ", ".join(COLS), (tenant_id,))
    rows = [dict(zip(COLS, r)) for r in cur.fetchall()]
    cur.execute("select auto_id, state, count(*) from auto_log where tenant_id = %s group by 1, 2", (tenant_id,))
    sent: dict = {}
    for aid, state, n in cur.fetchall():
        sent.setdefault(aid, {})[state] = n
    cur.execute("select auto_id, count(*) from auto_enrolments where tenant_id = %s and state = 'active' group by 1", (tenant_id,))
    active = dict(cur.fetchall())
    for r in rows:
        r["sent"], r["skipped"], r["failed"] = (sent.get(r["id"], {}).get(k, 0) for k in ("sent", "skipped", "failed"))
        r["active"] = active.get(r["id"], 0)
    return rows


def get(cur, tenant_id, auto_id) -> dict:
    a = _row(cur, tenant_id, auto_id)
    return {**a, **stats(cur, tenant_id, auto_id)}


def stats(cur, tenant_id, auto_id) -> dict:
    _row(cur, tenant_id, auto_id)
    cur.execute("select state, count(*) from auto_log where auto_id = %s group by 1", (auto_id,))
    log_counts = dict(cur.fetchall())
    cur.execute("select state, count(*) from auto_enrolments where auto_id = %s group by 1", (auto_id,))
    people = dict(cur.fetchall())
    cur.execute("select reason, count(*) from auto_enrolments where auto_id = %s and state = 'stopped' group by 1 order by 2 desc limit 5", (auto_id,))
    return {"sent": log_counts.get("sent", 0), "skipped": log_counts.get("skipped", 0), "failed": log_counts.get("failed", 0),
            "active": people.get("active", 0), "done": people.get("done", 0), "stopped": people.get("stopped", 0), "stopped_why": [{"reason": r or "", "count": n} for r, n in cur.fetchall()]}


def recent(cur, tenant_id, auto_id, limit: int = 50) -> list:
    _row(cur, tenant_id, auto_id)
    cur.execute("select l.id, c.name, c.phone, l.occasion, l.state, l.reason, l.at from auto_log l join contacts c on c.id = l.contact_id where l.auto_id = %s order by l.at desc, l.id desc limit %s",
                (auto_id, min(limit, 200)))
    return [dict(zip(("id", "name", "phone", "occasion", "state", "reason", "at"), r)) for r in cur.fetchall()]


# ── date messages ────────────────────────────────────────────────────────────

def _days_for(cfg: dict, local_day: date) -> tuple:
    """(month-day patterns or a full date, occasion) of the attribute dates that fall due on `local_day`."""
    target = local_day - timedelta(days=cfg["offset_days"])
    if cfg["match"] == "once":
        return [target.isoformat()], target.isoformat()
    md = "%02d-%02d" % (target.month, target.day)
    patterns = [md] + (["02-29"] if md == "02-28" and not calendar.isleap(target.year) else [])        # a 29 February birthday is kept on 28 February in other years
    return patterns, str(target.year)


def _due_contacts(cur, tenant_id, cfg: dict, local_day: date, limit: int = BATCH * 10) -> tuple:
    keys, occasion = _days_for(cfg, local_day)
    clause = "substr(a.value #>> '{}', 6, 5) = any(%s)" if cfg["match"] == "yearly" else "(a.value #>> '{}') = any(%s)"
    sql = ("select c.id, c.name from contact_attributes a join contacts c on c.id = a.contact_id where a.tenant_id = %s and a.key = %s"
           " and (a.value #>> '{}') ~ %s and " + clause + " and c.opt_out = false and c.blocked = false and (c.phone is not null or c.bsuid is not null)")
    params: list = [tenant_id, cfg["attribute"], DATE_VALUE, keys]
    if cfg.get("tags"):
        sql += " and coalesce(c.tags, '{}') && %s"
        params.append(cfg["tags"])
    sql += " order by c.name nulls last, c.id limit %s"
    params.append(limit)
    cur.execute(sql, params)
    return [(r[0], r[1]) for r in cur.fetchall()], occasion


def preview(cur, tenant_id, config: dict, *, days: int = 7, now: Optional[datetime] = None) -> dict:
    """Who a date message would reach on each of the next days, so the owner sees it do something before switching it on."""
    config = dict(config or {})
    if not (config.get("template") or {}).get("name"):                 # who it would reach does not depend on what is sent; no template is chosen yet
        config["template"] = {"name": "preview"}
    cfg = clean_config(cur, tenant_id, "date", config)
    today = (now or utcnow()).astimezone(ZoneInfo(cfg["tz"])).date()
    out = []
    for i in range(max(1, min(days, 31))):
        day = today + timedelta(days=i)
        people, _ = _due_contacts(cur, tenant_id, cfg, day, limit=200)
        out.append({"date": day.isoformat(), "count": len(people), "names": [n or "" for _id, n in people[:3]]})
    return {"days": out, "total": sum(d["count"] for d in out)}


def _components(cur, tenant_id, contact_id, template: dict):
    cur.execute("select t.components from templates t where t.tenant_id = %s and t.name = %s and t.language = %s limit 1", (tenant_id, template["name"], template["language"]))
    row = cur.fetchone()
    if row is None or not template.get("variables"):
        return None
    cur.execute("select name, phone from contacts where id = %s", (contact_id,))
    c = cur.fetchone()
    cur.execute("select key, value #>> '{}' from contact_attributes where contact_id = %s", (contact_id,))
    attrs = {k: v for k, v in cur.fetchall()}
    built = campaigns.build_components(row[0], template["variables"], {"name": c[0] or "", "phone": c[1] or ""}, attrs, make_link=lambda target: target,
                                       campaign_id=None, recipient_id=None)
    built = [x for x in built if not (x.get("type") == "button" and x.get("sub_type") == "quick_reply")]       # a reply button here has no campaign to report to
    return built or None


def _deliver(cur, tenant_id, auto_id, contact_id, occasion: str, template: dict, now: datetime) -> str:
    """Send one message once. -> 'sent', 'skipped' or 'failed' (already done = 'again')."""
    cur.execute("insert into auto_log (tenant_id, auto_id, contact_id, occasion, state) values (%s, %s, %s, %s, 'sent') on conflict do nothing returning id",
                (tenant_id, auto_id, contact_id, occasion))
    row = cur.fetchone()
    if row is None:
        return "again"
    cur.execute("savepoint auto_send")
    state, reason = "sent", None
    try:
        send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=template["name"], language=template["language"],
                              components=_components(cur, tenant_id, contact_id, template), idem_key="auto:%s:%s:%s" % (auto_id, contact_id, occasion), now=now, sender="bot")
    except send.SendRefused as why:
        cur.execute("rollback to savepoint auto_send")
        state, reason = "skipped", str(why)
    except Exception as why:                                        # noqa: BLE001  (one person must not stop the rest)
        cur.execute("rollback to savepoint auto_send")
        log.exception("automatic message %s could not be sent to %s", auto_id, contact_id)
        state, reason = "failed", "unexpected: %s" % why
    cur.execute("release savepoint auto_send")
    if state != "sent":
        cur.execute("update auto_log set state = %s, reason = %s where id = %s", (state, (reason or "")[:300], row[0]))
    return state


def _run_date(cur, a: dict, now: datetime) -> int:
    cfg = a["config"]
    tz = ZoneInfo(cfg["tz"])
    today = now.astimezone(tz).date()
    behind = (today - a["next_run_at"].astimezone(tz).date()).days if a["next_run_at"] else 0
    sent = 0
    for back in range(min(max(behind, 0), 2), -1, -1):
        people, occasion = _due_contacts(cur, a["tenant_id"], cfg, today - timedelta(days=back))
        for contact_id, _name in people:
            if _deliver(cur, a["tenant_id"], a["id"], contact_id, occasion, cfg["template"], now) == "sent":
                sent += 1
    return sent


# ── drips ────────────────────────────────────────────────────────────────────

def _first_wait(cfg: dict) -> int:
    return cfg["steps"][0]["delay_minutes"]


def _enrol_new(cur, a: dict, now: datetime) -> int:
    cfg, tenant_id = a["config"], a["tenant_id"]
    due = now + timedelta(minutes=_first_wait(cfg))
    trig = cfg["trigger"]
    if trig["type"] == "tag":
        cur.execute("insert into auto_enrolments (auto_id, contact_id, tenant_id, step, next_at, started_at) select %s, c.id, %s, 0, %s, %s from contacts c"
                    " where c.tenant_id = %s and %s = any(coalesce(c.tags, '{}')) and c.opt_out = false and c.blocked = false and (c.phone is not null or c.bsuid is not null)"
                    " on conflict do nothing", (a["id"], tenant_id, due, now, tenant_id, trig["tag"]))
    elif trig["type"] == "opt_in":
        cur.execute("insert into auto_enrolments (auto_id, contact_id, tenant_id, step, next_at, started_at) select distinct %s, c.id, %s, 0, %s, %s from consent_ledger l"
                    " join contacts c on c.id = l.contact_id where l.tenant_id = %s and l.action = 'opt_in' and l.at >= %s and c.opt_out = false and c.blocked = false"
                    " and (c.phone is not null or c.bsuid is not null) on conflict do nothing", (a["id"], tenant_id, due, now, tenant_id, cfg.get("since") or a["created_at"]))
    else:
        return 0
    return cur.rowcount


def enrol(cur, tenant_id, auto_id, audience: dict, *, now: Optional[datetime] = None) -> dict:
    """Add people to a drip by hand (a segment, some tags, a list of contacts). Anyone already in it, or who has opted out, is left out."""
    a = _row(cur, tenant_id, auto_id, lock=True)
    if a["kind"] != "drip":
        raise AutoError("not_a_drip", "Only a drip can have people added to it.")
    now = now or utcnow()
    ids, _removed = campaigns.resolve_audience(cur, tenant_id, audience or {}, now=now)
    n = 0
    for contact_id in ids[:5000]:
        cur.execute("insert into auto_enrolments (auto_id, contact_id, tenant_id, step, next_at, started_at) values (%s, %s, %s, 0, %s, %s) on conflict do nothing",
                    (auto_id, contact_id, tenant_id, now + timedelta(minutes=_first_wait(a["config"])), now))
        n += cur.rowcount
    return {"enrolled": n, "already_in": min(len(ids), 5000) - n, "left_out": _removed}


def _in_window(quiet: Optional[dict], local: datetime) -> Optional[datetime]:
    """None when it is fine to send now; otherwise the next moment (local) the window opens."""
    if not quiet:
        return None
    start, end = quiet["from"], quiet["to"]
    t = local.strftime("%H:%M")
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if inside:
        return None
    hh, mm = int(start[:2]), int(start[3:5])
    opens = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return opens if opens > local else opens + timedelta(days=1)


def _run_drip(cur, a: dict, now: datetime) -> int:
    cfg, tenant_id = a["config"], a["tenant_id"]
    if cfg["trigger"]["type"] in ("tag", "opt_in"):
        _enrol_new(cur, a, now)
    cur.execute("select e.contact_id, e.step, e.started_at, c.opt_out, c.blocked, coalesce(c.tags, '{}') from auto_enrolments e join contacts c on c.id = e.contact_id"
                " where e.auto_id = %s and e.state = 'active' and e.next_at <= %s order by e.next_at limit %s for update of e skip locked", (a["id"], now, BATCH))
    sent = 0
    zone = ZoneInfo(cfg["tz"])
    for contact_id, step, started, opted_out, blocked, tags in cur.fetchall():
        stop = None
        if opted_out or blocked:
            stop = "opted out" if opted_out else "blocked"
        elif set(tags) & set(cfg["stop"]["tags"]):
            stop = "got the tag “%s”" % sorted(set(tags) & set(cfg["stop"]["tags"]))[0]
        elif cfg["stop"]["on_reply"]:
            cur.execute("select exists (select 1 from messages m join conversations v on v.id = m.conversation_id where v.contact_id = %s and m.direction = 'in' and m.created_at > %s)",
                        (contact_id, started))
            if cur.fetchone()[0]:
                stop = "replied"
        if stop:
            cur.execute("update auto_enrolments set state = 'stopped', reason = %s, finished_at = %s, next_at = null where auto_id = %s and contact_id = %s", (stop, now, a["id"], contact_id))
            continue
        wait = _in_window(cfg.get("quiet"), now.astimezone(zone))
        if wait is not None:
            cur.execute("update auto_enrolments set next_at = %s where auto_id = %s and contact_id = %s", (wait.astimezone(timezone.utc), a["id"], contact_id))
            continue
        if _deliver(cur, tenant_id, a["id"], contact_id, "step-%d" % step, cfg["steps"][step]["template"], now) == "sent":
            sent += 1
        if step + 1 >= len(cfg["steps"]):
            cur.execute("update auto_enrolments set state = 'done', step = %s, finished_at = %s, next_at = null where auto_id = %s and contact_id = %s", (step + 1, now, a["id"], contact_id))
        else:
            cur.execute("update auto_enrolments set step = %s, next_at = %s where auto_id = %s and contact_id = %s",
                        (step + 1, now + timedelta(minutes=cfg["steps"][step + 1]["delay_minutes"]), a["id"], contact_id))
    return sent


# ── the worker's pass ────────────────────────────────────────────────────────

def run_due(cur, *, now: Optional[datetime] = None, limit: int = 20) -> int:
    """Date messages whose daily check is due, and every switched-on drip. -> how many messages were queued."""
    now = now or utcnow()
    cur.execute("select %s from autos where enabled and ((kind = 'date' and next_run_at <= %%s) or kind = 'drip') order by coalesce(next_run_at, %%s) limit %%s for update skip locked"
                % ", ".join(COLS + ("tenant_id",)), (now, now, limit))
    todo = [dict(zip(COLS + ("tenant_id",), r)) for r in cur.fetchall()]
    queued = 0
    for a in todo:
        cur.execute("savepoint auto_run")
        error: Optional[str] = None
        n = 0
        try:
            plans.require(cur, a["tenant_id"], FEATURE, "Automatic messages")
            n = _run_date(cur, a, now) if a["kind"] == "date" else _run_drip(cur, a, now)
        except plans.PlanLimit as why:
            error = str(why)
        except Exception as why:                                    # noqa: BLE001  (one broken automatic message must not stop the others)
            log.exception("automatic message %s failed", a["id"])
            error = "unexpected: %s" % why
        if error is not None:
            cur.execute("rollback to savepoint auto_run")
        cur.execute("release savepoint auto_run")
        queued += n
        nxt = _first_run(a["config"], now) if a["kind"] == "date" else None
        cur.execute("update autos set runs = runs + %s, last_run_at = %s, last_error = %s, next_run_at = %s where id = %s",
                    (0 if error else 1, now, error, nxt if a["kind"] == "date" else a["next_run_at"], a["id"]))
    return queued
