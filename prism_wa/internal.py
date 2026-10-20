"""The service-to-service API and the customer-facing connect page.

/internal/*  Called by the licence server, which has already checked the seat
             and the licence and says which tenant it is acting for. Guarded by a
             shared secret (X-Internal-Token); without WA_INTERNAL_TOKEN set the
             routes do not exist.
               POST /internal/reply              queue a free-form reply
               POST /internal/broadcast          queue one template to many contacts
               POST /internal/onboarding/link    a signed connect link for a tenant
/onboarding  The page a customer opens to connect their own number, and the
             endpoint it posts to. It carries a short-lived signed `state` that
             names the tenant, so it needs no secret of its own.

These are thin: the rules (24-hour window, opt-out, which number, idempotency)
live in send.py and are the same for a reply, a broadcast and, later, a bot.
"""
from __future__ import annotations

import hmac
import logging
import uuid
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from prism_wa import campaigns, inbox, onboarding, ops, send
from prism_wa.graph import Graph
from prism_wa.ops_common import refuse
from prism_wa.send import SendRefused
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.internal")

MAX_RECIPIENTS = 10_000


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _bad(message: str, status: int = 400, **extra):
    return JSONResponse({"ok": False, "error": "bad_request", "message": message, **extra}, status_code=status)


def _refused(exc: SendRefused):
    return JSONResponse({"ok": False, "error": exc.reason, "message": str(exc)}, status_code=409)


def register(app: FastAPI, *, internal_token: str, connect: Callable, graph: Graph, vault: Optional[Vault],
             app_id: str, app_secret: str, es_config_id: str, verify_token: str) -> None:

    def authorised(request: Request) -> bool:
        given = (request.headers.get("x-internal-token") or "").encode("utf-8")
        return bool(internal_token) and hmac.compare_digest(given, internal_token.encode("utf-8"))

    async def body_of(request: Request):
        try:
            data = await request.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # ── replies ──────────────────────────────────────────────────────────────
    def _reply(data: dict):
        conn = connect()
        try:
            with conn.cursor() as cur:
                contact_id = data.get("contact_id")
                if not contact_id:
                    cur.execute("select contact_id from conversations where id = %s and tenant_id = %s",
                                (data["conversation_id"], data["tenant_id"]))
                    row = cur.fetchone()
                    if row is None:
                        raise SendRefused("unknown_contact", "that conversation is not in this workspace")
                    contact_id = row[0]
                if data.get("agent_id"):                       # a person replying is taking the chat
                    inbox.note_reply_by(cur, data["tenant_id"], contact_id, data["agent_id"])
                outbox_id = send.enqueue_text(cur, data["tenant_id"], contact_id=contact_id, text=data["text"],
                                              idem_key=data.get("idem_key") or "reply:" + uuid.uuid4().hex,
                                              number_id=data.get("number_id"), sender=data.get("sender") or "agent")
            conn.commit()
            return str(outbox_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/internal/reply")
    async def reply(request: Request):
        if not authorised(request):
            return await refuse(request)
        data = await body_of(request)
        if data is None or not _is_uuid(data.get("tenant_id")) or not isinstance(data.get("text"), str) \
                or not (_is_uuid(data.get("contact_id")) or _is_uuid(data.get("conversation_id"))):
            return _bad("tenant_id, text and a contact_id or conversation_id are required")
        try:
            return {"ok": True, "outbox_id": await run_in_threadpool(_reply, data)}
        except SendRefused as exc:
            return _refused(exc)
        except inbox.InboxError as exc:
            return JSONResponse({"ok": False, "error": exc.reason, "message": str(exc)}, status_code=409)

    # ── broadcasts ───────────────────────────────────────────────────────────
    def _broadcast(data: dict):
        conn = connect()
        try:
            queued, refused = 0, []
            with conn.cursor() as cur:
                cur.execute("select id from campaigns where id = %s and tenant_id = %s",
                            (data["campaign_id"], data["tenant_id"]))
                if cur.fetchone() is None:
                    raise SendRefused("unknown_campaign", "that campaign is not in this workspace")
                for contact_id in data["recipients"]:
                    try:
                        send.enqueue_template(
                            cur, data["tenant_id"], contact_id=contact_id, name=data["template_name"],
                            language=data.get("language") or "en_US", components=data.get("components") or None,
                            idem_key="bc:%s:%s" % (data["campaign_id"], contact_id),
                            number_id=data.get("number_id"), campaign_id=data["campaign_id"], sender="broadcast")
                        queued += 1
                    except SendRefused as exc:
                        refused.append({"contact_id": str(contact_id), "reason": exc.reason})
                if refused:
                    cur.execute("update campaigns set failed = failed + %s where id = %s",
                                (len(refused), data["campaign_id"]))
                    send._close_campaign_if_done(cur, data["campaign_id"])
            conn.commit()
            return queued, refused
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.post("/internal/broadcast")
    async def broadcast(request: Request):
        if not authorised(request):
            return await refuse(request)
        data = await body_of(request)
        recipients = (data or {}).get("recipients")
        if (data is None or not _is_uuid(data.get("tenant_id")) or not _is_uuid(data.get("campaign_id"))
                or not isinstance(data.get("template_name"), str) or not data["template_name"].strip()
                or not isinstance(recipients, list) or not all(_is_uuid(r) for r in recipients)):
            return _bad("tenant_id, campaign_id, template_name and a list of recipient contact ids are required")
        if len(recipients) > MAX_RECIPIENTS:
            return _bad("at most %d recipients per call" % MAX_RECIPIENTS)
        try:
            queued, refused = await run_in_threadpool(_broadcast, data)
        except SendRefused as exc:
            return _refused(exc)
        return {"ok": True, "queued": queued, "refused": refused}

    # ── onboarding ───────────────────────────────────────────────────────────
    @app.post("/internal/onboarding/link")
    async def onboarding_link(request: Request):
        if not authorised(request):
            return await refuse(request)
        data = await body_of(request)
        if data is None or not _is_uuid(data.get("tenant_id")):
            return _bad("tenant_id is required")
        state = onboarding.make_state(internal_token, data["tenant_id"])
        return {"ok": True, "state": state, "path": "/onboarding?state=" + state + ("&mode=coexistence" if data.get("coexistence") else "")}

    @app.get("/onboarding")
    async def onboarding_page(state: str = "", mode: str = ""):
        try:
            onboarding.read_state(internal_token, state)
        except onboarding.OnboardingError as exc:
            return HTMLResponse("<p>%s</p>" % str(exc), status_code=400)
        if not (app_id and es_config_id):
            return HTMLResponse("<p>Connecting a number is not set up on this server yet.</p>", status_code=503)
        return HTMLResponse(onboarding.render_page(app_id=app_id, config_id=es_config_id, state=state,
                                                   version=graph.version, coexistence=(mode == "coexistence")))

    def _complete(data: dict, tenant_id: str):
        conn = connect()
        try:
            return onboarding.complete_signup(
                conn, graph=graph, vault=vault, tenant_id=tenant_id, app_id=app_id, app_secret=app_secret,
                code=str(data.get("code") or ""), waba_id=str(data.get("waba_id") or ""),
                phone_number_id=str(data.get("phone_number_id") or ""), display=str(data.get("display") or ""),
                coexistence=bool(data.get("coexistence")))
        finally:
            conn.close()

    @app.post("/onboarding/complete")
    async def onboarding_complete(request: Request):
        data = await body_of(request)
        if data is None:
            return _bad("expected JSON")
        try:
            tenant_id = onboarding.read_state(internal_token, str(data.get("state") or ""))
            if not (app_id and app_secret and vault is not None):
                return JSONResponse({"ok": False, "error": "not_configured",
                                     "message": "Connecting a number is not set up on this server yet."},
                                    status_code=503)
            result = await run_in_threadpool(_complete, data, tenant_id)
        except onboarding.OnboardingError as exc:
            status = {"bad_state": 401, "waba_taken": 409, "number_taken": 409}.get(exc.reason, 400)
            return JSONResponse({"ok": False, "error": exc.reason, "message": str(exc)}, status_code=status)
        except Exception:                                   # noqa: BLE001
            log.exception("onboarding failed")
            return JSONResponse({"ok": False, "error": "server_error", "message": "Please try again."},
                                status_code=500)
        return {"ok": result.registered and result.subscribed, "registered": result.registered,
                "subscribed": result.subscribed, "errors": result.errors,
                "message": "" if result.registered and result.subscribed else
                           "The number was linked but Meta did not finish setting it up; try again."}

    # ── the rest: templates, campaigns, segments, contacts, consent (ops.py) ─────────────────
    ops.mount(app, authorised=authorised, connect=connect, graph=graph, vault=vault, app_id=app_id, app_secrets=(app_secret,))

    # ── tracked links: public, opened from a customer's phone ───────────────────────────────
    def _click(token: str):
        conn = connect()
        try:
            with conn.cursor() as cur:
                target = campaigns.record_click(cur, token)
            conn.commit()
            return target
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.get("/r/{token}")
    async def redirect(token: str):
        if len(token) > 64:
            return PlainTextResponse("not found", status_code=404)
        target = await run_in_threadpool(_click, token)
        if not target or not target.startswith(("http://", "https://")):
            return PlainTextResponse("not found", status_code=404)
        return RedirectResponse(target, status_code=302)
