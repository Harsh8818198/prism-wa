"""The ingest worker against a real Postgres: events -> inbox rows."""
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
from prism_wa import ingest as ingest_mod
from prism_wa import jobs


def seed_outbound(db, tenant, wamid="wamid.OUT1", status="sent", phone=P.ASHA):
    """A message we sent, as the send engine leaves it: conversation + outbound row with a wamid."""
    contact = db.one("insert into contacts (tenant_id, name, phone) values (%s, 'Asha', %s) returning id",
                     (tenant, phone))
    conv = db.one("insert into conversations (tenant_id, contact_id, channel) values (%s, %s, 'whatsapp')"
                  " returning id", (tenant, contact))
    msg = db.one("insert into messages (tenant_id, conversation_id, direction, sender, text, wamid, status)"
                 " values (%s, %s, 'out', 'agent', 'hello', %s, %s) returning id",
                 (tenant, conv, wamid, status))
    return contact, conv, msg


# ── inbound messages ─────────────────────────────────────────────────────────

def test_an_inbound_message_becomes_a_contact_a_conversation_and_a_message(migrated):
    tenant, number = H.onboard(migrated)
    H.deliver(migrated, P.inbound(P.text("hello there", "wamid.T1", "1760000001")))
    stats = H.work(migrated)
    assert stats.done == 1 and stats.dead == 0

    contact = H.only(migrated, "select * from contacts")
    assert (contact["name"], contact["phone"], contact["tenant_id"]) == ("Asha", P.ASHA, tenant)
    assert contact["source_first_seen"] == "whatsapp"
    conv = H.only(migrated, "select * from conversations")
    assert (conv["contact_id"], conv["channel"], conv["number_id"]) == (contact["id"], "whatsapp", number)
    assert conv["last_inbound_at"] is not None and conv["message_count"] == 1
    msg = H.only(migrated, "select * from messages")
    assert (msg["direction"], msg["text"], msg["wamid"], msg["msg_type"], msg["tenant_id"]) == \
        ("in", "hello there", "wamid.T1", "text", tenant)
    assert msg["raw_payload"]["message"]["id"] == "wamid.T1"


def test_a_second_message_from_the_same_person_joins_the_same_conversation(migrated):
    H.onboard(migrated)
    H.deliver(migrated, P.inbound(P.text("one", "wamid.1", "1760000001")))
    H.deliver(migrated, P.inbound(P.text("two", "wamid.2", "1760000002")))
    H.work(migrated)
    assert migrated.one("select count(*) from contacts") == 1
    assert migrated.one("select count(*) from conversations") == 1
    assert migrated.one("select message_count from conversations") == 2


def test_running_the_same_event_twice_adds_one_message(migrated):
    tenant, _ = H.onboard(migrated)
    H.deliver(migrated, P.inbound(P.text("once", "wamid.1")))
    event = migrated.q("select id from wa_events")[0]["id"]
    conn = psycopg2.connect(migrated.dsn)
    try:
        with conn.cursor() as cur:
            ingest_mod.handle_event_job(cur, tenant, {"event_id": str(event)})
            ingest_mod.handle_event_job(cur, tenant, {"event_id": str(event)})
        conn.commit()
    finally:
        conn.close()
    assert migrated.one("select count(*) from messages") == 1
    assert migrated.one("select message_count from conversations") == 1


def test_a_username_only_sender_is_a_contact_with_no_phone_and_is_recognised_again(migrated):
    H.onboard(migrated)
    H.deliver(migrated, P.username_only_sender())
    H.work(migrated)
    c = H.only(migrated, "select * from contacts")
    assert c["phone"] is None and c["bsuid"] == "IN.abc123XYZ" and c["wa_username"] == "@ravi"

    again = P.username_only_sender()
    again["entry"][0]["changes"][0]["value"]["messages"][0].update(id="wamid.U2", timestamp="1760000099")
    H.deliver(migrated, again)
    H.work(migrated)
    assert migrated.one("select count(*) from contacts") == 1
    assert migrated.one("select message_count from conversations") == 2


def test_media_without_a_caption_gets_a_readable_placeholder(migrated):
    H.onboard(migrated)
    msg = P.image("media-9", "")
    H.deliver(migrated, P.inbound(msg))
    H.work(migrated)
    row = H.only(migrated, "select text, msg_type, media_id, mime_type from messages")
    assert row == {"text": "[image]", "msg_type": "image", "media_id": "media-9", "mime_type": "image/jpeg"}


def test_a_click_to_whatsapp_ad_marks_where_the_contact_came_from(migrated):
    H.onboard(migrated)
    H.deliver(migrated, P.inbound(P.ad_click()))
    H.work(migrated)
    assert migrated.one("select source_first_seen from contacts") == "whatsapp_ad"


def test_an_unrouted_event_waits_with_no_job_and_works_once_the_number_is_onboarded(migrated):
    H.deliver(migrated, P.inbound(P.text("early", "wamid.E1")))
    assert migrated.one("select count(*) from wa_jobs") == 0
    assert migrated.one("select count(*) from messages") == 0


def test_the_inbox_tables_are_tenant_scoped(migrated):
    a, _ = H.onboard(migrated, "orena", "W1", "P1")
    b, _ = H.onboard(migrated, "shakti", "W2", "P2")
    H.deliver(migrated, P.inbound(P.text("a", "wamid.A"), pnid="P1", waba="W1"))
    H.deliver(migrated, P.inbound(P.text("b", "wamid.B"), pnid="P2", waba="W2"))
    H.work(migrated)
    assert migrated.one("select count(*) from contacts where tenant_id = %s", (a,)) == 1
    assert migrated.one("select count(*) from contacts where tenant_id = %s", (b,)) == 1
    # the same phone number is two different contacts, one per tenant
    assert migrated.one("select count(distinct id) from contacts") == 2


# ── statuses ─────────────────────────────────────────────────────────────────

def test_statuses_move_a_message_forward_and_never_back(migrated):
    tenant, _ = H.onboard(migrated)
    seed_outbound(migrated, tenant, status="sent")
    for state, ts in (("delivered", "10"), ("read", "20"), ("delivered", "30"), ("sent", "40")):
        H.deliver(migrated, P.statuses(P.status("wamid.OUT1", state, ts)))
    H.work(migrated)
    row = H.only(migrated, "select status, delivered, read_at from messages")
    assert row["status"] == "read" and row["delivered"] is True and row["read_at"] is not None


def test_a_failure_after_delivery_does_not_undo_it_but_one_after_sending_does(migrated):
    tenant, _ = H.onboard(migrated)
    seed_outbound(migrated, tenant, "wamid.A", "delivered")
    seed_outbound(migrated, tenant, "wamid.B", "sent", phone="919800000002")
    H.deliver(migrated, P.statuses(P.failed_status("wamid.A", 131049, "5")))
    H.deliver(migrated, P.statuses(P.failed_status("wamid.B", 131049, "6")))
    H.work(migrated)
    rows = {r["wamid"]: r for r in migrated.q("select wamid, status, error from messages")}
    assert rows["wamid.A"]["status"] == "delivered" and rows["wamid.A"]["error"] is None
    assert rows["wamid.B"]["status"] == "failed" and rows["wamid.B"]["error"]["code"] == 131049


def test_pricing_from_a_status_is_kept_on_the_message(migrated):
    tenant, _ = H.onboard(migrated)
    seed_outbound(migrated, tenant)
    H.deliver(migrated, P.statuses(P.status("wamid.OUT1", "delivered", "9", pricing=P.PRICING)))
    H.work(migrated)
    assert migrated.one("select pricing->>'category' from messages") == "marketing"


def test_an_opted_out_failure_flips_the_contacts_opt_out_flag(migrated):
    tenant, _ = H.onboard(migrated)
    contact, _, _ = seed_outbound(migrated, tenant)
    H.deliver(migrated, P.statuses(P.failed_status("wamid.OUT1", 131050, "7")))
    H.work(migrated)
    assert migrated.one("select opt_out from contacts where id = %s", (contact,)) is True


def test_a_failed_campaign_message_moves_the_counters(migrated):
    tenant, number = H.onboard(migrated)
    contact, conv, msg = seed_outbound(migrated, tenant)
    camp = migrated.one("insert into campaigns (tenant_id, template_name, total, sent) values (%s, 't', 1, 1)"
                        " returning id", (tenant,))
    migrated.q("insert into outbox (tenant_id, number_id, to_phone, kind, payload, idem_key, message_id,"
               " campaign_id, state, wamid) values (%s, %s, %s, 'template', '{}', 'k', %s, %s, 'sent', 'wamid.OUT1')",
               (tenant, number, P.ASHA, msg, camp))
    H.deliver(migrated, P.statuses(P.failed_status("wamid.OUT1", 131049, "8")))
    H.work(migrated)
    assert migrated.q("select sent, failed from campaigns")[0] == {"sent": 0, "failed": 1}


def test_a_status_that_beats_its_message_waits_then_applies(migrated):
    tenant, _ = H.onboard(migrated)
    H.deliver(migrated, P.statuses(P.status("wamid.OUT1", "delivered", "10")))
    now = jobs.utcnow()
    stats = H.work(migrated, now=now)
    assert stats.deferred >= 1 and stats.dead == 0
    assert migrated.one("select attempts from wa_jobs") == 0          # waiting is not failing

    seed_outbound(migrated, tenant, status="sent")
    H.work(migrated, now=now + timedelta(seconds=60))
    assert migrated.one("select status from messages") == "delivered"


def test_a_status_for_a_message_this_platform_never_sent_is_dropped_after_a_while(migrated):
    H.onboard(migrated)
    H.deliver(migrated, P.statuses(P.status("wamid.NOT.OURS", "delivered", "10")))
    migrated.q("update wa_events set received_at = now() - interval '10 minutes'")
    stats = H.work(migrated)
    assert stats.done == 1 and stats.deferred == 0 and stats.dead == 0


def test_next_status_rules():
    n = ingest_mod.next_status
    assert n(None, "sent") == "sent" and n("sent", "delivered") == "delivered"
    assert n("read", "delivered") == "read" and n("read", "failed") == "read"
    assert n("sent", "failed") == "failed" and n("failed", "read") == "failed"
    assert n("sent", "deleted") == "sent" and n("queued", "sent") == "sent"
