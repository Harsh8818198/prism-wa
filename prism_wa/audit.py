"""Who changed what, and when.

Every route that changes something that matters (a setting, a person's access, a template, a campaign, an ad, money, a key) leaves one line here once it has worked. The line says
who (the teammate, by name as they were then), what, and to which thing, and never carries a secret, a message body or a phone number. Recording is on for every plan;
reading and downloading it is an Enterprise feature.

A line is written after the change has been saved, in its own short transaction, and a failure to write it is logged, never shown: a change must not be undone because its
receipt could not be filed. Lines are kept for 400 days.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from psycopg2.extras import Json

from prism_wa import plans
from prism_wa.jobs import utcnow

log = logging.getLogger("prism_wa.audit")
KEEP_DAYS = 400

# route -> (action, what it was done to, request field that names it, the words)
ROUTES = {
    "ads/connect": ("ads.connect", "ad account", "ad_account_id", "Connected an ad account"),
    "ads/connect_choose": ("ads.connect", "ad account", "ad_account_id", "Connected an ad account through Facebook"),
    "ads/create": ("ads.create", "ad", "name", "Made an ad"),
    "ads/activate": ("ads.activate", "ad", "ad_id", "Turned an ad on"),
    "ads/pause": ("ads.pause", "ad", "ad_id", "Paused an ad"),
    "ads/decide": ("ads.suggestion", "suggestion", "suggestion_id", "Decided on an ads suggestion"),
    "ai/agents/create": ("ai.create", "AI agent", "name", "Made an AI agent"),
    "ai/agents/update": ("ai.update", "AI agent", "agent_id", "Changed an AI agent"),
    "ai/agents/delete": ("ai.delete", "AI agent", "agent_id", "Removed an AI agent"),
    "ai/agents/publish": ("ai.publish", "AI agent", "agent_id", "Published an AI agent"),
    "ai/agents/unpublish": ("ai.unpublish", "AI agent", "agent_id", "Paused an AI agent"),
    "ai/agents/set_enabled": ("ai.enable", "AI agent", "agent_id", "Switched an AI agent on or off"),
    "api_campaigns/create": ("api.campaign_create", "API campaign", "name", "Made an API campaign"),
    "api_campaigns/delete": ("api.campaign_delete", "API campaign", "api_campaign_id", "Removed an API campaign"),
    "api_keys/create": ("api.key_create", "API key", "name", "Made an API key"),
    "api_keys/revoke": ("api.key_revoke", "API key", "key_id", "Revoked an API key"),
    "attributes/define": ("contacts.attribute_define", "attribute", "key", "Added a contact attribute"),
    "attributes/delete": ("contacts.attribute_delete", "attribute", "key", "Removed a contact attribute"),
    "autos/create": ("autos.create", "automatic message", "name", "Made an automatic message"),
    "autos/update": ("autos.update", "automatic message", "auto_id", "Changed an automatic message"),
    "autos/delete": ("autos.delete", "automatic message", "auto_id", "Removed an automatic message"),
    "autos/set_enabled": ("autos.enable", "automatic message", "auto_id", "Switched an automatic message on or off"),
    "campaigns/launch": ("campaigns.launch", "campaign", "campaign_id", "Launched a campaign"),
    "campaigns/cancel": ("campaigns.cancel", "campaign", "campaign_id", "Cancelled a campaign"),
    "campaigns/settings/save": ("campaigns.settings", "campaign settings", None, "Changed the campaign settings"),
    "capi/save": ("capi.save", "conversions", None, "Changed the Conversions API settings"),
    "catalog/delete": ("shop.product_delete", "product", "product_id", "Removed a product"),
    "catalog/import": ("shop.import", "catalogue", None, "Imported products"),
    "catalog/push": ("shop.push", "catalogue", None, "Sent the catalogue to Meta"),
    "commerce/config/save": ("shop.settings", "shop settings", None, "Changed the shop settings"),
    "consent/save": ("consent.settings", "opt-in settings", None, "Changed the opt-in and opt-out settings"),
    "contacts/delete": ("contacts.delete", "contact", "contact_id", "Deleted a contact"),
    "contacts/erase": ("contacts.erase", "contact", "contact_id", "Erased a contact's data"),
    "contacts/import": ("contacts.import", "contacts", None, "Imported contacts"),
    "contacts/export": ("contacts.export", "contacts", None, "Downloaded contacts"),
    "flows/create": ("flows.create", "chatbot flow", "name", "Made a chatbot flow"),
    "flows/delete": ("flows.delete", "chatbot flow", "flow_id", "Removed a chatbot flow"),
    "flows/publish": ("flows.publish", "chatbot flow", "flow_id", "Published a chatbot flow"),
    "flows/set_enabled": ("flows.enable", "chatbot flow", "flow_id", "Switched a chatbot flow on or off"),
    "forms/create": ("forms.create", "form", "name", "Made a form"),
    "forms/delete": ("forms.delete", "form", "form_id", "Removed a form"),
    "forms/publish": ("forms.publish", "form", "form_id", "Published a form"),
    "forms/retire": ("forms.retire", "form", "form_id", "Retired a form"),
    "forms/export": ("forms.export", "form answers", "form_id", "Downloaded form answers"),
    "inbox/agents/update": ("team.update", "team member", "agent_id", "Changed a team member"),
    "inbox/settings/save": ("inbox.settings", "inbox settings", None, "Changed the inbox settings"),
    "integrations/create": ("integrations.create", "integration", "name", "Connected an integration"),
    "integrations/update": ("integrations.update", "integration", "integration_id", "Changed an integration"),
    "integrations/delete": ("integrations.delete", "integration", "integration_id", "Removed an integration"),
    "media/delete": ("media.delete", "file", "asset_id", "Removed a file from the Media Library"),
    "orders/cancel": ("shop.order_cancel", "order", "order_id", "Cancelled an order"),
    "partner/branding/save": ("partner.branding", "branding", None, "Changed the branding"),
    "partner/redeem": ("partner.link", "customer workspace", None, "Linked a customer workspace"),
    "partner/unlink": ("partner.unlink", "customer workspace", "child_tenant_id", "Unlinked a customer workspace"),
    "payments/refund": ("shop.refund", "payment", "payment_id", "Gave money back"),
    "payments/request": ("shop.payment_request", "order", "order_id", "Asked a customer to pay"),
    "profile/save": ("profile.save", "business profile", None, "Changed the business profile"),
    "schedules/create": ("campaigns.schedule", "repeating campaign", "name", "Made a repeating campaign"),
    "schedules/delete": ("campaigns.schedule_delete", "repeating campaign", "schedule_id", "Removed a repeating campaign"),
    "segments/delete": ("segments.delete", "segment", "segment_id", "Removed a segment"),
    "sso/save": ("sso.settings", "single sign-on", None, "Changed the single sign-on settings"),
    "tags/delete": ("contacts.tag_delete", "tag", "name", "Removed a tag"),
    "templates/create": ("templates.create", "template", "name", "Submitted a template"),
    "templates/delete": ("templates.delete", "template", "template_id", "Deleted a template"),
    "usage/rates_save": ("usage.rates", "rate card", None, "Changed the message price list"),
    "webhooks/create": ("webhooks.create", "webhook", "url", "Added a webhook"),
    "webhooks/update": ("webhooks.update", "webhook", "endpoint_id", "Changed a webhook"),
    "webhooks/delete": ("webhooks.delete", "webhook", "endpoint_id", "Removed a webhook"),
    "webhooks/rotate_secret": ("webhooks.rotate", "webhook", "endpoint_id", "Changed a webhook's signing secret"),
}
SAFE_TARGETS = {"name", "key", "tag", "url", "ad_account_id"}                       # fields whose value is fine to show; ids are shown as ids


class AuditError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def record(cur, tenant_id, action: str, *, summary: str, actor_id=None, actor_label: str = "", source: str = "app", target_kind: str = "", target_id: str = "",
           detail: Optional[dict] = None, now: Optional[datetime] = None) -> None:
    label = actor_label
    if actor_id and not label:
        cur.execute("select name, role from agents where id = %s and tenant_id = %s", (actor_id, tenant_id))
        row = cur.fetchone()
        label = ("%s (%s)" % (row[0], row[1])) if row else ""
    cur.execute("insert into audit_log (tenant_id, at, actor_id, actor_label, source, action, target_kind, target_id, summary, detail) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, now or utcnow(), actor_id, label[:200] or None, source, action[:80], target_kind[:60] or None, str(target_id)[:200] or None, summary[:300], Json(detail) if detail else None))


def log_route(connect: Callable, tenant_id, path: str, data: dict, result: Optional[dict]) -> None:
    """File the receipt for a route that just worked. Never raises."""
    key = path.replace("/internal/", "", 1)
    spec = ROUTES.get(key)
    if spec is None:
        return
    action, kind, field, words = spec
    target = ""
    if field:
        target = str((result or {}).get(field) or data.get(field) or (((result or {}).get("ad") or {}).get("id")) or "")
    summary = words if not (target and field in SAFE_TARGETS) else "%s: %s" % (words, target[:80])
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            record(cur, tenant_id, action, summary=summary, actor_id=data.get("by_agent_id"), target_kind=kind, target_id=target if field not in SAFE_TARGETS else "",
                   source="api" if data.get("via") == "api" else "app")
        conn.commit()
    except Exception:                                                      # noqa: BLE001
        log.exception("could not file the audit line for %s", key)
        if conn is not None:
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()


def _need_feature(cur, tenant_id) -> None:
    plans.require(cur, tenant_id, "audit_log", "The audit log")


def list_entries(cur, tenant_id, *, action: str = "", actor: str = "", q: str = "", since=None, until=None, limit: int = 100, offset: int = 0) -> dict:
    _need_feature(cur, tenant_id)
    where, params = ["tenant_id = %s"], [tenant_id]
    if action:
        where.append("action like %s")
        params.append(action.replace("%", "") + "%")
    if actor:
        where.append("actor_label ilike %s")
        params.append("%" + actor + "%")
    if q:
        where.append("(summary ilike %s or coalesce(target_id, '') ilike %s)")
        params += ["%" + q + "%"] * 2
    if since:
        where.append("at >= %s")
        params.append(since)
    if until:
        where.append("at < %s")
        params.append(until)
    cur.execute("select count(*) from audit_log where " + " and ".join(where), params)
    total = cur.fetchone()[0]
    cur.execute("select id, at, actor_id, actor_label, source, action, target_kind, target_id, summary, detail from audit_log where " + " and ".join(where)
                + " order by at desc, id desc limit %s offset %s", params + [max(1, min(int(limit), 500)), max(0, int(offset))])
    cols = ("id", "at", "actor_id", "actor", "source", "action", "target_kind", "target_id", "summary", "detail")
    entries = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("select distinct split_part(action, '.', 1) from audit_log where tenant_id = %s order by 1", (tenant_id,))
    return {"entries": entries, "total": total, "areas": [r[0] for r in cur.fetchall()]}


def export_csv(cur, tenant_id, since=None, until=None) -> str:
    _need_feature(cur, tenant_id)
    where, params = ["tenant_id = %s"], [tenant_id]
    if since:
        where.append("at >= %s")
        params.append(since)
    if until:
        where.append("at < %s")
        params.append(until)
    cur.execute("select at, actor_label, source, action, target_kind, target_id, summary from audit_log where " + " and ".join(where) + " order by at, id limit 100000", params)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["when", "who", "through", "action", "kind", "target", "what happened"])
    for at, who, source, action, kind, target, summary in cur.fetchall():
        w.writerow([at.isoformat(), _safe(who), source, action, kind or "", _safe(target), _safe(summary)])
    return out.getvalue()


def _safe(v) -> str:
    text = "" if v is None else str(v)
    return "'" + text if text[:1] in "=+-@" else text


def purge(cur, tenant_id=None, *, now: Optional[datetime] = None, keep_days: int = KEEP_DAYS) -> int:
    """Drop lines older than `keep_days` (for one workspace, or for all of them when none is named)."""
    cutoff = (now or utcnow()) - timedelta(days=keep_days)
    if tenant_id is None:
        cur.execute("delete from audit_log where at < %s", (cutoff,))
    else:
        cur.execute("delete from audit_log where at < %s and tenant_id = %s", (cutoff, tenant_id))
    return cur.rowcount
