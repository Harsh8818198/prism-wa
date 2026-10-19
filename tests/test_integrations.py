"""Shopify and WooCommerce: connecting a store, signed webhooks, consent, tracking, abandoned carts."""
import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import integrations, jobs
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.integrations import IntegrationError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

SECRET = "shpss_a-long-store-secret"
ORDER = {"id": 450789469, "name": "#1001", "email": "asha@example.com", "phone": None, "total_price": "1299.00",
         "currency": "INR", "checkout_id": 901414060, "order_status_url": "https://shop.example/orders/abc",
         "customer": {"first_name": "Asha", "last_name": "Shah", "phone": "+91 98765 43210"},
         "fulfillments": [{"tracking_number": "AWB123", "tracking_url": "https://track.example/AWB123", "tracking_company": "Delhivery"}]}
CHECKOUT = {"id": 901414060, "token": "ck_abc", "email": "asha@example.com", "phone": "9876543210", "total_price": "1299.00",
            "currency": "INR", "abandoned_checkout_url": "https://shop.example/checkouts/ck_abc",
            "billing_address": {"first_name": "Asha", "last_name": "Shah"}}
CONFIG = {"consent_attested": True, "events": {
    "order_created": {"template": "order_confirmation", "variables": {"1": "customer.first_name", "2": "order.name", "3": "order.total"}},
    "order_fulfilled": {"template": "order_shipped", "variables": {"1": "customer.first_name", "2": "tracking.number", "3": "tracking.url"}},
    "abandoned_cart": {"template": "cart_reminder", "delay_minutes": 60, "variables": {"1": "customer.first_name", "2": "checkout.url", "3": "=Free shipping today"}}}}


def sign(secret, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def shop(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok",
                     graph=Graph(lambda *a: (404, b"{}")), vault=vault)
    client = TestClient(app)

    class S:
        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def internal(cls, path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "tok"})

        @classmethod
        def connect(cls, kind="shopify", config=None):
            boss = cls.internal("/internal/inbox/ensure_agent", seat_key="boss", name="Boss").json()["agent"]
            r = cls.internal("/internal/integrations/create", kind=kind, name="My store", secret=SECRET,
                             config=CONFIG if config is None else config, by_agent_id=boss["id"])
            assert r.status_code == 200, r.json()
            cls.boss = boss
            return r.json()["integration"]

        @staticmethod
        def hook(kind, token, payload, topic, *, secret=SECRET, webhook_id=None, headers=None, raw=None):
            body = raw if raw is not None else json.dumps(payload).encode()
            h = {"Content-Type": "application/json"}
            if kind == "shopify":
                h.update({"X-Shopify-Topic": topic, "X-Shopify-Hmac-Sha256": sign(secret, body)})
                if webhook_id:
                    h["X-Shopify-Webhook-Id"] = webhook_id
            else:
                h.update({"X-WC-Webhook-Topic": topic, "X-WC-Webhook-Signature": sign(secret, body)})
            h.update(headers or {})
            return client.post("/hooks/%s/%s" % (kind, token), content=body, headers=h)

    S.db, S.tid, S.nid, S.client, S.vault = migrated, tid, number, client, vault
    return S


def components(db, n=0):
    return db.q("select payload #> '{template,components,0,parameters}' as p, payload #>> '{template,name}' as t from outbox order by created_at")[n]


# ── connecting a store ───────────────────────────────────────────────────────

def test_a_connection_gets_a_secret_address_and_stores_the_signing_secret_encrypted(shop):
    c = shop.connect()
    assert c["path"] == "/hooks/shopify/%s" % c["token"] and len(c["token"]) >= 30
    row = H.only(shop.db, "select * from integrations")
    assert SECRET.encode() not in bytes(row["secret_enc"]) and shop.vault.decrypt(bytes(row["secret_enc"]), context=c["token"]) == SECRET
    listing = shop.internal("/internal/integrations/list").json()["integrations"]
    assert listing[0]["name"] == "My store" and "secret" not in json.dumps(listing) and listing[0]["config"]["consent_attested"] is True


def test_only_a_manager_can_connect_a_store_and_bad_settings_are_refused_in_words(shop):
    staff = shop.internal("/internal/inbox/ensure_agent", seat_key="boss", name="Boss").json()["agent"]
    staff = shop.internal("/internal/inbox/ensure_agent", seat_key="staff", name="Staff").json()["agent"]
    r = shop.internal("/internal/integrations/create", kind="shopify", name="s", secret=SECRET, config={}, by_agent_id=staff["id"])
    assert r.json()["error"] == "not_allowed"
    boss = shop.internal("/internal/inbox/agents/list").json()["agents"][0]
    for body, why in (({"kind": "magento"}, "bad_kind"), ({"name": " "}, "bad_name"), ({"secret": "short"}, "bad_secret"),
                      ({"config": {"events": {"order_paid": {"template": "x"}}}}, "bad_config"),
                      ({"config": {"events": {"order_created": {"template": " "}}}}, "bad_config"),
                      ({"config": {"events": {"order_created": {"template": "t", "variables": {"x": "customer.name"}}}}}, "bad_config"),
                      ({"config": {"events": {"abandoned_cart": {"template": "t", "delay_minutes": 1}}}}, "bad_config"),
                      ({"config": {"default_country": "abc"}}, "bad_config")):
        args = {"kind": "shopify", "name": "s", "secret": SECRET, "config": {}, "by_agent_id": boss["id"], **body}
        r = shop.internal("/internal/integrations/create", **args)
        assert r.status_code == 400 and r.json()["error"] == why, body
    assert shop.db.one("select count(*) from integrations") == 0


def test_disabling_rotating_the_secret_and_deleting(shop):
    c = shop.connect()
    ok = shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    assert ok.status_code == 200
    shop.internal("/internal/integrations/update", integration_id=c["id"], enabled=False, by_agent_id=shop.boss["id"])
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w2").status_code == 404
    shop.internal("/internal/integrations/update", integration_id=c["id"], enabled=True, secret="a-brand-new-secret", by_agent_id=shop.boss["id"])
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w3").status_code == 401           # the old secret is dead
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", secret="a-brand-new-secret", webhook_id="w3").status_code == 200
    assert shop.internal("/internal/integrations/delete", integration_id=c["id"], by_agent_id=shop.boss["id"]).json()["deleted"] is True
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", secret="a-brand-new-secret", webhook_id="w4").status_code == 404


# ── signatures ───────────────────────────────────────────────────────────────

def test_a_bad_or_missing_signature_or_an_unknown_address_changes_nothing(shop):
    c = shop.connect()
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", secret="not-the-secret").status_code == 401
    r = shop.client.post("/hooks/shopify/" + c["token"], content=json.dumps(ORDER).encode(), headers={"X-Shopify-Topic": "orders/create"})
    assert r.status_code == 401
    assert shop.hook("shopify", "no-such-token", ORDER, "orders/create").status_code == 404
    assert shop.hook("woocommerce", c["token"], ORDER, "order.created").status_code == 404                        # right token, wrong kind
    assert shop.client.post("/hooks/magento/%s" % c["token"], content=b"{}").status_code == 404
    body = json.dumps(ORDER).encode()
    tampered = shop.client.post("/hooks/shopify/" + c["token"], content=body.replace(b"1001", b"9999"),
                                headers={"X-Shopify-Topic": "orders/create", "X-Shopify-Hmac-Sha256": sign(SECRET, body)})
    assert tampered.status_code == 401
    assert shop.db.one("select count(*) from integration_events") == 0 and shop.db.one("select count(*) from outbox") == 0


# ── an order becomes a message ───────────────────────────────────────────────

def test_a_new_order_queues_the_confirmation_with_the_orders_values(shop):
    c = shop.connect()
    r = shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    assert r.json() == {"ok": True, "status": "queued"}
    got = components(shop.db)
    assert got["t"] == "order_confirmation" and [x["text"] for x in got["p"]] == ["Asha", "#1001", "1299.00"]
    out = H.only(shop.db, "select to_phone, kind from outbox")
    assert out == {"to_phone": "919876543210", "kind": "template"}
    assert shop.db.one("select sender from messages") == "shopify"
    ev = H.only(shop.db, "select topic, external_id, status from integration_events")
    assert ev == {"topic": "orders/create", "external_id": "w1", "status": "queued"}
    assert shop.db.one("select last_event_at from integrations") is not None


def test_the_store_retrying_the_same_webhook_sends_one_message(shop):
    c = shop.connect()
    first = shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    again = shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    assert (first.json()["status"], again.json()["status"]) == ("queued", "duplicate")
    assert shop.db.one("select count(*) from outbox") == 1 and shop.db.one("select count(*) from integration_events") == 1


def test_a_shipped_order_carries_its_tracking(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], ORDER, "orders/fulfilled", webhook_id="w9")
    got = components(shop.db)
    assert got["t"] == "order_shipped" and [x["text"] for x in got["p"]] == ["Asha", "AWB123", "https://track.example/AWB123"]
    shop.hook("shopify", c["token"], ORDER["fulfillments"][0] | {"id": 77, "customer": ORDER["customer"]}, "fulfillments/create", webhook_id="w10")
    assert shop.db.one("select count(*) from outbox") == 2


def test_topics_that_are_not_set_up_or_not_known_are_acknowledged_and_ignored(shop):
    c = shop.connect(config={"consent_attested": True, "events": {"order_created": CONFIG["events"]["order_created"]}})
    assert shop.hook("shopify", c["token"], ORDER, "orders/fulfilled", webhook_id="a").json()["status"] == "ignored"
    assert shop.hook("shopify", c["token"], ORDER, "customers/update", webhook_id="b").json()["status"] == "ignored"
    assert shop.hook("shopify", c["token"], [1, 2], "orders/create", webhook_id="c").json()["status"] == "ignored"
    assert shop.db.one("select count(*) from outbox") == 0


# ── consent ──────────────────────────────────────────────────────────────────

def test_without_attested_consent_only_people_already_opted_in_are_messaged(shop):
    c = shop.connect(config={**CONFIG, "consent_attested": False})
    r = shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    assert r.json()["status"] == "skipped:no_consent" and shop.db.one("select count(*) from outbox") == 0
    assert shop.db.one("select status from integration_events") == "skipped"
    shop.db.q("update contacts set opted_in = true")
    assert shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w2").json()["status"] == "queued"


def test_attested_consent_is_written_to_the_ledger_with_the_order_as_evidence(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    led = H.only(shop.db, "select action, source, evidence from consent_ledger")
    assert led["action"] == "opt_in" and led["source"] == "shopify" and "orders/create" in led["evidence"] and "w1" in led["evidence"]


def test_an_opted_out_customer_is_never_messaged_by_a_store_event(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    shop.db.q("update contacts set opt_out = true, opted_in = false")
    r = shop.hook("shopify", c["token"], ORDER, "orders/fulfilled", webhook_id="w2")
    assert r.json()["status"] == "skipped:opted_out" and shop.db.one("select count(*) from outbox") == 1


def test_an_order_with_no_usable_phone_number_is_skipped(shop):
    c = shop.connect()
    nophone = {**ORDER, "customer": {"first_name": "Asha"}, "phone": None}
    assert shop.hook("shopify", c["token"], nophone, "orders/create", webhook_id="w1").json()["status"] == "skipped:no_phone"
    bad = {**ORDER, "customer": {"first_name": "Asha", "phone": "12345"}}
    assert shop.hook("shopify", c["token"], bad, "orders/create", webhook_id="w2").json()["status"] == "skipped:no_phone"


def test_the_stores_default_country_decides_what_a_bare_number_means(shop):
    c = shop.connect(config={**CONFIG, "default_country": "44"})
    order = {**ORDER, "customer": {"first_name": "Bob", "phone": "7911123456"}}
    shop.hook("shopify", c["token"], order, "orders/create", webhook_id="w1")
    assert shop.db.one("select to_phone from outbox") == "447911123456"


# ── WooCommerce ──────────────────────────────────────────────────────────────

WOO = {"id": 727, "number": "727", "status": "processing", "currency": "INR", "total": "999.00", "date_modified_gmt": "2026-10-17T10:00:00",
       "billing": {"first_name": "Asha", "last_name": "Shah", "phone": "9876543210", "email": "a@x.in"}}


def test_woocommerce_orders_created_and_completed(shop):
    c = shop.connect("woocommerce", config={"consent_attested": True, "events": {
        "order_created": {"template": "order_confirmation", "variables": {"1": "customer.name", "2": "order.id"}},
        "order_fulfilled": {"template": "order_shipped", "variables": {"1": "customer.first_name"}}}})
    assert c["path"].startswith("/hooks/woocommerce/")
    assert shop.hook("woocommerce", c["token"], WOO, "order.created").json()["status"] == "queued"
    assert [x["text"] for x in components(shop.db)["p"]] == ["Asha Shah", "727"]
    assert shop.hook("woocommerce", c["token"], WOO, "order.updated").json()["status"] == "ignored"             # still processing
    done = {**WOO, "status": "completed", "date_modified_gmt": "2026-10-18T09:00:00"}
    assert shop.hook("woocommerce", c["token"], done, "order.updated").json()["status"] == "queued"
    assert shop.hook("woocommerce", c["token"], done, "order.updated").json()["status"] == "duplicate"           # same order, same change
    assert shop.db.one("select count(*) from outbox") == 2


def test_woocommerces_first_ping_is_not_json_and_is_accepted(shop):
    c = shop.connect("woocommerce")
    r = shop.hook("woocommerce", c["token"], None, "action.woocommerce_add_to_cart", raw=b"webhook_id=12")
    assert r.status_code == 200 and r.json()["status"] == "ping"
    body = b"webhook_id=12"
    bad = shop.client.post("/hooks/woocommerce/" + c["token"], content=body, headers={"X-WC-Webhook-Signature": sign("wrong", body)})
    assert bad.status_code == 401


# ── abandoned carts ──────────────────────────────────────────────────────────

def work(shop, when):
    return H.work(shop.db, {"abandoned_cart": integrations.handle_cart_job}, now=when)


def test_a_checkout_that_is_never_finished_gets_one_reminder_after_the_delay(shop):
    c = shop.connect()
    r = shop.hook("shopify", c["token"], CHECKOUT, "checkouts/create", webhook_id="c1")
    assert r.json()["status"] == "queued"
    cart = H.only(shop.db, "select state, checkout_id, due_at from cart_checkouts")
    assert cart["state"] == "waiting" and cart["checkout_id"] == "901414060"
    assert shop.db.one("select count(*) from outbox") == 0
    now = utcnow()
    assert abs((cart["due_at"] - (now + timedelta(minutes=60))).total_seconds()) < 5
    work(shop, now + timedelta(minutes=30))
    assert shop.db.one("select count(*) from outbox") == 0
    work(shop, now + timedelta(minutes=61))
    got = components(shop.db)
    assert got["t"] == "cart_reminder" and [x["text"] for x in got["p"]] == ["Asha", "https://shop.example/checkouts/ck_abc", "Free shipping today"]
    assert shop.db.one("select state from cart_checkouts") == "sent"
    work(shop, now + timedelta(hours=5))
    assert shop.db.one("select count(*) from outbox") == 1                                          # once


def test_an_update_before_the_reminder_refreshes_the_cart_and_keeps_one_reminder(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], {**CHECKOUT, "phone": None, "total_price": "100.00"}, "checkouts/create", webhook_id="c1")
    assert shop.db.one("select count(*) from cart_checkouts") == 0                                  # no phone yet: nothing to remind
    shop.hook("shopify", c["token"], CHECKOUT, "checkouts/update", webhook_id="c2")
    shop.hook("shopify", c["token"], {**CHECKOUT, "total_price": "1500.00"}, "checkouts/update", webhook_id="c3")
    assert shop.db.one("select count(*) from cart_checkouts") == 1
    assert shop.db.one("select payload->>'total_price' from cart_checkouts") == "1500.00"
    work(shop, utcnow() + timedelta(hours=2))
    assert shop.db.one("select count(*) from outbox") == 1


def test_an_order_for_the_checkout_cancels_its_reminder(shop):
    c = shop.connect(config={"consent_attested": True, "events": {"abandoned_cart": CONFIG["events"]["abandoned_cart"]}})
    shop.hook("shopify", c["token"], CHECKOUT, "checkouts/create", webhook_id="c1")
    shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="o1")                       # the same checkout_id
    assert shop.db.one("select state from cart_checkouts") == "converted"
    work(shop, utcnow() + timedelta(hours=2))
    assert shop.db.one("select count(*) from outbox") == 0


def test_an_order_from_the_same_phone_also_cancels_it(shop):
    c = shop.connect(config={"consent_attested": True, "events": {"abandoned_cart": CONFIG["events"]["abandoned_cart"]}})
    shop.hook("shopify", c["token"], CHECKOUT, "checkouts/create", webhook_id="c1")
    other_checkout_order = {**ORDER, "checkout_id": 111}
    shop.hook("shopify", c["token"], other_checkout_order, "orders/create", webhook_id="o1")
    assert shop.db.one("select state from cart_checkouts") == "converted"


def test_a_reminder_is_dropped_if_the_person_opts_out_in_the_meantime(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], CHECKOUT, "checkouts/create", webhook_id="c1")
    shop.db.q("update contacts set opt_out = true, opted_in = false")
    work(shop, utcnow() + timedelta(hours=2))
    assert shop.db.one("select count(*) from outbox") == 0 and shop.db.one("select state from cart_checkouts") == "skipped"


def test_disabling_the_reminder_stops_one_already_waiting(shop):
    c = shop.connect()
    shop.hook("shopify", c["token"], CHECKOUT, "checkouts/create", webhook_id="c1")
    cfg = {**CONFIG, "events": {**CONFIG["events"], "abandoned_cart": {**CONFIG["events"]["abandoned_cart"], "enabled": False}}}
    shop.internal("/internal/integrations/update", integration_id=c["id"], config=cfg, by_agent_id=shop.boss["id"])
    work(shop, utcnow() + timedelta(hours=2))
    assert shop.db.one("select state from cart_checkouts") == "skipped" and shop.db.one("select count(*) from outbox") == 0


# ── isolation ────────────────────────────────────────────────────────────────

def test_a_workspace_sees_only_its_own_connections_and_a_store_only_writes_to_its_own(shop):
    c = shop.connect()
    other = H.tenant(shop.db, "shakti")
    assert shop.internal("/internal/integrations/list", tenant_id=str(other)).json()["integrations"] == []
    r = shop.internal("/internal/integrations/delete", tenant_id=str(other), integration_id=c["id"], by_agent_id=None)
    assert r.json()["deleted"] is False and shop.db.one("select count(*) from integrations") == 1
    shop.hook("shopify", c["token"], ORDER, "orders/create", webhook_id="w1")
    assert {r_["tenant_id"] for r_ in shop.db.q("select tenant_id from contacts")} == {shop.tid}
