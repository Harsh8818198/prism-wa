"""The worker, as a thread of the gateway (WA_EMBEDDED_WORKER=yes).

For a host that gives a web service for free but not a background worker (Render's free tier). One process then does both jobs: the gateway
answers Meta and the licence server, and this thread sends queued messages, runs campaigns, delivers webhooks and checks payments, exactly as
`python -m prism_wa.worker` would (it is the same loop, `worker.run_forever`).

What it cannot do is work while the host has put the service to sleep: a free service that has had no traffic for a while is stopped and starts
again on the next request, so queued sends and scheduled campaigns wait until then. `/readyz` says whether it has been seen lately.
On a paid host, run the worker as its own service instead; do not set this on both.
"""
from __future__ import annotations

import logging
import os
import threading

from prism_wa import worker

log = logging.getLogger("prism_wa.embedded")

THREAD_NAME = "prism-wa-worker"
RESTART_AFTER_SECONDS = 5
JOIN_SECONDS = 20


def _guard(connect, graph, vault, stop: threading.Event) -> None:
    """run_forever already survives a failed pass; this survives anything else, so the thread is never the thing that quietly stops."""
    while not stop.is_set():
        try:
            worker.run_forever(connect, graph=graph, vault=vault, worker_id="embedded-%d" % os.getpid(), stop=stop)
        except Exception:                                   # noqa: BLE001
            log.exception("the embedded worker stopped unexpectedly; starting it again")
            stop.wait(RESTART_AFTER_SECONDS)


def attach(app, connect, *, graph, vault) -> None:
    """Start the worker thread when the app starts and ask it to finish its pass and stop when the app does."""
    if vault is None:
        raise RuntimeError("WA_EMBEDDED_WORKER needs WA_TOKEN_KEYS: without it nothing stored for a customer can be used to send")
    stop = threading.Event()
    holder = {}

    def start() -> None:
        stop.clear()
        t = threading.Thread(target=_guard, args=(connect, graph, vault, stop), name=THREAD_NAME, daemon=True)
        holder["thread"] = t
        t.start()
        log.info("the worker is running inside the gateway")

    def finish() -> None:
        stop.set()
        t = holder.get("thread")
        if t is not None:
            t.join(JOIN_SECONDS)

    app.add_event_handler("startup", start)
    app.add_event_handler("shutdown", finish)
