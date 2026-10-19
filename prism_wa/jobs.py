"""A small Postgres job queue (spec §2: `FOR UPDATE SKIP LOCKED`, no Redis).

Jobs live in `wa_jobs`. A worker claims a batch inside one transaction, runs
each handler under a savepoint, and commits: if the process dies mid-batch the
locks vanish and every job in it is still `queued`, so nothing is lost and
nothing runs twice. (Sending is different — see send.py: an HTTP call cannot be
rolled back, so it records "sending" before the call.)

A handler gets (cursor, tenant_id, payload). It may:
  * return normally            -> the job is `done`
  * raise Defer(seconds)       -> not ready yet (e.g. a status that raced its
                                  message); requeued, attempt not counted
  * raise anything else        -> the savepoint rolls back, the attempt is
                                  counted, the job is requeued with backoff and
                                  becomes `dead` after MAX_ATTEMPTS
Enqueueing is idempotent on `idem_key`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from psycopg2.extras import Json

log = logging.getLogger("prism_wa.jobs")

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 30, 120, 600, 3600)


class Defer(Exception):
    """Raise from a handler to try the job again later without counting a failure."""

    def __init__(self, seconds: float = 30):
        super().__init__("deferred %ss" % seconds)
        self.seconds = seconds


@dataclass
class RunStats:
    claimed: int = 0
    done: int = 0
    deferred: int = 0
    retried: int = 0
    dead: int = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(cur, tenant_id, kind: str, payload: dict, idem_key: str,
            run_at: Optional[datetime] = None) -> bool:
    """Queue a job; False when one with this idem_key already exists."""
    cur.execute(
        "insert into wa_jobs (tenant_id, kind, payload, idem_key, run_at)"
        " values (%s, %s, %s, %s, coalesce(%s, now())) on conflict (idem_key) do nothing",
        (tenant_id, kind, Json(payload), idem_key, run_at))
    return cur.rowcount == 1


def run_once(conn, handlers: dict, *, worker_id: str = "worker", batch: int = 20,
             now: Optional[datetime] = None) -> RunStats:
    """Claim up to `batch` due jobs, run them, commit. Returns what happened."""
    now = now or utcnow()
    stats = RunStats()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select id, tenant_id, kind, payload, attempts from wa_jobs"
                " where state = 'queued' and run_at <= %s"
                " order by run_at, created_at limit %s for update skip locked", (now, batch))
            jobs = cur.fetchall()
            stats.claimed = len(jobs)
            for job_id, tenant_id, kind, payload, attempts in jobs:
                handler = handlers.get(kind)
                cur.execute("savepoint job")
                try:
                    if handler is not None:
                        handler(cur, tenant_id, payload or {})
                    cur.execute("release savepoint job")
                    cur.execute("update wa_jobs set state = 'done', locked_by = %s, locked_at = %s,"
                                " error = null where id = %s", (worker_id, now, job_id))
                    stats.done += 1
                except Defer as later:
                    cur.execute("rollback to savepoint job")
                    cur.execute("update wa_jobs set run_at = %s where id = %s",
                                (now + timedelta(seconds=later.seconds), job_id))
                    stats.deferred += 1
                except Exception as exc:                            # noqa: BLE001
                    cur.execute("rollback to savepoint job")
                    attempts += 1
                    log.warning("job %s (%s) failed on attempt %d: %s", job_id, kind, attempts, exc)
                    if attempts >= MAX_ATTEMPTS:
                        state, run_at = "dead", now
                        stats.dead += 1
                    else:
                        state = "queued"
                        run_at = now + timedelta(seconds=BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)])
                        stats.retried += 1
                    cur.execute("update wa_jobs set state = %s, attempts = %s, run_at = %s, error = %s,"
                                " locked_by = %s, locked_at = %s where id = %s",
                                (state, attempts, run_at, ("%s: %s" % (type(exc).__name__, exc))[:2000],
                                 worker_id, now, job_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats
