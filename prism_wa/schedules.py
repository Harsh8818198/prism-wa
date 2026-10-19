"""Recurring campaigns (spec WS7): "every Monday at 10:00", made and launched by the worker.

A schedule is a recipe (`config`: what create_campaign takes — template, audience, variables, number, retry,
local send time) plus a rule (`rule`: how often, at what time, in which zone, until when). Each time the rule
fires the worker makes an ORDINARY campaign from the recipe and launches it, so every repeat is previewed by the
same preflight, reported, paused and cancelled like any other campaign — and shows in the history.

Rules:
    {"freq": "daily",   "time": "10:00", "tz": "Asia/Kolkata"}
    {"freq": "weekly",  "time": "10:00", "days": ["mon", "thu"], "tz": "Asia/Kolkata"}
    {"freq": "monthly", "time": "10:00", "day": 1, "tz": "Asia/Kolkata", "until": "2027-03-31", "max_runs": 12}
`day` is 1 to 28 so every month has it. `until` and `max_runs` are optional; reaching either ends the schedule.

What happens when things go wrong: a launch the preflight refuses (template no longer approved, nobody to send to...)
is recorded on the schedule and that run is skipped; three skipped runs in a row turn the schedule off so a broken
recipe does not fail forever unseen. A worker that was down fires a missed run once, not once per missed day.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from prism_wa import campaigns, plans
from prism_wa.jobs import utcnow

log = logging.getLogger("prism_wa.schedules")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_SCHEDULES = 20
MAX_FAILURES = 3
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def validate_rule(rule) -> dict:
    """The rule, cleaned; ScheduleError when it makes no sense."""
    if not isinstance(rule, dict) or rule.get("freq") not in ("daily", "weekly", "monthly"):
        raise ScheduleError("bad_rule", "Choose how often: daily, weekly or monthly.")
    out = {"freq": rule["freq"], "time": str(rule.get("time") or ""), "tz": str(rule.get("tz") or "Asia/Kolkata")}
    if not TIME_RE.match(out["time"]):
        raise ScheduleError("bad_rule", "The time must look like 10:00.")
    try:
        ZoneInfo(out["tz"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError("bad_rule", "That time zone is not known.") from None
    if out["freq"] == "weekly":
        days = rule.get("days")
        if not (isinstance(days, list) and days and all(d in DAYS for d in days)):
            raise ScheduleError("bad_rule", "Choose the days of the week.")
        out["days"] = sorted(set(days), key=DAYS.index)
    if out["freq"] == "monthly":
        day = rule.get("day")
        if not (isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 28):
            raise ScheduleError("bad_rule", "Choose the day of the month (1 to 28).")
        out["day"] = day
    if rule.get("until"):
        try:
            out["until"] = date.fromisoformat(str(rule["until"])).isoformat()
        except ValueError:
            raise ScheduleError("bad_rule", "The end date must look like 2027-03-31.") from None
    if rule.get("max_runs") is not None:
        n = rule["max_runs"]
        if not (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 365):
            raise ScheduleError("bad_rule", "The number of runs must be 1 to 365.")
        out["max_runs"] = n
    return out


def next_occurrence(rule: dict, after: datetime) -> Optional[datetime]:
    """The first moment strictly after `after` the rule fires (aware UTC), or None when `until` has passed."""
    zone = ZoneInfo(rule["tz"])
    hour, minute = int(rule["time"][:2]), int(rule["time"][3:5])
    local = after.astimezone(zone)
    until = date.fromisoformat(rule["until"]) if rule.get("until") else None
    day = local.date()
    for _ in range(0, 400):
        ok = False
        if rule["freq"] == "daily":
            ok = True
        elif rule["freq"] == "weekly":
            ok = DAYS[day.weekday()] in rule["days"]
        else:
            ok = day.day == rule["day"]
        if ok:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if candidate > local:
                if until and candidate.date() > until:
                    return None
                return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)
    return None


def _check_config(cur, tenant_id, config) -> dict:
    if not isinstance(config, dict):
        raise ScheduleError("bad_config", "The campaign to repeat is missing.")
    if not (config.get("template_name") and config.get("audience")):
        raise ScheduleError("bad_config", "Choose a template and an audience.")
    if config.get("schedule_at"):
        raise ScheduleError("bad_config", "A repeating campaign is timed by its rule, not a single date.")
    for key, spec in (config.get("variables") or {}).items():
        problem = campaigns._mapping_problem(spec)
        if problem:
            raise ScheduleError("bad_config", "Variable %s: %s" % (key, problem))
    allowed = ("template_name", "language", "audience", "variables", "number_id", "retry", "track_clicks", "local_time", "timezone")
    return {k: v for k, v in config.items() if k in allowed}


def create(cur, tenant_id, name: str, config: dict, rule: dict, *, created_by=None, now: Optional[datetime] = None) -> str:
    plans.require(cur, tenant_id, "scheduler", "Repeating campaigns")
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise ScheduleError("bad_name", "Give the repeating campaign a name (up to 80 characters).")
    config = _check_config(cur, tenant_id, config)
    rule = validate_rule(rule)
    cur.execute("select count(*) from campaign_schedules where tenant_id = %s", (tenant_id,))
    if cur.fetchone()[0] >= MAX_SCHEDULES:
        raise ScheduleError("too_many", "A workspace can have up to %d repeating campaigns." % MAX_SCHEDULES)
    first = next_occurrence(rule, now or utcnow())
    if first is None:
        raise ScheduleError("bad_rule", "That rule never fires: its end date has passed.")
    cur.execute("insert into campaign_schedules (tenant_id, name, config, rule, next_run_at, created_by) values (%s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, name, Json(config), Json(rule), first, created_by))
    return cur.fetchone()[0]


COLS = ("id", "name", "config", "rule", "enabled", "next_run_at", "last_run_at", "runs", "failures", "last_error", "created_at")


def list_schedules(cur, tenant_id) -> list:
    cur.execute("select %s from campaign_schedules where tenant_id = %%s order by created_at" % ", ".join(COLS), (tenant_id,))
    return [dict(zip(COLS, r)) for r in cur.fetchall()]


def _get(cur, tenant_id, schedule_id, *, lock: bool = False) -> dict:
    cur.execute("select %s from campaign_schedules where id = %%s and tenant_id = %%s" % ", ".join(COLS) + (" for update" if lock else ""),
                (schedule_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ScheduleError("unknown_schedule", "that repeating campaign is not in this workspace")
    return dict(zip(COLS, row))


def set_enabled(cur, tenant_id, schedule_id, enabled: bool, *, now: Optional[datetime] = None) -> dict:
    s = _get(cur, tenant_id, schedule_id, lock=True)
    if enabled:
        plans.require(cur, tenant_id, "scheduler", "Repeating campaigns")
        nxt = next_occurrence(s["rule"], now or utcnow())
        if nxt is None:
            raise ScheduleError("finished", "This repeating campaign has reached its end date.")
        cur.execute("update campaign_schedules set enabled = true, next_run_at = %s, failures = 0, last_error = null, updated_at = now() where id = %s",
                    (nxt, schedule_id))
    else:
        cur.execute("update campaign_schedules set enabled = false, next_run_at = null, updated_at = now() where id = %s", (schedule_id,))
    return _get(cur, tenant_id, schedule_id)


def delete(cur, tenant_id, schedule_id) -> bool:
    cur.execute("delete from campaign_schedules where id = %s and tenant_id = %s", (schedule_id, tenant_id))
    return cur.rowcount == 1


def run_due(cur, *, now: Optional[datetime] = None, limit: int = 20) -> int:
    """Make and launch a campaign for every schedule that is due. Called by the worker each cycle. -> how many fired."""
    now = now or utcnow()
    cur.execute("select id, tenant_id, name, config, rule, runs, failures from campaign_schedules"
                " where enabled and next_run_at <= %s order by next_run_at limit %s for update skip locked", (now, limit))
    fired = 0
    for sid, tenant_id, name, config, rule, runs, failures in cur.fetchall():
        cur.execute("savepoint sched")
        error: Optional[str] = None
        try:
            plans.require(cur, tenant_id, "scheduler", "Repeating campaigns")
            title = "%s · %s" % (name, now.astimezone(ZoneInfo(rule["tz"])).strftime("%d %b"))
            cid = campaigns.create_campaign(cur, tenant_id, name=title, schedule_id=sid, now=now,
                                            **{k: v for k, v in config.items() if k != "timezone"}, timezone_name=config.get("timezone"))
            campaigns.launch(cur, tenant_id, cid, now=now)
            fired += 1
        except campaigns.CampaignError as why:
            error = str(why)
        except plans.PlanLimit as why:
            error = str(why)
        except Exception as why:                                    # noqa: BLE001  (one broken schedule must not stop the others)
            log.exception("repeating campaign %s failed", sid)
            error = "unexpected: %s" % why
        if error is not None:
            cur.execute("rollback to savepoint sched")
        cur.execute("release savepoint sched")
        runs_now = runs + (0 if error else 1)
        failures_now = failures + 1 if error else 0
        nxt = next_occurrence(rule, now)
        finished = nxt is None or (rule.get("max_runs") and runs_now >= rule["max_runs"])
        off = finished or failures_now >= MAX_FAILURES
        cur.execute("update campaign_schedules set runs = %s, failures = %s, last_error = %s, last_run_at = %s,"
                    " enabled = %s, next_run_at = %s, updated_at = now() where id = %s",
                    (runs_now, failures_now, error, now, not off, None if off else nxt, sid))
    return fired
