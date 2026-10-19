"""The service-to-service API, the connect flow and the worker loop, against a real
Postgres and a fake Graph."""
import json
import os
import time
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import onboarding, send, worker
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.jobs import utcnow
from prism_wa.main import create_app_from_env
from prism_wa.vault import Vault, generate_key

APP_SECRET = "test-app-secret"
INTERNAL = "internal-shared-secret"
APP_ID = "1588055710027509"
TOKEN = "EAAB-customer-token"


class Meta:
    """A fake Graph that understands the onboarding calls and message sends."""

    def __init__(self):
        self.calls, self.reject_code, self.fail_register, self.sent = [], False, False, 0

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        self.calls.append((method, url, body))
        if "/oauth/access_token" in url:
            if self.reject_code:
                return 400, json.dumps({"error": {"message": "invalid code", "code": 100}}).encode()
            return 200, json.dumps({"access_token": TOKEN}).encode()
        if url.endswith("/register"):
            if self.fail_register:
                return 400, json.dumps({"error": {"message": "pin mismatch", "code": 133005}}).encode()
            return 200, b'{"success": true}'
        if url.endswith("/subscribed_apps"):
            return 200, b'{"success": true}'
        if url.endswith("/messages"):
            self.sent += 1
            return 200, json.dumps({"messages": [{"id": "wamid.SENT%d" % self.sent}]}).encode()
        return 404, b"{}"

    def pins(self):
        return [b["pin"] for m, u, b in self.calls if u.endswith("/register")]


@pytest.fixture
def vault():
    return Vault({1: os.urandom(32)})


@pytest.fixture
def meta():
    return Meta()


def make_client(db, meta, vault, **kw):
    kw.setdefault("internal_token", INTERNAL)
    kw.setdefault("app_id", APP_ID)
    kw.setdefault("es_config_id", "CONFIG123")
    app = create_app(app_secrets=APP_SECRET, verify_token="v", connect=lambda: psycopg2.connect(db.dsn),
                     graph=Graph(meta, version="v25.0"), vault=vault, **kw)
    return TestClient(app)


AUTH = {"X-Internal-Token": INTERNAL}


def contact(db, tenant, phone=P.ASHA, inbound_ago=timedelta(minutes=5), opt_out=False, number=None):
    cid = db.one("insert into contacts (tenant_id, name, phone, opt_out) values (%s, 'Asha', %s, %s) returning id",
                 (tenant, phone, opt_out))
    if inbound_ago is not None:
        db.q("insert into conversations (tenant_id, contact_id, channel, last_inbound_at, number_id)"
             " values (%s, %s, 'whatsapp', %s, %s)", (tenant, cid, utcnow() - inbound_ago, number))
    return cid


# ── the internal API ─────────────────────────────────────────────────────────

def test_the_internal_routes_do_not_exist_without_a_shared_secret(migrated, meta, vault):
    c = make_client(migrated, meta, vault, internal_token="")
    for path in ("/internal/reply", "/internal/broadcast", "/internal/onboarding/link"):
        assert c.post(path, json={}, headers=AUTH).status_code == 404
    assert c.get("/onboarding").status_code == 404


def test_internal_calls_need_the_shared_secret(migrated, meta, vault):
    c = make_client(migrated, meta, vault)
    for path in ("/internal/reply", "/internal/broadcast", "/internal/onboarding/link"):
        assert c.post(path, json={}).status_code == 401
        assert c.post(path, json={}, headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert migrated.one("select count(*) from outbox") == 0


def test_a_reply_is_queued_for_a_contact_or_a_conversation(migrated, meta, vault):
    tenant, number = H.onboard(migrated)
    cid = contact(migrated, tenant, number=number)
    conv = migrated.one("select id from conversations")
    c = make_client(migrated, meta, vault)

    r = c.post("/internal/reply", headers=AUTH,
               json={"tenant_id": str(tenant), "contact_id": str(cid), "text": "on my way", "idem_key": "r1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r2 = c.post("/internal/reply", headers=AUTH,
                json={"tenant_id": str(tenant), "conversation_id": str(conv), "text": "and again", "idem_key": "r2"})
    assert r2.status_code == 200
    assert migrated.one("select count(*) from outbox") == 2
    again = c.post("/internal/reply", headers=AUTH,
                   json={"tenant_id": str(tenant), "contact_id": str(cid), "text": "on my way", "idem_key": "r1"})
    assert again.json()["outbox_id"] == r.json()["outbox_id"] and migrated.one("select count(*) from outbox") == 2


def test_a_reply_outside_the_window_is_a_409_with_a_reason(migrated, meta, vault):
    tenant, number = H.onboard(migrated)
    cid = contact(migrated, tenant, inbound_ago=timedelta(hours=30), number=number)
    r = make_client(migrated, meta, vault).post(
        "/internal/reply", headers=AUTH, json={"tenant_id": str(tenant), "contact_id": str(cid), "text": "hi"})
    assert r.status_code == 409 and r.json()["error"] == "window_closed"


def test_malformed_requests_are_400s(migrated, meta, vault):
    tenant, _ = H.onboard(migrated)
    c = make_client(migrated, meta, vault)
    for body in ({}, {"tenant_id": "nope", "text": "x", "contact_id": "also-nope"},
                 {"tenant_id": str(tenant), "text": "x"}, {"tenant_id": str(tenant), "contact_id": str(tenant)}):
        assert c.post("/internal/reply", headers=AUTH, json=body).status_code == 400, body
    assert c.post("/internal/reply", headers=AUTH, content=b"{oops").status_code == 400


def test_one_workspace_cannot_reply_to_anothers_contact(migrated, meta, vault):
    a, number = H.onboard(migrated, "orena")
    b = H.tenant(migrated, "shakti")
    theirs = contact(migrated, b, "919811111111")
    r = make_client(migrated, meta, vault).post(
        "/internal/reply", headers=AUTH, json={"tenant_id": str(a), "contact_id": str(theirs), "text": "hi"})
    assert r.status_code == 409 and r.json()["error"] == "unknown_contact"


def test_a_broadcast_queues_a_template_per_contact_and_reports_who_was_refused(migrated, meta, vault):
    tenant, number = H.onboard(migrated)
    ok1 = contact(migrated, tenant, "919800000001", None)
    ok2 = contact(migrated, tenant, "919800000002", None)
    out = contact(migrated, tenant, "919800000003", None, opt_out=True)
    camp = migrated.one("insert into campaigns (tenant_id, template_name, total) values (%s, 'diwali', 3) returning id",
                        (tenant,))
    c = make_client(migrated, meta, vault)
    body = {"tenant_id": str(tenant), "campaign_id": str(camp), "template_name": "diwali", "language": "en_US",
            "recipients": [str(ok1), str(ok2), str(out)]}
    r = c.post("/internal/broadcast", headers=AUTH, json=body)
    assert r.status_code == 200
    assert r.json()["queued"] == 2 and r.json()["refused"] == [{"contact_id": str(out), "reason": "opted_out"}]
    assert migrated.q("select kind, campaign_id from outbox")[0] == {"kind": "template", "campaign_id": camp}
    assert migrated.one("select failed from campaigns") == 1

    assert c.post("/internal/broadcast", headers=AUTH, json=body).json()["queued"] == 2      # a repeat adds nothing
    assert migrated.one("select count(*) from outbox") == 2


def test_a_broadcast_for_another_workspaces_campaign_is_refused(migrated, meta, vault):
    a, _ = H.onboard(migrated, "orena")
    b = H.tenant(migrated, "shakti")
    camp = migrated.one("insert into campaigns (tenant_id, template_name, total) values (%s, 'x', 1) returning id", (b,))
    cid = contact(migrated, a, P.ASHA, None)
    r = make_client(migrated, meta, vault).post("/internal/broadcast", headers=AUTH, json={
        "tenant_id": str(a), "campaign_id": str(camp), "template_name": "x", "recipients": [str(cid)]})
    assert r.status_code == 409 and r.json()["error"] == "unknown_campaign"


# ── the connect state ────────────────────────────────────────────────────────

def test_the_connect_state_names_the_tenant_and_cannot_be_forged_or_replayed_late():
    state = onboarding.make_state("s3cret", "tenant-1", ttl=60, now=1000)
    assert onboarding.read_state("s3cret", state, now=1030) == "tenant-1"
    for bad in (state, state[:-2] + "xx", "garbage", "", state + "."):
        with pytest.raises(onboarding.OnboardingError):
            onboarding.read_state("another-secret" if bad == state else "s3cret", bad, now=1030)
    with pytest.raises(onboarding.OnboardingError):
        onboarding.read_state("s3cret", state, now=1061)                       # expired
    body, _, sig = state.partition(".")
    other = onboarding.make_state("s3cret", "tenant-2", ttl=60, now=1000).partition(".")[0]
    with pytest.raises(onboarding.OnboardingError):
        onboarding.read_state("s3cret", other + "." + sig, now=1030)           # someone else's body, our signature


def test_the_connect_page_is_served_only_for_a_valid_state_and_escapes_its_values(migrated, meta, vault):
    tenant, _ = H.onboard(migrated)
    c = make_client(migrated, meta, vault, es_config_id="CFG</script><b>")
    link = c.post("/internal/onboarding/link", headers=AUTH, json={"tenant_id": str(tenant)}).json()
    page = c.get(link["path"])
    assert page.status_code == 200 and APP_ID in page.text and link["state"] in page.text
    assert "</script><b>" not in page.text                                    # cannot break out of the script
    assert c.get("/onboarding?state=forged").status_code == 400
    assert make_client(migrated, meta, vault, app_id="").get(link["path"]).status_code == 503


# ── completing a signup ──────────────────────────────────────────────────────

def signup(client, tenant, **over):
    state = onboarding.make_state(INTERNAL, tenant)
    body = {"state": state, "code": "one-time-code", "waba_id": "W-NEW", "phone_number_id": "PN-NEW"}
    body.update(over)
    return client.post("/onboarding/complete", json=body)


def test_a_signup_stores_an_encrypted_token_registers_the_number_and_subscribes(migrated, meta, vault):
    tenant = H.tenant(migrated, "orena")
    r = signup(make_client(migrated, meta, vault), tenant)
    assert r.status_code == 200 and r.json()["ok"] is True

    waba = H.only(migrated, "select * from wabas")
    assert waba["tenant_id"] == tenant and waba["waba_id"] == "W-NEW" and waba["subscribed_at"] is not None
    assert TOKEN.encode() not in bytes(waba["token_enc"])
    assert vault.decrypt(bytes(waba["token_enc"]), context="W-NEW") == TOKEN
    num = H.only(migrated, "select * from phone_numbers")
    assert (num["tenant_id"], num["status"], num["phone_number_id"]) == (tenant, "connected", "PN-NEW")
    pin = vault.decrypt(bytes(num["pin_enc"]), context="PN-NEW")
    assert len(pin) == 6 and pin.isdigit() and meta.pins() == [pin]
    assert any(u.endswith("/W-NEW/subscribed_apps") for _, u, _ in meta.calls)


def test_a_signup_is_safe_to_repeat_and_keeps_the_same_pin(migrated, meta, vault):
    tenant = H.tenant(migrated, "orena")
    c = make_client(migrated, meta, vault)
    signup(c, tenant)
    signup(c, tenant)
    assert migrated.one("select count(*) from wabas") == 1 and migrated.one("select count(*) from phone_numbers") == 1
    assert len(set(meta.pins())) == 1 and len(meta.pins()) == 2


def test_a_waba_or_number_already_owned_by_another_workspace_is_never_taken_over(migrated, meta, vault):
    H.onboard(migrated, "shakti", "W-NEW", "PN-NEW")
    r = signup(make_client(migrated, meta, vault), H.tenant(migrated, "orena"))
    assert r.status_code == 409 and r.json()["error"] == "waba_taken"
    assert migrated.one("select tenant_id from wabas") == H.tenant(migrated, "shakti")
    assert not meta.pins()

    r = signup(make_client(migrated, meta, vault), H.tenant(migrated, "orena"), waba_id="W-OTHER")
    assert r.status_code == 409 and r.json()["error"] == "number_taken"


def test_a_bad_or_expired_state_cannot_onboard_anyone(migrated, meta, vault):
    c = make_client(migrated, meta, vault)
    tenant = H.tenant(migrated, "orena")
    assert signup(c, tenant, state="forged").status_code == 401
    old = onboarding.make_state(INTERNAL, tenant, ttl=1, now=time.time() - 100)
    assert signup(c, tenant, state=old).status_code == 401
    assert signup(c, tenant, state=onboarding.make_state("someone-elses-secret", tenant)).status_code == 401
    assert migrated.one("select count(*) from wabas") == 0 and not meta.calls


def test_a_code_meta_rejects_changes_nothing(migrated, meta, vault):
    meta.reject_code = True
    r = signup(make_client(migrated, meta, vault), H.tenant(migrated, "orena"))
    assert r.status_code == 400 and r.json()["error"] == "code_rejected"
    assert migrated.one("select count(*) from wabas") == 0


def test_a_failed_registration_is_reported_and_a_retry_finishes_it(migrated, meta, vault):
    tenant = H.tenant(migrated, "orena")
    c = make_client(migrated, meta, vault)
    meta.fail_register = True
    r = signup(c, tenant)
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["errors"][0]["step"] == "register"
    assert migrated.one("select status from phone_numbers") == "needs_attention"
    assert migrated.one("select registered_at from phone_numbers") is None

    meta.fail_register = False
    assert signup(c, tenant).json()["ok"] is True
    assert migrated.one("select status from phone_numbers") == "connected"
    assert len(set(meta.pins())) == 1


def test_signup_without_a_vault_or_app_id_says_it_is_not_set_up(migrated, meta):
    r = signup(make_client(migrated, meta, None), H.tenant(migrated, "orena"))
    assert r.status_code == 503 and r.json()["error"] == "not_configured"


# ── the worker loop ──────────────────────────────────────────────────────────

def test_one_worker_cycle_ingests_an_inbound_message_and_delivers_a_queued_reply(migrated, meta, vault):
    tenant, number = H.onboard(migrated)
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    connect = lambda: psycopg2.connect(migrated.dsn)                       # noqa: E731

    H.deliver(migrated, P.inbound(P.text("hello", "wamid.IN1", str(int(time.time())))))
    first = worker.cycle(connect, graph=Graph(meta), vault=vault)
    assert first.ingested == 1 and first.busy
    assert migrated.one("select text from messages where direction = 'in'") == "hello"

    contact_id = migrated.one("select id from contacts")
    conn = connect()
    with conn.cursor() as cur:
        send.enqueue_text(cur, tenant, contact_id=contact_id, text="hi back", idem_key="r1")
    conn.commit()
    conn.close()
    second = worker.cycle(connect, graph=Graph(meta), vault=vault)
    assert second.sent == 1
    assert migrated.one("select status from messages where direction = 'out'") == "sent"
    assert not worker.cycle(connect, graph=Graph(meta), vault=vault).busy


# ── the environment ──────────────────────────────────────────────────────────

def test_the_app_refuses_to_start_without_its_required_environment():
    for env in ({}, {"WA_APP_SECRET": "s", "WA_VERIFY_TOKEN": "v"}, {"WA_APP_SECRET": "s", "DATABASE_URL": "x"},
                {"WA_VERIFY_TOKEN": "v", "DATABASE_URL": "x"}):
        with pytest.raises(RuntimeError):
            create_app_from_env(env)


def test_optional_parts_switch_on_with_their_environment():
    base = {"WA_APP_SECRET": "s", "WA_VERIFY_TOKEN": "v", "DATABASE_URL": "postgresql://127.0.0.1:1/none"}
    paths = lambda app: {r.path for r in app.routes}                       # noqa: E731
    assert "/internal/reply" not in paths(create_app_from_env(base))
    full = dict(base, WA_INTERNAL_TOKEN="i", WA_TOKEN_KEYS="1:" + generate_key(), WA_APP_ID="1", WA_ES_CONFIG_ID="2")
    assert {"/internal/reply", "/onboarding", "/onboarding/complete"} <= paths(create_app_from_env(full))
    with pytest.raises(Exception):
        create_app_from_env(dict(full, WA_TOKEN_KEYS="1:not-a-key"))
