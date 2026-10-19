"""A campaign in detail: who got what and why it failed, the settings, and what happens when Meta re-labels a template.

    recipients      every person a campaign was meant for, with how far their message got (sent / delivered / read / replied /
                    clicked / failed / skipped / waiting) and, if it failed, why in plain words
    recipients_csv  the same as a file
    failures        the reasons, counted, each with what to do about it
    settings        workspace-wide campaign settings (auto-pause when a template changes category)
    on_category_change   Meta turned a Utility template into Marketing (or back): running campaigns stop before they bill at the
                    higher price without anyone knowing, and start again if it goes back
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Optional

from prism_wa import campaigns, segments

STATUSES = ("sent", "delivered", "read", "replied", "clicked", "failed", "skipped", "pending")
MAX_PAGE = 500
PAUSE_REASON = "category_change"

# Meta error code -> (what happened, what to do). Plain words: this is shown to someone who runs a shop, not an engineer.
EXPLAIN = {
    131026: ("This number is not on WhatsApp, or cannot get messages from a business.", "Take it off your list."),
    131049: ("Meta chose not to deliver this marketing message, to keep the person's chats from filling with promotions "
             "(its frequency cap). Your account is not penalised.", "Try again in a day or two, and send fewer marketing messages to the same people."),
    131050: ("They opted out of marketing messages from you.", "Leave them out of future campaigns."),
    131056: ("Too many messages went to this person too quickly.", "It is retried on its own; nothing to do."),
    130429: ("Messages were sent faster than your number allows.", "It is retried on its own; nothing to do."),
    131000: ("WhatsApp had a temporary problem.", "It is retried on its own; nothing to do."),
    131016: ("WhatsApp is busy right now.", "It is retried on its own; nothing to do."),
    131057: ("WhatsApp is under maintenance.", "It is retried on its own; nothing to do."),
    131047: ("The 24-hour reply window had closed.", "Send an approved template instead."),
    131031: ("Your WhatsApp business account is locked.", "Open Meta Business Suite to see why."),
    131042: ("Your WhatsApp account has a payment problem.", "Add or fix the payment method on your WhatsApp account in Meta."),
    132000: ("The number of values did not match the template.", "Check the blanks you filled."),
    132001: ("That template does not exist in this language.", "Check the template and its language."),
    132007: ("The template broke WhatsApp's content policy.", "Rewrite it and submit it again."),
    132012: ("A value was in the wrong form for the template.", "Check the blanks you filled (numbers, dates, links)."),
    132015: ("The template is paused because people reported it.", "Wait for it to be active again, or write a better one."),
    132016: ("The template was disabled by Meta.", "Write a new template."),
}


class BookError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def explain(code, message: str = "") -> dict:
    try:
        key = int(code)
    except (TypeError, ValueError):
        key = None
    if key in EXPLAIN:
        what, todo = EXPLAIN[key]
        return {"code": key, "reason": what, "advice": todo}
    if key in (100, 190, 368, 80007):
        return {"code": key, "reason": "Meta refused the request: the connection to your WhatsApp account needs attention.",
                "advice": "Reconnect your number in Settings."}
    return {"code": key, "reason": ("Meta said: %s" % message) if message else "The message did not go through.", "advice": ""}


_STATUS_SQL = ("case when r.status = 'skipped' then 'skipped'"
               " when o.state = 'failed' or m.status = 'failed' then 'failed'"
               " when m.status = 'read' then 'read'"
               " when m.status in ('delivered') then 'delivered'"
               " when o.state = 'sent' then 'sent'"
               " else 'pending' end")
_REPLIED_SQL = ("exists (select 1 from messages q where q.conversation_id = m.conversation_id and q.direction = 'in'"
                " and q.created_at > o.updated_at and q.created_at <= o.updated_at + interval '%d hours')" % segments.REPLY_WINDOW_HOURS)
_CLICKED_SQL = "exists (select 1 from click_events k where k.recipient_id = r.id)"


def _where(status: Optional[str]) -> str:
    if not status:
        return "true"
    if status not in STATUSES:
        raise BookError("bad_status", "status must be one of: %s" % ", ".join(STATUSES))
    return {"sent": "(o.state = 'sent')", "delivered": "(m.status in ('delivered', 'read'))", "read": "(m.status = 'read')",
            "replied": "(o.state = 'sent' and %s)" % _REPLIED_SQL, "clicked": "(%s)" % _CLICKED_SQL,
            "failed": "(o.state = 'failed' or m.status = 'failed')", "skipped": "(r.status = 'skipped')",
            "pending": "(r.status in ('pending', 'queued') and coalesce(o.state, 'queued') in ('queued', 'sending'))"}[status]


def _check(cur, tenant_id, campaign_id) -> None:
    campaigns._campaign(cur, tenant_id, campaign_id)                      # noqa: SLF001 (raises unknown_campaign)


def recipients(cur, tenant_id, campaign_id, *, status: Optional[str] = None, search: str = "", limit: int = 100, offset: int = 0) -> dict:
    _check(cur, tenant_id, campaign_id)
    where = ["r.campaign_id = %s", "r.tenant_id = %s", _where(status)]
    params: list = [campaign_id, tenant_id]
    text = (search or "").strip()
    if text:
        digits = re.sub(r"\D", "", text)
        clause = "(c.name ilike %s"
        params.append("%" + text + "%")
        if len(digits) >= 3:
            clause += " or c.phone like %s"
            params.append("%" + digits + "%")
        where.append(clause + ")")
    base = (" from campaign_recipients r join contacts c on c.id = r.contact_id left join outbox o on o.id = r.outbox_id"
            " left join messages m on m.id = o.message_id where " + " and ".join(where))
    cur.execute("select count(*)" + base, params)
    total = cur.fetchone()[0]
    limit = max(1, min(int(limit or 100), MAX_PAGE))
    cur.execute("select r.id, r.contact_id, c.name, c.phone, " + _STATUS_SQL + ", r.skip_reason, o.error, o.error_class, o.updated_at,"
                " (o.state = 'sent' and " + _REPLIED_SQL + "), " + _CLICKED_SQL + base + " order by c.name, r.id limit %s offset %s",
                params + [limit, max(int(offset or 0), 0)])
    out = []
    for rid, contact_id, name, phone, st, skip, error, error_class, at, replied, clicked in cur.fetchall():
        row = {"id": rid, "contact_id": contact_id, "name": name, "phone": phone, "status": st, "replied": bool(replied), "clicked": bool(clicked),
               "at": at, "reason": None, "advice": None, "code": None}
        if st == "failed":
            err = error if isinstance(error, dict) else {}
            row.update(explain(err.get("code"), str(err.get("message") or "")))
        elif st == "skipped":
            row["reason"] = {"opted_out": "They have opted out.", "no_number": "There is no number to send to.", "cancelled": "The campaign was cancelled.",
                             "window": "The reply window was closed.", "blocked": "They are blocked."}.get(skip or "", "Left out: %s." % (skip or "unknown reason"))
        out.append(row)
    return {"recipients": out, "total": total}


def recipients_csv(cur, tenant_id, campaign_id, *, status: Optional[str] = None) -> str:
    rows = []
    offset = 0
    while True:
        page = recipients(cur, tenant_id, campaign_id, status=status, limit=MAX_PAGE, offset=offset)["recipients"]
        rows += page
        if len(page) < MAX_PAGE:
            break
        offset += MAX_PAGE
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Name", "Mobile", "Status", "Replied", "Clicked", "Why it did not go", "What to do", "When"])

    def safe(v):
        t = "" if v is None else str(v)
        return "'" + t if t[:1] in ("=", "+", "-", "@", "\t", "\r") else t
    for r in rows:
        w.writerow([safe(r["name"]), safe(r["phone"]), r["status"], "yes" if r["replied"] else "no", "yes" if r["clicked"] else "no",
                    safe(r["reason"]), safe(r["advice"]), r["at"].isoformat() if r["at"] else ""])
    return out.getvalue()


def failures(cur, tenant_id, campaign_id) -> list:
    """[{code, reason, advice, count}], biggest first."""
    _check(cur, tenant_id, campaign_id)
    cur.execute("select o.error->>'code', o.error->>'message', count(*) from outbox o where o.campaign_id = %s and (o.state = 'failed' or o.error_class is not null)"
                " and o.state = 'failed' group by 1, 2 order by 3 desc", (campaign_id,))
    merged: dict = {}
    for code, message, n in cur.fetchall():
        e = explain(code, message or "")
        key = (e["code"], e["reason"])
        merged[key] = {**e, "count": merged.get(key, {}).get("count", 0) + n}
    return sorted(merged.values(), key=lambda x: -x["count"])


# ── settings ─────────────────────────────────────────────────────────────────

DEFAULTS = {"auto_pause_on_category_change": True}


def settings(cur, tenant_id) -> dict:
    cur.execute("select settings->'campaigns' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    stored = (row[0] if row and row[0] else {}) or {}
    return {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if isinstance(stored.get(k), bool)}}


def save_settings(cur, tenant_id, patch: dict) -> dict:
    new = {**settings(cur, tenant_id), **{k: bool(v) for k, v in (patch or {}).items() if k in DEFAULTS}}
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, jsonb_build_object('campaigns', %s::jsonb))"
                " on conflict (tenant_id) do update set settings = jsonb_set(tenant_settings.settings, '{campaigns}', %s::jsonb, true), updated_at = now()",
                (tenant_id, json.dumps(new), json.dumps(new)))
    return new


# ── when Meta re-labels a template ───────────────────────────────────────────

def on_category_change(cur, tenant_id, template_name: str, language: str, old: str, new: str) -> dict:
    """Utility -> Marketing: running campaigns using the template are paused and scheduled ones cancelled, so nothing is
    billed at the higher price unnoticed. Marketing -> Utility: campaigns THIS paused for that reason start again."""
    out = {"paused": [], "cancelled": [], "resumed": []}
    if not settings(cur, tenant_id)["auto_pause_on_category_change"]:
        return out
    if old == "UTILITY" and new == "MARKETING":
        cur.execute("select id, status from campaigns where tenant_id = %s and template_name = %s and language = %s and kind = 'broadcast'"
                    " and status in ('sending', 'scheduled')", (tenant_id, template_name, language))
        for cid, status in cur.fetchall():
            if status == "sending":
                campaigns.pause(cur, tenant_id, cid)
                cur.execute("update campaigns set pause_reason = %s where id = %s", (PAUSE_REASON, cid))
                out["paused"].append(cid)
            else:
                campaigns.cancel(cur, tenant_id, cid)
                cur.execute("update campaigns set pause_reason = %s where id = %s", (PAUSE_REASON, cid))
                out["cancelled"].append(cid)
    elif old == "MARKETING" and new == "UTILITY":
        cur.execute("select id from campaigns where tenant_id = %s and template_name = %s and language = %s and status = 'paused' and pause_reason = %s",
                    (tenant_id, template_name, language, PAUSE_REASON))
        for (cid,) in cur.fetchall():
            campaigns.resume(cur, tenant_id, cid)
            cur.execute("update campaigns set pause_reason = null where id = %s", (cid,))
            out["resumed"].append(cid)
    return out
