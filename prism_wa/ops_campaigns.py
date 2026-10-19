"""Routes for a campaign in detail, the campaign settings and API campaigns.

    campaigns   recipients  recipients_csv  failures  settings/get  settings/save
    api_campaigns   create  list  set_live  delete
"""
from __future__ import annotations

from prism_wa import api_campaigns, campaign_book
from prism_wa.ops_common import jsonable, managers_only, need


def campaigns_recipients(cur, t, d):
    need(d, "campaign_id")
    return jsonable(campaign_book.recipients(cur, t, d["campaign_id"], status=d.get("status") or None, search=d.get("search") or "",
                                             limit=int(d.get("limit") or 100), offset=int(d.get("offset") or 0)))


def campaigns_recipients_csv(cur, t, d):
    need(d, "campaign_id")
    return {"csv": campaign_book.recipients_csv(cur, t, d["campaign_id"], status=d.get("status") or None)}


def campaigns_failures(cur, t, d):
    need(d, "campaign_id")
    return {"failures": campaign_book.failures(cur, t, d["campaign_id"])}


def campaigns_settings_get(cur, t, d):
    return {"settings": campaign_book.settings(cur, t)}


def campaigns_settings_save(cur, t, d):
    managers_only(cur, t, d, "change campaign settings")
    return {"settings": campaign_book.save_settings(cur, t, d.get("settings") or {})}


def api_campaigns_create(cur, t, d):
    need(d, "name", "template_name")
    managers_only(cur, t, d, "make API campaigns")
    return {"campaign_id": api_campaigns.create(cur, t, d["name"], d["template_name"], d.get("language") or "en_US")}


def api_campaigns_list(cur, t, d):
    return {"campaigns": jsonable(api_campaigns.list_api(cur, t))}


def api_campaigns_set_live(cur, t, d):
    need(d, "campaign_id")
    managers_only(cur, t, d, "switch API campaigns on or off")
    api_campaigns.set_live(cur, t, d["campaign_id"], bool(d.get("live")))
    return {}


def api_campaigns_delete(cur, t, d):
    need(d, "campaign_id")
    managers_only(cur, t, d, "delete API campaigns")
    return {"deleted": api_campaigns.delete(cur, t, d["campaign_id"])}


OPS = {
    "/internal/campaigns/recipients": campaigns_recipients, "/internal/campaigns/recipients_csv": campaigns_recipients_csv,
    "/internal/campaigns/failures": campaigns_failures, "/internal/campaigns/settings/get": campaigns_settings_get,
    "/internal/campaigns/settings/save": campaigns_settings_save,
    "/internal/api_campaigns/create": api_campaigns_create, "/internal/api_campaigns/list": api_campaigns_list,
    "/internal/api_campaigns/set_live": api_campaigns_set_live, "/internal/api_campaigns/delete": api_campaigns_delete,
}

