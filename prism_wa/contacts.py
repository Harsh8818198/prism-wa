"""Contacts as a CRM (spec WS5): phone numbers, attributes, CSV import/export, erasure.

Everything takes a `tenant_id` and touches only that workspace's rows.

Phone numbers. A bare ten-digit number is Indian and becomes 91XXXXXXXXXX; a
number with `+` or `00` is taken as already carrying its country code. Indian
mobiles start 6-9, so 91 + a number starting 0-5 is refused rather than saved.

CSV. The only required column is Mobile (Name is expected; without it contacts
are named by their number). Other columns become attributes. An import records
consent only when the person importing says they have it (a checkbox, kept in
the ledger with the file name), and can never undo someone's opt-out.

Spreadsheets run formulas: a cell that starts with = + - or @ is written out
with a leading apostrophe so an exported contact list cannot execute anything.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from psycopg2.extras import Json

from prism_wa import consent, plans

ATTRIBUTE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
ATTRIBUTE_TYPES = ("string", "number", "date", "bool", "enum")
MAX_ROWS = 200_000
COMMIT_EVERY = 500
GENERIC_NAMES = ("", "unknown lead", "unknown")

NAME_HEADERS = {"name", "full name", "customer name", "contact name", "fullname"}
MOBILE_HEADERS = {"mobile", "mobile number", "phone", "phone number", "whatsapp", "whatsapp number",
                  "number", "contact number", "mobileno", "mobile no"}
EMAIL_HEADERS = {"email", "email address", "e-mail"}
TAG_HEADERS = {"tags", "tag"}
OPT_IN_HEADERS = {"opt in", "opt_in", "optin", "consent"}
SOURCE_HEADERS = {"source", "lead source", "origin"}
RESERVED = NAME_HEADERS | MOBILE_HEADERS | EMAIL_HEADERS | TAG_HEADERS | OPT_IN_HEADERS | SOURCE_HEADERS


class ImportProblem(Exception):
    """The file cannot be imported at all (as opposed to a bad row, which is reported and skipped)."""


# ── phone numbers ────────────────────────────────────────────────────────────

def normalise_phone(raw, default_country: str = "91") -> Optional[str]:
    """Digits with the country code and no `+`, or None when it cannot be a phone number."""
    text = str(raw or "").strip()
    if not text:
        return None
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    if not international:
        digits = digits.lstrip("0") if len(digits) > 10 else digits
        if len(digits) == 10:
            digits = default_country + digits
    if not 8 <= len(digits) <= 15:
        return None
    if digits.startswith("91") and len(digits) == 12 and digits[2] not in "6789":
        return None                                          # not an Indian mobile number
    return digits


# ── attributes ───────────────────────────────────────────────────────────────

def coerce(kind: str, value, options=None):
    """A value in the shape its attribute type stores, or ValueError saying what is wrong."""
    if kind == "string":
        return str(value).strip()[:500]
    if kind == "number":
        try:
            number = float(str(value).replace(",", "").strip())
        except ValueError:
            raise ValueError("%r is not a number" % (value,)) from None
        return int(number) if number == int(number) else number
    if kind == "bool":
        said = str(value).strip().lower()
        if said in ("true", "yes", "y", "1"):
            return True
        if said in ("false", "no", "n", "0"):
            return False
        raise ValueError("%r is not yes or no" % (value,))
    if kind == "date":
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError("%r is not a date (use 2026-10-17 or 17/10/2026)" % (value,))
    if kind == "enum":
        text = str(value).strip()
        allowed = list(options or [])
        for choice in allowed:
            if str(choice).lower() == text.lower():
                return choice
        raise ValueError("%r is not one of: %s" % (value, ", ".join(map(str, allowed))))
    raise ValueError("unknown attribute type %r" % (kind,))


def define_attribute(cur, tenant_id, key: str, kind: str = "string", *, is_global: bool = False, options=None) -> None:
    if not ATTRIBUTE_KEY_RE.match(key or ""):
        raise ValueError("an attribute name starts with a letter and uses letters, numbers and underscores (40 at most)")
    if kind not in ATTRIBUTE_TYPES:
        raise ValueError("unknown attribute type %r" % (kind,))
    if kind == "enum" and not options:
        raise ValueError("a choice attribute needs its options")
    cur.execute("select 1 from attribute_defs where tenant_id = %s and key = %s", (tenant_id, key))
    if cur.fetchone() is None:                                   # a NEW attribute counts against the plan
        cur.execute("select count(*) from attribute_defs where tenant_id = %s", (tenant_id,))
        plans.check_count(cur, tenant_id, "attributes", cur.fetchone()[0], 1, "custom attributes")
    cur.execute("insert into attribute_defs (tenant_id, key, type, is_global, options) values (%s, %s, %s, %s, %s)"
                " on conflict (tenant_id, key) do nothing",
                (tenant_id, key, kind, is_global, Json(list(options)) if options else None))


def attribute_defs(cur, tenant_id) -> dict:
    cur.execute("select key, type, options from attribute_defs where tenant_id = %s", (tenant_id,))
    return {k: {"type": t, "options": o} for k, t, o in cur.fetchall()}


def set_attribute(cur, tenant_id, contact_id, key: str, value, defs: Optional[dict] = None) -> None:
    defs = defs if defs is not None else attribute_defs(cur, tenant_id)
    if key not in defs:
        raise ValueError("there is no attribute called %r" % (key,))
    stored = coerce(defs[key]["type"], value, defs[key]["options"])
    cur.execute("insert into contact_attributes (contact_id, tenant_id, key, value) values (%s, %s, %s, %s)"
                " on conflict (contact_id, key) do update set value = excluded.value, updated_at = now()",
                (contact_id, tenant_id, key, Json(stored)))


# ── import ───────────────────────────────────────────────────────────────────

@dataclass
class ImportReport:
    rows: int = 0
    created: int = 0
    updated: int = 0
    invalid: int = 0
    consent_recorded: int = 0
    kept_opted_out: int = 0
    errors: list = field(default_factory=list)          # [{"row": 7, "error": "..."}]
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _attribute_key(header: str) -> Optional[str]:
    key = re.sub(r"[^A-Za-z0-9_]+", "_", header.strip()).strip("_")
    return key if ATTRIBUTE_KEY_RE.match(key) else None


def import_csv(conn, tenant_id, text: str, *, source: str = "IMPORT", attest_consent: bool = False,
               evidence: str = "", default_country: str = "91", extra_tags=(), max_rows: int = MAX_ROWS,
               mapping: Optional[dict] = None) -> ImportReport:
    """Import contacts from CSV text. Bad rows are reported and skipped; the rest are saved.

    `mapping` says what each column is — {"Customer": "name", "Ph": "mobile", "City": "attr:City", "Notes": "ignore"} —
    for a file whose headers we would not guess. Without it the headers are read by name, as before."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportProblem("The file is empty.") from None
    cols = {}
    attrs = {}
    report = ImportReport()
    for i, raw in enumerate(header):
        if mapping is not None:
            role = (mapping.get(raw) or mapping.get(raw.strip()) or "ignore").strip()
            if role in ("name", "mobile", "email", "tags", "opt_in", "source"):
                if role not in cols:
                    cols[role] = i
            elif role.startswith("attr:"):
                key = role[5:].strip()
                if ATTRIBUTE_KEY_RE.match(key) and key not in attrs.values():
                    attrs[i] = key
                else:
                    report.warnings.append("Column %r was skipped (%r cannot be used as an attribute name)." % (raw, key))
            continue
        h = raw.strip().lower()
        for role, names in (("name", NAME_HEADERS), ("mobile", MOBILE_HEADERS), ("email", EMAIL_HEADERS),
                            ("tags", TAG_HEADERS), ("opt_in", OPT_IN_HEADERS), ("source", SOURCE_HEADERS)):
            if h in names and role not in cols:
                cols[role] = i
                break
        else:
            if h:
                key = _attribute_key(raw)
                if key and key not in attrs.values():
                    attrs[i] = key
                else:
                    report.warnings.append("Column %r was skipped (its name cannot be used as an attribute)." % raw)
    if "mobile" not in cols:
        raise ImportProblem("The file needs a Mobile column (Mobile, Phone or WhatsApp).")
    if "name" not in cols:
        report.warnings.append("There is no Name column; contacts will be named by their number.")

    tags_extra = [t.strip() for t in extra_tags if t and t.strip()]
    defs = None
    try:
        with conn.cursor() as cur:
            # The plan caps custom attributes and distinct tags. A file that would pass the cap is still
            # imported; the columns / tags past it are left out, and the report says so.
            have = set(attribute_defs(cur, tenant_id))
            room = _room(cur, tenant_id, "attributes", len(have))
            for i, key in list(attrs.items()):
                if key in have:
                    continue
                if room is not None and room <= 0:
                    del attrs[i]
                    report.warnings.append("Column %r was left out: your plan allows %d custom attributes." % (
                        key, plans.limit(cur, tenant_id, "attributes")))
                    continue
                room = None if room is None else room - 1
            for key in attrs.values():
                define_attribute(cur, tenant_id, key, "string")
            defs = attribute_defs(cur, tenant_id)
            cur.execute("select distinct t from contacts, unnest(tags) t where tenant_id = %s", (tenant_id,))
            tag_state = {"known": {r[0] for r in cur.fetchall()}, "dropped": set()}
            tag_state["room"] = _room(cur, tenant_id, "tags", len(tag_state["known"]))
        conn.commit()

        for number, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            report.rows += 1
            if report.rows > max_rows:
                report.rows -= 1
                report.errors.append({"row": number, "error": "stopped: more than %d rows" % max_rows})
                break
            _import_row(conn, tenant_id, row, number, cols, attrs, defs, report, source=source,
                        attest=attest_consent, evidence=evidence, default_country=default_country,
                        extra_tags=tags_extra, tag_state=tag_state)
            if report.rows % COMMIT_EVERY == 0:
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if tag_state["dropped"]:
        report.warnings.append("These tags were left out because your plan's tag limit was reached: %s." %
                               ", ".join(sorted(tag_state["dropped"])[:10]))
    return report


def _room(cur, tenant_id, kind: str, used: int):
    """How many more of `kind` the plan allows (None = no ceiling)."""
    ceiling = plans.limit(cur, tenant_id, kind)
    return None if ceiling is None else max(ceiling - used, 0)


def _cell(row: list, cols: dict, role: str) -> str:
    i = cols.get(role)
    return row[i].strip() if i is not None and i < len(row) else ""


def _import_row(conn, tenant_id, row, number, cols, attrs, defs, report, *, source, attest, evidence,
                default_country, extra_tags, tag_state) -> None:
    phone = normalise_phone(_cell(row, cols, "mobile"), default_country)
    if phone is None:
        report.invalid += 1
        report.errors.append({"row": number, "error": "%r is not a valid phone number" % _cell(row, cols, "mobile")})
        return
    with conn.cursor() as cur:
        cur.execute("savepoint r")
        try:
            name = _cell(row, cols, "name") or None
            row_source = _cell(row, cols, "source") or source
            cur.execute("select contact_id, created from wa_resolve_contact(%s, %s, null, null, null, %s, %s)",
                        (tenant_id, phone, name, row_source))
            contact_id, created = cur.fetchone()
            report.created += 1 if created else 0
            report.updated += 0 if created else 1

            tags = [t.strip() for t in re.split(r"[|;]", _cell(row, cols, "tags")) if t.strip()] + list(extra_tags)
            kept = []
            for tag in tags:
                if tag in tag_state["known"]:
                    kept.append(tag)
                elif tag_state["room"] is None or tag_state["room"] > 0:
                    tag_state["known"].add(tag)
                    tag_state["room"] = None if tag_state["room"] is None else tag_state["room"] - 1
                    kept.append(tag)
                else:
                    tag_state["dropped"].add(tag)
            tags = kept
            if tags:
                cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t)"
                            " where id = %s", (tags, contact_id))
            email = _cell(row, cols, "email")
            if email:
                cur.execute("savepoint e")
                try:
                    cur.execute("update contacts set email = %s where id = %s and email is null", (email, contact_id))
                    cur.execute("release savepoint e")
                except Exception:                                  # noqa: BLE001  (that email belongs to someone else)
                    cur.execute("rollback to savepoint e")
                    report.errors.append({"row": number, "error": "email %r is already used by another contact (kept the rest)" % email})
            for i, key in attrs.items():
                value = row[i].strip() if i < len(row) else ""
                if value:
                    try:
                        set_attribute(cur, tenant_id, contact_id, key, value, defs)
                    except ValueError as exc:
                        report.errors.append({"row": number, "error": "%s: %s" % (key, exc)})
            wants_in = attest or _cell(row, cols, "opt_in").lower() in ("yes", "y", "true", "1", "opt in")
            if wants_in:
                if consent.record(cur, tenant_id, contact_id, "opt_in", "csv_import",
                                  evidence or "attested at import", by_person=False):
                    report.consent_recorded += 1
                else:
                    cur.execute("select opt_out from contacts where id = %s", (contact_id,))
                    if cur.fetchone()[0]:
                        report.kept_opted_out += 1
            cur.execute("release savepoint r")
        except Exception as exc:                                    # noqa: BLE001
            cur.execute("rollback to savepoint r")
            report.invalid += 1
            report.errors.append({"row": number, "error": str(exc).splitlines()[0][:200]})


# ── export ───────────────────────────────────────────────────────────────────

def _safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(conn, tenant_id, *, tags=None, mask: bool = False, ids=None) -> str:
    """Every contact (optionally only those with any of `tags`, or only `ids`) as CSV. `mask` hides all but the last four digits."""
    with conn.cursor() as cur:
        cur.execute("select key from attribute_defs where tenant_id = %s order by key", (tenant_id,))
        keys = [r[0] for r in cur.fetchall()]
        sql = ("select c.id, c.name, c.phone, c.email, c.tags, c.opted_in, c.opt_out, c.source_first_seen"
               " from contacts c where c.tenant_id = %s")
        params = [tenant_id]
        if tags:
            sql += " and c.tags && %s::text[]"
            params.append(list(tags))
        if ids is not None:
            sql += " and c.id = any(%s::uuid[])"
            params.append([str(i) for i in ids])
        cur.execute(sql + " order by c.name, c.id", params)
        rows = cur.fetchall()
        values = {}
        if rows and keys:
            cur.execute("select contact_id, key, value from contact_attributes where tenant_id = %s", (tenant_id,))
            for contact_id, key, value in cur.fetchall():
                values[(contact_id, key)] = value
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Name", "Mobile", "Email", "Tags", "Opted in", "Opted out", "Source"] + keys)
    for cid, name, phone, email, tag_list, opted_in, opted_out, src in rows:
        shown = ("*" * max(len(phone or "") - 4, 0) + (phone or "")[-4:]) if mask and phone else (phone or "")
        w.writerow([_safe(x) for x in (name, shown, "" if mask else email, "|".join(tag_list or []),
                                       "yes" if opted_in else "no", "yes" if opted_out else "no", src)]
                   + [_safe(values.get((cid, k), "")) for k in keys])
    return out.getvalue()


# ── erasure ──────────────────────────────────────────────────────────────────

def erase_contact(cur, tenant_id, contact_id) -> bool:
    """Remove a person's data everywhere this platform holds it (DPDP right to erasure).

    Deletes the contact (which takes their conversations, messages, attributes
    and consent history with it), the queued and sent outbox rows, every stored
    webhook event that names their number or id, the merge-audit snapshots and what
    they filled in on a form. Their orders stay as business records, without the
    delivery address and note.
    Returns False when the contact is not in this workspace."""
    cur.execute("select phone, bsuid from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        return False
    phone, bsuid = row
    cur.execute("select value from contact_identities where contact_id = %s", (contact_id,))
    keys = {v for (v,) in cur.fetchall()} | {x for x in (phone, bsuid) if x}
    digits = {re.sub(r"\D", "", k) for k in keys if re.sub(r"\D", "", k)}

    cur.execute("delete from outbox where tenant_id = %s and (to_phone = any(%s) or to_bsuid = any(%s)"
                " or message_id in (select m.id from messages m join conversations v on v.id = m.conversation_id"
                "                   where v.contact_id = %s))",
                (tenant_id, list(digits), list(keys), contact_id))
    cur.execute("delete from wa_events where tenant_id = %s and (canonical->>'wa_id' = any(%s)"
                " or canonical->>'bsuid' = any(%s) or canonical->>'parent_bsuid' = any(%s))",
                (tenant_id, list(digits), list(keys), list(keys)))
    cur.execute("delete from contact_identity_audit where tenant_id = %s and (contact_id = %s"
                " or other_contact_id = %s or value = any(%s))", (tenant_id, contact_id, contact_id, list(keys)))
    cur.execute("delete from form_responses where tenant_id = %s and contact_id = %s", (tenant_id, contact_id))          # what they filled in is theirs
    cur.execute("update orders set address = null, note = null where tenant_id = %s and contact_id = %s", (tenant_id, contact_id))   # the order itself is a business record; where it went is not
    cur.execute("delete from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    return True
