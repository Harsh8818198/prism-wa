"""Chatbot-flow steps that sell: send a form, send products (one, a list, or the catalogue), and ask for payment for the cart the customer just sent."""
import json
import os
import time

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import commerce, flow_engine, flows, forms, payments
from prism_wa.vault import Vault

FORM = "33333333-3333-4333-8333-333333333333"


def flow(entry, **nodes):
    return {"entry": entry, "nodes": nodes}


def walk(graph, inputs=("hi",), **kw):
    return flow_engine.test(graph, None, list(inputs), **kw)


def said(result):
    return [t for t in result["transcript"] if t["from"] != "customer"]


def codes(issues):
    return {i.code for i in issues if i.severity == "error"}


@pytest.mark.parametrize("node,problem", [
    ({"type": "send_form", "form_id": "nope"}, "bad_form"), ({"type": "send_form", "form_id": FORM, "text": "x" * 1025}, "bad_text"),
    ({"type": "send_form", "form_id": FORM, "button": "x" * 31}, "bad_text"),
    ({"type": "send_products", "mode": "everything"}, "bad_products"), ({"type": "send_products", "mode": "product"}, "bad_products"),
    ({"type": "send_products", "mode": "products", "retailer_ids": []}, "bad_products"), ({"type": "send_products", "mode": "products", "retailer_ids": ["a"] * 31}, "bad_products"),
    ({"type": "send_products", "mode": "catalog", "text": "x" * 1025}, "bad_text"), ({"type": "request_payment", "empty_text": "x" * 1025}, "bad_text")])
def test_each_selling_step_checks_its_own_fields(node, problem):
    assert problem in codes(flows.validate(flow("a", a=node)))


@pytest.mark.parametrize("node", [{"type": "send_form", "form_id": FORM}, {"type": "send_form", "form_id": FORM, "text": "Hi", "button": "Open", "next": "b"},
                                  {"type": "send_products", "mode": "product", "retailer_id": "wall-6"}, {"type": "send_products", "mode": "products", "retailer_ids": ["a", "b"]},
                                  {"type": "send_products", "mode": "catalog"}, {"type": "request_payment"}])
def test_good_selling_steps_pass(node):
    assert codes(flows.validate(flow("a", a={**node, "next": "b"}, b={"type": "end"}))) == set()


def test_a_pretend_walk_shows_what_each_step_would_send():
    g = flow("a", a={"type": "send_form", "form_id": FORM, "text": "Hello {{first_name}}", "button": "Open", "next": "b"},
             b={"type": "send_products", "mode": "products", "retailer_ids": ["a", "b"], "text": "Pick", "next": "c"}, c={"type": "request_payment", "empty_text": "No cart", "next": "d"}, d={"type": "end"})
    out = said(walk(g, name="Asha Patel"))
    assert out[0] == {"from": "bot", "kind": "form", "form_id": FORM, "text": "Hello Asha", "button": "Open"}
    assert out[1]["kind"] == "products" and out[1]["mode"] == "products" and out[1]["retailer_ids"] == ["a", "b"] and out[2]["kind"] == "payment_request"


@pytest.fixture
def world(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    payments.configure(vault)
    sent = []

    def fake_razorpay(method, url, data, headers):
        sent.append((method, url.split("/v1/", 1)[-1]))
        if method == "POST" and url.endswith("payment_links"):
            return 200, json.dumps({"id": "plink_1", "short_url": "https://rzp.io/i/flow1", "status": "created"}).encode()
        return 200, b"{}"
    payments.TRANSPORT = fake_razorpay

    class W:
        db, tid_ = migrated, tid

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
            finally:
                c.close()

        @classmethod
        def shop(cls):
            cls.do(commerce.save_config, vault, {"gateway": "razorpay", "razorpay_key_id": "rzp_test_ABCDEFGH12345", "razorpay_secret": "s" * 20, "catalog_id": "5551234567", "tax_percent": 18})
            cls.do(commerce.save_product, {"retailer_id": "wall-6", "name": "Wall panel", "price": "1000"})

        @classmethod
        def flow(cls, graph, keyword="hi"):
            fid = cls.do(flows.create, "F", trigger={"type": "keyword", "match": "contains", "keywords": [keyword]}, graph=graph)
            cls.do(flows.publish, fid)
            cls.do(flows.set_enabled, fid, True)

        @classmethod
        def say(cls, wamid, text="hi"):
            H.deliver(migrated, P.inbound(P.text(text, wamid, ts=str(int(time.time())))))
            H.work(migrated)

        @classmethod
        def cart(cls, wamid="wamid.CART"):
            H.deliver(migrated, P.inbound({"from": P.ASHA, "id": wamid, "timestamp": str(int(time.time())), "type": "order", "order": {
                "catalog_id": "5551234567", "text": "", "product_items": [{"product_retailer_id": "wall-6", "quantity": 2, "item_price": 1000, "currency": "INR"}]}}))
            H.work(migrated)

        @classmethod
        def sent(cls):
            return [dict(r) for r in migrated.q("select kind, payload from outbox order by created_at, id")]

    yield W
    payments.configure(None)
    payments.TRANSPORT = None


def test_a_flow_sends_a_product_card_the_catalogue_and_a_form(world):
    world.shop()
    f = world.do(forms.create_form, {"template": "lead_capture"})
    world.db.q("update forms set status = 'published', meta_flow_id = 'FLOW9' where id = %s", (f["id"],))
    world.flow(flow("a", a={"type": "send_products", "mode": "product", "retailer_id": "wall-6", "text": "Our best seller", "next": "b"},
                    b={"type": "send_products", "mode": "catalog", "text": "Browse", "next": "c"}, c={"type": "send_form", "form_id": str(f["id"]), "text": "Tell us more", "button": "Open", "next": "d"},
                    d={"type": "end"}))
    world.say("wamid.F1")
    kinds = {s["payload"]["interactive"]["type"]: s["payload"]["interactive"] for s in world.sent()}
    assert set(kinds) == {"product", "catalog_message", "flow"}
    assert kinds["product"]["action"]["product_retailer_id"] == "wall-6" and kinds["product"]["body"]["text"] == "Our best seller"
    assert kinds["flow"]["action"]["parameters"]["flow_id"] == "FLOW9" and kinds["flow"]["action"]["parameters"]["flow_cta"] == "Open"


def test_a_flow_that_sends_an_unpublished_form_fails_safely_and_the_message_is_kept(world):
    f = world.do(forms.create_form, {"template": "lead_capture"})
    world.flow(flow("a", a={"type": "send_form", "form_id": str(f["id"]), "next": "b"}, b={"type": "end"}))
    world.say("wamid.F2")
    assert world.db.one("select count(*) from messages where wamid = 'wamid.F2'") == 1 and not [s for s in world.sent() if s["payload"].get("interactive", {}).get("type") == "flow"]


def test_asking_for_payment_uses_the_cart_the_customer_just_sent(world):
    world.shop()
    world.cart()
    world.flow(flow("a", a={"type": "request_payment", "empty_text": "Send me your cart first.", "next": "b"}, b={"type": "end"}), keyword="pay")
    world.say("wamid.P1", "pay")
    link = [s for s in world.sent() if s["payload"].get("interactive", {}).get("type") == "cta_url"]
    assert len(link) == 1 and link[0]["payload"]["interactive"]["action"]["parameters"]["url"] == "https://rzp.io/i/flow1"
    row = world.db.q("select status, total_paise from orders")[0]
    assert (row["status"], row["total_paise"]) == ("awaiting_payment", 236000)                 # 2 x 1000 + 18% tax
    assert world.db.one("select provider from payments") == "razorpay"


def test_with_no_cart_the_flow_says_so_and_carries_on(world):
    world.shop()
    world.flow(flow("a", a={"type": "request_payment", "empty_text": "Send me your cart first.", "next": "b"}, b={"type": "send_text", "text": "Anything else?", "next": "c"}, c={"type": "end"}), keyword="pay")
    world.say("wamid.P2", "pay")
    texts = [s["payload"]["text"]["body"] for s in world.sent() if s["kind"] == "text"]
    assert sorted(texts) == ["Anything else?", "Send me your cart first."] and world.db.one("select count(*) from payments") == 0


def test_a_shop_that_is_not_ready_does_not_cost_the_customers_message(world):
    world.cart()
    world.flow(flow("a", a={"type": "request_payment", "next": "b"}, b={"type": "end"}), keyword="pay")
    world.say("wamid.P3", "pay")
    assert world.db.one("select count(*) from messages where wamid = 'wamid.P3'") == 1 and world.db.one("select count(*) from payments") == 0
