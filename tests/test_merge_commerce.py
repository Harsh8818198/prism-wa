"""Merging two contacts and erasing one, for the tables added since the merge was written: orders and what a person filled in on a form (0029) and the automatic-message
tables (0027). The guard in test_identity.py makes sure nobody adds a table that points at a contact without teaching the merge about it; this proves the teaching."""
import json

import helpers as H
import pytest

from prism_wa import contacts


@pytest.fixture
def world(migrated):
    tid, number = H.onboard(migrated)

    class W:
        db, tid_ = migrated, tid

        @staticmethod
        def person(name, phone):
            return migrated.one("insert into contacts (tenant_id, name, phone) values (%s, %s, %s) returning id", (tid, name, phone))

        @staticmethod
        def order(who, reference, **kw):
            return migrated.one("insert into orders (tenant_id, contact_id, reference_id, status, items, total_paise, address, note) values (%s, %s, %s, 'draft', %s, 100, %s, %s) returning id",
                                (tid, who, reference, json.dumps([]), json.dumps(kw.get("address")) if kw.get("address") else None, kw.get("note")))

        @staticmethod
        def answer(who, token, answers=None):
            return migrated.one("insert into form_responses (tenant_id, contact_id, flow_token, answers) values (%s, %s, %s, %s) returning id", (tid, who, token, json.dumps(answers or {"q": "a"})))

        @staticmethod
        def auto():
            return migrated.one("insert into autos (tenant_id, kind, name, config) values (%s, 'date', 'Birthday', '{}') returning id", (tid,))

        @staticmethod
        def merge(survivor, loser):
            return migrated.q("select wa_merge_contacts(%s, %s)", (survivor, loser))

    return W


def test_orders_and_form_answers_follow_the_person_into_the_survivor(world):
    keep, gone = world.person("Asha", "919800000001"), world.person("Asha P", "919800000002")
    o = world.order(gone, "REF-1")
    r = world.answer(gone, "tok-1")
    world.merge(keep, gone)
    assert world.db.one("select contact_id from orders where id = %s", (o,)) == keep
    assert world.db.one("select contact_id from form_responses where id = %s", (r,)) == keep


def test_automatic_message_rows_follow_unless_the_survivor_already_has_the_same(world):
    keep, gone = world.person("Asha", "919800000001"), world.person("Asha P", "919800000002")
    a, b = world.auto(), world.auto()
    for who, auto, occasion in ((keep, a, "2026"), (gone, a, "2026"), (gone, a, "2027"), (gone, b, "2026")):
        world.db.q("insert into auto_log (tenant_id, auto_id, contact_id, occasion, state) values (%s, %s, %s, %s, 'sent')", (world.tid_, auto, who, occasion))
    for who, auto in ((keep, a), (gone, a), (gone, b)):
        world.db.q("insert into auto_enrolments (auto_id, contact_id, tenant_id) values (%s, %s, %s)", (auto, who, world.tid_))
    world.merge(keep, gone)
    log = sorted((str(r["auto_id"]) == str(a), r["occasion"]) for r in world.db.q("select auto_id, occasion from auto_log where contact_id = %s", (keep,)))
    assert log == [(False, "2026"), (True, "2026"), (True, "2027")]                       # a/2026 once (not sent twice), a/2027 and b/2026 moved over
    assert world.db.one("select count(*) from auto_enrolments where contact_id = %s", (keep,)) == 2
    assert world.db.one("select count(*) from auto_log") == 3 and world.db.one("select count(*) from auto_enrolments") == 2


def test_erasing_a_person_removes_their_answers_and_the_address_on_their_orders_but_not_the_orders(world):
    who = world.person("Asha", "919800000001")
    other = world.person("Ravi", "919800000003")
    o = world.order(who, "REF-2", address={"line": "12 Alkapuri Road"}, note="Leave at the gate")
    kept = world.order(other, "REF-3", address={"line": "Elsewhere"}, note="mine")
    world.answer(who, "tok-2")
    world.answer(other, "tok-3")
    import psycopg2
    c = psycopg2.connect(world.db.dsn)
    with c.cursor() as cur:
        assert contacts.erase_contact(cur, world.tid_, who) is True
    c.commit()
    c.close()
    row = world.db.q("select contact_id, address, note from orders where id = %s", (o,))[0]
    assert row["contact_id"] is None and row["address"] is None and row["note"] is None
    assert world.db.one("select count(*) from form_responses") == 1
    other_row = world.db.q("select address, note from orders where id = %s", (kept,))[0]
    assert other_row["address"] == {"line": "Elsewhere"} and other_row["note"] == "mine"
