"""What Meta says about a number — how many people it may start chats with in a day, its display-name review, its speed — and the
business profile customers see (about, address, website, photo).

    refresh()       ask Meta about each connected number and keep the answer (messaging limit, name status, quality, speed)
    limits_view()   what is kept, with the daily limit in numbers
    profile_get()   the business profile, read from Meta
    profile_save()  change it (checked first against Meta's limits); the photo comes from the Media Library
"""
from __future__ import annotations

import re
from typing import Optional

from psycopg2.extras import Json

from prism_wa import media, templates
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

TIER_LIMITS = {"TIER_50": 50, "TIER_250": 250, "TIER_1K": 1000, "TIER_2K": 2000, "TIER_10K": 10000, "TIER_100K": 100000, "TIER_UNLIMITED": None}
TIER_ORDER = ("TIER_50", "TIER_250", "TIER_1K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED")
VERTICALS = ("UNDEFINED", "OTHER", "AUTO", "BEAUTY", "APPAREL", "EDU", "ENTERTAIN", "EVENT_PLAN", "FINANCE", "GROCERY", "GOVT", "HOTEL", "HEALTH",
             "NONPROFIT", "PROF_SERVICES", "RETAIL", "TRAVEL", "RESTAURANT", "NOT_A_BIZ")
ABOUT_MAX, ADDRESS_MAX, DESCRIPTION_MAX, EMAIL_MAX, WEBSITE_MAX, WEBSITES_MAX = 139, 256, 512, 128, 256, 2
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NUMBER_FIELDS = "verified_name,name_status,quality_rating,messaging_limit_tier,throughput,display_phone_number,status"
PROFILE_FIELDS = "about,address,description,email,profile_picture_url,websites,vertical"


class NumberError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _token(cur, vault: Vault, tenant_id, waba_row_id):
    return templates._waba_token(cur, vault, tenant_id, waba_row_id)


def _numbers(cur, tenant_id, number_id=None) -> list:
    sql = "select id, waba_id, phone_number_id, display from phone_numbers where tenant_id = %s"
    params = [tenant_id]
    if number_id:
        sql += " and id = %s"
        params.append(number_id)
    cur.execute(sql + " order by created_at", params)
    return [dict(zip(("id", "waba_id", "phone_number_id", "display"), r)) for r in cur.fetchall()]


def _one(cur, tenant_id, number_id=None) -> dict:
    rows = _numbers(cur, tenant_id, number_id)
    if not rows:
        raise NumberError("no_number", "This workspace has no WhatsApp number yet. Connect one in Settings.")
    if len(rows) > 1:
        raise NumberError("which_number", "This workspace has several numbers; say which one.")
    return rows[0]


def refresh(conn, *, graph: Graph, vault: Vault, tenant_id, now=None) -> list:
    """Ask Meta about each number and keep the answer. A number Meta refuses to talk about keeps what we had, with the reason."""
    now = now or utcnow()
    with conn.cursor() as cur:
        numbers = _numbers(cur, tenant_id)
    problems = []
    for n in numbers:
        try:
            with conn.cursor() as cur:
                _meta, token = _token(cur, vault, tenant_id, n["waba_id"])
            info = graph.phone_number_info(token, n["phone_number_id"])
        except (GraphError, LookupError) as exc:
            problems.append({"number": n["display"], "message": str(exc)})
            continue
        with conn.cursor() as cur:
            cur.execute("update phone_numbers set messaging_tier = coalesce(%s, messaging_tier), name_status = coalesce(%s, name_status),"
                        " verified_name = coalesce(%s, verified_name), throughput = coalesce(%s, throughput), quality = coalesce(%s, quality),"
                        " status = coalesce(%s, status), display = coalesce(%s, display), limits_checked_at = %s where id = %s",
                        (info.get("messaging_limit_tier"), info.get("name_status"), info.get("verified_name"),
                         (info.get("throughput") or {}).get("level") if isinstance(info.get("throughput"), dict) else info.get("throughput"),
                         info.get("quality_rating"), info.get("status"), info.get("display_phone_number"), now, n["id"]))
        conn.commit()
    return problems


def limits_view(cur, tenant_id) -> list:
    cur.execute("select id, display, status, quality, messaging_tier, name_status, verified_name, throughput, limits_checked_at"
                " from phone_numbers where tenant_id = %s order by created_at", (tenant_id,))
    out = []
    for r in cur.fetchall():
        tier = r[4]
        nxt = TIER_ORDER[TIER_ORDER.index(tier) + 1] if tier in TIER_ORDER[:-1] else None
        out.append({"id": r[0], "display": r[1], "status": r[2], "quality": r[3], "tier": tier,
                    "daily_limit": TIER_LIMITS.get(tier) if tier in TIER_LIMITS else None, "unlimited": tier == "TIER_UNLIMITED",
                    "next_limit": TIER_LIMITS.get(nxt) if nxt else None, "name_status": r[5], "verified_name": r[6], "throughput": r[7], "checked_at": r[8]})
    return out


# ── the business profile ─────────────────────────────────────────────────────

def profile_problems(data: dict) -> list:
    """What Meta would refuse in a business profile, in words (empty = fine)."""
    bad = []
    about = (data.get("about") or "").strip()
    if data.get("about") is not None and not 1 <= len(about) <= ABOUT_MAX:
        bad.append("The About line is 1 to %d characters." % ABOUT_MAX)
    if len(data.get("address") or "") > ADDRESS_MAX:
        bad.append("The address is up to %d characters." % ADDRESS_MAX)
    if len(data.get("description") or "") > DESCRIPTION_MAX:
        bad.append("The description is up to %d characters." % DESCRIPTION_MAX)
    email = (data.get("email") or "").strip()
    if email and (len(email) > EMAIL_MAX or not EMAIL_RE.match(email)):
        bad.append("That email address does not look right.")
    sites = [str(w).strip() for w in (data.get("websites") or []) if str(w).strip()]
    if len(sites) > WEBSITES_MAX:
        bad.append("At most %d websites." % WEBSITES_MAX)
    for w in sites:
        if not w.startswith(("http://", "https://")) or len(w) > WEBSITE_MAX:
            bad.append("A website needs its full address, like https://example.com (up to %d characters)." % WEBSITE_MAX)
            break
    if data.get("vertical") and str(data["vertical"]).upper() not in VERTICALS:
        bad.append("Choose a category from the list.")
    return bad


def profile_get(conn, *, graph: Graph, vault: Vault, tenant_id, number_id=None) -> dict:
    with conn.cursor() as cur:
        n = _one(cur, tenant_id, number_id)
        cur.execute("select business_profile from phone_numbers where id = %s", (n["id"],))
        cached = cur.fetchone()[0]
        _meta, token = _token(cur, vault, tenant_id, n["waba_id"])
    try:
        prof = graph.business_profile(token, n["phone_number_id"])
    except GraphError as exc:
        if cached:
            return {"profile": cached, "number": n["display"], "stale": True, "problem": str(exc)}
        raise
    with conn.cursor() as cur:
        cur.execute("update phone_numbers set business_profile = %s where id = %s", (Json(prof), n["id"]))
    conn.commit()
    return {"profile": prof, "number": n["display"], "stale": False}


def profile_save(conn, *, graph: Graph, vault: Vault, tenant_id, data: dict, app_id: str = "", number_id=None, store=None) -> dict:
    problems = profile_problems(data)
    if problems:
        raise NumberError("bad_profile", " ".join(problems))
    fields = {}
    for key in ("about", "address", "description", "email", "vertical"):
        if data.get(key) is not None:
            fields[key] = str(data[key]).strip() if key != "vertical" else str(data[key]).upper()
    if data.get("websites") is not None:
        fields["websites"] = [str(w).strip() for w in data["websites"] if str(w).strip()]
    with conn.cursor() as cur:
        n = _one(cur, tenant_id, number_id)
        _meta, token = _token(cur, vault, tenant_id, n["waba_id"])
        asset = None
        if data.get("picture_asset_id"):
            if not app_id:
                raise LookupError("changing the photo needs this service to know the Meta app (WA_APP_ID)")
            asset, blob = media.read(cur, tenant_id, data["picture_asset_id"], store=store)
            if asset["kind"] != "image" or asset["mime"] not in ("image/jpeg", "image/png"):
                raise NumberError("bad_photo", "The profile photo must be a JPG or PNG picture.")
    if asset is not None:
        fields["profile_picture_handle"] = graph.resumable_upload(token, app_id, asset["filename"], asset["mime"], blob)
    if not fields:
        raise NumberError("nothing_to_save", "There is nothing to change.")
    try:
        graph.update_business_profile(token, n["phone_number_id"], fields)
    except GraphError as exc:
        raise NumberError("meta_refused", "Meta did not accept the profile: %s" % (exc.details or exc.message)) from exc
    return profile_get(conn, graph=graph, vault=vault, tenant_id=tenant_id, number_id=n["id"])
