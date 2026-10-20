"""What a live deployment needs that a feature test never touches: the readiness check, the worker's heartbeat, migrations that apply only what is
missing (and refuse to be the first), secrets that must be strong, and the post-deploy smoke test itself."""
import json
import os
import socket
import sys
import threading
import time
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
    assert len(migrations.apply_pending(conn, first_run=True)) == len(migrations.migration_files())
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
