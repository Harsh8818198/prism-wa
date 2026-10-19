"""The public API: what a customer's own systems call, with an API key (spec WS14).

    POST /api/v1/template-messages     send an approved template to one number          (every plan)
    POST /api/v1/messages              reply in the 24-hour window                      (Pro and up)
    GET  /api/v1/messages/{id}         the state of a message we queued                 (Pro and up)
    POST /api/v1/contacts              create or update a contact, tags, attributes     (Pro and up)
    GET  /api/v1/contacts?phone=       look a contact up                                (Pro and up)
    GET  /api/v1/templates             list templates and their status                  (Pro and up)
    POST /api/v1/campaigns             create (and optionally launch) a campaign        (Pro and up)
    GET  /api/v1/campaigns/{id}        its report                                       (Pro and up)
    POST /campaign/t1/api/v2           send through a named API campaign, the way AiSensy's API does   (every plan)

Authorisation: `Authorization: Bearer pk_live_...` (or `X-Api-Key`). The key names the workspace and
carries scopes; nothing in a request can name another workspace. Every call is rate-limited per key.
Sending needs consent: a contact must already be opted in, or the request must say
`"consent_attested": true` (it is then written to the consent ledger with its source "api"); an
opted-out contact is never sent a business-initiated message.

Errors are `{"error": {"code": "...", "message": "..."}}` with an honest status: 400 bad request,
401 no/bad key, 403 not permitted by scope or plan, 404 not found, 409 refused by a rule (the 24-hour
window, opt-out, template not approved), 429 slow down.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from prism_wa import api_campaigns, api_keys, campaigns, consent, contacts, plans, segments, send, templates
from prism_wa.api_keys import ApiKeyError

WINDOW_SECONDS = 60.0


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


class RateLimiter:
    """At most `limit` calls per key in any rolling minute (per process)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock, self._hits = clock, defaultdict(deque)

    def check(self, key_id: str, limit: int) -> Optional[int]:
        """None if the call may go ahead, else seconds to wait."""
        now = self._clock()
        hits = self._hits[key_id]
        while hits and now - hits[0] >= WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(WINDOW_SECONDS - (now - hits[0])) + 1)
        hits.append(now)
        return None


def _error(status: int, code: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def _secret(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _digits(value) -> Optional[str]:
    return contacts.normalise_phone(value)


def mount(app: FastAPI, *, connect: Callable, limiter: Optional[RateLimiter] = None) -> None:
    limiter = limiter or RateLimiter()

    def serve(scope: str, fn: Callable, with_conn: bool = False):
        """Wrap a handler: authenticate, check scope/plan/rate, open a transaction, map module errors."""
        async def handler(request: Request):
            try:
                body = None
                if request.method in ("POST", "PUT"):
                    try:
                        body = await request.json()
                    except ValueError:
                        raise ApiProblem(400, "bad_json", "the body must be JSON") from None
                    if not isinstance(body, dict):
                        raise ApiProblem(400, "bad_json", "the body must be a JSON object")
                secret = _secret(request)
                result = await run_in_threadpool(_run, secret, scope, fn, with_conn, request, body, dict(request.path_params))
                status, payload = result if isinstance(result, tuple) else (200, result)
                return JSONResponse(payload, status_code=status)
            except ApiProblem as exc:
                return _error(exc.status, exc.code, exc.message, exc.headers)
        return handler

    def _run(secret, scope, fn, with_conn, request, body, path):
        conn = connect()
        try:
            with conn.cursor() as cur:
                key = api_keys.verify(cur, secret)
                if key is None:
                    raise ApiProblem(401, "invalid_key", "the API key is missing, wrong or revoked")
                if scope not in key.scopes:
                    raise ApiProblem(403, "scope", "this key may not do that (needs %s)" % scope)
                wait = limiter.check(key.id, key.rate_limit_per_minute)
                if wait is not None:
                    raise ApiProblem(429, "rate_limited", "too many requests; try again in %d seconds" % wait,
                                     {"Retry-After": str(wait)})
                conn.commit()                                 # the key's last-used stamp survives a refused request
            try:
                with conn.cursor() as cur:
                    plans.require(cur, key.tenant_id, api_keys.SCOPES[scope], "This part of the API")
                    out = fn(conn if with_conn else cur, key, request, body, **path)
                conn.commit()
                return out
            except Exception:
                conn.rollback()
                raise
        except plans.PlanLimit as exc:
            raise ApiProblem(403, exc.reason, str(exc)) from exc
        except send.SendRefused as exc:
            raise ApiProblem(409, exc.reason, str(exc)) from exc
        except campaigns.CampaignError as exc:
            raise ApiProblem(409, exc.reason, str(exc)) from exc
        except templates.TemplateInvalid as exc:
            raise ApiProblem(400, "template_invalid", str(exc)) from exc
        except (segments.FilterError, ApiKeyError, ValueError) as exc:
            raise ApiProblem(400, getattr(exc, "reason", "bad_request"), str(exc)) from exc
        finally:
            conn.close()

    # ── helpers shared by the routes ─────────────────────────────────────────
    def contact_for(cur, key, body, *, create: bool = True) -> tuple:
        phone = _digits(body.get("to") or body.get("phone"))
        if not phone:
            raise ApiProblem(400, "bad_phone", "`to` must be a phone number with country code, e.g. 919876543210")
        cur.execute("select id, opt_out, opted_in from contacts where tenant_id = %s and wa_norm_phone(phone) = %s", (key.tenant_id, phone))
        row = cur.fetchone()
        if row is None:
            if not create:
                raise ApiProblem(404, "not_found", "no contact with that number")
            cur.execute("select contact_id from wa_resolve_contact(%s, %s, null, null, null, %s, 'api')",
                        (key.tenant_id, phone, (body.get("name") or None)))
            cid = cur.fetchone()[0]
            return cid, False, False
        return row[0], bool(row[1]), bool(row[2])

    def need_consent(cur, key, body, contact_id, opted_out, opted_in) -> None:
        if opted_out:
            raise ApiProblem(409, "opted_out", "this person has opted out of business-initiated messages")
        if opted_in:
            return
        if body.get("consent_attested") is True:
            consent.record(cur, key.tenant_id, contact_id, "opt_in", "api", "attested in the API request by key %s" % key.name,
                           by_person=False)
            return
        raise ApiProblem(409, "no_consent", "this contact has not opted in; send `consent_attested: true` if you hold their consent")

    def idem(request, body, key) -> str:
        given = request.headers.get("idempotency-key") or (body or {}).get("idempotency_key")
        return "api:%s:%s" % (key.id, given) if given else "api:%s:%s" % (key.id, uuid.uuid4().hex)

    # ── send a template ──────────────────────────────────────────────────────
    def template_message(cur, key, request, body):
        template = (body.get("template") or "").strip()
        if not template:
            raise ApiProblem(400, "template_required", "`template` is required")
        contact_id, opted_out, opted_in = contact_for(cur, key, body)
        need_consent(cur, key, body, contact_id, opted_out, opted_in)
        components = []
        header = body.get("header")
        if isinstance(header, dict) and header.get("type") in ("image", "video", "document") and str(header.get("link") or "").startswith(("http://", "https://")):
            components.append({"type": "header", "parameters": [{"type": header["type"], header["type"]: {"link": header["link"]}}]})
        params = body.get("params") or []
        if not isinstance(params, list):
            raise ApiProblem(400, "bad_params", "`params` must be a list of values")
        if params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]})
        for i, value in enumerate(body.get("url_params") or []):
            components.append({"type": "button", "sub_type": "url", "index": str(i), "parameters": [{"type": "text", "text": str(value)}]})
        outbox_id = send.enqueue_template(cur, key.tenant_id, contact_id=contact_id, name=template,
                                          language=body.get("language") or "en_US", components=components or None,
                                          idem_key=idem(request, body, key), sender="api")
        return 202, {"id": str(outbox_id), "status": "queued"}

    app.post("/api/v1/template-messages")(serve("template_messages:send", template_message))

    # ── a reply inside the window ────────────────────────────────────────────
    def message(cur, key, request, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiProblem(400, "text_required", "`text` is required")
        contact_id, opted_out, _ = contact_for(cur, key, body, create=False)
        outbox_id = send.enqueue_text(cur, key.tenant_id, contact_id=contact_id, text=text,
                                      idem_key=idem(request, body, key), sender="api")
        return 202, {"id": str(outbox_id), "status": "queued"}

    app.post("/api/v1/messages")(serve("messages:send", message))

    def message_status(cur, key, request, body, message_id: str):
        try:
            uuid.UUID(message_id)
        except ValueError:
            raise ApiProblem(404, "not_found", "no such message") from None
        cur.execute("select o.id, o.state, o.error_class, m.status, m.wamid, m.error from outbox o"
                    " left join messages m on m.id = o.message_id where o.id = %s and o.tenant_id = %s", (message_id, key.tenant_id))
        row = cur.fetchone()
        if row is None:
            raise ApiProblem(404, "not_found", "no such message")
        return {"id": str(row[0]), "state": row[1], "status": row[3] or row[1], "error_class": row[2], "wamid": row[4], "error": row[5]}

    app.get("/api/v1/messages/{message_id}")(serve("messages:read", message_status))

    # ── contacts ─────────────────────────────────────────────────────────────
    def upsert_contact(conn, key, request, body):
        phone = _digits(body.get("phone") or body.get("to"))
        if not phone:
            raise ApiProblem(400, "bad_phone", "`phone` must be a phone number with country code")
        with conn.cursor() as cur:
            cur.execute("select contact_id, created from wa_resolve_contact(%s, %s, null, null, null, %s, 'api')",
                        (key.tenant_id, phone, body.get("name") or None))
            contact_id, created = cur.fetchone()
            tags = body.get("tags")
            if tags is not None:
                if not (isinstance(tags, list) and all(isinstance(t, str) and t.strip() for t in tags)):
                    raise ApiProblem(400, "bad_tags", "`tags` must be a list of words")
                cur.execute("select count(*) from (select distinct t from contacts, unnest(tags) t where tenant_id = %s) x", (key.tenant_id,))
                known = cur.fetchone()[0]
                cur.execute("select tags from contacts where id = %s", (contact_id,))
                mine = set(cur.fetchone()[0] or [])
                cur.execute("select distinct t from contacts, unnest(tags) t where tenant_id = %s", (key.tenant_id,))
                existing = {r[0] for r in cur.fetchall()}
                fresh = {t.strip() for t in tags} - existing
                plans.check_count(cur, key.tenant_id, "tags", known, len(fresh), "tags")
                cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t) where id = %s",
                            ([t.strip() for t in tags], contact_id))
            for attr, value in (body.get("attributes") or {}).items():
                contacts.define_attribute(cur, key.tenant_id, attr, "string")
                contacts.set_attribute(cur, key.tenant_id, contact_id, attr, value)
            cons = body.get("consent")
            if isinstance(cons, dict) and cons.get("opt_in") is True:
                consent.record(cur, key.tenant_id, contact_id, "opt_in", "api", str(cons.get("evidence") or "set via the API")[:500], by_person=False)
            if isinstance(cons, dict) and cons.get("opt_out") is True:
                consent.record(cur, key.tenant_id, contact_id, "opt_out", "api", str(cons.get("evidence") or "set via the API")[:500])
        return (201 if created else 200), {"id": str(contact_id), "created": bool(created), "phone": phone}

    app.post("/api/v1/contacts")(serve("contacts:write", upsert_contact, with_conn=True))

    def read_contact(cur, key, request, body):
        phone = _digits(request.query_params.get("phone"))
        if not phone:
            raise ApiProblem(400, "bad_phone", "`phone` is required, with country code")
        cur.execute("select id, name, phone, email, tags, opted_in, opt_out, created_at from contacts"
                    " where tenant_id = %s and wa_norm_phone(phone) = %s", (key.tenant_id, phone))
        row = cur.fetchone()
        if row is None:
            raise ApiProblem(404, "not_found", "no contact with that number")
        cur.execute("select key, value #>> '{}' from contact_attributes where contact_id = %s", (row[0],))
        return {"id": str(row[0]), "name": row[1], "phone": row[2], "email": row[3], "tags": row[4] or [],
                "opted_in": row[5], "opted_out": row[6], "created_at": row[7].isoformat(), "attributes": dict(cur.fetchall())}

    app.get("/api/v1/contacts")(serve("contacts:read", read_contact))

    # ── templates and campaigns ──────────────────────────────────────────────
    def list_templates(cur, key, request, body):
        cur.execute("select name, language, category, status, quality from templates where tenant_id = %s order by name, language", (key.tenant_id,))
        return {"templates": [dict(zip(("name", "language", "category", "status", "quality"), r)) for r in cur.fetchall()]}

    app.get("/api/v1/templates")(serve("templates:read", list_templates))

    def create_campaign(cur, key, request, body):
        for field in ("name", "template_name", "audience"):
            if not body.get(field):
                raise ApiProblem(400, "missing_field", "`%s` is required" % field)
        cid = campaigns.create_campaign(cur, key.tenant_id, name=body["name"], template_name=body["template_name"],
                                        language=body.get("language") or "en_US", audience=body["audience"],
                                        variables=body.get("variables"), schedule_at=body.get("schedule_at"),
                                        timezone_name=body.get("timezone"), retry=body.get("retry"))
        out = {"id": str(cid), "status": "draft"}
        if body.get("launch") is True:
            result = campaigns.launch(cur, key.tenant_id, cid)
            out.update(status=result["status"], queued=result.get("queued"), recipients=result.get("recipients"))
        return 201, out

    app.post("/api/v1/campaigns")(serve("campaigns:write", create_campaign))

    def campaign_report(cur, key, request, body, campaign_id: str):
        try:
            uuid.UUID(campaign_id)
        except ValueError:
            raise ApiProblem(404, "not_found", "no such campaign") from None
        try:
            return campaigns.report(cur, key.tenant_id, campaign_id)
        except campaigns.CampaignError:
            raise ApiProblem(404, "not_found", "no such campaign") from None

    app.get("/api/v1/campaigns/{campaign_id}")(serve("campaigns:read", campaign_report))

    # ── an API campaign, in the shape other software already speaks ───────────
    def api_campaign_send(cur, key, request, body):
        try:
            out = api_campaigns.send_one(cur, key.tenant_id, key.name, body)
        except api_campaigns.ApiCampaignError as exc:
            raise ApiProblem(exc.status, exc.reason, str(exc)) from None
        return 200, out

    async def api_campaign_handler(request: Request):
        try:
            try:
                body = await request.json()
            except ValueError:
                raise ApiProblem(400, "bad_json", "the body must be JSON") from None
            if not isinstance(body, dict):
                raise ApiProblem(400, "bad_json", "the body must be a JSON object")
            secret = str(body.get("apiKey") or "").strip() or _secret(request)          # the key may ride in the body, as it does there
            result = await run_in_threadpool(_run, secret, "template_messages:send", api_campaign_send, False, request, body, {})
            status, payload = result if isinstance(result, tuple) else (200, result)
            return JSONResponse(payload, status_code=status)
        except ApiProblem as exc:
            return _error(exc.status, exc.code, exc.message, exc.headers)

    app.post("/campaign/t1/api/v2")(api_campaign_handler)
