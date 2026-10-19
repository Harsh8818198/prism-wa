"""Message templates (spec WS4): lint, create, sync, and keeping status current.

A template is created on Meta, reviewed there, and only an APPROVED one may
start a conversation. Three things live here:

  lint()      Meta's documented rules, checked BEFORE submitting, so a customer
              sees "variable at the start" now instead of a rejection tomorrow.
              Errors are things Meta refuses; warnings are rules whose exact
              threshold Meta does not publish (the variable-to-word ratio) so
              they advise rather than block.
  create()    lint -> record -> submit to Meta -> record the answer.
  sync()      pull a WABA's templates from Meta (for ones made elsewhere, and to
              catch a status webhook we missed).
  EVENT_HANDLERS   Meta's template status / quality / category webhooks, applied
              to our copy by the ingest worker.

The rules mirror the spec (§1.2, WS4): name lowercase alphanumeric + underscore,
variables `{{1}}..{{n}}` in order and with examples, none at the very start or
end of the body (error 2388299), body <= 1024, header and footer <= 60, button
text <= 25, <= 100 templates created per WABA per hour. Meta's limits can
change; they are constants at the top so one edit updates them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from psycopg2.extras import Json

from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault, VaultError

NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
VARIABLE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
CATEGORIES = ("MARKETING", "UTILITY", "AUTHENTICATION")
HEADER_FORMATS = ("TEXT", "IMAGE", "VIDEO", "DOCUMENT", "LOCATION")
MEDIA_FORMATS = ("IMAGE", "VIDEO", "DOCUMENT")

BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_TEXT_MAX = 25
BUTTONS_MAX = 10
URL_BUTTONS_MAX = 2
PHONE_BUTTONS_MAX = 1
CARDS_MIN, CARDS_MAX = 2, 10
CREATED_PER_HOUR = 100
CARD_BODY_MAX = 160
CARD_BUTTONS_MAX = 2
LTO_TEXT_MAX = 16
OTP_TYPES = ("COPY_CODE", "ONE_TAP", "ZERO_TAP")
PACKAGE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
# How long Meta may keep trying to deliver a template before giving up (message validity), in seconds, by category. -1 is Meta's default.
TTL_RANGES = {"MARKETING": (12 * 3600, 30 * 86400), "UTILITY": (30, 12 * 3600), "AUTHENTICATION": (30, 15 * 60)}
# Words that make a UTILITY message read as promotion, which Meta then re-categorises (and bills) as Marketing.
PROMO_RE = re.compile(r"\b(offers?|discounts?|sales?|\d+\s*%\s*off|coupons?|limited[- ]time|buy now|deals?|cashback|free gift|"
                      r"special price|hurry|best price|book now|shop now)\b", re.I)
# Words (outside variables) wanted per variable. Meta does not publish the
# threshold behind error 2388293, so this only ever produces a warning.
WORDS_PER_VARIABLE = 3

ERR_VARIABLE_RATIO = 2388293
ERR_VARIABLE_AT_EDGE = 2388299


@dataclass(frozen=True)
class Issue:
    severity: str                 # "error" blocks submission; "warning" advises
    code: str
    message: str
    where: str = ""
    meta_code: Optional[int] = None

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "where": self.where, "meta_code": self.meta_code}


class TemplateInvalid(Exception):
    def __init__(self, issues: list):
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues if i.severity == "error"))


class TemplateRejected(Exception):
    """Meta refused the submission. Carries Meta's own words."""

    def __init__(self, error: GraphError):
        self.error = error
        super().__init__(error.details or error.message)


class TemplateLimit(Exception):
    pass


# ── lint ─────────────────────────────────────────────────────────────────────

def _variables(text: str) -> list:
    return [m.group(1) for m in VARIABLE_RE.finditer(text or "")]


def _sequential(variables: list) -> bool:
    return variables == [str(i) for i in range(1, len(variables) + 1)]


def lint(*, name: str, language: str, category: str, components: list, ttl_seconds: Optional[int] = None) -> list:
    issues = []
    err = lambda code, msg, where="", meta=None: issues.append(Issue("error", code, msg, where, meta))   # noqa: E731
    warn = lambda code, msg, where="", meta=None: issues.append(Issue("warning", code, msg, where, meta))  # noqa: E731

    if not NAME_RE.match(name or ""):
        err("bad_name", "The name may only use lowercase letters, numbers and underscores (up to 512 characters).", "name")
    if not LANGUAGE_RE.match(language or ""):
        err("bad_language", "The language must be a code such as en_US, hi or gu.", "language")
    if category not in CATEGORIES:
        err("bad_category", "The category must be MARKETING, UTILITY or AUTHENTICATION.", "category")
    if not isinstance(components, list) or not components:
        err("no_components", "A template needs at least a body.", "components")
        return issues

    by_type = {}
    for c in components:
        kind = (c or {}).get("type") if isinstance(c, dict) else None
        if kind not in ("HEADER", "BODY", "FOOTER", "BUTTONS", "CAROUSEL", "LIMITED_TIME_OFFER"):
            err("bad_component", "Unknown component %r." % (kind,), "components")
            continue
        if kind in by_type:
            err("duplicate_component", "A template can have only one %s." % kind.lower(), kind)
            continue
        by_type[kind] = c

    body = by_type.get("BODY")
    if category == "AUTHENTICATION":
        _lint_authentication(by_type, err)
    elif "CAROUSEL" in by_type:
        _lint_carousel(by_type, body, err, warn)
    elif body is None:
        err("no_body", "A template needs a body.", "BODY")
    if "LIMITED_TIME_OFFER" in by_type:
        _lint_offer(by_type, category, err, warn)
    if category == "UTILITY" and body is not None and PROMO_RE.search(body.get("text") or ""):
        warn("looks_promotional", "This reads like a promotion. Meta is likely to re-categorise a Utility message like this as "
             "Marketing, which is billed at the Marketing rate. Make it Marketing now, or take the offer wording out.", "BODY")
    if ttl_seconds not in (None, -1):
        low, high = TTL_RANGES.get(category, (0, 0))
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not low <= ttl_seconds <= high:
            err("bad_ttl", "Message validity for %s must be between %s and %s, or left on Meta's default." % (
                category.title() if category else "this category", _span(low), _span(high)), "ttl")

    if body is not None and category != "AUTHENTICATION":
        _lint_body(body, err, warn)
    if "HEADER" in by_type:
        _lint_header(by_type["HEADER"], err)
    if "FOOTER" in by_type:
        _lint_footer(by_type["FOOTER"], err)
    if "BUTTONS" in by_type:
        _lint_buttons(by_type["BUTTONS"].get("buttons"), err, warn, category)
    return issues


def _lint_body(body: dict, err, warn) -> None:
    text = body.get("text") or ""
    if not text.strip():
        err("empty_body", "The body cannot be empty.", "BODY")
        return
    if len(text) > BODY_MAX:
        err("body_too_long", "The body is %d characters; the limit is %d." % (len(text), BODY_MAX), "BODY")
    variables = _variables(text)
    if variables:
        if not _sequential(variables):
            err("variables_not_sequential", "Variables must be {{1}}, {{2}}, {{3}} ... in order, each used once.", "BODY")
        if VARIABLE_RE.match(text.strip()):
            err("variable_at_start", "The body cannot start with a variable.", "BODY", ERR_VARIABLE_AT_EDGE)
        if re.search(VARIABLE_RE.pattern + r"\s*$", text.strip()):
            err("variable_at_end", "The body cannot end with a variable.", "BODY", ERR_VARIABLE_AT_EDGE)
        words = len(VARIABLE_RE.sub(" ", text).split())
        if words < WORDS_PER_VARIABLE * len(variables):
            warn("variable_ratio", "This body has many variables for its length; Meta may reject it. "
                 "Add more fixed text or use fewer variables.", "BODY", ERR_VARIABLE_RATIO)
        samples = ((body.get("example") or {}).get("body_text") or [[]])
        if not samples or len(samples[0]) != len(variables) or any(not str(x).strip() for x in samples[0]):
            err("missing_example", "Give a sample value for each of the %d variables." % len(variables), "BODY")


def _lint_header(header: dict, err) -> None:
    fmt = header.get("format")
    if fmt not in HEADER_FORMATS:
        err("bad_header_format", "The header format must be one of %s." % ", ".join(HEADER_FORMATS), "HEADER")
        return
    if fmt == "TEXT":
        text = header.get("text") or ""
        if not text.strip():
            err("empty_header", "The header text cannot be empty.", "HEADER")
        if len(text) > HEADER_MAX:
            err("header_too_long", "The header is %d characters; the limit is %d." % (len(text), HEADER_MAX), "HEADER")
        variables = _variables(text)
        if len(variables) > 1:
            err("header_variables", "A header can have at most one variable.", "HEADER")
        elif variables and not (header.get("example") or {}).get("header_text"):
            err("missing_example", "Give a sample value for the header variable.", "HEADER")
    elif fmt in MEDIA_FORMATS and not (header.get("example") or {}).get("header_handle"):
        err("missing_example", "A %s header needs a sample file." % fmt.lower(), "HEADER")


def _lint_footer(footer: dict, err) -> None:
    text = footer.get("text") or ""
    if len(text) > FOOTER_MAX:
        err("footer_too_long", "The footer is %d characters; the limit is %d." % (len(text), FOOTER_MAX), "FOOTER")
    if _variables(text):
        err("footer_variables", "A footer cannot contain variables.", "FOOTER")


def _lint_buttons(buttons, err, warn, category: str) -> None:
    if not isinstance(buttons, list) or not buttons:
        err("no_buttons", "A buttons component needs at least one button.", "BUTTONS")
        return
    if len(buttons) > BUTTONS_MAX:
        err("too_many_buttons", "At most %d buttons." % BUTTONS_MAX, "BUTTONS")
    urls = phones = 0
    for i, b in enumerate(buttons):
        b = b if isinstance(b, dict) else {}
        kind, text = b.get("type"), (b.get("text") or "")
        where = "BUTTONS[%d]" % i
        if kind == "OTP":
            _lint_otp(b, err, where)
            continue
        if kind not in ("QUICK_REPLY", "URL", "PHONE_NUMBER", "COPY_CODE", "FLOW", "CATALOG", "MPM", "SPM"):
            err("bad_button", "Unknown button type %r." % (kind,), where)
            continue
        if not text.strip():
            err("button_text_empty", "Every button needs text.", where)
        if len(text) > BUTTON_TEXT_MAX:
            err("button_text_too_long", "Button text is limited to %d characters." % BUTTON_TEXT_MAX, where)
        if kind == "URL":
            urls += 1
            url = b.get("url") or ""
            if not url.startswith(("http://", "https://")):
                err("bad_url", "A URL button needs a full web address (https://...).", where)
            dynamic = _variables(url)
            if len(dynamic) > 1 or (dynamic and not url.rstrip().endswith("{{1}}")):
                err("bad_url_variable", "A URL can have one variable, {{1}}, at the very end.", where)
            if dynamic and not b.get("example"):
                err("missing_example", "Give a sample value for the URL variable.", where)
        elif kind == "PHONE_NUMBER":
            phones += 1
            if not re.match(r"^\+\d{6,15}$", b.get("phone_number") or ""):
                err("bad_phone", "A phone button needs a number like +919876543210.", where)
        elif kind == "COPY_CODE" and not b.get("example"):
            err("missing_example", "A copy-code button needs a sample code.", where)
    if urls > URL_BUTTONS_MAX:
        err("too_many_urls", "At most %d URL buttons." % URL_BUTTONS_MAX, "BUTTONS")
    if phones > PHONE_BUTTONS_MAX:
        err("too_many_phones", "At most %d phone-number button." % PHONE_BUTTONS_MAX, "BUTTONS")
    quick = [i for i, b in enumerate(buttons) if isinstance(b, dict) and b.get("type") == "QUICK_REPLY"]
    if quick and quick != list(range(quick[0], quick[0] + len(quick))):
        warn("quick_replies_not_together", "Meta expects quick-reply buttons to sit together.", "BUTTONS")


def _lint_authentication(by_type: dict, err) -> None:
    buttons = (by_type.get("BUTTONS") or {}).get("buttons") or []
    if not any(isinstance(b, dict) and b.get("type") == "OTP" for b in buttons):
        err("auth_needs_otp_button", "An authentication template needs a one-tap, zero-tap or copy-code button.", "BUTTONS")
    if len(buttons) > 1:
        err("auth_one_button", "An authentication template has just the one code button.", "BUTTONS")
    if by_type.get("BODY") is None:
        err("no_body", "An authentication template needs its body (it carries the security note setting).", "BODY")
    elif (by_type["BODY"] or {}).get("text"):
        err("auth_body_fixed", "An authentication template's body is fixed by Meta; only the security note and "
            "code expiry can be set.", "BODY")
    if "HEADER" in by_type:
        err("auth_no_header", "An authentication template cannot have a header.", "HEADER")
    footer = by_type.get("FOOTER") or {}
    if footer.get("text"):
        err("auth_footer_fixed", "An authentication template's footer is fixed by Meta; only the code expiry can be set.", "FOOTER")
    minutes = footer.get("code_expiration_minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 90):
        err("bad_code_expiry", "The code expiry must be from 1 to 90 minutes.", "FOOTER")


def _lint_otp(b: dict, err, where: str) -> None:
    kind = b.get("otp_type")
    if kind not in OTP_TYPES:
        err("bad_otp_type", "The code button must be copy code, one-tap autofill or zero-tap.", where)
        return
    if len(b.get("text") or "") > BUTTON_TEXT_MAX:
        err("button_text_too_long", "Button text is limited to %d characters." % BUTTON_TEXT_MAX, where)
    if kind == "COPY_CODE":
        return
    if not PACKAGE_RE.match(b.get("package_name") or ""):
        err("bad_package", "Give the Android app's package name, like com.example.app.", where)
    if len(b.get("signature_hash") or "") != 11:
        err("bad_signature_hash", "The app signature hash is 11 characters.", where)
    if kind == "ONE_TAP" and not (b.get("autofill_text") or "").strip():
        err("autofill_text_empty", "A one-tap button needs the text of its autofill button.", where)
    if len(b.get("autofill_text") or "") > BUTTON_TEXT_MAX:
        err("button_text_too_long", "Autofill text is limited to %d characters." % BUTTON_TEXT_MAX, where)
    if kind == "ZERO_TAP" and b.get("zero_tap_terms_accepted") is not True:
        err("zero_tap_terms", "Zero-tap needs your agreement to Meta's zero-tap terms.", where)


def _span(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return "%d days" % (seconds // 86400)
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%d hours" % (seconds // 3600)
    if seconds >= 60 and seconds % 60 == 0:
        return "%d minutes" % (seconds // 60)
    return "%d seconds" % seconds


def _lint_offer(by_type: dict, category: str, err, warn) -> None:
    """A limited-time-offer template: an image or video header, the offer text, optionally a countdown, a coupon and a link."""
    if category != "MARKETING":
        err("offer_is_marketing", "A limited-time offer is a Marketing template.", "LIMITED_TIME_OFFER")
    offer = (by_type["LIMITED_TIME_OFFER"] or {}).get("limited_time_offer") or {}
    text = (offer.get("text") or "").strip()
    if not text:
        err("offer_text_empty", "Say what the offer is, in a few words.", "LIMITED_TIME_OFFER")
    elif len(text) > LTO_TEXT_MAX:
        err("offer_text_too_long", "The offer text is %d characters; the limit is %d." % (len(text), LTO_TEXT_MAX), "LIMITED_TIME_OFFER")
    if "has_expiration" in offer and not isinstance(offer["has_expiration"], bool):
        err("offer_expiry_flag", "has_expiration must be true or false.", "LIMITED_TIME_OFFER")
    if (by_type.get("HEADER") or {}).get("format") not in ("IMAGE", "VIDEO"):
        err("offer_needs_media", "A limited-time offer needs an image or video header.", "HEADER")
    kinds = {b.get("type") for b in (by_type.get("BUTTONS") or {}).get("buttons", []) if isinstance(b, dict)}
    if not kinds & {"COPY_CODE", "URL"}:
        warn("offer_needs_action", "Meta expects an offer to have a copy-code (coupon) or a website button.", "BUTTONS")


def _lint_carousel(by_type: dict, body: Optional[dict], err, warn) -> None:
    if body is None:
        err("no_body", "A carousel template needs an intro body.", "BODY")
    cards = (by_type["CAROUSEL"] or {}).get("cards")
    if not isinstance(cards, list) or not CARDS_MIN <= len(cards) <= CARDS_MAX:
        err("bad_card_count", "A carousel needs %d to %d cards." % (CARDS_MIN, CARDS_MAX), "CAROUSEL")
        return
    layouts, layouts_formats = set(), set()
    for i, card in enumerate(cards):
        comps = {c.get("type"): c for c in (card or {}).get("components", []) if isinstance(c, dict)}
        where = "CAROUSEL[%d]" % i
        head = comps.get("HEADER") or {}
        if head.get("format") not in ("IMAGE", "VIDEO"):
            err("card_needs_media", "Every card needs an image or video header.", where)
        elif not (head.get("example") or {}).get("header_handle"):
            err("missing_example", "Card %d needs a sample picture or video." % (i + 1), where)
        layouts_formats.add(head.get("format"))
        if "BODY" not in comps:
            err("card_needs_body", "Every card needs a body.", where)
        else:
            card_text = comps["BODY"].get("text") or ""
            if not card_text.strip():
                err("card_body_empty", "Card %d needs some text." % (i + 1), where)
            if len(card_text) > CARD_BODY_MAX:
                err("card_body_too_long", "Card %d's text is %d characters; the limit is %d." % (i + 1, len(card_text), CARD_BODY_MAX), where)
            card_vars = _variables(card_text)
            if card_vars and not _sequential(card_vars):
                err("variables_not_sequential", "Variables in card %d must be {{1}}, {{2}} ... in order." % (i + 1), where)
            if card_vars and len((((comps["BODY"].get("example") or {}).get("body_text") or [[]])[0]) or []) != len(card_vars):
                err("missing_example", "Give a sample value for each variable in card %d." % (i + 1), where)
        card_buttons = (comps.get("BUTTONS") or {}).get("buttons", [])
        if len(card_buttons) > CARD_BUTTONS_MAX:
            err("card_too_many_buttons", "A card has at most %d buttons." % CARD_BUTTONS_MAX, where)
        for b in card_buttons:
            if isinstance(b, dict) and b.get("type") not in ("QUICK_REPLY", "URL", "PHONE_NUMBER"):
                err("bad_button", "A card button is a quick reply, a website or a phone number.", where)
        layouts.add(tuple(b.get("type") for b in card_buttons if isinstance(b, dict)))
    if len(layouts) > 1:
        err("card_buttons_differ", "Every card must have the same buttons.", "CAROUSEL")
    if len(layouts_formats) > 1:
        err("card_media_differs", "Every card must use the same kind of media: all pictures or all videos.", "CAROUSEL")


def errors(issues: list) -> list:
    return [i for i in issues if i.severity == "error"]


# ── storing, submitting, syncing ─────────────────────────────────────────────

def _waba_token(cur, vault: Vault, tenant_id, waba_row_id) -> tuple:
    cur.execute("select waba_id, token_enc from wabas where id = %s and tenant_id = %s", (waba_row_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise LookupError("that WhatsApp Business Account is not in this workspace")
    try:
        return row[0], vault.decrypt(row[1], context=row[0])
    except VaultError as exc:
        raise LookupError("this account has no usable token: %s" % exc) from exc


def create_template(conn, *, graph: Graph, vault: Vault, tenant_id, waba_row_id, name: str, language: str,
                    category: str, components: list, ttl_seconds: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """Lint, record, submit to Meta, record Meta's answer. Raises TemplateInvalid
    (nothing recorded), TemplateLimit, or TemplateRejected (recorded as DRAFT with
    Meta's reason, so the user can fix it and resubmit)."""
    now = now or utcnow()
    ttl_seconds = None if ttl_seconds in (None, -1, 0) else ttl_seconds
    problems = lint(name=name, language=language, category=category, components=components, ttl_seconds=ttl_seconds)
    if errors(problems):
        raise TemplateInvalid(problems)
    try:
        with conn.cursor() as cur:
            meta_waba, token = _waba_token(cur, vault, tenant_id, waba_row_id)
            cur.execute("select count(*) from templates where waba_id = %s and created_at > %s",
                        (waba_row_id, now - timedelta(hours=1)))
            if cur.fetchone()[0] >= CREATED_PER_HOUR:
                raise TemplateLimit("Meta allows %d new templates per hour per account; try again later." % CREATED_PER_HOUR)
            cur.execute("select id, status from templates where waba_id = %s and name = %s and language = %s",
                        (waba_row_id, name, language))
            existing = cur.fetchone()
            if existing and existing[1] not in ("DRAFT", "REJECTED"):
                raise TemplateInvalid([Issue("error", "exists", "A template with this name and language already "
                                             "exists (%s). Choose another name." % existing[1], "name")])
            if existing:
                template_id = existing[0]
                cur.execute("update templates set category = %s, components = %s, status = 'DRAFT', ttl_seconds = %s,"
                            " reject_reason = null where id = %s", (category, Json(components), ttl_seconds, template_id))
            else:
                cur.execute("insert into templates (tenant_id, waba_id, name, language, category, components, ttl_seconds)"
                            " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                            (tenant_id, waba_row_id, name, language, category, Json(components), ttl_seconds))
                template_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    try:
        payload = {"name": name, "language": language, "category": category, "components": components}
        if ttl_seconds:
            payload["message_send_ttl_seconds"] = ttl_seconds
        reply = graph.create_template(token, meta_waba, payload)
    except GraphError as exc:
        with conn.cursor() as cur:
            cur.execute("update templates set last_error = %s where id = %s", (Json(exc.as_dict()), template_id))
        conn.commit()
        raise TemplateRejected(exc) from exc
    with conn.cursor() as cur:
        cur.execute("update templates set meta_id = %s, status = %s, category = coalesce(%s, category),"
                    " last_error = null where id = %s",
                    (str(reply.get("id") or "") or None, str(reply.get("status") or "PENDING").upper(),
                     reply.get("category"), template_id))
    conn.commit()
    return {"id": str(template_id), "status": str(reply.get("status") or "PENDING").upper(),
            "meta_id": reply.get("id"), "issues": [i.as_dict() for i in problems]}


def delete_template(conn, *, graph: Graph, vault: Vault, tenant_id, template_id) -> dict:
    """Remove a template at Meta (that one language only) and here. A draft that never reached Meta is only
    removed here. Refused while a campaign that is scheduled, sending or paused still points at it."""
    with conn.cursor() as cur:
        cur.execute("select waba_id, name, meta_id from templates where id = %s and tenant_id = %s", (template_id, tenant_id))
        row = cur.fetchone()
        if row is None:
            raise LookupError("that template is not in this workspace")
        waba_row_id, name, meta_id = row
        cur.execute("select count(*) from campaigns where template_id = %s and status in ('scheduled', 'sending', 'paused')",
                    (template_id,))
        if cur.fetchone()[0]:
            raise TemplateInvalid([Issue("error", "in_use", "A campaign that is scheduled, sending or paused uses this "
                                         "template. Cancel that campaign first.", "name")])
        if meta_id:
            meta_waba, token = _waba_token(cur, vault, tenant_id, waba_row_id)
    if meta_id:
        try:
            graph.delete_template(token, meta_waba, name, hsm_id=str(meta_id))
        except GraphError as exc:
            raise TemplateRejected(exc) from exc
    with conn.cursor() as cur:
        cur.execute("delete from templates where id = %s and tenant_id = %s", (template_id, tenant_id))
    conn.commit()
    return {"deleted": True}


def _upsert_from_meta(cur, tenant_id, waba_row_id, item: dict) -> None:
    quality = (item.get("quality_score") or {}).get("score") if isinstance(item.get("quality_score"), dict) else None
    reason = item.get("rejected_reason")
    cur.execute(
        "insert into templates (tenant_id, waba_id, meta_id, name, language, category, status, quality,"
        " components, reject_reason) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (waba_id, name, language) do update set meta_id = excluded.meta_id,"
        " category = excluded.category, status = excluded.status, quality = excluded.quality,"
        " components = excluded.components, reject_reason = excluded.reject_reason, last_error = null",
        (tenant_id, waba_row_id, str(item.get("id") or "") or None, item.get("name"), item.get("language"),
         str(item.get("category") or "UTILITY").upper(), str(item.get("status") or "PENDING").upper(),
         quality, Json(item.get("components") or []), None if reason in (None, "", "NONE") else reason))


def sync_templates(conn, *, graph: Graph, vault: Vault, tenant_id, waba_row_id, max_pages: int = 50) -> int:
    """Pull every template of a WABA from Meta into our table. -> how many."""
    with conn.cursor() as cur:
        meta_waba, token = _waba_token(cur, vault, tenant_id, waba_row_id)
    count, after = 0, ""
    for _ in range(max_pages):
        page = graph.list_templates(token, meta_waba, after=after)
        with conn.cursor() as cur:
            for item in page.get("data") or []:
                if isinstance(item, dict) and item.get("name") and item.get("language"):
                    _upsert_from_meta(cur, tenant_id, waba_row_id, item)
                    count += 1
        conn.commit()
        paging = page.get("paging") or {}
        after = ((paging.get("cursors") or {}).get("after")) if paging.get("next") else ""
        if not after:
            break
    return count


# ── what Meta tells us ───────────────────────────────────────────────────────

def _waba_row(cur, tenant_id, meta_waba: str):
    cur.execute("select id from wabas where waba_id = %s and tenant_id = %s", (meta_waba, tenant_id))
    row = cur.fetchone()
    return row[0] if row else None


def _find(cur, waba_row, value: dict):
    tid = str(value.get("message_template_id") or "")
    if tid:
        cur.execute("select id from templates where waba_id = %s and meta_id = %s", (waba_row, tid))
        row = cur.fetchone()
        if row:
            return row[0]
    cur.execute("select id from templates where waba_id = %s and name = %s and language = %s",
                (waba_row, value.get("message_template_name"), value.get("message_template_language")))
    row = cur.fetchone()
    return row[0] if row else None


STATUS_ALIASES = {"REINSTATED": "APPROVED", "FLAGGED": "APPROVED"}


def on_template_status(cur, tenant_id, event: dict) -> None:
    value = (event["raw"] or {}).get("value") or {}
    waba = _waba_row(cur, tenant_id, event["waba_id"] or "")
    if waba is None:
        return
    status = str(value.get("event") or "").upper()
    status = STATUS_ALIASES.get(status, status)
    reason = value.get("reason")
    reason = None if reason in (None, "", "NONE") else reason
    template_id = _find(cur, waba, value)
    if template_id is None:
        if value.get("message_template_name") and value.get("message_template_language"):
            cur.execute("insert into templates (tenant_id, waba_id, meta_id, name, language, category, status,"
                        " reject_reason) values (%s, %s, %s, %s, %s, 'UTILITY', %s, %s)"
                        " on conflict (waba_id, name, language) do nothing",
                        (tenant_id, waba, str(value.get("message_template_id") or "") or None,
                         value["message_template_name"], value["message_template_language"], status or "PENDING", reason))
        return
    cur.execute("update templates set status = %s, reject_reason = %s,"
                " meta_id = coalesce(meta_id, %s) where id = %s returning name, language",
                (status or "PENDING", reason, str(value.get("message_template_id") or "") or None, template_id))
    changed = cur.fetchone()
    if changed:
        from prism_wa import webhooks_out
        webhooks_out.emit(cur, tenant_id, "template.status", {"name": changed[0], "language": changed[1], "status": status or "PENDING", "reason": reason})


def on_template_quality(cur, tenant_id, event: dict) -> None:
    value = (event["raw"] or {}).get("value") or {}
    waba = _waba_row(cur, tenant_id, event["waba_id"] or "")
    template_id = _find(cur, waba, value) if waba else None
    if template_id:
        cur.execute("update templates set quality = %s where id = %s",
                    (str(value.get("new_quality_score") or "UNKNOWN").upper(), template_id))


def on_template_category(cur, tenant_id, event: dict) -> None:
    value = (event["raw"] or {}).get("value") or {}
    waba = _waba_row(cur, tenant_id, event["waba_id"] or "")
    template_id = _find(cur, waba, value) if waba else None
    new = str(value.get("new_category") or "").upper()
    if template_id and new in CATEGORIES:
        cur.execute("select name, language, category from templates where id = %s for update", (template_id,))
        name, language, old = cur.fetchone()
        cur.execute("update templates set category = %s where id = %s", (new, template_id))
        if old != new:
            from prism_wa import campaign_book
            campaign_book.on_category_change(cur, tenant_id, name, language, old, new)      # a re-label must not bill campaigns unnoticed


EVENT_HANDLERS = {"template_status": on_template_status, "template_quality": on_template_quality,
                  "template_category": on_template_category}


# ── is it sendable? ──────────────────────────────────────────────────────────

def sendable_status(cur, number_id, name: str, language: str) -> Optional[str]:
    """The status of this template on the number's WABA, or None when we have never seen it."""
    cur.execute("select t.status from templates t join phone_numbers p on p.waba_id = t.waba_id"
                " where p.id = %s and t.name = %s and t.language = %s", (number_id, name, language))
    row = cur.fetchone()
    return row[0] if row else None
