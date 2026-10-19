"""The shop: catalogue, product messages, carts, orders, payments (Meta's and Razorpay's), refunds, the dashboard — against a real Postgres, a fake Meta and a fake Razorpay.

What this proves: the sequences, the money arithmetic, the states and the bookkeeping. It does NOT prove Meta accepts the exact field names of order_details or the catalogue
batch (commerce.py and payments.py say which parts are written from Meta's documentation and still to be seen live).
"""
import hashlib
import hmac
import json
import os
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import commerce, payments, plans, razorpay
from prism_wa.app import create_app
from prism_wa.commerce import CommerceError
from prism_wa.graph import Graph
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

KEY_ID, KEY_SECRET, HOOK_SECRET = "rzp_test_ABCDEFGH12345", "secret-for-razorpay-key", "whsec-for-razorpay-hooks"


class FakeMeta:
    def __init__(self):
        self.calls, self.catalog_pages, self.payment_reply = [], [], {}

    def __call__(self, method, url, data, headers):
        parts = urlsplit(url)
        path = parts.path.split("/v25.0/", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(data) if data and headers.get("Content-Type", "").startswith("application/json") else None
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        if method == "GET" and path.endswith("/products"):
            page = int(query.get("after") or 0)
            rows = self.catalog_pages[page] if page < len(self.catalog_pages) else []
            paging = {"cursors": {"after": str(page + 1)}, "next": "x"} if page + 1 < len(self.catalog_pages) else {}
            return 200, json.dumps({"data": rows, "paging": paging}).encode()
        if method == "GET" and path.endswith("/product_catalogs"):
            return 200, json.dumps({"data": [{"id": "5551234567", "name": "Shakti shop"}]}).encode()
        if method == "GET" and path.endswith("/whatsapp_commerce_settings"):
            return 200, json.dumps({"data": [{"is_catalog_visible": True, "is_cart_enabled": False}]}).encode()
        if method == "GET" and "/payments/" in path:
            return 200, json.dumps(self.payment_reply).encode()
        return 200, json.dumps({"success": True}).encode()


class FakeRazorpay:
    def __init__(self):
        self.calls, self.link_status, self.payments, self.fail = [], "created", [], None

    def __call__(self, method, url, data, headers):
        path = url.split("/v1/", 1)[-1]
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "path": path, "body": body, "auth": headers.get("Authorization")})
        if self.fail:
            return 400, json.dumps({"error": {"code": "BAD_REQUEST_ERROR", "description": self.fail}}).encode()
        if method == "POST" and path == "payment_links":
            return 200, json.dumps({"id": "plink_%d" % len(self.calls), "short_url": "https://rzp.io/i/abc%d" % len(self.calls), "status": "created"}).encode()
        if method == "GET" and path.startswith("payment_links/"):
            return 200, json.dumps({"id": path.split("/")[1], "status": self.link_status, "payments": self.payments}).encode()
        if method == "POST" and path.endswith("/cancel"):
            return 200, json.dumps({"status": "cancelled"}).encode()
        if method == "POST" and path.endswith("/refund"):
            return 200, json.dumps({"id": "rfnd_%d" % len(self.calls), "status": "processed", "amount": body["amount"]}).encode()
        return 404, b"{}"


@pytest.fixture
def world(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    blob, ver = vault.encrypt("EAAB-business-token-for-the-waba", context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    meta, rz = FakeMeta(), FakeRazorpay()
    payments.TRANSPORT = rz
    graph = Graph(meta)
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok", graph=graph, vault=vault)
    client = TestClient(app)

    class W:
        db, tid_, vault_, meta_, rz_, graph_, number_ = migrated, tid, vault, meta, rz, graph, number

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

        @classmethod
        def person(cls, name="Asha Shah", phone="919800100001", window=True, tags=()):
            c = cls.conn()
            with c.cursor() as cur:
                cur.execute("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s, %s, %s, %s, true) returning id", (tid, name, phone, list(tags)))
                cid = cur.fetchone()[0]
                cur.execute("insert into conversations (tenant_id, contact_id, channel, last_active_at, last_inbound_at, number_id) values (%s, %s, 'whatsapp', now(), %s, %s)",
                             (tid, cid, utcnow() if window else utcnow() - timedelta(days=3), number))
            c.commit()
            c.close()
            return cid

        @classmethod
        def configure(cls, **kw):
            base = {"gateway": "razorpay", "razorpay_key_id": KEY_ID, "razorpay_secret": KEY_SECRET, "razorpay_webhook_secret": HOOK_SECRET, "catalog_id": "5551234567",
                    "tax_percent": 18, "shipping": "50"}
            base.update(kw)
            return cls.do(commerce.save_config, vault, base)

        @classmethod
        def products(cls):
            cls.do(commerce.save_product, {"retailer_id": "wall-6", "name": "Boundary wall panel", "price": "1,299.50", "category": "Walls"})
            cls.do(commerce.save_product, {"retailer_id": "gate-1", "name": "Main gate", "price": "8000", "sale_price": "7500", "category": "Gates"})
            cls.do(commerce.save_product, {"retailer_id": "paint-1", "name": "Weather paint", "price": "2200", "category": "Finish", "available": False})

        @classmethod
        def outbox(cls):
            return [dict(r) for r in migrated.q("select kind, payload, to_phone, state from outbox order by created_at, id")]

        @classmethod
        def deliver(cls, payload):
            H.deliver(migrated, payload)
            H.work(migrated)

    yield W
    payments.TRANSPORT = None


def cart(wamid="wamid.CART1", items=None, text="Please send these"):
    return {"from": P.ASHA, "id": wamid, "timestamp": "1760000010", "type": "order",
            "order": {"catalog_id": "5551234567", "text": text, "product_items": items if items is not None else [
                {"product_retailer_id": "wall-6", "quantity": 4, "item_price": 1299.5, "currency": "INR"}, {"product_retailer_id": "gate-1", "quantity": 1, "item_price": 7500, "currency": "INR"}]}}


def payment_status(reference, status="captured", tx_status="success", wamid="wamid.OD1", method="upi", ts="1760000100", txn="pg_123"):
    return P.statuses({"id": wamid, "recipient_id": P.ASHA, "type": "payment", "status": status, "timestamp": ts,
                       "payment": {"reference_id": reference, "amount": {"value": 1, "offset": 100}, "currency": "INR",
                                   "transaction": {"id": "txn_1", "pg_transaction_id": txn, "status": tx_status, "method": {"type": method}}}})


# ── money ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [("499", 49900), ("1,299.50", 129950), ("Rs 499", 49900), ("₹ 10.005", 1001), (0, 0), (12, 1200), ("0.01", 1), ("INR 99.99", 9999)])
def test_amounts_become_whole_paise_and_round_half_up(value, expected):
    assert commerce.paise(value) == expected


@pytest.mark.parametrize("value", ["abc", "-5", "", None, True, "1e400", "999999999999"])
def test_a_bad_amount_is_refused_in_words(value):
    with pytest.raises(CommerceError) as why:
        commerce.paise(value)
    assert why.value.reason == "bad_amount" and str(why.value)


def test_an_orders_parts_always_add_up_to_its_total_to_the_paisa():
    items = [{"line_paise": 33333}, {"line_paise": 1}]
    for tax in (0, 5, 12, 18, 28, "7.5"):
        for shipping in (0, 4999):
            for discount in (0, 500, 99999999):
                t = commerce.totals(items, tax_percent=tax, shipping_paise=shipping, discount_paise=discount)
                assert t["total_paise"] == t["subtotal_paise"] - t["discount_paise"] + t["tax_paise"] + t["shipping_paise"]
                assert 0 <= t["discount_paise"] <= t["subtotal_paise"]


def test_tax_is_worked_out_after_the_discount():
    t = commerce.totals([{"line_paise": 100000}], tax_percent=18, shipping_paise=5000, discount_paise=10000)
    assert t == {"subtotal_paise": 100000, "discount_paise": 10000, "tax_paise": 16200, "shipping_paise": 5000, "total_paise": 111200}


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_are_saved_secrets_are_encrypted_and_never_come_back(world):
    cfg = world.configure()
    assert cfg["gateway"] == "razorpay" and cfg["razorpay_key_id"] == KEY_ID and cfg["razorpay_secret_set"] and cfg["razorpay_webhook_set"] and cfg["ready"]
    assert cfg["tax_percent"] == 18 and cfg["shipping_paise"] == 5000 and cfg["catalog_id"] == "5551234567"
    assert KEY_SECRET not in json.dumps(cfg, default=str) and HOOK_SECRET not in json.dumps(cfg, default=str)
    raw = bytes(world.db.one("select razorpay_secret_enc from commerce_config"))
    assert KEY_SECRET.encode() not in raw
    again = world.do(commerce.save_config, world.vault_, {"tax_percent": 5})                       # leaving a secret out keeps the stored one
    assert again["razorpay_secret_set"] and again["tax_percent"] == 5
    assert world.do(commerce.gateway_secrets, world.vault_)["secret"] == KEY_SECRET


@pytest.mark.parametrize("change,reason", [({"catalog_id": "abc"}, "bad_catalog"), ({"currency": "rupees"}, "bad_currency"), ({"tax_percent": 120}, "bad_tax"),
                                           ({"tax_percent": "x"}, "bad_tax"), ({"order_expiry_hours": 0}, "bad_expiry"), ({"gateway": "paypal"}, "bad_gateway"),
                                           ({"razorpay_key_id": "key_123"}, "bad_key"), ({"shipping": "-1"}, "bad_amount")])
def test_settings_are_checked_in_words(world, change, reason):
    with pytest.raises(CommerceError) as why:
        world.do(commerce.save_config, world.vault_, change)
    assert why.value.reason == reason and str(why.value)


def test_the_shop_is_not_ready_until_the_chosen_way_of_paying_is_complete(world):
    assert not world.do(commerce.get_config)["ready"]
    assert not world.do(commerce.save_config, world.vault_, {"gateway": "meta"})["ready"]
    assert world.do(commerce.save_config, world.vault_, {"gateway": "meta", "payment_configuration": "shakti-upi"})["ready"]


# ── products ─────────────────────────────────────────────────────────────────

def test_a_product_is_saved_by_its_retailer_id_and_saving_again_updates_it(world):
    a = world.do(commerce.save_product, {"retailer_id": "wall-6", "name": "Wall", "price": "100"})
    b = world.do(commerce.save_product, {"retailer_id": "wall-6", "name": "Wall panel", "price": "120.50", "category": "Walls"})
    assert a["id"] == b["id"] and b["name"] == "Wall panel" and b["price_paise"] == 12050
    assert world.do(commerce.list_products)["total"] == 1


@pytest.mark.parametrize("bad,reason", [({"name": "", "price": 1}, "no_name"), ({"name": "x", "price": "ten"}, "bad_amount"), ({"name": "x", "price": 10, "sale_price": 20}, "bad_amount"),
                                         ({"name": "x", "price": 10, "image_url": "ftp://a"}, "bad_link")])
def test_a_bad_product_is_refused(world, bad, reason):
    with pytest.raises(CommerceError) as why:
        world.do(commerce.save_product, bad)
    assert why.value.reason == reason


def test_a_catalogue_csv_with_metas_own_headers_is_imported_and_bad_rows_are_reported(world):
    text = ("id,title,description,availability,condition,price,link,image_link,sale_price,product_type\n"
            "wall-6,Wall panel,Strong,in stock,new,1299.50 INR,https://s.example/w,https://s.example/w.jpg,,Walls\n"
            "gate-1,Gate,Big,in stock,new,8000 INR,,,7500 INR,Gates\n"
            "bad-1,Broken,,in stock,new,not a price,,,,\n"
            "old-1,Out of stock thing,,out of stock,new,10 INR,,,,\n")
    out = world.do(commerce.import_csv, text)
    assert out["added"] == 3 and out["skipped"] == 1 and out["problems"][0]["row"] == 4
    rows = {p["retailer_id"]: p for p in world.do(commerce.list_products)["products"]}
    assert rows["gate-1"]["sale_price_paise"] == 750000 and rows["old-1"]["available"] is False and rows["wall-6"]["price_paise"] == 129950
    again = world.do(commerce.import_csv, text)
    assert again["added"] == 0 and again["updated"] == 3


def test_a_csv_without_a_price_column_is_refused(world):
    with pytest.raises(CommerceError):
        world.do(commerce.import_csv, "title\nx\n")


def test_products_can_be_found_by_words_category_and_stock(world):
    world.products()
    assert [p["retailer_id"] for p in world.do(commerce.list_products, q="gate")["products"]] == ["gate-1"]
    assert [p["retailer_id"] for p in world.do(commerce.list_products, category="Walls")["products"]] == ["wall-6"]
    assert [p["retailer_id"] for p in world.do(commerce.list_products, available=False)["products"]] == ["paint-1"]
    assert world.do(commerce.list_products)["categories"] == ["Finish", "Gates", "Walls"]


def test_pulling_the_meta_catalogue_reads_every_page_and_updates_what_it_knows(world):
    world.configure()
    world.do(commerce.save_product, {"retailer_id": "wall-6", "name": "Old name", "price": "1"})
    world.meta_.catalog_pages = [[{"id": "m1", "retailer_id": "wall-6", "name": "Wall panel", "price": "₹1,299.50", "availability": "in stock", "image_url": "https://x.example/a.jpg"}],
                                 [{"id": "m2", "retailer_id": "gate-1", "name": "Gate", "price": "8000.00 INR", "sale_price": "7500.00 INR", "availability": "out of stock"},
                                  {"id": "m3", "retailer_id": "bad", "name": "No price"}]]
    out = world.do(commerce.pull_catalog, world.graph_, world.vault_)
    assert out == {"added": 1, "updated": 1}
    rows = {p["retailer_id"]: p for p in world.do(commerce.list_products)["products"]}
    assert rows["wall-6"]["name"] == "Wall panel" and rows["wall-6"]["price_paise"] == 129950 and rows["wall-6"]["meta_product_id"] == "m1"
    assert rows["gate-1"]["sale_price_paise"] == 750000 and rows["gate-1"]["available"] is False and "bad" not in rows
    assert [c["query"].get("after") for c in world.meta_.calls if c["path"].endswith("/products")] == [None, "1"]


def test_pushing_sends_prices_as_whole_paise_with_the_currency(world):
    world.configure()
    world.products()
    out = world.do(commerce.push_catalog, world.graph_, world.vault_)
    call = [c for c in world.meta_.calls if c["path"].endswith("/items_batch")][0]
    assert out == {"sent": 3} and call["body"]["item_type"] == "PRODUCT_ITEM"
    sent = {r["data"]["id"]: r["data"] for r in call["body"]["requests"]}
    assert sent["wall-6"]["price"] == 129950 and sent["wall-6"]["currency"] == "INR" and sent["gate-1"]["sale_price"] == 750000 and sent["paint-1"]["availability"] == "out of stock"


def test_the_catalogue_needs_an_id_before_it_is_read_or_pushed(world):
    for fn in (commerce.pull_catalog, commerce.push_catalog):
        with pytest.raises(CommerceError) as why:
            world.do(fn, world.graph_, world.vault_)
        assert why.value.reason == "no_catalog"


# ── product messages ─────────────────────────────────────────────────────────

def test_a_single_product_is_sent_as_a_product_message(world):
    world.configure()
    world.products()
    who = world.person()
    world.do(commerce.send_product, contact_id=who, retailer_id="wall-6", body="This one?", footer="Free site visit", idem_key="k1")
    sent = world.outbox()[-1]
    assert sent["kind"] == "interactive" and sent["payload"]["interactive"]["type"] == "product"
    assert sent["payload"]["interactive"]["action"] == {"catalog_id": "5551234567", "product_retailer_id": "wall-6"} and sent["payload"]["interactive"]["footer"]["text"] == "Free site visit"


def test_a_product_that_is_not_in_the_catalogue_is_not_sent(world):
    world.configure()
    with pytest.raises(CommerceError) as why:
        world.do(commerce.send_product, contact_id=world.person(), retailer_id="ghost", idem_key="k")
    assert why.value.reason == "unknown_product"


def test_a_list_of_products_is_grouped_and_limited_to_what_whatsapp_allows(world):
    world.configure()
    world.products()
    who = world.person()
    world.do(commerce.send_products, contact_id=who, sections=[{"title": "Walls", "retailer_ids": ["wall-6"]}, {"title": "Gates", "retailer_ids": ["gate-1"]}], idem_key="k2")
    action = world.outbox()[-1]["payload"]["interactive"]["action"]
    assert [s["title"] for s in action["sections"]] == ["Walls", "Gates"] and action["sections"][0]["product_items"] == [{"product_retailer_id": "wall-6"}]
    for sections in ([], [{"title": "x", "retailer_ids": []}], [{"title": "t%d" % i, "retailer_ids": ["wall-6"]} for i in range(11)], [{"title": "big", "retailer_ids": ["wall-6"] * 31}]):
        with pytest.raises(CommerceError) as why:
            world.do(commerce.send_products, contact_id=who, sections=sections, idem_key="k3")
        assert why.value.reason == "bad_sections"


def test_the_whole_catalogue_can_be_offered(world):
    world.configure()
    who = world.person()
    world.do(commerce.send_catalog, contact_id=who, body="Browse", thumbnail_retailer_id="wall-6", idem_key="k4")
    i = world.outbox()[-1]["payload"]["interactive"]
    assert i["type"] == "catalog_message" and i["action"]["parameters"] == {"thumbnail_product_retailer_id": "wall-6"}


def test_product_messages_follow_the_24_hour_rule(world):
    world.configure()
    world.products()
    from prism_wa import send
    with pytest.raises(send.SendRefused) as why:
        world.do(commerce.send_product, contact_id=world.person(window=False), retailer_id="wall-6", idem_key="k5")
    assert why.value.reason == "window_closed"


# ── a cart that arrives ──────────────────────────────────────────────────────

def test_a_cart_from_whatsapp_becomes_a_draft_order_priced_from_the_catalogue_once(world):
    world.configure()
    world.products()
    world.deliver(P.inbound(cart()))
    world.deliver(P.inbound(cart()))                                     # Meta redelivers: still one order
    orders = world.do(commerce.list_orders)
    assert orders["total"] == 1
    o = world.do(commerce.get_order, orders["orders"][0]["id"])
    assert o["source"] == "cart" and o["status"] == "draft" and o["reference_id"].startswith("WA")
    assert [(i["retailer_id"], i["quantity"], i["unit_paise"]) for i in o["items"]] == [("wall-6", 4, 129950), ("gate-1", 1, 750000)]
    assert o["subtotal_paise"] == 4 * 129950 + 750000 and o["tax_paise"] == round(o["subtotal_paise"] * 0.18) and o["shipping_paise"] == 5000
    assert o["total_paise"] == o["subtotal_paise"] + o["tax_paise"] + o["shipping_paise"] and o["note"] == "Please send these"
    assert "cart" in world.db.one("select tags from contacts where phone = %s", (P.ASHA,))


def test_a_cart_with_no_known_items_makes_no_order(world):
    world.deliver(P.inbound(cart(wamid="wamid.EMPTY", items=[])))
    assert world.do(commerce.list_orders)["total"] == 0


def test_a_cart_for_a_product_we_do_not_have_is_kept_with_the_price_whatsapp_sent(world):
    world.deliver(P.inbound(cart(wamid="wamid.NEW", items=[{"product_retailer_id": "mystery", "quantity": 2, "item_price": 250, "currency": "INR"}])))
    o = world.do(commerce.get_order, world.do(commerce.list_orders)["orders"][0]["id"])
    assert o["items"][0]["name"] == "mystery" and o["items"][0]["unit_paise"] == 25000 and o["subtotal_paise"] == 50000


# ── orders ───────────────────────────────────────────────────────────────────

def make_order(world, **kw):
    world.configure(**kw.pop("config", {}))
    world.products()
    who = kw.pop("who", None) or world.person()
    return world.do(commerce.create_order, {"contact_id": who, "items": [{"retailer_id": "wall-6", "quantity": 2}, {"retailer_id": "gate-1", "quantity": 1}], **kw}), who


def test_an_order_takes_prices_from_the_catalogue_and_uses_the_sale_price(world):
    o, _ = make_order(world)
    assert [i["unit_paise"] for i in o["items"]] == [129950, 750000] and o["subtotal_paise"] == 1009900 and o["status"] == "draft" and len(o["reference_id"]) <= 35


def test_an_order_can_be_made_by_hand_with_its_own_charges(world):
    world.configure()
    who = world.person()
    o = world.do(commerce.create_order, {"contact_id": who, "items": [{"name": "Site survey", "quantity": 1, "price": "1500"}], "shipping": "0", "discount": "100", "tax_percent": 0,
                                         "reference_id": "SURVEY_1", "note": "Vadodara"})
    assert o["reference_id"] == "SURVEY_1" and o["total_paise"] == 140000 and o["discount_paise"] == 10000 and o["note"] == "Vadodara"


@pytest.mark.parametrize("items,reason", [([], "no_items"), ("x", "no_items"), ([{"retailer_id": "wall-6", "quantity": 0}], "bad_quantity"), ([{"quantity": 1}], "bad_item"),
                                           ([{"retailer_id": "ghost", "quantity": 1}], "unknown_product"), ([{"name": "x", "quantity": 1}], "bad_amount")])
def test_a_bad_order_is_refused_in_words(world, items, reason):
    world.configure()
    with pytest.raises(CommerceError) as why:
        world.do(commerce.create_order, {"contact_id": world.person(), "items": items})
    assert why.value.reason == reason and str(why.value)


def test_an_order_can_be_changed_until_a_payment_is_asked_for(world):
    o, _ = make_order(world)
    o2 = world.do(commerce.update_order, o["id"], {"items": [{"retailer_id": "wall-6", "quantity": 1}], "discount": "10"})
    assert o2["subtotal_paise"] == 129950 and o2["discount_paise"] == 1000
    world.do(payments.request_payment, world.vault_, order_id=o["id"])
    with pytest.raises(CommerceError) as why:
        world.do(commerce.update_order, o["id"], {"note": "late"})
    assert why.value.reason == "order_locked"


def test_an_order_only_moves_the_ways_the_shop_allows(world):
    o, _ = make_order(world)
    with pytest.raises(CommerceError) as why:
        world.do(commerce.set_status, o["id"], "shipped")
    assert why.value.reason == "bad_move"
    with pytest.raises(CommerceError):
        world.do(commerce.set_status, o["id"], "teleported")
    assert world.do(commerce.set_status, o["id"], "cancelled", notify=False)["status"] == "cancelled"
    with pytest.raises(CommerceError):
        world.do(commerce.set_status, o["id"], "draft")


def test_orders_are_listed_with_counts_and_found_by_reference_or_name(world):
    a, who = make_order(world)
    b = world.do(commerce.create_order, {"contact_id": who, "items": [{"retailer_id": "gate-1", "quantity": 1}]})
    world.do(commerce.set_status, b["id"], "cancelled", notify=False)
    out = world.do(commerce.list_orders)
    assert out["total"] == 2 and out["counts"] == {"draft": 1, "cancelled": 1}
    assert [o["id"] for o in world.do(commerce.list_orders, status="cancelled")["orders"]] == [b["id"]]
    assert world.do(commerce.list_orders, q=a["reference_id"][:8])["total"] >= 1 and world.do(commerce.list_orders, q="Asha")["total"] == 2


# ── payments: Razorpay links ─────────────────────────────────────────────────

def test_asking_for_payment_makes_a_link_and_sends_it_as_a_button(world):
    o, who = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert p["provider"] == "razorpay" and p["status"] == "pending" and p["amount_paise"] == o["total_paise"] and p["link_url"].startswith("https://rzp.io/")
    made = [c for c in world.rz_.calls if c["path"] == "payment_links"][0]
    assert made["body"]["amount"] == o["total_paise"] and made["body"]["currency"] == "INR" and made["body"]["reference_id"] == o["reference_id"] and made["auth"].startswith("Basic ")
    assert made["body"]["customer"]["contact"] == "+919800100001" and made["body"]["expire_by"] > 0
    msg = world.outbox()[-1]["payload"]["interactive"]
    assert msg["type"] == "cta_url" and msg["action"]["parameters"]["url"] == p["link_url"] and "Pay" in msg["action"]["parameters"]["display_text"]
    assert world.do(commerce.get_order, o["id"])["status"] == "awaiting_payment"
    assert "payment-pending" in world.db.one("select tags from contacts where id = %s", (who,))


def test_a_second_request_replaces_the_open_one_and_cancels_its_link(world):
    o, _ = make_order(world)
    first = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    second = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert world.do(payments.get_payment, first["id"])["status"] == "cancelled" and second["status"] == "pending"
    assert any(c["path"].endswith("/cancel") for c in world.rz_.calls)
    assert [c["body"]["reference_id"] for c in world.rz_.calls if c["path"] == "payment_links"] == [o["reference_id"], o["reference_id"] + "-2"]


def test_the_shop_must_be_ready_and_the_order_payable(world):
    world.products()
    who = world.person()
    o = world.do(commerce.create_order, {"contact_id": who, "items": [{"retailer_id": "wall-6", "quantity": 1}]})
    with pytest.raises(CommerceError) as why:
        world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert why.value.reason == "not_ready"
    world.configure()
    nobody = world.do(commerce.create_order, {"items": [{"retailer_id": "wall-6", "quantity": 1}]})
    with pytest.raises(CommerceError) as why:
        world.do(payments.request_payment, world.vault_, order_id=nobody["id"])
    assert why.value.reason == "no_contact"
    world.do(commerce.set_status, o["id"], "cancelled", notify=False)
    with pytest.raises(CommerceError) as why:
        world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert why.value.reason == "order_locked"


def test_only_a_link_can_be_made_without_sending_it(world):
    o, _ = make_order(world)
    before = len(world.outbox())
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"], send_message=False)
    assert p["link_url"] and len(world.outbox()) == before


def signed(payload: dict, secret=HOOK_SECRET):
    body = json.dumps(payload).encode()
    return body, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def paid_event(link_id, payment_id="pay_1", method="upi"):
    return {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {"id": link_id, "status": "paid", "payments": [{"payment_id": payment_id, "status": "captured"}]}},
                                                      "payment": {"entity": {"id": payment_id, "method": method}}}}


def test_razorpay_telling_us_a_link_was_paid_marks_the_order_paid_and_thanks_the_customer(world):
    o, who = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    body, sig = signed(paid_event(p["provider_ref"]))
    out = world.do(payments.razorpay_webhook, world.vault_, body, sig)
    assert out["handled"] and world.do(commerce.get_order, o["id"])["status"] == "paid"
    got = world.do(payments.get_payment, p["id"])
    assert got["status"] == "captured" and got["method"] == "upi" and got["txn_id"] == "pay_1" and got["paid_at"]
    tags = world.db.one("select tags from contacts where id = %s", (who,))
    assert "customer" in tags and "payment-pending" not in tags
    assert "received" in world.outbox()[-1]["payload"]["text"]["body"]
    assert world.do(payments.razorpay_webhook, world.vault_, body, sig)["handled"] and len([r for r in world.outbox() if r["kind"] == "text"]) == 1      # twice: still one thank-you


def test_a_notification_that_does_not_carry_razorpays_signature_is_refused(world):
    o, _ = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    body, _sig = signed(paid_event(p["provider_ref"]))
    for sig in ("", "deadbeef", signed(paid_event(p["provider_ref"]), secret="someone-elses-secret")[1]):
        with pytest.raises(CommerceError) as why:
            world.do(payments.razorpay_webhook, world.vault_, body, sig)
        assert why.value.reason == "bad_signature"
    assert world.do(commerce.get_order, o["id"])["status"] == "awaiting_payment"


def test_a_notification_for_a_payment_we_do_not_know_is_ignored(world):
    world.configure()
    body, sig = signed(paid_event("plink_unknown"))
    assert world.do(payments.razorpay_webhook, world.vault_, body, sig)["handled"] is False


def test_a_dropped_webhook_is_caught_by_asking_razorpay(world):
    o, _ = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.rz_.link_status, world.rz_.payments = "paid", [{"payment_id": "pay_9", "status": "captured"}]
    now = utcnow() + timedelta(minutes=10)
    c = world.conn()
    with c.cursor() as cur:
        changed = payments.reconcile(cur, graph=world.graph_, vault=world.vault_, now=now)
    c.commit()
    c.close()
    assert changed == 1 and world.do(payments.get_payment, p["id"])["status"] == "captured" and world.do(commerce.get_order, o["id"])["status"] == "paid"
    assert world.do(payments.get_payment, p["id"])["txn_id"] == "pay_9"


def test_a_payment_still_open_is_left_alone_and_a_gateway_outage_is_remembered_not_fatal(world):
    o, _ = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.rz_.fail = "Razorpay is having a bad day"
    c = world.conn()
    with c.cursor() as cur:
        assert payments.reconcile(cur, graph=world.graph_, vault=world.vault_, now=utcnow() + timedelta(minutes=10)) == 0
    c.commit()
    c.close()
    got = world.do(payments.get_payment, p["id"])
    assert got["status"] == "pending" and "bad day" in got["last_error"] and got["checked_at"]


def test_an_unpaid_request_expires_and_the_order_can_be_asked_again(world):
    o, who = make_order(world, config={"order_expiry_hours": 1})
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    c = world.conn()
    with c.cursor() as cur:
        payments.reconcile(cur, graph=world.graph_, vault=world.vault_, now=utcnow() + timedelta(hours=2))
    c.commit()
    c.close()
    assert world.do(payments.get_payment, p["id"])["status"] == "expired" and world.do(commerce.get_order, o["id"])["status"] == "failed"
    assert "payment-failed" in world.db.one("select tags from contacts where id = %s", (who,))
    again = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert again["status"] == "pending"


def test_money_that_arrives_after_a_request_expired_still_counts(world):
    o, _ = make_order(world, config={"order_expiry_hours": 1})
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    c = world.conn()
    with c.cursor() as cur:
        payments.reconcile(cur, graph=world.graph_, vault=world.vault_, now=utcnow() + timedelta(hours=2))
    c.commit()
    c.close()
    body, sig = signed(paid_event(p["provider_ref"]))
    world.do(payments.razorpay_webhook, world.vault_, body, sig)
    assert world.do(payments.get_payment, p["id"])["status"] == "captured" and world.do(commerce.get_order, o["id"])["status"] == "paid"


# ── payments: Meta's own ─────────────────────────────────────────────────────

def meta_order(world):
    return make_order(world, config={"gateway": "meta", "payment_configuration": "shakti-upi", "razorpay_key_id": KEY_ID, "razorpay_secret": KEY_SECRET})


def test_meta_payment_is_asked_for_with_an_order_details_message_whose_parts_add_up(world):
    o, _ = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    assert p["provider"] == "meta" and p["status"] == "pending" and not world.rz_.calls
    i = world.outbox()[-1]["payload"]["interactive"]
    params = i["action"]["parameters"]
    assert i["type"] == "order_details" and i["action"]["name"] == "review_and_pay" and params["reference_id"] == o["reference_id"] and params["payment_configuration"] == "shakti-upi"
    order = params["order"]
    assert params["total_amount"] == {"value": o["total_paise"], "offset": 100} and params["currency"] == "INR"
    total = order["subtotal"]["value"] - (order.get("discount") or {"value": 0})["value"] + order["tax"]["value"] + (order.get("shipping") or {"value": 0})["value"]
    assert total == params["total_amount"]["value"]
    assert [(x["quantity"], x["amount"]["value"]) for x in order["items"]] == [(2, 129950), (1, 750000)] and params["type"] == "physical-goods"
    assert int(order["expiration"]["timestamp"]) > int(utcnow().timestamp())


def test_meta_telling_us_the_payment_was_captured_marks_the_order_paid(world):
    o, who = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "captured"))
    got = world.do(payments.get_payment, p["id"])
    assert got["status"] == "captured" and got["method"] == "upi" and got["txn_id"] == "pg_123" and got["wamid"] == "wamid.OD1"
    assert world.do(commerce.get_order, o["id"])["status"] == "paid"


def test_a_failed_payment_does_not_undo_a_captured_one_and_news_may_come_in_any_order(world):
    o, _ = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "captured", ts="1760000200", wamid="wamid.OD2"))
    world.deliver(payment_status(o["reference_id"], "failed", tx_status="failed", ts="1760000100", wamid="wamid.OD3"))
    world.deliver(payment_status(o["reference_id"], "pending", tx_status="pending", ts="1760000050", wamid="wamid.OD4"))
    assert world.do(payments.get_payment, p["id"])["status"] == "captured" and world.do(commerce.get_order, o["id"])["status"] == "paid"


def test_a_failed_attempt_marks_the_order_failed_and_the_customer_for_a_reminder(world):
    o, who = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "failed", tx_status="failed"))
    assert world.do(payments.get_payment, p["id"])["status"] == "failed" and world.do(commerce.get_order, o["id"])["status"] == "failed"
    assert "payment-failed" in world.db.one("select tags from contacts where id = %s", (who,))
    assert [r["contact_id"] for r in world.do(payments.retarget, older_than_minutes=0)] == [who]


def test_the_customer_can_try_again_after_a_failure_and_a_late_success_is_counted(world):
    o, _ = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "failed", tx_status="failed", ts="1760000100", wamid="wamid.X1"))
    world.deliver(payment_status(o["reference_id"], "captured", ts="1760000200", wamid="wamid.X2"))
    assert world.do(payments.get_payment, p["id"])["status"] == "captured"


def test_a_payment_status_for_an_unknown_reference_is_ignored(world):
    world.configure(gateway="meta", payment_configuration="x")
    world.deliver(payment_status("NOT-OURS"))
    assert world.db.one("select count(*) from payments") == 0


def test_meta_is_asked_where_a_payment_stands_when_its_webhook_is_missing(world):
    o, _ = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.meta_.payment_reply = {"status": "captured", "payment": {"reference_id": o["reference_id"], "transaction": {"pg_transaction_id": "pg_77", "status": "success", "method": {"type": "upi"}}}}
    got = world.do(payments.check, world.vault_, world.graph_, p["id"])
    assert got["status"] == "captured" and got["txn_id"] == "pg_77"
    assert any(c["method"] == "GET" and ("payments/%s" % o["reference_id"]) in c["path"] for c in world.meta_.calls)


def test_the_meta_order_status_message_is_used_once_paid_and_the_chat_is_told_in_words(world):
    o, _ = meta_order(world)
    world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "captured"))
    world.do(commerce.set_status, o["id"], "shipped", note="Courier: Blue Dart")
    last = world.outbox()[-1]["payload"]["interactive"]
    assert last["type"] == "order_status" and last["action"]["parameters"]["order"]["status"] == "shipped" and last["action"]["parameters"]["reference_id"] == o["reference_id"]


def test_a_razorpay_paid_order_is_told_its_progress_in_plain_text(world):
    o, _ = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    body, sig = signed(paid_event(p["provider_ref"]))
    world.do(payments.razorpay_webhook, world.vault_, body, sig)
    world.do(commerce.set_status, o["id"], "shipped", note="Arriving Friday.")
    assert "has been shipped. Arriving Friday." in world.outbox()[-1]["payload"]["text"]["body"]


# ── refunds ──────────────────────────────────────────────────────────────────

def paid(world):
    o, who = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    body, sig = signed(paid_event(p["provider_ref"]))
    world.do(payments.razorpay_webhook, world.vault_, body, sig)
    return o, p


def test_part_of_a_payment_is_refunded_through_razorpay_then_the_rest(world):
    o, p = paid(world)
    got = world.do(payments.refund, world.vault_, payment_id=p["id"], amount_paise=10000, speed="instant", reason="Damaged panel")
    call = [c for c in world.rz_.calls if c["path"] == "payments/pay_1/refund"][0]
    assert call["body"]["amount"] == 10000 and call["body"]["speed"] == "optimum" and got["status"] == "partially_refunded" and got["refunded_paise"] == 10000
    assert got["refunds"][0]["status"] == "processed" and got["refunds"][0]["reason"] == "Damaged panel"
    done = world.do(payments.refund, world.vault_, payment_id=p["id"])
    assert done["status"] == "refunded" and done["refunded_paise"] == p["amount_paise"] and world.do(commerce.get_order, o["id"])["status"] == "refunded"


@pytest.mark.parametrize("amount", [0 - 1, 10 ** 9])
def test_a_refund_can_never_be_more_than_what_is_left(world, amount):
    o, p = paid(world)
    with pytest.raises(CommerceError) as why:
        world.do(payments.refund, world.vault_, payment_id=p["id"], amount_paise=amount)
    assert why.value.reason == "bad_amount"
    assert world.do(payments.get_payment, p["id"])["refunded_paise"] == 0


def test_only_money_that_arrived_can_be_refunded(world):
    o, _ = make_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    with pytest.raises(CommerceError) as why:
        world.do(payments.refund, world.vault_, payment_id=p["id"])
    assert why.value.reason == "not_refundable"


def test_a_refund_razorpay_refuses_is_not_recorded(world):
    o, p = paid(world)
    world.rz_.fail = "The payment has already been fully refunded"
    with pytest.raises(CommerceError) as why:
        world.do(payments.refund, world.vault_, payment_id=p["id"])
    assert why.value.reason == "gateway_refused" and "already been fully refunded" in str(why.value)
    assert world.do(payments.get_payment, p["id"])["refunded_paise"] == 0


def test_a_refund_made_elsewhere_can_be_recorded_but_prism_will_not_pretend_to_send_one(world):
    o, _ = meta_order(world)
    p = world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.deliver(payment_status(o["reference_id"], "captured"))
    world.db.q("update commerce_config set razorpay_secret_enc = null, razorpay_key_id = null")
    with pytest.raises(CommerceError) as why:
        world.do(payments.refund, world.vault_, payment_id=p["id"])
    assert why.value.reason == "no_gateway_refund"
    got = world.do(payments.refund, world.vault_, payment_id=p["id"], record_only=True, reason="Refunded in the Razorpay dashboard")
    assert got["status"] == "refunded" and got["refunds"][0]["status"] == "recorded"


# ── the dashboard and who to remind ──────────────────────────────────────────

def test_the_dashboard_counts_money_in_out_and_waiting(world):
    o1, p1 = paid(world)
    world.do(payments.refund, world.vault_, payment_id=p1["id"], amount_paise=20000)
    who2 = world.person("Ravi Patel", "919800100002")
    o2 = world.do(commerce.create_order, {"contact_id": who2, "items": [{"retailer_id": "gate-1", "quantity": 1}]})
    world.do(payments.request_payment, world.vault_, order_id=o2["id"])
    d = world.do(payments.dashboard)
    assert d["received_paise"] == o1["total_paise"] and d["refunded_paise"] == 20000 and d["net_paise"] == o1["total_paise"] - 20000
    assert d["received_count"] == 1 and d["pending"]["count"] == 1 and d["orders"] == 2 and d["average_paise"] == o1["total_paise"]
    assert d["methods"][0]["method"] == "upi" and len(d["days"]) == 1 and d["conversion_percent"] == 100.0


def test_people_who_left_a_payment_open_are_listed_to_remind_but_not_opted_out_ones_or_those_who_paid(world):
    o1, p1 = paid(world)
    who2 = world.person("Ravi Patel", "919800100002")
    who3 = world.person("Meena Joshi", "919800100003")
    for who in (who2, who3):
        o = world.do(commerce.create_order, {"contact_id": who, "items": [{"retailer_id": "gate-1", "quantity": 1}]})
        world.do(payments.request_payment, world.vault_, order_id=o["id"])
    world.db.q("update contacts set opt_out = true where id = %s", (who3,))
    people = world.do(payments.retarget, older_than_minutes=0)
    assert [p["contact_id"] for p in people] == [who2]
    assert world.do(payments.retarget, older_than_minutes=600) == []


# ── over HTTP ────────────────────────────────────────────────────────────────

def test_the_routes_return_what_the_screens_need(world):
    r = world.post("/internal/commerce/config/save", gateway="razorpay", razorpay_key_id=KEY_ID, razorpay_secret=KEY_SECRET, razorpay_webhook_secret=HOOK_SECRET,
                   catalog_id="5551234567", tax_percent=18, public_url="https://licence.example")
    assert r.status_code == 200 and r.json()["config"]["webhook_url"].endswith("/v1/whatsapp/pay/razorpay/%s" % world.tid_) and KEY_SECRET not in r.text
    world.post("/internal/catalog/save", name="Wall panel", price="1299.50", retailer_id="wall-6")
    who = world.person()
    order = world.post("/internal/orders/create", contact_id=str(who), items=[{"retailer_id": "wall-6", "quantity": 2}]).json()["order"]
    assert order["total_paise"] > 0 and world.post("/internal/orders/get", order_id=order["id"]).json()["order"]["reference_id"] == order["reference_id"]
    pay = world.post("/internal/payments/request", order_id=order["id"]).json()["payment"]
    assert pay["status"] == "pending" and world.post("/internal/payments/list").json()["total"] == 1
    body, sig = signed(paid_event(pay["provider_ref"]))
    hook = world.post("/internal/payments/razorpay_webhook", body=body.decode(), signature=sig)
    assert hook.status_code == 200 and hook.json()["handled"]
    assert world.post("/internal/payments/dashboard").json()["received_count"] == 1


def test_a_wrong_signature_over_http_is_forbidden_and_an_unknown_order_is_not_found(world):
    world.configure()
    assert world.post("/internal/payments/razorpay_webhook", body="{}", signature="nope").status_code == 403
    r = world.post("/internal/orders/get", order_id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and r.json()["error"] == "unknown_order"
    assert world.post("/internal/orders/status", order_id="00000000-0000-0000-0000-000000000000", status="shipped").status_code == 404


def test_the_shop_is_a_paid_plan_feature(world):
    world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}'")
    r = world.post("/internal/catalog/list")
    assert r.status_code == 403 and r.json()["error"] == "plan_feature" and r.json()["needs"] == "pro"


def test_another_workspace_sees_none_of_this(world):
    o, p = paid(world)
    other = world.db.one("insert into tenants (slug, name) values ('rival', 'Rival') returning id")
    c = world.conn()
    with c.cursor() as cur:
        with pytest.raises(CommerceError):
            commerce.get_order(cur, other, o["id"])
        with pytest.raises(CommerceError):
            payments.get_payment(cur, other, p["id"])
        assert commerce.list_products(cur, other)["total"] == 0 and payments.dashboard(cur, other)["received_count"] == 0
    c.close()
