"""0001_tenancy.sql against the real legacy schema."""
import psycopg2
import psycopg2.errors as pgerr
import pytest


def tenant(db, slug):
    return db.one("select id from tenants where slug = %s", (slug,))


def seed_legacy(db):
    """Two Orena-style legacy contacts (no tenant), one already-Shakti contact,
    each with a conversation and messages."""
    shakti = tenant(db, "shakti")
    ids = {}
    ids["a"] = db.one("insert into contacts (name, phone) values ('Asha','919800000001') returning id")
    ids["b"] = db.one("insert into contacts (name, email) values ('Bala','b@x.in') returning id")
    ids["s"] = db.one("insert into contacts (name, phone, tenant_id) values ('Sam','919800000009', %s) returning id", (shakti,))
    for key in ("a", "s"):
        conv = db.one("insert into conversations (contact_id, channel) values (%s,'whatsapp') returning id", (ids[key],))
        db.q("insert into messages (conversation_id, direction, text) values (%s,'in','hi'),(%s,'out','yo')", (conv, conv))
        ids["conv_" + key] = conv
    db.q("insert into leads (lead_id, contact_id) values ('LD-1', %s)", (ids["a"],))
    return ids


def test_backfill_attributes_legacy_rows_and_keeps_existing_tenants(legacy):
    ids = seed_legacy(legacy)
    before = legacy.one("select updated_at from contacts where id = %s", (ids["a"],))
    legacy.apply()
    orena, shakti = tenant(legacy, "orena"), tenant(legacy, "shakti")
    assert orena is not None

    got = {r["name"]: r["tenant_id"] for r in legacy.q("select name, tenant_id from contacts")}
    assert got == {"Asha": orena, "Bala": orena, "Sam": shakti}

    assert legacy.one("select tenant_id from conversations where id=%s", (ids["conv_a"],)) == orena
    assert legacy.one("select tenant_id from conversations where id=%s", (ids["conv_s"],)) == shakti
    assert legacy.one("select count(*) from messages where tenant_id is null") == 0
    assert legacy.one("select count(*) from messages m join conversations c on c.id=m.conversation_id "
                      "where m.tenant_id <> c.tenant_id") == 0
    assert legacy.one("select tenant_id from leads where lead_id='LD-1'") == orena
    # attribution must not look like an edit
    assert legacy.one("select updated_at from contacts where id = %s", (ids["a"],)) == before


def test_running_twice_changes_nothing(legacy):
    seed_legacy(legacy)
    legacy.apply()
    snap = legacy.q("select id, tenant_id, phone, updated_at from contacts order by id")
    tenants_n = legacy.one("select count(*) from tenants")
    legacy.apply()
    assert legacy.q("select id, tenant_id, phone, updated_at from contacts order by id") == snap
    assert legacy.one("select count(*) from tenants") == tenants_n


def test_works_from_the_pre_spine_schema(make_db):
    db = make_db("pre")
    db.q("insert into contacts (name, phone) values ('Old','919800000001')")
    db.apply()
    assert db.one("select count(*) from contacts where tenant_id is not null") == 1


def test_handmade_segments_and_campaigns_are_adopted(legacy):
    legacy.q("create table segments (id uuid primary key default gen_random_uuid(), name text, filter jsonb)")
    legacy.q("create table campaigns (id uuid primary key default gen_random_uuid(), template_name text)")
    legacy.q("insert into segments (name, filter) values ('VIP', '{}')")
    legacy.q("insert into campaigns (template_name) values ('hello_world')")
    legacy.apply()
    orena = tenant(legacy, "orena")
    assert legacy.one("select tenant_id from segments") == orena
    assert legacy.one("select tenant_id from campaigns") == orena
    assert legacy.one("select language from campaigns") == "en_US"


def test_same_phone_is_allowed_in_two_tenants_but_not_twice_in_one(migrated):
    orena, shakti = tenant(migrated, "orena"), tenant(migrated, "shakti")
    migrated.q("insert into contacts (name, phone, tenant_id) values ('x','919811111111',%s)", (orena,))
    migrated.q("insert into contacts (name, phone, tenant_id) values ('y','919811111111',%s)", (shakti,))
    with pytest.raises(pgerr.UniqueViolation):
        migrated.q("insert into contacts (name, phone, tenant_id) values ('z','919811111111',%s)", (orena,))
    with pytest.raises(pgerr.UniqueViolation):
        migrated.q("insert into contacts (name, email, tenant_id) values ('e1','A@x.in',%s),('e2','a@X.in',%s)", (orena, orena))


def test_a_legacy_writer_that_sends_no_tenant_lands_in_the_default_tenant(migrated):
    orena = tenant(migrated, "orena")
    c = migrated.one("insert into contacts (name, phone) values ('n8n','919822222222') returning id")
    conv = migrated.one("insert into conversations (contact_id, channel) values (%s,'whatsapp') returning id", (c,))
    m = migrated.one("insert into messages (conversation_id, direction, text) values (%s,'in','hi') returning id", (conv,))
    assert migrated.one("select tenant_id from contacts where id=%s", (c,)) == orena
    assert migrated.one("select tenant_id from conversations where id=%s", (conv,)) == orena
    assert migrated.one("select tenant_id from messages where id=%s", (m,)) == orena


def test_strict_mode_refuses_a_row_without_a_tenant(migrated):
    migrated.q("delete from wa_platform_config where key='legacy_default_tenant'")
    with pytest.raises(pgerr.NotNullViolation):
        migrated.q("insert into contacts (name, phone) values ('n8n','919833333333')")


def test_a_child_cannot_be_filed_under_another_tenant(migrated):
    orena, shakti = tenant(migrated, "orena"), tenant(migrated, "shakti")
    c = migrated.one("insert into contacts (name, phone, tenant_id) values ('o','919844444444',%s) returning id", (orena,))
    with pytest.raises(pgerr.CheckViolation):
        migrated.q("insert into conversations (contact_id, channel, tenant_id) values (%s,'whatsapp',%s)", (c, shakti))
    conv = migrated.one("insert into conversations (contact_id, channel) values (%s,'whatsapp') returning id", (c,))
    with pytest.raises(pgerr.CheckViolation):
        migrated.q("insert into messages (conversation_id, direction, tenant_id) values (%s,'in',%s)", (conv, shakti))


def test_a_row_cannot_quietly_change_tenant(migrated):
    orena, shakti = tenant(migrated, "orena"), tenant(migrated, "shakti")
    c = migrated.one("insert into contacts (name, phone, tenant_id) values ('o','919855555555',%s) returning id", (orena,))
    with pytest.raises(pgerr.CheckViolation):
        migrated.q("update contacts set tenant_id=%s where id=%s", (shakti, c))
    migrated.q("set wa.allow_tenant_change = on")
    migrated.q("update contacts set tenant_id=%s where id=%s", (shakti, c))
    assert migrated.one("select tenant_id from contacts where id=%s", (c,)) == shakti


def test_a_contact_known_only_by_bsuid_can_exist(migrated):
    migrated.q("insert into contacts (name, bsuid, wa_username) values ('@kiran','IN.abc123','kiran')")
    assert migrated.one("select count(*) from contacts where bsuid='IN.abc123'") == 1


def test_wrong_tags_type_aborts_and_changes_nothing(legacy):
    legacy.q("alter table contacts add column tags jsonb")
    with pytest.raises(psycopg2.Error):
        legacy.apply()
    cols = {r["column_name"] for r in legacy.q(
        "select column_name from information_schema.columns where table_name='conversations'")}
    assert "tenant_id" not in cols
    assert legacy.one("select to_regclass('public.wa_platform_config')") is None


def test_missing_legacy_table_aborts_with_a_clear_message(legacy):
    legacy.q("drop table follow_up_queue")
    with pytest.raises(psycopg2.Error) as e:
        legacy.apply()
    assert "follow_up_queue" in str(e.value)
