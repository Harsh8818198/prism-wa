"""Routes for the ads manager beyond the first click-to-WhatsApp ones.

    ads    create (any kind)  upload_image  copy  search  estimate  status  report  suggestions  suggest  decide
           connect_start  connect_finish  connect_status  connect_choose
    audiences   list  from_segment  lookalike  delete

An ad is always made paused; turning it on is ads/activate (unchanged) and needs `confirm: true`. The older routes (connect, activate, pause, insights, list, accounts)
are in ops.py.
"""
from __future__ import annotations

import os

from prism_wa import ads, ads_build, ads_connect, ads_insights, ads_targeting, ai_agent, plans
from prism_wa.graph import Graph
from prism_wa.ops_common import jsonable, managers_only, need
from prism_wa.vault import Vault


def make_conn_ops(graph: Graph, vault: Vault, app_id: str = "") -> dict:
    def need_vault():
        if vault is None:
            raise LookupError("this is not set up on this server (no token key)")

    def create(conn, t, d):
        need_vault()
        need(d, "account_id")
        spec = ads_build.build_spec(d)
        with conn.cursor() as cur:
            managers_only(cur, t, d, "create ads")
            plans.require(cur, t, "ads_manager", "The ads manager")
            try:
                out = ads_build.create_ad(cur, t, graph, vault, d["account_id"], spec)
            except ads.AdsError:
                conn.commit()                                   # keep the record of the failed attempt
                raise
        return {"ad": jsonable(out)}

    def upload_image(conn, t, d):
        need_vault()
        need(d, "account_id", "asset_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "send pictures to Meta")
            return ads_build.upload_image(cur, t, graph, vault, d["account_id"], d["asset_id"])

    def copy(conn, t, d):
        need(d, "offer")
        if ai_agent.LLM_CLIENT is None:
            raise ads.AdsError("ai_not_configured", "AI writing is not switched on for this server yet.")
        variants, tokens = ads_build.copy_variants(ai_agent.LLM_CLIENT, goal=d.get("goal") or "", offer=d["offer"], audience=d.get("audience") or "", language=d.get("language") or "English")
        with conn.cursor() as cur:
            ai_agent.record_usage(cur, t, None, None, "aux", tokens)
        return {"variants": variants}

    def search(conn, t, d):
        need_vault()
        need(d, "account_id", "kind", "q")
        with conn.cursor() as cur:
            return {"results": ads_targeting.search(cur, t, graph, vault, d["account_id"], d["kind"], d["q"], int(d.get("limit") or 12))}

    def estimate(conn, t, d):
        need_vault()
        need(d, "account_id", "targeting")
        with conn.cursor() as cur:
            return {"estimate": ads_targeting.estimate(cur, t, graph, vault, d["account_id"], d["targeting"], kind=d.get("kind") or "ctwa")}

    def status(conn, t, d):
        need_vault()
        need(d, "ad_id")
        with conn.cursor() as cur:
            return ads_insights.refresh_status(cur, t, graph, vault, d["ad_id"])

    def report(conn, t, d):
        need_vault()
        need(d, "ad_id")
        with conn.cursor() as cur:
            return jsonable(ads_insights.over_time(cur, t, graph, vault, d["ad_id"], d.get("since") or None, d.get("until") or None))

    def suggestions(conn, t, d):
        with conn.cursor() as cur:
            return {"suggestions": jsonable(ads_insights.list_suggestions(cur, t, d.get("state") or "open"))}

    def suggest(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            managers_only(cur, t, d, "see suggestions")
            return {"suggestions": jsonable(ads_insights.suggest(cur, t, graph, vault))}

    def decide(conn, t, d):
        need_vault()
        need(d, "suggestion_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "act on a suggestion")
            return ads_insights.decide(cur, t, graph, vault, d["suggestion_id"], apply=d.get("apply") is True)

    def connect_start(conn, t, d):
        need(d, "redirect_uri")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "connect an ad account")
            return ads_connect.start(cur, t, app_id=os.environ.get("META_APP_ID") or app_id, redirect_uri=d["redirect_uri"])

    def connect_finish(conn, t, d):
        need_vault()
        need(d, "state", "code")
        with conn.cursor() as cur:
            return ads_connect.finish(cur, t, graph, vault, state=d["state"], code=d["code"], app_id=os.environ.get("META_APP_ID") or app_id,
                                      app_secret=os.environ.get("META_APP_SECRET") or "")

    def connect_status(conn, t, d):
        need(d, "state")
        with conn.cursor() as cur:
            return ads_connect.status(cur, t, d["state"])

    def connect_choose(conn, t, d):
        need_vault()
        need(d, "state", "ad_account_id", "page_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "connect an ad account")
            return {"account_id": ads_connect.choose(cur, t, vault, state=d["state"], ad_account_id=d["ad_account_id"], page_id=d["page_id"], name=d.get("name") or "")}

    def audiences(conn, t, d):
        with conn.cursor() as cur:
            return {"audiences": jsonable(ads_targeting.list_audiences(cur, t, d.get("account_id") or None))}

    def from_segment(conn, t, d):
        need_vault()
        need(d, "account_id", "segment_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "send a people list to Meta")
            return ads_targeting.audience_from_segment(cur, t, graph, vault, d["account_id"], d["segment_id"], d.get("name") or "")

    def lookalike(conn, t, d):
        need_vault()
        need(d, "account_id", "audience_id", "country")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "make a look-alike list")
            return ads_targeting.lookalike(cur, t, graph, vault, d["account_id"], d["audience_id"], country=d["country"], ratio=d.get("ratio", 0.01), name=d.get("name") or "")

    def delete_audience(conn, t, d):
        need_vault()
        need(d, "audience_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "remove a people list")
            return {"deleted": ads_targeting.delete_audience(cur, t, graph, vault, d["audience_id"])}

    return {"/internal/ads/create": create, "/internal/ads/upload_image": upload_image, "/internal/ads/copy": copy, "/internal/ads/search": search,
            "/internal/ads/estimate": estimate, "/internal/ads/status": status, "/internal/ads/report": report, "/internal/ads/suggestions": suggestions,
            "/internal/ads/suggest": suggest, "/internal/ads/decide": decide, "/internal/ads/connect_start": connect_start, "/internal/ads/connect_finish": connect_finish,
            "/internal/ads/connect_status": connect_status, "/internal/ads/connect_choose": connect_choose,
            "/internal/audiences/list": audiences, "/internal/audiences/from_segment": from_segment, "/internal/audiences/lookalike": lookalike,
            "/internal/audiences/delete": delete_audience}
