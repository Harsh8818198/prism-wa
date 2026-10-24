"""The webhook gateway (spec WS2): verify, normalise, store, enqueue, answer.

    GET  /webhook/wa   Meta's subscription handshake (echo hub.challenge).
    POST /webhook/wa   A delivery. Checked against X-Hub-Signature-256 on the
                       RAW body first; a forged or unsigned request gets 401 and
                       leaves no trace in the database. Then each event is
                       stored (deduplicated) and a job queued, and Meta gets 200.
    GET  /, /privacy, /data-deletion   The public pages Meta asks for before an app goes live (legal.py).
    GET  /healthz      Liveness (never touches the database).
    GET  /readyz       Readiness: the database answers and has every migration; is the worker alive (health.py).

If the database write fails the answer is 500, never 200: Meta retries a
failed delivery with backoff, and the dedupe key makes the retry harmless.
Acknowledging an event we did not record would lose it.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Callable, Iterable, Optional, Union

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from prism_wa import health, hooks, internal, legal, public_api
from prism_wa.events import normalise
from prism_wa.graph import Graph
from prism_wa.signature import verify_signature
from prism_wa.store import ingest
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.gateway")

MAX_BODY = 5 * 1024 * 1024     # Meta's deliveries are small; refuse anything absurd


def _persist(connect: Callable, events: list) -> dict:
    conn = connect()
    try:
        return ingest(conn, events).as_dict()
    finally:
        conn.close()


def create_app(*, app_secrets: Union[str, Iterable[str]], verify_token: str,
               connect: Callable, internal_token: str = "", app_id: str = "",
               es_config_id: str = "", graph: Optional[Graph] = None,
               vault: Optional[Vault] = None) -> FastAPI:
    """`connect()` returns a new DB-API connection (psycopg2) per call.
    `app_secrets` may hold several secrets so one can be rotated in without a gap.
    With `internal_token` the service-to-service API and the connect page are mounted too
    (internal.py); without it they do not exist."""
    secrets = tuple(s for s in ([app_secrets] if isinstance(app_secrets, str) else app_secrets) if s)
    if not secrets:
        raise ValueError("an app secret is required: a gateway without one would accept forged events")

    app = FastAPI(title="prism-wa gateway", docs_url=None, redoc_url=None, openapi_url=None)
    if internal_token:
        public_api.mount(app, connect=connect)
        hooks.mount(app, connect=connect, vault=vault)
        internal.register(app, internal_token=internal_token, connect=connect, graph=graph or Graph(),
                          vault=vault, app_id=app_id, app_secret=secrets[0], es_config_id=es_config_id,
                          verify_token=verify_token)

    legal.mount(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        status, body = await run_in_threadpool(health.readiness, connect)
        return JSONResponse(body, status_code=status)

    @app.get("/webhook/wa")
    async def handshake(request: Request):
        q = request.query_params
        given = (q.get("hub.verify_token") or "").encode("utf-8")
        if (verify_token and q.get("hub.mode") == "subscribe"
                and hmac.compare_digest(given, verify_token.encode("utf-8"))):
            return PlainTextResponse(q.get("hub.challenge") or "")
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook/wa")
    async def receive(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        if not verify_signature(secrets, raw, request.headers.get("x-hub-signature-256")):
            return JSONResponse({"error": "bad signature"}, status_code=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        events = normalise(payload)
        if not events:
            return {"ok": True, "events": 0}
        try:
            result = await run_in_threadpool(_persist, connect, events)
        except Exception:                                   # noqa: BLE001
            log.exception("could not record a webhook delivery; asking Meta to retry")
            return JSONResponse({"error": "try again"}, status_code=500)
        return {"ok": True, **result}

    return app
