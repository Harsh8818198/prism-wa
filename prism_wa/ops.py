"""The rest of the service-to-service API: templates, campaigns, segments, contacts, consent.

Every route is `POST /internal/<area>/<action>` with a JSON body that names the
`tenant_id` it acts for. The licence server calls these after checking the seat
and the licence (so this is not a public API: see internal.py for the shared
secret). Each handler is a plain function of (cursor or connection, tenant,
body) so it is tested without HTTP; `mount` adds the HTTP skin and one place
that turns the module errors into answers a UI can act on.

    templates   list  lint  create  sync  delete  library
    campaigns   create  preflight  launch  pause  resume  cancel  reschedule  report  list  test
    schedules   create  list  set_enabled  delete          (repeating campaigns)
    flows       list  get  create  update  delete  check  publish  versions  rollback  set_enabled  test  runs  library  from_library
    ai          status  agents/*  knowledge/*  test
    segments    validate  count
    contacts    import  export  erase
    consent     get  save
"""
from __future__ import annotations

import uuid
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from prism_wa import (ads, ai_agent, audit, coexistence, commerce, form_endpoint, forms, ops_commerce, ops_forms, ops_partner, partner, razorpay, sso, api_campaigns, autos, api_keys, campaign_book, campaigns, chat_send, consent, contact_book, contacts, flow_engine, flow_library, flows, inbox,
                      integrations, media, number_info, ops_book, ops_ads, ops_ai, ops_autos, ops_campaigns, ops_chat, ops_flows, ops_media, ops_templates, ops_usage, plans, schedules, segments, send, template_library, template_ai, templates, usage, webhooks_out)
from prism_wa.graph import Graph, GraphError
from prism_wa import capi
from prism_wa.ops_common import refuse
from prism_wa.llm import LLMError
from prism_wa.vault import Vault


def _uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _need(d: dict, *keys) -> None:
    missing = [k for k in keys if d.get(k) in (None, "")]
    if missing:
        raise ValueError("missing: %s" % ", ".join(missing))


def _rows(cur, cols: tuple) -> list:
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _only_waba(cur, tenant_id, requested=None):
    if requested:
        return requested
    cur.execute("select id from wabas where tenant_id = %s limit 2", (tenant_id,))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise ValueError("this workspace has %s WhatsApp accounts; say which one" % ("no" if not rows else "several"))
    return rows[0][0]


# ── templates ────────────────────────────────────────────────────────────────

def templates_list(cur, t, d):
    cur.execute("select id, name, language, category, status, quality, reject_reason, meta_id, components, last_error, ttl_seconds"
                " from templates where tenant_id = %s order by name, language", (t,))
    return {"templates": _rows(cur, ("id", "name", "language", "category", "status", "quality", "reject_reason",
                                     "meta_id", "components", "last_error", "ttl_seconds"))}


def templates_lint(cur, t, d):
    _need(d, "name", "language", "category")
    issues = templates.lint(name=d["name"], language=d["language"], category=d["category"],
                            components=d.get("components") or [], ttl_seconds=d.get("ttl_seconds"))
    return {"issues": [i.as_dict() for i in issues], "valid": not templates.errors(issues)}


def numbers_list(cur, t, d):
    """The workspace's connected numbers: a campaign has to say which one sends it when there are several."""
    cur.execute("select id, display, status, quality from phone_numbers where tenant_id = %s order by created_at", (t,))
    return {"numbers": _rows(cur, ("id", "display", "status", "quality"))}


def templates_library(cur, t, d):
    return {"library": template_library.LIBRARY}


# ── campaigns ────────────────────────────────────────────────────────────────

def campaigns_create(cur, t, d):
    _need(d, "name", "template_name", "audience")
    cid = campaigns.create_campaign(
        cur, t, name=d["name"], template_name=d["template_name"], language=d.get("language") or "en_US",
        audience=d["audience"], variables=d.get("variables"), number_id=d.get("number_id"),
        schedule_at=d.get("schedule_at"), timezone_name=d.get("timezone"), retry=d.get("retry"),
        track_clicks=bool(d.get("track_clicks", True)))
    return {"campaign_id": str(cid)}


def campaigns_preflight(cur, t, d):
    _need(d, "campaign_id")
    return {"preflight": campaigns.preflight(cur, t, d["campaign_id"]).as_dict()}


def campaigns_launch(cur, t, d):
    _need(d, "campaign_id")
    return campaigns.launch(cur, t, d["campaign_id"])


def campaigns_pause(cur, t, d):
    _need(d, "campaign_id")
    campaigns.pause(cur, t, d["campaign_id"])
    return {}


def campaigns_resume(cur, t, d):
    _need(d, "campaign_id")
    campaigns.resume(cur, t, d["campaign_id"])
    return {}


def campaigns_cancel(cur, t, d):
    _need(d, "campaign_id")
    return {"dropped": campaigns.cancel(cur, t, d["campaign_id"])}


def campaigns_reschedule(cur, t, d):
    _need(d, "campaign_id", "schedule_at")
    campaigns.reschedule(cur, t, d["campaign_id"], d["schedule_at"], d.get("timezone"))
    return {}


def campaigns_report(cur, t, d):
    _need(d, "campaign_id")
    out = campaigns.report(cur, t, d["campaign_id"])
    out["failures"] = campaign_book.failures(cur, t, d["campaign_id"])
    return {"report": out}


def campaigns_list(cur, t, d):
    cur.execute("select id, name, status, template_name, language, total, sent, failed, schedule_at, started_at,"
                " finished_at, created_at, kind, pause_reason from campaigns where tenant_id = %s order by created_at desc limit 100", (t,))
    return {"campaigns": _rows(cur, ("id", "name", "status", "template_name", "language", "total", "sent", "failed",
                                     "schedule_at", "started_at", "finished_at", "created_at", "kind", "pause_reason"))}


# ── segments ─────────────────────────────────────────────────────────────────

def segments_validate(cur, t, d):
    _need(d, "filter")
    problem = segments.validate(cur, t, d["filter"])
    return {"valid": problem is None, "message": problem}


def segments_count(cur, t, d):
    _need(d, "filter")
    ids = segments.resolve(cur, t, d["filter"], opted_in_only=bool(d.get("opted_in_only", True)))
    return {"count": len(ids)}


def segments_save(cur, t, d):
    _need(d, "name", "filter")
    return {"segment_id": str(segments.save(cur, t, d["name"], d["filter"], dynamic=bool(d.get("dynamic", True))))}


def segments_delete(cur, t, d):
    _need(d, "segment_id")
    return {"deleted": segments.delete(cur, t, d["segment_id"])}


def segments_list(cur, t, d):
    return {"segments": [_jsonable(x) for x in segments.list_segments(cur, t)]}


# ── consent ──────────────────────────────────────────────────────────────────

def consent_get(cur, t, d):
    return {"settings": consent.settings(cur, t)}


def consent_save(cur, t, d):
    return {"settings": consent.save_settings(cur, t, d.get("settings") or {})}


def contacts_erase(cur, t, d):
    _need(d, "contact_id")
    return {"erased": contacts.erase_contact(cur, t, d["contact_id"])}


# ── inbox ────────────────────────────────────────────────────────────────────

def _jsonable(value):
    """UUIDs and datetimes -> strings, so a dict can be returned as JSON."""
    import datetime as _dt
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value


def _agent_of(cur, t, d) -> dict:
    _need(d, "agent_id")
    return inbox._agent(cur, t, d["agent_id"])


def _managers_only(cur, t, d, what: str) -> None:
    if d.get("by_agent_id") and inbox._agent(cur, t, d["by_agent_id"])["role"] == "agent":
        raise inbox.InboxError("not_allowed", "only an owner or manager can %s" % what)


def inbox_ensure_agent(cur, t, d):
    _need(d, "seat_key", "name")
    return {"agent": _jsonable(inbox.ensure_agent(cur, t, d["seat_key"], d["name"]))}


def inbox_agents_list(cur, t, d):
    """The team, and which of them is asking (`agent_id` is the caller, put there by the licence server) so a screen
    can say "with you"."""
    return {"agents": [_jsonable(a) for a in inbox.list_agents(cur, t)], "me": str(d["agent_id"]) if d.get("agent_id") else None}


def inbox_agents_update(cur, t, d):
    _need(d, "agent_id", "changes")
    _managers_only(cur, t, d, "change who works the inbox")
    return {"agent": _jsonable(inbox.update_agent(cur, t, d["agent_id"], **d["changes"]))}


def inbox_conversations(cur, t, d):
    agent = _agent_of(cur, t, d)
    rows = inbox.visible_conversations(cur, t, agent, status=d.get("status"), limit=min(int(d.get("limit") or 100), 500))
    return {"conversations": [_jsonable(r) for r in rows]}


def inbox_request_human(cur, t, d):
    _need(d, "conversation_id")
    agent = inbox.request_human(cur, t, d["conversation_id"], d.get("reason") or "")
    return {"assigned_to": str(agent["id"]) if agent else None}


def inbox_intervene(cur, t, d):
    _need(d, "conversation_id", "agent_id")
    inbox.intervene(cur, t, d["conversation_id"], d["agent_id"])
    return {}


def inbox_transfer(cur, t, d):
    _need(d, "conversation_id", "to_agent_id", "agent_id")
    inbox.transfer(cur, t, d["conversation_id"], d["to_agent_id"], d["agent_id"])
    return {}


def inbox_hand_back(cur, t, d):
    _need(d, "conversation_id", "agent_id")
    inbox.hand_back(cur, t, d["conversation_id"], d["agent_id"])
    return {}


def inbox_resolve(cur, t, d):
    _need(d, "conversation_id")
    inbox.resolve(cur, t, d["conversation_id"], d.get("agent_id"))
    return {}


def inbox_note_add(cur, t, d):
    _need(d, "conversation_id", "agent_id", "text")
    inbox.add_note(cur, t, d["conversation_id"], d["agent_id"], d["text"])
    return {}


def inbox_notes_list(cur, t, d):
    _need(d, "conversation_id")
    return {"notes": [_jsonable(n) for n in inbox.list_notes(cur, t, d["conversation_id"])]}


def inbox_canned_save(cur, t, d):
    _need(d, "shortcut", "body")
    inbox.save_canned(cur, t, d["shortcut"], d["body"], d.get("agent_id"))
    return {}


def inbox_canned_delete(cur, t, d):
    _need(d, "shortcut")
    return {"deleted": inbox.delete_canned(cur, t, d["shortcut"])}


def inbox_canned_list(cur, t, d):
    return {"canned": inbox.list_canned(cur, t)}


def inbox_canned_expand(cur, t, d):
    _need(d, "shortcut", "contact_id")
    return {"text": inbox.expand(cur, t, d["shortcut"], d["contact_id"])}


def inbox_settings_get(cur, t, d):
    return {"settings": inbox.settings(cur, t)}


def inbox_settings_save(cur, t, d):
    _managers_only(cur, t, d, "change inbox settings")
    return {"settings": inbox.save_settings(cur, t, d.get("settings") or {})}


def keys_create(cur, t, d):
    _need(d, "name", "scopes")
    _managers_only(cur, t, d, "create API keys")
    return {"key": api_keys.create(cur, t, d["name"], d["scopes"], created_by=d.get("by_agent_id"),
                                   rate_limit=int(d.get("rate_limit_per_minute") or api_keys.DEFAULT_RATE))}


def keys_list(cur, t, d):
    return {"keys": [_jsonable(k) for k in api_keys.list_keys(cur, t)], "scopes": sorted(api_keys.SCOPES)}


def keys_revoke(cur, t, d):
    _need(d, "key_id")
    _managers_only(cur, t, d, "revoke API keys")
    return {"revoked": api_keys.revoke(cur, t, d["key_id"])}


def integrations_list(cur, t, d):
    return {"integrations": [_jsonable(i) for i in integrations.list_integrations(cur, t)]}


def integrations_delete(cur, t, d):
    _need(d, "integration_id")
    _managers_only(cur, t, d, "disconnect a store")
    return {"deleted": integrations.delete(cur, t, d["integration_id"])}


def inbox_monitor(cur, t, d):
    _managers_only(cur, t, d, "watch the team")
    return {"monitor": _jsonable(inbox.monitor(cur, t))}


# the ones that need the whole connection (they commit as they go) or the Graph API ───────

def make_conn_ops(graph: Graph, vault: Vault, app_id: str = "") -> dict:
    def t_create(conn, t, d):
        _need(d, "name", "category", "components")
        if vault is None:
            raise LookupError("template submission is not set up on this server (no token key)")
        ttl = d.get("ttl_seconds")
        with conn.cursor() as cur:
            waba = _only_waba(cur, t, d.get("waba_id"))
            if ttl not in (None, -1, 0):
                plans.require(cur, t, "template_ttl", "set how long a template stays valid")
        return templates.create_template(conn, graph=graph, vault=vault, tenant_id=t, waba_row_id=waba, name=d["name"],
                                         language=d.get("language") or "en_US", category=d["category"],
                                         components=d["components"], ttl_seconds=ttl)

    def t_sync(conn, t, d):
        if vault is None:
            raise LookupError("template sync is not set up on this server (no token key)")
        with conn.cursor() as cur:
            waba = _only_waba(cur, t, d.get("waba_id"))
        return {"synced": templates.sync_templates(conn, graph=graph, vault=vault, tenant_id=t, waba_row_id=waba)}

    def t_delete(conn, t, d):
        if vault is None:
            raise LookupError("templates are not set up on this server (no token key)")
        _need(d, "template_id")
        try:
            template_id = uuid.UUID(str(d["template_id"]))
        except ValueError:
            raise ValueError("that is not a template id") from None
        return templates.delete_template(conn, graph=graph, vault=vault, tenant_id=t, template_id=str(template_id))

    def c_import(conn, t, d):
        _need(d, "csv")
        report = contacts.import_csv(conn, t, d["csv"], source=d.get("source") or "IMPORT",
                                     attest_consent=bool(d.get("attest_consent")), evidence=d.get("evidence") or "",
                                     extra_tags=d.get("tags") or (), mapping=d.get("mapping"))
        out = report.as_dict()
        with conn.cursor() as cur:                              # the log is part of the answer: a failed log is not hidden
            who = ""
            if d.get("by_agent_id"):
                try:
                    who = inbox._agent(cur, t, d["by_agent_id"])["name"]
                except inbox.InboxError:
                    who = ""
            out["import_id"] = contact_book.log_import(cur, t, out, file_name=d.get("file_name") or "", source=d.get("source") or "IMPORT",
                                                       by_name=who)
        conn.commit()
        return {"report": out}

    def c_export(conn, t, d):
        ids = None
        if d.get("contact_ids") or d.get("filter"):               # a selection, or everyone a filter matches
            with conn.cursor() as cur:
                ids = contact_book.ids_for(cur, t, ids=d.get("contact_ids"), filter_=d.get("filter"))
        return {"csv": contacts.export_csv(conn, t, tags=d.get("tags") or None, mask=bool(d.get("mask")), ids=ids)}

    def need_vault():
        if vault is None:
            raise LookupError("store connections are not set up on this server (no token key)")

    def i_create(conn, t, d):
        need_vault()
        _need(d, "kind", "name", "secret")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "connect a store")
            return {"integration": _jsonable(integrations.create(cur, t, vault, d["kind"], d["name"], d["secret"], d.get("config") or {}))}

    def i_update(conn, t, d):
        need_vault()
        _need(d, "integration_id")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "change a store connection")
            integrations.update(cur, t, vault, d["integration_id"], name=d.get("name"), config=d.get("config"),
                                enabled=d.get("enabled"), secret=d.get("secret"))
        return {}

    def a_connect(conn, t, d):
        need_vault()
        _need(d, "ad_account_id", "page_id", "token")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "connect an ad account")
            return {"account_id": ads.connect_account(cur, t, vault, ad_account_id=d["ad_account_id"], page_id=d["page_id"],
                                                      token=d["token"], name=d.get("name") or "")}

    def a_create(conn, t, d):
        need_vault()
        _need(d, "account_id", "name", "whatsapp_number", "daily_budget", "targeting", "message")
        spec = ads.build_spec(name=d["name"], whatsapp_number=d["whatsapp_number"], daily_budget=d["daily_budget"],
                              targeting=d["targeting"], message=d["message"], headline=d.get("headline") or "",
                              image_hash=d.get("image_hash") or "", welcome_text=d.get("welcome_text") or "",
                              ice_breakers=d.get("ice_breakers"), start_time=d.get("start_time") or "", end_time=d.get("end_time") or "")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "create ads")
            plans.require(cur, t, "ads_manager", "The ads manager")
            try:
                out = ads.create_ctwa_ad(cur, t, graph, vault, d["account_id"], spec)
            except ads.AdsError:
                conn.commit()                                   # keep the record of the failed attempt
                raise
        return {"ad": _jsonable(out)}

    def a_activate(conn, t, d):
        need_vault()
        _need(d, "ad_id")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "turn an ad on")
            return {"status": ads.activate(cur, t, graph, vault, d["ad_id"], confirm=d.get("confirm") is True)}

    def a_pause(conn, t, d):
        need_vault()
        _need(d, "ad_id")
        with conn.cursor() as cur:
            _managers_only(cur, t, d, "pause an ad")
            return {"status": ads.pause(cur, t, graph, vault, d["ad_id"])}

    def a_insights(conn, t, d):
        need_vault()
        _need(d, "ad_id")
        with conn.cursor() as cur:
            return {"insights": ads.insights(cur, t, graph, vault, d["ad_id"])}

    def a_list(conn, t, d):
        with conn.cursor() as cur:
            return {"ads": [_jsonable(a) for a in ads.list_ads(cur, t)]}

    def a_accounts(conn, t, d):
        with conn.cursor() as cur:
            return {"accounts": [_jsonable(a) for a in ads.list_accounts(cur, t)]}

    return {"/internal/ads/connect": a_connect, "/internal/ads/create": a_create, "/internal/ads/activate": a_activate,
            "/internal/ads/pause": a_pause, "/internal/ads/insights": a_insights, "/internal/ads/list": a_list, "/internal/ads/accounts": a_accounts,
            "/internal/integrations/create": i_create, "/internal/integrations/update": i_update,
            "/internal/templates/create": t_create, "/internal/templates/sync": t_sync,
            "/internal/templates/delete": t_delete,
            "/internal/contacts/import": c_import, "/internal/contacts/export": c_export,
            **ops_templates.make_conn_ops(graph, vault, app_id), **ops_usage.make_conn_ops(graph, vault, app_id), **ops_ads.make_conn_ops(graph, vault, app_id), **ops_commerce.make_conn_ops(graph, vault, app_id)}


def tenant_exists(cur, t, d):
    """Does this workspace exist here? The licence server asks before it links a licence to one, so a typo cannot give a paying customer an empty inbox."""
    cur.execute("select 1 from tenants where id = %s", (t,))
    return {"exists": cur.fetchone() is not None}


CURSOR_OPS = {
    "/internal/templates/list": templates_list, "/internal/templates/lint": templates_lint,
    "/internal/templates/library": templates_library, "/internal/numbers/list": numbers_list,
    "/internal/campaigns/create": campaigns_create, "/internal/campaigns/preflight": campaigns_preflight,
    "/internal/campaigns/launch": campaigns_launch, "/internal/campaigns/pause": campaigns_pause,
    "/internal/campaigns/resume": campaigns_resume, "/internal/campaigns/cancel": campaigns_cancel,
    "/internal/campaigns/reschedule": campaigns_reschedule, "/internal/campaigns/report": campaigns_report,
    "/internal/campaigns/list": campaigns_list,
    "/internal/segments/validate": segments_validate, "/internal/segments/count": segments_count,
    "/internal/segments/save": segments_save, "/internal/segments/delete": segments_delete,
    "/internal/segments/list": segments_list,
    "/internal/consent/get": consent_get, "/internal/consent/save": consent_save,
    "/internal/contacts/erase": contacts_erase,
    "/internal/inbox/ensure_agent": inbox_ensure_agent, "/internal/inbox/agents/list": inbox_agents_list,
    "/internal/inbox/agents/update": inbox_agents_update, "/internal/inbox/conversations": inbox_conversations,
    "/internal/inbox/request_human": inbox_request_human, "/internal/inbox/intervene": inbox_intervene,
    "/internal/inbox/transfer": inbox_transfer, "/internal/inbox/hand_back": inbox_hand_back,
    "/internal/inbox/resolve": inbox_resolve, "/internal/inbox/notes/add": inbox_note_add,
    "/internal/inbox/notes/list": inbox_notes_list, "/internal/inbox/canned/save": inbox_canned_save,
    "/internal/inbox/canned/delete": inbox_canned_delete, "/internal/inbox/canned/list": inbox_canned_list,
    "/internal/inbox/canned/expand": inbox_canned_expand, "/internal/inbox/settings/get": inbox_settings_get,
    "/internal/inbox/settings/save": inbox_settings_save, "/internal/inbox/monitor": inbox_monitor,
    "/internal/integrations/list": integrations_list, "/internal/integrations/delete": integrations_delete,
    "/internal/api_keys/create": keys_create, "/internal/api_keys/list": keys_list, "/internal/api_keys/revoke": keys_revoke,
}


# ── repeating campaigns and a test to your own phone ─────────────────────────

def campaigns_test(cur, t, d):
    _need(d, "template_name", "to")
    return {"result": campaigns.send_test(cur, t, template_name=d["template_name"], language=d.get("language") or "en_US",
                                          variables=d.get("variables") or {}, to=d["to"], number_id=d.get("number_id"),
                                          sample_name=d.get("sample_name") or "Test")}


def schedules_create(cur, t, d):
    _need(d, "name", "config", "rule")
    _managers_only(cur, t, d, "set up repeating campaigns")
    return {"schedule_id": str(schedules.create(cur, t, d["name"], d["config"], d["rule"], created_by=d.get("by_agent_id")))}


def schedules_list(cur, t, d):
    return {"schedules": [_jsonable(x) for x in schedules.list_schedules(cur, t)]}


def schedules_set_enabled(cur, t, d):
    _need(d, "schedule_id")
    _managers_only(cur, t, d, "change repeating campaigns")
    return {"schedule": _jsonable(schedules.set_enabled(cur, t, d["schedule_id"], bool(d.get("enabled"))))}


def schedules_delete(cur, t, d):
    _need(d, "schedule_id")
    _managers_only(cur, t, d, "delete repeating campaigns")
    return {"deleted": schedules.delete(cur, t, d["schedule_id"])}


# ── chatbot flows ────────────────────────────────────────────────────────────

def _issues(items) -> list:
    return [i.as_dict() for i in items]


def flows_list(cur, t, d):
    return {"flows": [_jsonable(f) for f in flows.list_flows(cur, t)]}


def flows_get(cur, t, d):
    _need(d, "flow_id")
    f = flows.get(cur, t, d["flow_id"])
    return {"flow": _jsonable(f), "issues": _issues(flows.validate(f["graph"], f["trigger"]))}


def flows_create(cur, t, d):
    _need(d, "name")
    _managers_only(cur, t, d, "create chatbot flows")
    return {"flow_id": str(flows.create(cur, t, d["name"], trigger=d.get("trigger"), graph=d.get("graph")))}


def flows_update(cur, t, d):
    _need(d, "flow_id")
    _managers_only(cur, t, d, "change chatbot flows")
    f = flows.update(cur, t, d["flow_id"], name=d.get("name"), trigger=d.get("trigger"), graph=d.get("graph"))
    return {"issues": _issues(flows.validate(f["graph"], f["trigger"]))}


def flows_delete(cur, t, d):
    _need(d, "flow_id")
    _managers_only(cur, t, d, "delete chatbot flows")
    return {"deleted": flows.delete(cur, t, d["flow_id"])}


def flows_check(cur, t, d):
    _need(d, "flow_id")
    issues = flows.check(cur, t, d["flow_id"])
    return {"issues": _issues(issues), "valid": not flows.errors(issues)}


def flows_publish(cur, t, d):
    _need(d, "flow_id")
    _managers_only(cur, t, d, "publish chatbot flows")
    return flows.publish(cur, t, d["flow_id"])


def flows_versions(cur, t, d):
    _need(d, "flow_id")
    return {"versions": [_jsonable(v) for v in flows.versions(cur, t, d["flow_id"])]}


def flows_rollback(cur, t, d):
    _need(d, "flow_id", "version")
    _managers_only(cur, t, d, "roll back chatbot flows")
    return flows.rollback(cur, t, d["flow_id"], int(d["version"]))


def flows_set_enabled(cur, t, d):
    _need(d, "flow_id")
    _managers_only(cur, t, d, "switch chatbot flows on or off")
    flows.set_enabled(cur, t, d["flow_id"], bool(d.get("enabled")))
    return {}


def flows_test(cur, t, d):
    _need(d, "graph")
    return flow_engine.test(d["graph"], d.get("trigger"), list(d.get("inputs") or []), name=d.get("name") or "Test customer",
                            attrs=d.get("attrs") or {}, open_now=bool(d.get("open_now", True)))


def flows_runs(cur, t, d):
    _need(d, "flow_id")
    return {"runs": [_jsonable(r) for r in flow_engine.runs(cur, t, d["flow_id"])]}


def flows_library(cur, t, d):
    return {"library": [dict(item) for item in flow_library.LIBRARY]}


def flows_from_library(cur, t, d):
    _need(d, "key")
    _managers_only(cur, t, d, "create chatbot flows")
    item = flow_library.get(d["key"])
    if item is None:
        raise ValueError("there is no ready-made flow called %r" % d["key"])
    return {"flow_id": str(flows.create(cur, t, d.get("name") or item["title"], trigger=item["trigger"], graph=item["graph"]))}


# ── AI agents ────────────────────────────────────────────────────────────────

def ai_status(cur, t, d):
    return {"usage": ai_agent.usage(cur, t), "included": plans.has(plans.plan_of(cur, t), "ai_agent")}


def ai_agents_list(cur, t, d):
    return {"agents": [_jsonable(a) for a in ai_agent.list_agents(cur, t)]}


def ai_agents_get(cur, t, d):
    _need(d, "agent_id")
    return {"agent": _jsonable(ai_agent.get(cur, t, d["agent_id"]))}


def ai_agents_create(cur, t, d):
    _need(d, "name")
    _managers_only(cur, t, d, "create AI agents")
    return {"agent_id": str(ai_agent.create(cur, t, d["name"], instructions=d.get("instructions") or "", handoff_words=d.get("handoff_words"),
                                            max_replies=int(d.get("max_replies") or 12), business_hours_only=bool(d.get("business_hours_only"))))}


def ai_agents_update(cur, t, d):
    _need(d, "agent_id", "changes")
    _managers_only(cur, t, d, "change AI agents")
    return {"agent": _jsonable(ai_agent.update(cur, t, d["agent_id"], **d["changes"]))}


def ai_agents_delete(cur, t, d):
    _need(d, "agent_id")
    _managers_only(cur, t, d, "delete AI agents")
    return {"deleted": ai_agent.delete(cur, t, d["agent_id"])}


def ai_agents_set_enabled(cur, t, d):
    _need(d, "agent_id")
    _managers_only(cur, t, d, "switch AI agents on or off")
    ai_agent.set_enabled(cur, t, d["agent_id"], bool(d.get("enabled")))
    return {}


def ai_agents_set_default(cur, t, d):
    _need(d, "agent_id")
    _managers_only(cur, t, d, "choose the default AI agent")
    ai_agent.set_default(cur, t, d["agent_id"], bool(d.get("default", True)))
    return {}


def ai_knowledge_list(cur, t, d):
    _need(d, "agent_id")
    return {"knowledge": [_jsonable(k) for k in ai_agent.list_knowledge(cur, t, d["agent_id"])]}


def ai_knowledge_add_text(cur, t, d):
    _need(d, "agent_id", "title", "body")
    _managers_only(cur, t, d, "teach AI agents")
    kind = d.get("kind") if d.get("kind") in ("text", "file") else "text"
    return {"knowledge_id": str(ai_agent.add_knowledge(cur, t, d["agent_id"], d["title"], d["body"], kind=kind, source=d.get("source") or ""))}


def ai_knowledge_add_faq(cur, t, d):
    _need(d, "agent_id", "question", "answer")
    _managers_only(cur, t, d, "teach AI agents")
    return {"knowledge_id": str(ai_agent.add_faq(cur, t, d["agent_id"], d["question"], d["answer"]))}


def ai_knowledge_add_url(cur, t, d):
    _need(d, "agent_id", "url")
    _managers_only(cur, t, d, "teach AI agents")
    return {"knowledge_id": str(ai_agent.add_url(cur, t, d["agent_id"], d["url"], d.get("title") or ""))}


def ai_knowledge_delete(cur, t, d):
    _need(d, "knowledge_id")
    _managers_only(cur, t, d, "teach AI agents")
    return {"deleted": ai_agent.delete_knowledge(cur, t, d["knowledge_id"])}


def ai_test(cur, t, d):
    _need(d, "agent_id", "messages")
    return {"answer": ai_agent.test_chat(cur, t, d["agent_id"], list(d["messages"]))}


CURSOR_OPS.update(ops_book.OPS)
CURSOR_OPS.update(ops_chat.OPS)
CURSOR_OPS.update(ops_campaigns.OPS)
CURSOR_OPS.update(ops_media.OPS)                   # (after the older canned routes: these replace them)
CURSOR_OPS.update(ops_templates.OPS)              # (after the older library route: this one adds the industries)
CURSOR_OPS.update(ops_usage.OPS)
CURSOR_OPS.update({
    "/internal/tenant/exists": tenant_exists,
    "/internal/campaigns/test": campaigns_test,
    "/internal/schedules/create": schedules_create, "/internal/schedules/list": schedules_list,
    "/internal/schedules/set_enabled": schedules_set_enabled, "/internal/schedules/delete": schedules_delete,
    "/internal/flows/list": flows_list, "/internal/flows/get": flows_get, "/internal/flows/create": flows_create,
    "/internal/flows/update": flows_update, "/internal/flows/delete": flows_delete, "/internal/flows/check": flows_check,
    "/internal/flows/publish": flows_publish, "/internal/flows/versions": flows_versions, "/internal/flows/rollback": flows_rollback,
    "/internal/flows/set_enabled": flows_set_enabled, "/internal/flows/test": flows_test, "/internal/flows/runs": flows_runs,
    "/internal/flows/library": flows_library, "/internal/flows/from_library": flows_from_library,
    "/internal/ai/status": ai_status, "/internal/ai/agents/list": ai_agents_list, "/internal/ai/agents/get": ai_agents_get,
    "/internal/ai/agents/create": ai_agents_create, "/internal/ai/agents/update": ai_agents_update,
    "/internal/ai/agents/delete": ai_agents_delete, "/internal/ai/agents/set_enabled": ai_agents_set_enabled,
    "/internal/ai/agents/set_default": ai_agents_set_default, "/internal/ai/knowledge/list": ai_knowledge_list,
    "/internal/ai/knowledge/add_text": ai_knowledge_add_text, "/internal/ai/knowledge/add_faq": ai_knowledge_add_faq,
    "/internal/ai/knowledge/add_url": ai_knowledge_add_url, "/internal/ai/knowledge/delete": ai_knowledge_delete,
    "/internal/ai/test": ai_test,
})


# ── the HTTP skin ────────────────────────────────────────────────────────────

def _answer_for(exc: Exception):
    """Module errors -> (status, body). Anything not listed is a bug and propagates."""
    if isinstance(exc, send.SendRefused):
        return 409, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, campaigns.CampaignError):
        body = {"error": exc.reason, "message": str(exc)}
        if getattr(exc, "report", None):
            body["preflight"] = exc.report.as_dict()
        return 409, body
    if isinstance(exc, flows.FlowError):
        return (422 if exc.reason == "flow_invalid" else 409), {"error": exc.reason, "message": str(exc),
                                                                   "issues": [i.as_dict() for i in exc.issues]}
    if isinstance(exc, media.MediaError):
        return (404 if exc.reason == "unknown_asset" else 413 if exc.reason in ("too_big", "storage_full") else 409 if exc.reason == "in_use" else 400),             {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, campaign_book.BookError):
        return 400, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, api_campaigns.ApiCampaignError):
        return exc.status, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, chat_send.ChatSendError):
        return (404 if exc.reason == "unknown_template" else 409 if exc.reason == "template_not_approved" else 400),             {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, contact_book.BookError):
        return (404 if exc.reason.startswith("unknown_") else 409 if exc.reason in ("tag_exists", "email_taken") else 400),             {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, ai_agent.AIError):
        return 409, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, LLMError):
        return 502, {"error": "ai_failed", "message": str(exc)}
    if isinstance(exc, GraphError):
        return 502, {"error": "meta_error", "message": exc.details or exc.message, "meta": exc.as_dict()}
    if isinstance(exc, capi.CapiError):
        return (409 if exc.reason in ("off", "not_set_up", "no_account", "no_token", "no_click", "no_test_code") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, usage.UsageError):
        return 400, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, number_info.NumberError):
        return (409 if exc.reason in ("no_number", "which_number") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, webhooks_out.WebhookError):
        return (404 if exc.reason.startswith("unknown_") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, schedules.ScheduleError):
        return 400, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, autos.AutoError):
        return (404 if exc.reason.startswith("unknown_") else 409 if exc.reason in ("template_unknown", "template_not_approved", "too_many") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, commerce.CommerceError):
        return (404 if exc.reason.startswith("unknown_") else 403 if exc.reason == "bad_signature" else 409 if exc.reason in (
            "bad_move", "order_locked", "not_ready", "no_gateway", "no_gateway_refund", "not_refundable", "gateway_refused", "no_catalog", "no_number") else 400),             {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, sso.SsoError):
        return (401 if exc.reason == "sso_required" else 404 if exc.reason == "unknown_state" else 409 if exc.reason in ("not_ready", "not_enabled", "not_set_up") else 502 if exc.reason in (
            "provider_unreachable", "provider_refused", "bad_discovery") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, partner.PartnerError):
        return (404 if exc.reason.startswith("unknown_") else 409 if exc.reason in ("already_linked", "self_link", "already_paid") else 400), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, coexistence.CoexistenceError):
        return (404 if exc.reason.startswith("unknown_") else 409), {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, form_endpoint.EndpointError):
        return exc.status, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, forms.FormError):
        return (404 if exc.reason.startswith("unknown_") else 422 if exc.reason in ("form_invalid", "meta_rejected") else 409 if exc.reason in (
            "not_published", "not_set_up", "no_number") else 400), {"error": exc.reason, "message": str(exc), "problems": exc.problems}
    if isinstance(exc, razorpay.RazorpayError):
        return 502, {"error": "gateway_error", "message": str(exc)}
    if isinstance(exc, templates.TemplateInvalid):
        return 422, {"error": "template_invalid", "message": str(exc), "issues": [i.as_dict() for i in exc.issues]}
    if isinstance(exc, templates.TemplateRejected):
        return 422, {"error": "meta_rejected", "message": str(exc), "meta": exc.error.as_dict()}
    if isinstance(exc, templates.TemplateLimit):
        return 429, {"error": "rate_limited", "message": str(exc)}
    if isinstance(exc, contacts.ImportProblem):
        return 400, {"error": "bad_file", "message": str(exc)}
    if isinstance(exc, segments.FilterError):
        return 400, {"error": "bad_filter", "message": str(exc)}
    if isinstance(exc, consent.SettingsError):
        return 400, {"error": "bad_settings", "message": str(exc)}
    if isinstance(exc, inbox.InboxError):
        return 409, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, ads.AdsError):
        return 409 if exc.reason in ("meta_refused", "confirm_required", "wrong_state", "unknown_ad", "unknown_account") else 400, \
            {"error": exc.reason, "message": str(exc), "meta": exc.meta}
    if isinstance(exc, integrations.IntegrationError):
        return 400, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, api_keys.ApiKeyError):
        return 400, {"error": exc.reason, "message": str(exc)}
    if isinstance(exc, plans.PlanLimit):
        return 403, {"error": exc.reason, "message": str(exc), "needs": exc.needs}
    if isinstance(exc, LookupError):
        return 409, {"error": "not_available", "message": str(exc)}
    if isinstance(exc, ValueError):
        return 400, {"error": "bad_request", "message": str(exc)}
    return None


CURSOR_OPS.update(ops_ai.OPS)
CURSOR_OPS.update(ops_autos.OPS)
CURSOR_OPS.update(ops_flows.OPS)                      # last: these replace the older flow test / library routes (a clock for the console, topics for the library)


def mount(app: FastAPI, *, authorised: Callable, connect: Callable, graph: Graph, vault, app_id: str = "", app_secrets: tuple = ()) -> None:
    conn_ops = make_conn_ops(graph, vault, app_id)
    conn_ops.update(ops_forms.make_conn_ops(graph, vault, app_id, app_secrets))
    conn_ops.update(ops_partner.make_conn_ops(graph, vault, app_id))
    ops_usage.mount_realtime(app, authorised=authorised, connect=connect)

    def run(fn, with_conn: bool, tenant_id: str, data: dict, path: str = ""):
        conn = connect()
        try:
            if isinstance(data.get("plan"), str):               # what the licence says; never trusted from a client
                with conn.cursor() as cur:
                    plans.set_plan(cur, tenant_id, data["plan"])
            if (data.get("by_agent_id") or data.get("sso_agent_id")) and path:   # a workspace may require its team to sign in with the company account
                with conn.cursor() as cur:
                    sso.enforce(cur, tenant_id, data, path)
            if with_conn:
                out = fn(conn, tenant_id, data)
            else:
                with conn.cursor() as cur:
                    out = fn(cur, tenant_id, data)
            conn.commit()
            return out
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add(path: str, fn, with_conn: bool) -> None:
        async def handler(request: Request):
            if not authorised(request):
                return await refuse(request)
            try:
                data = await request.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or not _uuid(data.get("tenant_id")):
                return JSONResponse({"ok": False, "error": "bad_request", "message": "tenant_id is required"}, status_code=400)
            try:
                result = await run_in_threadpool(run, fn, with_conn, data["tenant_id"], data, path)
            except Exception as exc:                                 # noqa: BLE001
                answer = _answer_for(exc)
                if answer is None:
                    raise
                status, body = answer
                return JSONResponse({"ok": False, **body}, status_code=status)
            if path.replace("/internal/", "", 1) in audit.ROUTES:
                await run_in_threadpool(audit.log_route, connect, data["tenant_id"], path, data, result if isinstance(result, dict) else {})
            return {"ok": True, **(result or {})}
        app.post(path)(handler)

    for path, fn in CURSOR_OPS.items():
        add(path, fn, False)
    for path, fn in conn_ops.items():
        add(path, fn, True)
