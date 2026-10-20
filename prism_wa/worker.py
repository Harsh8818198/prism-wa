"""The worker process: ingest events, deliver the outbox, close crashed sends.

    DATABASE_URL=postgresql://...  WA_TOKEN_KEYS=1:...  python -m prism_wa.worker

One loop, run as many copies as needed: the queue and the outbox both claim with
FOR UPDATE SKIP LOCKED, so workers never take the same row. A cycle that finds
nothing sleeps; one that finds work goes straight round again.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from prism_wa import ai_agent, audit, autos, capi, embeddings, health, inbox, ingest, jobs, llm, media, media_store, payments, schedules, send, sso, webhooks_out
from prism_wa.graph import Graph
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.worker")

IDLE_SLEEP = 1.0
REAP_EVERY = timedelta(minutes=1)


@dataclass
class Cycle:
    ingested: int = 0
    sent: int = 0
    reaped: int = 0
    resolved: int = 0
    scheduled: int = 0
    hooks: int = 0

    @property
    def busy(self) -> bool:
        return bool(self.ingested or self.sent or self.reaped or self.resolved or self.scheduled or self.hooks)


def cycle(connect: Callable, *, graph: Graph, vault: Vault, limiter: Optional[send.RateLimiter] = None,
          worker_id: str = "worker", reap: bool = True, now: Optional[datetime] = None) -> Cycle:
    """One pass: ingest due jobs, deliver due messages, optionally reap stuck sends."""
    out = Cycle()
    conn = connect()
    try:
        out.ingested = jobs.run_once(conn, ingest.JOB_HANDLERS, worker_id=worker_id, now=now).claimed
    finally:
        conn.close()
    conn = connect()
    try:                                                    # repeating campaigns that are due become ordinary campaigns
        with conn.cursor() as cur:
            out.scheduled = schedules.run_due(cur, now=now)
        conn.commit()
    except Exception:                                       # noqa: BLE001  (never let this stop sending)
        conn.rollback()
        log.exception("could not run the repeating campaigns")
    finally:
        conn.close()
    conn = connect()
    try:                                                    # birthday and reminder messages that are due, and the next step of every drip
        with conn.cursor() as cur:
            out.scheduled += autos.run_due(cur, now=now)
        conn.commit()
    except Exception:                                       # noqa: BLE001  (never let this stop sending)
        conn.rollback()
        log.exception("could not run the automatic messages")
    finally:
        conn.close()
    out.sent = send.send_due(connect, graph=graph, vault=vault, limiter=limiter, now=now).claimed
    try:                                                    # tell the workspaces' own systems what happened (never stops sending)
        told = webhooks_out.deliver_due(connect, now=now)
        out.hooks = told["sent"] + told["retry"] + told["failed"]
    except Exception:                                       # noqa: BLE001
        log.exception("could not deliver webhooks")
    if reap:
        out.reaped = send.reap_stuck(connect, now=now)
        out.resolved = inbox.auto_resolve(connect, now=now)
        conn = connect()
        try:                                                # payments whose news never came: ask the gateway, and expire the ones that ran out of time
            with conn.cursor() as cur:
                out.resolved += payments.reconcile(cur, graph=graph, vault=vault, now=now)
                audit.purge(cur, now=now)                   # lines older than 400 days, and sign-in sessions that ended
                sso.purge(cur, now=now)
            conn.commit()
        except Exception:                                   # noqa: BLE001
            conn.rollback()
            log.exception("could not check open payments")
        finally:
            conn.close()
    return out


def run_forever(connect: Callable, *, graph: Graph, vault: Vault, worker_id: str = "", stop: Optional[threading.Event] = None) -> None:
    """Run passes until `stop` is set (or SIGINT/SIGTERM arrives, when this is the main thread). It finishes the pass it is on first."""
    worker_id = worker_id or "%s-%d" % (socket.gethostname(), os.getpid())
    limiter = send.RateLimiter()
    stop = stop if stop is not None else threading.Event()

    def _stop(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):                       # not the main thread / unsupported here
            pass

    last_reap = datetime.min.replace(tzinfo=timezone.utc)
    last_beat = datetime.min.replace(tzinfo=timezone.utc)
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        try:
            due = jobs.utcnow() - last_reap >= REAP_EVERY
            c = cycle(connect, graph=graph, vault=vault, limiter=limiter, worker_id=worker_id, reap=due)
            if due:
                last_reap = jobs.utcnow()
            if jobs.utcnow() - last_beat >= timedelta(seconds=health.BEAT_EVERY_SECONDS):      # "I am alive", for /readyz
                health.beat(connect)
                last_beat = jobs.utcnow()
        except Exception:                                   # noqa: BLE001
            log.exception("worker cycle failed; backing off")
            stop.wait(5)
            continue
        if not c.busy:
            stop.wait(IDLE_SLEEP)
    log.info("worker %s stopped", worker_id)


def main() -> None:
    from prism_wa.main import configure_logging, connector
    configure_logging()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("missing environment: DATABASE_URL")
    ai_agent.configure(llm.from_env(os.environ), embedder=embeddings.from_env(os.environ))            # AI agents answer from here (WA_LLM_API_KEY), or hand over to a person
    graph, vault = Graph(), Vault.from_env()
    payments.configure(vault)                                                                                     # a chatbot flow that asks for payment needs the gateway's secret
    media.configure(graph=graph, vault=vault, store=media_store.from_env(os.environ, connector(dsn)))     # a customer's pictures are kept; ours are sent
    capi.configure(graph=graph, vault=vault)                                                              # a flow's "tell Meta" step
    run_forever(connector(dsn), graph=graph, vault=vault)


if __name__ == "__main__":
    main()
