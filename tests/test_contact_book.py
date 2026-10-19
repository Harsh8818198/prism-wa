"""The contact book: the table, the profile, tags, attributes, blocking, import mapping and the import log."""
import json
from datetime import datetime, timedelta, timezone

import helpers as H
import psycopg2
import pytest

from prism_wa import contact_book as book
from prism_wa import contacts, plans
from prism_wa.contact_book import BookError

NOW = datetime.now(timezone.utc)


@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tenant_id,))

    class W:
        db, tid = migrated, tenant_id

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, tenant=None, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tenant or tenant_id, *a, **kw)
                conn.commit()
                return out
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        @classmethod
        def add(cls, name, phone, *, tags=(), opted_in=True, last_seen=None, source=None, email=None, company=None, opt_out=False):
            return migrated.one(
                "insert into contacts (tenant_id, name, phone, tags, opted_in, opt_out, last_seen, source_first_seen, email, company)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (tenant_id, name, phone, list(tags), opted_in, opt_out, last_seen, source, email, company))

        @classmethod
        def chat(cls, contact_id, status="active", inbound=2, outbound=1, template=0):
            conv = migrated.one("insert into conversations (tenant_id, contact_id, channel, status, last_active_at)"
                                " values (%s,%s,'whatsapp',%s,now()) returning id", (tenant_id, contact_id, status))
            for i in range(inbound):
                migrated.q("insert into messages (tenant_id, conversation_id, direction, text) values (%s,%s,'in',%s)", (tenant_id, conv, "hi %d" % i))
            for i in range(outbound):
                migrated.q("insert into messages (tenant_id, conversation_id, direction, text) values (%s,%s,'out',%s)", (tenant_id, conv, "reply %d" % i))
            return conv

    return W


# ── the table ────────────────────────────────────────────────────────────────

def test_the_table_carries_the_columns_a_team_works_from(world):
    asha = world.add("Asha Shah", "919800100001", tags=["vip"], last_seen=NOW - timedelta(hours=2), source="IMPORT", email="a@x.in", company="Shah Steel")
    world.add("Ravi Patel", "919800100002", last_seen=NOW - timedelta(days=3), source="ORGANIC")
    world.chat(asha, status="requesting")
    out = world.do(book.list_contacts, sort="name", direction="asc")
    assert out["total"] == 2
    first, second = out["contacts"]
    assert (first["name"], first["source"], first["state"], first["window_open"]) == ("Asha Shah", "IMPORT", "requesting", True)
    assert first["tags"] == ["vip"] and first["company"] == "Shah Steel" and first["opted_in"] is True
    assert (second["name"], second["window_open"], second["state"]) == ("Ravi Patel", False, None)


def test_sorting_and_paging(world):
    for i in range(7):
        world.add("P%d" % i, "91980010%04d" % i)
    page = world.do(book.list_contacts, sort="name", direction="desc", limit=3, offset=3)
    assert page["total"] == 7 and [c["name"] for c in page["contacts"]] == ["P3", "P2", "P1"]
    assert [c["name"] for c in world.do(book.list_contacts, sort="name", direction="asc", limit=2)["contacts"]] == ["P0", "P1"]


def test_search_finds_a_name_a_number_an_email_or_a_company(world):
    world.add("Asha Shah", "919800100001", email="asha@shah.in", company="Shah Steel")
    world.add("Ravi Patel", "919800100002", company="Patel Constructions")
    find = lambda text: [c["name"] for c in world.do(book.list_contacts, search=text)["contacts"]]          # noqa: E731
    assert find("asha") == ["Asha Shah"] and find("0100002") == ["Ravi Patel"] and find("patel constr") == ["Ravi Patel"]
    assert find("shah.in") == ["Asha Shah"] and find("+91 98001 00001") == ["Asha Shah"] and find("nobody") == []


def test_a_filter_narrows_the_table_by_tag_attribute_and_state(world):
    a = world.add("Asha", "919800100001", tags=["vip", "builder"])
    r = world.add("Ravi", "919800100002", tags=["builder"])
    world.add("Meera", "919800100003")
    world.do(book.define_attribute, "City")
    world.do(book.update_contact, a, changes={"attributes": {"City": "Vadodara"}})
    world.do(book.update_contact, r, changes={"attributes": {"City": "Surat"}})
    names = lambda f: sorted(c["name"] for c in world.do(book.list_contacts, filter_=f)["contacts"])      # noqa: E731
    assert names({"field": "tags", "op": "has_any", "value": ["vip"]}) == ["Asha"]
    assert names({"all": [{"field": "tags", "op": "has", "value": ["builder"]}, {"field": "attr:City", "op": "eq", "value": "Surat"}]}) == ["Ravi"]
    assert names({"field": "tags", "op": "has_none", "value": ["builder"]}) == ["Meera"]
    out = world.do(book.list_contacts, filter_={"field": "tags", "op": "has", "value": ["builder"]})
    assert out["total"] == 2 and out["attribute_keys"] == ["City"]
    assert {c["name"]: c["attrs"] for c in out["contacts"]} == {"Asha": {"City": "Vadodara"}, "Ravi": {"City": "Surat"}}


def test_an_agent_who_may_not_see_numbers_gets_them_masked(world):
    world.add("Asha", "919800100001")
    assert world.do(book.list_contacts, masked=True)["contacts"][0]["phone"] == "********0001"


# ── one person ───────────────────────────────────────────────────────────────

def test_the_profile_counts_what_was_sent_and_what_was_asked(world):
    asha = world.add("Asha", "919800100001", last_seen=NOW)
    conv = world.chat(asha, status="intervened", inbound=3, outbound=2)
    world.db.q("insert into conversation_events (tenant_id, conversation_id, kind) values (%s,%s,'requested')", (world.tid, conv))
    p = world.do(book.get_profile, asha)
    assert p["name"] == "Asha" and p["window_open"] is True and p["state"] == "intervened"
    assert p["counts"] == {"template_messages": 0, "session_messages": 5, "unresolved_queries": 1}


def test_the_journey_is_everything_that_happened_newest_first(world):
    asha = world.add("Asha", "919800100001")
    conv = world.chat(asha)
    actor = {"id": None, "name": "Meera"}
    world.do(book.set_tags, [asha], add=["vip"], actor=actor)
    world.db.q("insert into conversation_events (tenant_id, conversation_id, kind, at) values (%s,%s,'intervened', now() + interval '1 minute')", (world.tid, conv))
    world.db.q("insert into consent_ledger (tenant_id, contact_id, action, source, at) values (%s,%s,'opt_in','manual', now() - interval '1 hour')", (world.tid, asha))
    kinds = [e["kind"] for e in world.do(book.journey, asha)]
    assert kinds[0] == "chat_intervened" and "tag_added" in kinds and kinds[-1] == "opt_in"
    tag = next(e for e in world.do(book.journey, asha) if e["kind"] == "tag_added")
    assert tag["who"] == "Meera" and tag["detail"] == {"tags": ["vip"]}


def test_a_person_can_be_added_without_a_file(world):
    world.do(book.define_attribute, "City")
    out = world.do(book.create_contact, name="Asha", phone="98765 43210", email="a@x.in", tags=["vip"], attributes={"City": "Vadodara"},
                   attest_consent=True, evidence="asked at the counter")
    assert out["created"] is True
    p = world.do(book.get_profile, out["contact_id"])
    assert (p["phone"], p["email"], p["tags"], p["attrs"], p["opted_in"]) == ("919876543210", "a@x.in", ["vip"], {"City": "Vadodara"}, True)
    again = world.do(book.create_contact, name="Asha S", phone="+91 98765 43210")
    assert again["created"] is False and again["contact_id"] == out["contact_id"]


def test_a_bad_number_or_a_taken_email_is_refused_in_words(world):
    with pytest.raises(BookError) as bad:
        world.do(book.create_contact, name="X", phone="12345")
    assert bad.value.reason == "bad_phone" and "country code" in str(bad.value)
    world.add("Asha", "919800100001", email="a@x.in")
    with pytest.raises(BookError) as taken:
        world.do(book.create_contact, name="Y", phone="9876543210", email="a@x.in")
    assert taken.value.reason == "email_taken"


def test_a_name_email_company_and_attribute_can_be_changed_or_cleared(world):
    a = world.add("Asha", "919800100001", company="Old Co")
    world.do(book.define_attribute, "City")
    p = world.do(book.update_contact, a, changes={"name": "Asha Shah", "company": "New Co", "attributes": {"City": "Surat"}})
    assert (p["name"], p["company"]) == ("Asha Shah", "New Co")
    assert world.do(book.get_profile, a)["attrs"] == {"City": "Surat"}
    world.do(book.update_contact, a, changes={"attributes": {"City": ""}})
    assert world.do(book.get_profile, a)["attrs"] == {}
    with pytest.raises(BookError):
        world.do(book.update_contact, a, changes={"name": "  "})
    with pytest.raises(BookError) as bad:
        world.do(book.update_contact, a, changes={"attributes": {"Nope": "x"}})
    assert bad.value.reason == "bad_attribute"


def test_blocking_stops_a_person_and_is_recorded(world):
    a = world.add("Asha", "919800100001")
    assert world.do(book.set_blocked, [a], True, {"id": None, "name": "Meera"}) == 1
    assert world.do(book.set_blocked, [a], True) == 0                                   # already blocked
    assert [c["name"] for c in world.do(book.list_contacts, filter_={"field": "blocked", "op": "is", "value": True})["contacts"]] == ["Asha"]
    assert "blocked" in [e["kind"] for e in world.do(book.journey, a)]


def test_erasing_removes_the_person(world):
    a = world.add("Asha", "919800100001")
    world.add("Ravi", "919800100002")
    assert world.do(book.erase, [a]) == 1
    assert world.do(book.list_contacts)["total"] == 1


# ── tags ─────────────────────────────────────────────────────────────────────

def test_tags_are_added_and_removed_for_many_at_once(world):
    ids = [world.add("P%d" % i, "91980010%04d" % i, tags=["old"]) for i in range(3)]
    assert world.do(book.set_tags, ids, add=["vip", "diwali"], remove=["old"])["changed"] >= 3
    assert sorted({t for c in world.do(book.list_contacts)["contacts"] for t in c["tags"]}) == ["diwali", "vip"]
    assert world.do(book.set_tags, ids, add=["vip"])["changed"] == 0                     # nothing new to add


def test_a_tag_can_exist_before_anyone_has_it_and_is_counted(world):
    world.add("A", "919800100001", tags=["vip"])
    world.do(book.create_tag, "builder", "#2563eb")
    tags = {t["name"]: t for t in world.do(book.list_tags)}
    assert tags["vip"]["contacts"] == 1 and tags["vip"]["registered"] is False
    assert tags["builder"]["contacts"] == 0 and tags["builder"]["registered"] is True and tags["builder"]["color"] == "#2563eb"
    with pytest.raises(BookError) as dup:
        world.do(book.create_tag, "builder")                                              # already made on purpose
    assert dup.value.reason == "tag_exists"
    world.do(book.create_tag, "vip")                                                       # one people carry can be made official
    assert {x["name"]: x["registered"] for x in world.do(book.list_tags)}["vip"] is True


@pytest.mark.parametrize("bad", ["", "   ", "a,b", "a|b", "x" * 61])
def test_a_tag_name_must_be_clean(world, bad):
    with pytest.raises(BookError) as e:
        world.do(book.create_tag, bad)
    assert e.value.reason == "bad_tag"


def test_renaming_a_tag_moves_everyone_and_refuses_a_clash(world):
    a = world.add("A", "919800100001", tags=["vip"])
    world.add("B", "919800100002", tags=["gold"])
    world.do(book.create_tag, "vip")
    assert world.do(book.rename_tag, "vip", "platinum") == 1
    assert world.do(book.get_profile, a)["tags"] == ["platinum"]
    assert "platinum" in {t["name"] for t in world.do(book.list_tags)} and "vip" not in {t["name"] for t in world.do(book.list_tags)}
    with pytest.raises(BookError):
        world.do(book.rename_tag, "platinum", "gold")
    with pytest.raises(BookError):
        world.do(book.rename_tag, "ghost", "x")


def test_deleting_a_tag_takes_it_off_everyone(world):
    a = world.add("A", "919800100001", tags=["vip", "keep"])
    world.do(book.create_tag, "vip")
    assert world.do(book.delete_tag, "vip") == 1
    assert world.do(book.get_profile, a)["tags"] == ["keep"]
    assert "vip" not in {t["name"] for t in world.do(book.list_tags)}


def test_the_plan_caps_distinct_tags(world):
    world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}' where tenant_id = %s", (world.tid,))
    a = world.add("A", "919800100001")
    world.do(book.set_tags, [a], add=["t%d" % i for i in range(10)])                    # the plan's ten
    with pytest.raises(plans.PlanLimit):
        world.do(book.set_tags, [a], add=["one-too-many"])
    with pytest.raises(plans.PlanLimit):
        world.do(book.create_tag, "also-too-many")
    assert world.do(book.set_tags, [a], add=["t1"])["changed"] == 0                     # an existing tag is never refused


# ── attributes ───────────────────────────────────────────────────────────────

def test_attributes_list_how_many_people_have_a_value(world):
    a, b = world.add("A", "919800100001"), world.add("B", "919800100002")
    world.do(book.define_attribute, "City")
    world.do(book.define_attribute, "Budget", "number")
    world.do(book.update_contact, a, changes={"attributes": {"City": "Surat", "Budget": "1,50,000"}})
    world.do(book.update_contact, b, changes={"attributes": {"City": "Vadodara"}})
    attrs = {x["key"]: x for x in world.do(book.list_attributes)}
    assert (attrs["City"]["filled"], attrs["Budget"]["filled"], attrs["Budget"]["type"]) == (2, 1, "number")
    with pytest.raises(BookError):
        world.do(book.define_attribute, "9bad name")


def test_a_workspace_wide_attribute_holds_one_value(world):
    world.do(book.define_attribute, "Company", is_global=True)
    world.do(book.define_attribute, "City")
    world.do(book.set_global_value, "Company", "Patel Constructions")
    world.do(book.set_global_value, "Company", "Patel Constructions Pvt Ltd")
    assert world.do(book.global_values) == {"Company": "Patel Constructions Pvt Ltd"}
    assert {a["key"]: a["value"] for a in world.do(book.list_attributes)}["Company"] == "Patel Constructions Pvt Ltd"
    with pytest.raises(BookError) as e:
        world.do(book.set_global_value, "City", "x")
    assert e.value.reason == "not_global"
    with pytest.raises(BookError):
        world.do(book.set_global_value, "Nope", "x")


def test_deleting_an_attribute_removes_it_and_its_values(world):
    a = world.add("A", "919800100001")
    world.do(book.define_attribute, "Company", is_global=True)
    world.do(book.set_global_value, "Company", "X")
    world.do(book.define_attribute, "City")
    world.do(book.update_contact, a, changes={"attributes": {"City": "Surat"}})
    assert world.do(book.delete_attribute, "City") is True and world.do(book.delete_attribute, "City") is False
    assert world.do(book.get_profile, a)["attrs"] == {}
    world.do(book.delete_attribute, "Company")
    assert world.do(book.global_values) == {}


# ── import mapping and the log ───────────────────────────────────────────────

def test_a_file_with_headers_we_would_not_guess_can_be_mapped(world):
    text = "Customer,Ph,Town,Lead from,Internal notes\nAsha,9876543210,Vadodara,Facebook,call back\nRavi,9876543211,Surat,,\n"
    mapping = {"Customer": "name", "Ph": "mobile", "Town": "attr:City", "Lead from": "source", "Internal notes": "ignore"}
    conn = world.conn()
    report = contacts.import_csv(conn, world.tid, text, mapping=mapping, source="IMPORT")
    conn.close()
    assert (report.rows, report.created, report.invalid) == (2, 2, 0)
    people = {c["name"]: c for c in world.do(book.list_contacts)["contacts"]}
    assert people["Asha"]["attrs"] == {"City": "Vadodara"} and people["Asha"]["source"] == "Facebook" and people["Ravi"]["source"] == "IMPORT"


def test_without_a_mapping_a_source_column_is_still_understood(world):
    conn = world.conn()
    contacts.import_csv(conn, world.tid, "Name,Mobile,Source\nAsha,9876543210,Expo 2026\n")
    conn.close()
    assert world.do(book.list_contacts)["contacts"][0]["source"] == "Expo 2026"


def test_every_import_is_logged_with_a_failure_file(world):
    text = "Name,Mobile\nAsha,9876543210\nBad,12\nRavi,9876543211\n"
    conn = world.conn()
    report = contacts.import_csv(conn, world.tid, text).as_dict()
    conn.close()
    import_id = world.do(book.log_import, report, file_name="leads.csv", source="IMPORT", by_name="Meera")
    listed = world.do(book.list_imports)
    assert [(i["file_name"], i["rows"], i["created"], i["invalid"], i["by_name"]) for i in listed] == [("leads.csv", 3, 2, 1, "Meera")]
    got = world.do(book.import_report, import_id)
    csv_text = book.failures_csv(got["report"])
    assert csv_text.splitlines()[0] == "Row,Why it did not import" and "3,'12' is not a valid phone number" in csv_text.replace('"', "")
    with pytest.raises(BookError):
        world.do(book.import_report, "00000000-0000-0000-0000-000000000000")


# ── two workspaces ───────────────────────────────────────────────────────────

def test_one_workspace_never_sees_anothers_people(world):
    a = world.add("Asha", "919800100001", tags=["vip"])
    other = world.db.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    assert world.do(book.list_contacts, tenant=other)["total"] == 0
    for fn, args in ((book.get_profile, (a,)), (book.journey, (a,)), (book.update_contact, (a,))):
        with pytest.raises(BookError) as e:
            world.do(fn, *args, tenant=other, **({"changes": {"name": "x"}} if fn is book.update_contact else {}))
        assert e.value.reason == "unknown_contact"
    assert world.do(book.set_tags, [a], add=["stolen"], tenant=other)["changed"] == 0
    assert world.do(book.get_profile, a)["tags"] == ["vip"]
    assert world.do(book.erase, [a], tenant=other) == 0
    assert world.do(book.list_tags, tenant=other) == []


def test_a_person_can_be_opted_out_and_back_in_by_hand_and_the_ledger_remembers(world):
    from prism_wa import ops_book
    a = world.add("Asha", "919800100001", opted_in=True)
    out = world.do(ops_book.contacts_consent, {"contact_id": a, "action": "opt_out", "evidence": "asked on the phone"})
    assert (out["changed"], out["opted_out"], out["opted_in"]) == (True, True, False)
    again = world.do(ops_book.contacts_consent, {"contact_id": a, "action": "opt_in"})
    assert (again["changed"], again["opted_in"], again["opted_out"]) == (True, True, False)
    ledger = world.db.q("select action, source, evidence from consent_ledger where contact_id = %s order by at", (a,))
    assert [(r["action"], r["source"], r["evidence"]) for r in ledger] == [("opt_out", "manual", "asked on the phone"), ("opt_in", "manual", "set by a teammate")]
    with pytest.raises(ValueError):
        world.do(ops_book.contacts_consent, {"contact_id": a, "action": "maybe"})
