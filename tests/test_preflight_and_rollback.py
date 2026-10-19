"""preflight.sql is read-only and works before the migrations; the rollback
restores the legacy behaviour without deleting data."""
import json
from pathlib import Path

import psycopg2.errors as pgerr
import pytest

DB = Path(__file__).resolve().parent.parent / "db"


def run_preflight(db):
    return json.loads(db.one((DB / "preflight.sql").read_text(encoding="utf-8")))


def test_preflight_works_on_a_legacy_database_and_changes_nothing(legacy):
    legacy.q("insert into contacts (name, phone) values ('A','+919800000001'),('B','919800000001')")
    before = legacy.one("select count(*) from pg_class")
    report = run_preflight(legacy)
    assert legacy.one("select count(*) from pg_class") == before
    assert report["contacts_total"] == 2
    assert report["phones_that_normalise_to_the_same_number"] == 1
    assert report["rows_without_tenant"]["contacts"] == 2
    assert report["tables_present"]["segments"] is False
    assert "contacts_phone_idx" in report["legacy_global_unique_indexes"]
    assert [t["slug"] for t in report["tenants"]] == ["shakti"]
    assert "wa_token" not in report["tenants"][0]              # never echo secrets


def test_preflight_works_on_the_pre_spine_schema(make_db):
    report = run_preflight(make_db("pre"))
    assert report["contacts_has_tenant_id"] is False
    # null = "not applicable: no tenant_id column yet"
    assert set(report["rows_without_tenant"].values()) == {None}


def test_preflight_after_migration_shows_nothing_left_unattributed(migrated):
    report = run_preflight(migrated)
    assert set(report["rows_without_tenant"].values()) <= {0}


def test_rollback_restores_the_legacy_writer_behaviour_and_keeps_the_data(migrated):
    migrated.q("insert into contacts (name, phone) values ('n8n','919800000001')")
    orena = migrated.one("select id from tenants where slug='orena'")
    migrated.q("select * from wa_resolve_contact(%s,'919800000009',null,null,null,null,null)", (orena,))
    migrated.q((DB / "rollback" / "0001_0003_down.sql").read_text(encoding="utf-8"))
    # a legacy writer with no tenant now lands as NULL again, as before the migration
    migrated.q("insert into contacts (name, phone, tenant_id) values ('late','919800000002', null)")
    assert migrated.one("select count(*) from contacts where tenant_id is null") == 1
    # the global unique index is back
    with pytest.raises(pgerr.UniqueViolation):
        migrated.q("insert into contacts (name, phone) values ('dup','919800000001')")
    # nothing was deleted
    assert migrated.one("select count(*) from contacts") == 3
    assert migrated.one("select count(*) from contact_identities") == 1      # kept, not dropped


def test_rollback_refuses_rather_than_dropping_a_contact_when_tenants_collide(migrated):
    o = migrated.one("select id from tenants where slug='orena'")
    s = migrated.one("select id from tenants where slug='shakti'")
    migrated.q("insert into contacts (name, phone, tenant_id) values ('x','919811111111',%s),('y','919811111111',%s)", (o, s))
    with pytest.raises(pgerr.UniqueViolation):
        migrated.q((DB / "rollback" / "0001_0003_down.sql").read_text(encoding="utf-8"))
    assert migrated.one("select count(*) from contacts") == 2
