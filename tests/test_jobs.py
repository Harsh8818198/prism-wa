"""The job queue: claim, retry, defer, die, never run twice."""
from datetime import timedelta

import psycopg2
from prism_wa import jobs

import helpers as H


def queue(db, tenant, n=1, kind="demo"):
    conn = psycopg2.connect(db.dsn)
    try:
        with conn.cursor() as cur:
            for i in range(n):
                jobs.enqueue(cur, tenant, kind, {"i": i}, "%s:%d" % (kind, i))
        conn.commit()
    finally:
        conn.close()


def run(db, handlers, **kw):
    conn = psycopg2.connect(db.dsn)
    try:
        return jobs.run_once(conn, handlers, **kw)
    finally:
        conn.close()


def test_enqueue_is_idempotent(migrated):
    tenant = H.tenant(migrated, "orena")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        assert jobs.enqueue(cur, tenant, "demo", {}, "same") is True
        assert jobs.enqueue(cur, tenant, "demo", {}, "same") is False
    conn.commit()
    conn.close()
    assert migrated.one("select count(*) from wa_jobs") == 1


def test_a_handler_runs_once_per_job_and_the_job_is_done(migrated):
    tenant = H.tenant(migrated, "orena")
    queue(migrated, tenant, 3)
    seen = []
    stats = run(migrated, {"demo": lambda cur, t, p: seen.append((t, p["i"]))})
    assert (stats.claimed, stats.done) == (3, 3)
    assert sorted(i for _, i in seen) == [0, 1, 2]
    assert migrated.one("select count(*) from wa_jobs where state = 'done'") == 3
    assert run(migrated, {"demo": lambda *a: seen.append("again")}).claimed == 0


def test_a_failing_job_is_retried_with_backoff_then_dies(migrated):
    tenant = H.tenant(migrated, "orena")
    queue(migrated, tenant)

    def boom(cur, t, p):
        raise RuntimeError("nope")

    now = jobs.utcnow()
    for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        stats = run(migrated, {"demo": boom}, now=now)
        assert stats.claimed == 1, attempt
        row = migrated.q("select state, attempts, run_at, error from wa_jobs")[0]
        assert row["attempts"] == attempt and "nope" in row["error"]
        if attempt < jobs.MAX_ATTEMPTS:
            assert row["state"] == "queued" and row["run_at"] > now
            now = row["run_at"] + timedelta(seconds=1)
        else:
            assert row["state"] == "dead"
    assert run(migrated, {"demo": boom}, now=now + timedelta(days=1)).claimed == 0


def test_a_handlers_database_writes_are_undone_when_it_fails(migrated):
    tenant = H.tenant(migrated, "orena")
    queue(migrated, tenant)

    def half(cur, t, p):
        cur.execute("insert into segments (tenant_id, name) values (%s, 'half-done')", (t,))
        raise RuntimeError("after the insert")

    run(migrated, {"demo": half})
    assert migrated.one("select count(*) from segments where name = 'half-done'") == 0


def test_deferring_does_not_count_as_a_failure(migrated):
    tenant = H.tenant(migrated, "orena")
    queue(migrated, tenant)
    now = jobs.utcnow()

    def later(cur, t, p):
        raise jobs.Defer(30)

    stats = run(migrated, {"demo": later}, now=now)
    assert stats.deferred == 1
    row = migrated.q("select state, attempts, run_at from wa_jobs")[0]
    assert row["state"] == "queued" and row["attempts"] == 0 and row["run_at"] > now
    assert run(migrated, {"demo": later}, now=now).claimed == 0      # not due yet


def test_a_job_with_no_handler_completes_without_error(migrated):
    queue(migrated, H.tenant(migrated, "orena"), kind="future_kind")
    assert run(migrated, {}).done == 1
    assert migrated.one("select state from wa_jobs") == "done"


def test_two_workers_never_claim_the_same_job(migrated):
    tenant = H.tenant(migrated, "orena")
    queue(migrated, tenant, 4)
    holder = psycopg2.connect(migrated.dsn)
    try:
        with holder.cursor() as cur:             # worker A has claimed all four and not yet committed
            cur.execute("select id from wa_jobs where state = 'queued' for update skip locked")
            assert len(cur.fetchall()) == 4
        seen = []
        stats = run(migrated, {"demo": lambda *a: seen.append(1)})     # worker B
        assert stats.claimed == 0 and not seen
    finally:
        holder.rollback()
        holder.close()
    assert run(migrated, {"demo": lambda *a: None}).done == 4
