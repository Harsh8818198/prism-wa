"""The harness itself: a fresh database holds the legacy schema and the two
template flavours differ exactly where they should."""


def test_legacy_schema_is_loaded(legacy):
    tables = {r["table_name"] for r in legacy.q(
        "select table_name from information_schema.tables where table_schema='public'")}
    assert {"contacts", "conversations", "messages", "leads", "tenants"} <= tables


def test_pre_spine_has_no_tenant_column_on_contacts(make_db):
    pre = make_db("pre")
    cols = {r["column_name"] for r in pre.q(
        "select column_name from information_schema.columns where table_name='contacts'")}
    assert "tenant_id" not in cols


def test_live_like_has_tenant_column_on_contacts(legacy):
    cols = {r["column_name"] for r in legacy.q(
        "select column_name from information_schema.columns where table_name='contacts'")}
    assert "tenant_id" in cols


def test_databases_are_isolated(make_db):
    a, b = make_db(), make_db()
    a.q("insert into contacts (phone) values ('1')")
    assert b.one("select count(*) from contacts") == 0
