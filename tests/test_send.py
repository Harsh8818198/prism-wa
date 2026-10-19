"""The send engine against a real Postgres and a fake Graph server."""
import json
import os
import threading
import time
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import send
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.send import RateLimiter, SendRefused
from prism_wa.vault import Vault

TOKEN = "EAAB-test-business-token"


class FakeGraph:
    """A Graph transport that records every call and can be scripted per recipient."""

    def __init__(self):
        self.calls, self.fail, self.delay, self._n, self._lock = [], {}, 0.0, 0, threading.Lock()

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        with self._lock:
            self._n += 1
            n = self._n
            self.calls.append({"url": url, "body": body, "auth": headers.get("Authorization")})
        if self.delay:
            time.sleep(self.delay)
        who = (body or {}).get("to") or (body or {}).get("recipient")
        scripted = self.fail.get(who)
        if isinstance(scripted, Exception):
            raise scripted
        if scripted:
            code, status = scripted
            return status, json.dumps({"error": {"message": "scripted", "code": code, "fbtrace_id": "T"}}).encode()
        return 200, json.dumps({"messaging_product": "whatsapp", "contacts": [{"wa_id": who}],
                                "messages": [{"id": "wamid.SENT%d" % n}]}).encode()


@pytest.fixture
def vault():
    return Vault({1: os.urandom(32)})


@pytest.fixture
def world(migrated, vault):
    """An onboarded tenant whose WABA holds an encrypted token, plus a fake Graph."""
    tenant, number = H.onboard(migrated)
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))

    class W:
        db, tid, nid, graph, vault_ = migrated, tenant, number, FakeGraph(), vault

        @staticmethod
        def connect():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def contact(cls, phone=P.ASHA, bsuid=None, inbound_ago=timedelta(minutes=5), opt_out=False):
            cid = migrated.one("insert into contacts (tenant_id, name, phone, bsuid, opt_out)"
                               " values (%s, 'Asha', %s, %s, %s) returning id", (tenant, phone, bsuid, opt_out))
            if inbound_ago is not None:
                migrated.q("insert into conversations (tenant_id, contact_id, channel, last_inbound_at, number_id)"
                           " values (%s, %s, 'whatsapp', %s, %s)", (tenant, cid, utcnow() - inbound_ago, number))
            return cid

        @classmethod
        def enqueue(cls, fn, **kw):
            conn = cls.connect()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tenant, **kw)
                conn.commit()
                return out
            finally:
                conn.close()

        @classmethod
        def deliver(cls, **kw):
            return send.send_due(cls.connect, graph=Graph(cls.graph), vault=vault, **kw)

    return W


def text(world, contact, body="hi", key="k1", **kw):
    return world.enqueue(send.enqueue_text, contact_id=contact, text=body, idem_key=key, **kw)


# ── enqueue rules ────────────────────────────────────────────────────────────

def test_a_reply_inside_the_window_is_queued_and_shown_in_the_inbox_as_queued(world):
    c = world.contact()
    text(world, c, "your quote is ready")
    out = H.only(world.db, "select * from outbox")
    assert out["state"] == "queued" and out["kind"] == "text" and out["to_phone"] == P.ASHA
    assert out["payload"] == {"type": "text", "text": {"body": "your quote is ready"}}
    msg = H.only(world.db, "select * from messages")
    assert (msg["direction"], msg["status"], msg["text"], msg["id"]) == ("out", "queued", "your quote is ready", out["message_id"])
    assert world.db.one("select message_count from conversations") == 1


def test_a_free_form_reply_outside_the_window_is_refused_and_writes_nothing(world):
    c = world.contact(inbound_ago=timedelta(hours=25))
    with pytest.raises(SendRefused) as e:
        text(world, c)
    assert e.value.reason == "window_closed"
    assert world.db.one("select count(*) from outbox") == 0 and world.db.one("select count(*) from messages") == 0


def test_the_window_closes_at_exactly_24_hours(world):
    now = utcnow()
    c = world.contact(inbound_ago=timedelta(hours=24) - timedelta(seconds=30))
    text(world, c, now=now)
    c2 = world.contact(phone="919800000002", inbound_ago=timedelta(hours=24))
    with pytest.raises(SendRefused):
        text(world, c2, key="k2", now=now + timedelta(seconds=1))


def test_a_contact_who_never_wrote_cannot_be_texted_but_can_be_sent_a_template(world):
    c = world.contact(inbound_ago=None)
    with pytest.raises(SendRefused) as e:
        text(world, c)
    assert e.value.reason == "window_closed"
    world.enqueue(send.enqueue_template, contact_id=c, name="diwali_offer", language="en_US",
                  components=[{"type": "body", "parameters": [{"type": "text", "text": "Asha"}]}], idem_key="t1")
    out = H.only(world.db, "select * from outbox")
    assert out["kind"] == "template"
    assert out["payload"]["template"] == {"name": "diwali_offer", "language": {"code": "en_US"},
                                          "components": [{"type": "body", "parameters": [{"type": "text", "text": "Asha"}]}]}
    assert world.db.one("select text from messages") == "[template] diwali_offer"


def test_an_opted_out_contact_gets_no_template_but_a_reply_inside_the_window_is_allowed(world):
    c = world.contact(opt_out=True)
    with pytest.raises(SendRefused) as e:
        world.enqueue(send.enqueue_template, contact_id=c, name="promo", idem_key="t1")
    assert e.value.reason == "opted_out"
    text(world, c, "sure, here you go")
    assert world.db.one("select count(*) from outbox") == 1


def test_the_same_request_twice_queues_one_message(world):
    c = world.contact()
    a, b = text(world, c, key="same"), text(world, c, key="same")
    assert a == b
    assert world.db.one("select count(*) from outbox") == 1 and world.db.one("select count(*) from messages") == 1


def test_a_contact_from_another_tenant_is_not_reachable(world):
    other = world.db.one("insert into contacts (tenant_id, name, phone)"
                         " values ((select id from tenants where slug = 'shakti'), 'X', '919811111111') returning id")
    with pytest.raises(SendRefused) as e:
        text(world, other)
    assert e.value.reason == "unknown_contact"


def test_which_number_to_send_from(world):
    c = world.contact(inbound_ago=None)                    # no conversation, so no remembered number
    wid = world.db.one("select id from wabas")
    world.db.q("insert into phone_numbers (tenant_id, waba_id, phone_number_id) values (%s, %s, 'SECOND')",
               (world.tid, wid))
    with pytest.raises(SendRefused) as e:
        world.enqueue(send.enqueue_template, contact_id=c, name="t", idem_key="a")
    assert e.value.reason == "no_number"
    world.enqueue(send.enqueue_template, contact_id=c, name="t", idem_key="b", number_id=world.nid)
    assert world.db.one("select number_id from outbox") == world.nid


def test_an_empty_message_is_refused(world):
    with pytest.raises(SendRefused):
        text(world, world.contact(), "   ")


# ── delivery ─────────────────────────────────────────────────────────────────

def test_a_queued_message_is_sent_with_the_decrypted_token_and_recorded(world):
    c = world.contact()
    text(world, c, "hello")
    stats = world.deliver()
    assert (stats.claimed, stats.sent, stats.failed) == (1, 1, 0)

    call = world.graph.calls[0]
    assert call["url"].endswith("/%s/messages" % P.PNID) and call["auth"] == "Bearer " + TOKEN
    assert call["body"] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": P.ASHA,
                            "type": "text", "text": {"body": "hello"}}
    out = H.only(world.db, "select state, wamid, attempts from outbox")
    assert out["state"] == "sent" and out["wamid"] == "wamid.SENT1" and out["attempts"] == 1
    msg = H.only(world.db, "select status, wamid from messages")
    assert msg == {"status": "sent", "wamid": "wamid.SENT1"}


def test_a_contact_known_only_by_bsuid_is_addressed_by_recipient(world):
    c = world.contact(phone=None, bsuid="IN.abc123")
    text(world, c)
    world.deliver()
    body = world.graph.calls[0]["body"]
    assert body["recipient"] == "IN.abc123" and "to" not in body


def test_a_sent_message_is_never_sent_again(world):
    text(world, world.contact())
    world.deliver()
    world.deliver()
    assert len(world.graph.calls) == 1


def test_a_message_not_yet_due_waits(world):
    text(world, world.contact())
    world.db.q("update outbox set run_at = now() + interval '1 hour'")
    assert world.deliver().claimed == 0 and not world.graph.calls


@pytest.mark.parametrize("code,status,klass", [
    (131047, 400, "needs_template"), (131050, 400, "opted_out"), (190, 401, "fix_setup"),
    (132001, 400, "fix_setup"), (131026, 400, "fatal"), (999999, 400, "fatal")])
def test_a_refusal_from_meta_is_recorded_with_its_class(world, code, status, klass):
    c = world.contact()
    text(world, c)
    world.graph.fail[P.ASHA] = (code, status)
    stats = world.deliver()
    assert (stats.sent, stats.failed) == (0, 1)
    out = H.only(world.db, "select state, error_class, error from outbox")
    assert (out["state"], out["error_class"], out["error"]["code"]) == ("failed", klass, code)
    msg = H.only(world.db, "select status, error from messages")
    assert msg["status"] == "failed" and msg["error"]["code"] == code
    assert world.db.one("select opt_out from contacts") is (klass == "opted_out")


def test_a_retryable_error_is_requeued_with_a_wait_and_tried_again(world):
    text(world, world.contact())
    world.graph.fail[P.ASHA] = (131056, 400)               # pair rate limit: wait a minute
    now = utcnow()
    assert world.deliver(now=now).retried == 1
    out = H.only(world.db, "select state, attempts, error_class, run_at from outbox")
    assert (out["state"], out["attempts"], out["error_class"]) == ("queued", 1, "retry")
    assert out["run_at"] == now + timedelta(seconds=60)
    assert world.db.one("select status from messages") == "queued"        # the inbox still says queued

    assert world.deliver(now=now + timedelta(seconds=30)).claimed == 0     # not yet
    world.graph.fail.clear()
    assert world.deliver(now=now + timedelta(seconds=61)).sent == 1
    assert world.db.one("select attempts from outbox") == 2


def test_a_network_failure_is_retried_and_gives_up_after_five_attempts(world):
    text(world, world.contact())
    world.graph.fail[P.ASHA] = GraphError("connection reset")
    now = utcnow()
    for attempt in range(1, send.MAX_ATTEMPTS + 1):
        world.deliver(now=now)
        row = H.only(world.db, "select state, attempts from outbox")
        assert row["attempts"] == attempt
        now += timedelta(hours=2)
    assert row["state"] == "failed"
    assert world.db.one("select status from messages") == "failed"


def test_a_number_without_a_working_token_fails_for_an_admin_to_fix(world):
    text(world, world.contact())
    world.db.q("update wabas set token_enc = null")
    world.deliver()
    out = H.only(world.db, "select state, error_class from outbox")
    assert (out["state"], out["error_class"]) == ("failed", "fix_setup")
    assert not world.graph.calls                                           # nothing went to Meta


def test_a_token_encrypted_for_another_waba_is_refused(world, vault):
    text(world, world.contact())
    blob, _ = vault.encrypt(TOKEN, context="SOME-OTHER-WABA")
    world.db.q("update wabas set token_enc = %s", (psycopg2.Binary(blob),))
    world.deliver()
    assert world.db.one("select error_class from outbox") == "fix_setup" and not world.graph.calls


def test_the_rate_limit_holds_a_burst_to_the_numbers_messages_per_second(world):
    world.db.q("update phone_numbers set mps_limit = 2")
    for i in range(5):
        text(world, world.contact(phone="9198000000%02d" % i), key="k%d" % i)
    clock = [100.0]
    limiter = RateLimiter(clock=lambda: clock[0])
    first = world.deliver(limiter=limiter)
    assert (first.sent, first.throttled) == (2, 3)
    assert world.deliver(limiter=limiter).sent == 0                        # same instant: bucket empty
    clock[0] += 1.0
    assert world.deliver(limiter=limiter).sent == 2
    clock[0] += 5.0
    assert world.deliver(limiter=limiter).sent == 1
    assert world.db.one("select count(*) from outbox where state = 'sent'") == 5


def test_two_senders_at_once_never_send_the_same_message_twice(world):
    for i in range(20):
        text(world, world.contact(phone="9198100000%02d" % i), key="k%d" % i)
    world.graph.delay = 0.02
    results = []
    threads = [threading.Thread(target=lambda: results.append(world.deliver(batch=10))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for _ in range(3):
        world.deliver()
    assert len(world.graph.calls) == 20
    assert len({c["body"]["to"] for c in world.graph.calls}) == 20
    assert world.db.one("select count(*) from outbox where state = 'sent'") == 20


# ── a crash mid-send ─────────────────────────────────────────────────────────

def test_a_row_left_sending_by_a_dead_worker_is_closed_not_resent(world):
    text(world, world.contact())
    world.db.q("update outbox set state = 'sending', attempts = 1, updated_at = now() - interval '30 minutes'")
    assert send.reap_stuck(world.connect) == 1
    out = H.only(world.db, "select state, error_class from outbox")
    assert (out["state"], out["error_class"]) == ("failed", "unknown_outcome")
    assert world.db.one("select status from messages") == "unknown"
    assert world.deliver().claimed == 0 and not world.graph.calls          # never resent


def test_a_recently_started_send_is_left_alone(world):
    text(world, world.contact())
    world.db.q("update outbox set state = 'sending', updated_at = now()")
    assert send.reap_stuck(world.connect) == 0


def test_the_status_webhook_can_still_advance_a_message_whose_outcome_was_unknown(world):
    text(world, world.contact())
    world.db.q("update outbox set state = 'sending', updated_at = now() - interval '30 minutes'")
    world.db.q("update messages set wamid = 'wamid.OUT1'")
    send.reap_stuck(world.connect)
    H.deliver(world.db, P.statuses(P.status("wamid.OUT1", "delivered", "9")))
    H.work(world.db)
    assert world.db.one("select status from messages") == "delivered"


# ── campaigns ────────────────────────────────────────────────────────────────

def test_campaign_counters_follow_the_sends_and_close_when_every_message_has_an_outcome(world):
    camp = world.db.one("insert into campaigns (tenant_id, template_name, total) values (%s, 'promo', 2) returning id",
                        (world.tid,))
    a, b = world.contact(inbound_ago=None), world.contact(phone="919800000002", inbound_ago=None)
    for i, c in enumerate((a, b)):
        world.enqueue(send.enqueue_template, contact_id=c, name="promo", idem_key="c%d" % i, campaign_id=camp)
    world.graph.fail["919800000002"] = (131026, 400)
    world.deliver()
    row = H.only(world.db, "select sent, failed, status from campaigns")
    assert row == {"sent": 1, "failed": 1, "status": "done"}
