"""The ads manager beyond click-to-WhatsApp: website and lead-form ads, targeting, people lists, Meta's review state, numbers over time, suggestions, and signing in with
Facebook. Against a fake Graph server: this proves the sequence, the safety rules and the bookkeeping, NOT that Meta accepts the field names (ads_build.py says so)."""
import json
import os
from urllib.parse import parse_qs, urlsplit

import helpers as H
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import ads, ads_build, ads_connect, ads_insights, ads_targeting, ai_agent, media
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.llm import LLM
from prism_wa.media_store import LocalStore
from prism_wa.vault import Vault

TOKEN = "EAAB-ads-token-that-is-long-enough"
TARGETING = {"geo_locations": {"countries": ["IN"]}, "age_min": 25}


class FakeMeta:
    def __init__(self):
        self.calls, self.fail_on, self.counter = [], None, 0
        self.status = {"effective_status": "PENDING_REVIEW"}
        self.rows = []
        self.me = {"name": "Asha Patel"}

    def __call__(self, method, url, data, headers):
        parts = urlsplit(url)
        path = parts.path.split("/v25.0/", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        raw = data
        body = None
        if data and headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(data)
        self.calls.append({"method": method, "path": path, "body": body, "query": query, "auth": headers.get("Authorization"), "raw": raw})
        if self.fail_on and path.endswith(self.fail_on) and method == "POST":
            return 400, json.dumps({"error": {"message": "Invalid parameter", "code": 100, "error_user_msg": "Budget too low"}}).encode()
        if method == "GET":
            if path == "search":
                if query["type"] == "adgeolocation":
                    return 200, json.dumps({"data": [{"key": "2418", "name": "Vadodara", "type": "city", "region": "Gujarat", "country_name": "India", "country_code": "IN"}]}).encode()
                if query["type"] == "adinterest":
                    return 200, json.dumps({"data": [{"id": "6003", "name": "Interior design", "audience_size_lower_bound": 1000000, "audience_size_upper_bound": 2000000, "path": ["Interests", "Home"]}]}).encode()
                return 200, json.dumps({"data": [{"id": "6004", "name": "Small business owners"}]}).encode()
            if path.endswith("/delivery_estimate"):
                return 200, json.dumps({"data": [{"estimate_ready": True, "estimate_dau": 12000, "estimate_mau_lower_bound": 300000, "estimate_mau_upper_bound": 400000}]}).encode()
            if path.endswith("/insights"):
                return 200, json.dumps({"data": self.rows}).encode()
            if path == "oauth/access_token":
                return 200, json.dumps({"access_token": "short-token" if "code" in query else "long-lived-token-for-the-owner"}).encode()
            if path == "me":
                return 200, json.dumps(self.me).encode()
            if path == "me/adaccounts":
                return 200, json.dumps({"data": [{"account_id": "1234567890", "name": "Main account", "currency": "INR"}, {"account_id": "555000111", "name": "Old account", "currency": "USD"}]}).encode()
            if path == "me/accounts":
                return 200, json.dumps({"data": [{"id": "987654321", "name": "Shakti Builders"}]}).encode()
            if path == "987654321":
                return 200, json.dumps({"access_token": "page-token"}).encode()
            return 200, json.dumps(self.status).encode()
        if method == "DELETE":
            return 200, b'{"success": true}'
        if method == "POST" and path.endswith("/adimages"):
            return 200, json.dumps({"images": {"photo.png": {"hash": "imghash123", "url": "https://cdn/x"}}}).encode()
        if method == "POST" and path.endswith(("/campaigns", "/adsets", "/adcreatives", "/ads", "/leadgen_forms", "/customaudiences")):
            self.counter += 1
            return 200, json.dumps({"id": "%s_%d" % (path.rsplit("/", 1)[1], self.counter)}).encode()
        return 200, b'{"success": true}'


def insight(day, spend, impressions, clicks, chats=0, leads=0, reach=None):
    actions = []
    if chats:
        actions.append({"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": str(chats)})
    if leads:
        actions.append({"action_type": "lead", "value": str(leads)})
    return {"date_start": day, "spend": str(spend), "impressions": str(impressions), "clicks": str(clicks), "reach": str(reach or impressions // 2),
            "outbound_clicks": [{"action_type": "outbound_click", "value": str(clicks // 2)}], "actions": actions}


@pytest.fixture
def world(migrated, tmp_path):
    vault = Vault({1: os.urandom(32)})
    tid, _ = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    meta = FakeMeta()
    graph = Graph(meta)
    media.configure(store=LocalStore(str(tmp_path / "media")))
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok", graph=graph, vault=vault)
    client = TestClient(app)

    class W:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "tok"})

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            c = cls.conn()
            try:
                with c.cursor() as cur:
                    out = fn(cur, tid, *a, **kw)
                c.commit()
                return out
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    W.db, W.tid, W.meta, W.vault, W.graph = migrated, tid, meta, vault, graph
    W.boss = W.post("/internal/inbox/ensure_agent", seat_key="boss", name="Boss").json()["agent"]
    yield W
    media.configure(store=None)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def connect(w, **kw):
    args = dict(ad_account_id="act_1234567890", page_id="987654321", token=TOKEN, name="Main", by_agent_id=w.boss["id"])
    args.update(kw)
    return ok(w.post("/internal/ads/connect", **args))["account_id"]


def make(w, account, **kw):
    args = dict(account_id=account, kind="website", name="Walls", daily_budget=50000, targeting=TARGETING, message="Boundary walls built to last", headline="Free quote",
                link="https://shakti.example/walls", by_agent_id=w.boss["id"])
    args.update(kw)
    return w.post("/internal/ads/create", **args)


def posts(w):
    return [c for c in w.meta.calls if c["method"] == "POST"]


# ── targeting ────────────────────────────────────────────────────────────────

def test_a_targeting_is_built_from_what_the_screen_collects():
    t = ads_build.clean_targeting({"geo_locations": {"countries": ["in"], "cities": [{"key": 2418, "radius": 25}], "regions": ["2000"]}, "age_min": 25, "age_max": 55, "genders": [2, 2],
                                   "interests": [{"id": 6003, "name": "Interior design"}], "behaviors": [{"id": "6004"}], "custom_audiences": [{"id": "23850"}],
                                   "excluded_custom_audiences": ["23851"], "publisher_platforms": ["facebook", "instagram"], "instagram_positions": ["stream", "reels"], "advantage_audience": False})
    assert t["geo_locations"] == {"countries": ["IN"], "cities": [{"key": "2418", "radius": 25, "distance_unit": "kilometer"}], "regions": [{"key": "2000"}]}
    assert t["age_min"] == 25 and t["genders"] == [2] and t["flexible_spec"] == [{"interests": [{"id": "6003", "name": "Interior design"}], "behaviors": [{"id": "6004", "name": ""}]}]
    assert t["custom_audiences"] == [{"id": "23850"}] and t["excluded_custom_audiences"] == [{"id": "23851"}] and t["instagram_positions"] == ["stream", "reels"]
    assert t["targeting_automation"] == {"advantage_audience": 0} and t["publisher_platforms"] == ["facebook", "instagram"]


@pytest.mark.parametrize("bad", [
    None, {}, {"geo_locations": {"countries": ["India"]}}, {"geo_locations": {"cities": ["Vadodara"]}}, {"geo_locations": {"cities": [{"key": "1", "radius": 5}]}},
    {"geo_locations": {"countries": ["IN"]}, "age_min": 12}, {"geo_locations": {"countries": ["IN"]}, "age_min": 40, "age_max": 30}, {"geo_locations": {"countries": ["IN"]}, "genders": [3]},
    {"geo_locations": {"countries": ["IN"]}, "interests": ["not an id"]}, {"geo_locations": {"countries": ["IN"]}, "interests": [{"id": str(i + 100)} for i in range(26)]},
    {"geo_locations": {"countries": ["IN"]}, "publisher_platforms": ["tiktok"]}, {"geo_locations": {"countries": ["IN"]}, "facebook_positions": ["billboard"]}])
def test_a_targeting_that_makes_no_sense_is_refused_in_words(bad):
    with pytest.raises(ads.AdsError) as why:
        ads_build.clean_targeting(bad)
    assert why.value.reason == "bad_targeting" and str(why.value)


def test_places_and_interests_are_searched_in_metas_own_directory(world):
    account = connect(world)
    geo = ok(world.post("/internal/ads/search", account_id=account, kind="geo", q="Vadodara"))["results"]
    assert geo == [{"key": "2418", "name": "Vadodara", "type": "city", "detail": "Gujarat, India", "country": "IN"}]
    interests = ok(world.post("/internal/ads/search", account_id=account, kind="interests", q="interior"))["results"]
    assert interests[0]["id"] == "6003" and interests[0]["size"] == [1000000, 2000000] and "Interests > Home" in interests[0]["detail"]
    ok(world.post("/internal/ads/search", account_id=account, kind="behaviors", q="small business"))
    last = world.meta.calls[-1]
    assert last["query"]["type"] == "adTargetingCategory" and last["query"]["class"] == "behaviors" and last["auth"] == "Bearer " + TOKEN


def test_a_search_needs_two_letters_and_a_known_kind(world):
    account = connect(world)
    assert world.post("/internal/ads/search", account_id=account, kind="geo", q="V").json()["error"] == "bad_search"
    assert world.post("/internal/ads/search", account_id=account, kind="stars", q="Vadodara").json()["error"] == "bad_search"


def test_how_many_people_a_targeting_reaches_is_estimated_before_the_ad_exists(world):
    account = connect(world)
    got = ok(world.post("/internal/ads/estimate", account_id=account, targeting=TARGETING, kind="website"))["estimate"]
    assert got == {"ready": True, "daily_low": 12000, "daily_high": 12000, "monthly_low": 300000, "monthly_high": 400000}
    q = world.meta.calls[-1]["query"]
    assert q["optimization_goal"] == "LINK_CLICKS" and json.loads(q["targeting_spec"]) == TARGETING
    assert not posts(world)


# ── the other kinds of ad ────────────────────────────────────────────────────

def test_a_website_ad_is_a_traffic_campaign_to_the_address_and_is_made_paused(world):
    account = connect(world)
    r = make(world, account, description="Free site visit", cta="GET_QUOTE", image_hash="imghash123")
    assert r.status_code == 200 and r.json()["ad"]["status"] == "PAUSED" and r.json()["ad"]["kind"] == "website"
    campaign, adset, creative, ad = (c["body"] for c in posts(world))
    assert campaign == {"name": "Walls", "objective": "OUTCOME_TRAFFIC", "status": "PAUSED", "special_ad_categories": []}
    assert adset["status"] == "PAUSED" and adset["destination_type"] == "WEBSITE" and adset["optimization_goal"] == "LINK_CLICKS" and adset["daily_budget"] == 50000
    link = creative["object_story_spec"]["link_data"]
    assert link["link"] == "https://shakti.example/walls" and link["call_to_action"] == {"type": "GET_QUOTE", "value": {"link": "https://shakti.example/walls"}}
    assert link["description"] == "Free site visit" and link["image_hash"] == "imghash123" and ad["status"] == "PAUSED"
    row = H.only(world.db, "select kind, status from ads")
    assert (row["kind"], row["status"]) == ("website", "PAUSED")


def test_a_website_ad_can_aim_for_landing_page_views_when_it_has_a_pixel(world):
    account = connect(world)
    make(world, account, pixel_id="1122334455", optimize_for="landing_page_views")
    adset = posts(world)[1]["body"]
    assert adset["optimization_goal"] == "LANDING_PAGE_VIEWS" and adset["promoted_object"] == {"pixel_id": "1122334455", "custom_event_type": "PAGE_VIEW"}
    world.meta.calls.clear()
    make(world, account, optimize_for="landing_page_views")                                       # without a pixel it cannot, and says nothing about it: clicks
    assert posts(world)[1]["body"]["optimization_goal"] == "LINK_CLICKS"


@pytest.mark.parametrize("bad,why", [({"link": "shakti.example"}, "bad_link"), ({"link": ""}, "bad_link"), ({"cta": "BUY_NOW"}, "bad_cta"), ({"pixel_id": "abc"}, "bad_pixel"),
                                     ({"name": " "}, "name_required"), ({"message": " "}, "message_required"), ({"daily_budget": 5}, "bad_budget"),
                                     ({"message": "x" * 2001}, "too_long"), ({"kind": "billboard"}, "bad_kind")])
def test_a_bad_website_ad_is_refused_before_anything_is_sent_to_meta(world, bad, why):
    account = connect(world)
    r = make(world, account, **bad)
    assert r.status_code == 400 and r.json()["error"] == why and not posts(world) and world.db.one("select count(*) from ads") == 0


def test_a_lead_form_ad_makes_the_form_on_the_page_first_and_points_the_ad_at_it(world):
    account = connect(world)
    r = make(world, account, kind="leadform", link=None, form={"questions": ["FULL_NAME", "PHONE", {"type": "CUSTOM", "label": "Which city is the project in?"}],
                                                                "privacy_url": "https://shakti.example/privacy", "thank_you_url": "https://shakti.example/thanks", "intro": "Tell us a little."})
    assert r.status_code == 200 and r.json()["ad"]["kind"] == "leadform"
    paths = [c["path"] for c in posts(world)]
    assert paths == ["987654321/leadgen_forms", "act_1234567890/campaigns", "act_1234567890/adsets", "act_1234567890/adcreatives", "act_1234567890/ads"]
    form, campaign, adset, creative, _ad = posts(world)
    assert form["auth"] == "Bearer page-token"                                                  # a form is made as the Page
    assert form["body"]["questions"][2] == {"type": "CUSTOM", "label": "Which city is the project in?", "key": "which_city_is_the_project_in"}
    assert form["body"]["privacy_policy"] == {"url": "https://shakti.example/privacy"} and form["body"]["follow_up_action_url"] == "https://shakti.example/thanks"
    assert campaign["body"]["objective"] == "OUTCOME_LEADS" and adset["body"]["optimization_goal"] == "LEAD_GENERATION" and adset["body"]["destination_type"] == "ON_AD"
    assert adset["body"]["promoted_object"] == {"page_id": "987654321"}
    link = creative["body"]["object_story_spec"]["link_data"]
    assert link["call_to_action"] == {"type": "SIGN_UP", "value": {"lead_gen_form_id": "leadgen_forms_1"}}
    assert H.only(world.db, "select meta_ids from ads")["meta_ids"]["form"] == "leadgen_forms_1"


def test_a_lead_form_ad_can_use_a_form_that_already_exists(world):
    account = connect(world)
    make(world, account, kind="leadform", link=None, form={"id": "99887766"})
    assert [c["path"] for c in posts(world)][0] == "act_1234567890/campaigns"
    assert posts(world)[2]["body"]["object_story_spec"]["link_data"]["call_to_action"]["value"] == {"lead_gen_form_id": "99887766"}


@pytest.mark.parametrize("form", [
    {"questions": ["FULL_NAME"], "privacy_url": "https://x.example/p"}, {"questions": ["FULL_NAME", "PHONE"]}, {"questions": ["FULL_NAME", "PHONE"], "privacy_url": "x.example"},
    {"questions": ["SHOE_SIZE", "PHONE"], "privacy_url": "https://x.example/p"}, {"questions": [], "privacy_url": "https://x.example/p"}, {"questions": [{"type": "CUSTOM"}, "PHONE"], "privacy_url": "https://x.example/p"},
    {"id": "abc"}])
def test_a_lead_form_that_could_not_work_is_refused(world, form):
    account = connect(world)
    r = make(world, account, kind="leadform", link=None, form=form)
    assert r.status_code == 400 and r.json()["error"] in ("bad_form", "bad_link") and not posts(world)


def test_a_total_budget_needs_an_end_date_and_replaces_the_daily_one(world):
    account = connect(world)
    assert make(world, account, daily_budget=None, lifetime_budget=500000).json()["error"] == "bad_budget"
    r = make(world, account, daily_budget=None, lifetime_budget=500000, start_time="2026-11-01T00:00:00+0530", end_time="2026-11-08T00:00:00+0530")
    assert r.status_code == 200
    adset = posts(world)[1]["body"]
    assert adset["lifetime_budget"] == 500000 and "daily_budget" not in adset and adset["end_time"] == "2026-11-08T00:00:00+0530"
    world.meta.calls.clear()
    make(world, account, kind="ctwa", whatsapp_number="919726063133", daily_budget=None, lifetime_budget=500000, end_time="2026-11-08T00:00:00+0530")
    assert posts(world)[1]["body"]["lifetime_budget"] == 500000 and "daily_budget" not in posts(world)[1]["body"] and posts(world)[1]["body"]["destination_type"] == "WHATSAPP"


def test_a_click_to_whatsapp_ad_still_goes_through_the_old_sequence_with_the_new_targeting(world):
    account = connect(world)
    r = make(world, account, kind="ctwa", whatsapp_number="+91 97260 63133", targeting={**TARGETING, "genders": [2], "publisher_platforms": ["instagram"]}, message="Chat with us")
    assert r.status_code == 200 and r.json()["ad"]["status"] == "PAUSED"
    adset = posts(world)[1]["body"]
    assert adset["targeting"]["genders"] == [2] and adset["targeting"]["publisher_platforms"] == ["instagram"] and adset["optimization_goal"] == "CONVERSATIONS"
    assert H.only(world.db, "select kind from ads")["kind"] == "ctwa"


def test_a_failure_part_way_takes_back_what_was_made_for_any_kind(world):
    account = connect(world)
    world.meta.fail_on = "/adcreatives"
    r = make(world, account)
    assert r.status_code == 409 and r.json()["error"] == "meta_refused" and "Budget too low" in r.json()["message"]
    assert [c["path"] for c in world.meta.calls if c["method"] == "DELETE"] == ["adsets_2", "campaigns_1"]
    row = H.only(world.db, "select status, kind from ads")
    assert (row["status"], row["kind"]) == ("FAILED", "website")


def test_only_a_manager_can_make_an_ad(world):
    account = connect(world)
    staff = world.post("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    assert make(world, account, by_agent_id=staff["id"]).json()["error"] == "not_allowed" and not posts(world)


# ── pictures and words ───────────────────────────────────────────────────────

def library_picture(world, name="photo.png", mime="image/png", data=b"\x89PNG-bytes"):
    return world.do(lambda cur, tid: media.upload(cur, tid, filename=name, mime=mime, data=data))


def test_a_picture_from_the_library_is_sent_to_meta_and_comes_back_as_a_hash(world):
    account = connect(world)
    asset = library_picture(world)
    got = ok(world.post("/internal/ads/upload_image", account_id=account, asset_id=str(asset["id"]), by_agent_id=world.boss["id"]))
    assert (got["hash"], got["filename"]) == ("imghash123", "photo.png")
    call = world.meta.calls[-1]
    assert call["path"] == "act_1234567890/adimages" and b"\x89PNG-bytes" in call["raw"] and call["auth"] == "Bearer " + TOKEN


def test_only_a_picture_not_too_big_can_be_sent(world):
    account = connect(world)
    doc = library_picture(world, "brochure.pdf", "application/pdf", b"%PDF-1.4")
    assert world.post("/internal/ads/upload_image", account_id=account, asset_id=str(doc["id"]), by_agent_id=world.boss["id"]).json()["error"] == "bad_image"
    assert world.post("/internal/ads/upload_image", account_id=account, asset_id="11111111-1111-4111-8111-111111111111", by_agent_id=world.boss["id"]).json()["error"] == "bad_image"


def test_three_ways_of_writing_the_ad_come_from_the_model_and_are_trimmed(world):
    account = connect(world)

    def transport(method, url, data, headers):
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps({"variants": [
            {"message": "Boundary walls that last. Free site visit.", "headline": "Built to last", "description": "Vadodara"},
            {"message": "Planning a boundary wall? We measure for free.", "headline": "Free measurement"}, {"message": ""},
            {"message": "Your wall, our promise " + "x" * 3000, "headline": "H" * 400}, {"message": "A fifth one"}]})}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode()
    ai_agent.configure(LLM(api_key="k", transport=transport))
    try:
        got = ok(world.post("/internal/ads/copy", goal="site visits", offer="Boundary walls in Vadodara", audience="homeowners", language="English"))["variants"]
    finally:
        ai_agent.configure(None)
    assert len(got) == 3 and got[0]["headline"] == "Built to last" and len(got[2]["message"]) <= 2000 and len(got[2]["headline"]) <= 255
    assert world.db.one("select count(*) from ai_usage where kind = 'aux'") == 1
    assert world.post("/internal/ads/copy", goal="x", offer="y").json()["error"] == "ai_not_configured"


# ── people lists ─────────────────────────────────────────────────────────────

def people(world, n, opted=True, **kw):
    c = world.conn()
    with c.cursor() as cur:
        for i in range(n):
            cur.execute("insert into contacts (tenant_id, name, phone, tags, opted_in, opt_out, blocked) values (%s, %s, %s, %s, %s, %s, %s)",
                        (world.tid, "P%d" % i, "9198%06d" % (kw.get("start", 0) + i), ["vip"], opted, kw.get("opt_out", False), kw.get("blocked", False)))
    c.commit()
    c.close()


def segment(world, name="VIPs"):
    return world.db.one("insert into segments (tenant_id, name, filter) values (%s, %s, %s) returning id", (world.tid, name, json.dumps({"tags": ["vip"]})))


def test_a_people_list_is_made_from_a_segment_with_hashed_numbers_of_people_who_opted_in(world):
    account = connect(world)
    people(world, 130)
    people(world, 5, opted=False, start=1000)
    people(world, 3, opt_out=True, start=2000)
    seg = segment(world)
    got = ok(world.post("/internal/audiences/from_segment", account_id=account, segment_id=str(seg), by_agent_id=world.boss["id"]))
    assert got["sent"] == 130 and got["name"] == "Prism · VIPs"
    made, upload = posts(world)[-2:]
    assert made["path"] == "act_1234567890/customaudiences" and made["body"]["subtype"] == "CUSTOM" and made["body"]["customer_file_source"] == "USER_PROVIDED_ONLY"
    assert upload["path"] == "customaudiences_1/users" and upload["body"]["payload"]["schema"] == ["PHONE_SHA256"]
    sent = [r[0] for r in upload["body"]["payload"]["data"]]
    assert len(sent) == 130 and all(len(h) == 64 for h in sent) and "919800000000" not in json.dumps(upload["body"])
    assert ads_targeting.hash_phone("+91 98000 00000") == ads_targeting.hash_phone("919800000000") == sent[0] or sent[0] in {ads_targeting.hash_phone("9198%06d" % i) for i in range(130)}
    row = H.only(world.db, "select kind, size, source from ad_audiences")
    assert row["kind"] == "segment" and row["size"] == 130 and row["source"]["sent"] == 130


def test_too_small_a_segment_is_refused_with_the_count(world):
    account = connect(world)
    people(world, 20)
    r = world.post("/internal/audiences/from_segment", account_id=account, segment_id=str(segment(world)), by_agent_id=world.boss["id"])
    assert r.status_code == 400 and r.json()["error"] == "too_few" and "has 20" in r.json()["message"] and not posts(world)
    assert world.post("/internal/audiences/from_segment", account_id=account, segment_id="11111111-1111-4111-8111-111111111111", by_agent_id=world.boss["id"]).json()["error"] == "unknown_segment"


def test_a_look_alike_is_made_from_a_list_in_one_country_at_a_chosen_closeness(world):
    account = connect(world)
    people(world, 150)
    first = ok(world.post("/internal/audiences/from_segment", account_id=account, segment_id=str(segment(world)), by_agent_id=world.boss["id"]))
    got = ok(world.post("/internal/audiences/lookalike", account_id=account, audience_id=first["id"], country="in", ratio=0.02, by_agent_id=world.boss["id"]))
    body = posts(world)[-1]["body"]
    assert body["subtype"] == "LOOKALIKE" and body["origin_audience_id"] == first["meta_id"] and body["lookalike_spec"] == {"type": "similarity", "country": "IN", "ratio": 0.02}
    assert "look-alike 2% IN" in got["name"]
    for bad, why in (({"country": "India"}, "bad_country"), ({"ratio": 0.5}, "bad_ratio"), ({"ratio": 0.001}, "bad_ratio")):
        assert world.post("/internal/audiences/lookalike", account_id=account, audience_id=first["id"], by_agent_id=world.boss["id"], **{"country": "IN", **bad}).json()["error"] == why
    assert world.post("/internal/audiences/lookalike", account_id=account, audience_id="11111111-1111-4111-8111-111111111111", country="IN", by_agent_id=world.boss["id"]).json()["error"] == "unknown_audience"


def test_lists_are_listed_and_can_be_removed_at_meta_too(world):
    account = connect(world)
    people(world, 150)
    first = ok(world.post("/internal/audiences/from_segment", account_id=account, segment_id=str(segment(world)), by_agent_id=world.boss["id"]))
    assert [a["name"] for a in ok(world.post("/internal/audiences/list"))["audiences"]] == ["Prism · VIPs"]
    assert ok(world.post("/internal/audiences/delete", audience_id=first["id"], by_agent_id=world.boss["id"]))["deleted"] is True
    assert [c["path"] for c in world.meta.calls if c["method"] == "DELETE"] == ["customaudiences_1"] and ok(world.post("/internal/audiences/list"))["audiences"] == []


def test_only_a_manager_sends_people_to_meta(world):
    account = connect(world)
    people(world, 150)
    staff = world.post("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    assert world.post("/internal/audiences/from_segment", account_id=account, segment_id=str(segment(world)), by_agent_id=staff["id"]).json()["error"] == "not_allowed" and not posts(world)


# ── where an ad stands ───────────────────────────────────────────────────────

def made_ad(world, **kw):
    account = connect(world)
    return account, make(world, account, **kw).json()["ad"]["id"]


def test_the_review_state_is_asked_of_meta_and_kept_with_the_reason_it_was_turned_down(world):
    _account, ad = made_ad(world)
    world.meta.status = {"effective_status": "DISAPPROVED", "ad_review_feedback": {"global": {"Personal attributes": "Your ad suggests you know a personal trait"}},
                         "issues_info": [{"error_summary": "Ad rejected", "error_message": "details"}]}
    got = ok(world.post("/internal/ads/status", ad_id=ad))
    assert got["state"] == "rejected" and got["words"] == "Turned down by Meta" and "Personal attributes: Your ad suggests" in got["reasons"][0] and "Ad rejected" in got["reasons"]
    row = H.only(world.db, "select meta_status, review, synced_at from ads")
    assert row["meta_status"] == "DISAPPROVED" and row["review"]["reasons"] and row["synced_at"]
    listed = ok(world.post("/internal/ads/list"))["ads"][0]
    assert listed["kind"] == "website" and listed["meta_status"] == "DISAPPROVED" and listed["budget"]["daily"] == 50000


@pytest.mark.parametrize("meta,state", [("ACTIVE", "running"), ("PENDING_REVIEW", "review"), ("WITH_ISSUES", "issues"), ("ADSET_PAUSED", "paused"), ("ARCHIVED", "archived"), ("SOMETHING_NEW", "unknown")])
def test_each_state_meta_reports_is_put_in_words(world, meta, state):
    _account, ad = made_ad(world)
    world.meta.status = {"effective_status": meta}
    got = ok(world.post("/internal/ads/status", ad_id=ad))
    assert got["state"] == state and got["words"]


def test_an_ad_that_meta_says_is_running_is_marked_running_here_too(world):
    _account, ad = made_ad(world)
    world.meta.status = {"effective_status": "ACTIVE"}
    ok(world.post("/internal/ads/status", ad_id=ad))
    assert world.db.one("select status from ads") == "ACTIVE"


def test_the_numbers_come_day_by_day_with_the_rates_worked_out(world):
    _account, ad = made_ad(world, kind="ctwa", whatsapp_number="919726063133")
    world.meta.rows = [insight("2026-10-02", 200, 4000, 80, chats=8), insight("2026-10-01", 100, 2000, 40, chats=2)]
    got = ok(world.post("/internal/ads/report", ad_id=ad, since="2026-10-01", until="2026-10-02"))
    assert [d["day"] for d in got["days"]] == ["2026-10-01", "2026-10-02"] and got["days"][0]["ctr"] == 2.0 and got["days"][0]["cpc"] == 2.5 and got["days"][1]["cost_per_conversation"] == 25.0
    t = got["totals"]
    assert (t["impressions"], t["clicks"], t["spend"], t["conversations"], t["outbound_clicks"]) == (6000, 120, 300.0, 10, 60) and t["ctr"] == 2.0 and t["cpc"] == 2.5 and t["cost_per_conversation"] == 30.0
    q = world.meta.calls[-1]["query"]
    assert q["time_increment"] == "1" and json.loads(q["time_range"]) == {"since": "2026-10-01", "until": "2026-10-02"}


def test_a_range_must_be_dates_oldest_first_and_within_a_year(world):
    _account, ad = made_ad(world)
    for bad in ({"since": "yesterday"}, {"since": "2026-10-05", "until": "2026-10-01"}, {"since": "2024-01-01", "until": "2026-01-01"}):
        assert world.post("/internal/ads/report", ad_id=ad, **bad).json()["error"] == "bad_range"
    world.meta.rows = []
    assert ok(world.post("/internal/ads/report", ad_id=ad))["totals"]["spend"] == 0


# ── suggestions ──────────────────────────────────────────────────────────────

def running_ad(world, account, name, **kw):
    ad = make(world, account, name=name, kind="ctwa", whatsapp_number="919726063133", **kw).json()["ad"]["id"]
    world.db.q("update ads set status = 'ACTIVE' where id = %s", (ad,))
    return ad


def feed(world, per_ad):
    """The next report asked for returns these rows (by ad path)."""
    original = world.meta.__call__

    def routed(method, url, data, headers):
        if "/insights" in url:
            for path, rows in per_ad.items():
                if path in url:
                    world.meta.calls.append({"method": method, "path": url, "body": None, "query": {}, "auth": None, "raw": None})
                    return 200, json.dumps({"data": rows}).encode()
        return original(method, url, data, headers)
    world.graph.transport = routed


def test_an_ad_that_spent_money_and_started_no_chats_is_suggested_for_pausing_and_nothing_happens_until_approved(world):
    account = connect(world)
    bad = running_ad(world, account, "Wasteful")
    ad_path = world.db.one("select meta_ids->>'ad' from ads where id = %s", (bad,))
    feed(world, {ad_path + "/insights": [insight("2026-10-02", 2000, 6000, 90)]})
    got = ok(world.post("/internal/ads/suggest", by_agent_id=world.boss["id"]))["suggestions"]
    assert len(got) == 1 and got[0]["kind"] == "pause" and "no chats" in got[0]["reason"] and got[0]["ad_name"] == "Wasteful"
    assert world.db.one("select status from ads") == "ACTIVE" and not [c for c in world.meta.calls if c["method"] == "POST" and c["body"] == {"status": "PAUSED"}]
    again = ok(world.post("/internal/ads/suggest", by_agent_id=world.boss["id"]))["suggestions"]
    assert len(again) == 1                                                                         # the same one is not made twice while it is open
    done = ok(world.post("/internal/ads/decide", suggestion_id=got[0]["id"], apply=True, by_agent_id=world.boss["id"]))
    assert done["state"] == "applied" and world.db.one("select status from ads") == "PAUSED"
    assert ok(world.post("/internal/ads/suggestions", state="applied"))["suggestions"][0]["state"] == "applied"
    assert world.post("/internal/ads/decide", suggestion_id=got[0]["id"], apply=True, by_agent_id=world.boss["id"]).json()["error"] == "wrong_state"


def test_a_suggestion_can_be_turned_down_and_then_nothing_changes(world):
    account = connect(world)
    bad = running_ad(world, account, "Wasteful")
    ad_path = world.db.one("select meta_ids->>'ad' from ads where id = %s", (bad,))
    feed(world, {ad_path + "/insights": [insight("2026-10-02", 2000, 6000, 90)]})
    sid = ok(world.post("/internal/ads/suggest", by_agent_id=world.boss["id"]))["suggestions"][0]["id"]
    assert ok(world.post("/internal/ads/decide", suggestion_id=sid, apply=False, by_agent_id=world.boss["id"]))["state"] == "dismissed"
    assert world.db.one("select status from ads") == "ACTIVE"
    assert ok(world.post("/internal/ads/suggestions"))["suggestions"] == []


def test_an_ad_that_costs_far_more_than_the_others_is_suggested_for_pausing_and_a_cheap_one_for_more_money(world):
    account = connect(world)
    ids = {n: running_ad(world, account, n) for n in ("A", "B", "C", "D")}
    path = {n: world.db.one("select meta_ids->>'ad' from ads where id = %s", (i,)) for n, i in ids.items()}
    feed(world, {path["A"] + "/insights": [insight("2026-10-02", 3500, 90000, 1800, chats=50)],       # 70 a chat
                 path["B"] + "/insights": [insight("2026-10-02", 3000, 90000, 1800, chats=60)],       # 50
                 path["C"] + "/insights": [insight("2026-10-02", 7500, 90000, 1800, chats=50)],       # 150: more than twice the middle
                 path["D"] + "/insights": [insight("2026-10-02", 2600, 90000, 1800, chats=104)]})     # 25: under half the middle, spending its 500-a-day budget
    got = ok(world.post("/internal/ads/suggest", by_agent_id=world.boss["id"]))["suggestions"]
    kinds = {s["ad_name"]: s["kind"] for s in got}
    assert kinds == {"C": "pause", "D": "budget_up"}
    up = next(s for s in got if s["kind"] == "budget_up")
    assert up["params"] == {"from": 50000, "to": 60000}
    ok(world.post("/internal/ads/decide", suggestion_id=up["id"], apply=True, by_agent_id=world.boss["id"]))
    assert [c for c in world.meta.calls if c["method"] == "POST" and c["body"] == {"daily_budget": 60000}]
    assert world.db.one("select (spec->>'daily_budget')::int from ads where id = %s", (ids["D"],)) == 60000


def test_an_ad_few_people_click_is_suggested_a_new_picture_and_approving_it_only_ticks_it_off(world):
    account = connect(world)
    ad = running_ad(world, account, "Dull")
    ad_path = world.db.one("select meta_ids->>'ad' from ads where id = %s", (ad,))
    feed(world, {ad_path + "/insights": [insight("2026-10-02", 100, 5000, 10, chats=2)]})
    got = ok(world.post("/internal/ads/suggest", by_agent_id=world.boss["id"]))["suggestions"]
    assert [s["kind"] for s in got] == ["new_creative"] and "0.2%" in got[0]["reason"]
    world.meta.calls.clear()
    assert ok(world.post("/internal/ads/decide", suggestion_id=got[0]["id"], apply=True, by_agent_id=world.boss["id"]))["state"] == "applied"
    assert not [c for c in world.meta.calls if c["method"] == "POST" and c["body"] and c["body"].get("status")]


def test_only_a_manager_acts_on_suggestions(world):
    staff = world.post("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    assert world.post("/internal/ads/suggest", by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    assert world.post("/internal/ads/decide", suggestion_id="11111111-1111-4111-8111-111111111111", by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    assert world.post("/internal/ads/decide", suggestion_id="11111111-1111-4111-8111-111111111111", by_agent_id=world.boss["id"]).json()["error"] == "unknown_suggestion"


# ── signing in with Facebook ─────────────────────────────────────────────────

def test_a_sign_in_is_started_with_a_state_that_names_the_workspace_and_expires(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "777888")
    got = ok(world.post("/internal/ads/connect_start", redirect_uri="https://licence.example/whatsapp/ads/callback", by_agent_id=world.boss["id"]))
    q = parse_qs(urlsplit(got["url"]).query)
    assert q["client_id"] == ["777888"] and q["state"] == [got["state"]] and q["redirect_uri"] == ["https://licence.example/whatsapp/ads/callback"]
    assert "ads_management" in q["scope"][0] and ads_connect.tenant_of(got["state"]) == str(world.tid)
    assert ads_connect.tenant_of("garbage") is None and ads_connect.tenant_of(str(world.tid)) is None
    assert ok(world.post("/internal/ads/connect_status", state=got["state"]))["status"] == "waiting"
    world.db.q("update ad_connect_states set expires_at = now() - interval '1 minute'")
    assert world.post("/internal/ads/connect_status", state=got["state"]).json()["error"] == "expired"


def test_a_sign_in_that_is_not_set_up_or_not_secure_says_so(world, monkeypatch):
    monkeypatch.delenv("META_APP_ID", raising=False)
    assert world.post("/internal/ads/connect_start", redirect_uri="https://x.example/cb", by_agent_id=world.boss["id"]).json()["error"] == "not_set_up"
    monkeypatch.setenv("META_APP_ID", "777888")
    assert world.post("/internal/ads/connect_start", redirect_uri="http://x.example/cb", by_agent_id=world.boss["id"]).json()["error"] == "not_set_up"


def test_when_meta_sends_the_person_back_the_choices_are_kept_and_the_token_is_encrypted(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "777888")
    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    state = ok(world.post("/internal/ads/connect_start", redirect_uri="https://licence.example/cb", by_agent_id=world.boss["id"]))["state"]
    assert ok(world.post("/internal/ads/connect_finish", state=state, code="the-code"))["status"] == "ready"
    exchange = [c for c in world.meta.calls if c["path"] == "oauth/access_token"]
    assert exchange[0]["query"]["code"] == "the-code" and exchange[0]["query"]["client_secret"] == "s3cret" and exchange[1]["query"]["grant_type"] == "fb_exchange_token"
    got = ok(world.post("/internal/ads/connect_status", state=state))
    assert got["status"] == "ready" and got["options"]["user"] == "Asha Patel" and [a["id"] for a in got["options"]["accounts"]] == ["1234567890", "555000111"]
    assert got["options"]["pages"] == [{"id": "987654321", "name": "Shakti Builders"}] and "long-lived" not in json.dumps(got)
    stored = bytes(world.db.one("select token_enc from ad_connect_states"))
    assert b"long-lived-token" not in stored
    assert world.post("/internal/ads/connect_finish", state=state, code="again").json()["error"] == "wrong_state"


def test_choosing_an_account_and_a_page_connects_it_just_like_a_pasted_token(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "777888")
    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    state = ok(world.post("/internal/ads/connect_start", redirect_uri="https://licence.example/cb", by_agent_id=world.boss["id"]))["state"]
    ok(world.post("/internal/ads/connect_finish", state=state, code="c"))
    bad = world.post("/internal/ads/connect_choose", state=state, ad_account_id="999", page_id="987654321", by_agent_id=world.boss["id"])
    assert bad.status_code == 400 and bad.json()["error"] == "bad_choice"
    account = ok(world.post("/internal/ads/connect_choose", state=state, ad_account_id="act_1234567890", page_id="987654321", by_agent_id=world.boss["id"]))["account_id"]
    row = H.only(world.db, "select ad_account_id, page_id, name, connected_via, currency from ad_accounts")
    assert (row["ad_account_id"], row["page_id"], row["name"], row["connected_via"], row["currency"]) == ("1234567890", "987654321", "Main account", "facebook", "INR")
    assert world.vault.decrypt(bytes(world.db.one("select token_enc from ad_accounts")), context="act_1234567890") == "long-lived-token-for-the-owner"
    assert world.db.one("select token_enc from ad_connect_states") is None and world.db.one("select status from ad_connect_states") == "done"
    assert account and ok(world.post("/internal/ads/accounts"))["accounts"][0]["connected_via"] == "facebook"
    assert make(world, account).status_code == 200                                                   # and it makes ads


def test_a_sign_in_meta_refuses_is_recorded_with_its_reason(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "777888")
    monkeypatch.setenv("META_APP_SECRET", "s3cret")
    state = ok(world.post("/internal/ads/connect_start", redirect_uri="https://licence.example/cb", by_agent_id=world.boss["id"]))["state"]
    original = world.graph.transport

    def refusing(method, url, data, headers):
        if "oauth/access_token" in url:
            return 400, json.dumps({"error": {"message": "Error validating verification code", "code": 100}}).encode()
        return original(method, url, data, headers)
    world.graph.transport = refusing
    got = ok(world.post("/internal/ads/connect_finish", state=state, code="stale"))
    assert got["status"] == "failed" and "verification code" in got["error"]
    status = ok(world.post("/internal/ads/connect_status", state=state))
    assert (status["status"], status["error"], status["options"]) == ("failed", got["error"], None)
    assert world.post("/internal/ads/connect_status", state="x" * 60).json()["error"] == "unknown_state"


def test_another_workspace_cannot_read_or_finish_a_sign_in(world, migrated, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "777888")
    state = ok(world.post("/internal/ads/connect_start", redirect_uri="https://licence.example/cb", by_agent_id=world.boss["id"]))["state"]
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    r = world.post("/internal/ads/connect_status", tenant_id=str(other), state=state)
    assert r.status_code in (400, 404, 409) and r.json()["error"] == "unknown_state"
