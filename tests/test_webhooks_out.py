"""Outbound webhooks: who may have one, where they may point, what is queued when something happens, how it is signed and sent, and what
happens when the receiver is down. Real Postgres and a fake receiver."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import inbox, webhooks_out, worker
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.vault import Vault
from prism_wa.webhooks_out import WebhookError

INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}


def soon():
    """"Now" for the sender: after the rows (made a moment ago) became due. Taken when a test asks for it, not once when the file is imported: in a
    long run the import-time clock was minutes older than the rows it was supposed to be later than, and ten of these tests failed for that alone."""
    return datetime.now(timezone.utc) + timedelta(minutes=5)


PUBLIC = {"hooks.example.com": ["93.184.216.34"], "10.example.com": ["10.0.0.5"], "meta.example.com": ["169.254.169.254"], "gone.example.com": []}


class Receiver:
    """What the workspace's server does with a delivery: records it and answers with whatever status it was told to."""

    def __init__(self):
        self.calls, self.status = [], 200

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append({"method": method, "url": url, "body": json.loads(data), "raw": data, "headers": headers, "timeout": timeout})
        return self.status, b"ok" if self.status < 300 else b"it broke"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(webhooks_out, "_resolve", lambda host: PUBLIC.get(host, ["93.184.216.34"]))


@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"unlimited\"}')", (tenant_id,))
    recv = Receiver()

    class W:
        db, tid, nid, recv_ = migrated, tenant_id, number_id, recv

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tenant_id, *a, **kw)
                conn.commit()
                return out
            finally:
                conn.close()

        @classmethod
        def make(cls, url="https://hooks.example.com/prism", events=("message.received",)):
            return cls.do(webhooks_out.create, url=url, events=list(events))

        @classmethod
        def send(cls, now=None, **kw):
            return webhooks_out.deliver_due(cls.conn, transport=recv, now=now or soon(), **kw)

        @classmethod
        def rows(cls, sql="select event, state, attempts, response_status, error from webhook_deliveries order by created_at"):
            return migrated.q(sql)
    return W


# ── who may have one ─────────────────────────────────────────────────────────

def test_an_endpoint_is_made_with_a_secret_shown_once(world):
    ep = world.make(events=("message.received", "chat.handoff"))
    assert ep["secret"].startswith("whsec_") and len(ep["secret"]) > 20 and ep["enabled"] is True and ep["failure_count"] == 0
    assert ep["events"] == ["message.received", "chat.handoff"] and ep["url"] == "https://hooks.example.com/prism"
    listed = world.do(webhooks_out.list_endpoints)
    assert len(listed) == 1 and "secret" not in listed[0]


def test_webhooks_are_a_premium_feature_with_a_count_per_plan(world):
    world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}'")
    with pytest.raises(Exception) as e:
        world.make()
    assert getattr(e.value, "reason", "") == "plan_feature"
    world.db.q("update tenant_settings set settings = '{\"plan\": \"premium\"}'")
    world.make()
    with pytest.raises(Exception) as e2:
        world.make(url="https://hooks.example.com/second")
    assert e2.value.reason == "plan_limit" and "1 webhooks" in str(e2.value)
    world.db.q("update tenant_settings set settings = '{\"plan\": \"unlimited\"}'")
    world.make(url="https://hooks.example.com/second")
    world.make(url="https://hooks.example.com/third")
    with pytest.raises(Exception) as e3:
        world.make(url="https://hooks.example.com/fourth")
    assert e3.value.reason == "plan_limit"


@pytest.mark.parametrize("url,reason", [
    ("http://hooks.example.com/x", "insecure_url"), ("ftp://hooks.example.com/x", "bad_url"), ("https://", "bad_url"), ("", "bad_url"),
    ("https://user:pass@hooks.example.com/x", "bad_url"), ("https://10.example.com/x", "private_url"), ("https://meta.example.com/x", "private_url"),
    ("https://gone.example.com/x", "unknown_host"), ("https://127.0.0.2/x", "private_url"), ("x" * 600, "bad_url")])
def test_an_address_that_could_reach_inside_the_network_is_refused(world, url, reason):
    monkey = {"127.0.0.2": ["127.0.0.2"]}
    webhooks_out._resolve = lambda host: monkey.get(host) or PUBLIC.get(host, ["93.184.216.34"])
    with pytest.raises(WebhookError) as e:
        world.make(url=url)
    assert e.value.reason == reason


def test_http_to_this_computer_is_allowed_for_trying_things_out(world):
    assert world.make(url="http://localhost:8000/hook")["url"] == "http://localhost:8000/hook"
    world.db.q("delete from webhook_endpoints")
    assert world.make(url="http://127.0.0.1:9/hook")


def test_the_events_must_be_real_and_there_must_be_one(world):
    with pytest.raises(WebhookError) as e:
        world.make(events=("message.received", "nonsense"))
    assert e.value.reason == "bad_event"
    with pytest.raises(WebhookError) as e2:
        world.make(events=())
    assert e2.value.reason == "no_events"
    assert world.make(events=("message.received", "message.received"))["events"] == ["message.received"]


# ── what is queued ───────────────────────────────────────────────────────────

def test_only_the_endpoints_that_asked_for_an_event_get_it(world):
    a = world.make("https://hooks.example.com/a", ("message.received",))
    b = world.make("https://hooks.example.com/b", ("chat.handoff",))
    c = world.make("https://hooks.example.com/c", ("message.received", "chat.handoff"))
    world.do(webhooks_out.update, c["id"], {"enabled": False})
    assert world.do(webhooks_out.emit, "message.received", {"text": "hi"}) == 1
    rows = world.db.q("select endpoint_id, event, payload, state from webhook_deliveries")
    assert [(r["endpoint_id"], r["event"], r["state"]) for r in rows] == [(a["id"], "message.received", "pending")] and rows[0]["payload"] == {"text": "hi"}
    assert b["id"] != a["id"]


def test_the_data_is_only_built_when_somebody_is_listening(world):
    built = []
    assert world.do(webhooks_out.emit, "message.received", lambda: built.append(1) or {"x": 1}) == 0 and built == []
    world.make()
    assert world.do(webhooks_out.emit, "message.received", lambda: built.append(1) or {"x": 1}) == 1 and built == [1]
    assert world.do(webhooks_out.emit, "not.an.event", {}) == 0


def test_telling_the_world_never_breaks_what_happened(world):
    world.make()
    assert world.do(webhooks_out.emit, "message.received", {"bad": object()}) == 0           # an unserialisable payload is dropped, not raised
    assert world.db.q("select 1 from webhook_deliveries") == []


def test_another_workspaces_endpoint_hears_nothing(world):
    other = world.db.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    world.db.q("insert into webhook_endpoints (tenant_id, url, secret, events) values (%s, 'https://hooks.example.com/o', 's', '{message.received}')", (other,))
    assert world.do(webhooks_out.emit, "message.received", {"x": 1}) == 0


# ── sending, signing ─────────────────────────────────────────────────────────

def test_a_delivery_is_a_signed_json_post_the_receiver_can_verify(world):
    ep = world.make()
    world.do(webhooks_out.emit, "message.received", {"text": "hello", "wamid": "wamid.1"})
    assert world.send() == {"sent": 1, "retry": 0, "failed": 0}
    call = world.recv_.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://hooks.example.com/prism" and call["timeout"] == 10
    h = call["headers"]
    assert h["Content-Type"] == "application/json" and h["X-Prism-Event"] == "message.received" and h["User-Agent"].startswith("Prism-Webhooks")
    body = call["body"]
    assert body["event"] == "message.received" and body["data"] == {"text": "hello", "wamid": "wamid.1"} and body["id"] == h["X-Prism-Delivery"]
    expected = "v1=" + hmac.new(ep["secret"].encode(), h["X-Prism-Timestamp"].encode() + b"." + call["raw"], hashlib.sha256).hexdigest()
    assert h["X-Prism-Signature"] == expected == webhooks_out.sign(ep["secret"], h["X-Prism-Timestamp"], call["raw"])
    assert world.rows() == [{"event": "message.received", "state": "delivered", "attempts": 1, "response_status": 200, "error": None}]
    ep_row = world.do(webhooks_out.list_endpoints)[0]
    assert ep_row["last_status"] == 200 and ep_row["failure_count"] == 0 and ep_row["last_delivery_at"] is not None


def test_a_different_secret_gives_a_different_signature(world):
    a = world.make("https://hooks.example.com/a")
    secret = world.do(webhooks_out.rotate_secret, a["id"])
    assert secret != a["secret"] and secret.startswith("whsec_")
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    world.send()
    h = world.recv_.calls[0]["headers"]
    assert h["X-Prism-Signature"] == webhooks_out.sign(secret, h["X-Prism-Timestamp"], world.recv_.calls[0]["raw"])
    assert h["X-Prism-Signature"] != webhooks_out.sign(a["secret"], h["X-Prism-Timestamp"], world.recv_.calls[0]["raw"])


def test_a_test_ping_goes_to_the_endpoint_whatever_it_listens_for(world):
    ep = world.make(events=("chat.handoff",))
    world.do(webhooks_out.send_test, ep["id"])
    world.send()
    assert world.recv_.calls[0]["body"]["event"] == "ping" and "test from Prism" in world.recv_.calls[0]["body"]["data"]["message"]


# ── when the receiver is down ────────────────────────────────────────────────

def test_a_failing_receiver_is_retried_with_a_growing_wait(world):
    world.make()
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    world.recv_.status = 500
    t = soon()
    waits = []
    for attempt in range(1, 6):
        assert world.send(now=t) == {"sent": 0, "retry": 1, "failed": 0}
        row = world.db.q("select attempts, state, next_at, error, response_status from webhook_deliveries")[0]
        assert row["attempts"] == attempt and row["state"] == "pending" and row["response_status"] == 500 and "HTTP 500: it broke" in row["error"]
        waits.append(int((row["next_at"] - t).total_seconds()))
        assert world.send(now=t) == {"sent": 0, "retry": 0, "failed": 0}                  # not due yet: nothing is tried early
        t = row["next_at"]
    assert waits == [60, 300, 1800, 7200, 21600]
    assert world.send(now=t) == {"sent": 0, "retry": 0, "failed": 1}
    assert world.rows() == [{"event": "message.received", "state": "failed", "attempts": 6, "response_status": 500, "error": "HTTP 500: it broke"}]
    assert world.do(webhooks_out.list_endpoints)[0]["failure_count"] == 1


def test_a_receiver_that_comes_back_gets_the_delivery_and_a_clean_slate(world):
    world.make()
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    world.recv_.status = 503
    first = soon()
    world.send(now=first)
    world.db.q("update webhook_endpoints set failure_count = 7")
    world.recv_.status = 200
    assert world.send(now=first + timedelta(minutes=2)) == {"sent": 1, "retry": 0, "failed": 0}
    assert world.do(webhooks_out.list_endpoints)[0]["failure_count"] == 0 and world.recv_.calls[-1]["headers"]["X-Prism-Delivery"] == world.recv_.calls[0]["headers"]["X-Prism-Delivery"]


def test_a_receiver_that_cannot_be_reached_is_a_failure_with_a_reason(world):
    world.make()
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    world.recv_.status = 0
    world.send()
    assert "could not connect" in world.rows()[0]["error"]


def test_an_endpoint_that_keeps_failing_is_switched_off_and_can_be_turned_back_on(world):
    ep = world.make()
    world.recv_.status = 500
    for _ in range(webhooks_out.SWITCH_OFF_AFTER):
        did = world.db.one("insert into webhook_deliveries (tenant_id, endpoint_id, event, payload, attempts) values (%s, %s, 'message.received', '{}', %s) returning id",
                           (world.tid, ep["id"], webhooks_out.MAX_ATTEMPTS - 1))
        world.send()
        assert world.db.one("select state from webhook_deliveries where id = %s", (did,)) == "failed"
    row = world.do(webhooks_out.list_endpoints)[0]
    assert row["enabled"] is False and "Switched off" in row["disabled_reason"] and row["failure_count"] == webhooks_out.SWITCH_OFF_AFTER
    assert world.do(webhooks_out.emit, "message.received", {"x": 1}) == 0                  # a switched-off endpoint is not queued for
    back = world.do(webhooks_out.update, ep["id"], {"enabled": True})
    assert back["enabled"] is True and back["failure_count"] == 0 and back["disabled_reason"] is None


def test_the_address_is_checked_again_at_every_delivery(world):
    world.make("https://hooks.example.com/x")
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    PUBLIC["hooks.example.com"] = ["192.168.1.9"]                                       # the name now points inside the network
    try:
        assert world.send()["retry"] == 1
    finally:
        PUBLIC["hooks.example.com"] = ["93.184.216.34"]
    assert world.recv_.calls == [] and "private network" in world.rows()[0]["error"]


def test_a_huge_event_is_not_sent(world):
    world.make()
    world.do(webhooks_out.emit, "message.received", {"blob": "x" * (webhooks_out.BODY_MAX + 10)})
    world.send()
    assert world.recv_.calls == [] and "too big" in world.rows()[0]["error"]


# ── managing them ────────────────────────────────────────────────────────────

def test_an_endpoint_can_be_changed_and_removed(world):
    ep = world.make()
    out = world.do(webhooks_out.update, ep["id"], {"url": "https://hooks.example.com/new", "events": ["chat.handoff"], "description": "CRM"})
    assert (out["url"], out["events"], out["description"]) == ("https://hooks.example.com/new", ["chat.handoff"], "CRM")
    with pytest.raises(WebhookError) as e:
        world.do(webhooks_out.update, ep["id"], {"url": "http://hooks.example.com/plain"})
    assert e.value.reason == "insecure_url"
    with pytest.raises(WebhookError) as e2:
        world.do(webhooks_out.update, ep["id"], {})
    assert e2.value.reason == "nothing_to_change"
    with pytest.raises(WebhookError) as e3:
        world.do(webhooks_out.update, "00000000-0000-0000-0000-000000000000", {"enabled": False})
    assert e3.value.reason == "unknown_endpoint"
    assert world.do(webhooks_out.delete, ep["id"]) is True and world.do(webhooks_out.delete, ep["id"]) is False


def test_the_log_shows_each_delivery_and_a_past_one_can_be_sent_again(world):
    ep = world.make()
    world.do(webhooks_out.emit, "message.received", {"n": 1})
    world.do(webhooks_out.emit, "message.received", {"n": 2})
    world.send()
    log = world.do(webhooks_out.deliveries, ep["id"], 10)
    assert [d["state"] for d in log] == ["delivered", "delivered"] and log[0]["url"] == "https://hooks.example.com/prism"
    assert len(world.do(webhooks_out.deliveries, None, 1)) == 1
    again = world.do(webhooks_out.replay, log[0]["id"])
    assert again != str(log[0]["id"])
    world.send()
    assert [c["body"]["data"] for c in world.recv_.calls][-1] in ({"n": 1}, {"n": 2}) and len(world.recv_.calls) == 3
    with pytest.raises(WebhookError) as e:
        world.do(webhooks_out.replay, "00000000-0000-0000-0000-000000000000")
    assert e.value.reason == "unknown_delivery"


def test_deleting_an_endpoint_takes_its_log_with_it(world):
    ep = world.make()
    world.do(webhooks_out.emit, "message.received", {"n": 1})
    world.do(webhooks_out.delete, ep["id"])
    assert world.db.q("select 1 from webhook_deliveries") == []


# ── what actually triggers them ──────────────────────────────────────────────

def events(world):
    return [(r["event"], r["payload"]) for r in world.db.q("select event, payload from webhook_deliveries order by created_at")]


def test_a_customers_message_is_told_to_the_workspace(world):
    world.make(events=("message.received",))
    H.deliver(world.db, P.inbound(P.text("hello there", "wamid.T9", "1760000001")))
    H.work(world.db)
    (event, data), = events(world)
    assert event == "message.received" and data["text"] == "hello there" and data["wamid"] == "wamid.T9" and data["type"] == "text"
    assert data["contact_id"] and data["conversation_id"] and data["message_id"]
    H.deliver(world.db, P.inbound(P.text("hello there", "wamid.T9", "1760000001")))            # the same delivery from Meta again
    H.work(world.db)
    assert len(events(world)) == 1


def test_a_status_change_is_told_with_its_price(world):
    world.make(events=("message.status",))
    contact = world.db.one("insert into contacts (tenant_id, name, phone) values (%s, 'Asha', %s) returning id", (world.tid, P.ASHA))
    conv = world.db.one("insert into conversations (tenant_id, contact_id, channel) values (%s, %s, 'whatsapp') returning id", (world.tid, contact))
    world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, wamid, status) values (%s, %s, 'out', 'agent', 'hi', 'wamid.OUT1', 'sent')", (world.tid, conv))
    H.deliver(world.db, P.statuses(P.status("wamid.OUT1", "delivered", "9", pricing=P.PRICING)))
    H.work(world.db)
    (event, data), = events(world)
    assert event == "message.status" and data["status"] == "delivered" and data["wamid"] == "wamid.OUT1" and data["pricing"]["category"] == "marketing"


def test_a_customer_stopping_messages_is_told(world):
    world.make(events=("contact.opt_out", "contact.opt_in"))
    H.deliver(world.db, P.inbound(P.text("STOP", "wamid.S1", "1760000001")))
    H.work(world.db)
    got = events(world)
    assert [e for e, _d in got] == ["contact.opt_out"] and got[0][1]["source"] == "keyword"
    H.deliver(world.db, P.inbound(P.text("START", "wamid.S2", "1760000002")))
    H.work(world.db)
    assert [e for e, _d in events(world)] == ["contact.opt_out", "contact.opt_in"]


def test_asking_for_a_person_is_told_with_who_got_it(world):
    world.make(events=("chat.handoff",))
    owner = world.do(inbox.ensure_agent, "seat-owner", "Owner")
    contact = world.db.one("insert into contacts (tenant_id, name, phone) values (%s, 'Asha', %s) returning id", (world.tid, P.ASHA))
    conv = world.db.one("insert into conversations (tenant_id, contact_id, channel, number_id) values (%s, %s, 'whatsapp', %s) returning id", (world.tid, contact, world.nid))
    world.do(inbox.request_human, conv, "wants a quote")
    (event, data), = events(world)
    assert event == "chat.handoff" and data["reason"] == "wants a quote" and data["conversation_id"] == str(conv) and data["assigned_to"] == owner["name"]


def test_a_template_decision_is_told(world):
    world.make(events=("template.status",))
    waba = world.db.one("select id from wabas")
    world.db.q("insert into templates (tenant_id, waba_id, meta_id, name, language, category, status, components) values (%s, %s, '555', 'diwali_offer', 'en_US', 'MARKETING', 'PENDING', '[]')",
               (world.tid, waba))
    H.deliver(world.db, P.template_status("REJECTED", "diwali_offer", 555, reason="INVALID_FORMAT"))
    H.work(world.db)
    (event, data), = events(world)
    assert event == "template.status" and data == {"name": "diwali_offer", "language": "en_US", "status": "REJECTED", "reason": "INVALID_FORMAT"}


def test_a_finished_campaign_is_told(world):
    from prism_wa import send
    world.make(events=("campaign.completed",))
    cid = world.db.one("insert into campaigns (tenant_id, name, template_name, status, total, sent, failed, kind) values (%s, 'Diwali', 'diwali_offer', 'sending', 5, 4, 1, 'broadcast') returning id", (world.tid,))
    conn = world.conn()
    with conn.cursor() as cur:
        send._close_campaign_if_done(cur, cid)
    conn.commit()
    conn.close()
    (event, data), = events(world)
    assert event == "campaign.completed" and data == {"campaign_id": str(cid), "name": "Diwali", "total": 5, "sent": 4, "failed": 1}


def test_the_worker_delivers_them_as_part_of_its_cycle(world, monkeypatch):
    world.make()
    world.do(webhooks_out.emit, "message.received", {"x": 1})
    monkeypatch.setattr(webhooks_out, "urllib_transport", world.recv_)
    out = worker.cycle(world.conn, graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}), reap=False)
    assert out.hooks == 1 and out.busy and len(world.recv_.calls) == 1


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(world, migrated):
    client = TestClient(create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token=INTERNAL,
                                   graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)})))

    class A:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(world.tid))
            return client.post(path, json=body, headers=AUTH)
    A.world = world
    return A


def test_the_whole_lifecycle_over_http_and_the_secret_is_shown_only_once(api):
    r = api.post("/internal/webhooks/create", url="https://hooks.example.com/prism", events=["message.received", "message.status"], description="CRM")
    assert r.status_code == 200, r.text
    secret, ep = r.json()["secret"], r.json()["endpoint"]
    assert secret.startswith("whsec_") and "secret" not in ep
    listed = api.post("/internal/webhooks/list").json()
    assert listed["endpoints"][0]["description"] == "CRM" and "secret" not in listed["endpoints"][0] and "message.received" in listed["events"]
    assert api.post("/internal/webhooks/update", endpoint_id=ep["id"], changes={"enabled": False}).json()["endpoint"]["enabled"] is False
    new = api.post("/internal/webhooks/rotate_secret", endpoint_id=ep["id"]).json()["secret"]
    assert new != secret
    d = api.post("/internal/webhooks/test", endpoint_id=ep["id"]).json()["delivery_id"]
    log = api.post("/internal/webhooks/deliveries", endpoint_id=ep["id"]).json()["deliveries"]
    assert log[0]["event"] == "ping" and log[0]["id"] == d and log[0]["state"] == "pending"
    assert api.post("/internal/webhooks/replay", delivery_id=d).status_code == 200
    assert api.post("/internal/webhooks/delete", endpoint_id=ep["id"]).json() == {"ok": True, "deleted": True}


def test_over_http_the_refusals_have_honest_codes(api):
    assert api.post("/internal/webhooks/create", url="http://hooks.example.com/x", events=["message.received"]).status_code == 400
    assert api.post("/internal/webhooks/create", url="https://10.example.com/x", events=["message.received"]).json()["error"] == "private_url"
    assert api.post("/internal/webhooks/update", endpoint_id="00000000-0000-0000-0000-000000000000", changes={"enabled": True}).status_code == 404
    api.world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}'")
    r = api.post("/internal/webhooks/create", url="https://hooks.example.com/x", events=["message.received"], plan="basic")
    assert r.status_code == 403 and r.json()["needs"] == "premium"


def test_only_a_manager_changes_webhooks(api):
    world = api.world
    world.do(inbox.ensure_agent, "seat-owner", "Owner")
    bala = world.do(inbox.ensure_agent, "seat-bala", "Bala")
    r = api.post("/internal/webhooks/create", url="https://hooks.example.com/x", events=["message.received"], by_agent_id=str(bala["id"]))
    assert r.status_code == 409 and "owner or manager" in r.json()["message"]
