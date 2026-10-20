"""Routes for the Analytics tab, Settings → Plan and usage / Business profile / Webhooks, and the Inbox's live updates.

    usage      summary  plan  rates  rates_save
    reports    dashboard  overview  agents  optins  campaigns  download
    numbers    limits  refresh
    profile    get  save
    webhooks   list  events  create  update  delete  test  rotate_secret  deliveries  replay
    inbox      changes            (long poll: answers the moment something in the Inbox changes, or after `wait` seconds)
"""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from prism_wa import media, number_info, reports, usage, webhooks_out
from prism_wa.graph import Graph
from prism_wa.ops_common import jsonable, managers_only, need, refuse
from prism_wa.vault import Vault

CHANGES_MAX_WAIT = 25
CHANGES_TICK = 1.0


def _range(d: dict) -> tuple:
    return d.get("since") or None, d.get("until") or None


# ── usage and reports ────────────────────────────────────────────────────────

def usage_summary(cur, t, d):
    return jsonable(usage.summary(cur, t, *_range(d)))


def usage_plan(cur, t, d):
    return jsonable(usage.plan_view(cur, t))


def usage_rates(cur, t, d):
    return {"rates": usage.rates(cur, t), "defaults": usage.DEFAULT_RATES, "gst": usage.GST}


def usage_rates_save(cur, t, d):
    need(d, "rates")
    managers_only(cur, t, d, "change the rate card")
    return {"rates": usage.set_rates(cur, t, d["rates"]), "defaults": usage.DEFAULT_RATES, "gst": usage.GST}


def reports_dashboard(cur, t, d):
    return jsonable(reports.dashboard(cur, t, *_range(d)))


def reports_overview(cur, t, d):
    return jsonable(reports.overview(cur, t, *_range(d)))


def reports_agents(cur, t, d):
    return jsonable(reports.agents(cur, t, *_range(d)))


def reports_optins(cur, t, d):
    return jsonable(reports.optins(cur, t, *_range(d)))


def reports_campaigns(cur, t, d):
    return {"campaigns": jsonable(reports.campaign_rows(cur, t, *_range(d)))}


def reports_download(cur, t, d):
    need(d, "kind")
    return reports.download(cur, t, d["kind"], *_range(d))


# ── numbers and the business profile ─────────────────────────────────────────

def numbers_limits(cur, t, d):
    return {"numbers": jsonable(number_info.limits_view(cur, t)), "tiers": number_info.TIER_LIMITS}


def make_conn_ops(graph: Graph, vault: Vault, app_id: str) -> dict:
    def need_vault():
        if vault is None:
            raise LookupError("this is not set up on this server (no token key)")

    def n_refresh(conn, t, d):
        need_vault()
        problems = number_info.refresh(conn, graph=graph, vault=vault, tenant_id=t)
        with conn.cursor() as cur:
            numbers = jsonable(number_info.limits_view(cur, t))
        return {"numbers": numbers, "problems": problems, "tiers": number_info.TIER_LIMITS}

    def p_get(conn, t, d):
        need_vault()
        return jsonable(number_info.profile_get(conn, graph=graph, vault=vault, tenant_id=t, number_id=d.get("number_id") or None))

    def p_save(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            managers_only(cur, t, d, "change the business profile")
        return jsonable(number_info.profile_save(conn, graph=graph, vault=vault, tenant_id=t, data=d.get("profile") or {}, app_id=app_id,
                                                 number_id=d.get("number_id") or None))
    return {"/internal/numbers/refresh": n_refresh, "/internal/profile/get": p_get, "/internal/profile/save": p_save}


# ── webhooks ─────────────────────────────────────────────────────────────────

def _no_secret(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "secret"}


def webhooks_list(cur, t, d):
    return {"endpoints": jsonable([_no_secret(e) for e in webhooks_out.list_endpoints(cur, t)]), "events": webhooks_out.EVENTS}


def webhooks_events(cur, t, d):
    return {"events": webhooks_out.EVENTS}


def webhooks_create(cur, t, d):
    need(d, "url", "events")
    managers_only(cur, t, d, "add a webhook")
    row = webhooks_out.create(cur, t, url=d["url"], events=d["events"], description=d.get("description") or "", agent_id=d.get("by_agent_id") or None)
    return {"endpoint": jsonable({k: v for k, v in row.items() if k != "secret"}), "secret": row["secret"]}


def webhooks_update(cur, t, d):
    need(d, "endpoint_id", "changes")
    managers_only(cur, t, d, "change a webhook")
    return {"endpoint": jsonable(_no_secret(webhooks_out.update(cur, t, d["endpoint_id"], d["changes"])))}


def webhooks_delete(cur, t, d):
    need(d, "endpoint_id")
    managers_only(cur, t, d, "remove a webhook")
    return {"deleted": webhooks_out.delete(cur, t, d["endpoint_id"])}


def webhooks_test(cur, t, d):
    need(d, "endpoint_id")
    return {"delivery_id": webhooks_out.send_test(cur, t, d["endpoint_id"])}


def webhooks_rotate(cur, t, d):
    need(d, "endpoint_id")
    managers_only(cur, t, d, "change a webhook's secret")
    return {"secret": webhooks_out.rotate_secret(cur, t, d["endpoint_id"])}


def webhooks_deliveries(cur, t, d):
    return {"deliveries": jsonable(webhooks_out.deliveries(cur, t, d.get("endpoint_id") or None, int(d.get("limit") or 50)))}


def webhooks_replay(cur, t, d):
    need(d, "delivery_id")
    return {"delivery_id": webhooks_out.replay(cur, t, d["delivery_id"])}


# ── the Inbox, live ──────────────────────────────────────────────────────────

def inbox_latest(cur, tenant_id) -> str:
    """A marker that changes whenever a chat gets a message or changes hands: the newest of the chats' last activity and the team events."""
    cur.execute("select greatest((select max(last_active_at) from conversations where tenant_id = %s),"
                "                (select max(at) from conversation_events where tenant_id = %s))::text", (tenant_id, tenant_id))
    return cur.fetchone()[0] or "0"                      # a workspace with no chats yet still has a marker, so its first chat counts as a change


def mount_realtime(app: FastAPI, *, authorised, connect) -> None:
    """POST /internal/inbox/changes {tenant_id, cursor, wait}: the long poll. It does not hold a worker thread while it waits."""
    def look(tenant_id: str) -> str:
        conn = connect()
        try:
            with conn.cursor() as cur:
                return inbox_latest(cur, tenant_id)
        finally:
            conn.close()

    @app.post("/internal/inbox/changes")
    async def changes(request: Request):
        if not authorised(request):
            return await refuse(request)
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("tenant_id"):
            return JSONResponse({"ok": False, "error": "bad_request", "message": "tenant_id is required"}, status_code=400)
        try:
            wait = max(0.0, min(float(data.get("wait") or 0), CHANGES_MAX_WAIT))
        except (TypeError, ValueError):
            wait = 0.0
        cursor = str(data.get("cursor") or "")
        deadline = time.monotonic() + wait
        while True:
            now = await run_in_threadpool(look, str(data["tenant_id"]))
            if (cursor and now != cursor) or not cursor or time.monotonic() >= deadline:
                return {"ok": True, "cursor": now, "changed": bool(cursor) and now != cursor}
            await asyncio.sleep(CHANGES_TICK)


OPS = {"/internal/usage/summary": usage_summary, "/internal/usage/plan": usage_plan, "/internal/usage/rates": usage_rates,
       "/internal/usage/rates_save": usage_rates_save,
       "/internal/reports/dashboard": reports_dashboard, "/internal/reports/overview": reports_overview, "/internal/reports/agents": reports_agents,
       "/internal/reports/optins": reports_optins, "/internal/reports/campaigns": reports_campaigns, "/internal/reports/download": reports_download,
       "/internal/numbers/limits": numbers_limits,
       "/internal/webhooks/list": webhooks_list, "/internal/webhooks/events": webhooks_events, "/internal/webhooks/create": webhooks_create,
       "/internal/webhooks/update": webhooks_update, "/internal/webhooks/delete": webhooks_delete, "/internal/webhooks/test": webhooks_test,
       "/internal/webhooks/rotate_secret": webhooks_rotate, "/internal/webhooks/deliveries": webhooks_deliveries,
       "/internal/webhooks/replay": webhooks_replay}
