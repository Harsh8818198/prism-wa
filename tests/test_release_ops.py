"""What a live deployment needs that a feature test never touches: the readiness check, the worker's heartbeat, migrations that apply only what is
missing (and refuse to be the first), secrets that must be strong, and the post-deploy smoke test itself."""
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
import uvicorn
from fastapi.testclient import TestClient

import apply as migrations
import helpers as H
import payloads as P
from prism_wa import embedded, health, main as gateway, migrate, send
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.vault import Vault, generate_key

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import smoke                                                                    # noqa: E402

SECRET, TOKEN, INTERNAL = "a" * 32, "v" * 48, "i" * 64


def connect_to(db):
    return lambda: psycopg2.connect(db.dsn)


# ── readiness ────────────────────────────────────────────────────────────────

def test_a_current_database_is_ready_and_a_worker_nobody_has_seen_is_only_reported(migrated):
    status, body = health.readiness(connect_to(migrated))
    assert status == 200 and body == {"ok": True, "database": True, "schema": "current", "worker": "never"}


def test_the_worker_heartbeat_makes_it_running_and_old_news_makes_it_stale(migrated):
    health.beat(connect_to(migrated))
    status, body = health.readiness(connect_to(migrated))
    assert status == 200 and body["worker"] == "running" and body["worker_seen_seconds_ago"] < 30
    later = datetime.now(timezone.utc) + timedelta(seconds=health.STALE_AFTER_SECONDS + 60)
    status, body = health.readiness(connect_to(migrated), now=later)
    assert status == 200 and body["worker"] == "stale"                          # reported, but the gateway itself is fine


def test_a_second_beat_replaces_the_first_it_does_not_pile_up(migrated):
    for _ in range(3):
        health.beat(connect_to(migrated))
    assert migrated.one("select count(*) from wa_platform_config where key = %s", (health.HEARTBEAT_KEY,)) == 1


def test_a_beat_that_cannot_be_written_never_stops_the_worker():
    def broken():
        raise psycopg2.OperationalError("down")
    health.beat(broken)                                                         # does not raise


def test_a_database_that_is_behind_this_build_is_not_ready(migrated):
    migrated.q("delete from wa_schema_migrations where name = (select max(name) from wa_schema_migrations)")
    status, body = health.readiness(connect_to(migrated))
    assert status == 503 and body["ok"] is False and body["schema"] == "behind" and body["migrations_missing"] == 1


def test_a_database_whose_migrations_were_applied_by_hand_is_unknown_not_broken(legacy):
    status, body = health.readiness(connect_to(legacy))
    assert status == 200 and body["schema"] == "unknown"


def test_a_database_that_does_not_answer_is_not_ready():
    def down():
        raise psycopg2.OperationalError("down")
    status, body = health.readiness(down)
    assert status == 503 and body["database"] is False


def test_the_route_answers_with_that_status_and_healthz_never_asks_the_database(migrated):
    def down():
        raise psycopg2.OperationalError("down")
    client = TestClient(create_app(app_secrets=SECRET, verify_token=TOKEN, connect=down))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    client = TestClient(create_app(app_secrets=SECRET, verify_token=TOKEN, connect=connect_to(migrated)))
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["schema"] == "current"


# ── migrations that apply only what is missing ───────────────────────────────

def test_the_first_ever_run_is_refused_and_changes_nothing(legacy):
    conn = psycopg2.connect(legacy.dsn)
    with pytest.raises(migrations.FirstRunRefused):
        migrations.apply_pending(conn)
    conn.close()
    assert legacy.one("select to_regclass('public.wa_schema_migrations')") is None
    assert legacy.one("select to_regclass('public.wa_platform_config')") is None


def test_the_first_run_with_the_flag_applies_everything_and_the_next_has_nothing_to_do(legacy):
    conn = psycopg2.connect(legacy.dsn)
    done = migrations.apply_pending(conn, first_run=True)
    assert [p.stem for p in migrations.migration_files()] == done
    assert migrations.apply_pending(conn) == []                                  # no flag needed once recorded
    assert migrations.pending(conn) == []
    conn.close()
    assert health.readiness(connect_to(legacy))[1]["schema"] == "current"


def test_a_migration_that_was_edited_is_applied_again_and_the_others_are_left_alone(migrated):
    migrated.q("update wa_schema_migrations set sha256 = 'old' where name = '0029_commerce_forms_partner'")
    conn = psycopg2.connect(migrated.dsn)
    assert migrations.apply_pending(conn) == ["0029_commerce_forms_partner"]
    assert migrations.apply_pending(conn) == []
    conn.close()


def test_a_database_set_up_by_hand_needs_the_flag_once_then_records_itself(migrated):
    migrated.q("drop table wa_schema_migrations")
    conn = psycopg2.connect(migrated.dsn)
    with pytest.raises(migrations.FirstRunRefused):
        migrations.apply_pending(conn)
    with pytest.raises(migrations.FirstRunRefused, match="already exist"):                  # the tables are there: from an earlier run of these files, or someone else's?
        migrations.apply_pending(conn, first_run=True)
    assert len(migrations.apply_pending(conn, first_run=True, adopt=True)) == len(migrations.migration_files())     # the owner says they are from an earlier hand-run
    conn.close()


def test_the_runner_says_what_is_waiting_without_changing_anything(migrated, capsys):
    migrated.q("delete from wa_schema_migrations where name = (select max(name) from wa_schema_migrations)")
    assert migrations.main(["apply.py", "--status", migrated.dsn]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("waiting") and all(line.startswith("applied") for line in out[:-1])
    assert migrated.one("select count(*) from wa_schema_migrations") == len(migrations.migration_files()) - 1


def test_the_runner_refuses_a_first_run_with_a_message_and_a_failing_exit_code(legacy, capsys):
    assert migrations.main(["apply.py", "--pending", legacy.dsn]) == 1
    assert "first-run" in capsys.readouterr().out.lower()


# ── secrets that must be strong ──────────────────────────────────────────────

def good_env(**over):
    env = {"WA_ENV": "prod", "WA_APP_SECRET": SECRET, "WA_VERIFY_TOKEN": TOKEN, "DATABASE_URL": "postgresql://127.0.0.1:1/none",
           "WA_INTERNAL_TOKEN": INTERNAL, "WA_TOKEN_KEYS": "1:" + generate_key(), "WA_APP_ID": "1", "WA_ES_CONFIG_ID": "2"}
    env.update(over)
    return env


def test_a_production_service_with_good_secrets_starts():
    assert gateway.create_app_from_env(good_env()) is not None


@pytest.mark.parametrize("change, named", [
    ({"WA_APP_SECRET": "short"}, "WA_APP_SECRET"),
    ({"WA_VERIFY_TOKEN": "short"}, "WA_VERIFY_TOKEN"),
    ({"WA_INTERNAL_TOKEN": "short"}, "WA_INTERNAL_TOKEN"),
    ({"WA_INTERNAL_TOKEN": ""}, "WA_INTERNAL_TOKEN"),
    ({"WA_TOKEN_KEYS": ""}, "WA_TOKEN_KEYS"),
])
def test_a_production_service_refuses_a_weak_or_missing_secret_and_names_it_without_showing_it(change, named):
    env = good_env(**change)
    with pytest.raises(RuntimeError) as err:
        gateway.create_app_from_env(env)
    text = str(err.value)
    assert named in text
    assert all(v not in text for v in (SECRET, TOKEN, INTERNAL) if v not in change.values())      # (the values themselves are never in the message)


def test_a_service_not_marked_production_keeps_accepting_short_secrets():
    assert gateway.create_app_from_env(good_env(WA_ENV="", WA_APP_SECRET="s", WA_VERIFY_TOKEN="v", WA_INTERNAL_TOKEN="i")) is not None


def test_the_ads_sign_in_uses_the_whatsapp_app_secret_unless_given_its_own(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "x")
    monkeypatch.delenv("META_APP_SECRET")
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)
    gateway.create_app_from_env()
    assert os.environ["META_APP_SECRET"] == SECRET
    monkeypatch.setenv("META_APP_SECRET", "its-own")
    gateway.create_app_from_env()
    assert os.environ["META_APP_SECRET"] == "its-own"


# ── the smoke test, against a real server ────────────────────────────────────

@pytest.fixture
def live(migrated):
    app = create_app(app_secrets=SECRET, verify_token=TOKEN, connect=connect_to(migrated), internal_token=INTERNAL)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield "http://127.0.0.1:%d" % port, migrated
    server.should_exit = True
    thread.join(10)


def test_the_smoke_test_passes_a_healthy_deployment_and_says_what_it_checked(live):
    base, db = live
    health.beat(connect_to(db))
    results = smoke.run(base, TOKEN, INTERNAL)
    failed = [(n, d) for n, ok, d in results if not ok]
    assert failed == [] and len(results) >= 11


def test_the_smoke_test_fails_when_the_worker_is_gone_and_when_a_secret_is_wrong(live):
    base, db = live
    by = {n: ok for n, ok, _ in smoke.run(base, TOKEN, INTERNAL)}
    assert by["the worker has been seen"] is False                                # nobody has beaten yet
    results = smoke.run(base, "not-the-token", "not-the-internal-token")
    by = {n: ok for n, ok, _ in results}
    assert by["Meta's handshake works with WA_VERIFY_TOKEN"] is False and by["...and open with WA_INTERNAL_TOKEN (a harmless read)"] is False, results
    assert by["a forged webhook is refused"] is True and by["the licence server's door is shut without the token"] is True, results


def test_a_caller_without_the_token_always_gets_the_refusal_and_never_a_reset(live):
    # Answering and closing while the caller is still sending made it see a dropped connection about one time in thirty, and the licence server then
    # said "could not reach the WhatsApp service" for what is really a wrong shared token.
    base, _ = live
    body = json.dumps({"tenant_id": "00000000-0000-0000-0000-000000000000"}).encode()
    answers = [smoke.call("POST", base + "/internal/sso/required", body=body, headers={"Content-Type": "application/json"}, tries=1)[0] for _ in range(120)]
    assert set(answers) == {401}, [a for a in answers if a != 401][:5]


def test_the_smoke_test_stops_at_once_when_nothing_answers():
    results = smoke.run("http://127.0.0.1:1")
    assert [ok for _, ok, _ in results] == [False]


# ── a host with no background-worker service: the worker inside the gateway, migrations at start ─────────────────────────────────────────────

class FakeMeta:
    def __init__(self):
        self.sent = 0

    def __call__(self, method, url, data, headers):
        if url.endswith("/messages"):
            self.sent += 1
            return 200, json.dumps({"messages": [{"id": "wamid.E%d" % self.sent}]}).encode()
        return 404, b"{}"


def wait_for(check, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def worker_threads():
    return [t for t in threading.enumerate() if t.name == embedded.THREAD_NAME and t.is_alive()]


def test_the_worker_runs_inside_the_gateway_when_asked_does_the_work_and_stops_with_it(migrated, monkeypatch):
    key = generate_key()
    vault = Vault.from_env({"WA_TOKEN_KEYS": "1:" + key})
    meta = FakeMeta()
    monkeypatch.setattr(gateway, "Graph", lambda: Graph(meta))
    tenant, _ = H.onboard(migrated)
    blob, ver = vault.encrypt("EAAB-customer-token", context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    H.deliver(migrated, P.inbound(P.text("hello", "wamid.IN1", str(int(time.time())))))
    app = gateway.create_app_from_env(good_env(DATABASE_URL=migrated.dsn, WA_TOKEN_KEYS="1:" + key, WA_EMBEDDED_WORKER="yes"))
    with TestClient(app) as client:
        assert wait_for(lambda: migrated.one("select count(*) from messages where direction = 'in'") == 1)      # it ingested the webhook
        contact = migrated.one("select id from contacts")
        conn = psycopg2.connect(migrated.dsn)
        with conn.cursor() as cur:
            send.enqueue_text(cur, tenant, contact_id=contact, text="hi back", idem_key="embedded-1")
        conn.commit()
        conn.close()
        assert wait_for(lambda: migrated.one("select status from messages where direction = 'out'") == "sent")   # and delivered the reply
        assert meta.sent == 1
        assert wait_for(lambda: client.get("/readyz").json().get("worker") == "running")                          # and says so
        assert len(worker_threads()) == 1
    assert wait_for(lambda: not worker_threads(), seconds=10)                                                     # the app's shutdown stopped it


def test_the_worker_is_not_started_unless_asked(migrated):
    app = gateway.create_app_from_env(good_env(DATABASE_URL=migrated.dsn))
    with TestClient(app):
        assert worker_threads() == []


def test_an_embedded_worker_without_token_keys_refuses_to_start():
    with pytest.raises(RuntimeError, match="WA_TOKEN_KEYS"):
        gateway.create_app_from_env(good_env(WA_ENV="", WA_TOKEN_KEYS="", WA_EMBEDDED_WORKER="yes"))


def test_the_embedded_worker_starts_its_loop_again_if_it_dies_and_stops_when_asked(monkeypatch):
    calls, stop = [], threading.Event()

    def loop(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("it died")
        stop.set()
    monkeypatch.setattr(embedded.worker, "run_forever", loop)
    monkeypatch.setattr(embedded, "RESTART_AFTER_SECONDS", 0)
    embedded._guard(None, None, None, stop)
    assert len(calls) == 3


def test_migrating_at_start_refuses_to_be_the_first_and_changes_nothing(legacy):
    with pytest.raises(RuntimeError, match="first-run"):
        gateway.create_app_from_env(good_env(DATABASE_URL=legacy.dsn, WA_MIGRATE_ON_START="yes"))
    assert legacy.one("select to_regclass('public.wa_schema_migrations')") is None


def test_migrating_at_start_applies_everything_once_it_is_allowed_and_then_has_nothing_to_do(legacy):
    gateway.create_app_from_env(good_env(DATABASE_URL=legacy.dsn, WA_MIGRATE_ON_START="yes", WA_FIRST_RUN="yes"))
    assert health.readiness(connect_to(legacy))[1]["schema"] == "current"
    gateway.create_app_from_env(good_env(DATABASE_URL=legacy.dsn, WA_MIGRATE_ON_START="yes"))               # no flag needed now
    assert migrate.apply_on_start(legacy.dsn) == []


def test_migrating_at_start_is_not_done_unless_asked(legacy):
    gateway.create_app_from_env(good_env(DATABASE_URL=legacy.dsn))
    assert legacy.one("select to_regclass('public.wa_schema_migrations')") is None


# ── the three blueprint files must not drift apart ────────────────────────────────────────────────────────────────────────────────────────

def test_the_three_blueprints_agree_on_what_they_must():
    import yaml
    docs = {n: yaml.safe_load((ROOT / n).read_text(encoding="utf-8")) for n in ("render.yaml", "render.free.yaml", "render.single.yaml")}

    def env(svc):
        return {e["key"]: e for e in svc["envVars"]}

    def asked_for(svc):
        return {k for k, v in env(svc).items() if v.get("sync") is False}
    paid_gateway, paid_worker = docs["render.yaml"]["services"]
    free, single = docs["render.free.yaml"]["services"][0], docs["render.single.yaml"]["services"][0]
    assert (paid_gateway["type"], paid_worker["type"]) == ("web", "worker")
    assert "WA_EMBEDDED_WORKER" not in env(paid_gateway)                           # the worker is its own service there: it must never also run inside the gateway
    assert len(docs["render.free.yaml"]["services"]) == len(docs["render.single.yaml"]["services"]) == 1
    assert free["plan"] == "free" and single["plan"] == "0.5c-512mb"               # free is free; the $7 one is the smallest paid size
    for svc in (free, single):
        assert env(svc)["WA_ENV"]["value"] == "prod" and env(svc)["WA_EMBEDDED_WORKER"]["value"] == "yes"
        assert svc["healthCheckPath"] == "/healthz" and svc["runtime"] == "docker" and svc["region"] == "singapore"
    assert asked_for(free) == asked_for(single) == asked_for(paid_gateway)         # the same secrets are asked for in all three
    assert env(free)["WA_MIGRATE_ON_START"]["value"] == "yes" and "preDeployCommand" not in free         # no pre-deploy step on the free tier: it migrates at start
    assert single["preDeployCommand"] == "python db/apply.py --pending" and "WA_MIGRATE_ON_START" not in env(single)      # the paid one migrates BEFORE going live


# ── the database is shared with other Prism services: never quietly adopt someone else's table ──────────────────────────────────────────────

# Tables other Prism services keep in the SAME database. Keep in step with prism-license-server/app/models.py (every __tablename__) and the legacy CRM.
FOREIGN_TABLES = {"audit_log", "credit_ledger", "credit_offers", "credit_rates", "credit_requests", "customers", "device_sightings", "devices", "leads_calls",
                  "licenses", "payloads", "usage_events", "alembic_version",
                  "tenants", "contacts", "conversations", "messages", "leads", "follow_up_queue", "activity_log", "tasks", "users", "settings"}


def test_no_table_prism_wa_creates_has_the_name_of_one_that_another_prism_service_keeps_there():
    taken = sorted(set(migrations.created_tables()) & FOREIGN_TABLES)
    assert taken == [], "these would silently adopt another service's table: %s" % taken               # audit_log was one: the licence server's is called that


def test_the_first_run_refuses_when_a_table_it_would_create_already_belongs_to_someone_else(legacy):
    legacy.q("create table orders (id serial primary key, total numeric)")                              # e.g. the CRM's own
    conn = psycopg2.connect(legacy.dsn)
    with pytest.raises(migrations.FirstRunRefused, match="orders"):
        migrations.apply_pending(conn, first_run=True)
    assert migrations.collisions(conn) == ["orders"]
    conn.close()
    assert legacy.one("select to_regclass('public.wa_schema_migrations')") is None                    # nothing was changed
    assert legacy.one("select to_regclass('public.wa_platform_config')") is None


def test_the_two_tables_shared_by_design_are_not_collisions(legacy):
    conn = psycopg2.connect(legacy.dsn)
    assert migrations.collisions(conn) == []
    legacy.q("create table segments (id serial primary key, name text)")
    legacy.q("create table campaigns (id serial primary key, name text)")
    assert migrations.collisions(conn) == []
    conn.close()


def test_the_status_report_names_collisions_before_anything_is_applied(legacy, capsys):
    legacy.q("create table products (id serial primary key)")
    assert migrations.main(["apply.py", "--status", legacy.dsn]) == 0
    assert "name collisions: products" in capsys.readouterr().out


def _preflight_fragment():
    text = (ROOT / "db" / "preflight.sql").read_text(encoding="utf-8")
    start = text.index("'name_collisions', ") + len("'name_collisions', ")
    return text[start:text.index(",\n  'postgres_version'")]


def test_preflight_reports_the_same_collisions_and_lists_every_table_the_migrations_create(legacy):
    frag = _preflight_fragment()
    listed = set(__import__("re").findall(r"'(\w+)'", frag.split("unnest(array[", 1)[1].split("]", 1)[0]))
    assert listed == set(migrations.created_tables()) - set(migrations.SHARED_BY_DESIGN)                   # a new table must be added to the preflight too
    assert legacy.one("select %s" % frag) == []                                                          # a database that never had prism-wa: nothing taken
    legacy.q("create table orders (id serial primary key)")
    legacy.q("create table flows (id serial primary key)")
    assert legacy.one("select %s" % frag) == ["flows", "orders"]


# ── the database and its connections are SHARED with the licence server ──────────────────────────────────────────────────────────────────────

def test_messages_are_sent_four_at_a_time_unless_told_otherwise(monkeypatch):
    from prism_wa import send as send_module
    monkeypatch.delenv("WA_SEND_CONCURRENCY", raising=False)
    assert send_module._default_concurrency() == 4
    for given, want in (("2", 2), ("99", 16), ("0", 1), ("many", 4), ("", 4)):
        monkeypatch.setenv("WA_SEND_CONCURRENCY", given)
        assert send_module._default_concurrency() == want, given


def test_migrations_use_their_own_connection_string_when_given_and_the_service_keeps_its_own(legacy, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:1/none")                      # the service's (could be the transaction pooler): unreachable here
    monkeypatch.setenv("WA_MIGRATE_DATABASE_URL", legacy.dsn)
    assert migrations.main(["apply.py", "--status"]) == 0                                    # the runner used the migration string
    assert "name collisions: none" in capsys.readouterr().out
    gateway.create_app_from_env(good_env(DATABASE_URL="postgresql://127.0.0.1:1/none", WA_MIGRATE_ON_START="yes", WA_FIRST_RUN="yes",
                                         WA_MIGRATE_DATABASE_URL=legacy.dsn))                # ...and so did the start-up migration
    assert health.readiness(connect_to(legacy))[1]["schema"] == "current"


def test_the_backup_script_copies_what_the_first_migrations_change_leaves_the_originals_alone_and_locks_the_copies(legacy):
    sql = (ROOT / "db" / "backup_before_first_run.sql").read_text(encoding="utf-8")
    block = sql.split("-- What was copied", 1)[0]                                            # (the report under it needs libxml, which a minimal Postgres lacks)
    legacy.q("insert into tenants (slug, name) values ('acme', 'Acme'), ('beta', 'Beta')")
    before = {t: legacy.one("select count(*) from %s" % t) for t in ("tenants", "contacts", "conversations", "messages", "leads")}
    legacy.q(block)
    for t, n in before.items():
        assert legacy.one("select count(*) from bak_%s" % t) == n, t
        assert legacy.one("select count(*) from %s" % t) == n                               # the originals are untouched
        assert legacy.one("select relrowsecurity from pg_class where relname = 'bak_%s'" % t) is True
        assert legacy.one("select has_table_privilege('anon', 'bak_%s', 'select')" % t) is False        # not reachable with the public key
    legacy.q("insert into tenants (slug, name) values ('later', 'Later')")
    legacy.q(block)                                                                          # run again: the FIRST copy is kept
    assert legacy.one("select count(*) from bak_tenants") == before["tenants"]
    report = sql.split("-- What was copied", 1)[1].split("\n", 1)[1]
    legacy.q("explain " + report)                                                            # the report under it is at least valid SQL (it cannot RUN without libxml)


# ── a NEW, EMPTY Supabase project: the baseline, then the migrations, with no tenant invented ────────────────────────────────────────────────

@pytest.fixture
def fresh(admin_dsn_for):
    """A database that has only what a new Supabase project has (the API roles and the default privileges), nothing of the old CRM."""
    import conftest as cf
    import pg
    name = pg.DB_PREFIX + "fresh_" + uuid.uuid4().hex[:8]
    cf._create_db(admin_dsn_for, name)
    db = cf.Db(admin_dsn_for, name)
    db.q(cf._read("supabase_stubs.sql"))
    yield db
    db.close()
    cf._drop_db(admin_dsn_for, name)


BASELINE = ROOT / "db" / "baseline" / "0000_fresh_project.sql"


def test_the_baseline_creates_exactly_the_old_crm_base_tables_the_tests_run_against_and_no_data():
    text = BASELINE.read_text(encoding="utf-8")
    fixtures = ROOT / "tests" / "fixtures"
    expected = set(re.findall(r"create table if not exists (\w+)", (fixtures / "legacy_schema.sql").read_text(encoding="utf-8"), re.I)) | {"tenants"}
    assert set(re.findall(r"create table if not exists (\w+)", text, re.I)) == expected
    assert not re.search(r"insert\s+into", text, re.I)                                                 # no seed row: no Shakti, no Orena, nobody's data


def test_a_new_empty_project_can_be_set_up_with_the_baseline_then_the_migrations_and_has_no_tenant_until_you_make_one(fresh, monkeypatch):
    fresh.q(BASELINE.read_text(encoding="utf-8"))
    fresh.q(BASELINE.read_text(encoding="utf-8"))                                                       # safe to run twice
    for t in ("users", "contacts", "leads", "tenants", "settings"):                                      # locked against the public Supabase key
        assert fresh.one("select relrowsecurity from pg_class where relname = %s", (t,)) is True
        assert fresh.one("select has_table_privilege('anon', %s, 'select')", (t,)) is False
    monkeypatch.setenv("WA_LEGACY_TENANT", "none")
    conn = psycopg2.connect(fresh.dsn)
    first = migrations.apply_pending(conn, first_run=True)
    assert len(first) == len(migrations.migration_files())
    conn.close()
    assert fresh.one("select count(*) from tenants") == 0                                                # no 'orena', no one
    assert fresh.one("select wa_default_tenant()") is None                                               # strict: a row must name its workspace
    with pytest.raises(psycopg2.errors.NotNullViolation):
        fresh.q("insert into contacts (name, phone) values ('Nobody', '919800000001')")
    tenant = fresh.one("insert into tenants (slug, name) values ('acme', 'Acme') returning id")
    fresh.q("insert into contacts (tenant_id, name, phone) values (%s, 'Asha', '919800000001')", (tenant,))
    assert health.readiness(connect_to(fresh))[1]["schema"] == "current"


def test_without_the_setting_the_old_tenant_is_still_orena_and_a_named_one_is_used_as_given(legacy, make_db, monkeypatch):
    monkeypatch.delenv("WA_LEGACY_TENANT", raising=False)
    conn = psycopg2.connect(legacy.dsn)
    migrations.apply_pending(conn, first_run=True)
    conn.close()
    assert legacy.one("select name from tenants where slug = 'orena'") == "Orena"                        # unchanged behaviour for an existing project
    other = make_db("live")
    monkeypatch.setenv("WA_LEGACY_TENANT", "Acme")
    conn = psycopg2.connect(other.dsn)
    migrations.apply_pending(conn, first_run=True)
    conn.close()
    assert other.one("select name from tenants where slug = 'acme'") == "Acme"
    assert other.one("select count(*) from tenants where slug = 'orena'") == 0
    assert other.one("select value from wa_platform_config where key = 'legacy_default_tenant'") == str(other.one("select id from tenants where slug = 'acme'"))


# ── the licence server asks THIS service whether a workspace exists (not the old CRM's database) ─────────────────────────────────────────────

def test_a_workspace_can_be_looked_up_by_the_licence_server_and_only_with_the_token(migrated):
    tenant = H.tenant(migrated, "orena")
    client = TestClient(create_app(app_secrets=SECRET, verify_token=TOKEN, connect=connect_to(migrated), internal_token=INTERNAL))

    def ask(tenant_id, headers=None):
        return client.post("/internal/tenant/exists", json={"tenant_id": str(tenant_id)}, headers={"X-Internal-Token": INTERNAL} if headers is None else headers)
    assert ask(tenant).json() == {"ok": True, "exists": True}
    assert ask("00000000-0000-4000-8000-000000000000").json() == {"ok": True, "exists": False}
    assert ask(tenant, headers={}).status_code == 401
    assert ask(tenant, headers={"X-Internal-Token": "wrong"}).status_code == 401
