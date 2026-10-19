"""Making an ad of any kind: a chat on WhatsApp, a visit to a website, or a lead form on Facebook — and the pieces that go into one.

    clean_targeting()   who sees it: places, ages, genders, interests, behaviours, people lists, and where on Facebook and Instagram it may appear
    build_spec()        what was asked for, checked and normalised (the click-to-WhatsApp kind is checked by ads.build_spec, unchanged)
    create_ad()         the campaign, ad set, creative and ad — always PAUSED, and a failure part-way takes back what was made
    upload_image()      a picture from the Media Library, sent to Meta, which gives back the hash an ad names it by
    make_lead_form()    the questions a lead-form ad asks, made on the Page
    copy_variants()     three ways of writing the ad's words, from a few facts about the offer (the language model)

SAFETY is the same as for every ad here: nothing is created live, turning one on is a separate explicit step (ads.activate), and a total budget needs an end date so
it cannot run on. NOT VERIFIED AGAINST META LIVE: the field names follow Meta's Marketing API documentation and are tested against a fake Graph server; the first ad of
a new account must be looked at, paused, in Ads Manager before anything is turned on (the same advice the click-to-WhatsApp ads give).
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

from psycopg2.extras import Json

from prism_wa import ads, media
from prism_wa.graph import Graph, GraphError
from prism_wa.vault import Vault

KINDS = ("ctwa", "website", "leadform")
OBJECTIVES = {"ctwa": "OUTCOME_ENGAGEMENT", "website": "OUTCOME_TRAFFIC", "leadform": "OUTCOME_LEADS"}
WEBSITE_CTAS = ("LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "GET_QUOTE", "BOOK_TRAVEL", "APPLY_NOW", "DOWNLOAD", "ORDER_NOW", "SUBSCRIBE", "GET_OFFER")
LEAD_CTAS = ("SIGN_UP", "LEARN_MORE", "GET_QUOTE", "APPLY_NOW", "SUBSCRIBE", "CONTACT_US")
FORM_QUESTIONS = ("FULL_NAME", "FIRST_NAME", "LAST_NAME", "PHONE", "EMAIL", "CITY", "STATE", "ZIP", "COMPANY_NAME", "JOB_TITLE")
PUBLISHERS = ("facebook", "instagram", "audience_network", "messenger")
FACEBOOK_POSITIONS = ("feed", "story", "video_feeds", "marketplace", "search", "instream_video", "reels", "facebook_reels")
INSTAGRAM_POSITIONS = ("stream", "story", "explore", "reels", "shop", "explore_home")
MAX_INTERESTS = 25
LANGUAGES_COPY = 3


def _ids(items, name: str, limit: int = MAX_INTERESTS) -> list:
    out = []
    for i in items or []:
        i = i if isinstance(i, dict) else {"id": i}
        if not re.match(r"^[0-9]{3,25}$", str(i.get("id") or "")):
            raise ads.AdsError("bad_targeting", "%s must be chosen from the search results" % name)
        out.append({"id": str(i["id"]), "name": str(i.get("name") or "")[:80]})
    if len(out) > limit:
        raise ads.AdsError("bad_targeting", "choose up to %d %s" % (limit, name))
    return out


def clean_targeting(t) -> dict:
    """The targeting spec Meta takes, built from what the screen collects. Raises AdsError(bad_targeting) in plain words."""
    if not isinstance(t, dict):
        raise ads.AdsError("bad_targeting", "choose who should see it")
    geo = t.get("geo_locations") if isinstance(t.get("geo_locations"), dict) else {}
    out_geo = {}
    countries = [str(c).upper() for c in geo.get("countries") or []]
    if any(not re.match(r"^[A-Z]{2}$", c) for c in countries):
        raise ads.AdsError("bad_targeting", "a country is a two-letter code like IN")
    if countries:
        out_geo["countries"] = countries
    for kind in ("regions", "cities"):
        keys = []
        for item in geo.get(kind) or []:
            item = item if isinstance(item, dict) else {"key": item}
            if not re.match(r"^[0-9]{1,15}$", str(item.get("key") or "")):
                raise ads.AdsError("bad_targeting", "%s must be chosen from the search results" % ("a city" if kind == "cities" else "a region"))
            entry = {"key": str(item["key"])}
            if kind == "cities":
                radius = int(item.get("radius") or 0)
                if radius:
                    if not 17 <= radius <= 80:
                        raise ads.AdsError("bad_targeting", "a city's radius is 17 to 80 kilometres")
                    entry.update(radius=radius, distance_unit="kilometer")
            keys.append(entry)
        if keys:
            out_geo[kind] = keys
    if not out_geo:
        raise ads.AdsError("bad_targeting", "choose where to show it: targeting.geo_locations needs countries or cities")
    out = {"geo_locations": out_geo}
    lo, hi = t.get("age_min"), t.get("age_max")
    for key, v in (("age_min", lo), ("age_max", hi)):
        if v is not None:
            if not (isinstance(v, int) and not isinstance(v, bool) and 13 <= v <= 65):
                raise ads.AdsError("bad_targeting", "ages are from 13 to 65")
            out[key] = v
    if lo is not None and hi is not None and lo > hi:
        raise ads.AdsError("bad_targeting", "the youngest age cannot be above the oldest")
    genders = t.get("genders")
    if genders:
        if not (isinstance(genders, list) and set(genders) <= {1, 2}):
            raise ads.AdsError("bad_targeting", "genders are 1 (men) and 2 (women); leave it out for everyone")
        out["genders"] = sorted(set(genders))
    flexible = {}
    for key in ("interests", "behaviors", "demographics"):
        found = _ids(t.get(key), key)
        if found:
            flexible[key] = found
    if flexible:
        out["flexible_spec"] = [flexible]
    for key in ("custom_audiences", "excluded_custom_audiences"):
        found = _ids(t.get(key), "people lists", 10)
        if found:
            out[key] = [{"id": f["id"]} for f in found]
    for key, allowed in (("publisher_platforms", PUBLISHERS), ("facebook_positions", FACEBOOK_POSITIONS), ("instagram_positions", INSTAGRAM_POSITIONS)):
        chosen = t.get(key)
        if chosen:
            if not (isinstance(chosen, list) and set(chosen) <= set(allowed)):
                raise ads.AdsError("bad_targeting", "%s must be some of: %s" % (key.replace("_", " "), ", ".join(allowed)))
            out[key] = list(dict.fromkeys(chosen))
    if t.get("advantage_audience") is not None:
        out["targeting_automation"] = {"advantage_audience": 1 if t["advantage_audience"] else 0}
    return out


def _link(url, name: str = "link") -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url) or len(url) > 500:
        raise ads.AdsError("bad_link", "the %s must be a full web address starting with https://" % name)
    return url


def build_spec(d: dict) -> dict:
    """What an ad needs, for any kind, checked and normalised. Raises AdsError with a reason the screen can show beside a field."""
    kind = d.get("kind") or "ctwa"
    if kind not in KINDS:
        raise ads.AdsError("bad_kind", "an ad is a chat on WhatsApp, a visit to a website, or a lead form")
    targeting = clean_targeting(d.get("targeting") or {})
    daily = d.get("daily_budget") if d.get("lifetime_budget") is None else None
    if kind == "ctwa":
        spec = ads.build_spec(name=d.get("name") or "", whatsapp_number=d.get("whatsapp_number") or "", daily_budget=daily, lifetime_budget=d.get("lifetime_budget"),
                              targeting=targeting, message=d.get("message") or "", headline=d.get("headline") or "", image_hash=d.get("image_hash") or "",
                              welcome_text=d.get("welcome_text") or "", ice_breakers=d.get("ice_breakers"), start_time=d.get("start_time") or "", end_time=d.get("end_time") or "")
        spec["kind"] = "ctwa"
        return spec
    if not (d.get("name") or "").strip():
        raise ads.AdsError("name_required", "give the ad a name")
    if not (d.get("message") or "").strip():
        raise ads.AdsError("message_required", "write the ad's main text")
    if len(d["message"]) > 2000 or len(d.get("headline") or "") > 255 or len(d.get("description") or "") > 255:
        raise ads.AdsError("too_long", "the text is up to 2000 characters, the headline and description up to 255")
    spec = {"kind": kind, "name": d["name"].strip(), "targeting": targeting, "message": d["message"].strip(), "headline": (d.get("headline") or "").strip(),
            "description": (d.get("description") or "").strip(), "image_hash": d.get("image_hash") or "", "start_time": d.get("start_time") or "", "end_time": d.get("end_time") or ""}
    if d.get("lifetime_budget") is not None:
        lt = d["lifetime_budget"]
        if not (isinstance(lt, int) and not isinstance(lt, bool) and lt >= ads.MIN_DAILY_BUDGET):
            raise ads.AdsError("bad_budget", "the total budget is a whole number of the smallest currency unit (paise)")
        if not spec["end_time"]:
            raise ads.AdsError("bad_budget", "a total budget needs an end date: it is spent between the start and the end")
        spec["lifetime_budget"] = lt
    else:
        if not (isinstance(d.get("daily_budget"), int) and not isinstance(d.get("daily_budget"), bool) and d["daily_budget"] >= ads.MIN_DAILY_BUDGET):
            raise ads.AdsError("bad_budget", "the daily budget is a whole number of the smallest currency unit (paise)")
        spec["daily_budget"] = d["daily_budget"]
    if kind == "website":
        spec["link"] = _link(d.get("link"))
        spec["cta"] = d.get("cta") or "LEARN_MORE"
        if spec["cta"] not in WEBSITE_CTAS:
            raise ads.AdsError("bad_cta", "the button is one of: %s" % ", ".join(WEBSITE_CTAS))
        if d.get("pixel_id"):
            if not re.match(r"^[0-9]{5,25}$", str(d["pixel_id"])):
                raise ads.AdsError("bad_pixel", "the pixel id is the number shown in Events Manager")
            spec["pixel_id"] = str(d["pixel_id"])
        spec["optimize_for"] = "landing_page_views" if (d.get("optimize_for") == "landing_page_views" and spec.get("pixel_id")) else "link_clicks"
        return spec
    spec["cta"] = d.get("cta") or "SIGN_UP"
    if spec["cta"] not in LEAD_CTAS:
        raise ads.AdsError("bad_cta", "the button is one of: %s" % ", ".join(LEAD_CTAS))
    form = d.get("form") if isinstance(d.get("form"), dict) else {}
    if form.get("id"):
        if not re.match(r"^[0-9]{5,25}$", str(form["id"])):
            raise ads.AdsError("bad_form", "the form id is a number")
        spec["form"] = {"id": str(form["id"])}
    else:
        questions = form["questions"] if "questions" in form else ["FULL_NAME", "PHONE"]       # a form left alone asks name and phone; one with none chosen is refused below
        clean = []
        for q in questions:
            q = q if isinstance(q, dict) else {"type": q}
            if q.get("type") in FORM_QUESTIONS:
                clean.append({"type": q["type"]})
            elif q.get("type") == "CUSTOM" and 1 <= len(str(q.get("label") or "").strip()) <= 80:
                clean.append({"type": "CUSTOM", "label": str(q["label"]).strip(), "key": re.sub(r"[^a-z0-9_]+", "_", str(q["label"]).lower()).strip("_")[:40] or "question"})
            else:
                raise ads.AdsError("bad_form", "a form question is one of %s, or your own with a label" % ", ".join(FORM_QUESTIONS))
        if not 1 <= len(clean) <= 15:
            raise ads.AdsError("bad_form", "a form asks 1 to 15 questions")
        if not any(q["type"] in ("PHONE", "EMAIL") for q in clean):
            raise ads.AdsError("bad_form", "ask for a phone number or an email address, or you cannot reach the people who fill it in")
        spec["form"] = {"name": (form.get("name") or spec["name"] + " - form")[:80], "questions": clean, "privacy_url": _link(form.get("privacy_url"), "privacy policy address"),
                        "thank_you_url": _link(form["thank_you_url"], "thank-you address") if form.get("thank_you_url") else "", "intro": (form.get("intro") or "")[:500]}
    return spec


# ── pieces ───────────────────────────────────────────────────────────────────

def upload_image(cur, tenant_id, graph: Graph, vault: Vault, account_id, asset_id) -> dict:
    """Send a picture from the Media Library to the ad account. -> {"hash", "filename"}. Only a picture, and not a huge one."""
    acct = ads._account(cur, tenant_id, vault, account_id)
    try:
        meta, data = media.read(cur, tenant_id, asset_id)
    except media.MediaError as why:
        raise ads.AdsError("bad_image", str(why)) from why
    if meta.get("kind") != "image":
        raise ads.AdsError("bad_image", "choose a picture from your Media Library")
    if len(data) > 8 * 1024 * 1024:
        raise ads.AdsError("bad_image", "ad pictures are up to 8 MB")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", meta.get("filename") or "ad.jpg")[:80] or "ad.jpg"
    try:
        out = graph._multipart("%s/adimages" % acct["act"], acct["token"], {}, "filename", name, meta.get("mime") or "image/jpeg", data)
    except GraphError as exc:
        raise ads.AdsError("meta_refused", "Meta did not take the picture: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    images = out.get("images") or {}
    first = next(iter(images.values()), {}) if isinstance(images, dict) else {}
    if not first.get("hash"):
        raise ads.AdsError("meta_refused", "Meta did not give back a picture reference")
    return {"hash": first["hash"], "filename": name}


def _page_token(graph: Graph, acct: dict) -> str:
    """A lead form is made as the Page, so it needs the Page's own token."""
    try:
        got = graph._call("GET", acct["page_id"], token=acct["token"], query={"fields": "access_token"})
    except GraphError as exc:
        raise ads.AdsError("meta_refused", "Meta did not let this token act as the Page: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    return got.get("access_token") or acct["token"]


def make_lead_form(graph: Graph, acct: dict, spec: dict) -> str:
    form = spec["form"]
    body = {"name": form["name"], "privacy_policy": {"url": form["privacy_url"]}, "questions": form["questions"], "locale": "EN_US"}
    if form.get("thank_you_url"):
        body["follow_up_action_url"] = form["thank_you_url"]
    if form.get("intro"):
        body["context_card"] = {"title": spec["headline"] or spec["name"], "content": [form["intro"]], "style": "PARAGRAPH_STYLE"}
    return graph._call("POST", "%s/leadgen_forms" % acct["page_id"], token=_page_token(graph, acct), body=body)["id"]


def create_ad(cur, tenant_id, graph: Graph, vault: Vault, account_id, spec: dict) -> dict:
    """Make the ad, paused. Click-to-WhatsApp ads go through ads.create_ctwa_ad; the others are built here the same way, and a failure takes back what was made."""
    kind = spec.get("kind") or "ctwa"
    if kind == "ctwa":
        out = ads.create_ctwa_ad(cur, tenant_id, graph, vault, account_id, spec)
        cur.execute("update ads set kind = 'ctwa' where id = %s", (out["id"],))
        return out
    acct = ads._account(cur, tenant_id, vault, account_id)
    made: dict = {}
    try:
        if kind == "leadform" and not spec["form"].get("id"):
            made["form"] = make_lead_form(graph, acct, spec)
            spec = {**spec, "form": {**spec["form"], "id": made["form"]}}
        made["campaign"] = graph._call("POST", "%s/campaigns" % acct["act"], token=acct["token"], body={
            "name": spec["name"], "objective": OBJECTIVES[kind], "status": "PAUSED", "special_ad_categories": []})["id"]
        budget = {"lifetime_budget": spec["lifetime_budget"]} if spec.get("lifetime_budget") else {"daily_budget": spec["daily_budget"]}
        adset = {"name": spec["name"] + " - audience", "campaign_id": made["campaign"], "status": "PAUSED", **budget, "billing_event": "IMPRESSIONS",
                 "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "targeting": spec["targeting"]}
        if kind == "website":
            adset.update(optimization_goal="LANDING_PAGE_VIEWS" if spec["optimize_for"] == "landing_page_views" else "LINK_CLICKS", destination_type="WEBSITE")
            if spec.get("pixel_id"):
                adset["promoted_object"] = {"pixel_id": spec["pixel_id"], "custom_event_type": "PAGE_VIEW"}
        else:
            adset.update(optimization_goal="LEAD_GENERATION", destination_type="ON_AD", promoted_object={"page_id": acct["page_id"]})
        for key in ("start_time", "end_time"):
            if spec.get(key):
                adset[key] = spec[key]
        made["adset"] = graph._call("POST", "%s/adsets" % acct["act"], token=acct["token"], body=adset)["id"]
        link = {"message": spec["message"]}
        if spec["headline"]:
            link["name"] = spec["headline"]
        if spec["description"]:
            link["description"] = spec["description"]
        if spec["image_hash"]:
            link["image_hash"] = spec["image_hash"]
        if kind == "website":
            link.update(link=spec["link"], call_to_action={"type": spec["cta"], "value": {"link": spec["link"]}})
        else:
            link.update(link="https://fb.me/", call_to_action={"type": spec["cta"], "value": {"lead_gen_form_id": spec["form"]["id"]}})
        made["creative"] = graph._call("POST", "%s/adcreatives" % acct["act"], token=acct["token"], body={
            "name": spec["name"] + " - creative", "object_story_spec": {"page_id": acct["page_id"], "link_data": link}})["id"]
        made["ad"] = graph._call("POST", "%s/ads" % acct["act"], token=acct["token"], body={
            "name": spec["name"], "adset_id": made["adset"], "creative": {"creative_id": made["creative"]}, "status": "PAUSED"})["id"]
    except GraphError as exc:
        for k in ("ad", "adset", "campaign"):                         # best-effort: leave nothing half-built (a form and a creative cost nothing and stay)
            if made.get(k):
                try:
                    graph._call("DELETE", made[k], token=acct["token"])
                except GraphError:
                    pass
        cur.execute("insert into ads (tenant_id, account_id, name, meta_ids, spec, status, last_error, kind) values (%s, %s, %s, %s, %s, 'FAILED', %s, %s)",
                    (tenant_id, account_id, spec["name"], Json(made), Json(spec), Json(exc.as_dict()), kind))
        raise ads.AdsError("meta_refused", "Meta did not accept the ad: %s" % (exc.details or exc.message), exc.as_dict()) from exc
    cur.execute("insert into ads (tenant_id, account_id, name, meta_ids, spec, status, kind) values (%s, %s, %s, %s, %s, 'PAUSED', %s) returning id",
                (tenant_id, account_id, spec["name"], Json(made), Json(spec), kind))
    return {"id": str(cur.fetchone()[0]), "meta_ids": made, "status": "PAUSED", "kind": kind}


# ── words ────────────────────────────────────────────────────────────────────

COPY_PROMPT = ('You write short Facebook and Instagram ad copy for a small business in India. Answer JSON only: {"variants": [{"message": "<the main text, under 220 characters>", '
               '"headline": "<under 40 characters>", "description": "<under 60 characters>"}]} with exactly 3 variants that differ in angle (a benefit, a question, an offer). '
               'Use only facts the owner gave. Never invent prices, discounts or guarantees. No emojis, no all-caps shouting.')


def copy_variants(llm, *, goal: str, offer: str, audience: str = "", language: str = "English") -> tuple:
    """-> (list of {"message", "headline", "description"}, tokens). Raises LLMError when the model fails."""
    from prism_wa.llm import LLMError
    ask = "What the ad is for: %s\nThe offer or business: %s\nWho it is for: %s\nLanguage: %s" % ((goal or "").strip()[:200], (offer or "").strip()[:600], (audience or "").strip()[:200], language or "English")
    raw, tokens = llm.chat([{"role": "system", "content": COPY_PROMPT}, {"role": "user", "content": ask}], json_mode=True, max_tokens=700, temperature=0.7)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        data = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        data = json.loads(match.group(0)) if match else {}
    out = []
    for v in (data.get("variants") if isinstance(data, dict) else None) or []:
        if isinstance(v, dict) and str(v.get("message") or "").strip():
            out.append({"message": str(v["message"]).strip()[:2000], "headline": str(v.get("headline") or "").strip()[:255], "description": str(v.get("description") or "").strip()[:255]})
    if not out:
        raise LLMError("the language model sent no usable ad text")
    return out[:LANGUAGES_COPY], tokens
