"""Forms that open inside a WhatsApp chat (Meta calls them Flows): build one, check it, give it to Meta, send it, and keep what customers fill in.

A form is kept in Prism's own small model — screens, each with fields (a name box, a date, a choice) — and turned into the Flow JSON Meta wants (`to_flow_json`). Prism
checks it first in plain words (`check`), because Meta's own error messages are terse. Meta has the last word: what it says is wrong with an upload is kept with the form.

Two kinds:
  plain      Meta runs the whole form on the phone; when the customer taps Send, the answers come back in one message (`on_reply`).
  live       the form asks Prism as it goes (form_endpoint.py): which slots are free on the date they picked, and whether the slot is still free when they confirm.
             Those requests are encrypted with a key pair that belongs to the workspace.

The limits below are from Meta's Flow JSON reference as read in October 2026; they are here to catch the common mistakes early, not to replace Meta's check.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import Json

from prism_wa import commerce, contacts, send, webhooks_out
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.forms")

FLOW_VERSION = "6.3"
DATA_API_VERSION = "3.0"
CATEGORIES = ("SIGN_UP", "SIGN_IN", "APPOINTMENT_BOOKING", "LEAD_GENERATION", "CONTACT_US", "CUSTOMER_SUPPORT", "SURVEY", "OTHER")
KINDS = {"text": "text", "email": "email", "phone": "phone", "number": "number", "paragraph": None, "date": None, "dropdown": None, "radio": None, "checkbox": None, "opt_in": None}
CHOICE_KINDS = ("dropdown", "radio", "checkbox")
MAX_SCREENS = 8
MAX_FIELDS = 20                      # per screen
LABEL_MAX = 20                       # an input's label
OPTION_MAX = {"dropdown": 200, "radio": 20, "checkbox": 20}
OPTION_TITLE_MAX = 30
FOOTER_MAX = 35
HEADING_MAX = 80
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SCREEN_RE = re.compile(r"^[A-Z][A-Z_]{0,39}$")
STATUSES = ("draft", "published", "deprecated", "throttled", "blocked")
FORM_COLS = ("id", "name", "template", "categories", "screens", "flow_json", "meta_flow_id", "status", "version", "endpoint", "mapping", "tag_on_submit", "settings", "problems",
             "responses", "published_at", "created_at", "updated_at")


class FormError(Exception):
    def __init__(self, reason: str, message: str, problems: Optional[list] = None):
        super().__init__(message)
        self.reason, self.problems = reason, problems or []


# ── starting points ──────────────────────────────────────────────────────────

def _choices(*titles) -> list:
    return [{"id": re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:30] or "x", "title": t} for t in titles]


TEMPLATES = {
    "lead_capture": {
        "name": "Lead capture", "categories": ["LEAD_GENERATION"], "endpoint": False, "tag_on_submit": "form-lead",
        "about": "A short form for new enquiries: name, phone and what they need.",
        "screens": [{"id": "ENQUIRY", "title": "Tell us about you", "texts": [{"style": "body", "text": "We will call you back within a day."}], "button": "Send", "fields": [
            {"kind": "text", "name": "name", "label": "Your name", "required": True},
            {"kind": "phone", "name": "phone", "label": "Phone number", "required": True},
            {"kind": "email", "name": "email", "label": "Email", "required": False},
            {"kind": "dropdown", "name": "need", "label": "What do you need?", "required": True, "options": _choices("Boundary wall", "Gate", "Painting", "Something else")},
            {"kind": "paragraph", "name": "note", "label": "Anything to add?", "required": False}]}],
        "mapping": {"name": "@name", "email": "@email", "need": "Interest"}, "settings": {"thank_you": "Thank you! We will be in touch soon."}},
    "appointment": {
        "name": "Book an appointment", "categories": ["APPOINTMENT_BOOKING"], "endpoint": True, "tag_on_submit": "booked",
        "about": "Pick a day and a free time. Prism shows only the times that are still free and will not double-book one.",
        "screens": [{"id": "BOOK", "title": "Book a visit", "texts": [{"style": "body", "text": "Choose a day, then a time."}], "button": "Book", "fields": [
            {"kind": "text", "name": "name", "label": "Your name", "required": True},
            {"kind": "date", "name": "date", "label": "Day", "required": True, "live": "date"},
            {"kind": "dropdown", "name": "slot", "label": "Time", "required": True, "live": "slots", "options": []},
            {"kind": "paragraph", "name": "note", "label": "Anything to know?", "required": False}]}],
        "mapping": {"name": "@name", "date": "Visit_date", "slot": "Visit_time"},
        "settings": {"thank_you": "Booked! We will remind you before the visit.",
                     "availability": {"weekdays": [0, 1, 2, 3, 4, 5], "start": "10:00", "end": "17:00", "slot_minutes": 60, "capacity": 1, "min_notice_hours": 2, "window_days": 21,
                                      "tz": "Asia/Kolkata", "date_field": "date", "slot_field": "slot"}}},
    "feedback": {
        "name": "Feedback", "categories": ["SURVEY"], "endpoint": False, "tag_on_submit": "feedback-given",
        "about": "Ask how it went: a rating, what they liked and a comment.",
        "screens": [{"id": "FEEDBACK", "title": "How did we do?", "texts": [], "button": "Send feedback", "fields": [
            {"kind": "radio", "name": "rating", "label": "Your rating", "required": True, "options": [{"id": "5", "title": "5 - Excellent"}, {"id": "4", "title": "4 - Good"},
                                                                                                    {"id": "3", "title": "3 - Okay"}, {"id": "2", "title": "2 - Poor"}, {"id": "1", "title": "1 - Very poor"}]},
            {"kind": "checkbox", "name": "liked", "label": "What went well?", "required": False, "options": _choices("The work", "The price", "The team", "On time")},
            {"kind": "paragraph", "name": "comment", "label": "Comments", "required": False}]}],
        "mapping": {"rating": "Last_rating"}, "settings": {"thank_you": "Thank you for telling us."}},
    "site_visit": {
        "name": "Site visit request", "categories": ["APPOINTMENT_BOOKING"], "endpoint": False, "tag_on_submit": "visit-requested",
        "about": "Ask where and when they would like someone to come. A person confirms the time.",
        "screens": [{"id": "VISIT", "title": "Request a site visit", "texts": [], "button": "Request visit", "fields": [
            {"kind": "text", "name": "name", "label": "Your name", "required": True},
            {"kind": "phone", "name": "phone", "label": "Phone number", "required": True},
            {"kind": "paragraph", "name": "address", "label": "Site address", "required": True},
            {"kind": "date", "name": "date", "label": "Preferred day", "required": True},
            {"kind": "dropdown", "name": "period", "label": "Preferred time", "required": True, "options": _choices("Morning", "Afternoon", "Evening")}]}],
        "mapping": {"name": "@name", "address": "Site_address", "date": "Visit_date"}, "settings": {"thank_you": "Thank you. We will confirm the visit shortly."}},
    "quotation": {
        "name": "Quotation request", "categories": ["LEAD_GENERATION"], "endpoint": False, "tag_on_submit": "quote-requested",
        "about": "Two steps: what they want built, then how to reach them.",
        "screens": [
            {"id": "PROJECT", "title": "Your project", "texts": [], "button": "Next", "fields": [
                {"kind": "dropdown", "name": "project", "label": "Type of work", "required": True, "options": _choices("Boundary wall", "Gate", "Painting", "Repair", "Other")},
                {"kind": "number", "name": "area", "label": "Area in sq ft", "required": False},
                {"kind": "text", "name": "city", "label": "City", "required": True}]},
            {"id": "CONTACT", "title": "How do we reach you?", "texts": [], "button": "Get my quote", "fields": [
                {"kind": "text", "name": "name", "label": "Your name", "required": True},
                {"kind": "phone", "name": "phone", "label": "Phone number", "required": True},
                {"kind": "email", "name": "email", "label": "Email", "required": False},
                {"kind": "opt_in", "name": "consent", "label": "OK to message me", "required": True}]}],
        "mapping": {"name": "@name", "email": "@email", "city": "City", "project": "Interest"}, "settings": {"thank_you": "Thank you! Your quotation is on its way."}},
    "kyc_lite": {
        "name": "Basic details", "categories": ["SIGN_UP"], "endpoint": False, "tag_on_submit": "details-given",
        "about": "Name, date of birth, city and only the last four digits of an ID. Never ask for a whole ID number here.",
        "screens": [{"id": "DETAILS", "title": "Your details", "texts": [{"style": "caption", "text": "Only the last four digits of your ID. Never send the whole number in a chat."}],
                     "button": "Submit", "fields": [
            {"kind": "text", "name": "name", "label": "Full name", "required": True},
            {"kind": "date", "name": "dob", "label": "Date of birth", "required": True},
            {"kind": "text", "name": "city", "label": "City", "required": True},
            {"kind": "dropdown", "name": "id_type", "label": "ID type", "required": True, "options": _choices("Aadhaar", "PAN", "Driving licence", "Voter ID")},
            {"kind": "text", "name": "id_last4", "label": "Last 4 digits", "required": True, "helper": "Only the last four digits"},
            {"kind": "opt_in", "name": "consent", "label": "I agree", "required": True}]}],
        "mapping": {"name": "@name", "dob": "Date_of_birth", "city": "City"}, "settings": {"thank_you": "Thank you. Your details are saved."}},
}


def template_list() -> list:
    return [{"key": k, "name": t["name"], "about": t["about"], "categories": t["categories"], "live": t["endpoint"], "screens": len(t["screens"]),
             "fields": sum(len(s["fields"]) for s in t["screens"])} for k, t in TEMPLATES.items()]


# ── cleaning what the screen sends ───────────────────────────────────────────

def _text(value, n: int) -> str:
    return str(value or "").strip()[:n]


def clean_screens(raw) -> list:
    if not isinstance(raw, list):
        raise FormError("bad_screens", "a form is a list of screens")
    out = []
    for s in raw[:MAX_SCREENS + 4]:
        if not isinstance(s, dict):
            raise FormError("bad_screens", "each screen is a title, some text and some fields")
        fields = []
        for f in (s.get("fields") or [])[:MAX_FIELDS + 5]:
            if not isinstance(f, dict):
                raise FormError("bad_screens", "each field has a kind, a name and a label")
            kind = str(f.get("kind") or "text")
            options = []
            for o in f.get("options") or []:
                if isinstance(o, dict):
                    options.append({"id": _text(o.get("id") or o.get("title"), 30), "title": _text(o.get("title") or o.get("id"), 60)})
                elif str(o).strip():
                    options.append({"id": re.sub(r"[^a-z0-9]+", "_", str(o).lower()).strip("_")[:30] or "x", "title": _text(o, 60)})
            field = {"kind": kind, "name": _text(f.get("name"), 60).lower(), "label": _text(f.get("label"), 80), "required": f.get("required") is not False}
            if f.get("helper"):
                field["helper"] = _text(f["helper"], 80)
            if kind in CHOICE_KINDS:
                field["options"] = options
            if f.get("live") in ("date", "slots"):
                field["live"] = f["live"]
            fields.append(field)
        texts = [{"style": t.get("style") if t.get("style") in ("heading", "subheading", "body", "caption") else "body", "text": _text(t.get("text"), 600)}
                 for t in (s.get("texts") or []) if isinstance(t, dict) and str(t.get("text") or "").strip()]
        out.append({"id": _text(s.get("id"), 40).upper().replace(" ", "_"), "title": _text(s.get("title"), 60), "texts": texts, "button": _text(s.get("button") or "Continue", 60),
                    "fields": fields})
    return out


def clean_mapping(raw, screens: list) -> dict:
    names = {f["name"] for s in screens for f in s["fields"]}
    out = {}
    for field, target in (raw or {}).items():
        field, target = str(field).strip().lower(), str(target or "").strip()
        if field not in names or not target:
            continue
        if target not in ("@name", "@email") and not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", target):
            raise FormError("bad_mapping", "%r cannot be a contact detail's name (letters, numbers and underscores, starting with a letter)" % target)
        out[field] = target
    return out


def _int(value, default: int) -> int:
    return default if value in (None, "") else int(value)


def clean_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {"thank_you": _text(raw.get("thank_you"), 600)}
    a = raw.get("availability")
    if isinstance(a, dict):
        try:
            weekdays = sorted({int(x) for x in a.get("weekdays") or [] if 0 <= int(x) <= 6})
            slot, capacity = _int(a.get("slot_minutes"), 60), _int(a.get("capacity"), 1)
            notice, window = _int(a.get("min_notice_hours"), 2), _int(a.get("window_days"), 21)
        except (TypeError, ValueError):
            raise FormError("bad_availability", "days, slot length, capacity and notice are whole numbers") from None
        start, end = str(a.get("start") or "10:00"), str(a.get("end") or "17:00")
        if not (re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start) and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", end)) or start >= end:
            raise FormError("bad_availability", "the opening time must be before the closing time (like 10:00 and 17:00)")
        if not weekdays:
            raise FormError("bad_availability", "choose at least one day of the week")
        if not 10 <= slot <= 480 or not 1 <= capacity <= 100 or not 0 <= notice <= 720 or not 1 <= window <= 120:
            raise FormError("bad_availability", "a slot is 10 minutes to 8 hours, a slot can take 1 to 100 people, notice is up to 30 days, and booking opens up to 120 days ahead")
        tz = str(a.get("tz") or "Asia/Kolkata")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise FormError("bad_availability", "%r is not a time zone name (like Asia/Kolkata)" % tz) from None
        out["availability"] = {"weekdays": weekdays, "start": start, "end": end, "slot_minutes": slot, "capacity": capacity, "min_notice_hours": notice, "window_days": window,
                               "tz": tz, "date_field": _text(a.get("date_field") or "date", 40), "slot_field": _text(a.get("slot_field") or "slot", 40)}
    return out


# ── checking, in words ───────────────────────────────────────────────────────

def check(form: dict) -> list:
    """What is wrong with a form, in plain words: [{"where": "Screen 1 · Your name", "message": "..."}]. Empty when it is ready to give to Meta."""
    problems = []

    def bad(where, message):
        problems.append({"where": where, "message": message})

    screens = form.get("screens") or []
    if not (form.get("name") or "").strip():
        bad("Form", "Give the form a name.")
    if not screens:
        bad("Form", "Add at least one screen.")
    if len(screens) > MAX_SCREENS:
        bad("Form", "A form can have up to %d screens." % MAX_SCREENS)
    ids, names = set(), {}
    for n, s in enumerate(screens, start=1):
        here = "Screen %d" % n
        if not SCREEN_RE.match(s["id"] or ""):
            bad(here, "The screen's id uses capital letters and underscores only (like BOOK_VISIT).")
        elif s["id"] in ids:
            bad(here, "Two screens are called %s." % s["id"])
        ids.add(s["id"])
        if s["id"] == "SUCCESS" and form.get("endpoint"):
            bad(here, "SUCCESS is kept for the thank-you screen of a live form; choose another id.")
        if not s["title"]:
            bad(here, "Give the screen a title.")
        if not s["fields"] and not s["texts"]:
            bad(here, "A screen needs at least a field or some text.")
        if len(s["fields"]) > MAX_FIELDS:
            bad(here, "A screen holds up to %d fields; move some to the next screen." % MAX_FIELDS)
        if not s["button"] or len(s["button"]) > FOOTER_MAX:
            bad(here, "The button needs a label of up to %d characters." % FOOTER_MAX)
        for t in s["texts"]:
            if len(t["text"]) > (HEADING_MAX if t["style"] in ("heading", "subheading") else 600):
                bad(here, "A heading is up to %d characters." % HEADING_MAX)
        for f in s["fields"]:
            where = "%s · %s" % (here, f["label"] or f["name"] or "a field")
            if f["kind"] not in KINDS:
                bad(where, "%r is not a kind of field." % f["kind"])
                continue
            if not NAME_RE.match(f["name"]):
                bad(where, "The field's name uses lowercase letters, numbers and underscores, and starts with a letter.")
            elif f["name"] in names:
                bad(where, "Two fields are called %r (screen %d and screen %d)." % (f["name"], names[f["name"]], n))
            names[f["name"]] = n
            if not f["label"]:
                bad(where, "Give the field a label.")
            elif len(f["label"]) > (30 if f["kind"] == "opt_in" else LABEL_MAX):
                bad(where, "The label is up to %d characters; use the helper text for more." % (30 if f["kind"] == "opt_in" else LABEL_MAX))
            if len(f.get("helper", "")) > 80:
                bad(where, "The helper text is up to 80 characters.")
            if f["kind"] in CHOICE_KINDS:
                live = f.get("live") == "slots"
                if not f["options"] and not live:
                    bad(where, "Add at least one choice.")
                if len(f["options"]) > OPTION_MAX[f["kind"]]:
                    bad(where, "A %s can have up to %d choices." % (f["kind"], OPTION_MAX[f["kind"]]))
                if any(len(o["title"]) > OPTION_TITLE_MAX or not o["title"] for o in f["options"]):
                    bad(where, "Each choice needs a title of up to %d characters." % OPTION_TITLE_MAX)
                if len({o["id"] for o in f["options"]}) != len(f["options"]):
                    bad(where, "Two choices share an id.")
            if f.get("live") == "slots" and f["kind"] != "dropdown":
                bad(where, "Times offered live must be a dropdown.")
            if f.get("live") == "date" and f["kind"] != "date":
                bad(where, "Picking a day live needs a date field.")
        # a button on the last non-live screen of a plain form completes it; payload size is not limited here
    if form.get("endpoint"):
        a = (form.get("settings") or {}).get("availability")
        has_slots = any(f.get("live") == "slots" for s in screens for f in s["fields"])
        if has_slots:
            if not a:
                bad("Form", "A form that offers live times needs opening days and hours.")
            else:
                if a["date_field"] not in names:
                    bad("Form", "The day field %r is not in the form." % a["date_field"])
                if a["slot_field"] not in names:
                    bad("Form", "The time field %r is not in the form." % a["slot_field"])
        if len(screens) < 1:
            pass
    for field, target in (form.get("mapping") or {}).items():
        if field not in names:
            bad("Contact details", "%r is saved to %s but is not a field of the form." % (field, target))
    if form.get("categories") and any(c not in CATEGORIES for c in form["categories"]):
        bad("Form", "Choose categories from: %s." % ", ".join(CATEGORIES))
    if not form.get("categories"):
        bad("Form", "Choose at least one category.")
    return problems


# ── the Flow JSON ────────────────────────────────────────────────────────────

def _component(f: dict, a: Optional[dict]) -> dict:
    kind, base = f["kind"], {"name": f["name"], "label": f["label"], "required": bool(f["required"])}
    if f.get("helper"):
        base["helper-text"] = f["helper"]
    if kind in ("text", "email", "phone", "number"):
        return {"type": "TextInput", "input-type": KINDS[kind], **base}
    if kind == "paragraph":
        return {"type": "TextArea", **base}
    if kind == "date":
        out = {"type": "DatePicker", **base}
        if f.get("live") == "date":
            out["min-date"], out["max-date"] = "${data.min_date}", "${data.max_date}"
            out["on-select-action"] = {"name": "data_exchange", "payload": {"step": "date", "date": "${form.%s}" % f["name"]}}
        return out
    if kind == "opt_in":
        return {"type": "OptIn", "name": f["name"], "label": f["label"], "required": bool(f["required"])}
    source = "${data.slots}" if f.get("live") == "slots" else [{"id": o["id"], "title": o["title"]} for o in f["options"]]
    return {"type": {"dropdown": "Dropdown", "radio": "RadioButtonsGroup", "checkbox": "CheckboxGroup"}[kind], "data-source": source, **base}


def to_flow_json(form: dict) -> dict:
    """The form as Meta's Flow JSON. Carries what earlier screens collected forward, so the last button hands back every answer at once."""
    screens, endpoint = form["screens"], bool(form.get("endpoint"))
    a = (form.get("settings") or {}).get("availability")
    out, carried = [], []
    routing = {}
    for n, s in enumerate(screens):
        last = n == len(screens) - 1
        data = {c: {"type": "string", "__example__": ""} for c in carried}
        own = [f["name"] for f in s["fields"]]
        slots = any(f.get("live") == "slots" for f in s["fields"])
        if slots:
            data["slots"] = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}}},
                             "__example__": [{"id": "10:00", "title": "10:00 AM"}]}
        if any(f.get("live") == "date" for f in s["fields"]):
            data["min_date"], data["max_date"] = {"type": "string", "__example__": "2026-01-01"}, {"type": "string", "__example__": "2026-12-31"}
        if slots or endpoint and last:
            data["error_message"] = {"type": "string", "__example__": ""}
            data["has_error"] = {"type": "boolean", "__example__": False}
        payload = {c: "${data.%s}" % c for c in carried}
        payload.update({f["name"]: "${form.%s}" % f["name"] for f in s["fields"]})
        children = []
        for t in s["texts"]:
            children.append({"type": {"heading": "TextHeading", "subheading": "TextSubheading", "body": "TextBody", "caption": "TextCaption"}[t["style"]], "text": t["text"]})
        if slots or endpoint and last:
            children.append({"type": "TextCaption", "text": "${data.error_message}", "visible": "${data.has_error}"})
        form_children = [_component(f, a) for f in s["fields"]]
        if last and not endpoint:
            action = {"name": "complete", "payload": payload}
        elif last:
            action = {"name": "data_exchange", "payload": {"step": "submit", **payload}}
        else:
            action = {"name": "navigate", "next": {"type": "screen", "name": screens[n + 1]["id"]}, "payload": payload}
        form_children.append({"type": "Footer", "label": s["button"], "on-click-action": action})
        children.append({"type": "Form", "name": "form", "children": form_children})
        screen = {"id": s["id"], "title": s["title"], "data": data, "layout": {"type": "SingleColumnLayout", "children": children}}
        if last and not endpoint:
            screen["terminal"], screen["success"] = True, True
        out.append(screen)
        routing[s["id"]] = [screens[n + 1]["id"]] if not last else (["SUCCESS"] if endpoint else [])
        carried += own
    if endpoint:
        out.append({"id": "SUCCESS", "title": "Done", "terminal": True, "success": True, "data": {},
                    "layout": {"type": "SingleColumnLayout", "children": [
                        {"type": "TextHeading", "text": "Done"},
                        {"type": "TextBody", "text": (form.get("settings") or {}).get("thank_you") or "Thank you."},
                        {"type": "Footer", "label": "Close", "on-click-action": {"name": "complete", "payload": {}}}]}})
        routing["SUCCESS"] = []
    flow = {"version": FLOW_VERSION, "screens": out}
    if endpoint:
        flow["data_api_version"], flow["routing_model"] = DATA_API_VERSION, routing
    return flow


# ── keeping forms ────────────────────────────────────────────────────────────

def _row(cur, tenant_id, form_id) -> dict:
    cur.execute("select " + ", ".join(FORM_COLS) + " from forms where id = %s and tenant_id = %s", (form_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise FormError("unknown_form", "that form is not in this workspace")
    return dict(zip(FORM_COLS, row))


def get_form(cur, tenant_id, form_id) -> dict:
    f = _row(cur, tenant_id, form_id)
    f["problems_now"] = check(f)
    return f


def list_forms(cur, tenant_id) -> list:
    cur.execute("select " + ", ".join(FORM_COLS) + " from forms where tenant_id = %s order by updated_at desc", (tenant_id,))
    out = []
    for r in cur.fetchall():
        f = dict(zip(FORM_COLS, r))
        f["screens_count"], f["fields_count"] = len(f["screens"]), sum(len(s["fields"]) for s in f["screens"])
        f.pop("flow_json", None)
        out.append(f)
    return out


def create_form(cur, tenant_id, d: dict, *, agent_id=None) -> dict:
    base = {}
    if d.get("template"):
        if d["template"] not in TEMPLATES:
            raise FormError("unknown_template", "there is no starting point called %r" % d["template"])
        base = json.loads(json.dumps(TEMPLATES[d["template"]]))
    screens = clean_screens(d["screens"] if "screens" in d else base.get("screens", []))
    categories = d.get("categories") or base.get("categories") or ["LEAD_GENERATION"]
    endpoint = bool(d["endpoint"]) if "endpoint" in d else bool(base.get("endpoint"))
    name = _text(d.get("name") or base.get("name"), 100)
    if not name:
        raise FormError("no_name", "give the form a name")
    cur.execute("insert into forms (tenant_id, name, template, categories, screens, endpoint, mapping, tag_on_submit, settings, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " returning id", (tenant_id, name, d.get("template"), Json(categories), Json(screens), endpoint, Json(clean_mapping(d.get("mapping", base.get("mapping")), screens)),
                                  _text(d.get("tag_on_submit", base.get("tag_on_submit")), 40) or None, Json(clean_settings(d.get("settings", base.get("settings")))), agent_id))
    return get_form(cur, tenant_id, cur.fetchone()[0])


def update_form(cur, tenant_id, form_id, d: dict) -> dict:
    f = _row(cur, tenant_id, form_id)
    screens = clean_screens(d["screens"]) if "screens" in d else f["screens"]
    name = _text(d["name"], 100) if "name" in d else f["name"]
    if not name:
        raise FormError("no_name", "give the form a name")
    cur.execute("update forms set name = %s, categories = %s, screens = %s, endpoint = %s, mapping = %s, tag_on_submit = %s, settings = %s, updated_at = now() where id = %s",
                (name, Json(d.get("categories") or f["categories"]), Json(screens), bool(d["endpoint"]) if "endpoint" in d else f["endpoint"],
                 Json(clean_mapping(d["mapping"], screens) if "mapping" in d else clean_mapping(f["mapping"], screens)),
                 (_text(d["tag_on_submit"], 40) or None) if "tag_on_submit" in d else f["tag_on_submit"],
                 Json(clean_settings(d["settings"]) if "settings" in d else f["settings"]), form_id))
    return get_form(cur, tenant_id, form_id)


def duplicate_form(cur, tenant_id, form_id, *, agent_id=None) -> dict:
    f = _row(cur, tenant_id, form_id)
    return create_form(cur, tenant_id, {"name": (f["name"] + " (copy)")[:100], "screens": f["screens"], "categories": f["categories"], "endpoint": f["endpoint"], "mapping": f["mapping"],
                                        "tag_on_submit": f["tag_on_submit"], "settings": f["settings"], "template": f["template"]}, agent_id=agent_id)


def delete_form(cur, tenant_id, form_id, *, graph=None, vault: Optional[Vault] = None) -> bool:
    f = _row(cur, tenant_id, form_id)
    if f["meta_flow_id"] and graph is not None and vault is not None:
        try:
            token, _waba, _pnid = commerce._token_for(cur, tenant_id, vault)
            (graph.delete_flow if f["status"] == "draft" else graph.deprecate_flow)(token, f["meta_flow_id"])
        except Exception as why:                                       # noqa: BLE001  (Meta unreachable or already gone: ours is removed all the same)
            log.info("could not retire flow %s at Meta: %s", f["meta_flow_id"], why)
    cur.execute("delete from forms where id = %s and tenant_id = %s", (form_id, tenant_id))
    return cur.rowcount > 0


# ── the encryption key a live form needs ─────────────────────────────────────

def ensure_key(cur, tenant_id, vault: Vault) -> str:
    """The workspace's public key (made on first use). The private half is stored encrypted and never leaves this server."""
    cur.execute("select public_pem from flow_keys where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    if row:
        return row[0]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("ascii")
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    blob, ver = vault.encrypt(private, context="flowkey:%s" % tenant_id)
    cur.execute("insert into flow_keys (tenant_id, public_pem, private_enc, key_ver) values (%s, %s, %s, %s) on conflict (tenant_id) do nothing", (tenant_id, public, blob, ver))
    return public


def private_key(cur, tenant_id, vault: Vault) -> Optional[str]:
    cur.execute("select private_enc from flow_keys where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return vault.decrypt(bytes(row[0]), context="flowkey:%s" % tenant_id) if row else None


def upload_key(cur, tenant_id, graph, vault: Vault) -> dict:
    """Give Meta the public half, for every number of the workspace. Meta then encrypts a live form's requests with it."""
    public = ensure_key(cur, tenant_id, vault)
    cur.execute("select w.token_enc, w.waba_id, p.phone_number_id from phone_numbers p join wabas w on w.id = p.waba_id where p.tenant_id = %s", (tenant_id,))
    rows = cur.fetchall()
    if not rows:
        raise FormError("no_number", "connect a WhatsApp number first")
    done = 0
    for token_enc, waba, pnid in rows:
        graph.set_flow_encryption_key(vault.decrypt(token_enc, context=waba), pnid, public)
        done += 1
    cur.execute("update flow_keys set uploaded_at = now() where tenant_id = %s", (tenant_id,))
    return {"uploaded": done}


def key_status(cur, tenant_id) -> dict:
    cur.execute("select uploaded_at, created_at from flow_keys where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return {"has_key": row is not None, "uploaded_at": row[0] if row else None, "created_at": row[1] if row else None}


# ── giving a form to Meta ────────────────────────────────────────────────────

def endpoint_url(public_url: str, tenant_id, form_id) -> str:
    return "%s/v1/whatsapp/forms/endpoint/%s/%s" % ((public_url or "").rstrip("/"), tenant_id, form_id)


def publish(cur, tenant_id, graph, vault: Vault, form_id, *, public_url: str = "") -> dict:
    """Check, upload and publish. A published form cannot be edited at Meta, so changing one makes a new version there (and the old is retired)."""
    f = _row(cur, tenant_id, form_id)
    problems = check(f)
    if problems:
        raise FormError("form_invalid", "The form is not ready: %s" % problems[0]["message"], problems)
    token, waba, _pnid = commerce._token_for(cur, tenant_id, vault)
    uri = ""
    if f["endpoint"]:
        if not public_url.startswith("https://"):
            raise FormError("not_set_up", "live forms need this server's public address (PUBLIC_URL) to be set")
        upload_key(cur, tenant_id, graph, vault)
        uri = endpoint_url(public_url, tenant_id, form_id)
    flow_json = to_flow_json(f)
    old = f["meta_flow_id"] if f["status"] == "published" else None
    flow_id = None if old else f["meta_flow_id"]
    if flow_id:
        graph.update_flow(token, flow_id, name=f["name"], categories=f["categories"], endpoint_uri=uri)
    else:
        created = graph.create_flow(token, waba, name="%s v%d" % (f["name"], f["version"] + (1 if old else 0)) if old else f["name"], categories=f["categories"], endpoint_uri=uri)
        flow_id = str(created.get("id") or "")
        if not flow_id:
            raise FormError("meta_error", "Meta did not return a form id")
    reply = graph.upload_flow_json(token, flow_id, flow_json)
    errors = [e for e in (reply.get("validation_errors") or []) if isinstance(e, dict)]
    if errors:
        found = [{"where": "Meta", "message": str(e.get("message") or e.get("error") or "Meta did not accept the form")} for e in errors]
        cur.execute("update forms set meta_flow_id = %s, problems = %s, flow_json = %s, status = case when status = 'published' then status else 'draft' end, updated_at = now() where id = %s",
                    (flow_id if not old else f["meta_flow_id"], Json(found), Json(flow_json), form_id))
        raise FormError("meta_rejected", "Meta found problems with the form: %s" % found[0]["message"], found)
    graph.publish_flow(token, flow_id)
    cur.execute("update forms set meta_flow_id = %s, status = 'published', flow_json = %s, problems = '[]'::jsonb, version = version + %s, published_at = now(), updated_at = now() where id = %s",
                (flow_id, Json(flow_json), 1 if old else 0, form_id))
    if old:
        try:
            graph.deprecate_flow(token, old)
        except Exception as why:                                       # noqa: BLE001
            log.info("could not retire the earlier version %s: %s", old, why)
    return get_form(cur, tenant_id, form_id)


def retire(cur, tenant_id, graph, vault: Vault, form_id) -> dict:
    f = _row(cur, tenant_id, form_id)
    if f["status"] != "published" or not f["meta_flow_id"]:
        raise FormError("not_published", "only a published form can be retired")
    token, _waba, _pnid = commerce._token_for(cur, tenant_id, vault)
    graph.deprecate_flow(token, f["meta_flow_id"])
    cur.execute("update forms set status = 'deprecated', updated_at = now() where id = %s", (form_id,))
    return get_form(cur, tenant_id, form_id)


def sync_status(cur, tenant_id, graph, vault: Vault) -> int:
    """Read the state Meta holds for every form we gave it (Meta can throttle or block one)."""
    token, waba, _pnid = commerce._token_for(cur, tenant_id, vault)
    flows = {str(x.get("id")): x for x in graph.list_flows(token, waba)}
    n = 0
    cur.execute("select id, meta_flow_id, status from forms where tenant_id = %s and meta_flow_id is not null", (tenant_id,))
    for fid, mid, status in cur.fetchall():
        theirs = str((flows.get(mid) or {}).get("status") or "").lower()
        if theirs in STATUSES and theirs != status:
            cur.execute("update forms set status = %s, updated_at = now() where id = %s", (theirs, fid))
            n += 1
    return n


# ── sending ──────────────────────────────────────────────────────────────────

def new_token(form_id, contact_id) -> str:
    return "pf1.%s.%s.%s" % (str(form_id).replace("-", ""), str(contact_id).replace("-", ""), secrets.token_hex(4))


def read_token(token: str) -> tuple:
    """-> (form_id, contact_id) from a flow token we made; (None, None) for anything else."""
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "pf1" or len(parts[1]) != 32 or len(parts[2]) != 32:
        return None, None
    try:
        import uuid as _uuid
        return str(_uuid.UUID(parts[1])), str(_uuid.UUID(parts[2]))
    except ValueError:
        return None, None


def interactive_for(form: dict, contact_id, *, body: str, button: str = "", header: str = "", footer: str = "", prefill: Optional[dict] = None) -> dict:
    if form["status"] != "published" or not form["meta_flow_id"]:
        raise FormError("not_published", "publish the form before sending it")
    first = form["screens"][0]["id"]
    params = {"flow_message_version": "3", "flow_token": new_token(form["id"], contact_id), "flow_id": form["meta_flow_id"], "flow_cta": (button or "Open form")[:30], "mode": "published"}
    if form["endpoint"]:
        params["flow_action"] = "data_exchange"
    else:
        params["flow_action"] = "navigate"
        params["flow_action_payload"] = {"screen": first, "data": {k: str(v) for k, v in (prefill or {}).items()}} if prefill else {"screen": first}
    interactive = {"type": "flow", "body": {"text": (body or "Please fill in this short form.")[:1024]}, "action": {"name": "flow", "parameters": params}}
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return interactive


def send_form(cur, tenant_id, form_id, *, contact_id, body: str = "", button: str = "", header: str = "", footer: str = "", idem_key: str, number_id=None, sender: str = "agent",
              now: Optional[datetime] = None):
    form = _row(cur, tenant_id, form_id)
    interactive = interactive_for(form, contact_id, body=body, button=button, header=header, footer=footer)
    return send.enqueue_interactive(cur, tenant_id, contact_id=contact_id, interactive=interactive, preview="[form] %s" % form["name"], idem_key=idem_key, number_id=number_id,
                                    sender=sender, now=now)


# ── what customers filled in ─────────────────────────────────────────────────

def _define_and_set(cur, tenant_id, contact_id, key: str, value) -> None:
    defs = contacts.attribute_defs(cur, tenant_id)
    try:
        if key not in defs:
            contacts.define_attribute(cur, tenant_id, key, "string")
            defs = contacts.attribute_defs(cur, tenant_id)
        contacts.set_attribute(cur, tenant_id, contact_id, key, value if isinstance(value, str) else json.dumps(value) if isinstance(value, (list, dict)) else str(value), defs)
    except Exception as why:                                          # noqa: BLE001  (a plan limit on attributes must not lose the answers, which are kept whole)
        log.info("could not save %s on contact %s: %s", key, contact_id, why)


def record_response(cur, tenant_id, *, form_id, contact_id, conversation_id, flow_token: str, answers: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Keep one filled-in form, and do what the form says to do about it (details onto the contact, a tag, a thank-you, a webhook). Once per flow token."""
    now = now or utcnow()
    cur.execute("insert into form_responses (tenant_id, form_id, contact_id, conversation_id, flow_token, answers) values (%s, %s, %s, %s, %s, %s)"
                " on conflict (tenant_id, flow_token) where flow_token is not null do nothing returning id", (tenant_id, form_id, contact_id, conversation_id, flow_token or None, Json(answers)))
    row = cur.fetchone()
    if row is None:
        return None
    form = None
    if form_id:
        cur.execute("update forms set responses = responses + 1 where id = %s", (form_id,))
        form = _row(cur, tenant_id, form_id)
    if contact_id and form:
        for field, target in (form["mapping"] or {}).items():
            value = answers.get(field)
            if value in (None, "", []):
                continue
            if target == "@name":
                cur.execute("update contacts set name = %s where id = %s", (str(value)[:200], contact_id))
            elif target == "@email":
                cur.execute("select 1 from contacts where tenant_id = %s and lower(email) = lower(%s) and id <> %s", (tenant_id, str(value), contact_id))
                if cur.fetchone() is None:
                    cur.execute("update contacts set email = %s where id = %s", (str(value)[:200], contact_id))
            else:
                _define_and_set(cur, tenant_id, contact_id, target, value)
        if form["tag_on_submit"]:
            cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t) where id = %s", ([form["tag_on_submit"]], contact_id))
        thanks = (form["settings"] or {}).get("thank_you")
        if thanks and not form["endpoint"]:                            # a live form shows its own thank-you screen
            try:
                cur.execute("savepoint thanks")
                send.enqueue_text(cur, tenant_id, contact_id=contact_id, text=thanks, idem_key="form-thanks:%s" % row[0], sender="system", now=now)
                cur.execute("release savepoint thanks")
            except send.SendRefused:
                cur.execute("rollback to savepoint thanks")
                cur.execute("release savepoint thanks")
    webhooks_out.emit(cur, tenant_id, "flow.response", lambda: {"response_id": str(row[0]), "form_id": str(form_id) if form_id else None, "form": form["name"] if form else None,
                                                                "contact_id": str(contact_id) if contact_id else None, "answers": answers})
    return str(row[0])


def on_reply(cur, tenant_id, *, contact_id, conversation_id, event_raw: dict, wamid: str = "") -> Optional[str]:
    """An inbound `nfm_reply`: the answers of a form. Matched to our form by the flow token (or, for a form sent some other way, by the last form sent to this person)."""
    message = (event_raw.get("message") if isinstance(event_raw, dict) else None) or {}
    reply = ((message.get("interactive") or {}).get("nfm_reply")) or {}
    try:
        answers = json.loads(reply.get("response_json") or "{}")
    except ValueError:
        answers = {}
    if not isinstance(answers, dict):
        answers = {}
    token = str(answers.pop("flow_token", "") or "")
    form_id, _who = read_token(token)
    if form_id:
        cur.execute("select 1 from forms where id = %s and tenant_id = %s", (form_id, tenant_id))
        if cur.fetchone() is None:
            form_id = None
    if not form_id:
        cur.execute("select f.id from outbox o join forms f on f.meta_flow_id = o.payload->'interactive'->'action'->'parameters'->>'flow_id' and f.tenant_id = o.tenant_id"
                    " join messages m on m.id = o.message_id join conversations c on c.id = m.conversation_id where o.tenant_id = %s and c.contact_id = %s"
                    " order by o.created_at desc limit 1", (tenant_id, contact_id))
        row = cur.fetchone()
        form_id = row[0] if row else None
    return record_response(cur, tenant_id, form_id=form_id, contact_id=contact_id, conversation_id=conversation_id, flow_token=token or ("wamid:" + wamid if wamid else ""), answers=answers)


def list_responses(cur, tenant_id, form_id=None, *, limit: int = 100, offset: int = 0) -> dict:
    where, params = ["r.tenant_id = %s"], [tenant_id]
    if form_id:
        where.append("r.form_id = %s")
        params.append(form_id)
    cur.execute("select count(*) from form_responses r where " + " and ".join(where), params)
    total = cur.fetchone()[0]
    cur.execute("select r.id, r.form_id, f.name, r.contact_id, c.name, c.phone, r.answers, r.created_at from form_responses r left join forms f on f.id = r.form_id"
                " left join contacts c on c.id = r.contact_id where " + " and ".join(where) + " order by r.created_at desc limit %s offset %s",
                params + [max(1, min(int(limit), 500)), max(0, int(offset))])
    cols = ("id", "form_id", "form", "contact_id", "contact_name", "contact_phone", "answers", "created_at")
    return {"responses": [dict(zip(cols, r)) for r in cur.fetchall()], "total": total}


def export_csv(cur, tenant_id, form_id) -> str:
    form = _row(cur, tenant_id, form_id)
    names = [f["name"] for s in form["screens"] for f in s["fields"]]
    cur.execute("select r.created_at, c.name, c.phone, r.answers from form_responses r left join contacts c on c.id = r.contact_id where r.form_id = %s and r.tenant_id = %s"
                " order by r.created_at", (form_id, tenant_id))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["received", "contact", "phone"] + names)
    for at, name, phone, answers in cur.fetchall():
        w.writerow([at.isoformat(), name or "", phone or ""] + [_cell(answers.get(n)) for n in names])
    return out.getvalue()


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return ", ".join(str(x) for x in v) if isinstance(v, list) else json.dumps(v)
    text = str(v)
    return "'" + text if text[:1] in "=+-@" else text              # a spreadsheet must not run an answer as a formula
