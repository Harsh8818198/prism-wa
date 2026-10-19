"""Resellers: a business's own name on the product, customer workspaces under it, and the commission it earns.

  branding   the name, colour, logo, support address and domain shown to a reseller's customers instead of Prism's
  link       a customer workspace gives its reseller a one-time code; the reseller redeems it; either side can end the link
  ledger     what a reseller earns from a customer in a month: the base amount (what the customer paid the reseller) times the agreed percent, kept in whole paise

A reseller sees how busy a linked customer is (messages sent and received, contacts) and nothing inside it: the database function that gives it those counts checks the link
itself. Creating a customer workspace and billing it belong to the licence server; this only records the relationship and the money owed.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from prism_wa import commerce, plans
from prism_wa.jobs import utcnow

COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INVITE_HOURS = 72


class PartnerError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _gate(cur, tenant_id) -> None:
    plans.require(cur, tenant_id, "white_label", "White-label and reseller tools")


# ── branding ─────────────────────────────────────────────────────────────────

BRANDING_COLS = ("name", "colour", "logo_asset_id", "support_email", "support_url", "custom_domain", "hide_vendor", "updated_at")


def get_branding(cur, tenant_id) -> dict:
    cur.execute("select " + ", ".join(BRANDING_COLS) + " from tenant_branding where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return dict(zip(BRANDING_COLS, row)) if row else {"name": None, "colour": None, "logo_asset_id": None, "support_email": None, "support_url": None, "custom_domain": None,
                                                      "hide_vendor": False, "updated_at": None}


def save_branding(cur, tenant_id, d: dict) -> dict:
    _gate(cur, tenant_id)
    b = get_branding(cur, tenant_id)
    if "name" in d:
        b["name"] = (str(d["name"] or "").strip()[:60]) or None
    if "colour" in d:
        c = str(d["colour"] or "").strip()
        if c and not COLOUR_RE.match(c):
            raise PartnerError("bad_colour", "A colour is six hex digits after a #, like #1A7F4B.")
        b["colour"] = c.upper() or None
    if "support_email" in d:
        e = str(d["support_email"] or "").strip()
        if e and not EMAIL_RE.match(e):
            raise PartnerError("bad_email", "That support e-mail address is not valid.")
        b["support_email"] = e or None
    if "support_url" in d:
        u = str(d["support_url"] or "").strip()
        if u and not re.match(r"^https://[^\s]+$", u):
            raise PartnerError("bad_url", "The support page address must start with https://")
        b["support_url"] = u or None
    if "custom_domain" in d:
        dom = str(d["custom_domain"] or "").strip().lower()
        if dom and not DOMAIN_RE.match(dom):
            raise PartnerError("bad_domain", "That is not a domain name (like app.yourbrand.com).")
        b["custom_domain"] = dom or None
    if "logo_asset_id" in d:
        if d["logo_asset_id"]:
            cur.execute("select mime from media_assets where id = %s and tenant_id = %s", (d["logo_asset_id"], tenant_id))
            row = cur.fetchone()
            if row is None or not str(row[0] or "").startswith("image/"):
                raise PartnerError("bad_logo", "Choose a picture from your Media Library for the logo.")
        b["logo_asset_id"] = d["logo_asset_id"] or None
    if "hide_vendor" in d:
        b["hide_vendor"] = bool(d["hide_vendor"])
    cur.execute("insert into tenant_branding (tenant_id, name, colour, logo_asset_id, support_email, support_url, custom_domain, hide_vendor) values (%s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (tenant_id) do update set name = excluded.name, colour = excluded.colour, logo_asset_id = excluded.logo_asset_id, support_email = excluded.support_email,"
                " support_url = excluded.support_url, custom_domain = excluded.custom_domain, hide_vendor = excluded.hide_vendor, updated_at = now()",
                (tenant_id, b["name"], b["colour"], b["logo_asset_id"], b["support_email"], b["support_url"], b["custom_domain"], b["hide_vendor"]))
    return get_branding(cur, tenant_id)


def effective_branding(cur, tenant_id) -> dict:
    """What this workspace's people should see: its own branding if it has one, else its reseller's, else none."""
    own = get_branding(cur, tenant_id)
    if own["name"]:
        return {**own, "from": "own"}
    cur.execute("select parent_tenant_id from tenant_parent where child_tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    if row:
        cur.execute("select " + ", ".join(BRANDING_COLS) + " from tenant_branding where tenant_id = %s", (row[0],))
        r = cur.fetchone()
        if r and r[0]:
            return {**dict(zip(BRANDING_COLS, r)), "from": "reseller"}
    return {**own, "from": "none"}


# ── links ────────────────────────────────────────────────────────────────────

def invite(cur, tenant_id, *, now: Optional[datetime] = None) -> dict:
    """A customer workspace makes a one-time code for its reseller. It works for 72 hours, once."""
    cur.execute("select 1 from tenant_parent where child_tenant_id = %s", (tenant_id,))
    if cur.fetchone():
        raise PartnerError("already_linked", "This workspace already has a reseller. Unlink it first.")
    now = now or utcnow()
    cur.execute("delete from partner_invites where tenant_id = %s and used_at is null", (tenant_id,))
    code = secrets.token_hex(8).upper()
    cur.execute("insert into partner_invites (code, tenant_id, expires_at) values (%s, %s, %s)", (code, tenant_id, now + timedelta(hours=INVITE_HOURS)))
    return {"code": "-".join([code[i:i + 4] for i in range(0, 16, 4)]), "hours": INVITE_HOURS}


def redeem(cur, tenant_id, code: str, percent, note: str = "") -> dict:
    _gate(cur, tenant_id)
    try:
        pct = Decimal(str(percent if percent not in (None, "") else 0))
    except Exception:                                                      # noqa: BLE001
        raise PartnerError("bad_percent", "The commission is a percentage, like 20.") from None
    if pct < 0 or pct > 100:
        raise PartnerError("bad_percent", "The commission is between 0 and 100 percent.")
    clean = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    try:
        cur.execute("select wa_redeem_partner_code(%s, %s, %s, %s)", (clean, tenant_id, pct, (note or "").strip()[:200] or None))
    except Exception as why:                                               # noqa: BLE001
        text = str(why)
        if "own reseller" in text:
            raise PartnerError("self_link", "A workspace cannot be its own reseller.") from None
        if "already has a reseller" in text:
            raise PartnerError("already_linked", "That workspace already has a reseller.") from None
        raise
    child = cur.fetchone()[0]
    if child is None:
        raise PartnerError("bad_code", "That code is not valid, or it has expired or been used. Ask the customer for a new one.")
    return {"child_tenant_id": str(child)}


def unlink(cur, tenant_id, child_tenant_id=None) -> bool:
    """A reseller ends the link to one customer (named), or a customer ends the link to its reseller (no name)."""
    if child_tenant_id:
        cur.execute("delete from tenant_parent where child_tenant_id = %s and parent_tenant_id = %s", (child_tenant_id, tenant_id))
    else:
        cur.execute("delete from tenant_parent where child_tenant_id = %s", (tenant_id,))
    return cur.rowcount > 0


def set_percent(cur, tenant_id, child_tenant_id, percent) -> None:
    pct = Decimal(str(percent))
    if pct < 0 or pct > 100:
        raise PartnerError("bad_percent", "The commission is between 0 and 100 percent.")
    cur.execute("update tenant_parent set commission_percent = %s where child_tenant_id = %s and parent_tenant_id = %s", (pct, child_tenant_id, tenant_id))
    if cur.rowcount == 0:
        raise PartnerError("unknown_customer", "That customer is not linked to this workspace.")


def customers(cur, tenant_id, *, now: Optional[datetime] = None, days: int = 30) -> list:
    _gate(cur, tenant_id)
    now = now or utcnow()
    cur.execute("select child_tenant_id, commission_percent, note, created_at from tenant_parent where parent_tenant_id = %s order by created_at", (tenant_id,))
    out = []
    for child, pct, note, since in cur.fetchall():
        cur.execute("select wa_child_name(%s, %s)", (tenant_id, child))
        name = cur.fetchone()[0]
        cur.execute("select sent, received, contacts from wa_child_activity(%s, %s, %s, %s)", (tenant_id, child, now - timedelta(days=days), now + timedelta(days=1)))
        sent, received, n = cur.fetchone()
        out.append({"child_tenant_id": str(child), "name": name, "commission_percent": float(pct), "note": note, "linked_at": since, "sent": sent, "received": received, "contacts": n})
    return out


def reseller_of(cur, tenant_id) -> Optional[dict]:
    cur.execute("select p.parent_tenant_id, p.created_at, b.name, b.support_email, b.support_url from tenant_parent p left join tenant_branding b on b.tenant_id = p.parent_tenant_id"
                " where p.child_tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return dict(zip(("tenant_id", "linked_at", "name", "support_email", "support_url"), row)) if row else None


# ── commission ───────────────────────────────────────────────────────────────

def ledger_add(cur, tenant_id, child_tenant_id, period: str, base, note: str = "") -> dict:
    _gate(cur, tenant_id)
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", period or ""):
        raise PartnerError("bad_period", "A period is a month like 2026-10.")
    cur.execute("select commission_percent from tenant_parent where child_tenant_id = %s and parent_tenant_id = %s", (child_tenant_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise PartnerError("unknown_customer", "That customer is not linked to this workspace.")
    base_paise = commerce.paise(base, what="base amount")
    pct = Decimal(row[0])
    amount = int((Decimal(base_paise) * pct / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    cur.execute("insert into commission_ledger (parent_tenant_id, child_tenant_id, period, base_paise, percent, amount_paise, note) values (%s, %s, %s, %s, %s, %s, %s)"
                " on conflict (parent_tenant_id, child_tenant_id, period) do update set base_paise = excluded.base_paise, percent = excluded.percent,"
                " amount_paise = excluded.amount_paise, note = excluded.note where commission_ledger.status = 'accrued' returning id",
                (tenant_id, child_tenant_id, period, base_paise, pct, amount, (note or "").strip()[:200] or None))
    got = cur.fetchone()
    if got is None:
        raise PartnerError("already_paid", "The commission for %s was already paid and cannot be changed." % period)
    return {"id": got[0], "base_paise": base_paise, "percent": float(pct), "amount_paise": amount}


def ledger_list(cur, tenant_id, child_tenant_id=None) -> dict:
    _gate(cur, tenant_id)
    params = [tenant_id]
    extra = ""
    if child_tenant_id:
        extra = " and child_tenant_id = %s"
        params.append(child_tenant_id)
    cur.execute("select id, child_tenant_id, period, base_paise, percent, amount_paise, status, note, created_at from commission_ledger where parent_tenant_id = %s" + extra
                + " order by period desc, id desc limit 500", params)
    cols = ("id", "child_tenant_id", "period", "base_paise", "percent", "amount_paise", "status", "note", "created_at")
    rows = [dict(zip(cols, r), percent=float(r[4])) for r in cur.fetchall()]
    return {"entries": rows, "owed_paise": sum(r["amount_paise"] for r in rows if r["status"] == "accrued"), "paid_paise": sum(r["amount_paise"] for r in rows if r["status"] == "paid")}


def ledger_mark_paid(cur, tenant_id, entry_id) -> bool:
    _gate(cur, tenant_id)
    cur.execute("update commission_ledger set status = 'paid' where id = %s and parent_tenant_id = %s and status = 'accrued'", (entry_id, tenant_id))
    return cur.rowcount > 0
