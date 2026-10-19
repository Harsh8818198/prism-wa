"""The webhook receiver for connected stores: POST /hooks/<shopify|woocommerce>/<token>.

The token in the address says which connection this is; the signature says the request really came from
that store. Both must hold: an unknown token is a 404, a bad signature a 401 that changes nothing. A good
request is recorded once and answered 200 quickly (the store retries anything else), and a database
failure is a 500 so the store sends it again.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from prism_wa import integrations
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.hooks")
MAX_BODY = 2 * 1024 * 1024

HEADERS = {
    "shopify": {"signature": "x-shopify-hmac-sha256", "topic": "x-shopify-topic", "id": "x-shopify-webhook-id"},
    "woocommerce": {"signature": "x-wc-webhook-signature", "topic": "x-wc-webhook-topic", "id": ""},
}


def mount(app: FastAPI, *, connect: Callable, vault: Optional[Vault]) -> None:
    def process(kind: str, token: str, raw: bytes, headers: dict):
        conn = connect()
        try:
            with conn.cursor() as cur:
                integration = integrations.load(cur, kind, token, vault)
                if integration is None:
                    return 404, {"error": "not_found"}
                names = HEADERS[kind]
                if not integrations.signature_ok(integration["secret"], raw, headers.get(names["signature"])):
                    return 401, {"error": "bad_signature"}
                topic = headers.get(names["topic"], "")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    return 200, {"ok": True, "status": "ping"}            # WooCommerce's first call is not JSON
                if not isinstance(payload, dict):
                    return 200, {"ok": True, "status": "ignored"}
                ext = integrations.external_id(kind, topic, payload, headers.get(names["id"], "") if names["id"] else "")
                status = integrations.handle_event(cur, integration, topic, payload, ext)
            conn.commit()
            return 200, {"ok": True, "status": status}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/hooks/{kind}/{token}")
    async def receive(kind: str, token: str, request: Request):
        if kind not in HEADERS or vault is None or len(token) > 80:
            return JSONResponse({"error": "not_found"}, status_code=404)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "too_large"}, status_code=413)
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            status, body = await run_in_threadpool(process, kind, token, raw, headers)
        except Exception:                                               # noqa: BLE001
            log.exception("could not record a store webhook; asking the store to retry")
            return JSONResponse({"error": "try_again"}, status_code=500)
        return JSONResponse(body, status_code=status)
