"""The gateway end to end: HTTP -> signature -> normaliser -> real Postgres.

The two promises from the spec (WS2 "Done"): a forged signature is a 401 and
leaves no row; a duplicate delivery is stored once.
"""
import json

import payloads as P
import psycopg2
import psycopg2.errors as pgerr
import pytest
from fastapi.testclient import TestClient

from prism_wa.app import create_app
from prism_wa.signature import sign

SECRET = "test-app-secret"
TOKEN = "test-verify-token"


def tenant(db, slug):
    return db.one("select id from tenants where slug = %s", (slug,))


def onboard(db, slug, waba=P.WABA, pnid=P.PNID):
    """What Embedded Signup will do: give a tenant a WABA and a number."""
    tid = tenant(db, slug)
    wid = db.one("insert into wabas (tenant_id, waba_id) values (%s, %s) returning id", (tid, waba))
    db.q("insert into phone_numbers (tenant_id, waba_id, phone_number_id) values (%s, %s, %s)",
         (tid, wid, pnid))
    return tid


def client_for(db, **overrides):
    app = create_app(app_secrets=overrides.pop("secrets", SECRET), verify_token=TOKEN,
                     connect=overrides.pop("connect", lambda: psycopg2.connect(db.dsn)))
    return TestClient(app)


def post(client, payload, *, secret=SECRET, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if secret is not None:
        h["X-Hub-Signature-256"] = sign(secret, body)
    h.update(headers or {})
    return client.post("/webhook/wa", content=body, headers=h)


def counts(db):
    return db.one("select count(*) from wa_events"), db.one("select count(*) from wa_jobs")


# ── the schema ───────────────────────────────────────────────────────────────

def test_the_migration_is_idempotent(migrated):
    migrated.apply()
    migrated.apply()
    for table in ("wabas", "phone_numbers", "wa_events", "wa_jobs"):
        assert migrated.one("select to_regclass(%s) is not null", ("public." + table,))


def test_a_number_cannot_be_filed_under_a_different_tenants_waba(migrated):
    orena, shakti = tenant(migrated, "orena"), tenant(migrated, "shakti")
    waba = migrated.one("insert into wabas (tenant_id, waba_id) values (%s, 'W1') returning id", (orena,))
    with pytest.raises(pgerr.CheckViolation):
        migrated.q("insert into phone_numbers (tenant_id, waba_id, phone_number_id) values (%s, %s, 'P1')",
                   (shakti, waba))


def test_a_tenant_scoped_session_sees_only_its_own_events_and_jobs(migrated):
    a, b = onboard(migrated, "orena"), onboard(migrated, "shakti", "W2", P.OTHER_PNID)
    client = client_for(migrated)
    assert post(client, P.inbound(P.text("for a", "wamid.A"))).status_code == 200
    assert post(client, P.inbound(P.text("for b", "wamid.B"), pnid=P.OTHER_PNID, waba="W2")).status_code == 200
    assert post(client, P.inbound(P.text("nobody", "wamid.N"), pnid="999", waba="W9")).status_code == 200

    migrated.q("grant wa_app to current_user")
    scoped = migrated.connect(role="wa_app", tenant=str(a))
    seen = migrated.q("select canonical->>'text' as t from wa_events order by 1", conn=scoped)
    assert [r["t"] for r in seen] == ["for a"]                      # not b's, not the unrouted one
    assert migrated.one("select count(*) from wa_jobs", conn=scoped) == 1
    assert migrated.one("select count(*) from wabas", conn=scoped) == 1
    other = migrated.connect(role="wa_app", tenant=str(b))
    assert [r["t"] for r in migrated.q("select canonical->>'text' as t from wa_events", conn=other)] == ["for b"]
    nothing = migrated.connect(role="wa_app")                       # no tenant set: fails closed
    assert migrated.one("select count(*) from wa_events", conn=nothing) == 0


# ── the handshake ────────────────────────────────────────────────────────────

def test_the_handshake_echoes_the_challenge_only_for_the_right_token(migrated):
    c = client_for(migrated)
    ok = c.get("/webhook/wa", params={"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "1158201444"})
    assert (ok.status_code, ok.text) == (200, "1158201444")
    for params in ({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
                   {"hub.mode": "unsubscribe", "hub.verify_token": TOKEN, "hub.challenge": "x"},
                   {"hub.challenge": "x"}, {}):
        assert c.get("/webhook/wa", params=params).status_code == 403


def test_an_empty_verify_token_never_verifies(migrated):
    app = create_app(app_secrets=SECRET, verify_token="", connect=lambda: psycopg2.connect(migrated.dsn))
    r = TestClient(app).get("/webhook/wa", params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x"})
    assert r.status_code == 403


def test_a_gateway_without_a_secret_refuses_to_exist(migrated):
    for secrets in ("", [], [""]):
        with pytest.raises(ValueError):
            create_app(app_secrets=secrets, verify_token=TOKEN, connect=lambda: None)


# ── forged and unsigned deliveries ───────────────────────────────────────────

def test_a_forged_signature_is_refused_and_leaves_no_row(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated)
    r = post(c, P.inbound(P.text("inject me")), secret="not-the-app-secret")
    assert r.status_code == 401
    assert counts(migrated) == (0, 0)


def test_an_unsigned_or_wrongly_prefixed_delivery_is_refused(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated)
    assert post(c, P.inbound(P.text("x")), secret=None).status_code == 401
    assert post(c, P.inbound(P.text("x")), secret=None,
                headers={"X-Hub-Signature-256": "sha1=deadbeef"}).status_code == 401
    assert counts(migrated) == (0, 0)


def test_a_body_changed_after_signing_is_refused(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated)
    body = json.dumps(P.inbound(P.text("original"))).encode()
    r = c.post("/webhook/wa", content=body.replace(b"original", b"tampered"),
               headers={"X-Hub-Signature-256": sign(SECRET, body), "Content-Type": "application/json"})
    assert r.status_code == 401 and counts(migrated) == (0, 0)


def test_the_old_secret_still_works_during_a_rotation(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated, secrets=["new-secret", SECRET])
    assert post(c, P.inbound(P.text("a", "wamid.1")), secret=SECRET).status_code == 200
    assert post(c, P.inbound(P.text("b", "wamid.2")), secret="new-secret").status_code == 200
    assert post(c, P.inbound(P.text("c", "wamid.3")), secret="third").status_code == 401


# ── accepted deliveries ──────────────────────────────────────────────────────

def test_a_signed_message_is_stored_attributed_and_queued(migrated):
    orena = onboard(migrated, "orena")
    r = post(client_for(migrated), P.inbound(P.text("hello", "wamid.T1")))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "events": 1, "stored": 1, "duplicates": 0, "unrouted": 0, "jobs": 1}

    ev = migrated.q("select * from wa_events")[0]
    assert ev["tenant_id"] == orena and ev["kind"] == "message"
    assert (ev["waba_id"], ev["phone_number_id"]) == (P.WABA, P.PNID)
    assert ev["canonical"]["text"] == "hello" and ev["canonical"]["wa_id"] == P.ASHA
    assert ev["raw"]["message"]["id"] == "wamid.T1"

    job = migrated.q("select * from wa_jobs")[0]
    assert (job["tenant_id"], job["kind"], job["state"]) == (orena, "ingest_event", "queued")
    assert job["payload"]["event_id"] == str(ev["id"])


def test_a_duplicate_delivery_is_stored_once(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated)
    payload = P.inbound(P.text("hello", "wamid.DUP"))
    first, second = post(c, payload), post(c, payload)
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicates"] == 1 and second.json()["stored"] == 0
    assert counts(migrated) == (1, 1)


def test_a_delivery_with_a_message_and_a_status_makes_two_events_and_two_jobs(migrated):
    onboard(migrated, "orena")
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": P.PNID},
             "contacts": [{"profile": {"name": "Asha"}, "wa_id": P.ASHA}],
             "messages": [P.text("hi", "wamid.M1")], "statuses": [P.status("wamid.OUT1", "read", "9")]}
    r = post(client_for(migrated), P.envelope(value))
    assert r.json()["stored"] == 2 and counts(migrated) == (2, 2)


def test_sent_delivered_read_are_three_rows_for_one_message(migrated):
    onboard(migrated, "orena")
    c = client_for(migrated)
    for state, ts in (("sent", "1"), ("delivered", "2"), ("read", "3")):
        post(c, P.statuses(P.status("wamid.OUT1", state, ts)))
    assert counts(migrated) == (3, 3)


def test_an_event_for_an_unknown_number_is_kept_but_not_queued(migrated):
    r = post(client_for(migrated), P.inbound(P.text("who am i", "wamid.U"), pnid="777", waba="W777"))
    assert r.status_code == 200
    assert r.json()["unrouted"] == 1 and r.json()["jobs"] == 0
    row = migrated.q("select tenant_id, phone_number_id from wa_events")[0]
    assert row["tenant_id"] is None and row["phone_number_id"] == "777"
    assert counts(migrated) == (1, 0)


def test_an_account_event_with_no_number_routes_by_its_waba(migrated):
    orena = onboard(migrated, "orena")
    r = post(client_for(migrated), P.phone_quality("UPGRADE", "TIER_10K"))
    assert r.json()["jobs"] == 1
    assert migrated.q("select tenant_id, kind from wa_events")[0] == {"tenant_id": orena, "kind": "phone_quality"}


def test_a_delivery_with_nothing_we_recognise_is_acknowledged_and_stores_nothing(migrated):
    c = client_for(migrated)
    r = post(c, {"object": "page", "entry": []})
    assert (r.status_code, r.json()) == (200, {"ok": True, "events": 0})
    assert counts(migrated) == (0, 0)


def test_signed_but_not_json_is_a_400(migrated):
    assert post(client_for(migrated), b"{not json").status_code == 400


# ── when the database is the problem Meta must retry, not be told 200 ────────

def test_a_database_outage_is_a_500_so_meta_retries(migrated):
    def down():
        raise psycopg2.OperationalError("connection refused")
    r = post(client_for(migrated, connect=down), P.inbound(P.text("lost?", "wamid.X")))
    assert r.status_code == 500


def test_a_failure_part_way_through_a_delivery_records_none_of_it(migrated):
    onboard(migrated, "orena")
    migrated.q("drop table wa_jobs")                  # the event insert will work, the job insert will not
    r = post(client_for(migrated), P.inbound(P.text("a", "wamid.1"), P.text("b", "wamid.2")))
    assert r.status_code == 500
    assert migrated.one("select count(*) from wa_events") == 0

    migrated.apply()                                  # table back; Meta's retry now succeeds in full
    r = post(client_for(migrated), P.inbound(P.text("a", "wamid.1"), P.text("b", "wamid.2")))
    assert r.status_code == 200 and counts(migrated) == (2, 2)
