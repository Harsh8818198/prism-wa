"""Merging two contacts loses nothing that 0004-0009 hang off them."""
import json

import helpers as H
import psycopg2
import pytest

from prism_wa import consent, contacts


def make(db, tid, name, phone, **cols):
    keys = ["tenant_id", "name", "phone"] + list(cols)
    vals = [tid, name, phone] + list(cols.values())
    return db.one("insert into contacts (%s) values (%s) returning id" % (", ".join(keys), ", ".join(["%s"] * len(vals))), vals)


def merge(db, survivor, loser):
    return db.one("select wa_merge_contacts(%s, %s, 'test')", (survivor, loser))


@pytest.fixture
def pair(migrated):
    tid = H.tenant(migrated, "orena")
    keep = make(migrated, tid, "Asha", "919800000001", tags=["vip"])
    dup = make(migrated, tid, "Asha S", "919800000002", tags=["cold"])
    return migrated, tid, keep, dup


def test_attributes_are_united_and_the_survivor_wins_a_clash(pair):
    db, tid, keep, dup = pair
    conn = psycopg2.connect(db.dsn)
    with conn.cursor() as cur:
        for key in ("City", "Plan", "Budget"):
            contacts.define_attribute(cur, tid, key)
        contacts.set_attribute(cur, tid, keep, "City", "Vadodara")
        contacts.set_attribute(cur, tid, dup, "City", "Surat")           # clash: keep's value stays
        contacts.set_attribute(cur, tid, dup, "Plan", "Gold")            # only the duplicate has it
        contacts.set_attribute(cur, tid, keep, "Budget", "10")
    conn.commit()
    conn.close()
    merge(db, keep, dup)
    got = {r["key"]: r["value"] for r in db.q("select key, value from contact_attributes where contact_id = %s", (keep,))}
    assert got == {"City": "Vadodara", "Plan": "Gold", "Budget": "10"}
    assert db.one("select count(*) from contact_attributes") == 3 and db.one("select count(*) from contacts") == 1


def test_all_consent_history_survives_and_an_opt_out_on_either_side_wins(pair):
    db, tid, keep, dup = pair
    conn = psycopg2.connect(db.dsn)
    with conn.cursor() as cur:
        consent.record(cur, tid, keep, "opt_in", "csv_import", "leads.csv")
        consent.record(cur, tid, dup, "opt_in", "manual", "at the counter")
        consent.record(cur, tid, dup, "opt_out", "keyword", "STOP")
    conn.commit()
    conn.close()
    merge(db, keep, dup)
    assert db.one("select count(*) from consent_ledger where contact_id = %s", (keep,)) == 3
    assert db.q("select opt_out, opted_in from contacts where id = %s", (keep,))[0] == {"opt_out": True, "opted_in": False}


def test_opted_in_survives_a_merge_only_when_nobody_opted_out(pair):
    db, tid, keep, dup = pair
    conn = psycopg2.connect(db.dsn)
    with conn.cursor() as cur:
        consent.record(cur, tid, dup, "opt_in", "manual", "yes")
    conn.commit()
    conn.close()
    merge(db, keep, dup)
    assert db.q("select opt_out, opted_in from contacts")[0] == {"opt_out": False, "opted_in": True}


def test_the_contacts_own_new_columns_are_carried(migrated):
    tid = H.tenant(migrated, "orena")
    keep = make(migrated, tid, "A", "919800000001")
    dup = make(migrated, tid, "B", "919800000002", blocked=True, first_msg_tag="quote please",
               last_seen="2026-10-01T00:00:00Z")
    migrated.q("update contacts set last_seen = '2026-09-01T00:00:00Z' where id = %s", (keep,))
    merge(migrated, keep, dup)
    row = migrated.q("select blocked, first_msg_tag, last_seen from contacts")[0]
    assert row["blocked"] is True and row["first_msg_tag"] == "quote please"
    assert row["last_seen"].isoformat().startswith("2026-10-01")


def test_campaign_membership_segments_and_clicks_move_without_duplicates_or_loss(pair):
    db, tid, keep, dup = pair
    c1 = db.one("insert into campaigns (tenant_id, template_name, total) values (%s, 'one', 2) returning id", (tid,))
    c2 = db.one("insert into campaigns (tenant_id, template_name, total) values (%s, 'two', 1) returning id", (tid,))
    rec = {}
    for campaign, contact in ((c1, keep), (c1, dup), (c2, dup)):
        rec[(campaign, contact)] = db.one(
            "insert into campaign_recipients (tenant_id, campaign_id, contact_id) values (%s, %s, %s) returning id",
            (tid, campaign, contact))
    for recipient, token in ((rec[(c1, dup)], "tokdup1"), (rec[(c2, dup)], "tokdup2")):
        campaign = c1 if recipient == rec[(c1, dup)] else c2
        db.q("insert into short_links (token, tenant_id, campaign_id, recipient_id, contact_id, target) values (%s,%s,%s,%s,%s,'https://x.in')",
             (token, tid, campaign, recipient, dup))
        db.q("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind, url) values (%s,%s,%s,%s,'link','https://x.in')",
             (tid, campaign, recipient, dup))
    seg_both = db.one("insert into segments (tenant_id, name, filter) values (%s, 'both', '{}') returning id", (tid,))
    seg_dup = db.one("insert into segments (tenant_id, name, filter) values (%s, 'dup only', '{}') returning id", (tid,))
    for seg, contact in ((seg_both, keep), (seg_both, dup), (seg_dup, dup)):
        db.q("insert into segment_members (segment_id, contact_id, tenant_id) values (%s,%s,%s)", (seg, contact, tid))

    merge(db, keep, dup)

    assert db.one("select count(*) from contacts") == 1
    # campaign 1: only the survivor's row remains; campaign 2: the duplicate's row moved over
    assert {r["campaign_id"] for r in db.q("select campaign_id from campaign_recipients")} == {c1, c2}
    assert db.one("select count(*) from campaign_recipients where contact_id = %s", (keep,)) == 2
    # every click and link still exists, now pointing at the survivor and at a recipient row that still exists
    clicks = db.q("select contact_id, recipient_id from click_events")
    assert len(clicks) == 2 and {c["contact_id"] for c in clicks} == {keep}
    live = {r["id"] for r in db.q("select id from campaign_recipients")}
    assert all(c["recipient_id"] in live for c in clicks)
    assert db.one("select count(*) from short_links where contact_id = %s", (keep,)) == 2
    assert all(r["recipient_id"] in live for r in db.q("select recipient_id from short_links"))
    assert {r["segment_id"] for r in db.q("select segment_id from segment_members")} == {seg_both, seg_dup}
    assert db.one("select count(*) from segment_members") == 2


def test_the_merge_is_still_tenant_safe(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    x, y = make(migrated, a, "X", "919800000001"), make(migrated, b, "Y", "919800000002")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        merge(migrated, x, y)
    assert migrated.one("select count(*) from contacts") == 2


def test_folding_two_conversations_keeps_the_notes_the_history_and_the_owner(pair):
    db, tid, keep, dup = pair
    agent = db.one("insert into agents (tenant_id, seat_key, name, role) values (%s, 's1', 'Asha', 'owner') returning id", (tid,))
    conv_keep = db.one("insert into conversations (tenant_id, contact_id, channel, status) values (%s,%s,'whatsapp','active') returning id", (tid, keep))
    conv_dup = db.one("insert into conversations (tenant_id, contact_id, channel, status, assignee_id, last_inbound_at)"
                      " values (%s,%s,'whatsapp','intervened',%s,'2026-10-01T00:00:00Z') returning id", (tid, dup, agent))
    db.q("insert into conversation_notes (tenant_id, conversation_id, agent_id, text) values (%s,%s,%s,'asked for a discount')", (tid, conv_dup, agent))
    db.q("insert into conversation_events (tenant_id, conversation_id, kind, actor_id) values (%s,%s,'intervened',%s)", (tid, conv_dup, agent))
    merge(db, keep, dup)
    assert db.one("select count(*) from conversations") == 1
    row = db.q("select id, status, assignee_id from conversations")[0]
    assert row == {"id": conv_keep, "status": "intervened", "assignee_id": agent}
    assert db.one("select count(*) from conversation_notes where conversation_id = %s", (conv_keep,)) == 1
    assert db.one("select count(*) from conversation_events where conversation_id = %s", (conv_keep,)) == 1


def test_the_timeline_of_a_merged_contact_follows_the_survivor(pair):
    db, tid, keep, dup = pair
    db.q("insert into contact_events (tenant_id, contact_id, kind, detail) values (%s, %s, 'tag_added', '{\"tag\": \"cold\"}')", (tid, dup))
    db.q("insert into contact_events (tenant_id, contact_id, kind) values (%s, %s, 'created')", (tid, keep))
    merge(db, keep, dup)
    kinds = sorted(r["kind"] for r in db.q("select kind from contact_events where contact_id = %s", (keep,)))
    assert kinds == ["created", "merged", "tag_added"]
    merged = db.q("select detail from contact_events where kind = 'merged'")[0]["detail"]
    assert merged["merged_name"] == "Asha S" and merged["reason"] == "test"
    assert db.q("select 1 from contact_events where contact_id = %s", (dup,)) == []
