"""Contacts as a CRM: phone numbers, attributes, CSV in and out, consent, erasure."""
import csv
import io
import time

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import consent, contacts
from prism_wa.contacts import ImportProblem, coerce, normalise_phone


def run_import(db, tenant, text, **kw):
    conn = psycopg2.connect(db.dsn)
    try:
        return contacts.import_csv(conn, tenant, text, **kw)
    finally:
        conn.close()


def rows(db, sql="select * from contacts order by name"):
    return db.q(sql)


# ── phone numbers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "919876543210"), ("09876543210", "919876543210"), ("98765 43210", "919876543210"),
    ("+91 98765-43210", "919876543210"), ("919876543210", "919876543210"), ("0091 9876543210", "919876543210"),
    ("+447911123456", "447911123456"), ("+1 (415) 555-2671", "14155552671"), (9876543210, "919876543210")])
def test_phone_numbers_become_digits_with_a_country_code(raw, expected):
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "12345", "5876543210", "+915876543210", "1" * 16, "98765"])
def test_things_that_are_not_phone_numbers_are_refused(raw):
    assert normalise_phone(raw) is None


def test_a_different_default_country_is_honoured():
    assert normalise_phone("4155552671", "1") == "14155552671"


def test_attribute_values_are_coerced_to_their_type():
    assert coerce("number", "1,250") == 1250 and coerce("number", "3.5") == 3.5
    assert coerce("bool", "Yes") is True and coerce("bool", "0") is False
    assert coerce("date", "17/10/2026") == "2026-10-17" and coerce("date", "2026-10-17") == "2026-10-17"
    assert coerce("enum", "gold", ["Gold", "Silver"]) == "Gold" and coerce("string", "  hi ") == "hi"
    for kind, value in (("number", "x"), ("bool", "maybe"), ("date", "yesterday"), ("enum", "bronze")):
        with pytest.raises(ValueError):
            coerce(kind, value, ["Gold"])


# ── import ───────────────────────────────────────────────────────────────────

CSV = ("Name,Mobile,Email,Tags,City,Budget\n"
       "Asha Shah,98765 43210,asha@x.in,vip|builder,Vadodara,\"12,50,000\"\n"
       "Bala Patel,+91 98765 43211,,cold,Surat,\n"
       "Chirag,9876543212,c@x.in,,Vadodara,800000\n")


def test_a_csv_creates_contacts_with_tags_email_and_attributes(migrated):
    tid = H.tenant(migrated, "orena")
    r = run_import(migrated, tid, CSV, source="IMPORT")
    assert (r.rows, r.created, r.updated, r.invalid) == (3, 3, 0, 0)
    asha = H.only(migrated, "select * from contacts where name = 'Asha Shah'")
    assert asha["phone"] == "919876543210" and asha["email"] == "asha@x.in" and sorted(asha["tags"]) == ["builder", "vip"]
    assert asha["source_first_seen"] == "IMPORT" and asha["tenant_id"] == tid
    attrs = {(r_["contact_id"], r_["key"]): r_["value"] for r_ in migrated.q("select * from contact_attributes")}
    assert attrs[(asha["id"], "City")] == "Vadodara" and attrs[(asha["id"], "Budget")] == "12,50,000"
    assert migrated.one("select count(*) from attribute_defs") == 2


def test_a_second_import_updates_rather_than_duplicates_and_adds_tags(migrated):
    tid = H.tenant(migrated, "orena")
    run_import(migrated, tid, CSV)
    r = run_import(migrated, tid, "Name,Mobile,Tags\nAsha Shah,9876543210,repeat-buyer\n")
    assert (r.created, r.updated) == (0, 1)
    assert migrated.one("select count(*) from contacts") == 3
    assert sorted(migrated.one("select tags from contacts where phone = '919876543210'")) == ["builder", "repeat-buyer", "vip"]


def test_the_same_number_twice_in_one_file_is_one_contact(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"), "Name,Mobile\nA,9876543210\nA again,+919876543210\n")
    assert (r.created, r.updated) == (1, 1) and migrated.one("select count(*) from contacts") == 1


def test_bad_rows_are_reported_with_their_line_and_the_good_ones_are_saved(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"),
                   "Name,Mobile,Budget\nGood,9876543210,1\nBad phone,12345,2\n,,\nWorse,5876543210,3\n")
    assert (r.rows, r.created, r.invalid) == (3, 1, 2)
    assert [e["row"] for e in r.errors] == [3, 5] and "not a valid phone" in r.errors[0]["error"]
    assert migrated.one("select count(*) from contacts") == 1


def test_a_bad_attribute_value_is_reported_but_the_contact_is_kept(migrated):
    tid = H.tenant(migrated, "orena")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        contacts.define_attribute(cur, tid, "Visits", "number")
    conn.commit()
    conn.close()
    r = run_import(migrated, tid, "Name,Mobile,Visits\nAsha,9876543210,lots\n")
    assert r.created == 1 and any("Visits" in e["error"] for e in r.errors)
    assert migrated.one("select count(*) from contact_attributes") == 0


def test_a_file_without_a_mobile_column_or_with_nothing_in_it_is_refused_whole(migrated):
    tid = H.tenant(migrated, "orena")
    for text in ("Name,City\nAsha,Vadodara\n", ""):
        with pytest.raises(ImportProblem):
            run_import(migrated, tid, text)
    assert migrated.one("select count(*) from contacts") == 0


def test_a_byte_order_mark_semicolon_tags_and_odd_headers_are_handled(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"),
                   "﻿Full Name,WhatsApp Number,Tag,Cust ID,!!!\nAsha,9876543210,a;b,77,x\n")
    assert r.created == 1 and sorted(migrated.one("select tags from contacts")) == ["a", "b"]
    assert migrated.one("select count(*) from attribute_defs where key = 'Cust_ID'") == 1
    assert any("!!!" in w for w in r.warnings)


def test_no_name_column_names_contacts_by_number_and_says_so(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"), "Mobile\n9876543210\n")
    assert r.created == 1 and r.warnings and migrated.one("select count(*) from contacts") == 1


def test_the_same_number_in_two_workspaces_is_two_contacts(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    run_import(migrated, a, "Name,Mobile\nAsha,9876543210\n")
    run_import(migrated, b, "Name,Mobile\nAsha,9876543210\n")
    assert migrated.one("select count(distinct tenant_id) from contacts") == 2


def test_an_email_that_belongs_to_another_contact_is_reported_not_fatal(migrated):
    tid = H.tenant(migrated, "orena")
    run_import(migrated, tid, "Name,Mobile,Email\nAsha,9876543210,same@x.in\n")
    r = run_import(migrated, tid, "Name,Mobile,Email\nBala,9876543211,same@x.in\n")
    assert r.created == 1 and any("already used" in e["error"] for e in r.errors)


def test_the_row_limit_stops_the_import_and_says_so(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"), "Name,Mobile\n" + "".join(
        "N%d,98765%05d\n" % (i, i) for i in range(10)), max_rows=4)
    assert r.created == 4 and "stopped" in r.errors[-1]["error"]


def test_import_speed_for_a_few_thousand_rows(migrated):
    n = 2000
    text = "Name,Mobile,Tags\n" + "".join("N%d,%d,t%d\n" % (i, 9000000000 + i, i % 5) for i in range(n))
    start = time.time()
    r = run_import(migrated, H.tenant(migrated, "orena"), text)
    took = time.time() - start
    print("\nimport: %d rows in %.1fs (%.0f rows/s)" % (n, took, n / took))
    assert r.created == n and took < 120


# ── consent ──────────────────────────────────────────────────────────────────

def test_importing_with_consent_attested_records_it_with_the_file_as_evidence(migrated):
    tid = H.tenant(migrated, "orena")
    r = run_import(migrated, tid, "Name,Mobile\nAsha,9876543210\n", attest_consent=True, evidence="leads_oct.csv")
    assert r.consent_recorded == 1
    c = H.only(migrated, "select opted_in, opt_out from contacts")
    assert c == {"opted_in": True, "opt_out": False}
    led = H.only(migrated, "select action, source, evidence from consent_ledger")
    assert led == {"action": "opt_in", "source": "csv_import", "evidence": "leads_oct.csv"}


def test_an_import_never_undoes_someones_opt_out(migrated):
    tid = H.tenant(migrated, "orena")
    run_import(migrated, tid, "Name,Mobile\nAsha,9876543210\n")
    cid = migrated.one("select id from contacts")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        consent.record(cur, tid, cid, "opt_out", "keyword", "STOP")
    conn.commit()
    conn.close()
    r = run_import(migrated, tid, "Name,Mobile\nAsha,9876543210\n", attest_consent=True)
    assert r.kept_opted_out == 1 and r.consent_recorded == 0
    assert migrated.one("select opt_out from contacts") is True
    assert migrated.one("select count(*) from consent_ledger where action = 'opt_in'") == 0


def test_a_row_level_opt_in_column_is_honoured_without_the_checkbox(migrated):
    r = run_import(migrated, H.tenant(migrated, "orena"), "Name,Mobile,Opt in\nA,9876543210,yes\nB,9876543211,no\n")
    assert r.consent_recorded == 1 and migrated.one("select count(*) from contacts where opted_in") == 1


def keyword_world(db):
    tid, number = H.onboard(db)
    return tid


def say(db, text, wamid, ts="1760000001"):
    H.deliver(db, P.inbound(P.text(text, wamid, ts)))
    H.work(db)


def test_stop_opts_the_contact_out_records_the_words_and_confirms(migrated):
    keyword_world(migrated)
    say(migrated, "STOP", "wamid.1")
    c = H.only(migrated, "select opt_out, opted_in from contacts")
    assert c == {"opt_out": True, "opted_in": False}
    led = H.only(migrated, "select action, source, evidence from consent_ledger")
    assert led == {"action": "opt_out", "source": "keyword", "evidence": "STOP"}
    out = H.only(migrated, "select kind, payload, state from outbox")
    assert out["kind"] == "text" and "unsubscribed" in out["payload"]["text"]["body"]


def test_start_opts_back_in(migrated):
    keyword_world(migrated)
    say(migrated, "stop", "wamid.1")
    say(migrated, " Start! ", "wamid.2", "1760000009")
    assert H.only(migrated, "select opt_out, opted_in from contacts") == {"opt_out": False, "opted_in": True}
    assert migrated.one("select count(*) from consent_ledger") == 2


def test_a_sentence_containing_stop_is_just_a_customer_talking(migrated):
    keyword_world(migrated)
    say(migrated, "please stop by the site tomorrow", "wamid.1")
    assert migrated.one("select opt_out from contacts") is False
    assert migrated.one("select count(*) from consent_ledger") == 0 and migrated.one("select count(*) from outbox") == 0


def test_a_templates_opt_out_button_counts(migrated):
    keyword_world(migrated)
    H.deliver(migrated, P.inbound(P.template_button("optout", "Stop messages")))
    H.work(migrated)
    assert migrated.one("select opt_out from contacts") is True
    assert migrated.one("select source from consent_ledger") == "button"


def test_keywords_and_replies_are_configurable_and_can_be_silenced(migrated):
    tid = keyword_world(migrated)
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        consent.save_settings(cur, tid, {"opt_out_keywords": ["band karo", "stop"], "confirm": False})
    conn.commit()
    conn.close()
    say(migrated, "Band karo", "wamid.1")
    assert migrated.one("select opt_out from contacts") is True
    assert migrated.one("select count(*) from outbox") == 0                    # confirmation switched off


@pytest.mark.parametrize("patch", [
    {"opt_out_keywords": []}, {"opt_out_keywords": ["a", "b", "c", "d", "e", "f"]},
    {"opt_out_keywords": ["stop", "stop"]}, {"opt_in_keywords": ["stop"]}, {"opt_out_keywords": ["###"]},
    {"opt_out_reply": " "}, {"opt_in_reply": "x" * 1001}])
def test_bad_keyword_settings_are_refused_with_a_reason(migrated, patch):
    tid = H.tenant(migrated, "orena")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        with pytest.raises(consent.SettingsError):
            consent.save_settings(cur, tid, patch)
    conn.close()


def test_the_first_message_and_the_last_time_seen_are_recorded(migrated):
    keyword_world(migrated)
    say(migrated, "I want a quote for a boundary wall", "wamid.1", "1760000001")
    say(migrated, "hello again", "wamid.2", "1760000500")
    c = H.only(migrated, "select first_msg_tag, last_seen from contacts")
    assert c["first_msg_tag"] == "I want a quote for a boundary wall" and c["last_seen"].isoformat().startswith("2025-10-09")
    ad = P.ad_click("wamid.AD", "1760000600")
    ad["from"] = "919811111111"
    H.deliver(migrated, P.inbound(ad, contact={"profile": {"name": "Ravi"}, "wa_id": "919811111111"}))
    H.work(migrated)
    assert migrated.one("select first_msg_tag from contacts where phone = '919811111111'") == "ad:ad-77"


# ── export ───────────────────────────────────────────────────────────────────

def test_export_round_trips_attributes_and_can_mask_numbers(migrated):
    tid = H.tenant(migrated, "orena")
    run_import(migrated, tid, CSV)
    conn = psycopg2.connect(migrated.dsn)
    try:
        out = list(csv.reader(io.StringIO(contacts.export_csv(conn, tid))))
        assert out[0][:7] == ["Name", "Mobile", "Email", "Tags", "Opted in", "Opted out", "Source"] and out[0][7:] == ["Budget", "City"]
        asha = next(r for r in out if r[0] == "Asha Shah")
        assert asha[1] == "919876543210" and asha[3] == "builder|vip" and asha[7:] == ["12,50,000", "Vadodara"]
        masked = list(csv.reader(io.StringIO(contacts.export_csv(conn, tid, mask=True))))
        assert next(r for r in masked if r[0] == "Asha Shah")[1:3] == ["********3210", ""]
        only_vip = list(csv.reader(io.StringIO(contacts.export_csv(conn, tid, tags=["vip"]))))
        assert [r[0] for r in only_vip[1:]] == ["Asha Shah"]
    finally:
        conn.close()


def test_an_exported_cell_can_never_be_a_spreadsheet_formula(migrated):
    tid = H.tenant(migrated, "orena")
    run_import(migrated, tid, "Name,Mobile\n=HYPERLINK(\"http://evil\"),9876543210\n+cmd|x,9876543211\n@SUM(1),9876543212\n")
    conn = psycopg2.connect(migrated.dsn)
    try:
        names = [r[0] for r in csv.reader(io.StringIO(contacts.export_csv(conn, tid)))][1:]
    finally:
        conn.close()
    assert names and all(n.startswith("'") for n in names)


def test_export_only_includes_this_workspace(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    run_import(migrated, a, "Name,Mobile\nMine,9876543210\n")
    run_import(migrated, b, "Name,Mobile\nTheirs,9876543211\n")
    conn = psycopg2.connect(migrated.dsn)
    try:
        text = contacts.export_csv(conn, a)
    finally:
        conn.close()
    assert "Mine" in text and "Theirs" not in text


# ── erasure ──────────────────────────────────────────────────────────────────

def dump_everything(db) -> str:
    """Every public table as text, so a phone number can be searched for anywhere."""
    chunks = []
    for (table,) in [(r["tablename"],) for r in db.q("select tablename from pg_tables where schemaname = 'public'")]:
        for r in db.q('select row_to_json(t)::text as j from "%s" t' % table):
            chunks.append("%s: %s" % (table, r["j"]))
    return "\n".join(chunks)


def test_erasing_a_contact_leaves_no_trace_of_them_anywhere_and_touches_nobody_else(migrated):
    tid, number = H.onboard(migrated)
    H.deliver(migrated, P.inbound(P.text("hello", "wamid.A1", str(int(time.time())), frm="919876500001"),
                                  contact={"profile": {"name": "Asha"}, "wa_id": "919876500001"}))
    H.deliver(migrated, P.inbound(P.text("hi", "wamid.B1", frm="919876500002"),
                                  contact={"profile": {"name": "Bala"}, "wa_id": "919876500002"}))
    H.deliver(migrated, P.statuses(P.status("wamid.OUT1", "sent", "5")))
    H.work(migrated)
    # an outbound reply to Asha, in the outbox and in the inbox
    from prism_wa import send
    asha = migrated.one("select id from contacts where phone = '919876500001'")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        send.enqueue_text(cur, tid, contact_id=asha, text="your quote", idem_key="q1")
        contacts.define_attribute(cur, tid, "City")
        contacts.set_attribute(cur, tid, asha, "City", "Vadodara")
        consent.record(cur, tid, asha, "opt_in", "manual", "asked at the counter")
    conn.commit()
    assert "919876500001" in dump_everything(migrated)

    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        assert contacts.erase_contact(cur, tid, asha) is True
    conn.commit()
    conn.close()
    assert "919876500001" not in dump_everything(migrated)
    assert "Asha" not in dump_everything(migrated)
    bala = migrated.q("select name, phone from contacts")
    assert bala == [{"name": "Bala", "phone": "919876500002"}]
    assert migrated.one("select count(*) from messages") >= 1


def test_erasure_refuses_a_contact_from_another_workspace(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    run_import(migrated, b, "Name,Mobile\nTheirs,9876543211\n")
    cid = migrated.one("select id from contacts")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        assert contacts.erase_contact(cur, a, cid) is False
    conn.close()
    assert migrated.one("select count(*) from contacts") == 1
