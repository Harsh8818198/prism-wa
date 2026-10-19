"""Who an ad is shown to: finding places and interests, how many people that is, and lists of people made from a Prism segment.

    search()                  places (countries, regions, cities), interests, behaviours and demographics, by the words the owner types, as Meta's own search returns them
    estimate()                roughly how many people a targeting reaches each day, before the ad is made
    audience_from_segment()   a people list in Meta made from a saved segment — phone numbers, hashed here before they leave Prism, and only for people who opted in
    lookalike()               a list of people like those in another list, in one country, at a chosen closeness
    list_audiences()          the lists made from Prism, with their size as Meta reports it

Meta is only ever sent a SHA-256 of a phone number's digits (country code first, no plus or spaces), never the number. People who have not opted in, who opted out, or who
are blocked are never sent. Everything here is tested against a fake Graph server; the field names follow Meta's Marketing API documentation.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from psycopg2.extras import Json

from prism_wa import ads, ads_build, campaigns
from prism_wa.graph import Graph, GraphError

BATCH = 10000
MAX_PEOPLE = 100000
MIN_PEOPLE = 100                                   # Meta will not use a list smaller than this for look-alikes or delivery
SEARCH_KINDS = ("geo", "interests", "behaviors", "demographics")


def _meta_error(exc: GraphError, what: str):
    return ads.AdsError("meta_refused", "Meta did not %s: %s" % (what, exc.details or exc.message), exc.as_dict())


def search(cur, tenant_id, graph: Graph, vault, account_id, kind: str, q: str, limit: int = 12) -> list:
    """-> [{"id"/"key", "name", "type", "detail", "size"}]. Needs at least two letters; Meta's own answer, trimmed."""
    if kind not in SEARCH_KINDS:
        raise ads.AdsError("bad_search", "search for %s" % ", ".join(SEARCH_KINDS))
    q = (q or "").strip()
    if len(q) < 2:
        raise ads.AdsError("bad_search", "type at least two letters")
    acct = ads._account(cur, tenant_id, vault, account_id)
    limit = max(1, min(int(limit or 12), 25))
    try:
        if kind == "geo":
            out = graph._call("GET", "search", token=acct["token"], query={"type": "adgeolocation", "q": q, "limit": limit, "location_types": json.dumps(["country", "region", "city"])})
        elif kind == "interests":
            out = graph._call("GET", "search", token=acct["token"], query={"type": "adinterest", "q": q, "limit": limit})
        else:
            out = graph._call("GET", "search", token=acct["token"], query={"type": "adTargetingCategory", "class": kind, "q": q, "limit": limit})
    except GraphError as exc:
        raise _meta_error(exc, "search") from exc
    rows = []
    for r in out.get("data") or []:
        if kind == "geo":
            where = ", ".join(x for x in (r.get("region"), r.get("country_name")) if x and x != r.get("name"))
            rows.append({"key": str(r.get("key") or r.get("country_code") or ""), "name": r.get("name") or "", "type": r.get("type") or "", "detail": where,
                         "country": r.get("country_code") or ""})
        else:
            low, high = r.get("audience_size_lower_bound"), r.get("audience_size_upper_bound")
            rows.append({"id": str(r.get("id") or ""), "name": r.get("name") or "", "type": kind, "detail": (r.get("path") and " > ".join(r["path"])) or r.get("topic") or "",
                         "size": [low, high] if low is not None else None})
    return [r for r in rows if r.get("key") or r.get("id")]


def estimate(cur, tenant_id, graph: Graph, vault, account_id, targeting: dict, *, kind: str = "ctwa") -> dict:
    """-> {"ready", "daily_low", "daily_high", "monthly_low", "monthly_high"}: how many people the targeting reaches, as Meta puts it."""
    spec = ads_build.clean_targeting(targeting)
    acct = ads._account(cur, tenant_id, vault, account_id)
    goal = {"ctwa": "CONVERSATIONS", "website": "LINK_CLICKS", "leadform": "LEAD_GENERATION"}.get(kind, "REACH")
    try:
        out = graph._call("GET", "%s/delivery_estimate" % acct["act"], token=acct["token"], query={"optimization_goal": goal, "targeting_spec": json.dumps(spec)})
    except GraphError as exc:
        raise _meta_error(exc, "estimate the audience") from exc
    d = (out.get("data") or [{}])[0]
    return {"ready": bool(d.get("estimate_ready", True)), "daily_low": d.get("estimate_dau"), "daily_high": d.get("estimate_dau"),
            "monthly_low": d.get("estimate_mau_lower_bound"), "monthly_high": d.get("estimate_mau_upper_bound")}


def hash_phone(phone: str) -> Optional[str]:
    """SHA-256 of the digits of a phone number (country code first), or None for something that cannot be a number."""
    digits = re.sub(r"\D", "", phone or "")
    if not 8 <= len(digits) <= 15:
        return None
    return hashlib.sha256(digits.encode("ascii")).hexdigest()


def people_of(cur, tenant_id, segment_id) -> list:
    """The phone numbers of a segment's people who may be advertised to: opted in, not opted out, not blocked."""
    ids, _removed = campaigns.resolve_audience(cur, tenant_id, {"segment_id": segment_id})
    if not ids:
        return []
    cur.execute("select phone from contacts where tenant_id = %s and id = any(%s::uuid[]) and opted_in = true and opt_out = false and blocked = false and phone is not null",
                (tenant_id, [str(i) for i in ids]))
    return [r[0] for r in cur.fetchall()]


def audience_from_segment(cur, tenant_id, graph: Graph, vault, account_id, segment_id, name: str = "") -> dict:
    cur.execute("select name from segments where id = %s and tenant_id = %s", (segment_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ads.AdsError("unknown_segment", "that segment is not in this workspace")
    phones = people_of(cur, tenant_id, segment_id)
    hashes = list(dict.fromkeys(h for h in (hash_phone(p) for p in phones) if h))[:MAX_PEOPLE]
    if len(hashes) < MIN_PEOPLE:
        raise ads.AdsError("too_few", "Meta needs at least %d people with a phone number who have opted in; this segment has %d." % (MIN_PEOPLE, len(hashes)))
    acct = ads._account(cur, tenant_id, vault, account_id)
    label = (name or "").strip() or "Prism · %s" % row[0]
    try:
        made = graph._call("POST", "%s/customaudiences" % acct["act"], token=acct["token"], body={
            "name": label[:100], "subtype": "CUSTOM", "description": "Made by Prism from the segment “%s”, opted-in contacts only." % row[0],
            "customer_file_source": "USER_PROVIDED_ONLY"})
        for i in range(0, len(hashes), BATCH):
            graph._call("POST", "%s/users" % made["id"], token=acct["token"], body={"payload": {"schema": ["PHONE_SHA256"], "data": [[h] for h in hashes[i:i + BATCH]]}})
    except GraphError as exc:
        raise _meta_error(exc, "make the people list") from exc
    cur.execute("insert into ad_audiences (tenant_id, account_id, name, kind, meta_id, segment_id, source, size) values (%s, %s, %s, 'segment', %s, %s, %s, %s) returning id",
                (tenant_id, account_id, label[:100], made["id"], segment_id, Json({"sent": len(hashes), "skipped_not_opted_in": max(len(phones) - len(hashes), 0)}), len(hashes)))
    return {"id": str(cur.fetchone()[0]), "meta_id": made["id"], "sent": len(hashes), "name": label[:100]}


def lookalike(cur, tenant_id, graph: Graph, vault, account_id, audience_id, *, country: str, ratio: float = 0.01, name: str = "") -> dict:
    cur.execute("select meta_id, name from ad_audiences where id = %s and tenant_id = %s and account_id = %s", (audience_id, tenant_id, account_id))
    row = cur.fetchone()
    if row is None:
        raise ads.AdsError("unknown_audience", "that people list is not in this workspace")
    country = (country or "").upper()
    if not re.match(r"^[A-Z]{2}$", country):
        raise ads.AdsError("bad_country", "a country is a two-letter code like IN")
    if not (isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0.01 <= ratio <= 0.2):
        raise ads.AdsError("bad_ratio", "closeness is from 1% (very like them) to 20% (wider)")
    acct = ads._account(cur, tenant_id, vault, account_id)
    label = (name or "").strip() or "%s · look-alike %d%% %s" % (row[1], round(ratio * 100), country)
    try:
        made = graph._call("POST", "%s/customaudiences" % acct["act"], token=acct["token"], body={
            "name": label[:100], "subtype": "LOOKALIKE", "origin_audience_id": row[0], "lookalike_spec": {"type": "similarity", "country": country, "ratio": float(ratio)}})
    except GraphError as exc:
        raise _meta_error(exc, "make the look-alike list") from exc
    cur.execute("insert into ad_audiences (tenant_id, account_id, name, kind, meta_id, source) values (%s, %s, %s, 'lookalike', %s, %s) returning id",
                (tenant_id, account_id, label[:100], made["id"], Json({"of": row[0], "country": country, "ratio": float(ratio)})))
    return {"id": str(cur.fetchone()[0]), "meta_id": made["id"], "name": label[:100]}


def list_audiences(cur, tenant_id, account_id=None) -> list:
    sql = "select id, account_id, name, kind, meta_id, segment_id, source, size, created_at from ad_audiences where tenant_id = %s"
    params: list = [tenant_id]
    if account_id:
        sql += " and account_id = %s"
        params.append(account_id)
    cur.execute(sql + " order by created_at desc", params)
    return [dict(zip(("id", "account_id", "name", "kind", "meta_id", "segment_id", "source", "size", "created_at"), r)) for r in cur.fetchall()]


def delete_audience(cur, tenant_id, graph: Graph, vault, audience_id) -> bool:
    cur.execute("select account_id, meta_id from ad_audiences where id = %s and tenant_id = %s", (audience_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        return False
    acct = ads._account(cur, tenant_id, vault, row[0])
    try:
        graph._call("DELETE", row[1], token=acct["token"])
    except GraphError as exc:
        if exc.http_status != 404:
            raise _meta_error(exc, "remove the list") from exc
    cur.execute("delete from ad_audiences where id = %s", (audience_id,))
    return True
