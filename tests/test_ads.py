"""Click-to-WhatsApp ads: always paused, activation is explicit, failures leave nothing behind.

Against a fake Graph server: this proves the sequence, the safety rules and the bookkeeping, NOT that Meta
accepts the field names (ads.py says so at the top).
"""
import json
import os

import helpers as H
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import ads
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.vault import Vault

TOKEN = "EAAB-ads-token-that-is-long-enough"
TARGETING = {"geo_locations": {"countries": ["IN"]}, "age_min": 25}


class FakeMarketing:
    def __init__(self):
        self.calls, self.fail_on, self.counter = [], None, 0

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        path = url.split("/v25.0/", 1)[1].split("?")[0]
        self.calls.append({"method": method, "path": path, "body": body, "auth": headers.get("Authorization")})
        if self.fail_on and path.endswith(self.fail_on) and method == "POST":
            return 400, json.dumps({"error": {"message": "Invalid parameter", "code": 100, "error_user_msg": "Budget too low"}}).encode()
        if method == "GET" and path.endswith("/insights"):
            return 200, json.dumps({"data": [{"impressions": "1200", "reach": "900", "clicks": "40", "spend": "250.50", "actions": [
                {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "10"},
                {"action_type": "link_click", "value": "40"}]}]}).encode()
        if method == "DELETE":
            return 200, b'{"success": true}'
        if method == "POST" and path.endswith(("/campaigns", "/adsets", "/adcreatives", "/ads")):
            self.counter += 1
            return 200, json.dumps({"id": "%s_%d" % (path.rsplit("/", 1)[1], self.counter)}).encode()
        return 200, b'{"success": true}'


@pytest.fixture
def world(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, _ = H.onboard(migrated)
    meta = FakeMarketing()
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok",
                     graph=Graph(meta), vault=vault)
    client = TestClient(app)

    class W:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "tok"})

    W.db, W.tid, W.meta, W.vault = migrated, tid, meta, vault
    W.boss = W.post("/internal/inbox/ensure_agent", seat_key="boss", name="Boss").json()["agent"]
    return W


def connect(w, **kw):
    args = dict(ad_account_id="act_1234567890", page_id="987654321", token=TOKEN, name="Main", by_agent_id=w.boss["id"])
    args.update(kw)
    return w.post("/internal/ads/connect", **args)


def create(w, account, **kw):
    args = dict(account_id=account, name="Diwali", whatsapp_number="+91 97260 63133", daily_budget=50000, targeting=TARGETING,
                message="Chat with us for a free site visit", headline="Free site visit", image_hash="abc123",
                welcome_text="Hi! How can we help?", ice_breakers=["Get a quote", "Book a visit"], by_agent_id=w.boss["id"])
    args.update(kw)
    return w.post("/internal/ads/create", **args)


# ── connecting ───────────────────────────────────────────────────────────────

def test_an_account_is_stored_with_its_token_encrypted(world):
    r = connect(world)
    assert r.status_code == 200
    row = H.only(world.db, "select * from ad_accounts")
    assert (row["ad_account_id"], row["page_id"], row["name"]) == ("1234567890", "987654321", "Main")
    assert TOKEN.encode() not in bytes(row["token_enc"])
    assert world.vault.decrypt(bytes(row["token_enc"]), context="act_1234567890") == TOKEN
    assert connect(world).status_code == 200 and world.db.one("select count(*) from ad_accounts") == 1          # re-connect updates


def test_connected_accounts_are_listed_without_their_tokens(world):
    assert world.post("/internal/ads/accounts").json()["accounts"] == []
    connect(world)
    accounts = world.post("/internal/ads/accounts").json()["accounts"]
    assert len(accounts) == 1 and accounts[0]["ad_account_id"] == "1234567890" and accounts[0]["name"] == "Main"
    assert "token" not in json.dumps(accounts) and TOKEN not in json.dumps(accounts)


@pytest.mark.parametrize("bad,why", [({"ad_account_id": "act_x"}, "bad_account"), ({"page_id": "abc"}, "bad_page"), ({"token": "short"}, "bad_token")])
def test_bad_account_details_are_refused(world, bad, why):
    r = connect(world, **bad)
    assert r.status_code == 400 and r.json()["error"] == why


def test_only_a_manager_connects_an_account(world):
    staff = world.post("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    assert connect(world, by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    assert world.db.one("select count(*) from ad_accounts") == 0


# ── creating ─────────────────────────────────────────────────────────────────

def test_an_ad_is_built_in_four_steps_and_every_one_is_paused(world):
    account = connect(world).json()["account_id"]
    r = create(world, account)
    assert r.status_code == 200 and r.json()["ad"]["status"] == "PAUSED"
    posts = [c for c in world.meta.calls if c["method"] == "POST"]
    assert [c["path"] for c in posts] == ["act_1234567890/campaigns", "act_1234567890/adsets", "act_1234567890/adcreatives", "act_1234567890/ads"]
    assert {c["auth"] for c in posts} == {"Bearer " + TOKEN}
    campaign, adset, creative, ad = (c["body"] for c in posts)
    assert campaign == {"name": "Diwali", "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED", "special_ad_categories": []}
    assert adset["status"] == "PAUSED" and adset["destination_type"] == "WHATSAPP" and adset["optimization_goal"] == "CONVERSATIONS"
    assert adset["promoted_object"] == {"page_id": "987654321", "whatsapp_phone_number": "919726063133"}
    assert adset["campaign_id"] == "campaigns_1" and adset["daily_budget"] == 50000 and adset["targeting"] == TARGETING
    link = creative["object_story_spec"]["link_data"]
    assert link["call_to_action"] == {"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP"}}
    assert link["message"] == "Chat with us for a free site visit" and link["name"] == "Free site visit" and link["image_hash"] == "abc123"
    welcome = json.loads(link["page_welcome_message"])
    assert [b["title"] for b in welcome["ice_breakers"]] == ["Get a quote", "Book a visit"]
    assert ad["status"] == "PAUSED" and ad["adset_id"] == "adsets_2" and ad["creative"] == {"creative_id": "adcreatives_3"}
    row = H.only(world.db, "select status, meta_ids from ads")
    assert row["status"] == "PAUSED" and row["meta_ids"] == {"campaign": "campaigns_1", "adset": "adsets_2", "creative": "adcreatives_3", "ad": "ads_4"}


@pytest.mark.parametrize("bad,why", [({"name": " "}, "name_required"), ({"whatsapp_number": "123"}, "bad_number"), ({"daily_budget": 5}, "bad_budget"),
                                     ({"daily_budget": "500"}, "bad_budget"), ({"targeting": {}}, "bad_targeting"), ({"targeting": {"geo_locations": {}}}, "bad_targeting"),
                                     ({"message": " "}, "message_required"), ({"ice_breakers": list("abcde")}, "bad_ice_breakers"),
                                     ({"ice_breakers": ["x" * 81]}, "bad_ice_breakers")])
def test_bad_ad_requests_are_refused_before_anything_is_sent_to_meta(world, bad, why):
    account = connect(world).json()["account_id"]
    r = create(world, account, **bad)
    assert r.status_code == 400 and r.json()["error"] == why
    assert not [c for c in world.meta.calls if c["method"] == "POST"] and world.db.one("select count(*) from ads") == 0


def test_a_failure_part_way_deletes_what_was_made_and_records_the_attempt(world):
    account = connect(world).json()["account_id"]
    world.meta.fail_on = "/adcreatives"
    r = create(world, account)
    assert r.status_code == 409 and r.json()["error"] == "meta_refused" and "Budget too low" in r.json()["message"]
    deleted = [c["path"] for c in world.meta.calls if c["method"] == "DELETE"]
    assert deleted == ["adsets_2", "campaigns_1"]                                           # nothing left half-built (the ad was never made)
    row = H.only(world.db, "select status, last_error from ads")
    assert row["status"] == "FAILED" and row["last_error"]["code"] == 100


def test_an_unknown_account_and_a_foreign_account_are_refused(world):
    assert create(world, "11111111-1111-4111-8111-111111111111").json()["error"] == "unknown_account"
    other = H.tenant(world.db, "shakti")
    theirs = world.db.one("insert into ad_accounts (tenant_id, ad_account_id, page_id, token_enc) values (%s,'55555','66666',%s) returning id",
                          (other, psycopg2.Binary(b"x")))
    assert create(world, str(theirs)).json()["error"] == "unknown_account"


# ── turning on and off ───────────────────────────────────────────────────────

def test_an_ad_is_only_turned_on_when_told_to_with_a_confirmation(world):
    account = connect(world).json()["account_id"]
    ad = create(world, account).json()["ad"]["id"]
    world.meta.calls.clear()
    r = world.post("/internal/ads/activate", ad_id=ad, by_agent_id=world.boss["id"])
    assert r.status_code == 409 and r.json()["error"] == "confirm_required" and not world.meta.calls
    assert world.db.one("select status from ads") == "PAUSED"
    r = world.post("/internal/ads/activate", ad_id=ad, confirm=True, by_agent_id=world.boss["id"])
    assert r.json()["status"] == "ACTIVE"
    assert [(c["path"], c["body"]) for c in world.meta.calls] == [("campaigns_1", {"status": "ACTIVE"}), ("adsets_2", {"status": "ACTIVE"}), ("ads_4", {"status": "ACTIVE"})]
    assert world.post("/internal/ads/activate", ad_id=ad, confirm=True, by_agent_id=world.boss["id"]).json()["error"] == "wrong_state"
    assert world.post("/internal/ads/pause", ad_id=ad, by_agent_id=world.boss["id"]).json()["status"] == "PAUSED"
    assert world.db.one("select status from ads") == "PAUSED"


def test_only_a_manager_can_create_or_turn_on_an_ad_and_confirm_must_be_true_not_just_present(world):
    account = connect(world).json()["account_id"]
    staff = world.post("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    assert create(world, account, by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    ad = create(world, account).json()["ad"]["id"]
    assert world.post("/internal/ads/activate", ad_id=ad, confirm=True, by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    for sneaky in ("true", 1, "yes", None):
        assert world.post("/internal/ads/activate", ad_id=ad, confirm=sneaky, by_agent_id=world.boss["id"]).json()["error"] == "confirm_required"


def test_insights_report_conversations_and_the_cost_of_each(world):
    account = connect(world).json()["account_id"]
    ad = create(world, account).json()["ad"]["id"]
    got = world.post("/internal/ads/insights", ad_id=ad).json()["insights"]
    assert got == {"impressions": 1200, "reach": 900, "clicks": 40, "spend": 250.5, "conversations_started": 10, "cost_per_conversation": 25.05}
    assert [a["name"] for a in world.post("/internal/ads/list").json()["ads"]] == ["Diwali"]
