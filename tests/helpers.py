"""Shared helpers for the worker tests."""
import payloads as P
import psycopg2

from prism_wa import ingest as ingest_mod
from prism_wa import jobs
from prism_wa.events import normalise
from prism_wa.store import ingest


def tenant(db, slug):
    return db.one("select id from tenants where slug = %s", (slug,))


def onboard(db, slug="orena", waba=P.WABA, pnid=P.PNID):
    """What Embedded Signup will do: give a tenant a WABA and a number."""
    tid = tenant(db, slug)
    wid = db.one("insert into wabas (tenant_id, waba_id) values (%s, %s) returning id", (tid, waba))
    nid = db.one("insert into phone_numbers (tenant_id, waba_id, phone_number_id) values (%s, %s, %s)"
                 " returning id", (tid, wid, pnid))
    return tid, nid


def deliver(db, payload):
    """Record a webhook payload exactly as the gateway would."""
    conn = psycopg2.connect(db.dsn)
    try:
        return ingest(conn, normalise(payload))
    finally:
        conn.close()


def work(db, handlers=None, *, now=None, rounds=5):
    """Run the worker until the queue is quiet; returns the summed stats."""
    handlers = handlers or ingest_mod.JOB_HANDLERS
    total = jobs.RunStats()
    conn = psycopg2.connect(db.dsn)
    try:
        for _ in range(rounds):
            s = jobs.run_once(conn, handlers, now=now)
            for k in total.__dict__:
                setattr(total, k, getattr(total, k) + getattr(s, k))
            if not s.claimed:
                break
    finally:
        conn.close()
    return total


def only(db, sql, params=None):
    rows = db.q(sql, params)
    assert len(rows) == 1, rows
    return rows[0]
