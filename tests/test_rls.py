"""0003_rls.sql: two tenants cannot see each other under the app role."""
import psycopg2.errors as pgerr
import pytest

TABLES = ["contacts", "conversations", "messages", "segments", "campaigns",
          "contact_identities", "contact_identity_audit"]


def tenant(db, slug):
    return db.one("select id from tenants where slug = %s", (slug,))


@pytest.fixture
def two_tenants(migrated):
    db = migrated
    o, s = tenant(db, "orena"), tenant(db, "shakti")
    made = {}
    for slug, t, phone in (("orena", o, "919800000001"), ("shakti", s, "919800000002")):
        c = db.one("select contact_id from wa_resolve_contact(%s,%s,null,null,null,%s,null)",
                   (t, phone, slug.title() + " person"))
        conv = db.one("insert into conversations (contact_id, channel) values (%s,'whatsapp') returning id", (c,))
        db.q("insert into messages (conversation_id, direction, text) values (%s,'in','hello')", (conv,))
        db.q("insert into segments (tenant_id, name) values (%s,'VIP')", (t,))
        db.q("insert into campaigns (tenant_id, template_name) values (%s,'hello_world')", (t,))
        db.q("insert into contact_identity_audit (tenant_id, action, contact_id) values (%s,'merge',%s)", (t, c))
        made[slug] = {"tenant": t, "contact": c, "conv": conv}
    return db, made


def app(db, tenant_id):
    return db.connect(autocommit=True, role="wa_app", tenant=str(tenant_id) if tenant_id else "")


def count(db, table, conn):
    return db.one("select count(*) from %s" % table, conn=conn)


def test_each_tenant_sees_only_its_own_rows_in_every_table(two_tenants):
    db, made = two_tenants
    for slug, other in (("orena", "shakti"), ("shakti", "orena")):
        conn = app(db, made[slug]["tenant"])
        for table in TABLES:
            assert count(db, table, conn) >= 1, table
            leaked = db.one("select count(*) from %s where tenant_id = %%s" % table,
                            (made[other]["tenant"],), conn=conn)
            assert leaked == 0, "%s leaks %s rows to %s" % (table, other, slug)
    # the admin (owner) still sees both — proves the fixture really has two tenants
    assert db.one("select count(distinct tenant_id) from contacts") == 2


def test_a_session_with_no_tenant_sees_nothing(two_tenants):
    db, _ = two_tenants
    conn = app(db, None)
    for table in TABLES:
        assert count(db, table, conn) == 0, table


def test_cannot_write_into_another_tenant(two_tenants):
    db, made = two_tenants
    conn = app(db, made["orena"]["tenant"])
    with pytest.raises(pgerr.InsufficientPrivilege):
        db.q("insert into contacts (tenant_id, name, phone) values (%s,'x','919899999999')",
             (made["shakti"]["tenant"],), conn=conn)
    with pytest.raises(pgerr.InsufficientPrivilege):
        db.q("insert into segments (tenant_id, name) values (%s,'Sneaky')",
             (made["shakti"]["tenant"],), conn=conn)


def test_updates_and_deletes_of_another_tenants_rows_touch_nothing(two_tenants):
    db, made = two_tenants
    conn = app(db, made["orena"]["tenant"])
    victim = made["shakti"]["contact"]
    with conn.cursor() as cur:
        cur.execute("update contacts set opt_out = true where id = %s", (victim,))
        assert cur.rowcount == 0
        cur.execute("delete from contacts where id = %s", (victim,))
        assert cur.rowcount == 0
        cur.execute("update messages set text = 'pwned' where conversation_id = %s", (made["shakti"]["conv"],))
        assert cur.rowcount == 0
    assert db.one("select opt_out from contacts where id=%s", (victim,)) is False
    assert db.one("select text from messages where conversation_id=%s", (made["shakti"]["conv"],)) == "hello"


def test_the_resolver_works_inside_a_tenant_and_refuses_outside_it(two_tenants):
    db, made = two_tenants
    o, s = made["orena"]["tenant"], made["shakti"]["tenant"]
    conn = app(db, o)
    rows = db.q("select * from wa_resolve_contact(%s,'919877777777',null,null,null,null,null)", (o,), conn=conn)
    assert rows[0]["created"] is True
    with pytest.raises(pgerr.InsufficientPrivilege):
        db.q("select * from wa_resolve_contact(%s,'919866666666',null,null,null,null,null)", (s,), conn=conn)
    # and it cannot even *find* the other tenant's person: the same number in
    # its own tenant is a new contact, not Shakti's
    other = db.q("select * from wa_resolve_contact(%s,'919800000002',null,null,null,null,null)", (o,), conn=conn)[0]
    assert other["created"] is True and other["contact_id"] != made["shakti"]["contact"]


def test_merge_is_confined_to_the_sessions_tenant(two_tenants):
    db, made = two_tenants
    s = made["shakti"]["tenant"]
    a = made["shakti"]["contact"]
    b = db.one("select contact_id from wa_resolve_contact(%s,'919811111111',null,null,null,null,null)", (s,))
    conn = app(db, made["orena"]["tenant"])
    with pytest.raises(pgerr.InsufficientPrivilege):
        db.q("select wa_merge_contacts(%s,%s)", (a, b), conn=conn)
    assert db.one("select count(*) from contacts where tenant_id=%s", (s,)) == 2


def test_the_app_role_cannot_touch_the_legacy_sales_tables(two_tenants):
    db, _ = two_tenants
    conn = app(db, tenant(db, "orena"))
    for table in ("leads", "tasks", "follow_up_queue", "activity_log", "tenants"):
        with pytest.raises(pgerr.InsufficientPrivilege):
            db.q("select * from %s" % table, conn=conn)


def test_public_roles_cannot_read_the_new_tables(two_tenants):
    db, _ = two_tenants
    for role in ("anon", "authenticated"):
        conn = db.connect(autocommit=True, role=role)
        for table in ("segments", "campaigns", "contact_identities", "wa_platform_config"):
            with pytest.raises(pgerr.InsufficientPrivilege):
                db.q("select * from %s" % table, conn=conn)


def test_service_role_still_sees_everything(two_tenants):
    db, _ = two_tenants
    conn = db.connect(autocommit=True, role="service_role")
    assert db.one("select count(distinct tenant_id) from contacts", conn=conn) == 2
    assert db.one("select count(distinct tenant_id) from segments", conn=conn) == 2


def test_rerunning_rls_is_harmless(two_tenants):
    db, made = two_tenants
    db.apply()
    conn = app(db, made["orena"]["tenant"])
    assert count(db, "contacts", conn) == 1
