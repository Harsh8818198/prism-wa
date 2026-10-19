"""The numbers behind the Analytics tab, and the CSV files a Premium workspace can download.

    dashboard()    one answer for the whole screen: totals, a series by day, spend, who is busy, opt-ins, the latest campaigns
    overview()     messages by day: sent, delivered, read, failed, received; new chats; hand-offs
    agents()       per teammate: replies, chats, chats resolved, how long they take to answer
    optins()       who opted in or out, by day and by where it came from
    campaign_rows()  the campaigns in the range with their results
    download()     any of those as a CSV file (Premium)

Every figure is read from what is already stored (messages, conversations, conversation_events, consent_ledger, campaigns), nothing is
kept twice. Dates are UTC days.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Optional

from prism_wa import campaigns, plans, usage
from prism_wa.usage import UsageError

KINDS = ("overview", "usage", "campaigns", "agents", "optins")


def _span(since, until, now=None) -> tuple:
    return usage._range(since, until, now)


def overview(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    since, until = _span(since, until, now)
    cur.execute(
        "select (created_at at time zone 'UTC')::date,"
        "  count(*) filter (where direction = 'out'),"
        "  count(*) filter (where direction = 'out' and status in ('delivered', 'read')),"
        "  count(*) filter (where direction = 'out' and status = 'read'),"
        "  count(*) filter (where direction = 'out' and status = 'failed'),"
        "  count(*) filter (where direction = 'in')"
        " from messages where tenant_id = %s and created_at >= %s and created_at < %s group by 1 order by 1", (tenant_id, since, until))
    days = {str(r[0]): {"day": str(r[0]), "sent": r[1], "delivered": r[2], "read": r[3], "failed": r[4], "received": r[5]} for r in cur.fetchall()}
    cur.execute("select (created_at at time zone 'UTC')::date, count(*) from conversations where tenant_id = %s and created_at >= %s and created_at < %s group by 1",
                (tenant_id, since, until))
    for day, n in cur.fetchall():
        days.setdefault(str(day), {"day": str(day), "sent": 0, "delivered": 0, "read": 0, "failed": 0, "received": 0})["new_chats"] = n
    cur.execute("select (created_at at time zone 'UTC')::date, count(*) from contacts where tenant_id = %s and created_at >= %s and created_at < %s group by 1",
                (tenant_id, since, until))
    for day, n in cur.fetchall():
        days.setdefault(str(day), {"day": str(day), "sent": 0, "delivered": 0, "read": 0, "failed": 0, "received": 0})["new_contacts"] = n
    rows = []
    for d in sorted(days):
        r = days[d]
        r.setdefault("new_chats", 0)
        r.setdefault("new_contacts", 0)
        rows.append(r)
    t = {k: sum(r[k] for r in rows) for k in ("sent", "delivered", "read", "failed", "received", "new_chats", "new_contacts")}
    cur.execute("select count(*) from conversation_events where tenant_id = %s and kind = 'requested' and at >= %s and at < %s", (tenant_id, since, until))
    t["handoffs"] = cur.fetchone()[0]
    pct = lambda n: round(100.0 * n / t["sent"], 1) if t["sent"] else 0.0                            # noqa: E731
    t.update(delivered_rate=pct(t["delivered"]), read_rate=pct(t["read"]), failed_rate=pct(t["failed"]))
    cur.execute("select coalesce(sender, 'unknown'), count(*) from messages where tenant_id = %s and direction = 'out' and created_at >= %s and created_at < %s"
                " group by 1 order by 2 desc", (tenant_id, since, until))
    return {"range": {"since": since.isoformat(), "until": until.isoformat()}, "days": rows, "totals": t,
            "by_sender": [{"sender": s, "messages": n} for s, n in cur.fetchall()]}


def agents(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    since, until = _span(since, until, now)
    cur.execute(
        "select a.id, a.name, a.role,"
        "  (select count(*) from messages m where m.tenant_id = a.tenant_id and m.sent_by = a.id and m.created_at >= %(s)s and m.created_at < %(u)s),"
        "  (select count(distinct m.conversation_id) from messages m where m.tenant_id = a.tenant_id and m.sent_by = a.id and m.created_at >= %(s)s and m.created_at < %(u)s),"
        "  (select count(*) from conversation_events e where e.tenant_id = a.tenant_id and e.actor_id = a.id and e.kind = 'resolved' and e.at >= %(s)s and e.at < %(u)s),"
        "  (select count(*) from conversations c where c.tenant_id = a.tenant_id and c.assignee_id = a.id and c.status <> 'closed'),"
        "  (select avg(extract(epoch from r.created_at - i.created_at))"
        "     from messages i cross join lateral (select created_at from messages r where r.conversation_id = i.conversation_id and r.sent_by = a.id"
        "       and r.direction = 'out' and r.created_at > i.created_at and r.created_at < i.created_at + interval '24 hours'"
        "       order by r.created_at limit 1) r"
        "    where i.tenant_id = a.tenant_id and i.direction = 'in' and i.created_at >= %(s)s and i.created_at < %(u)s"
        "      and exists (select 1 from conversations c where c.id = i.conversation_id and c.assignee_id = a.id))"
        " from agents a where a.tenant_id = %(t)s and a.active order by 4 desc, a.name", {"t": tenant_id, "s": since, "u": until})
    rows = [{"agent_id": str(r[0]), "name": r[1], "role": r[2], "replies": r[3], "chats": r[4], "resolved": r[5], "open_now": r[6],
             "avg_reply_seconds": round(float(r[7]), 1) if r[7] is not None else None} for r in cur.fetchall()]
    return {"range": {"since": since.isoformat(), "until": until.isoformat()}, "agents": rows}


def optins(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    since, until = _span(since, until, now)
    cur.execute("select (at at time zone 'UTC')::date, action, count(*) from consent_ledger where tenant_id = %s and at >= %s and at < %s group by 1, 2 order by 1",
                (tenant_id, since, until))
    days = {}
    for day, action, n in cur.fetchall():
        days.setdefault(str(day), {"day": str(day), "opt_in": 0, "opt_out": 0})[action] = n
    cur.execute("select action, source, count(*) from consent_ledger where tenant_id = %s and at >= %s and at < %s group by 1, 2 order by 3 desc", (tenant_id, since, until))
    sources = [{"action": a, "source": s, "count": n} for a, s, n in cur.fetchall()]
    cur.execute("select count(*) filter (where opted_in and not opt_out), count(*) filter (where opt_out), count(*) from contacts where tenant_id = %s", (tenant_id,))
    now_in, now_out, total = cur.fetchone()
    return {"range": {"since": since.isoformat(), "until": until.isoformat()}, "days": list(days.values()), "sources": sources,
            "totals": {"opt_in": sum(d["opt_in"] for d in days.values()), "opt_out": sum(d["opt_out"] for d in days.values())},
            "now": {"opted_in": now_in, "opted_out": now_out, "contacts": total}}


def campaign_rows(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None, limit: int = 100) -> list:
    since, until = _span(since, until, now)
    cur.execute("select id from campaigns where tenant_id = %s and created_at >= %s and created_at < %s order by created_at desc limit %s",
                (tenant_id, since, until, limit))
    out = []
    for (cid,) in cur.fetchall():
        r = campaigns.report(cur, tenant_id, cid)
        out.append({"id": str(cid), **r["campaign"], **r["counts"], "rates": r["rates"], "spend": r["spend_estimate"]["inr"]})
    return out


def dashboard(cur, tenant_id, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    since, until = _span(since, until, now)
    o = overview(cur, tenant_id, since, until, now=now)
    u = usage.summary(cur, tenant_id, since, until, now=now)
    a = agents(cur, tenant_id, since, until, now=now)
    c = optins(cur, tenant_id, since, until, now=now)
    cur.execute("select count(*) from conversations where tenant_id = %s and status in ('requesting', 'intervened', 'human_takeover', 'new', 'active')", (tenant_id,))
    open_chats = cur.fetchone()[0]
    camps = campaign_rows(cur, tenant_id, since, until, now=now, limit=8)
    return {"range": o["range"], "overview": o, "usage": {"totals": u["totals"], "by_category": u["by_category"], "days": u["days"], "is_estimate": True},
            "agents": a["agents"], "optins": {"totals": c["totals"], "now": c["now"], "days": c["days"]}, "campaigns": camps, "open_chats": open_chats,
            "can_download": plans.has(plans.plan_of(cur, tenant_id), "reports_download")}


# ── CSV ──────────────────────────────────────────────────────────────────────

def _csv(header: list, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def download(cur, tenant_id, kind: str, since=None, until=None, *, now: Optional[dt.datetime] = None) -> dict:
    """A report as a CSV file (opens in Excel). Part of the Premium plan."""
    if kind not in KINDS:
        raise UsageError("bad_report", "Choose one of: %s." % ", ".join(KINDS))
    plans.require(cur, tenant_id, "reports_download", "Downloading reports")
    since, until = _span(since, until, now)
    stamp = "%s_to_%s" % (since.date(), (until - dt.timedelta(seconds=1)).date())
    if kind == "overview":
        o = overview(cur, tenant_id, since, until, now=now)
        text = _csv(["Day", "Sent", "Delivered", "Read", "Failed", "Received", "New chats", "New contacts"],
                    [[d["day"], d["sent"], d["delivered"], d["read"], d["failed"], d["received"], d["new_chats"], d["new_contacts"]] for d in o["days"]])
    elif kind == "usage":
        u = usage.summary(cur, tenant_id, since, until, now=now)
        cats = [c["category"] for c in u["by_category"]]
        text = _csv(["Day", "Messages", "Billable"] + ["%s (Rs, estimate)" % c.title() for c in cats] + ["Total (Rs, estimate)"],
                    [[d["day"], d["messages"], d["billable"]] + [d["by_category"].get(c, {}).get("cost", 0) for c in cats] + [d["cost"]] for d in u["days"]])
    elif kind == "campaigns":
        rows = campaign_rows(cur, tenant_id, since, until, now=now, limit=500)
        text = _csv(["Name", "Status", "Template", "Recipients", "Sent", "Delivered", "Read", "Replied", "Clicked", "Failed", "Spend (Rs, estimate)"],
                    [[r["name"], r["status"], r["template"], r["recipients"], r["sent"], r["delivered"], r["read"], r["replied"], r["clicked"], r["failed"], r["spend"]]
                     for r in rows])
    elif kind == "agents":
        a = agents(cur, tenant_id, since, until, now=now)
        text = _csv(["Name", "Role", "Replies", "Chats", "Resolved", "Open now", "Average reply (seconds)"],
                    [[r["name"], r["role"], r["replies"], r["chats"], r["resolved"], r["open_now"], r["avg_reply_seconds"] if r["avg_reply_seconds"] is not None else ""]
                     for r in a["agents"]])
    else:
        c = optins(cur, tenant_id, since, until, now=now)
        text = _csv(["Day", "Opted in", "Opted out"], [[d["day"], d["opt_in"], d["opt_out"]] for d in c["days"]])
    return {"filename": "prism-whatsapp-%s-%s.csv" % (kind, stamp), "csv": text, "rows": text.count("\n") - 1}
