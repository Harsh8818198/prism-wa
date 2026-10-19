"""Chatbot flows, part 1: what a flow IS — its shape, the rules it must meet, and saving it (spec WS8).

A flow is a graph the owner draws and a customer walks through by chatting:

    {"entry": "welcome",
     "nodes": {
        "welcome": {"type": "buttons", "text": "How can we help?",
                    "options": [{"id": "quote", "title": "Get a quote", "next": "ask_name"},
                                {"id": "visit", "title": "Book a site visit", "next": "ask_when"}]},
        "ask_name": {"type": "ask", "text": "What is your name?", "save_as": "attr:Name", "next": "thanks"},
        "thanks":   {"type": "send_text", "text": "Thanks {{first_name}}, we will call you.", "next": "done"},
        "done":     {"type": "handoff", "text": "A colleague will take it from here."}}}

and a trigger that says what starts it (a keyword, a first message, an ad, a button...).

Two copies exist. `flows.graph` is the DRAFT being edited; publishing checks it, freezes it as the next
`flow_versions` row and makes that the one customers walk. A flow runs only when it is switched on AND has
been published, so an unfinished draft never reaches a customer, and a bad publish can be rolled back.

This file holds the rules (validate), the saving (create / update / publish / rollback) and the plan
limits. The walking is in flow_engine.py.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from psycopg2.extras import Json

from prism_wa import plans

NODE_TYPES = ("send_text", "buttons", "list", "template", "ask", "condition", "set_attribute", "add_tag",
              "remove_tag", "delay", "handoff", "agent", "jump_flow", "api_request", "end",
              "media", "location_request", "send_location", "webhook", "capi_event", "send_form", "send_products", "request_payment")
# the ones that stop and wait for the customer (or the clock); everything else runs straight through
WAITING = ("buttons", "list", "ask", "delay", "location_request")
MEDIA_KINDS = ("image", "video", "audio", "document")
CAPI_EVENTS = ("Lead", "Purchase", "ViewContent", "CompleteRegistration", "Contact", "Schedule", "SubmitApplication", "AddToCart",
               "InitiateCheckout", "Subscribe", "StartTrial")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TRIGGER_TYPES = ("keyword", "regex", "first_message", "default", "ad", "button")
EXPLICIT_TRIGGERS = ("keyword", "regex", "ad", "button")          # these start a flow even mid-way through another
VALIDATORS = ("text", "number", "email", "phone", "date", "options", "regex")
CONDITION_FIELDS_RE = re.compile(r"^(message|tag|hours|weekday|date|time|holiday|attr:[A-Za-z][A-Za-z0-9_]{0,39}|var:[A-Za-z][A-Za-z0-9_]{0,39})$")
CONDITION_OPS = ("eq", "ne", "contains", "starts_with", "exists", "not_exists", "gt", "lt", "regex", "open", "closed", "in", "between", "yes", "no")
SAVE_AS_RE = re.compile(r"^(attr|var):[A-Za-z][A-Za-z0-9_]{0,39}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
OPTION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")

MAX_NODES = 100
MAX_TEXT = 1024
MAX_DELAY_MINUTES = 7 * 24 * 60
MAX_REGEX = 100                                                   # characters: a long pattern is how a regex turns hostile

# every key that points at another node
NEXT_KEYS = ("next", "else", "timeout_next", "fallback_next", "invalid_next", "ok_next", "error_next")


class FlowError(Exception):
    def __init__(self, reason: str, message: str = "", issues: Optional[list] = None):
        super().__init__(message or reason)
        self.reason = reason
        self.issues = issues or []


@dataclass
class Issue:
    severity: str                 # "error" blocks publishing; "warning" advises
    code: str
    message: str
    node: str = ""

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message, "node": self.node}


def errors(issues: list) -> list:
    return [i for i in issues if i.severity == "error"]


# ── the rules ────────────────────────────────────────────────────────────────

def _targets(node: dict) -> list:
    """Every node id this node can lead to."""
    out = [node[k] for k in NEXT_KEYS if isinstance(node.get(k), str) and node[k]]
    for key in ("options", "rows", "rules"):
        for item in node.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("next"), str) and item["next"]:
                out.append(item["next"])
    for section in node.get("sections") or []:
        for item in (section.get("rows") if isinstance(section, dict) else None) or []:
            if isinstance(item, dict) and isinstance(item.get("next"), str) and item["next"]:
                out.append(item["next"])
    return out


def list_rows(node: dict) -> list:
    """The rows of a list step, whether it is one list or several sections."""
    if node.get("sections"):
        return [r for s in node["sections"] if isinstance(s, dict) for r in (s.get("rows") or []) if isinstance(r, dict)]
    return [r for r in (node.get("rows") or []) if isinstance(r, dict)]


def _regex_problem(pattern) -> Optional[str]:
    if not isinstance(pattern, str) or not pattern:
        return "a pattern is needed"
    if len(pattern) > MAX_REGEX:
        return "the pattern is longer than %d characters" % MAX_REGEX
    try:
        re.compile(pattern)
    except re.error as exc:
        return "the pattern is not valid (%s)" % exc
    if re.search(r"\((?:[^()]*[+*][^()]*)\)[+*{]", pattern):
        return "nested repeats like (a+)+ can hang the bot; simplify the pattern"
    return None


def validate_trigger(trigger) -> list:
    out: list = []
    err = lambda code, msg: out.append(Issue("error", code, msg, "trigger"))        # noqa: E731
    if not isinstance(trigger, dict) or trigger.get("type") not in TRIGGER_TYPES:
        err("bad_trigger", "Choose what starts this flow: %s." % ", ".join(TRIGGER_TYPES))
        return out
    kind = trigger["type"]
    if kind == "keyword":
        words = trigger.get("keywords")
        if trigger.get("match") not in ("exact", "contains", "starts_with"):
            err("bad_trigger", "A keyword trigger must say how to match: exact, contains or starts_with.")
        if not (isinstance(words, list) and 1 <= len(words) <= 20 and all(isinstance(w, str) and 1 <= len(w.strip()) <= 60 for w in words)):
            err("bad_trigger", "Give 1 to 20 keywords, each up to 60 characters.")
    elif kind == "regex":
        problem = _regex_problem(trigger.get("pattern"))
        if problem:
            err("bad_trigger", "The regex trigger: %s." % problem)
    elif kind == "ad":
        ids = trigger.get("ad_ids")
        if not (isinstance(ids, list) and 1 <= len(ids) <= 20 and all(isinstance(i, str) and i.strip() for i in ids)):
            err("bad_trigger", "Give the ids of the ads that start this flow (up to 20).")
    elif kind == "button":
        if not (isinstance(trigger.get("payload"), str) and 1 <= len(trigger["payload"]) <= 256):
            err("bad_trigger", "Give the button payload that starts this flow.")
    return out


def validate(graph, trigger=None) -> list:
    """Every problem with a flow, as Issues. Errors block publishing; warnings are advice."""
    out: list = []

    def err(code, msg, node=""):
        out.append(Issue("error", code, msg, node))

    def warn(code, msg, node=""):
        out.append(Issue("warning", code, msg, node))

    if trigger is not None:
        out += validate_trigger(trigger)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), dict) or not graph["nodes"]:
        err("empty", "The flow has no steps yet.")
        return out
    nodes = graph["nodes"]
    if len(nodes) > MAX_NODES:
        err("too_big", "A flow can have at most %d steps." % MAX_NODES)
        return out
    entry = graph.get("entry")
    if entry not in nodes:
        err("no_entry", "Choose which step the flow starts with.")

    for nid, node in nodes.items():
        if not NODE_ID_RE.match(str(nid)):
            err("bad_id", "Step names may only use letters, numbers and underscores (up to 40).", str(nid))
            continue
        if not isinstance(node, dict) or node.get("type") not in NODE_TYPES:
            err("bad_type", "Step \"%s\" has an unknown type." % nid, nid)
            continue
        _node_rules(nid, node, err, warn)
        for target in _targets(node):
            if target not in nodes:
                err("dangling", "Step \"%s\" leads to \"%s\", which does not exist." % (nid, target), nid)

    if entry in nodes:
        seen, stack = set(), [entry]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in nodes or not isinstance(nodes[cur], dict):
                continue
            seen.add(cur)
            stack.extend(_targets(nodes[cur]))
        for nid in nodes:
            if nid not in seen:
                warn("unreachable", "Step \"%s\" can never be reached." % nid, str(nid))
        if _runs_forever(nodes):
            err("loop", "Some steps lead back to each other without ever waiting for the customer, so the bot would "
                        "answer itself forever. Put a question or a delay in the loop.")
    return out


def _node_rules(nid: str, node: dict, err, warn) -> None:
    kind = node["type"]
    text = node.get("text")

    def need_text(label="text", maximum=MAX_TEXT):
        if not (isinstance(text, str) and 1 <= len(text.strip()) <= maximum):
            err("bad_text", "Step \"%s\" needs its %s (1 to %d characters)." % (nid, label, maximum), nid)

    if kind == "send_text":
        need_text()
    elif kind == "buttons":
        need_text()
        opts = node.get("options")
        if not (isinstance(opts, list) and 1 <= len(opts) <= 3):
            err("bad_options", "Step \"%s\" needs 1 to 3 buttons." % nid, nid)
            return
        ids = set()
        for o in opts:
            o = o if isinstance(o, dict) else {}
            if not OPTION_ID_RE.match(str(o.get("id") or "")) or o["id"] in ids:
                err("bad_options", "Each button in \"%s\" needs its own short id." % nid, nid)
            ids.add(o.get("id"))
            if not (isinstance(o.get("title"), str) and 1 <= len(o["title"].strip()) <= 20):
                err("bad_options", "A button's text is 1 to 20 characters (step \"%s\")." % nid, nid)
        _timeout_rules(nid, node, err)
    elif kind == "list":
        need_text()
        if not (isinstance(node.get("button_label"), str) and 1 <= len(node["button_label"].strip()) <= 20):
            err("bad_options", "The list in \"%s\" needs a button label (up to 20 characters)." % nid, nid)
        sections = node.get("sections")
        if sections is not None:
            if not (isinstance(sections, list) and 1 <= len(sections) <= 10):
                err("bad_options", "A list in \"%s\" has 1 to 10 sections." % nid, nid)
                return
            for s in sections:
                s = s if isinstance(s, dict) else {}
                if not (isinstance(s.get("title"), str) and 1 <= len(s["title"].strip()) <= 24):
                    err("bad_options", "A section's title in \"%s\" is 1 to 24 characters." % nid, nid)
                if not (isinstance(s.get("rows"), list) and s["rows"]):
                    err("bad_options", "Every section in \"%s\" needs at least one row." % nid, nid)
                    return
        rows = list_rows(node)
        if not 1 <= len(rows) <= 10:
            err("bad_options", "A list in \"%s\" needs 1 to 10 rows in all." % nid, nid)
            return
        ids = set()
        for r in rows:
            r = r if isinstance(r, dict) else {}
            if not OPTION_ID_RE.match(str(r.get("id") or "")) or r["id"] in ids:
                err("bad_options", "Each row in \"%s\" needs its own short id." % nid, nid)
            ids.add(r.get("id"))
            if not (isinstance(r.get("title"), str) and 1 <= len(r["title"].strip()) <= 24):
                err("bad_options", "A row's title is 1 to 24 characters (step \"%s\")." % nid, nid)
            if r.get("description") is not None and len(str(r["description"])) > 72:
                err("bad_options", "A row's description is up to 72 characters (step \"%s\")." % nid, nid)
        _timeout_rules(nid, node, err)
    elif kind == "template":
        if not (isinstance(node.get("name"), str) and node["name"].strip()):
            err("bad_template", "Step \"%s\" needs the name of an approved template." % nid, nid)
        if node.get("variables") is not None and not isinstance(node["variables"], dict):
            err("bad_template", "Step \"%s\": the template's values must be a list of blanks." % nid, nid)
    elif kind == "ask":
        need_text("question")
        validator = node.get("validator", "text")
        if validator not in VALIDATORS:
            err("bad_validator", "Step \"%s\": the answer check must be one of %s." % (nid, ", ".join(VALIDATORS)), nid)
        if validator == "regex":
            problem = _regex_problem(node.get("pattern"))
            if problem:
                err("bad_validator", "Step \"%s\": %s." % (nid, problem), nid)
        if validator == "options" and not (isinstance(node.get("options"), list) and node["options"]):
            err("bad_validator", "Step \"%s\" needs the list of allowed answers." % nid, nid)
        if node.get("save_as") is not None and not SAVE_AS_RE.match(str(node["save_as"])):
            err("bad_save", "Step \"%s\": save the answer as attr:Name or var:name." % nid, nid)
        if not (isinstance(node.get("max_tries", 2), int) and 1 <= node.get("max_tries", 2) <= 5):
            err("bad_options", "Step \"%s\": tries must be 1 to 5." % nid, nid)
        _timeout_rules(nid, node, err)
    elif kind == "condition":
        rules = node.get("rules")
        if not (isinstance(rules, list) and 1 <= len(rules) <= 10):
            err("bad_rules", "Step \"%s\" needs 1 to 10 conditions." % nid, nid)
            return
        for r in rules:
            r = r if isinstance(r, dict) else {}
            if not CONDITION_FIELDS_RE.match(str(r.get("field") or "")) or r.get("op") not in CONDITION_OPS:
                err("bad_rules", "A condition in \"%s\" is not valid." % nid, nid)
            elif _time_rule_problem(r):
                err("bad_rules", "A condition in \"%s\": %s." % (nid, _time_rule_problem(r)), nid)
            elif r["op"] == "regex" and _regex_problem(r.get("value")):
                err("bad_rules", "A condition in \"%s\": %s." % (nid, _regex_problem(r.get("value"))), nid)
        if not node.get("else"):
            warn("no_else", "Step \"%s\" has nowhere to go when no condition fits; the flow will end there." % nid, nid)
    elif kind == "set_attribute":
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", str(node.get("key") or "")):
            err("bad_attribute", "Step \"%s\" needs a valid attribute name." % nid, nid)
        if not (isinstance(node.get("value"), str) and len(node["value"]) <= 200):
            err("bad_attribute", "Step \"%s\" needs a value (up to 200 characters)." % nid, nid)
    elif kind in ("add_tag", "remove_tag"):
        if not (isinstance(node.get("tag"), str) and 1 <= len(node["tag"].strip()) <= 40):
            err("bad_tag", "Step \"%s\" needs a tag (up to 40 characters)." % nid, nid)
    elif kind == "delay":
        minutes, seconds = node.get("minutes"), node.get("seconds")
        if minutes is not None:
            if not (isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= MAX_DELAY_MINUTES):
                err("bad_delay", "Step \"%s\": wait from 1 minute to 7 days." % nid, nid)
        elif not (isinstance(seconds, int) and not isinstance(seconds, bool) and 5 <= seconds <= 3600):
            err("bad_delay", "Step \"%s\" needs how long to wait." % nid, nid)
    elif kind == "handoff":
        if text is not None and not (isinstance(text, str) and len(text) <= MAX_TEXT):
            err("bad_text", "Step \"%s\": the message is up to %d characters." % (nid, MAX_TEXT), nid)
    elif kind == "agent":
        if node.get("agent_id") is not None:
            try:
                uuid.UUID(str(node["agent_id"]))
            except ValueError:
                err("bad_agent", "Step \"%s\": that is not an AI agent." % nid, nid)
    elif kind == "jump_flow":
        try:
            uuid.UUID(str(node.get("flow_id")))
        except ValueError:
            err("bad_flow", "Step \"%s\" needs the flow to hand over to." % nid, nid)
    elif kind == "media":
        if node.get("kind") not in MEDIA_KINDS:
            err("bad_media", "Step \"%s\": choose a picture, video, voice note or document." % nid, nid)
        try:
            uuid.UUID(str(node.get("asset_id")))
        except ValueError:
            err("bad_media", "Step \"%s\" needs a file from your Media Library." % nid, nid)
        if node.get("caption") is not None and not (isinstance(node["caption"], str) and len(node["caption"]) <= 1024):
            err("bad_text", "Step \"%s\": the words under the file are up to 1024 characters." % nid, nid)
    elif kind == "location_request":
        need_text("request")
        _timeout_rules(nid, node, err)
    elif kind == "send_location":
        lat, lng = node.get("latitude"), node.get("longitude")
        num = lambda v, lo, hi: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi             # noqa: E731
        if not (num(lat, -90, 90) and num(lng, -180, 180)):
            err("bad_location", "Step \"%s\" needs a latitude (-90 to 90) and a longitude (-180 to 180)." % nid, nid)
        for key, maximum in (("name", 100), ("address", 200)):
            if node.get(key) is not None and not (isinstance(node[key], str) and len(node[key]) <= maximum):
                err("bad_location", "Step \"%s\": the %s is up to %d characters." % (nid, key, maximum), nid)
    elif kind == "webhook":
        url = str(node.get("url") or "")
        if node.get("method", "POST") not in ("GET", "POST"):
            err("bad_request", "Step \"%s\": the method is GET or POST." % nid, nid)
        if not url.startswith("https://") or len(url) > 500:
            err("bad_request", "Step \"%s\": the address must start with https:// (up to 500 characters)." % nid, nid)
        if not (isinstance(node.get("headers", {}), dict) and len(node.get("headers", {})) <= 10):
            err("bad_request", "Step \"%s\": up to 10 headers." % nid, nid)
        if node.get("body") is not None and not (isinstance(node["body"], str) and len(node["body"]) <= 2000):
            err("bad_request", "Step \"%s\": the body is up to 2000 characters." % nid, nid)
    elif kind == "capi_event":
        if node.get("event") not in CAPI_EVENTS:
            err("bad_event", "Step \"%s\": choose one of %s." % (nid, ", ".join(CAPI_EVENTS)), nid)
        if node.get("value") is not None and not (isinstance(node["value"], (str, int, float)) and not isinstance(node["value"], bool) and len(str(node["value"])) <= 40):
            err("bad_event", "Step \"%s\": the value is a number or a {{var.name}}." % nid, nid)
        if node.get("currency") is not None and not re.match(r"^[A-Z]{3}$", str(node["currency"])):
            err("bad_event", "Step \"%s\": the currency is a three-letter code like INR." % nid, nid)
    elif kind == "send_form":
        try:
            uuid.UUID(str(node.get("form_id")))
        except ValueError:
            err("bad_form", "Step \"%s\" needs the form to send." % nid, nid)
        if node.get("text") is not None and not (isinstance(node["text"], str) and len(node["text"]) <= 1024):
            err("bad_text", "Step \"%s\": the words above the form are up to 1024 characters." % nid, nid)
        if node.get("button") is not None and not (isinstance(node["button"], str) and len(node["button"]) <= 30):
            err("bad_text", "Step \"%s\": the button words are up to 30 characters." % nid, nid)
    elif kind == "send_products":
        if node.get("mode") not in ("product", "products", "catalog"):
            err("bad_products", "Step \"%s\": choose one product, a list of products, or the whole catalogue." % nid, nid)
        elif node["mode"] == "product" and not (isinstance(node.get("retailer_id"), str) and node["retailer_id"].strip()):
            err("bad_products", "Step \"%s\" needs the product to send." % nid, nid)
        elif node["mode"] == "products":
            ids = node.get("retailer_ids")
            if not (isinstance(ids, list) and 1 <= len(ids) <= 30 and all(isinstance(i, str) and i.strip() for i in ids)):
                err("bad_products", "Step \"%s\" needs 1 to 30 products." % nid, nid)
        if node.get("text") is not None and not (isinstance(node["text"], str) and len(node["text"]) <= 1024):
            err("bad_text", "Step \"%s\": the words with the products are up to 1024 characters." % nid, nid)
    elif kind == "request_payment":
        if node.get("empty_text") is not None and not (isinstance(node["empty_text"], str) and len(node["empty_text"]) <= 1024):
            err("bad_text", "Step \"%s\": the words for when there is no cart are up to 1024 characters." % nid, nid)
    elif kind == "api_request":
        url = str(node.get("url") or "")
        if node.get("method", "GET") not in ("GET", "POST"):
            err("bad_request", "Step \"%s\": the method is GET or POST." % nid, nid)
        if not url.startswith("https://") or len(url) > 500:
            err("bad_request", "Step \"%s\": the address must start with https:// (up to 500 characters)." % nid, nid)
        if not (isinstance(node.get("headers", {}), dict) and len(node.get("headers", {})) <= 10):
            err("bad_request", "Step \"%s\": up to 10 headers." % nid, nid)
        if not (isinstance(node.get("save", {}), dict) and len(node.get("save", {})) <= 10):
            err("bad_request", "Step \"%s\": save up to 10 values from the answer." % nid, nid)
        elif any(not re.match(r"^(attr:)?[A-Za-z][A-Za-z0-9_]{0,39}$", str(k)) for k in (node.get("save") or {})):
            err("bad_request", "Step \"%s\": a kept value is called a name like city, or attr:City to put it on the contact." % nid, nid)
        if node.get("body") is not None and not (isinstance(node["body"], str) and len(node["body"]) <= 2000):
            err("bad_request", "Step \"%s\": the body is up to 2000 characters." % nid, nid)
    # "end" has no rules


def _time_rule_problem(rule: dict) -> Optional[str]:
    """What is wrong with a condition on the day, the date, the time or a holiday (None = fine)."""
    field, op, value = rule.get("field"), rule.get("op"), str(rule.get("value") or "").strip()
    if field == "holiday":
        return None if op in ("yes", "no") else "a holiday condition is yes or no"
    if field == "weekday":
        days = [d.strip().lower() for d in value.split(",") if d.strip()]
        if op not in ("eq", "ne", "in") or not days or any(d not in WEEKDAYS for d in days):
            return "choose days from %s" % ", ".join(WEEKDAYS)
    elif field == "date":
        if op not in ("eq", "gt", "lt", "between"):
            return "a date condition is on, after, before or between"
        parts = value.split("..") if op == "between" else [value]
        if (op == "between" and len(parts) != 2) or any(not (p == "today" or DATE_RE.match(p)) for p in parts):
            return "write dates like 2026-11-01, or today (and two dates with .. between them)"
    elif field == "time":
        if op not in ("gt", "lt", "between"):
            return "a time condition is after, before or between"
        parts = value.split("..") if op == "between" else [value]
        if (op == "between" and len(parts) != 2) or any(not TIME_RE.match(p) for p in parts):
            return "write times like 09:30 (and two times with .. between them)"
    return None


def _timeout_rules(nid: str, node: dict, err) -> None:
    t = node.get("timeout_minutes")
    if t is not None and not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= MAX_DELAY_MINUTES):
        err("bad_delay", "Step \"%s\": the time to wait for an answer is 1 minute to 7 days." % nid, nid)


def _runs_forever(nodes: dict) -> bool:
    """A cycle that never stops to wait for the customer. Edges out of a waiting step are not followed: the
    customer's answer, not the bot, takes the next step."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {nid: WHITE for nid in nodes}

    def walk(nid) -> bool:
        colour[nid] = GREY
        node = nodes[nid]
        if isinstance(node, dict) and node.get("type") not in WAITING:
            for t in _targets(node):
                if t not in nodes:
                    continue
                if colour[t] == GREY:
                    return True
                if colour[t] == WHITE and walk(t):
                    return True
        colour[nid] = BLACK
        return False

    return any(colour[n] == WHITE and walk(n) for n in nodes if isinstance(nodes[n], dict))


# ── saving ───────────────────────────────────────────────────────────────────

def _flow(cur, tenant_id, flow_id, *, lock: bool = False) -> dict:
    cur.execute("select id, name, enabled, trigger, graph, version, published_at, created_at, updated_at from flows"
                " where id = %s and tenant_id = %s" + (" for update" if lock else ""), (flow_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise FlowError("unknown_flow", "that flow is not in this workspace")
    return dict(zip(("id", "name", "enabled", "trigger", "graph", "version", "published_at", "created_at", "updated_at"), row))


def get(cur, tenant_id, flow_id) -> dict:
    return _flow(cur, tenant_id, flow_id)


def list_flows(cur, tenant_id) -> list:
    cur.execute("select f.id, f.name, f.enabled, f.trigger, f.version, f.published_at, f.updated_at,"
                " (select count(*) from flow_runs r where r.flow_id = f.id and r.state in ('running', 'waiting')),"
                " (select count(*) from flow_runs r where r.flow_id = f.id)"
                " from flows f where f.tenant_id = %s order by f.created_at", (tenant_id,))
    return [dict(zip(("id", "name", "enabled", "trigger", "version", "published_at", "updated_at", "live", "total_runs"), r))
            for r in cur.fetchall()]


def create(cur, tenant_id, name: str, *, trigger: Optional[dict] = None, graph: Optional[dict] = None) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise FlowError("bad_name", "Give the flow a name (up to 80 characters).")
    plans.require(cur, tenant_id, "flows", "Chatbot flows")
    cur.execute("select count(*) from flows where tenant_id = %s", (tenant_id,))
    plans.check_count(cur, tenant_id, "flows", cur.fetchone()[0], 1, "flows")
    cur.execute("insert into flows (tenant_id, name, trigger, graph) values (%s, %s, %s, %s) returning id",
                (tenant_id, name, Json(trigger or {}), Json(graph or {})))
    return cur.fetchone()[0]


def update(cur, tenant_id, flow_id, *, name=None, trigger=None, graph=None) -> dict:
    """Edit the draft. Nothing a customer sees changes until it is published."""
    f = _flow(cur, tenant_id, flow_id, lock=True)
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise FlowError("bad_name", "Give the flow a name (up to 80 characters).")
        f["name"] = name
    if trigger is not None:
        f["trigger"] = trigger
    if graph is not None:
        if not isinstance(graph, dict) or len(json_size(graph)) > 400_000:
            raise FlowError("bad_graph", "That flow is too large.")
        f["graph"] = graph
    cur.execute("update flows set name = %s, trigger = %s, graph = %s, updated_at = now() where id = %s",
                (f["name"], Json(f["trigger"]), Json(f["graph"]), flow_id))
    return f


def json_size(value) -> str:
    import json
    return json.dumps(value, separators=(",", ":"))


def delete(cur, tenant_id, flow_id) -> bool:
    cur.execute("delete from flows where id = %s and tenant_id = %s", (flow_id, tenant_id))
    return cur.rowcount == 1


def asset_issues(cur, tenant_id, graph) -> list:
    """The files a flow sends must still be in the Media Library."""
    out = []
    for nid, node in ((graph or {}).get("nodes") or {}).items():
        if isinstance(node, dict) and node.get("type") == "media":
            try:
                key = str(uuid.UUID(str(node.get("asset_id"))))
            except ValueError:
                continue                                             # already reported as a bad step
            cur.execute("select 1 from media_assets where id = %s and tenant_id = %s", (key, tenant_id))
            if cur.fetchone() is None:
                out.append(Issue("error", "unknown_asset", "Step \"%s\" sends a file that is no longer in your Media Library." % nid, str(nid)))
    return out


def check(cur, tenant_id, flow_id) -> list:
    f = _flow(cur, tenant_id, flow_id)
    return validate(f["graph"], f["trigger"]) + asset_issues(cur, tenant_id, f["graph"])


def publish(cur, tenant_id, flow_id) -> dict:
    """Freeze the draft as the next version and make it the one customers walk. Refused while it has errors."""
    plans.require(cur, tenant_id, "flows", "Chatbot flows")
    f = _flow(cur, tenant_id, flow_id, lock=True)
    issues = validate(f["graph"], f["trigger"]) + asset_issues(cur, tenant_id, f["graph"])
    if errors(issues):
        raise FlowError("flow_invalid", "; ".join(i.message for i in errors(issues)), issues)
    version = f["version"] + 1
    cur.execute("insert into flow_versions (tenant_id, flow_id, version, trigger, graph) values (%s, %s, %s, %s, %s)",
                (tenant_id, flow_id, version, Json(f["trigger"]), Json(f["graph"])))
    cur.execute("update flows set version = %s, published_at = now(), updated_at = now() where id = %s", (version, flow_id))
    return {"version": version, "issues": [i.as_dict() for i in issues]}


def versions(cur, tenant_id, flow_id) -> list:
    _flow(cur, tenant_id, flow_id)
    cur.execute("select version, published_at from flow_versions where flow_id = %s and tenant_id = %s order by version desc limit 50",
                (flow_id, tenant_id))
    return [{"version": v, "published_at": at} for v, at in cur.fetchall()]


def rollback(cur, tenant_id, flow_id, version: int) -> dict:
    """Make an earlier published version live again (as a new version, so the history stays whole) and load it
    into the draft. Customers mid-flow keep walking the version they started on."""
    f = _flow(cur, tenant_id, flow_id, lock=True)
    cur.execute("select trigger, graph from flow_versions where flow_id = %s and tenant_id = %s and version = %s",
                (flow_id, tenant_id, version))
    row = cur.fetchone()
    if row is None:
        raise FlowError("unknown_version", "that version was never published")
    new = f["version"] + 1
    cur.execute("insert into flow_versions (tenant_id, flow_id, version, trigger, graph) values (%s, %s, %s, %s, %s)",
                (tenant_id, flow_id, new, Json(row[0]), Json(row[1])))
    cur.execute("update flows set version = %s, trigger = %s, graph = %s, published_at = now(), updated_at = now() where id = %s",
                (new, Json(row[0]), Json(row[1]), flow_id))
    return {"version": new}


def set_enabled(cur, tenant_id, flow_id, enabled: bool) -> None:
    f = _flow(cur, tenant_id, flow_id, lock=True)
    if enabled:
        plans.require(cur, tenant_id, "flows", "Chatbot flows")
        if f["version"] < 1:
            raise FlowError("not_published", "Publish the flow before switching it on.")
    cur.execute("update flows set enabled = %s, updated_at = now() where id = %s", (bool(enabled), flow_id))
    if not enabled:                                                # stop whoever is mid-flow, so nobody is left hanging
        cur.execute("update flow_runs set state = 'cancelled', finished_at = now() where flow_id = %s"
                    " and state in ('running', 'waiting')", (flow_id,))
