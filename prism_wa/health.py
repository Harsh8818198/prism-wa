"""Is the service ready, and is its worker alive? (`GET /readyz`)

`/healthz` only says the process answers; the host uses it to decide whether to restart the container, so it must never depend on the database (a blip
there would restart a healthy gateway). `/readyz` is for people and monitors: it opens a connection, checks that the database has every migration this
build ships with (a deploy whose migrations did not run is the commonest way to break a release), and reports whether the worker has been seen lately.

The worker has no address of its own, so it leaves a heartbeat in `wa_platform_config` (a table that already exists, so this needs no migration): the time
it last finished a pass. A worker that has stopped is silent to customers — queued messages, scheduled campaigns, webhooks out and payment checks all wait
on it — so this is the one thing worth looking at first when "nothing is being sent".
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HEARTBEAT_KEY = "worker_heartbeat"
BEAT_EVERY_SECONDS = 30
STALE_AFTER_SECONDS = 180                    # six missed beats: a restart takes seconds, a stuck worker takes much longer

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def shipped_migrations(folder: Optional[Path] = None) -> list:
    """The migration names this build was made with (file stems, in order)."""
    return sorted(p.stem for p in (folder or MIGRATIONS_DIR).glob("[0-9][0-9][0-9][0-9]_*.sql"))


def beat(connect: Callable, *, now: Optional[datetime] = None) -> None:
    """Record that the worker finished a pass. Never raises: a heartbeat that fails must not stop the worker."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("insert into wa_platform_config (key, value) values (%s, %s) "
                            "on conflict (key) do update set value = excluded.value, updated_at = now()", (HEARTBEAT_KEY, stamp))
            conn.commit()
        finally:
            conn.close()
    except Exception:                         # noqa: BLE001
        pass


def readiness(connect: Callable, *, now: Optional[datetime] = None, folder: Optional[Path] = None) -> tuple:
    """(http status, body). 200 when the database answers and has every migration this build ships; 503 when it does not answer or is behind.

    `schema` is `current`, `behind` (with how many are missing) or `unknown` when migrations were applied by hand and not recorded. `worker` is
    `running`, `stale` or `never`. A stale or missing worker does not make the gateway unready (it is a different process), it is only reported."""
    body = {"ok": False, "database": False, "schema": "unknown", "worker": "never"}
    try:
        conn = connect()
    except Exception:                         # noqa: BLE001
        return 503, body
    try:
        with conn.cursor() as cur:
            cur.execute("select 1")
            body["database"] = True
            cur.execute("select to_regclass('public.wa_schema_migrations')")
            if cur.fetchone()[0] is not None:
                cur.execute("select name from wa_schema_migrations")
                have = {r[0] for r in cur.fetchall()}
                missing = [m for m in shipped_migrations(folder) if m not in have]
                body["schema"] = "behind" if missing else "current"
                if missing:
                    body["migrations_missing"] = len(missing)
            row = None
            cur.execute("select to_regclass('public.wa_platform_config')")
            if cur.fetchone()[0] is not None:
                cur.execute("select value from wa_platform_config where key = %s", (HEARTBEAT_KEY,))
                row = cur.fetchone()
        if row:
            try:
                seen = datetime.fromisoformat(row[0])
                age = ((now or datetime.now(timezone.utc)) - seen).total_seconds()
                body["worker"] = "running" if age <= STALE_AFTER_SECONDS else "stale"
                body["worker_seen_seconds_ago"] = max(0, int(age))
            except ValueError:
                body["worker"] = "stale"
        conn.rollback()
    except Exception:                         # noqa: BLE001
        return 503, body
    finally:
        try:
            conn.close()
        except Exception:                     # noqa: BLE001
            pass
    body["ok"] = body["schema"] != "behind"
    return (200 if body["ok"] else 503), body
