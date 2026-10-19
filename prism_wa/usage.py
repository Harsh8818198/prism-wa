"""What the workspace has used and what it costs: Meta's own bill, read from the `pricing` it sends with every message status.

Meta bills by conversation-category ("pricing_model": "PMP" since 2025: per message, by category). It tells us, for each message
it delivered, whether it was billable and in which category, and that is kept on `messages.pricing`. Everything here adds those up.
The RATES are our estimate of Meta's India rate card (`campaigns.RATE_CARD_ESTIMATE`) and can be overridden per workspace
(`tenant_settings.settings.rate_card`), because Meta changes its rates and the owner knows what they were actually charged. Every
amount is labelled an estimate: the real invoice is Meta's.

    summary()      messages and estimated cost by day and by category, for any range
    plan_view()    the plan, each limit against what is used, and where the month's spend stands
    rates() / set_rates()   the rate card in force
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from psycopg2.extras import Json

from prism_wa import ai_agent, campaigns, contact_book, media, plans
from prism_wa.jobs import utcnow

CATEGORIES = ("MARKETING", "UTILITY", "AUTHENTICATION", "SERVICE")
DEFAULT_RATES = {**campaigns.RATE_CARD_ESTIMATE, "SERVICE": 0.0}
GST = campaigns.GST
MAX_DAYS = 400


class UsageError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def rates(cur, tenant_id) -> dict:
    cur.execute("select settings->'rate_card' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    custom = row[0] if row and isinstance(row[0], dict) else {}
    out = dict(DEFAULT_RATES)
    for k, v in custom.items():
        if str(k).upper() in CATEGORIES and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1000:
            out[str(k).upper()] = float(v)
    return out


def set_rates(cur, tenant_id, new: dict) -> dict:
    """Save the rate card (rupees per billable message, before GST). An empty value puts a category back to the default."""
    clean = {}
    for k, v in (new or {}).items():
        key = str(k).upper()
        if key not in CATEGORIES:
            raise UsageError("bad_rate", "%r is not a message category (%s)." % (k, ", ".join(c.title() for c in CATEGORIES)))
        if v in (None, ""):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1000:
            raise UsageError("bad_rate", "The rate for %s must be an amount in rupees, from 0 to 1000." % key.title())
        clean[key] = round(float(v), 4)
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, jsonb_build_object('rate_card', %s::jsonb))"
                " on conflict (tenant_id) do update set settings = jsonb_set(tenant_settings.settings, '{rate_card}', %s::jsonb), updated_at = now()",
                (tenant_id, Json(clean), Json(clean)))
    return rates(cur, tenant_id)


def _range(since, until, now: Optional[dt.datetime] = None) -> tuple:
    now = now or utcnow()
    until = until or now + dt.timedelta(seconds=1)
    since = since or until - dt.timedelta(days=30)
    if isinstance(since, str):
        since = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
    if isinstance(until, str):
        until = dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=dt.timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    if until <= since:
        raise UsageError("bad_range", "The end of the range must be after its start.")
    if (until - since).days > MAX_DAYS:
        raise UsageError("range_too_long", "Pick a range of at most %d days." % MAX_DAYS)
    return since, until


def summary(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    """Messages sent and the estimated cost, by day and by category, from Meta's own `pricing` on each message."""
    since, until = _range(since, until, now)
    card = rates(cur, tenant_id)
    cur.execute("select (m.created_at at time zone 'UTC')::date, upper(coalesce(m.pricing->>'category', 'unknown')),"
                "       count(*), count(*) filter (where coalesce((m.pricing->>'billable')::boolean, false))"
                " from messages m where m.tenant_id = %s and m.direction = 'out' and m.created_at >= %s and m.created_at < %s"
                " group by 1, 2 order by 1, 2", (tenant_id, since, until))
    days, by_cat = {}, {}
    for day, category, messages, billable in cur.fetchall():
        rate = card.get(category, 0.0)
        cost = round(billable * rate, 4)
        d = days.setdefault(str(day), {"day": str(day), "messages": 0, "billable": 0, "cost": 0.0, "by_category": {}})
        d["messages"] += messages
        d["billable"] += billable
        d["cost"] = round(d["cost"] + cost, 4)
        d["by_category"][category] = {"messages": messages, "billable": billable, "cost": cost}
        c = by_cat.setdefault(category, {"category": category, "messages": 0, "billable": 0, "cost": 0.0, "rate": rate})
        c["messages"] += messages
        c["billable"] += billable
        c["cost"] = round(c["cost"] + cost, 4)
    total_cost = round(sum(c["cost"] for c in by_cat.values()), 2)
    return {"range": {"since": since.isoformat(), "until": until.isoformat()}, "days": list(days.values()),
            "by_category": sorted(by_cat.values(), key=lambda c: -c["cost"]),
            "totals": {"messages": sum(c["messages"] for c in by_cat.values()), "billable": sum(c["billable"] for c in by_cat.values()),
                       "cost": total_cost, "cost_with_gst": round(total_cost * (1 + GST), 2)},
            "rates": card, "gst": GST, "is_estimate": True}


def _count(cur, sql: str, params: tuple) -> int:
    cur.execute(sql, params)
    return int(cur.fetchone()[0])


def plan_view(cur, tenant_id, *, now: Optional[dt.datetime] = None) -> dict:
    """The plan, each ceiling against what is in use, and this month's estimated Meta spend so far."""
    now = now or utcnow()
    plan = plans.plan_of(cur, tenant_id)
    limits = plans.LIMITS[plan]
    q = media.quota(cur, tenant_id)
    ai = ai_agent.usage(cur, tenant_id, now=now)
    used = {"tags": len(contact_book._known_tags(cur, tenant_id)),
            "attributes": _count(cur, "select count(*) from attribute_defs where tenant_id = %s", (tenant_id,)),
            "segments": _count(cur, "select count(*) from segments where tenant_id = %s", (tenant_id,)),
            "agents": _count(cur, "select count(*) from agents where tenant_id = %s and active", (tenant_id,)),
            "flows": _count(cur, "select count(*) from flows where tenant_id = %s", (tenant_id,)),
            "webhooks": _count(cur, "select count(*) from webhook_endpoints where tenant_id = %s", (tenant_id,)),
            "ai_messages": ai["replies"], "storage_mb": q["used"] // media.MB}
    rows = [{"key": k, "limit": limits.get(k), "used": used.get(k, 0)} for k in
            ("agents", "tags", "attributes", "segments", "flows", "webhooks", "ai_messages", "storage_mb", "mps")]
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = summary(cur, tenant_id, month_start, now + dt.timedelta(seconds=1), now=now)
    return {"plan": plan, "label": plans.LABELS[plan], "limits": rows, "features": sorted(plans.FEATURES[plan]),
            "next_plan": next((p for p in plans.ORDER[plans.ORDER.index(plan) + 1:]), None),
            "month": {"label": now.strftime("%B %Y"), "totals": month["totals"], "by_category": month["by_category"]},
            "storage": q, "ai": ai, "rates": month["rates"], "gst": GST, "is_estimate": True}
