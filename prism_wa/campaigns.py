"""The campaign engine (spec WS7): audience -> preflight -> schedule -> send -> measure.

A campaign sends ONE approved template to an audience. Its life:

    draft -> (scheduled) -> sending -> done          also: paused, cancelled

create     records what to send, to whom (a filter, a segment, tags, or a list of
           contacts), with which values for the template's variables, and when.
preflight  answers "is this safe to launch?" with BLOCKING problems (template not
           approved, a variable with no value, no number, over Meta's daily limit,
           low-quality template or number) and warnings, plus the audience size
           and a cost estimate. Nothing is sent by asking.
launch     takes the audience SNAPSHOT (campaign_recipients: who, and the exact
           values each person gets), then queues one outbox message per person.
           In the future it queues a job for the scheduled time instead.
pause / resume / cancel   take effect within one send batch: the send engine does
           not pick up messages of a paused or cancelled campaign.
report     the numbers: sent, delivered, read, replied, clicked, failed, skipped,
           and an estimate of what it will cost.

Variable values come from a mapping per template variable:
    {"1": {"source": "field", "field": "first_name"},
     "2": {"source": "attribute", "key": "City", "default": "your city"},
     "3": {"source": "static", "value": "Diwali"},
     "4": {"source": "tracked_link", "target": "https://example.com/offer"}}
`field` is name, first_name, last_name or mobile. A tracked link becomes a short
token (the template's URL button points at <gateway>/r/{{1}}); the redirect
records who clicked.

NOT built yet: recurring schedules, per-recipient local send time, automatic
resume the next day when a campaign exceeds Meta's daily limit (it is blocked at
preflight instead), and ROI (needs payments).
"""
from __future__ import annotations

import re
import secrets as pysecrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from prism_wa import contacts, jobs, plans, segments, send, templates, timezones
from prism_wa.jobs import utcnow

STATUSES = ("draft", "scheduled", "sending", "paused", "done", "cancelled", "failed")
MAX_SCHEDULE_AHEAD = timedelta(days=60)
MAX_RETRIES = 3
MAX_RETRY_DELAY_MINUTES = 24 * 60
# Contacts Meta lets a number message to in 24 hours, by tier. Unknown tier is treated
# as the lowest: an unverified account is held to 250.
TIER_LIMITS = {"TIER_250": 250, "TIER_1K": 1000, "TIER_2K": 2000, "TIER_10K": 10000,
               "TIER_100K": 100000, "TIER_UNLIMITED": None}
# Estimated per-message prices in INR before GST. These are the THIRD-PARTY figures in the spec
# (§1.2); confirm against Meta's own rate card (spec §9 item 2) before showing them as a quote.
RATE_CARD_ESTIMATE = {"MARKETING": 0.8631, "UTILITY": 0.1150, "AUTHENTICATION": 0.1150}
GST = 0.18
CLICK_PAYLOAD = re.compile(r"^cmp:([0-9a-f-]{36}):([0-9a-f-]{36}):(\d+)$")
VARIABLE_RE = templates.VARIABLE_RE


class CampaignError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass
class Preflight:
    blocking: list = field(default_factory=list)       # [{"code", "message"}]
    warnings: list = field(default_factory=list)
    recipients: int = 0
    opted_out_removed: int = 0
    cost: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blocking": self.blocking, "warnings": self.warnings, "recipients": self.recipients,
                "opted_out_removed": self.opted_out_removed, "cost": self.cost}


# ── create ───────────────────────────────────────────────────────────────────

def _local_to_utc(when, tz: Optional[str]) -> Optional[datetime]:
    if when is None:
        return None
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        try:
            when = when.replace(tzinfo=ZoneInfo(tz or "Asia/Kolkata"))
        except Exception:                                  # noqa: BLE001  (unknown zone name)
            raise CampaignError("bad_timezone", "unknown time zone %r" % tz) from None
    return when.astimezone(timezone.utc)


def create_campaign(cur, tenant_id, *, name: str, template_name: str, language: str = "en_US", audience: dict,
                    variables: Optional[dict] = None, number_id=None, schedule_at=None, timezone_name: Optional[str] = None,
                    retry: Optional[dict] = None, track_clicks: bool = True, now: Optional[datetime] = None,
                    local_time: Optional[str] = None, schedule_id=None):
    now = now or utcnow()
    if not (name or "").strip():
        raise CampaignError("name_required", "give the campaign a name")
    audience = audience or {}
    if not any(k in audience for k in ("filter", "segment_id", "tags", "contact_ids")):
        raise CampaignError("audience_required", "choose who this goes to")
    if retry is None and not plans.has(plans.plan_of(cur, tenant_id), "auto_retry"):
        retry = {"max": 0}                                  # the plan has no automatic retry: one attempt
    elif retry and retry.get("max", 0) > 0:
        plans.require(cur, tenant_id, "auto_retry", "Automatic retry of failed messages")
    retry = {"max": 3, "delay_minutes": 30, **(retry or {})}
    if not (isinstance(retry["max"], int) and 0 <= retry["max"] <= MAX_RETRIES):
        raise CampaignError("bad_retry", "retries can be 0 to %d" % MAX_RETRIES)
    if not (isinstance(retry["delay_minutes"], int) and 1 <= retry["delay_minutes"] <= MAX_RETRY_DELAY_MINUTES):
        raise CampaignError("bad_retry", "the retry delay can be 1 minute to 24 hours")
    when = _local_to_utc(schedule_at, timezone_name)
    if when:
        plans.require(cur, tenant_id, "scheduler", "Scheduling a campaign")
    if local_time is not None:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(local_time)):
            raise CampaignError("bad_local_time", "the local send time must look like 10:00")
        plans.require(cur, tenant_id, "scheduler", "Sending at each person's local time")
    if when and when - now > MAX_SCHEDULE_AHEAD:
        raise CampaignError("too_far", "a campaign can be scheduled at most 60 days ahead")
    if any(isinstance(v, dict) and v.get("source") == "tracked_link" for v in (variables or {}).values()):
        plans.require(cur, tenant_id, "click_tracking", "Click tracking")
    cur.execute("select id from templates t where t.tenant_id = %s and t.name = %s and t.language = %s"
                " order by t.created_at limit 1", (tenant_id, template_name, language))
    row = cur.fetchone()
    cur.execute(
        "insert into campaigns (tenant_id, name, template_name, language, template_id, audience, variables, number_id,"
        " schedule_at, timezone, retry, track_clicks, status, segment_name, total, local_time, schedule_id)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, 0, %s, %s) returning id",
        (tenant_id, name.strip(), template_name, language, row[0] if row else None, Json(audience), Json(variables or {}),
         number_id, when, timezone_name, Json(retry), track_clicks, audience.get("segment_name") or "", local_time, schedule_id))
    return cur.fetchone()[0]


def _campaign(cur, tenant_id, campaign_id, *, lock: bool = False) -> dict:
    cur.execute("select id, name, template_name, language, template_id, audience, variables, number_id, schedule_at,"
                " timezone, retry, track_clicks, status, local_time from campaigns where id = %s and tenant_id = %s"
                + (" for update" if lock else ""), (campaign_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CampaignError("unknown_campaign", "that campaign is not in this workspace")
    return dict(zip(("id", "name", "template_name", "language", "template_id", "audience", "variables", "number_id",
                     "schedule_at", "timezone", "retry", "track_clicks", "status", "local_time"), row))


# ── audience ─────────────────────────────────────────────────────────────────

def resolve_audience(cur, tenant_id, audience: dict, *, now: Optional[datetime] = None) -> tuple:
    """-> (contact ids who may be messaged, how many matched but were removed as opted out/blocked/unreachable)."""
    if "contact_ids" in audience:
        wanted = [str(c) for c in audience["contact_ids"]]
        cur.execute("select id from contacts where tenant_id = %s and id = any(%s::uuid[])", (tenant_id, wanted))
        everyone = [r[0] for r in cur.fetchall()]
        filt = {"field": "__ids__"}
        cur.execute("select id from contacts where tenant_id = %s and id = any(%s::uuid[]) and opt_out = false"
                    " and blocked = false and (phone is not null or bsuid is not null) order by id",
                    (tenant_id, [str(i) for i in everyone]))
        reachable = [r[0] for r in cur.fetchall()]
        return reachable, len(everyone) - len(reachable)
    if "segment_id" in audience:
        filt = {"field": "segment", "op": "in", "value": str(audience["segment_id"])}
    elif "filter" in audience:
        filt = audience["filter"]
    else:
        filt = {"tags": audience.get("tags") or []}
    everyone = segments.resolve(cur, tenant_id, filt, now=now)
    reachable = segments.resolve(cur, tenant_id, filt, now=now, opted_in_only=True)
    return reachable, len(everyone) - len(reachable)


# ── preflight ────────────────────────────────────────────────────────────────

def _template_variables(components: list) -> dict:
    """Which variables the template uses, by place: {'body': [1, 2], 'header': [1], 'url': {0: True}}."""
    found = {"body": [], "header": [], "url_buttons": [], "header_media": None, "quick_replies": 0, "copy_code": False}
    for c in components or []:
        kind = c.get("type")
        if kind == "BODY":
            found["body"] = [int(v) for v in VARIABLE_RE.findall(c.get("text") or "") if v.isdigit()]
        elif kind == "HEADER":
            if c.get("format") == "TEXT":
                found["header"] = [int(v) for v in VARIABLE_RE.findall(c.get("text") or "") if v.isdigit()]
            elif c.get("format") in templates.MEDIA_FORMATS:
                found["header_media"] = c["format"].lower()
        elif kind == "BUTTONS":
            for i, b in enumerate(c.get("buttons") or []):
                if b.get("type") == "URL" and VARIABLE_RE.search(b.get("url") or ""):
                    found["url_buttons"].append(i)
                elif b.get("type") == "QUICK_REPLY":
                    found["quick_replies"] += 1
                elif b.get("type") == "COPY_CODE":
                    found["copy_code"] = True
    return found


def _category(cur, template_id) -> str:
    cur.execute("select category from templates where id = %s", (template_id,))
    row = cur.fetchone()
    return row[0] if row else "MARKETING"


def preflight(cur, tenant_id, campaign_id, *, now: Optional[datetime] = None) -> Preflight:
    now = now or utcnow()
    c = _campaign(cur, tenant_id, campaign_id)
    out = Preflight()
    block = lambda code, msg: out.blocking.append({"code": code, "message": msg})      # noqa: E731
    warn = lambda code, msg: out.warnings.append({"code": code, "message": msg})       # noqa: E731

    template = None
    if c["template_id"]:
        cur.execute("select status, quality, category, components from templates where id = %s", (c["template_id"],))
        row = cur.fetchone()
        template = dict(zip(("status", "quality", "category", "components"), row)) if row else None
    if template is None:
        block("template_unknown", "The template \"%s\" (%s) is not known for this account. Sync your templates first."
              % (c["template_name"], c["language"]))
    else:
        if template["status"] != "APPROVED":
            block("template_not_approved", "The template is %s; only an approved template can be sent."
                  % template["status"].lower())
        if (template["quality"] or "").upper() == "RED":
            block("template_quality_red", "This template's quality rating is low (red); Meta may stop delivering it.")
        elif (template["quality"] or "").upper() == "YELLOW":
            warn("template_quality_yellow", "This template's quality rating is medium (yellow).")
        used = _template_variables(template["components"])
        mapping = c["variables"] or {}
        needed = sorted(set(used["body"]) | set(used["header"]))
        for v in needed:
            if str(v) not in mapping:
                block("variable_unmapped", "Template variable {{%d}} has no value. Choose a field, an attribute or text." % v)
        for i in used["url_buttons"]:
            if str(100 + i) not in mapping and "url_%d" % i not in mapping:
                block("url_button_unmapped", "URL button %d needs a value (a tracked link or text)." % (i + 1))
        if used["header_media"] and not ((mapping.get("header") or {}).get("link") or (mapping.get("header") or {}).get("asset_id")):
            block("header_media_missing", "The template has a %s header; choose the file to send." % used["header_media"])
        for key, spec in mapping.items():
            problem = _mapping_problem(spec)
            if problem:
                block("variable_invalid", "Variable %s: %s" % (key, problem))

    # the number
    cur.execute("select p.id, p.quality, w.tier from phone_numbers p join wabas w on w.id = p.waba_id"
                " where p.tenant_id = %s and (%s::uuid is null or p.id = %s::uuid)",
                (tenant_id, c["number_id"], c["number_id"]))
    numbers = cur.fetchall()
    if not numbers:
        block("no_number", "This workspace has no connected WhatsApp number.")
    elif len(numbers) > 1:
        block("which_number", "This workspace has several numbers; choose which one sends this campaign.")

    # the audience
    try:
        audience, removed = resolve_audience(cur, tenant_id, c["audience"], now=now)
    except segments.FilterError as exc:
        block("bad_audience", str(exc))
        audience, removed = [], 0
    out.recipients, out.opted_out_removed = len(audience), removed
    if not audience and not any(b["code"] == "bad_audience" for b in out.blocking):
        block("no_recipients", "Nobody in this audience can be messaged (everyone is opted out, blocked or unreachable).")
    if removed:
        warn("opted_out_removed", "%d people were left out: opted out, blocked or without a number." % removed)

    if numbers and len(numbers) == 1:
        _, number_quality, tier = numbers[0]
        if (number_quality or "").upper() == "RED":
            block("number_quality_red", "This number's quality rating is low (red); sending is risky.")
        limit = TIER_LIMITS.get((tier or "").upper(), 250)
        if limit is not None:
            cur.execute("select count(distinct coalesce(o.to_phone, o.to_bsuid)) from outbox o"
                        " join phone_numbers p on p.id = o.number_id join wabas w on w.id = p.waba_id"
                        " where w.id = (select waba_id from phone_numbers where id = %s)"
                        " and o.state in ('sent', 'sending') and o.updated_at > %s and o.kind = 'template'",
                        (numbers[0][0], now - timedelta(hours=24)))
            already = cur.fetchone()[0]
            if already + len(audience) > limit:
                block("tier_limit", "Meta allows this account to message %d people per day; %d already were in the last "
                      "24 hours and this campaign adds %d. Reduce the audience or wait."
                      % (limit, already, len(audience)))
            elif not tier:
                warn("tier_unknown", "Your daily messaging limit is not known; assuming %d." % limit)

    if c["schedule_at"]:
        if c["schedule_at"] <= now:
            warn("schedule_past", "The scheduled time has passed, so this will start as soon as you launch it.")
        elif c["schedule_at"] - now > MAX_SCHEDULE_AHEAD:
            block("too_far", "A campaign can be scheduled at most 60 days ahead.")

    category = (template or {}).get("category", "MARKETING")
    rate = RATE_CARD_ESTIMATE.get(category, RATE_CARD_ESTIMATE["MARKETING"])
    out.cost = {"category": category, "per_message": rate, "gst": GST, "currency": "INR", "recipients": len(audience),
                "total": round(len(audience) * rate * (1 + GST), 2), "is_estimate": True,
                "basis": "third-party per-message rates plus 18% GST; confirm against Meta's rate card"}
    cur.execute("update campaigns set preflight = %s where id = %s", (Json(out.as_dict()), campaign_id))
    return out


def _mapping_problem(spec) -> Optional[str]:
    if not isinstance(spec, dict):
        return "must be an object"
    source = spec.get("source")
    if source == "field":
        return None if spec.get("field") in ("name", "first_name", "last_name", "mobile") else \
            "field must be name, first_name, last_name or mobile"
    if source == "attribute":
        return None if re.match(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", str(spec.get("key") or "")) else "attribute name is not valid"
    if source == "static":
        return None if str(spec.get("value") or "").strip() else "text cannot be empty"
    if source == "tracked_link":
        return None if str(spec.get("target") or "").startswith(("http://", "https://")) else "target must be a web address"
    if "asset_id" in spec and not source:                   # the header file chosen from the Media Library
        return None if re.match(r"^[0-9a-fA-F-]{36}$", str(spec["asset_id"])) else "the header file is not a Media Library file"
    if "link" in spec and not source:                       # the header media entry
        return None if str(spec["link"]).startswith(("http://", "https://")) else "the file link must be a web address"
    return "unknown source %r" % (source,)


# ── building each person's message ───────────────────────────────────────────

def _first_last(name: str) -> tuple:
    parts = (name or "").split()
    return (parts[0] if parts else ""), (parts[-1] if len(parts) > 1 else "")


def _value(spec: dict, contact: dict, attrs: dict, make_link) -> str:
    source = spec["source"]
    if source == "field":
        first, last = _first_last(contact["name"])
        got = {"name": contact["name"], "first_name": first, "last_name": last, "mobile": contact["phone"] or ""}[spec["field"]]
        return (got or spec.get("default") or "").strip() or "there"
    if source == "attribute":
        got = attrs.get(spec["key"])
        return str(got) if got not in (None, "") else str(spec.get("default") or "").strip() or "-"
    if source == "static":
        return str(spec["value"])
    return make_link(spec["target"])


def build_components(template_components: list, mapping: dict, contact: dict, attrs: dict, *, make_link,
                     campaign_id, recipient_id) -> list:
    """The `components` array Meta wants for this person: body/header/URL-button values, and tracking
    payloads on quick-reply buttons."""
    used = _template_variables(template_components)
    out = []
    header = mapping.get("header") or {}
    if used["header_media"]:
        kind = used["header_media"]
        ref = {"asset_id": str(header["asset_id"])} if header.get("asset_id") else {"link": header["link"]}       # a library file is uploaded to Meta when it goes out
        out.append({"type": "header", "parameters": [{"type": kind, kind: ref}]})
    elif used["header"]:
        out.append({"type": "header", "parameters": [
            {"type": "text", "text": _value(mapping[str(v)], contact, attrs, make_link)} for v in used["header"]]})
    if used["body"]:
        out.append({"type": "body", "parameters": [
            {"type": "text", "text": _value(mapping[str(v)], contact, attrs, make_link)} for v in used["body"]]})
    for i in used["url_buttons"]:
        spec = mapping.get(str(100 + i)) or mapping.get("url_%d" % i)
        out.append({"type": "button", "sub_type": "url", "index": str(i),
                    "parameters": [{"type": "text", "text": _value(spec, contact, attrs, make_link)}]})
    qr = 0
    for c in template_components or []:
        if c.get("type") == "BUTTONS":
            for i, b in enumerate(c.get("buttons") or []):
                if b.get("type") == "QUICK_REPLY":
                    out.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                                "parameters": [{"type": "payload", "payload": "cmp:%s:%s:%d" % (campaign_id, recipient_id, qr)}]})
                    qr += 1
    return out


# ── launch ───────────────────────────────────────────────────────────────────

def launch(cur, tenant_id, campaign_id, *, now: Optional[datetime] = None) -> dict:
    """Preflight, then start now or schedule. Raises CampaignError('preflight_failed') listing the blockers."""
    now = now or utcnow()
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    if c["status"] not in ("draft", "scheduled", "failed"):
        raise CampaignError("wrong_state", "this campaign is %s and cannot be launched" % c["status"])
    report = preflight(cur, tenant_id, campaign_id, now=now)
    if not report.ok:
        err = CampaignError("preflight_failed", "; ".join(b["message"] for b in report.blocking))
        err.report = report
        raise err
    if c["schedule_at"] and c["schedule_at"] > now:
        cur.execute("update campaigns set status = 'scheduled', updated_at = now() where id = %s", (campaign_id,))
        jobs.enqueue(cur, tenant_id, "launch_campaign", {"campaign_id": str(campaign_id)},
                     "launch:%s:%s" % (campaign_id, c["schedule_at"].isoformat()), run_at=c["schedule_at"])
        return {"status": "scheduled", "schedule_at": c["schedule_at"].isoformat(), "recipients": report.recipients}
    return start(cur, tenant_id, campaign_id, now=now)


def start(cur, tenant_id, campaign_id, *, now: Optional[datetime] = None) -> dict:
    """Snapshot the audience and queue every message. Safe to call twice (queued work is idempotent)."""
    now = now or utcnow()
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    cur.execute("select id, components from templates where id = %s", (c["template_id"],))
    template = cur.fetchone()
    audience, _ = resolve_audience(cur, tenant_id, c["audience"], now=now)
    mapping = c["variables"] or {}
    defs = None
    queued = skipped = 0
    reasons = {}
    cur.execute("update campaigns set status = 'sending', started_at = coalesce(started_at, %s), updated_at = now() where id = %s",
                (now, campaign_id))
    for contact_id in audience:
        cur.execute("select id, name, phone from contacts where id = %s", (contact_id,))
        contact = dict(zip(("id", "name", "phone"), cur.fetchone()))
        run_at = None
        if c["local_time"]:                                   # "10:00 their time": held in the outbox until then
            run_at = timezones.next_at_local(c["local_time"], timezones.zone_for_phone(contact["phone"], c["timezone"] or timezones.DEFAULT_ZONE), now)
        cur.execute("insert into campaign_recipients (tenant_id, campaign_id, contact_id) values (%s, %s, %s)"
                    " on conflict (campaign_id, contact_id) do nothing returning id", (tenant_id, campaign_id, contact_id))
        row = cur.fetchone()
        if row is None:
            continue                                       # already in the snapshot: a repeat start
        recipient_id = row[0]
        cur.execute("select key, value #>> '{}' from contact_attributes where contact_id = %s", (contact_id,))
        attrs = dict(cur.fetchall())

        def make_link(target, _c=contact_id, _r=recipient_id):
            token = pysecrets.token_urlsafe(8)
            cur.execute("insert into short_links (token, tenant_id, campaign_id, recipient_id, contact_id, target)"
                        " values (%s, %s, %s, %s, %s, %s)", (token, tenant_id, campaign_id, _r, _c, target))
            return token

        components = build_components(template[1], mapping, contact, attrs, make_link=make_link,
                                       campaign_id=campaign_id, recipient_id=recipient_id)
        try:
            outbox_id = send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=c["template_name"],
                                              language=c["language"], components=components or None,
                                              idem_key="cmp:%s:%s" % (campaign_id, contact_id), number_id=c["number_id"],
                                              campaign_id=campaign_id, sender="campaign", now=now, run_at=run_at)
            cur.execute("update campaign_recipients set status = 'queued', outbox_id = %s, params = %s where id = %s",
                        (outbox_id, Json({"components": components}), recipient_id))
            queued += 1
        except send.SendRefused as why:
            cur.execute("update campaign_recipients set status = 'skipped', skip_reason = %s where id = %s",
                        (why.reason, recipient_id))
            skipped += 1
            reasons[why.reason] = reasons.get(why.reason, 0) + 1
    cur.execute("update campaigns set total = (select count(*) from campaign_recipients where campaign_id = %s"
                " and status = 'queued') where id = %s", (campaign_id, campaign_id))
    if not queued:
        cur.execute("update campaigns set status = 'done', finished_at = %s where id = %s", (now, campaign_id))
    return {"status": "sending" if queued else "done", "queued": queued, "skipped": skipped, "skipped_reasons": reasons}


def handle_launch_job(cur, tenant_id, payload: dict) -> None:
    """The scheduled launch. A stale job (the campaign was rescheduled, paused or cancelled since) does nothing."""
    cid = payload.get("campaign_id")
    try:
        c = _campaign(cur, tenant_id, cid)
    except CampaignError:
        return
    if c["status"] != "scheduled" or (c["schedule_at"] and c["schedule_at"] > utcnow()):
        return
    start(cur, tenant_id, cid)


# ── control ──────────────────────────────────────────────────────────────────

def pause(cur, tenant_id, campaign_id) -> None:
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    if c["status"] not in ("sending", "scheduled"):
        raise CampaignError("wrong_state", "only a running or scheduled campaign can be paused")
    cur.execute("update campaigns set status = 'paused', updated_at = now() where id = %s", (campaign_id,))


def resume(cur, tenant_id, campaign_id, *, now: Optional[datetime] = None) -> None:
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    if c["status"] != "paused":
        raise CampaignError("wrong_state", "only a paused campaign can be resumed")
    cur.execute("select count(*) from campaign_recipients where campaign_id = %s", (campaign_id,))
    started = cur.fetchone()[0] > 0
    cur.execute("update campaigns set status = %s, updated_at = now() where id = %s",
                ("sending" if started else "scheduled", campaign_id))


def cancel(cur, tenant_id, campaign_id) -> int:
    """Stop the campaign. Messages not yet sent are dropped; those already with Meta cannot be recalled."""
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    if c["status"] in ("done", "cancelled"):
        raise CampaignError("wrong_state", "this campaign has already finished")
    cur.execute("update outbox set state = 'skipped', error_class = 'cancelled', updated_at = now()"
                " where campaign_id = %s and state = 'queued' returning id, message_id", (campaign_id,))
    dropped = cur.fetchall()
    message_ids = [r[1] for r in dropped if r[1]]
    if message_ids:
        cur.execute("update messages set status = 'cancelled' where id = any(%s::uuid[])", ([str(m) for m in message_ids],))
    if dropped:
        cur.execute("update campaign_recipients set status = 'skipped', skip_reason = 'cancelled'"
                    " where outbox_id = any(%s::uuid[])", ([str(r[0]) for r in dropped],))
    cur.execute("update campaign_recipients set status = 'skipped', skip_reason = 'cancelled'"
                " where campaign_id = %s and status = 'pending'", (campaign_id,))
    cur.execute("update campaigns set status = 'cancelled', finished_at = now(), updated_at = now() where id = %s", (campaign_id,))
    return len(message_ids)


def reschedule(cur, tenant_id, campaign_id, schedule_at, timezone_name: Optional[str] = None, *,
               now: Optional[datetime] = None) -> None:
    now = now or utcnow()
    c = _campaign(cur, tenant_id, campaign_id, lock=True)
    if c["status"] not in ("draft", "scheduled"):
        raise CampaignError("wrong_state", "only a draft or scheduled campaign can be rescheduled")
    plans.require(cur, tenant_id, "scheduler", "Scheduling a campaign")
    when = _local_to_utc(schedule_at, timezone_name or c["timezone"])
    if when and when - now > MAX_SCHEDULE_AHEAD:
        raise CampaignError("too_far", "a campaign can be scheduled at most 60 days ahead")
    cur.execute("update campaigns set schedule_at = %s, timezone = coalesce(%s, timezone), updated_at = now() where id = %s",
                (when, timezone_name, campaign_id))
    if c["status"] == "scheduled" and when:
        jobs.enqueue(cur, tenant_id, "launch_campaign", {"campaign_id": str(campaign_id)},
                     "launch:%s:%s" % (campaign_id, when.isoformat()), run_at=when)


# ── tracking ─────────────────────────────────────────────────────────────────

def record_click(cur, token: str) -> Optional[str]:
    """A tracked link was opened: log it and return where to send the person, or None if unknown."""
    cur.execute("select tenant_id, campaign_id, recipient_id, contact_id, target, card from short_links where token = %s", (token,))
    row = cur.fetchone()
    if row is None:
        return None
    tenant_id, campaign_id, recipient_id, contact_id, target, card = row
    cur.execute("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind, card, url)"
                " values (%s, %s, %s, %s, 'link', %s, %s)", (tenant_id, campaign_id, recipient_id, contact_id, card, target))
    return target


def record_button(cur, tenant_id, payload_id: str) -> bool:
    """A quick-reply button on a campaign message was pressed (its payload is `cmp:<campaign>:<recipient>:<n>`)."""
    m = CLICK_PAYLOAD.match(payload_id or "")
    if not m:
        return False
    campaign_id, recipient_id, index = m.groups()
    cur.execute("select contact_id from campaign_recipients where id = %s and campaign_id = %s and tenant_id = %s",
                (recipient_id, campaign_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        return False
    cur.execute("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind, card)"
                " values (%s, %s, %s, %s, 'button', %s)", (tenant_id, campaign_id, recipient_id, row[0], "button:%s" % index))
    return True


def retarget_filter(campaign_id, action: str, *, negate: bool = False) -> dict:
    """A segment filter for "everyone who <action> in that campaign" (or who did not): feed it to a new campaign."""
    leaf = {"field": "campaign", "op": action, "campaign": str(campaign_id)}
    return {"not": leaf} if negate else leaf


# ── reports ──────────────────────────────────────────────────────────────────

def report(cur, tenant_id, campaign_id) -> dict:
    c = _campaign(cur, tenant_id, campaign_id)
    cur.execute(
        "select count(*), count(*) filter (where status = 'queued'), count(*) filter (where status = 'skipped')"
        " from campaign_recipients where campaign_id = %s", (campaign_id,))
    recipients, queued, skipped = cur.fetchone()
    cur.execute(
        "select count(*) filter (where o.state = 'sent'),"
        "       count(*) filter (where m.status in ('delivered', 'read')),"
        "       count(*) filter (where m.status = 'read'),"
        "       count(*) filter (where o.state = 'failed' or m.status = 'failed'),"
        "       count(*) filter (where o.state = 'sent' and exists (select 1 from messages r where r.conversation_id = m.conversation_id"
        "          and r.direction = 'in' and r.created_at > o.updated_at"
        "          and r.created_at <= o.updated_at + interval '%d hours'))"
        " from outbox o left join messages m on m.id = o.message_id where o.campaign_id = %%s" % segments.REPLY_WINDOW_HOURS,
        (campaign_id,))
    sent, delivered, read, failed, replied = cur.fetchone()
    cur.execute("select count(distinct contact_id), count(*) from click_events where campaign_id = %s", (campaign_id,))
    clickers, clicks = cur.fetchone()
    cur.execute("select m.pricing->>'category', count(*) from outbox o join messages m on m.id = o.message_id"
                " where o.campaign_id = %s and (m.pricing->>'billable')::boolean group by 1", (campaign_id,))
    billable = {k or "unknown": n for k, n in cur.fetchall()}
    spend = round(sum(RATE_CARD_ESTIMATE.get(str(k).upper(), 0) * n for k, n in billable.items()) * (1 + GST), 2)
    pct = lambda n: round(100.0 * n / sent, 1) if sent else 0.0                         # noqa: E731
    return {"campaign": {"id": str(c["id"]), "name": c["name"], "status": c["status"], "template": c["template_name"]},
            "counts": {"recipients": recipients, "queued": queued, "skipped": skipped, "sent": sent, "delivered": delivered,
                       "read": read, "replied": replied, "clicked": clickers, "clicks": clicks, "failed": failed},
            "rates": {"delivered": pct(delivered), "read": pct(read), "replied": pct(replied), "clicked": pct(clickers)},
            "billable": billable, "spend_estimate": {"inr": spend, "is_estimate": True}}


def handle_button_payload(cur, tenant_id, canonical: dict) -> None:
    """Called by the ingest worker for an inbound quick-reply: counts it as a click when it came from a campaign."""
    if canonical.get("msg_type") == "button":
        record_button(cur, tenant_id, canonical.get("payload_id") or "")


# ── a test to your own phone ─────────────────────────────────────────────────

TESTS_PER_HOUR = 20


def send_test(cur, tenant_id, *, template_name: str, language: str = "en_US", variables: Optional[dict] = None, to,
              number_id=None, sample_name: str = "Test", now: Optional[datetime] = None) -> dict:
    """Send one message built from a template and the values the campaign would use, to ONE number — normally the
    owner's own phone — so they can see it before it goes to everyone. It is an ordinary template send (billed like
    one), limited to 20 an hour, and the number is kept as a contact tagged `test`."""
    now = now or utcnow()
    digits = contacts.normalise_phone(to)
    if not digits:
        raise CampaignError("bad_number", "that is not a phone number; write it with the country code, like 919812345678")
    cur.execute("select status, components from templates where tenant_id = %s and name = %s and language = %s"
                " order by created_at limit 1", (tenant_id, template_name, language))
    row = cur.fetchone()
    if row is None:
        raise CampaignError("template_unknown", "that template is not known for this account; sync your templates first")
    if row[0] != "APPROVED":
        raise CampaignError("template_not_approved", "the template is %s; only an approved template can be sent" % row[0].lower())
    mapping = variables or {}
    used = _template_variables(row[1])
    for v in sorted(set(used["body"]) | set(used["header"])):
        if str(v) not in mapping:
            raise CampaignError("variable_unmapped", "Template variable {{%d}} has no value" % v)
    for key, spec in mapping.items():
        problem = _mapping_problem(spec)
        if problem:
            raise CampaignError("variable_invalid", "Variable %s: %s" % (key, problem))
    cur.execute("select count(*) from outbox where tenant_id = %s and idem_key like 'test:%%' and created_at > %s",
                (tenant_id, now - timedelta(hours=1)))
    if cur.fetchone()[0] >= TESTS_PER_HOUR:
        raise CampaignError("test_limit", "That is enough test messages for this hour (%d); try again later." % TESTS_PER_HOUR)
    cur.execute("select contact_id from wa_resolve_contact(%s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, digits, None, None, None, sample_name, "test"))
    contact_id = cur.fetchone()[0]
    cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || '{test}'::text[]) t order by t)"
                " where id = %s", (contact_id,))
    cur.execute("select name, phone from contacts where id = %s", (contact_id,))
    contact = dict(zip(("name", "phone"), cur.fetchone()))
    cur.execute("select key, value #>> '{}' from contact_attributes where contact_id = %s", (contact_id,))
    components = build_components(row[1], mapping, contact, dict(cur.fetchall()), make_link=lambda target: target,
                                  campaign_id="test", recipient_id="test")
    key = "test:%s" % pysecrets.token_hex(8)
    try:
        outbox_id = send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=template_name, language=language,
                                          components=components or None, idem_key=key, number_id=number_id, sender="test", now=now)
    except send.SendRefused as why:
        raise CampaignError(why.reason, str(why)) from why
    return {"outbox_id": str(outbox_id), "to": digits, "contact_id": str(contact_id)}
