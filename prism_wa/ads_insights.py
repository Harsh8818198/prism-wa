"""How an ad is doing and whether Meta has let it run — and what to do about it.

    refresh_status()   Meta's own state for an ad (in review, running, turned down — and the reason), read and kept
    over_time()        the numbers day by day for a date range, with click-through rate and cost per click worked out
    suggest()          a short list of "pause this" / "put more into that" from the last seven days' numbers
    decide()           applying a suggestion, only when the owner says yes — pausing an ad, or moving a daily budget up or down by a fifth

Meta decides how long a review takes; an ad that has been turned on can show "in review" for hours. The reason an ad was turned down is Meta's own wording.
Suggestions are rules over numbers (an ad that spent but started no chats, one that costs far more than the others, one that few people click), not guesses by a model;
each says why, and nothing happens until it is approved.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from psycopg2.extras import Json

from prism_wa import ads
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow

STATUS_WORDS = {
    "ACTIVE": ("running", "Running"), "PAUSED": ("paused", "Paused"), "PENDING_REVIEW": ("review", "In review at Meta"), "IN_PROCESS": ("review", "Meta is processing it"),
    "DISAPPROVED": ("rejected", "Turned down by Meta"), "WITH_ISSUES": ("issues", "Running with problems"), "CAMPAIGN_PAUSED": ("paused", "Its campaign is paused"),
    "ADSET_PAUSED": ("paused", "Its audience is paused"), "ARCHIVED": ("archived", "Archived"), "DELETED": ("archived", "Removed"), "PENDING_BILLING_INFO": ("issues", "Meta needs payment details"),
}
MIN_BUDGET_STEP = 0.2


def _meta_error(exc: GraphError, what: str):
    return ads.AdsError("meta_refused", "Meta did not %s: %s" % (what, exc.details or exc.message), exc.as_dict())


def _ad(cur, tenant_id, ad_id, *, lock: bool = False) -> dict:
    cur.execute("select id, account_id, name, meta_ids, status, spec, kind from ads where id = %s and tenant_id = %s" + (" for update" if lock else ""), (ad_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ads.AdsError("unknown_ad", "that ad is not in this workspace")
    return dict(zip(("id", "account_id", "name", "meta_ids", "status", "spec", "kind"), row))


def refresh_status(cur, tenant_id, graph: Graph, vault, ad_id) -> dict:
    """Ask Meta where the ad stands. -> {"meta_status", "state", "words", "reasons": [..]}."""
    ad = _ad(cur, tenant_id, ad_id, lock=True)
    if not ad["meta_ids"].get("ad"):
        return {"meta_status": None, "state": "failed", "words": "Never made at Meta", "reasons": []}
    acct = ads._account(cur, tenant_id, vault, ad["account_id"])
    try:
        got = graph._call("GET", ad["meta_ids"]["ad"], token=acct["token"], query={"fields": "effective_status,configured_status,ad_review_feedback,issues_info"})
    except GraphError as exc:
        raise _meta_error(exc, "say where the ad stands") from exc
    status = str(got.get("effective_status") or got.get("configured_status") or "")
    reasons = []
    feedback = got.get("ad_review_feedback") or {}
    for group in ("global", "placement_specific"):
        part = feedback.get(group) or {}
        if isinstance(part, dict):
            for k, v in part.items():
                if isinstance(v, dict):
                    reasons.extend("%s: %s" % (kk, vv) if isinstance(vv, str) else str(kk) for kk, vv in v.items())
                else:
                    reasons.append("%s: %s" % (k, v))
    for issue in got.get("issues_info") or []:
        text = issue.get("error_summary") or issue.get("error_message")
        if text:
            reasons.append(str(text))
    reasons = list(dict.fromkeys(r for r in reasons if r))[:6]
    mapped = STATUS_WORDS.get(status, ("unknown", status.replace("_", " ").capitalize() or "Unknown"))
    new_status = "PAUSED" if mapped[0] == "paused" else "ACTIVE" if mapped[0] in ("running", "issues") else ad["status"]
    cur.execute("update ads set meta_status = %s, review = %s, synced_at = now(), status = case when status in ('PAUSED', 'ACTIVE') then %s else status end where id = %s",
                (status or None, Json({"reasons": reasons}) if reasons else None, new_status, ad_id))
    return {"meta_status": status, "state": mapped[0], "words": mapped[1], "reasons": reasons}


# ── numbers ──────────────────────────────────────────────────────────────────

def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _actions(row: dict, wanted: str) -> int:
    return sum(int(_num(a.get("value"))) for a in row.get("actions") or [] if wanted in str(a.get("action_type")))


def _row(row: dict) -> dict:
    spend, impressions, clicks = _num(row.get("spend")), int(_num(row.get("impressions"))), int(_num(row.get("clicks")))
    outbound = sum(int(_num(a.get("value"))) for a in row.get("outbound_clicks") or [])
    chats = _actions(row, "messaging_conversation_started")
    leads = _actions(row, "lead")
    return {"impressions": impressions, "reach": int(_num(row.get("reach"))), "clicks": clicks, "outbound_clicks": outbound, "spend": round(spend, 2),
            "conversations": chats, "leads": leads, "ctr": round(100.0 * clicks / impressions, 2) if impressions else 0.0, "cpc": round(spend / clicks, 2) if clicks else None,
            "cost_per_conversation": round(spend / chats, 2) if chats else None, "cost_per_lead": round(spend / leads, 2) if leads else None}


def over_time(cur, tenant_id, graph: Graph, vault, ad_id, since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Day-by-day numbers for a range (default the last 30 days) and their totals. -> {"days": [{"day", ...}], "totals": {...}}."""
    ad = _ad(cur, tenant_id, ad_id)
    today = utcnow().date()
    try:
        s = date.fromisoformat(since) if since else today - timedelta(days=29)
        u = date.fromisoformat(until) if until else today
    except ValueError:
        raise ads.AdsError("bad_range", "dates look like 2026-10-01") from None
    if s > u or (u - s).days > 365:
        raise ads.AdsError("bad_range", "choose a range of up to a year, oldest first")
    acct = ads._account(cur, tenant_id, vault, ad["account_id"])
    try:
        out = graph._call("GET", "%s/insights" % ad["meta_ids"]["ad"], token=acct["token"], query={
            "fields": "impressions,reach,clicks,outbound_clicks,ctr,cpc,spend,actions", "time_increment": 1, "time_range": json.dumps({"since": s.isoformat(), "until": u.isoformat()}), "limit": 400})
    except GraphError as exc:
        raise _meta_error(exc, "return the numbers") from exc
    days = []
    for r in out.get("data") or []:
        days.append({"day": r.get("date_start") or "", **_row(r)})
    days.sort(key=lambda d: d["day"])
    total = {"impressions": sum(d["impressions"] for d in days), "clicks": sum(d["clicks"] for d in days), "outbound_clicks": sum(d["outbound_clicks"] for d in days),
             "spend": round(sum(d["spend"] for d in days), 2), "conversations": sum(d["conversations"] for d in days), "leads": sum(d["leads"] for d in days),
             "reach": max([d["reach"] for d in days] + [0])}
    total["ctr"] = round(100.0 * total["clicks"] / total["impressions"], 2) if total["impressions"] else 0.0
    total["cpc"] = round(total["spend"] / total["clicks"], 2) if total["clicks"] else None
    total["cost_per_conversation"] = round(total["spend"] / total["conversations"], 2) if total["conversations"] else None
    total["cost_per_lead"] = round(total["spend"] / total["leads"], 2) if total["leads"] else None
    return {"range": {"since": s.isoformat(), "until": u.isoformat()}, "days": days, "totals": total}


# ── suggestions ──────────────────────────────────────────────────────────────

def suggest(cur, tenant_id, graph: Graph, vault, *, now: Optional[datetime] = None) -> list:
    """Look at the last seven days of every running ad and put new suggestions on the list (none twice while one is open). -> the open suggestions."""
    now = now or utcnow()
    cur.execute("select id from ads where tenant_id = %s and status = 'ACTIVE' and meta_ids ? 'ad'", (tenant_id,))
    running = [r[0] for r in cur.fetchall()]
    week_ago, today = (now - timedelta(days=6)).date().isoformat(), now.date().isoformat()
    stats = {}
    for ad_id in running:
        try:
            stats[ad_id] = over_time(cur, tenant_id, graph, vault, ad_id, week_ago, today)["totals"]
        except ads.AdsError:
            continue
    goals = {}
    cur.execute("select id, kind, spec from ads where id = any(%s::uuid[])", ([str(i) for i in stats],))
    for i, kind, spec in cur.fetchall():
        goals[i] = (kind, spec or {})
    costs = []
    for ad_id, t in stats.items():
        kind = goals[ad_id][0]
        c = t["cost_per_conversation"] if kind == "ctwa" else t["cost_per_lead"] if kind == "leadform" else t["cpc"]
        if c:
            costs.append(c)
    middle = statistics.median(costs) if len(costs) >= 3 else None
    made = []
    for ad_id, t in stats.items():
        kind, spec = goals[ad_id]
        result = t["conversations"] if kind == "ctwa" else t["leads"] if kind == "leadform" else t["clicks"]
        cost = t["cost_per_conversation"] if kind == "ctwa" else t["cost_per_lead"] if kind == "leadform" else t["cpc"]
        daily = spec.get("daily_budget")
        noun = {"ctwa": "chats", "leadform": "leads", "website": "clicks"}[kind]
        if t["spend"] >= max(300.0, 2 * (daily or 10000) / 100.0) and result == 0:
            made.append((ad_id, "pause", "It spent Rs %s in the last seven days and got no %s." % ("{:,.0f}".format(t["spend"]), noun), {}))
        elif middle and cost and cost > 2 * middle and t["spend"] >= 300.0:
            made.append((ad_id, "pause", "Each %s costs Rs %s — more than twice the middle of your other ads (Rs %s)." % (noun[:-1], "{:,.0f}".format(cost), "{:,.0f}".format(middle)), {}))
        elif t["impressions"] >= 2000 and t["ctr"] < 0.5:
            made.append((ad_id, "new_creative", "Only %s%% of the people who saw it clicked. A new picture or first line may do better." % t["ctr"], {}))
        elif middle and cost and cost < 0.5 * middle and daily and t["spend"] >= 0.7 * 7 * daily / 100.0:
            made.append((ad_id, "budget_up", "Each %s costs Rs %s, under half the middle of your other ads, and it is spending its whole budget." % (noun[:-1], "{:,.0f}".format(cost)),
                         {"from": daily, "to": int(daily * (1 + MIN_BUDGET_STEP))}))
    for ad_id, kind, reason, params in made:
        cur.execute("select 1 from ad_suggestions where tenant_id = %s and ad_id = %s and kind = %s and state = 'open'", (tenant_id, ad_id, kind))
        if cur.fetchone() is None:
            cur.execute("insert into ad_suggestions (tenant_id, ad_id, kind, reason, params) values (%s, %s, %s, %s, %s)", (tenant_id, ad_id, kind, reason, Json(params)))
    return list_suggestions(cur, tenant_id)


def list_suggestions(cur, tenant_id, state: str = "open") -> list:
    cur.execute("select s.id, s.ad_id, a.name, s.kind, s.reason, s.params, s.state, s.created_at from ad_suggestions s join ads a on a.id = s.ad_id"
                " where s.tenant_id = %s and s.state = %s order by s.created_at desc", (tenant_id, state if state in ("open", "applied", "dismissed") else "open"))
    return [dict(zip(("id", "ad_id", "ad_name", "kind", "reason", "params", "state", "created_at"), r)) for r in cur.fetchall()]


def decide(cur, tenant_id, graph: Graph, vault, suggestion_id, *, apply: bool) -> dict:
    cur.execute("select ad_id, kind, params, state from ad_suggestions where id = %s and tenant_id = %s for update", (suggestion_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ads.AdsError("unknown_suggestion", "that suggestion is not in this workspace")
    ad_id, kind, params, state = row
    if state != "open":
        raise ads.AdsError("wrong_state", "that suggestion was already dealt with")
    done = "dismissed"
    if apply:
        if kind == "pause":
            ads.pause(cur, tenant_id, graph, vault, ad_id)
        elif kind in ("budget_up", "budget_down"):
            ad = _ad(cur, tenant_id, ad_id)
            acct = ads._account(cur, tenant_id, vault, ad["account_id"])
            try:
                graph._call("POST", ad["meta_ids"]["adset"], token=acct["token"], body={"daily_budget": int(params["to"])})
            except GraphError as exc:
                raise _meta_error(exc, "change the budget") from exc
            spec = dict(ad["spec"] or {})
            spec["daily_budget"] = int(params["to"])
            cur.execute("update ads set spec = %s where id = %s", (Json(spec), ad_id))
        done = "applied"                                              # a new picture or first line is the owner's to make; saying yes only ticks it off
    cur.execute("update ad_suggestions set state = %s, decided_at = now() where id = %s", (done, suggestion_id))
    return {"state": done}
