"""Click-to-WhatsApp ads (spec WS13): campaign -> ad set -> creative -> ad, always paused.

The sequence and field names follow the spec's research (§1.2, Marketing API): a campaign with the
engagement objective, an ad set whose destination is WhatsApp and which optimises for conversations
and promotes the Page + WhatsApp number, a creative with a WhatsApp call-to-action and an optional
welcome message with ice-breaker questions, and the ad itself.

SAFETY. Nothing here can spend money by itself:
  * every object is created with status PAUSED;
  * `activate` is a separate call that must be told `confirm=True`;
  * a failure part-way deletes the objects already made, so no half-built campaign is left behind.

NOT VERIFIED AGAINST META LIVE. The calls are tested against a fake Graph server and the shapes come
from Meta's documentation as the spec recorded it on 20-Sep-2026. Creating real ads needs an ad account,
a Page, a token with ads_management / pages_manage_ads / pages_read_engagement / pages_show_list (Advanced
Access review, spec §8), and a first paused ad checked in Ads Manager before anything is activated.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from psycopg2.extras import Json

from prism_wa import plans
from prism_wa.graph import Graph, GraphError
from prism_wa.vault import Vault

WHATSAPP_LINK = "https://api.whatsapp.com/send"
MIN_DAILY_BUDGET = 100            # minor currency units (paise/cents): refuse nonsense, Meta enforces the real minimum
OBJECTIVE = "OUTCOME_ENGAGEMENT"


class AdsError(Exception):
    def __init__(self, reason: str, message: str, meta: Optional[dict] = None):
        super().__init__(message)
        self.reason = reason
        self.meta = meta or {}


def connect_account(cur, tenant_id, vault: Vault, *, ad_account_id: str, page_id: str, token: str, name: str = "") -> str:
    plans.require(cur, tenant_id, "ads_manager", "The ads manager")
    ad_account_id = re.sub(r"^act_", "", (ad_account_id or "").strip())
    if not re.match(r"^[0-9]{5,20}$", ad_account_id):
        raise AdsError("bad_account", "the ad account id is the number shown in Ads Manager")
    if not re.match(r"^[0-9]{5,25}$", (page_id or "").strip()):
        raise AdsError("bad_page", "the Page id is a number")
    if len((token or "").strip()) < 20:
        raise AdsError("bad_token", "paste the access token for this ad account")
    blob, ver = vault.encrypt(token.strip(), context="act_" + ad_account_id)
    cur.execute("insert into ad_accounts (tenant_id, ad_account_id, page_id, name, token_enc, key_ver)"
                " values (%s, %s, %s, %s, %s, %s) on conflict (tenant_id, ad_account_id) do update set"
                " page_id = excluded.page_id, name = excluded.name, token_enc = excluded.token_enc, key_ver = excluded.key_ver"
                " returning id", (tenant_id, ad_account_id, page_id.strip(), (name or "").strip() or None, blob, ver))
    return str(cur.fetchone()[0])


def _account(cur, tenant_id, vault: Vault, account_id) -> dict:
    cur.execute("select id, ad_account_id, page_id, token_enc from ad_accounts where id = %s and tenant_id = %s", (account_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise AdsError("unknown_account", "that ad account is not connected to this workspace")
    return {"id": row[0], "act": "act_" + row[1], "page_id": row[2], "token": vault.decrypt(row[3], context="act_" + row[1])}


def build_spec(*, name: str, whatsapp_number: str, daily_budget: Optional[int] = None, targeting: dict, message: str, headline: str = "",
               image_hash: str = "", welcome_text: str = "", ice_breakers: Optional[list] = None, start_time: str = "",
               end_time: str = "", lifetime_budget: Optional[int] = None) -> dict:
    """Validate what was asked for and return it normalised. A budget is a daily one, or a lifetime one that needs an end time."""
    if not (name or "").strip():
        raise AdsError("name_required", "give the ad a name")
    digits = re.sub(r"\D", "", whatsapp_number or "")
    if not 8 <= len(digits) <= 15:
        raise AdsError("bad_number", "the WhatsApp number must include its country code")
    if lifetime_budget is not None:
        if not (isinstance(lifetime_budget, int) and not isinstance(lifetime_budget, bool) and lifetime_budget >= MIN_DAILY_BUDGET):
            raise AdsError("bad_budget", "the total budget is a whole number of the smallest currency unit (paise)")
        if not end_time:
            raise AdsError("bad_budget", "a total budget needs an end date: it is spent between the start and the end")
    elif not (isinstance(daily_budget, int) and daily_budget >= MIN_DAILY_BUDGET):
        raise AdsError("bad_budget", "the daily budget is a whole number of the smallest currency unit (paise)")
    geo = targeting.get("geo_locations") if isinstance(targeting, dict) else None
    if not (isinstance(geo, dict) and (geo.get("countries") or geo.get("cities"))):
        raise AdsError("bad_targeting", "choose where to show it: targeting.geo_locations needs countries or cities")
    if not (message or "").strip():
        raise AdsError("message_required", "write the ad's main text")
    breakers = [b.strip() for b in (ice_breakers or []) if b and b.strip()]
    if len(breakers) > 4 or any(len(b) > 80 for b in breakers):
        raise AdsError("bad_ice_breakers", "up to 4 quick questions of 80 characters each")
    out = {"name": name.strip(), "whatsapp_number": digits, "daily_budget": daily_budget, "targeting": targeting,
           "message": message.strip(), "headline": (headline or "").strip(), "image_hash": image_hash or "",
           "welcome_text": (welcome_text or "").strip(), "ice_breakers": breakers, "start_time": start_time, "end_time": end_time}
    if lifetime_budget is not None:
        out["lifetime_budget"] = lifetime_budget
        out.pop("daily_budget")
    return out


def create_ctwa_ad(cur, tenant_id, graph: Graph, vault: Vault, account_id, spec: dict) -> dict:
    """Make the four objects, paused. Returns the ad row. Cleans up after itself on failure."""
    acct = _account(cur, tenant_id, vault, account_id)
    made = {}
    try:
        made["campaign"] = graph._call("POST", "%s/campaigns" % acct["act"], token=acct["token"], body={
            "name": spec["name"], "objective": OBJECTIVE, "status": "PAUSED", "special_ad_categories": []})["id"]
        budget = {"lifetime_budget": spec["lifetime_budget"]} if spec.get("lifetime_budget") else {"daily_budget": spec["daily_budget"]}
        adset = {"name": spec["name"] + " - audience", "campaign_id": made["campaign"], "status": "PAUSED",
                 **budget, "billing_event": "IMPRESSIONS", "optimization_goal": "CONVERSATIONS",
                 "destination_type": "WHATSAPP", "targeting": spec["targeting"],
                 "promoted_object": {"page_id": acct["page_id"], "whatsapp_phone_number": spec["whatsapp_number"]}}
        for key in ("start_time", "end_time"):
            if spec.get(key):
                adset[key] = spec[key]
        made["adset"] = graph._call("POST", "%s/adsets" % acct["act"], token=acct["token"], body=adset)["id"]
        link = {"link": WHATSAPP_LINK, "message": spec["message"],
                "call_to_action": {"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP"}}}
        if spec["headline"]:
            link["name"] = spec["headline"]
        if spec["image_hash"]:
            link["image_hash"] = spec["image_hash"]
        if spec["welcome_text"] or spec["ice_breakers"]:
            welcome = {"type": "VISUAL_EDITOR", "version": 2, "landing_screen_type": "welcome_message",
                       "media_type": "text", "text_format": {"customer_action_type": "autofill_message",
                                                              "message": {"text": spec["welcome_text"] or "Hi! How can we help?",
                                                                          "autofill_message": {"body": spec["welcome_text"] or "Hi!"}}}}
            if spec["ice_breakers"]:
                welcome["ice_breakers"] = [{"title": b, "response": b} for b in spec["ice_breakers"]]
            link["page_welcome_message"] = json.dumps(welcome)
        made["creative"] = graph._call("POST", "%s/adcreatives" % acct["act"], token=acct["token"], body={
            "name": spec["name"] + " - creative", "object_story_spec": {"page_id": acct["page_id"], "link_data": link}})["id"]
        made["ad"] = graph._call("POST", "%s/ads" % acct["act"], token=acct["token"], body={
            "name": spec["name"], "adset_id": made["adset"], "creative": {"creative_id": made["creative"]}, "status": "PAUSED"})["id"]
    except GraphError as exc:
        for kind in ("ad", "creative", "adset", "campaign"):          # best-effort: leave nothing half-built
            if made.get(kind) and kind != "creative":
                try:
                    graph._call("DELETE", made[kind], token=acct["token"])
                except GraphError:
                    pass
        cur.execute("insert into ads (tenant_id, account_id, name, meta_ids, spec, status, last_error)"
                    " values (%s, %s, %s, %s, %s, 'FAILED', %s)",
                    (tenant_id, account_id, spec["name"], Json(made), Json(spec), Json(exc.as_dict())))
        raise AdsError("meta_refused", "Meta did not accept the ad: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    cur.execute("insert into ads (tenant_id, account_id, name, meta_ids, spec, status) values (%s, %s, %s, %s, %s, 'PAUSED') returning id",
                (tenant_id, account_id, spec["name"], Json(made), Json(spec)))
    return {"id": str(cur.fetchone()[0]), "meta_ids": made, "status": "PAUSED"}


def activate(cur, tenant_id, graph: Graph, vault: Vault, ad_id, *, confirm: bool = False) -> str:
    """Turn a paused ad on. Spends money, so it must be told to."""
    if confirm is not True:
        raise AdsError("confirm_required", "turning an ad on spends money; confirm it explicitly")
    cur.execute("select account_id, meta_ids, status from ads where id = %s and tenant_id = %s for update", (ad_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise AdsError("unknown_ad", "that ad is not in this workspace")
    if row[2] != "PAUSED":
        raise AdsError("wrong_state", "only a paused ad can be turned on")
    acct = _account(cur, tenant_id, vault, row[0])
    ids = row[1]
    try:
        for kind in ("campaign", "adset", "ad"):
            graph._call("POST", ids[kind], token=acct["token"], body={"status": "ACTIVE"})
    except GraphError as exc:
        raise AdsError("meta_refused", "Meta did not turn the ad on: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    cur.execute("update ads set status = 'ACTIVE' where id = %s", (ad_id,))
    return "ACTIVE"


def pause(cur, tenant_id, graph: Graph, vault: Vault, ad_id) -> str:
    cur.execute("select account_id, meta_ids from ads where id = %s and tenant_id = %s for update", (ad_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise AdsError("unknown_ad", "that ad is not in this workspace")
    acct = _account(cur, tenant_id, vault, row[0])
    try:
        graph._call("POST", row[1]["ad"], token=acct["token"], body={"status": "PAUSED"})
    except GraphError as exc:
        raise AdsError("meta_refused", "Meta did not pause the ad: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    cur.execute("update ads set status = 'PAUSED' where id = %s", (ad_id,))
    return "PAUSED"


def insights(cur, tenant_id, graph: Graph, vault: Vault, ad_id) -> dict:
    """Impressions, reach, clicks, spend and conversations started, as Meta reports them."""
    cur.execute("select account_id, meta_ids from ads where id = %s and tenant_id = %s", (ad_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise AdsError("unknown_ad", "that ad is not in this workspace")
    acct = _account(cur, tenant_id, vault, row[0])
    try:
        out = graph._call("GET", "%s/insights" % row[1]["ad"], token=acct["token"],
                          query={"fields": "impressions,reach,clicks,ctr,cpc,spend,actions", "date_preset": "maximum"})
    except GraphError as exc:
        raise AdsError("meta_refused", "Meta did not return the numbers: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    data = (out.get("data") or [{}])[0]
    started = sum(int(float(a.get("value", 0))) for a in data.get("actions") or []
                  if "messaging_conversation_started" in str(a.get("action_type")))
    spend = float(data.get("spend") or 0)
    return {"impressions": int(data.get("impressions") or 0), "reach": int(data.get("reach") or 0),
            "clicks": int(data.get("clicks") or 0), "spend": spend, "conversations_started": started,
            "cost_per_conversation": round(spend / started, 2) if started else None}


def list_ads(cur, tenant_id) -> list:
    cur.execute("select id, name, status, meta_ids, created_at, last_error, kind, meta_status, review, synced_at, spec from ads where tenant_id = %s order by created_at desc", (tenant_id,))
    out = [dict(zip(("id", "name", "status", "meta_ids", "created_at", "last_error", "kind", "meta_status", "review", "synced_at", "spec"), r)) for r in cur.fetchall()]
    for a in out:
        spec = a.pop("spec") or {}
        a["budget"] = {"daily": spec.get("daily_budget"), "lifetime": spec.get("lifetime_budget"), "start": spec.get("start_time") or None, "end": spec.get("end_time") or None}
    return out


def list_accounts(cur, tenant_id) -> list:
    """The connected ad accounts. Never the token."""
    cur.execute("select id, ad_account_id, page_id, name, created_at, connected_via, pixel_id, currency from ad_accounts where tenant_id = %s order by created_at", (tenant_id,))
    return [dict(zip(("id", "ad_account_id", "page_id", "name", "created_at", "connected_via", "pixel_id", "currency"), r)) for r in cur.fetchall()]
