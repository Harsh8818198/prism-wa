"""Chatbot flows, part 2: walking a flow (spec WS8). The shape and the rules are in flows.py.

A customer writes; if a flow's trigger fits (or they are already mid-flow) the engine takes them one or
more steps: it sends what the step says, then either carries straight on or STOPS and waits — for an
answer, or for a timer. The place it stopped is saved in `flow_runs`, so the next message (or the timer)
picks up exactly there, even if the worker restarted in between.

Rules the engine keeps:
  * Only when nobody else is answering. A conversation a person owns, or has asked for, is never touched,
    and a run already in progress is cancelled the moment a person steps in.
  * Only the published version. A run keeps walking the version it started on, whatever is published since.
  * One live run per conversation (the database enforces it). A keyword, ad or button trigger starts its flow
    even mid-way through another; everything else carries on the run the customer is in.
  * It cannot run away. At most MAX_STEPS steps per advance, and a flow with a no-wait loop cannot be published.
  * Sends are idempotent: every message has a key made from the run and its step counter.

The steps run against an "actions" object. LiveActions queues real messages through the send engine;
DryActions only records what would be sent, which is how the owner tests a flow in Prism without a phone.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from prism_wa import contacts, flows, inbox, jobs, media, plans, send
from prism_wa.jobs import utcnow

log = logging.getLogger("prism_wa.flows")

MAX_STEPS = 50
MAX_TURNS_IN_TEST = 30
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_]+(?:\.[A-Za-z0-9_]+)?)\s*\}\}")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d.%m.%Y", "%d/%m/%y")

_WAIT = object()           # the step stopped and is waiting
_STOP = object()           # the flow ended (state already set)
_STAY = object()           # a wait is still going: the answer was not the one wanted


class FlowRuntimeError(Exception):
    """A step could not be carried out (e.g. the 24-hour window closed). The run is marked failed."""


# ── talking to the outside world ─────────────────────────────────────────────

HTTP: Optional[Callable] = None        # tests (and the worker) may set a stand-in: (method, url, headers, body) -> (status, text)


def configure(http: Optional[Callable] = None) -> None:
    global HTTP
    HTTP = http


def _public_address(host: str) -> bool:
    """A web address a bot may call: a real public host, never this machine, a private network or a cloud's
    metadata service. Checked on the addresses the name resolves to right now."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def default_http(method: str, url: str, headers: dict, body: Optional[str]) -> tuple:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not _public_address(parts.hostname):
        raise FlowRuntimeError("that address cannot be called from a flow")
    req = urllib.request.Request(url, data=body.encode("utf-8") if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **{str(k): str(v) for k, v in (headers or {}).items()}})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:                      # noqa: S310  (https and a public host only)
            return resp.status, resp.read(100_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(100_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise FlowRuntimeError("the request failed: %s" % exc) from exc


# ── what a step can do ───────────────────────────────────────────────────────

class DryActions:
    """Records instead of sending. `out` is what the customer would have received."""

    def __init__(self, *, open_now: bool = True, http: Optional[Callable] = None, local: Optional[datetime] = None, holidays=()):
        self.out: list = []
        self.open_now = open_now
        self._http = http
        self.handoff = None
        self.agent = None
        self._local = local
        self.holidays = set(holidays or ())
        self.events: list = []

    def send_text(self, text):
        self.out.append({"kind": "text", "text": text})

    def send_buttons(self, text, options):
        self.out.append({"kind": "buttons", "text": text, "options": [{"id": o["id"], "title": o["title"]} for o in options]})

    def send_list(self, text, label, rows, sections=None):
        item = {"kind": "list", "text": text, "button": label,
                "rows": [{"id": r["id"], "title": r["title"], "description": r.get("description", "")} for r in rows]}
        if sections:
            item["sections"] = [{"title": s["title"], "rows": [r["id"] for r in s["rows"]]} for s in sections]
        self.out.append(item)

    def send_media(self, kind, asset_id, caption):
        self.out.append({"kind": "media", "media": kind, "text": caption or "", "asset_id": str(asset_id)})

    def send_location(self, latitude, longitude, name, address):
        self.out.append({"kind": "location", "latitude": latitude, "longitude": longitude, "name": name or "", "address": address or ""})

    def send_form(self, form_id, text, button):
        self.out.append({"kind": "form", "form_id": str(form_id), "text": text or "", "button": button or ""})

    def send_products(self, node, text):
        self.out.append({"kind": "products", "mode": node["mode"], "text": text or "", "retailer_id": node.get("retailer_id"), "retailer_ids": list(node.get("retailer_ids") or [])})

    def request_payment(self, empty_text):
        self.out.append({"kind": "payment_request", "text": empty_text or ""})

    def request_location(self, text):
        self.out.append({"kind": "location_request", "text": text})

    def local_now(self):
        return self._local or datetime.now(timezone.utc)

    def is_holiday(self, iso):
        return iso in self.holidays

    def capi(self, event, value, currency, ctx):
        self.events.append({"event": event, "value": value, "currency": currency})
        self.out.append({"kind": "note", "text": "would tell Meta about a %s (not sent in a test)" % event})

    def visit(self, nid, node):
        pass

    def send_template(self, name, language, mapping, ctx):
        self.out.append({"kind": "template", "name": name, "language": language})

    def set_attr(self, key, value):
        self.out.append({"kind": "note", "text": "saved %s = %s" % (key, value)})

    def add_tag(self, tag):
        self.out.append({"kind": "note", "text": "added the tag %s" % tag})

    def remove_tag(self, tag):
        self.out.append({"kind": "note", "text": "removed the tag %s" % tag})

    def request_human(self, reason):
        self.handoff = reason or "asked for a person"
        self.out.append({"kind": "note", "text": "handed to a person"})

    def start_agent(self, agent_id):
        self.agent = agent_id or "default"
        self.out.append({"kind": "note", "text": "handed to the AI agent"})

    def http(self, method, url, headers, body):
        if self._http is None:
            self.out.append({"kind": "note", "text": "would call %s %s (not called in a test)" % (method, url)})
            return 200, "{}"
        return self._http(method, url, headers, body)

    def in_hours(self):
        return self.open_now


class LiveActions:
    """Queues real messages and makes real changes, inside the caller's transaction."""

    def __init__(self, cur, tenant_id, *, contact_id, conversation_id, run_id, run, now, http=None):
        self.cur, self.tenant_id, self.contact_id = cur, tenant_id, contact_id
        self.conversation_id, self.run_id, self.run, self.now = conversation_id, run_id, run, now
        self._http = http or HTTP or default_http

    def _key(self) -> str:
        seq = int(self.run["vars"].get("_seq", 0)) + 1
        self.run["vars"]["_seq"] = seq
        return "flow:%s:%d" % (self.run_id, seq)

    def _send(self, fn, **kw):
        try:
            fn(self.cur, self.tenant_id, contact_id=self.contact_id, idem_key=self._key(), now=self.now, sender="bot", **kw)
        except send.SendRefused as why:
            raise FlowRuntimeError("the message could not be sent: %s" % why) from why

    def send_text(self, text):
        self._send(send.enqueue_text, text=text)

    def send_buttons(self, text, options):
        interactive = {"type": "button", "body": {"text": text},
                       "action": {"buttons": [{"type": "reply", "reply": {"id": o["id"], "title": o["title"]}} for o in options]}}
        self._send(send.enqueue_interactive, interactive=interactive, preview=text)

    def send_list(self, text, label, rows, sections=None):
        def row(r):
            return {k: v for k, v in {"id": r["id"], "title": r["title"], "description": r.get("description")}.items() if v}
        groups = [{"title": s["title"], "rows": [row(r) for r in s["rows"]]} for s in sections] if sections else [{"title": "Options", "rows": [row(r) for r in rows]}]
        interactive = {"type": "list", "body": {"text": text}, "action": {"button": label, "sections": groups}}
        self._send(send.enqueue_interactive, interactive=interactive, preview=text)

    def send_media(self, kind, asset_id, caption):
        try:
            asset = media.get(self.cur, self.tenant_id, asset_id)
        except media.MediaError as why:
            raise FlowRuntimeError("the file could not be sent: %s" % why) from why
        self._send(send.enqueue_media, kind=asset["kind"], asset_id=asset["id"], caption=caption or "", filename=asset["filename"])

    def send_location(self, latitude, longitude, name, address):
        self._send(send.enqueue_location, latitude=latitude, longitude=longitude, name=name or "", address=address or "")

    def request_location(self, text):
        interactive = {"type": "location_request_message", "body": {"text": text}, "action": {"name": "send_location"}}
        self._send(send.enqueue_interactive, interactive=interactive, preview=text)

    def send_form(self, form_id, text, button):
        from prism_wa import forms
        try:
            form = forms._row(self.cur, self.tenant_id, form_id)
            interactive = forms.interactive_for(form, self.contact_id, body=text, button=button)
        except forms.FormError as why:
            raise FlowRuntimeError("the form could not be sent: %s" % why) from why
        self._send(send.enqueue_interactive, interactive=interactive, preview="[form] %s" % form["name"])

    def send_products(self, node, text):
        from prism_wa import commerce
        try:
            if node["mode"] == "product":
                self._send(commerce.send_product, retailer_id=node["retailer_id"], body=text)
            elif node["mode"] == "products":
                self._send(commerce.send_products, sections=[{"title": (node.get("title") or "Products")[:24], "retailer_ids": list(node["retailer_ids"])}], body=text)
            else:
                self._send(commerce.send_catalog, body=text, thumbnail_retailer_id=node.get("thumbnail") or "")
        except commerce.CommerceError as why:
            raise FlowRuntimeError("the products could not be sent: %s" % why) from why

    def request_payment(self, empty_text):
        """Ask the customer to pay the cart they just sent (their newest unpaid order). With no cart, say so and carry on."""
        from prism_wa import commerce, payments
        self.cur.execute("select id from orders where tenant_id = %s and contact_id = %s and status in ('draft', 'failed', 'awaiting_payment') order by created_at desc limit 1",
                         (self.tenant_id, self.contact_id))
        row = self.cur.fetchone()
        if row is None:
            self.send_text(empty_text or "I could not find your cart. Please add what you want and send it again.")
            return
        try:
            payments.request_payment(self.cur, self.tenant_id, payments.VAULT, order_id=row[0], now=self.now)
        except commerce.CommerceError as why:
            raise FlowRuntimeError("the payment could not be asked for: %s" % why) from why

    def _hours_settings(self) -> dict:
        return inbox.settings(self.cur, self.tenant_id)

    def local_now(self):
        tz = ((self._hours_settings().get("business_hours") or {}).get("tz")) or "Asia/Kolkata"
        try:
            return self.now.astimezone(ZoneInfo(tz))
        except Exception:                                              # noqa: BLE001  (an unknown zone name: use India's)
            return self.now.astimezone(ZoneInfo("Asia/Kolkata"))

    def is_holiday(self, iso):
        return iso in {str(h.get("date") if isinstance(h, dict) else h) for h in (self._hours_settings().get("holidays") or [])}

    def capi(self, event, value, currency, ctx):
        from prism_wa import capi as _capi
        try:
            _capi.send_for_contact(self.cur, self.tenant_id, self.contact_id, event=event, value=value, currency=currency, now=self.now)
        except _capi.CapiError as why:
            log.info("flow could not tell Meta about %s: %s", event, why)

    def visit(self, nid, node):
        if self.run.get("flow_id") is None:
            return
        self.cur.execute("insert into flow_run_steps (tenant_id, flow_id, run_id, version, node, kind) values (%s, %s, %s, %s, %s, %s)",
                         (self.tenant_id, self.run["flow_id"], self.run_id, int(self.run.get("version") or 0), nid, node.get("type") or ""))

    def send_template(self, name, language, mapping, ctx):
        from prism_wa import campaigns
        self.cur.execute("select t.components from templates t where t.tenant_id = %s and t.name = %s and t.language = %s limit 1",
                         (self.tenant_id, name, language))
        row = self.cur.fetchone()
        components = None
        if row and mapping:
            contact = {"name": ctx.name, "phone": ctx.phone}
            used = campaigns._template_variables(row[0])
            params = []
            for v in sorted(set(used["body"]) | set(used["header"])):
                spec = mapping.get(str(v))
                if spec:
                    params.append((v, campaigns._value(spec, contact, ctx.attrs, lambda target: target)))
            if used["header"] and params:
                components = [{"type": "header", "parameters": [{"type": "text", "text": t} for v, t in params if v in used["header"]]}]
            if used["body"]:
                components = (components or []) + [{"type": "body", "parameters": [
                    {"type": "text", "text": t} for v, t in params if v in used["body"]]}]
        try:
            send.enqueue_template(self.cur, self.tenant_id, contact_id=self.contact_id, name=name, language=language or "en_US",
                                  components=components, idem_key=self._key(), now=self.now, sender="bot")
        except send.SendRefused as why:
            raise FlowRuntimeError("the template could not be sent: %s" % why) from why

    def set_attr(self, key, value):
        defs = contacts.attribute_defs(self.cur, self.tenant_id)
        try:
            if key not in defs:
                contacts.define_attribute(self.cur, self.tenant_id, key, "string")
                defs = contacts.attribute_defs(self.cur, self.tenant_id)
            contacts.set_attribute(self.cur, self.tenant_id, self.contact_id, key, value, defs)
        except (plans.PlanLimit, ValueError) as why:
            log.info("flow could not save %s on contact %s: %s", key, self.contact_id, why)

    def add_tag(self, tag):
        cur = self.cur
        cur.execute("select %s = any(coalesce(tags, '{}')) from contacts where id = %s", (tag, self.contact_id))
        if cur.fetchone()[0]:
            return
        cur.execute("select exists (select 1 from contacts c, unnest(c.tags) t where c.tenant_id = %s and t = %s)", (self.tenant_id, tag))
        if not cur.fetchone()[0]:                                      # a tag new to the workspace counts against the plan
            cur.execute("select count(distinct t) from contacts c, unnest(c.tags) t where c.tenant_id = %s", (self.tenant_id,))
            try:
                plans.check_count(cur, self.tenant_id, "tags", cur.fetchone()[0], 1, "tags")
            except plans.PlanLimit as why:
                log.info("flow could not add the tag %r: %s", tag, why)
                return
        cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t)"
                    " where id = %s", ([tag], self.contact_id))

    def remove_tag(self, tag):
        self.cur.execute("update contacts set tags = array_remove(coalesce(tags, '{}'), %s) where id = %s", (tag, self.contact_id))

    def request_human(self, reason):
        inbox.request_human(self.cur, self.tenant_id, self.conversation_id, reason or "the bot handed over", now=self.now)

    def start_agent(self, agent_id):
        self.cur.execute("select id from ai_agents where tenant_id = %s and enabled and (id = %s or (%s::uuid is null and is_default))"
                         " order by (id = %s) desc nulls last limit 1", (self.tenant_id, agent_id, agent_id, agent_id))
        row = self.cur.fetchone()
        if row is None:
            self.request_human("no AI agent is switched on")
            return
        self.cur.execute("update conversations set ai_agent_id = %s, ai_replies = 0 where id = %s", (row[0], self.conversation_id))

    def http(self, method, url, headers, body):
        return self._http(method, url, headers, body)

    def in_hours(self):
        return inbox.in_hours(inbox.settings(self.cur, self.tenant_id)["business_hours"], self.now)


class Context:
    """What the steps can read about the customer."""

    def __init__(self, *, name="", phone="", tags=(), attrs=None, message="", payload_id="", msg_type="text", location=None):
        self.name, self.phone = name or "", phone or ""
        self.tags = list(tags or [])
        self.attrs = dict(attrs or {})
        self.message, self.payload_id, self.msg_type = message or "", payload_id or "", msg_type or "text"
        self.location = dict(location or {})

    @property
    def first_name(self):
        return (self.name.split() or [""])[0]

    @property
    def last_name(self):
        parts = self.name.split()
        return parts[-1] if len(parts) > 1 else ""


def interp(text: str, ctx: Context, run: dict) -> str:
    """{{name}} {{first_name}} {{last_name}} {{mobile}} {{message}} {{attr.City}} {{var.order}}."""
    def sub(m):
        token = m.group(1)
        simple = {"name": ctx.name, "first_name": ctx.first_name, "last_name": ctx.last_name, "mobile": ctx.phone, "message": ctx.message}
        if token in simple:
            return simple[token] or ("there" if token in ("name", "first_name") else "")
        if token.startswith("attr."):
            return str(ctx.attrs.get(token[5:], "") or "")
        if token.startswith("var."):
            return str(run["vars"].get(token[4:], "") or "")
        return ""
    return VAR_RE.sub(sub, text or "")


# ── answers ──────────────────────────────────────────────────────────────────

def validate_answer(node: dict, text: str):
    """(ok, value) for an `ask` step."""
    text = (text or "").strip()
    kind = node.get("validator", "text")
    if not text:
        return False, None
    if kind == "text":
        return True, text[:500]
    if kind == "number":
        try:
            n = float(text.replace(",", "").replace(" ", ""))
        except ValueError:
            return False, None
        return True, str(int(n)) if n == int(n) else str(n)
    if kind == "email":
        return (True, text.lower()) if EMAIL_RE.match(text) else (False, None)
    if kind == "phone":
        digits = contacts.normalise_phone(text)
        return (True, digits) if digits else (False, None)
    if kind == "date":
        for fmt in DATE_FORMATS:
            try:
                return True, datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return False, None
    if kind == "options":
        for opt in node.get("options") or []:
            if str(opt).strip().lower() == text.lower():
                return True, str(opt)
        return False, None
    if kind == "regex":
        try:
            return (True, text[:500]) if re.search(node.get("pattern") or "", text[:500]) else (False, None)
        except re.error:
            return False, None
    return False, None


def _compare(op: str, have, want) -> bool:
    have_s, want_s = str(have if have is not None else "").strip(), str(want if want is not None else "").strip()
    if op == "exists":
        return bool(have_s)
    if op == "not_exists":
        return not have_s
    if op == "eq":
        return have_s.lower() == want_s.lower()
    if op == "ne":
        return have_s.lower() != want_s.lower()
    if op == "contains":
        return want_s.lower() in have_s.lower()
    if op == "starts_with":
        return have_s.lower().startswith(want_s.lower())
    if op in ("gt", "lt"):
        try:
            a, b = float(have_s), float(want_s)
        except ValueError:
            return False
        return a > b if op == "gt" else a < b
    if op == "regex":
        try:
            return bool(re.search(want_s, have_s[:500]))
        except re.error:
            return False
    return False


def _calendar_holds(field: str, op: str, want, act) -> bool:
    """A condition on today: its weekday, its date, the time now (in the workspace's time zone), or whether it is a holiday."""
    local = act.local_now()
    day = local.date().isoformat()
    if field == "holiday":
        return act.is_holiday(day) if op == "yes" else (not act.is_holiday(day))
    if field == "weekday":
        names = [w.strip().lower() for w in str(want or "").split(",") if w.strip()]
        today = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[local.weekday()]
        return (today not in names) if op == "ne" else (today in names)
    if field == "date":
        parts = [day if p.strip() == "today" else p.strip() for p in str(want or "").split("..")]
        if op == "between":
            return len(parts) == 2 and parts[0] <= day <= parts[1]
        return {"eq": day == parts[0], "gt": day > parts[0], "lt": day < parts[0]}.get(op, False)
    now_t = local.strftime("%H:%M")
    parts = [p.strip() for p in str(want or "").split("..")]
    if op == "between":
        return len(parts) == 2 and parts[0] <= now_t < parts[1]
    return now_t > parts[0] if op == "gt" else now_t < parts[0]


def _condition_holds(rule: dict, ctx: Context, run: dict, act) -> bool:
    field, op, want = rule["field"], rule["op"], rule.get("value")
    if field in ("weekday", "date", "time", "holiday"):
        return _calendar_holds(field, op, want, act)
    if field == "hours":
        return act.in_hours() if op == "open" else (not act.in_hours())
    if field == "tag":
        have = {t.lower() for t in ctx.tags}
        if op == "exists":
            return bool(have)
        if op == "not_exists":
            return not have
        present = str(want or "").strip().lower() in have
        return present if op in ("eq", "contains", "starts_with") else (not present)
    if field == "message":
        have = ctx.message
    elif field.startswith("attr:"):
        have = ctx.attrs.get(field[5:])
    else:
        have = run["vars"].get(field[4:])
    return _compare(op, have, want)


# ── the steps ────────────────────────────────────────────────────────────────

def _deadline(node: dict, now: datetime) -> Optional[datetime]:
    t = node.get("timeout_minutes")
    return now + timedelta(minutes=t) if t else None


def _prompt(node: dict, ctx: Context, run: dict, act) -> None:
    """Say what a waiting step says (again, on a retry)."""
    text = interp(node.get("text", ""), ctx, run)
    if node["type"] == "buttons":
        act.send_buttons(text, node["options"])
    elif node["type"] == "list":
        act.send_list(text, node["button_label"], flows.list_rows(node), node.get("sections"))
    elif node["type"] == "location_request":
        act.request_location(text)
    else:
        act.send_text(text)


def _run_step(nid: str, node: dict, run: dict, ctx: Context, act, now: datetime):
    kind = node["type"]
    if kind == "send_text":
        act.send_text(interp(node["text"], ctx, run))
        return node.get("next")
    if kind in ("buttons", "list", "ask", "location_request"):
        _prompt(node, ctx, run, act)
        run["waiting"] = {"kind": kind, "node": nid, "tries": 0}
        run["deadline"] = _deadline(node, now)
        return _WAIT
    if kind == "media":
        act.send_media(node["kind"], node["asset_id"], interp(node.get("caption") or "", ctx, run))
        return node.get("next")
    if kind == "send_location":
        act.send_location(node["latitude"], node["longitude"], interp(node.get("name") or "", ctx, run), interp(node.get("address") or "", ctx, run))
        return node.get("next")
    if kind == "send_form":
        act.send_form(node["form_id"], interp(node.get("text") or "", ctx, run), node.get("button") or "")
        return node.get("next")
    if kind == "send_products":
        act.send_products(node, interp(node.get("text") or "", ctx, run))
        return node.get("next")
    if kind == "request_payment":
        act.request_payment(interp(node.get("empty_text") or "", ctx, run))
        return node.get("next")
    if kind == "webhook":
        _api_request({**node, "method": node.get("method", "POST"), "ok_next": node.get("next"), "error_next": node.get("next")}, run, ctx, act)
        return node.get("next")
    if kind == "capi_event":
        value = interp(str(node.get("value", "")), ctx, run) if node.get("value") is not None else None
        act.capi(node["event"], value, node.get("currency") or "INR", ctx)
        return node.get("next")
    if kind == "delay":
        delta = timedelta(minutes=node["minutes"]) if node.get("minutes") else timedelta(seconds=node["seconds"])
        run["waiting"] = {"kind": "delay", "node": nid}
        run["deadline"] = now + delta
        return _WAIT
    if kind == "condition":
        for rule in node["rules"]:
            if _condition_holds(rule, ctx, run, act):
                return rule.get("next")
        return node.get("else")
    if kind == "set_attribute":
        value = interp(node["value"], ctx, run)
        act.set_attr(node["key"], value)
        ctx.attrs[node["key"]] = value
        return node.get("next")
    if kind == "add_tag":
        act.add_tag(node["tag"].strip())
        if node["tag"].strip() not in ctx.tags:
            ctx.tags.append(node["tag"].strip())
        return node.get("next")
    if kind == "remove_tag":
        act.remove_tag(node["tag"].strip())
        ctx.tags = [t for t in ctx.tags if t != node["tag"].strip()]
        return node.get("next")
    if kind == "template":
        act.send_template(node["name"].strip(), node.get("language") or "en_US", node.get("variables") or {}, ctx)
        return node.get("next")
    if kind == "handoff":
        if node.get("text"):
            act.send_text(interp(node["text"], ctx, run))
        act.request_human(node.get("reason") or "the bot handed over")
        run["state"], run["handoff"] = "done", True
        return _STOP
    if kind == "agent":
        act.start_agent(node.get("agent_id"))
        run["state"], run["agent"] = "done", True
        return _STOP
    if kind == "jump_flow":
        run["state"], run["jump"] = "done", node["flow_id"]
        return _STOP
    if kind == "api_request":
        return _api_request(node, run, ctx, act)
    run["state"] = "done"                                              # "end"
    return _STOP


def _json_path(data, path: str):
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


def _api_request(node: dict, run: dict, ctx: Context, act):
    url = interp(node["url"], ctx, run)
    body = interp(node["body"], ctx, run) if node.get("body") else None
    headers = {k: interp(str(v), ctx, run) for k, v in (node.get("headers") or {}).items()}
    try:
        status, text = act.http(node.get("method", "GET"), url, headers, body)
    except FlowRuntimeError as why:
        log.info("flow api_request failed: %s", why)
        run["vars"]["_api_error"] = str(why)[:200]
        return node.get("error_next") or node.get("next")
    ok = 200 <= int(status) < 300
    run["vars"]["_api_status"] = int(status)
    if ok and node.get("save"):
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        for var, path in node["save"].items():
            found = _json_path(data, str(path))
            if found is None:
                continue
            if re.match(r"^attr:[A-Za-z][A-Za-z0-9_]{0,39}$", str(var)):          # "attr:City" keeps it on the contact
                act.set_attr(str(var)[5:], str(found)[:500])
                ctx.attrs[str(var)[5:]] = str(found)[:500]
            elif re.match(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", str(var)):
                run["vars"][var] = str(found)[:500]
    return (node.get("ok_next") or node.get("next")) if ok else (node.get("error_next") or node.get("next"))


def try_request(node: dict, *, name: str = "Test customer", attrs: Optional[dict] = None, values: Optional[dict] = None,
                message: str = "", http: Optional[Callable] = None) -> dict:
    """Make the call an API step makes, once, so its owner can see the answer and what would be kept from it.
    `values` fills {{var.x}}; the address is held to the same rules as in a live flow (https, a public host)."""
    ctx = Context(name=name, phone="919800000000", attrs=attrs, message=message)
    run = {"vars": dict(values or {})}
    url = interp(str(node.get("url") or ""), ctx, run)
    body = interp(node["body"], ctx, run) if node.get("body") else None
    headers = {k: interp(str(v), ctx, run) for k, v in (node.get("headers") or {}).items()}
    method = node.get("method", "GET")
    try:
        status, text = (http or default_http)(method, url, headers, body)
    except FlowRuntimeError as why:
        return {"ok": False, "error": str(why)[:300], "url": url}
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = None
    kept = {}
    if data is not None:
        for var, path in (node.get("save") or {}).items():
            found = _json_path(data, str(path))
            kept[str(var)] = None if found is None else str(found)[:500]
    return {"ok": 200 <= int(status) < 300, "status": int(status), "url": url, "body": (text or "")[:2000], "json": data is not None, "kept": kept}


def _resume(nodes: dict, run: dict, ctx: Context, act, event: dict, now: datetime):
    """The customer answered (or the timer ran out) while the run was waiting. -> the next node id, None to
    end, or _STAY when the wait carries on."""
    waiting = run["waiting"]
    node = nodes.get(waiting.get("node")) or {}
    kind = waiting.get("kind")
    if event.get("timeout"):
        return node.get("next") if kind == "delay" else node.get("timeout_next")
    if kind == "delay":
        return _STAY                                                    # chatter during a delay changes nothing
    if kind == "location_request":
        where = ctx.location if ctx.msg_type == "location" else {}
        if where.get("latitude") is not None and where.get("longitude") is not None:
            run["vars"].update(location_lat=str(where["latitude"]), location_lng=str(where["longitude"]),
                               location_name=where.get("name") or "", location_address=where.get("address") or "")
            return node.get("next")
        waiting["tries"] = int(waiting.get("tries", 0)) + 1
        if node.get("fallback_next"):
            return node["fallback_next"]
        if waiting["tries"] <= 1:
            _prompt(node, ctx, run, act)
            return _STAY
        return node.get("timeout_next")
    if kind in ("buttons", "list"):
        items = (node.get("options") if kind == "buttons" else flows.list_rows(node)) or []
        text = ctx.message.strip().lower()
        chosen = None
        for i, item in enumerate(items):
            if ctx.payload_id and ctx.payload_id == item["id"]:
                chosen = item
            elif text and (text == item["title"].strip().lower() or text == str(i + 1)):
                chosen = item
            if chosen:
                break
        if chosen:
            run["vars"]["_choice"] = chosen["id"]
            return chosen.get("next")
        waiting["tries"] = int(waiting.get("tries", 0)) + 1
        if node.get("fallback_next"):
            return node["fallback_next"]
        if waiting["tries"] <= 1:
            _prompt(node, ctx, run, act)                                # one more go
            return _STAY
        return node.get("timeout_next")
    if kind == "ask":
        ok, value = validate_answer(node, ctx.message) if ctx.msg_type in ("text", "button", "interactive") else (False, None)
        if ok:
            save = node.get("save_as")
            if save:
                where, key = save.split(":", 1)
                if where == "attr":
                    act.set_attr(key, value)
                    ctx.attrs[key] = value
                else:
                    run["vars"][key] = value
            run["vars"]["_answer"] = value
            return node.get("next")
        waiting["tries"] = int(waiting.get("tries", 0)) + 1
        if waiting["tries"] >= int(node.get("max_tries", 2)):
            return node.get("invalid_next")
        act.send_text(interp(node.get("retry_text") or "Sorry, I didn't get that. " + node.get("text", ""), ctx, run))
        return _STAY
    return None


def advance(graph: dict, run: dict, ctx: Context, act, *, event: Optional[dict] = None, now: Optional[datetime] = None) -> None:
    """Take a run as far as it can go: resume it with `event` if it is waiting, then run steps until one waits
    or the flow ends. Mutates `run` (state, node, vars, waiting, deadline)."""
    now = now or utcnow()
    nodes = graph["nodes"]
    if event is not None and run.get("waiting"):
        nxt = _resume(nodes, run, ctx, act, event, now)
        if nxt is _STAY:
            return
        run["waiting"], run["deadline"], run["state"], run["node"] = None, None, "running", nxt
    start = run["steps"]
    try:
        while run["state"] == "running":
            nid = run["node"]
            if nid is None:
                run["state"] = "done"
                break
            if run["steps"] - start >= MAX_STEPS:
                run["state"], run["last_error"] = "failed", "the flow took too many steps in a row"
                break
            node = nodes.get(nid)
            if not isinstance(node, dict):
                run["state"], run["last_error"] = "failed", "step \"%s\" does not exist" % nid
                break
            run["steps"] += 1
            act.visit(nid, node)
            outcome = _run_step(nid, node, run, ctx, act, now)
            if outcome is _WAIT:
                run["state"], run["node"] = "waiting", nid
                break
            if outcome is _STOP:
                run["node"] = nid
                break
            run["node"] = outcome
    except FlowRuntimeError as why:
        run["state"], run["last_error"] = "failed", str(why)[:300]


# ── testing without a phone ──────────────────────────────────────────────────

def test(graph: dict, trigger: Optional[dict], inputs: list, *, name: str = "Test customer", attrs: Optional[dict] = None,
         open_now: bool = True, http: Optional[Callable] = None, auto_advance: bool = True, local: Optional[datetime] = None, holidays=()) -> dict:
    """Walk a flow with pretend customer messages and say what the bot would have sent. Nothing is sent or saved."""
    issues = flows.validate(graph, trigger)
    if flows.errors(issues):
        return {"transcript": [], "state": "invalid", "issues": [i.as_dict() for i in issues]}
    act = DryActions(open_now=open_now, http=http, local=local, holidays=holidays)
    run = {"node": graph["entry"], "vars": {}, "waiting": None, "deadline": None, "state": "running", "steps": 0}
    ctx = Context(name=name, phone="919800000000", attrs=attrs)
    transcript: list = []
    taken = 0

    def flush():
        nonlocal taken
        transcript.extend({"from": "system" if item["kind"] == "note" else "bot", **item} for item in act.out[taken:])
        taken = len(act.out)

    first = True
    for raw in inputs[:MAX_TURNS_IN_TEST]:
        item = {"text": raw} if isinstance(raw, str) else dict(raw or {})
        ctx.message, ctx.payload_id = item.get("text", ""), item.get("payload_id", "")
        ctx.msg_type, ctx.location = ("location", dict(item["location"])) if item.get("location") else ("text", {})
        transcript.append({"from": "customer", "kind": "text", "text": ctx.message or ctx.payload_id})
        if first:
            first = False
            advance(graph, run, ctx, act)
        elif run["state"] == "waiting":
            advance(graph, run, ctx, act, event={"text": ctx.message})
        elif run["state"] in ("done", "failed"):
            flush()
            break
        flush()
        while auto_advance and run["state"] == "waiting" and run["waiting"].get("kind") == "delay":
            transcript.append({"from": "clock", "kind": "note", "text": "time passes"})
            advance(graph, run, ctx, act, event={"timeout": True})
            flush()
        if run["state"] in ("done", "failed"):
            break
    if not inputs:
        advance(graph, run, ctx, act)
        flush()
    return {"transcript": transcript, "state": run["state"], "waiting": (run.get("waiting") or {}).get("kind"),
            "node": run.get("node"), "error": run.get("last_error"), "handoff": act.handoff, "agent": act.agent,
            "issues": [i.as_dict() for i in issues]}


# ── live: starting, resuming, timers ─────────────────────────────────────────

def _context(cur, tenant_id, contact_id, canonical: dict) -> Context:
    cur.execute("select name, phone, tags from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    name, phone, tags = cur.fetchone()
    cur.execute("select key, value #>> '{}' from contact_attributes where contact_id = %s", (contact_id,))
    return Context(name=name, phone=phone, tags=tags or [], attrs=dict(cur.fetchall()), message=canonical.get("text") or "",
                   payload_id=canonical.get("payload_id") or "", msg_type=canonical.get("msg_type") or "text", location=canonical.get("location") or {})


def _published(cur, tenant_id) -> list:
    """The flows that are switched on, with the graph of their published version."""
    cur.execute("select f.id, f.name, f.trigger, f.version, v.graph from flows f"
                " join flow_versions v on v.flow_id = f.id and v.version = f.version"
                " where f.tenant_id = %s and f.enabled and f.version > 0 order by f.created_at", (tenant_id,))
    return [dict(zip(("id", "name", "trigger", "version", "graph"), r)) for r in cur.fetchall()]


def trigger_fits(trigger: dict, canonical: dict, *, first_message: bool) -> Optional[str]:
    """How a message fits a trigger: "explicit" (keyword, regex, ad, button), "first", "default", or None."""
    kind = (trigger or {}).get("type")
    text = (canonical.get("text") or "").strip()
    low = text.lower()
    if kind == "keyword":
        for word in trigger.get("keywords") or []:
            w = str(word).strip().lower()
            if not w:
                continue
            m = trigger.get("match")
            if (m == "exact" and low == w) or (m == "contains" and w in low) or (m == "starts_with" and low.startswith(w)):
                return "explicit"
        return None
    if kind == "regex":
        try:
            return "explicit" if text and re.search(trigger.get("pattern") or "", text[:500]) else None
        except re.error:
            return None
    if kind == "ad":
        source = str(((canonical.get("referral") or {}).get("source_id")) or "")
        return "explicit" if source and source in [str(i) for i in trigger.get("ad_ids") or []] else None
    if kind == "button":
        return "explicit" if canonical.get("payload_id") and canonical["payload_id"] == trigger.get("payload") else None
    if kind == "first_message":
        return "first" if first_message else None
    if kind == "default":
        return "default"
    return None


def _live_run(cur, conversation_id, *, lock: bool = False) -> Optional[dict]:
    cur.execute("select id, flow_id, version, state, node, vars, waiting, deadline, steps from flow_runs"
                " where conversation_id = %s and state in ('running', 'waiting')" + (" for update" if lock else ""), (conversation_id,))
    row = cur.fetchone()
    return dict(zip(("id", "flow_id", "version", "state", "node", "vars", "waiting", "deadline", "steps"), row)) if row else None


def _save(cur, run: dict) -> None:
    cur.execute("update flow_runs set state = %s, node = %s, vars = %s, waiting = %s, deadline = %s, steps = %s,"
                " last_error = %s, updated_at = now(), finished_at = case when %s in ('done', 'failed', 'cancelled') then now() end"
                " where id = %s",
                (run["state"], run["node"], Json(run["vars"]), Json(run["waiting"]) if run.get("waiting") else None, run.get("deadline"),
                 run["steps"], run.get("last_error"), run["state"], run["id"]))


def _arm_timer(cur, tenant_id, run: dict) -> None:
    if run["state"] == "waiting" and run.get("deadline"):
        jobs.enqueue(cur, tenant_id, "flow_timeout", {"run_id": str(run["id"]), "node": run["node"], "deadline": run["deadline"].isoformat()},
                     "flowto:%s:%s:%s" % (run["id"], run["node"], run["deadline"].isoformat()), run_at=run["deadline"])


def _owned_or_waiting_for_person(cur, conversation_id) -> bool:
    cur.execute("select status, bot_paused from conversations where id = %s", (conversation_id,))
    row = cur.fetchone()
    return bool(row) and (row[0] in inbox.OWNED or row[0] == "requesting" or bool(row[1]))


def cancel_for_conversation(cur, conversation_id) -> None:
    cur.execute("update flow_runs set state = 'cancelled', finished_at = now() where conversation_id = %s and state in ('running', 'waiting')",
                (conversation_id,))


def _drive(cur, tenant_id, run: dict, graph: dict, ctx: Context, *, contact_id, conversation_id, now, event=None) -> None:
    act = LiveActions(cur, tenant_id, contact_id=contact_id, conversation_id=conversation_id, run_id=run["id"], run=run, now=now)
    before = run.get("state")
    advance(graph, run, ctx, act, event=event, now=now)
    _save(cur, run)
    _arm_timer(cur, tenant_id, run)
    if run.get("state") == "done" and before != "done":
        from prism_wa import webhooks_out
        webhooks_out.emit(cur, tenant_id, "flow.response", lambda: {
            "flow_id": str(run.get("flow_id") or ""), "run_id": str(run["id"]), "contact_id": str(contact_id), "conversation_id": str(conversation_id),
            "answers": {k: v for k, v in (run.get("vars") or {}).items() if not str(k).startswith("_")}, "handed_to_person": bool(run.get("handoff"))})
    if run.get("jump"):
        target = run.pop("jump")
        _start_by_id(cur, tenant_id, target, conversation_id=conversation_id, contact_id=contact_id, ctx=ctx, now=now)


def _start(cur, tenant_id, flow: dict, *, conversation_id, contact_id, ctx: Context, now) -> None:
    cur.execute("insert into flow_runs (tenant_id, flow_id, conversation_id, version, node, vars) values (%s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, flow["id"], conversation_id, flow["version"], flow["graph"].get("entry"),
                 Json({"_last_message": ctx.message[:200]})))
    run = {"id": cur.fetchone()[0], "state": "running", "node": flow["graph"].get("entry"), "vars": {"_last_message": ctx.message[:200]},
           "waiting": None, "deadline": None, "steps": 0, "flow_id": flow["id"], "version": flow["version"]}
    _drive(cur, tenant_id, run, flow["graph"], ctx, contact_id=contact_id, conversation_id=conversation_id, now=now)


def _start_by_id(cur, tenant_id, flow_id, *, conversation_id, contact_id, ctx, now) -> None:
    for flow in _published(cur, tenant_id):
        if str(flow["id"]) == str(flow_id):
            _start(cur, tenant_id, flow, conversation_id=conversation_id, contact_id=contact_id, ctx=ctx, now=now)
            return


def on_inbound(cur, tenant_id, *, contact_id, conversation_id, canonical: dict, when: datetime, first_message: bool) -> bool:
    """A customer wrote. -> True when a flow took the message (so nothing else should answer it)."""
    if not plans.has(plans.plan_of(cur, tenant_id), "flows"):
        return False
    if _owned_or_waiting_for_person(cur, conversation_id):
        cancel_for_conversation(cur, conversation_id)
        return False
    live = _live_run(cur, conversation_id, lock=True)
    available = _published(cur, tenant_id)
    by_kind = {"explicit": None, "first": None, "default": None}
    for flow in available:
        fit = trigger_fits(flow["trigger"], canonical, first_message=first_message)
        if fit and by_kind[fit] is None:
            by_kind[fit] = flow
    ctx = _context(cur, tenant_id, contact_id, canonical)

    explicit = by_kind["explicit"]
    if explicit and (live is None or str(live["flow_id"]) != str(explicit["id"])):
        if live:
            cancel_for_conversation(cur, conversation_id)
        _start(cur, tenant_id, explicit, conversation_id=conversation_id, contact_id=contact_id, ctx=ctx, now=when)
        return True
    if live and live["state"] == "waiting":
        graph = _graph_of(cur, live)
        if graph is None:
            cancel_for_conversation(cur, conversation_id)
            return False
        ctx.message = canonical.get("text") or ""
        live["vars"] = live["vars"] or {}
        _drive(cur, tenant_id, live, graph, ctx, contact_id=contact_id, conversation_id=conversation_id, now=when, event={"text": ctx.message})
        return True
    if live:
        return True                                                    # mid-step (cannot happen between transactions)
    for kind in ("first", "default"):
        if by_kind[kind]:
            _start(cur, tenant_id, by_kind[kind], conversation_id=conversation_id, contact_id=contact_id, ctx=ctx, now=when)
            return True
    return False


def _graph_of(cur, run: dict) -> Optional[dict]:
    cur.execute("select graph from flow_versions where flow_id = %s and version = %s", (run["flow_id"], run["version"]))
    row = cur.fetchone()
    return row[0] if row else None


def handle_timeout_job(cur, tenant_id, payload: dict) -> None:
    """A wait ran out (an unanswered question, or a delay). Stale timers — the customer answered, a person took
    over, the flow was switched off — do nothing."""
    cur.execute("select id, flow_id, version, state, node, vars, waiting, deadline, steps, conversation_id from flow_runs where id = %s and tenant_id = %s for update",
                (payload.get("run_id"), tenant_id))
    row = cur.fetchone()
    if row is None:
        return
    run = dict(zip(("id", "flow_id", "version", "state", "node", "vars", "waiting", "deadline", "steps", "conversation_id"), row))
    try:
        armed = datetime.fromisoformat(str(payload.get("deadline")))
    except ValueError:
        return
    if run["state"] != "waiting" or run["node"] != payload.get("node") or not run["deadline"] \
            or abs((run["deadline"] - armed).total_seconds()) > 1:
        return
    if _owned_or_waiting_for_person(cur, run["conversation_id"]):
        cancel_for_conversation(cur, run["conversation_id"])
        return
    graph = _graph_of(cur, run)
    cur.execute("select contact_id from conversations where id = %s", (run["conversation_id"],))
    contact_id = cur.fetchone()[0]
    if graph is None:
        cancel_for_conversation(cur, run["conversation_id"])
        return
    ctx = _context(cur, tenant_id, contact_id, {"text": (run["vars"] or {}).get("_last_message", "")})
    _drive(cur, tenant_id, run, graph, ctx, contact_id=contact_id, conversation_id=run["conversation_id"], now=utcnow(), event={"timeout": True})


def runs(cur, tenant_id, flow_id, limit: int = 50) -> list:
    cur.execute("select r.id, r.state, r.node, r.steps, r.started_at, r.finished_at, r.last_error, c.name"
                " from flow_runs r join conversations v on v.id = r.conversation_id join contacts c on c.id = v.contact_id"
                " where r.flow_id = %s and r.tenant_id = %s order by r.started_at desc limit %s", (flow_id, tenant_id, limit))
    return [dict(zip(("id", "state", "node", "steps", "started_at", "finished_at", "last_error", "contact"), r)) for r in cur.fetchall()]
