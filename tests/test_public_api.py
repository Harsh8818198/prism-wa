"""The public API: keys, scopes, plan limits, rate limits, sending with consent, contacts, campaigns."""
import json
import os
import time

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_send import FakeGraph, TOKEN

from prism_wa import api_keys, plans, send
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.public_api import RateLimiter, mount
from prism_wa.vault import Vault

BODY = {"type": "BODY", "text": "Hello {{1}}, your order {{2}} has shipped and will reach you tomorrow morning.",
        "example": {"body_text": [["Asha", "A-1"]]}}
ALL = sorted(api_keys.SCOPES)


@pytest.fixture
def api(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s, tier = 'TIER_10K'", (psycopg2.Binary(blob), ver))
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok",
                     graph=Graph(lambda *a: (404, b"{}")), vault=vault)
    client = TestClient(app)

    class A:
        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def plan(cls, name):
            conn = cls.conn()
            with conn.cursor() as cur:
                plans.set_plan(cur, tid, name)
            conn.commit()
            conn.close()

        @classmethod
        def key(cls, scopes=("template_messages:send",), **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = api_keys.create(cur, tid, kw.pop("name", "test key"), list(scopes), **kw)
                conn.commit()
                return out["secret"]
            finally:
                conn.close()

        @staticmethod
        def call(method, path, secret=None, headers=None, **kw):
            h = dict(headers or {})
            if secret:
                h.setdefault("Authorization", "Bearer " + secret)
            return client.request(method, path, headers=h, **kw)

        @classmethod
        def approved_template(cls, name="shipped"):
            migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
                       " values (%s, (select id from wabas), %s, 'en_US', 'UTILITY', 'APPROVED', %s::jsonb)"
                       " on conflict (waba_id, name, language) do nothing", (tid, name, json.dumps([BODY])))

    A.db, A.tid, A.nid, A.vault, A.client = migrated, tid, number, vault, client
    return A


# ── keys ─────────────────────────────────────────────────────────────────────

def test_a_key_is_shown_once_and_stored_only_as_a_hash(api):
    secret = api.key()
    assert secret.startswith("pk_live_") and len(secret) == len("pk_live_") + 32
    row = H.only(api.db, "select * from api_keys")
    assert row["key_hash"] == api_keys._hash(secret) and row["prefix"] == secret[:12] and secret not in json.dumps(row, default=str)
    assert row["last_used_at"] is None and row["revoked_at"] is None
    conn = api.conn()
    with conn.cursor() as cur:
        listing = api_keys.list_keys(cur, api.tid)
    conn.close()
    assert secret not in json.dumps(listing, default=str) and listing[0]["prefix"] == secret[:12]


def test_key_creation_is_validated_and_limited_by_plan(api):
    conn = api.conn()
    cur = conn.cursor()
    for args, why in (((" ", ["template_messages:send"]), "bad_name"), (("k", []), "no_scopes"), (("k", ["root"]), "bad_scope")):
        with pytest.raises(api_keys.ApiKeyError) as e:
            api_keys.create(cur, api.tid, *args)
        assert e.value.reason == why
    api_keys.create(cur, api.tid, "basic key", ["template_messages:send"])
    with pytest.raises(plans.PlanLimit) as e:
        api_keys.create(cur, api.tid, "too much", ["contacts:write"])
    assert e.value.needs == "pro"
    conn.rollback()
    conn.close()
    api.plan("pro")
    api.key(ALL)


def test_a_revoked_or_made_up_key_is_refused_and_use_is_recorded(api):
    secret = api.key()
    body = {"to": "919876543210", "template": "shipped", "consent_attested": True}
    api.approved_template()
    assert api.call("POST", "/api/v1/template-messages", secret, json=body).status_code == 202
    assert api.db.one("select last_used_at from api_keys") is not None
    conn = api.conn()
    with conn.cursor() as cur:
        assert api_keys.revoke(cur, api.tid, api.db.one("select id from api_keys")) is True
        assert api_keys.revoke(cur, api.tid, api.db.one("select id from api_keys")) is False
    conn.commit()
    conn.close()
    for who in (secret, "pk_live_" + "0" * 32, "garbage", "", "pk_live_" + "x" * 100):
        r = api.call("POST", "/api/v1/template-messages", who or None, json=body)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_key", who


def test_the_key_can_arrive_as_x_api_key_too(api):
    secret = api.key()
    api.approved_template()
    r = api.call("POST", "/api/v1/template-messages", None, headers={"X-Api-Key": secret},
                 json={"to": "919876543210", "template": "shipped", "consent_attested": True})
    assert r.status_code == 202


def test_a_key_cannot_do_what_its_scopes_do_not_say(api):
    api.plan("pro")
    send_only = api.key(["template_messages:send"])
    r = api.call("GET", "/api/v1/templates", send_only)
    assert r.status_code == 403 and r.json()["error"]["code"] == "scope"
    assert api.call("POST", "/api/v1/contacts", send_only, json={"phone": "919876543210"}).status_code == 403


def test_a_plan_downgrade_takes_the_wider_api_away_at_once(api):
    api.plan("pro")
    secret = api.key(["templates:read"])
    assert api.call("GET", "/api/v1/templates", secret).status_code == 200
    api.plan("basic")
    r = api.call("GET", "/api/v1/templates", secret)
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_feature"


def test_calls_are_rate_limited_per_key(migrated):
    clock = [0.0]
    app = FastAPI()
    mount(app, connect=lambda: psycopg2.connect(migrated.dsn), limiter=RateLimiter(clock=lambda: clock[0]))
    tid, _ = H.onboard(migrated)
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        plans.set_plan(cur, tid, "pro")
        secret = api_keys.create(cur, tid, "slow", ["templates:read"], rate_limit=3)["secret"]
        other = api_keys.create(cur, tid, "other", ["templates:read"])["secret"]
    conn.commit()
    conn.close()
    c = TestClient(app)
    hit = lambda key: c.get("/api/v1/templates", headers={"Authorization": "Bearer " + key})        # noqa: E731
    assert [hit(secret).status_code for _ in range(3)] == [200, 200, 200]
    r = hit(secret)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.json()["error"]["code"] == "rate_limited"
    assert hit(other).status_code == 200                                                          # another key is unaffected
    clock[0] += 61
    assert hit(secret).status_code == 200


def test_the_internal_api_makes_lists_and_revokes_keys_for_managers_only(api):
    owner = H.only(api.db, "select 1 as x")  # noqa: F841
    app_client = api.client
    post = lambda path, **b: app_client.post(path, json={"tenant_id": str(api.tid), **b}, headers={"X-Internal-Token": "tok"})   # noqa: E731
    boss = post("/internal/inbox/ensure_agent", seat_key="s1", name="Boss").json()["agent"]
    staff = post("/internal/inbox/ensure_agent", seat_key="s2", name="Staff").json()["agent"]
    r = post("/internal/api_keys/create", name="shop", scopes=["template_messages:send"], by_agent_id=staff["id"])
    assert r.json()["error"] == "not_allowed"
    r = post("/internal/api_keys/create", name="shop", scopes=["template_messages:send"], by_agent_id=boss["id"], plan="basic")
    assert r.status_code == 200 and r.json()["key"]["secret"].startswith("pk_live_")
    listing = post("/internal/api_keys/list").json()
    assert [k["name"] for k in listing["keys"]] == ["shop"] and "secret" not in listing["keys"][0]
    assert post("/internal/api_keys/create", name="x", scopes=["nope"], by_agent_id=boss["id"]).status_code == 400
    r = post("/internal/api_keys/create", name="wide", scopes=["contacts:write"], by_agent_id=boss["id"], plan="basic")
    assert r.status_code == 403 and r.json()["needs"] == "pro"
    assert post("/internal/api_keys/revoke", key_id=listing["keys"][0]["id"], by_agent_id=staff["id"]).json()["error"] == "not_allowed"
    assert post("/internal/api_keys/revoke", key_id=listing["keys"][0]["id"], by_agent_id=boss["id"]).json()["revoked"] is True


# ── sending a template ───────────────────────────────────────────────────────

def send_template(api, secret, **body):
    body.setdefault("to", "919876543210")
    body.setdefault("template", "shipped")
    return api.call("POST", "/api/v1/template-messages", secret, json=body)


def test_a_template_message_is_queued_with_its_values_and_the_state_can_be_read_back(api):
    api.plan("pro")
    secret = api.key(["template_messages:send", "messages:read"])
    api.approved_template()
    r = send_template(api, secret, to="+91 98765-43210", params=["Asha", "A-1"], consent_attested=True,
                      header={"type": "image", "link": "https://example.com/a.jpg"}, url_params=["track-77"], language="en_US")
    assert r.status_code == 202 and r.json()["status"] == "queued"
    out = H.only(api.db, "select kind, to_phone, payload, state, message_id from outbox")
    assert (out["kind"], out["to_phone"], out["state"]) == ("template", "919876543210", "queued")
    comps = out["payload"]["template"]["components"]
    assert comps[0] == {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://example.com/a.jpg"}}]}
    assert [p["text"] for p in comps[1]["parameters"]] == ["Asha", "A-1"]
    assert comps[2] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "track-77"}]}
    assert api.db.one("select sender from messages") == "api"

    state = api.call("GET", "/api/v1/messages/" + r.json()["id"], secret).json()
    assert state["status"] == "queued"
    send.send_due(api.conn, graph=Graph(FakeGraph()), vault=api.vault)
    state = api.call("GET", "/api/v1/messages/" + r.json()["id"], secret).json()
    assert state["state"] == "sent" and state["status"] == "sent" and state["wamid"].startswith("wamid.")


def test_the_same_idempotency_key_never_sends_twice(api):
    secret = api.key()
    api.approved_template()
    ids = {send_template(api, secret, consent_attested=True, headers=None).json()["id"] for _ in range(1)}
    first = api.call("POST", "/api/v1/template-messages", secret, headers={"Idempotency-Key": "order-1"},
                     json={"to": "919876543210", "template": "shipped", "consent_attested": True})
    again = api.call("POST", "/api/v1/template-messages", secret, headers={"Idempotency-Key": "order-1"},
                     json={"to": "919876543210", "template": "shipped", "consent_attested": True})
    other = api.call("POST", "/api/v1/template-messages", secret, json={"to": "919876543210", "template": "shipped", "idempotency_key": "order-2"})
    assert first.json()["id"] == again.json()["id"] != other.json()["id"] and ids
    assert api.db.one("select count(*) from outbox") == 3


def test_a_stranger_cannot_be_messaged_without_consent_and_the_attestation_is_recorded(api):
    secret = api.key()
    api.approved_template()
    r = send_template(api, secret)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_consent"
    assert api.db.one("select count(*) from outbox") == 0
    ok = send_template(api, secret, consent_attested=True)
    assert ok.status_code == 202
    led = H.only(api.db, "select action, source, evidence from consent_ledger")
    assert led["action"] == "opt_in" and led["source"] == "api" and "test key" in led["evidence"]
    assert send_template(api, secret).status_code == 202                      # opted in now: no attestation needed


def test_an_opted_out_number_is_never_sent_a_template_whatever_the_request_says(api):
    secret = api.key()
    api.approved_template()
    send_template(api, secret, consent_attested=True)
    api.db.q("update contacts set opt_out = true, opted_in = false")
    r = send_template(api, secret, consent_attested=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "opted_out"
    assert api.db.one("select count(*) from outbox") == 1


def test_bad_template_message_requests_say_what_is_wrong(api):
    secret = api.key()
    api.approved_template()
    for body, status, code in (({"to": "12345", "template": "shipped"}, 400, "bad_phone"),
                               ({"to": "919876543210"}, 400, "template_required"),
                               ({"to": "919876543210", "template": "shipped", "params": "Asha", "consent_attested": True}, 400, "bad_params")):
        r = api.call("POST", "/api/v1/template-messages", secret, json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code), body
    assert api.call("POST", "/api/v1/template-messages", secret, data="{not json", headers={"Content-Type": "application/json"}).json()["error"]["code"] == "bad_json"
    assert api.call("POST", "/api/v1/template-messages", secret, json=[1]).json()["error"]["code"] == "bad_json"


def test_a_template_that_is_not_approved_is_refused_with_the_reason(api):
    secret = api.key()
    api.approved_template()
    api.db.q("update templates set status = 'PAUSED'")
    r = send_template(api, secret, consent_attested=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "template_not_approved"


# ── the wider API (Pro) ──────────────────────────────────────────────────────

def test_a_reply_inside_the_window_works_and_outside_it_is_refused(api):
    api.plan("pro")
    secret = api.key(["messages:send"])
    H.deliver(api.db, P.inbound(P.text("hello", "wamid.IN", str(int(time.time())))))
    H.work(api.db)
    r = api.call("POST", "/api/v1/messages", secret, json={"to": "919800000001", "text": "your quote is ready"})
    assert r.status_code == 202
    api.db.q("update conversations set last_inbound_at = now() - interval '30 hours'")
    r = api.call("POST", "/api/v1/messages", secret, json={"to": "919800000001", "text": "again"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "window_closed"
    assert api.call("POST", "/api/v1/messages", secret, json={"to": "919811111111", "text": "x"}).status_code == 404
    assert api.call("POST", "/api/v1/messages", secret, json={"to": "919800000001", "text": " "}).status_code == 400


def test_contacts_can_be_created_updated_looked_up_and_given_consent(api):
    api.plan("pro")
    secret = api.key(["contacts:write", "contacts:read"])
    r = api.call("POST", "/api/v1/contacts", secret, json={"phone": "98765 43210", "name": "Asha Shah", "tags": ["vip", "builder"],
                                                           "attributes": {"City": "Vadodara"}, "consent": {"opt_in": True, "evidence": "web form"}})
    assert r.status_code == 201 and r.json()["created"] is True and r.json()["phone"] == "919876543210"
    r2 = api.call("POST", "/api/v1/contacts", secret, json={"phone": "919876543210", "tags": ["repeat"]})
    assert r2.status_code == 200 and r2.json()["created"] is False and r2.json()["id"] == r.json()["id"]
    got = api.call("GET", "/api/v1/contacts?phone=%2B919876543210", secret).json()
    assert got["name"] == "Asha Shah" and got["tags"] == ["builder", "repeat", "vip"] and got["attributes"] == {"City": "Vadodara"}
    assert got["opted_in"] is True and got["opted_out"] is False
    assert api.db.one("select evidence from consent_ledger") == "web form"
    assert api.call("GET", "/api/v1/contacts?phone=919000000000", secret).status_code == 404
    assert api.call("GET", "/api/v1/contacts", secret).status_code == 400
    assert api.call("POST", "/api/v1/contacts", secret, json={"phone": "nope"}).status_code == 400
    assert api.call("POST", "/api/v1/contacts", secret, json={"phone": "919876543211", "tags": "vip"}).status_code == 400
    api.call("POST", "/api/v1/contacts", secret, json={"phone": "919876543210", "consent": {"opt_out": True}})
    assert api.call("GET", "/api/v1/contacts?phone=919876543210", secret).json()["opted_out"] is True


def test_the_tag_ceiling_applies_to_the_api_too(api):
    api.plan("pro")
    secret = api.key(["contacts:write"])
    r = api.call("POST", "/api/v1/contacts", secret, json={"phone": "919876543210", "tags": ["t%d" % i for i in range(101)]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_limit"


def test_one_workspace_never_sees_anothers_contact_or_message_or_campaign(api):
    api.plan("pro")
    secret = api.key(["contacts:read", "messages:read", "campaigns:read", "contacts:write"])
    other = H.tenant(api.db, "shakti")
    theirs = api.db.one("insert into contacts (tenant_id, name, phone) values (%s,'Theirs','919876543210') returning id", (other,))
    assert api.call("GET", "/api/v1/contacts?phone=919876543210", secret).status_code == 404
    created = api.call("POST", "/api/v1/contacts", secret, json={"phone": "919876543210", "name": "Mine"})
    assert created.status_code == 201 and created.json()["id"] != str(theirs)
    assert api.db.one("select name from contacts where id = %s", (theirs,)) == "Theirs"
    wa = api.db.one("insert into wabas (tenant_id, waba_id) values (%s,'WB') returning id", (other,))
    num = api.db.one("insert into phone_numbers (tenant_id, waba_id, phone_number_id) values (%s,%s,'PB') returning id", (other, wa))
    foreign = api.db.one("insert into outbox (tenant_id, number_id, to_phone, kind, payload, idem_key) values (%s,%s,'919876543210','text','{}','x') returning id", (other, num))
    assert api.call("GET", "/api/v1/messages/%s" % foreign, secret).status_code == 404
    camp = api.db.one("insert into campaigns (tenant_id, template_name, total) values (%s,'t',1) returning id", (other,))
    assert api.call("GET", "/api/v1/campaigns/%s" % camp, secret).status_code == 404
    assert api.call("GET", "/api/v1/messages/not-a-uuid", secret).status_code == 404


def test_templates_and_campaigns_through_the_api(api):
    api.plan("pro")
    secret = api.key(["templates:read", "campaigns:write", "campaigns:read", "contacts:write"])
    api.approved_template("diwali")
    api.db.q("update templates set components = %s::jsonb", (json.dumps([{"type": "BODY", "text": "Hello {{1}}, our Diwali offer ends on Friday so do reply soon please.", "example": {"body_text": [["Asha"]]}}]),))
    assert [t["name"] for t in api.call("GET", "/api/v1/templates", secret).json()["templates"]] == ["diwali"]
    for i in range(3):
        api.call("POST", "/api/v1/contacts", secret, json={"phone": "98765432%02d" % i, "name": "Asha %d" % i, "tags": ["vip"], "consent": {"opt_in": True}})
    r = api.call("POST", "/api/v1/campaigns", secret, json={"name": "Diwali", "template_name": "diwali", "audience": {"tags": ["vip"]},
                                                            "variables": {"1": {"source": "field", "field": "first_name"}}, "launch": True})
    assert r.status_code == 201 and r.json()["status"] == "sending" and r.json()["queued"] == 3
    rep = api.call("GET", "/api/v1/campaigns/" + r.json()["id"], secret).json()
    assert rep["counts"]["recipients"] == 3 and rep["campaign"]["name"] == "Diwali"
    bad = api.call("POST", "/api/v1/campaigns", secret, json={"name": "x"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "missing_field"
    blocked = api.call("POST", "/api/v1/campaigns", secret, json={"name": "Nobody", "template_name": "diwali", "audience": {"tags": ["none"]},
                                                                  "variables": {"1": {"source": "static", "value": "x"}}, "launch": True})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "preflight_failed"
