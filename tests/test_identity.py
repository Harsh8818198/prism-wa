"""0002_identity.sql: the resolver, the merge, and who may call them."""
import threading

import psycopg2
import psycopg2.errors as pgerr
import pytest


def tenant(db, slug):
    return db.one("select id from tenants where slug = %s", (slug,))


def resolve(db, tenant_id, **kw):
    args = {"phone": None, "bsuid": None, "parent": None, "username": None,
            "name": None, "source": None}
    args.update(kw)
    rows = db.q("select * from wa_resolve_contact(%s,%s,%s,%s,%s,%s,%s)",
                (tenant_id, args["phone"], args["bsuid"], args["parent"],
                 args["username"], args["name"], args["source"]))
    return rows[0]


def identities(db, contact):
    return {(r["kind"], r["value"]) for r in db.q(
        "select kind, value from contact_identities where contact_id=%s", (contact,))}


# ── create / find / attach ───────────────────────────────────────────────────

def test_a_new_phone_creates_a_contact_with_an_identity(migrated):
    t = tenant(migrated, "orena")
    r = resolve(migrated, t, phone="919726063133", name="Kiran")
    assert r["created"] is True and r["merged_ids"] == [] and r["moved_from"] == []
    row = migrated.q("select * from contacts where id=%s", (r["contact_id"],))[0]
    assert row["tenant_id"] == t and row["phone"] == "919726063133" and row["name"] == "Kiran"
    assert identities(migrated, r["contact_id"]) == {("phone", "919726063133")}


def test_the_same_person_is_found_again_whatever_the_phone_formatting(migrated):
    t = tenant(migrated, "orena")
    first = resolve(migrated, t, phone="919726063133")
    again = resolve(migrated, t, phone="+91 97260 63133")
    assert again["created"] is False and again["contact_id"] == first["contact_id"]
    assert migrated.one("select count(*) from contacts") == 1


def test_a_username_only_user_becomes_a_contact_with_no_phone(migrated):
    t = tenant(migrated, "orena")
    r = resolve(migrated, t, bsuid="IN.abc123", username="kiran.shah")
    row = migrated.q("select * from contacts where id=%s", (r["contact_id"],))[0]
    assert row["phone"] is None and row["bsuid"] == "IN.abc123"
    assert row["wa_username"] == "kiran.shah" and row["name"] == "kiran.shah"
    assert identities(migrated, r["contact_id"]) == {("bsuid", "IN.abc123")}


def test_a_phone_contact_learns_its_bsuid_when_one_arrives_with_the_phone(migrated):
    t = tenant(migrated, "orena")
    a = resolve(migrated, t, phone="919726063133")
    b = resolve(migrated, t, phone="919726063133", bsuid="IN.abc123")
    assert b["contact_id"] == a["contact_id"] and b["created"] is False
    assert identities(migrated, a["contact_id"]) == {("phone", "919726063133"), ("bsuid", "IN.abc123")}
    # and now the bsuid alone reaches the same person
    c = resolve(migrated, t, bsuid="IN.abc123")
    assert c["contact_id"] == a["contact_id"]


def test_a_bsuid_contact_learns_the_phone_when_it_reappears(migrated):
    t = tenant(migrated, "orena")
    a = resolve(migrated, t, bsuid="IN.abc123", username="kiran")
    b = resolve(migrated, t, bsuid="IN.abc123", phone="919726063133")
    assert b["contact_id"] == a["contact_id"]
    assert migrated.one("select phone from contacts where id=%s", (a["contact_id"],)) == "919726063133"


def test_a_curated_name_is_never_overwritten_by_a_profile_name(migrated):
    t = tenant(migrated, "orena")
    a = resolve(migrated, t, phone="919726063133", name="Kiran Shah")
    resolve(migrated, t, phone="919726063133", name="kiran 🚀", username="kiran")
    assert migrated.one("select name from contacts where id=%s", (a["contact_id"],)) == "Kiran Shah"
    assert migrated.one("select wa_username from contacts where id=%s", (a["contact_id"],)) == "kiran"


def test_the_same_phone_in_two_tenants_is_two_people(migrated):
    o, s = tenant(migrated, "orena"), tenant(migrated, "shakti")
    a = resolve(migrated, o, phone="919726063133")
    b = resolve(migrated, s, phone="919726063133")
    assert a["contact_id"] != b["contact_id"]


def test_no_identifier_is_refused(migrated):
    with pytest.raises(pgerr.InvalidParameterValue):
        resolve(migrated, tenant(migrated, "orena"), name="nobody")


def test_a_legacy_contact_written_directly_is_found_not_duplicated(migrated):
    t = tenant(migrated, "orena")
    legacy = migrated.one("insert into contacts (name, phone) values ('n8n','919800000001') returning id")
    assert identities(migrated, legacy) == set()          # the legacy writer made no identity
    r = resolve(migrated, t, phone="919800000001")
    assert r["contact_id"] == legacy and r["created"] is False
    assert identities(migrated, legacy) == {("phone", "919800000001")}


def test_legacy_format_duplicates_are_merged_when_the_person_next_appears(migrated):
    t = tenant(migrated, "orena")
    a = migrated.one("insert into contacts (name, phone) values ('A','+919800000001') returning id")
    b = migrated.one("insert into contacts (name, phone) values ('B','919800000001') returning id")
    r = resolve(migrated, t, phone="919800000001")
    assert len(r["merged_ids"]) == 1 and {r["contact_id"], r["merged_ids"][0]} == {a, b}
    assert migrated.one("select count(*) from contacts") == 1


def test_backfill_lists_duplicates_and_never_merges_them(migrated):
    a = migrated.one("insert into contacts (name, phone) values ('A','+919800000001') returning id")
    b = migrated.one("insert into contacts (name, phone) values ('B','919800000001') returning id")
    row = migrated.q("select * from wa_backfill_identities()")[0]
    assert row["conflicts"] == 1
    conflicts = migrated.q("select contact_id, owner_contact_id from contact_identity_conflicts")
    assert len(conflicts) == 1 and {conflicts[0]["contact_id"], conflicts[0]["owner_contact_id"]} == {a, b}
    assert migrated.one("select count(*) from contacts") == 2
    assert migrated.q("select * from wa_backfill_identities()")[0]["identities_added"] == 0


# ── merging ──────────────────────────────────────────────────────────────────

def _build_split_person(db, t):
    """A (known by phone) and B (known by bsuid) — really one person."""
    a = resolve(db, t, phone="919726063133", name="Kiran Shah")["contact_id"]
    b = resolve(db, t, bsuid="IN.abc123", username="kiran")["contact_id"]
    for who, text in ((a, "from phone"), (b, "from bsuid")):
        conv = db.one("insert into conversations (contact_id, channel, message_count, last_active_at) "
                      "values (%s,'whatsapp',3, now()) returning id", (who,))
        db.q("insert into messages (conversation_id, direction, text) values (%s,'in',%s)", (conv, text))
    db.q("update contacts set tags = '{vip}', opt_out = true where id=%s", (a,))
    db.q("update contacts set tags = '{demo}' where id=%s", (b,))
    db.q("insert into leads (lead_id, contact_id) values ('LD-A', %s)", (a,))
    db.q("insert into activity_log (contact_id, actor, action) values (%s,'bot','x')", (a,))
    return a, b


def test_an_event_that_carries_both_keys_merges_the_two_halves_without_losing_anything(migrated):
    t = tenant(migrated, "orena")
    a, b = _build_split_person(migrated, t)
    r = resolve(migrated, t, phone="919726063133", bsuid="IN.abc123")

    assert r["contact_id"] == b                    # the holder of the strongest key survives
    assert r["merged_ids"] == [a]
    assert migrated.one("select count(*) from contacts") == 1
    row = migrated.q("select * from contacts where id=%s", (b,))[0]
    assert set(row["tags"]) == {"vip", "demo"}
    assert row["opt_out"] is True                  # an opt-out is never lost
    assert row["name"] == "Kiran Shah"             # the real name beats the placeholder username
    assert row["phone"] == "919726063133" and row["bsuid"] == "IN.abc123"
    assert identities(migrated, b) == {("phone", "919726063133"), ("bsuid", "IN.abc123")}
    # one whatsapp conversation, both messages, counts summed
    conv = migrated.q("select * from conversations")
    assert len(conv) == 1 and conv[0]["message_count"] == 6
    assert migrated.one("select count(*) from messages") == 2
    assert migrated.one("select contact_id from leads where lead_id='LD-A'") == b
    assert migrated.one("select count(*) from activity_log where contact_id=%s", (b,)) == 1
    audit = migrated.q("select * from contact_identity_audit where action='merge'")
    assert len(audit) == 1 and audit[0]["other_contact_id"] == a
    assert audit[0]["detail"]["contact"]["name"] == "Kiran Shah"      # enough to undo by hand


def test_merging_keeps_a_human_takeover(migrated):
    t = tenant(migrated, "orena")
    a, b = _build_split_person(migrated, t)
    migrated.q("update conversations set status='human_takeover', bot_paused=true where contact_id=%s", (a,))
    resolve(migrated, t, phone="919726063133", bsuid="IN.abc123")
    row = migrated.q("select status, bot_paused from conversations")[0]
    assert row["status"] == "human_takeover" and row["bot_paused"] is True


def test_merge_refuses_two_tenants(migrated):
    o, s = tenant(migrated, "orena"), tenant(migrated, "shakti")
    a = resolve(migrated, o, phone="919726063133")["contact_id"]
    b = resolve(migrated, s, phone="919726063133")["contact_id"]
    with pytest.raises(pgerr.InsufficientPrivilege):
        migrated.q("select wa_merge_contacts(%s,%s)", (a, b))
    assert migrated.one("select count(*) from contacts") == 2


def test_merge_refuses_itself_and_strangers(migrated):
    t = tenant(migrated, "orena")
    a = resolve(migrated, t, phone="919726063133")["contact_id"]
    with pytest.raises(pgerr.InvalidParameterValue):
        migrated.q("select wa_merge_contacts(%s,%s)", (a, a))
    with pytest.raises(pgerr.NoDataFound):
        migrated.q("select wa_merge_contacts(%s, gen_random_uuid())", (a,))


def test_every_table_that_points_at_contacts_is_handled_by_the_merge(migrated):
    """The guard for the next person to add a contact_id column: it must be
    re-pointed by wa_merge_contacts (or the loser's DELETE will cascade over it)."""
    referencing = {r["t"] for r in migrated.q("""
        select conrelid::regclass::text as t from pg_constraint
         where contype='f' and confrelid = 'public.contacts'::regclass""")}
    handled = {"conversations", "leads", "follow_up_queue", "activity_log", "contact_identities",
               # 0004-0012 hang off a contact too; 0013_merge_all.sql re-points each (tests/test_merge_all.py)
               "consent_ledger", "contact_attributes", "campaign_recipients", "segment_members",
               "short_links", "click_events", "cart_checkouts",
               # 0018 contact timeline; 0021_merge_contact_events.sql re-points it
               "contact_events",
               # 0027 automatic messages and 0029 orders / form answers; 0029 teaches wa_merge_contacts (tests/test_merge_commerce.py)
               "auto_log", "auto_enrolments", "orders", "form_responses"}
    assert referencing == handled, (
        "a table now references contacts(id): teach wa_merge_contacts about %s and "
        "add it to `handled`" % sorted(referencing ^ handled))


# ── recycled phone numbers ───────────────────────────────────────────────────

def test_a_recycled_phone_number_moves_to_the_person_who_now_has_it(migrated):
    t = tenant(migrated, "orena")
    old = resolve(migrated, t, phone="919726063133", bsuid="IN.old111", name="Old Owner")["contact_id"]
    r = resolve(migrated, t, phone="919726063133", bsuid="IN.new222", name="New Owner")

    assert r["created"] is True and r["contact_id"] != old
    assert r["moved_from"] == [old] and r["merged_ids"] == []
    assert migrated.one("select phone from contacts where id=%s", (r["contact_id"],)) == "919726063133"
    assert migrated.one("select phone from contacts where id=%s", (old,)) is None
    assert identities(migrated, old) == {("bsuid", "IN.old111")}           # still reachable by bsuid
    assert identities(migrated, r["contact_id"]) == {("phone", "919726063133"), ("bsuid", "IN.new222")}
    audit = migrated.q("select * from contact_identity_audit where action='move_identity'")
    assert len(audit) == 1 and audit[0]["other_contact_id"] == old and audit[0]["value"] == "919726063133"


def test_two_bsuids_are_never_merged_just_because_they_share_a_phone(migrated):
    t = tenant(migrated, "orena")
    resolve(migrated, t, phone="919726063133", bsuid="IN.old111")
    resolve(migrated, t, phone="919726063133", bsuid="IN.new222")
    assert migrated.one("select count(*) from contacts") == 2


def test_a_shared_parent_bsuid_says_same_person_across_portfolios(migrated):
    t = tenant(migrated, "orena")
    a = resolve(migrated, t, bsuid="IN.port1", parent="IN.parent9")["contact_id"]
    b = resolve(migrated, t, bsuid="IN.port2", parent="IN.parent9")["contact_id"]
    assert a == b
    assert identities(migrated, a) == {("bsuid", "IN.port1"), ("bsuid", "IN.port2"), ("parent_bsuid", "IN.parent9")}


# ── concurrency ──────────────────────────────────────────────────────────────

def _race(db, t, calls):
    errors, out = [], []
    barrier = threading.Barrier(len(calls))

    def run(kw):
        conn = db.connect()
        try:
            barrier.wait()
            with conn.cursor() as cur:
                cur.execute("select contact_id from wa_resolve_contact(%s,%s,%s,%s,null,null,null)",
                            (t, kw.get("phone"), kw.get("bsuid"), kw.get("parent")))
                out.append(cur.fetchone()[0])
            conn.commit()
        except Exception as e:                          # noqa: BLE001
            conn.rollback()
            errors.append(e)

    threads = [threading.Thread(target=run, args=(kw,)) for kw in calls]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    return out, errors


def test_simultaneous_events_for_one_new_number_make_one_contact(migrated):
    t = tenant(migrated, "orena")
    out, errors = _race(migrated, t, [{"phone": "919726063133"}] * 8)
    assert errors == []
    assert len(set(out)) == 1
    assert migrated.one("select count(*) from contacts") == 1


def test_simultaneous_partial_and_full_events_converge_on_one_person(migrated):
    t = tenant(migrated, "orena")
    calls = [{"phone": "919726063133"}, {"bsuid": "IN.abc123"},
             {"phone": "919726063133", "bsuid": "IN.abc123"}] * 3
    out, errors = _race(migrated, t, calls)
    assert errors == []
    assert migrated.one("select count(*) from contacts") == 1
    assert identities(migrated, out[0]) == {("phone", "919726063133"), ("bsuid", "IN.abc123")}


# ── who may call ─────────────────────────────────────────────────────────────

def test_the_public_api_roles_cannot_resolve_or_merge(migrated):
    t = tenant(migrated, "orena")
    for role in ("anon", "authenticated"):
        conn = migrated.connect(autocommit=True, role=role)
        with pytest.raises(pgerr.InsufficientPrivilege):
            migrated.q("select * from wa_resolve_contact(%s,'919726063133',null,null,null,null,null)", (t,), conn=conn)
        with pytest.raises(pgerr.InsufficientPrivilege):
            migrated.q("select * from contact_identities", conn=conn)


def test_service_role_can_resolve(migrated):
    t = tenant(migrated, "orena")
    conn = migrated.connect(autocommit=True, role="service_role")
    rows = migrated.q("select * from wa_resolve_contact(%s,'919726063133',null,null,null,null,null)", (t,), conn=conn)
    assert rows[0]["created"] is True
