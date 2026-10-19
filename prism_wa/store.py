"""Persisting events and enqueueing their work (spec WS2).

The gateway's whole job after the signature check: write the event down, put a
job on the queue, answer Meta. Nothing is processed here — processing is a
worker's business (WS3 onwards), so a slow or crashing handler can never make
Meta retry or drop a delivery.

One delivery is one transaction: all of its events are stored, or none are and
the caller answers 500 so Meta retries. Dedupe makes that retry harmless.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg2.extras import Json

JOB_KIND = "ingest_event"


@dataclass
class IngestResult:
    events: int = 0          # events in the delivery
    stored: int = 0          # new rows written
    duplicates: int = 0      # already seen (a redelivery); nothing written
    unrouted: int = 0        # stored, but no tenant owns that number yet
    jobs: int = 0            # jobs enqueued

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def route(cur, waba_id: str, phone_number_id: str):
    """The tenant that owns this number, else the one that owns its WABA, else
    None. Number first: it is the more specific of the two."""
    if phone_number_id:
        cur.execute("select tenant_id from phone_numbers where phone_number_id = %s",
                    (phone_number_id,))
        row = cur.fetchone()
        if row:
            return row[0]
    if waba_id:
        cur.execute("select tenant_id from wabas where waba_id = %s", (waba_id,))
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def ingest(conn, events: list) -> IngestResult:
    """Store `events`, enqueue one job for each new routed one, commit.

    Raises on any database error after rolling back, so the delivery as a whole
    is retried rather than half-recorded."""
    result = IngestResult(events=len(events))
    tenants = {}
    try:
        with conn.cursor() as cur:
            for ev in events:
                key = (ev.waba_id, ev.phone_number_id)
                if key not in tenants:
                    tenants[key] = route(cur, ev.waba_id, ev.phone_number_id)
                tenant = tenants[key]
                cur.execute(
                    "insert into wa_events (tenant_id, dedupe_key, kind, waba_id, phone_number_id,"
                    " occurred_at, canonical, raw) values (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " on conflict (dedupe_key) do nothing returning id",
                    (tenant, ev.dedupe_key, ev.kind, ev.waba_id or None, ev.phone_number_id or None,
                     ev.occurred_at, Json(ev.canonical()), Json(ev.raw)))
                row = cur.fetchone()
                if row is None:
                    result.duplicates += 1
                    continue
                result.stored += 1
                if tenant is None:
                    result.unrouted += 1
                    continue
                cur.execute(
                    "insert into wa_jobs (tenant_id, kind, payload, idem_key)"
                    " values (%s, %s, %s, %s) on conflict (idem_key) do nothing",
                    (tenant, JOB_KIND, Json({"event_id": str(row[0]), "event_kind": ev.kind}),
                     "ingest:" + ev.dedupe_key))
                result.jobs += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result
