"""The usage ledger, the plan view, the reports (and their CSV downloads), a number's limits, the business profile, and the Inbox's
long poll. Real Postgres and a fake Graph."""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import inbox, inbox_view, media, number_info, usage
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.media_store import LocalStore
from prism_wa.vault import Vault

INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}
TOKEN = "EAAB-usage-token"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 3000


class Meta:
    def __init__(self):
        self.calls, self.info, self.profile, self.fail_profile = [], {}, {}, False

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data and headers.get("Content-Type", "").startswith("application/json") else None
        self.calls.append({"method": method, "url": url, "body": body, "auth": headers.get("Authorization")})
        if method == "GET" and "fields=verified_name" in url:
            return 200, json.dumps(self.info).encode()
        if method == "GET" and "whatsapp_business_profile" in url:
            if self.fail_profile:
                return 400, json.dumps({"error": {"message": "(#100) nope", "code": 100}}).encode()
            return 200, json.dumps({"data": [self.profile]}).encode()
        if method == "POST" and "whatsapp_business_profile" in url:
            if self.fail_profile:
                return 400, json.dumps({"error": {"message": "(#100) nope", "code": 100, "error_user_msg": "That website is not allowed"}}).encode()
            self.profile = {**self.profile, **{k: v for k, v in body.items() if k not in ("messaging_product", "profile_picture_handle")}}
            return 200, json.dumps({"success": True}).encode()
        if method == "POST" and "/uploads" in url and "upload:" not in url:
            return 200, json.dumps({"id": "upload:S1"}).encode()
        if method == "POST" and "upload:S1" in url:
            return 200, json.dumps({"h": "4::photo-handle"}).encode()
        return 404, b"{}"


@pytest.fixture
def api(migrated, tmp_path):
    vault = Vault({1: os.urandom(32)})
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tenant_id,))
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    meta = Meta()
    graph = Graph(meta)
    media.configure(graph=graph, vault=vault, store=LocalStore(str(tmp_path / "media")))

    def build(app_id="APP1"):
        return TestClient(create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token=INTERNAL,
                                     graph=graph, vault=vault, app_id=app_id), follow_redirects=False)
    client = build()

    class A:
        db, tid, nid, meta_, vault_ = migrated, tenant_id, number_id, meta, vault

        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tenant_id))
            return client.post(path, json=body, headers=AUTH)

        @staticmethod
        def post_without_app_id(path, **body):
            body.setdefault("tenant_id", str(tenant_id))
            return build("").post(path, json=body, headers=AUTH)

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tenant_id, *a, **kw)
                conn.commit()
                return out
            finally:
                conn.close()

        @classmethod
        def contact(cls, name="Asha", phone="919800100001"):
            cid = migrated.one("insert into contacts (tenant_id, name, phone, opted_in) values (%s,%s,%s,true) returning id", (tenant_id, name, phone))
            conv = migrated.one("insert into conversations (tenant_id, contact_id, channel, last_active_at, last_inbound_at, number_id)"
                                " values (%s,%s,'whatsapp',now(),now(),%s) returning id", (tenant_id, cid, number_id))
            return cid, conv

        @classmethod
        def message(cls, conv, *, direction="out", status="sent", at=NOW, pricing=None, sender="agent", sent_by=None, text="x"):
            return migrated.one("insert into messages (tenant_id, conversation_id, direction, sender, text, status, pricing, created_at, sent_by)"
                                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                                (tenant_id, conv, direction, sender, text, status, json.dumps(pricing) if pricing else None, at, sent_by))
    yield A
    media.configure(graph=None, vault=None, store=None)


def pricing(category, billable=True):
    return {"billable": billable, "pricing_model": "PMP", "category": category}


@pytest.fixture
def billed(api):
    _cid, conv = api.contact()
    for category, billable, n, day in (("marketing", True, 4, 17), ("marketing", False, 1, 17), ("utility", True, 5, 17), ("marketing", True, 2, 16),
                                        ("service", False, 3, 16)):
        for _ in range(n):
            api.message(conv, status="delivered", at=datetime(2026, 10, day, 10, tzinfo=timezone.utc), pricing=pricing(category, billable))
    api.message(conv, status="delivered", at=datetime(2026, 10, 17, 11, tzinfo=timezone.utc))           # no pricing yet: counted, not billed
    return api


# ── the usage ledger ─────────────────────────────────────────────────────────

def test_the_ledger_adds_up_meta_pricing_by_day_and_category(billed):
    out = billed.post("/internal/usage/summary", since="2026-10-16T00:00:00Z", until="2026-10-18T00:00:00Z").json()
    days = {d["day"]: d for d in out["days"]}
    assert set(days) == {"2026-10-16", "2026-10-17"}
    assert days["2026-10-17"]["messages"] == 11 and days["2026-10-17"]["billable"] == 9
    assert days["2026-10-17"]["by_category"]["MARKETING"] == {"messages": 5, "billable": 4, "cost": round(4 * 0.8631, 4)}
    assert days["2026-10-16"]["by_category"]["SERVICE"] == {"messages": 3, "billable": 0, "cost": 0.0}
    assert days["2026-10-17"]["by_category"]["UNKNOWN"]["messages"] == 1
    cats = {c["category"]: c for c in out["by_category"]}
    assert cats["MARKETING"]["billable"] == 6 and cats["UTILITY"]["billable"] == 5 and cats["UTILITY"]["rate"] == 0.115
    assert out["totals"]["billable"] == 11 and out["totals"]["messages"] == 16
    expected = round(6 * 0.8631 + 5 * 0.115, 2)
    assert out["totals"]["cost"] == expected and out["totals"]["cost_with_gst"] == round(expected * 1.18, 2) and out["is_estimate"] is True


def test_the_default_range_is_the_last_thirty_days_and_a_bad_one_is_refused(api):
    out = api.post("/internal/usage/summary").json()
    assert out["days"] == [] and out["totals"]["messages"] == 0
    since = datetime.fromisoformat(out["range"]["since"])
    assert 29 <= (datetime.fromisoformat(out["range"]["until"]) - since).days <= 30
    assert api.post("/internal/usage/summary", since="2026-10-18T00:00:00Z", until="2026-10-01T00:00:00Z").status_code == 400
    r = api.post("/internal/usage/summary", since="2024-01-01T00:00:00Z", until="2026-10-01T00:00:00Z")
    assert r.status_code == 400 and "400 days" in r.json()["message"]


def test_the_owner_can_set_the_rates_that_were_actually_charged(billed):
    assert billed.post("/internal/usage/rates").json()["rates"]["MARKETING"] == 0.8631
    r = billed.post("/internal/usage/rates_save", rates={"marketing": 0.5, "utility": 0.1}).json()
    assert r["rates"]["MARKETING"] == 0.5 and r["rates"]["UTILITY"] == 0.1 and r["rates"]["AUTHENTICATION"] == 0.115
    out = billed.post("/internal/usage/summary", since="2026-10-16T00:00:00Z", until="2026-10-18T00:00:00Z").json()
    assert out["totals"]["cost"] == round(6 * 0.5 + 5 * 0.1, 2)
    again = billed.post("/internal/usage/rates_save", rates={"marketing": ""}).json()
    assert again["rates"]["MARKETING"] == 0.8631                                         # blank puts a category back to the default


@pytest.mark.parametrize("bad", [{"nonsense": 1}, {"marketing": -1}, {"marketing": "lots"}, {"marketing": 5000}, {"marketing": True}])
def test_a_rate_that_makes_no_sense_is_refused(api, bad):
    r = api.post("/internal/usage/rates_save", rates=bad)
    assert r.status_code == 400 and r.json()["error"] == "bad_rate"


def test_only_a_manager_changes_the_rate_card(api):
    owner = api.do(inbox.ensure_agent, "seat-owner", "Owner")
    agent = api.do(inbox.ensure_agent, "seat-bala", "Bala")
    r = api.post("/internal/usage/rates_save", rates={"marketing": 1}, by_agent_id=str(agent["id"]))
    assert r.status_code == 409 and "owner or manager" in r.json()["message"]
    assert api.post("/internal/usage/rates_save", rates={"marketing": 1}, by_agent_id=str(owner["id"])).status_code == 200


# ── the plan view ────────────────────────────────────────────────────────────

def test_the_plan_view_puts_each_limit_against_what_is_used(billed):
    billed.do(inbox.ensure_agent, "seat-owner", "Owner")
    billed.db.q("insert into tag_defs (tenant_id, name) values (%s, 'vip')", (billed.tid,))
    out = billed.post("/internal/usage/plan", plan="premium").json()
    assert out["plan"] == "premium" and out["label"] == "Premium" and out["next_plan"] == "unlimited"
    rows = {r["key"]: r for r in out["limits"]}
    assert rows["agents"] == {"key": "agents", "limit": 10, "used": 1} and rows["tags"]["used"] == 1 and rows["tags"]["limit"] == 250
    assert rows["storage_mb"]["limit"] == 5120 and rows["webhooks"]["limit"] == 1 and rows["ai_messages"]["limit"] == 1000
    assert "reports_download" in out["features"] and out["is_estimate"] is True and out["gst"] == 0.18
    assert out["month"]["totals"]["messages"] >= 0 and "label" in out["month"]


def test_the_enterprise_plan_has_no_ceilings(api):
    out = api.post("/internal/usage/plan", plan="enterprise").json()
    assert out["next_plan"] is None and all(r["limit"] is None for r in out["limits"])


# ── the reports ──────────────────────────────────────────────────────────────

@pytest.fixture
def busy(api):
    owner = api.do(inbox.ensure_agent, "seat-owner", "Owner")
    bala = api.do(inbox.ensure_agent, "seat-bala", "Bala")
    _c1, conv1 = api.contact("Asha", "919800100001")
    _c2, conv2 = api.contact("Ravi", "919800100002")
    day = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)
    api.db.q("update conversations set assignee_id = %s, status = 'intervened' where id = %s", (bala["id"], conv1))
    api.db.q("update conversations set created_at = %s", (day,))
    api.db.q("update contacts set created_at = %s", (day,))
    api.message(conv1, direction="in", sender="customer", status="", at=day)
    api.message(conv1, direction="out", status="read", at=day + timedelta(minutes=2), sent_by=bala["id"])
    api.message(conv1, direction="out", status="delivered", at=day + timedelta(minutes=5), sent_by=bala["id"], pricing=pricing("utility"))
    api.message(conv1, direction="out", status="failed", at=day + timedelta(minutes=6), sender="bot")
    api.message(conv2, direction="in", sender="customer", status="", at=day + timedelta(hours=1))
    api.message(conv2, direction="out", status="sent", at=day + timedelta(hours=2), sender="campaign")
    api.db.q("insert into conversation_events (tenant_id, conversation_id, kind, actor_id, at) values (%s, %s, 'requested', null, %s)", (api.tid, conv1, day))
    api.db.q("insert into conversation_events (tenant_id, conversation_id, kind, actor_id, at) values (%s, %s, 'resolved', %s, %s)", (api.tid, conv2, bala["id"], day))
    for action, source, n in (("opt_in", "csv_import", 3), ("opt_out", "keyword", 2)):
        for _ in range(n):
            api.db.q("insert into consent_ledger (tenant_id, contact_id, action, source, at) values (%s, (select id from contacts limit 1), %s, %s, %s)",
                     (api.tid, action, source, day))
    api.owner, api.bala = owner, bala
    return api


RANGE = {"since": "2026-10-17T00:00:00Z", "until": "2026-10-18T00:00:00Z"}


def test_the_overview_counts_messages_by_day_and_what_they_became(busy):
    out = busy.post("/internal/reports/overview", **RANGE).json()
    assert len(out["days"]) == 1
    d = out["days"][0]
    assert (d["sent"], d["delivered"], d["read"], d["failed"], d["received"]) == (4, 2, 1, 1, 2) and d["new_chats"] == 2 and d["new_contacts"] == 2
    t = out["totals"]
    assert t["handoffs"] == 1 and t["delivered_rate"] == 50.0 and t["read_rate"] == 25.0 and t["failed_rate"] == 25.0
    senders = {s["sender"]: s["messages"] for s in out["by_sender"]}
    assert senders == {"agent": 2, "bot": 1, "campaign": 1}


def test_the_team_report_counts_what_each_person_did(busy):
    out = busy.post("/internal/reports/agents", **RANGE).json()["agents"]
    bala = next(a for a in out if a["name"] == "Bala")
    assert bala["replies"] == 2 and bala["chats"] == 1 and bala["resolved"] == 1 and bala["open_now"] == 1
    assert bala["avg_reply_seconds"] == 120.0                                            # the customer wrote at 09:00, Bala answered at 09:02
    owner = next(a for a in out if a["name"] == "Owner")
    assert owner["replies"] == 0 and owner["avg_reply_seconds"] is None


def test_the_opt_in_report_says_how_people_came_and_went(busy):
    out = busy.post("/internal/reports/optins", **RANGE).json()
    assert out["totals"] == {"opt_in": 3, "opt_out": 2} and out["days"] == [{"day": "2026-10-17", "opt_in": 3, "opt_out": 2}]
    assert {(s["action"], s["source"], s["count"]) for s in out["sources"]} == {("opt_in", "csv_import", 3), ("opt_out", "keyword", 2)}
    assert out["now"]["contacts"] == 2


def test_the_dashboard_is_one_answer_for_the_whole_screen(busy):
    out = busy.post("/internal/reports/dashboard", **RANGE).json()
    assert set(out) >= {"overview", "usage", "agents", "optins", "campaigns", "open_chats", "can_download", "range"}
    assert out["open_chats"] == 2 and out["can_download"] is True and out["usage"]["is_estimate"] is True
    assert out["usage"]["totals"]["billable"] == 1 and out["overview"]["totals"]["sent"] == 4


def test_the_campaigns_report_lists_each_with_its_results(busy):
    busy.db.q("insert into campaigns (tenant_id, name, template_name, status, total, sent, failed, created_at) values (%s, 'Diwali', 'diwali_offer', 'done', 10, 9, 1, %s)",
              (busy.tid, datetime(2026, 10, 17, 8, tzinfo=timezone.utc)))
    rows = busy.post("/internal/reports/campaigns", **RANGE).json()["campaigns"]
    assert len(rows) == 1 and rows[0]["name"] == "Diwali" and rows[0]["template"] == "diwali_offer" and "read" in rows[0] and "spend" in rows[0]


@pytest.mark.parametrize("kind,header", [("overview", "Day,Sent,Delivered"), ("usage", "Day,Messages,Billable"), ("campaigns", "Name,Status,Template"),
                                          ("agents", "Name,Role,Replies"), ("optins", "Day,Opted in,Opted out")])
def test_every_report_downloads_as_a_csv_file(busy, kind, header):
    out = busy.post("/internal/reports/download", kind=kind, **RANGE).json()
    assert out["filename"] == "prism-whatsapp-%s-2026-10-17_to_2026-10-17.csv" % kind
    assert out["csv"].startswith(header)
    assert out["rows"] == out["csv"].count("\n") - 1


def test_the_overview_csv_has_the_days_numbers(busy):
    rows = busy.post("/internal/reports/download", kind="overview", **RANGE).json()["csv"].strip().splitlines()
    assert rows[1] == "2026-10-17,4,2,1,1,2,2,2"


def test_downloads_are_a_premium_feature_and_the_kind_must_exist(busy):
    r = busy.post("/internal/reports/download", kind="overview", plan="pro", **RANGE)
    assert r.status_code == 403 and r.json()["needs"] == "premium"
    assert busy.post("/internal/reports/download", kind="nonsense", **RANGE).status_code == 400
    assert busy.post("/internal/reports/dashboard", plan="pro", **RANGE).json()["can_download"] is False


# ── who sent a reply ─────────────────────────────────────────────────────────

def test_a_reply_typed_in_the_inbox_is_credited_to_the_person_who_typed_it(api):
    owner = api.do(inbox.ensure_agent, "seat-owner", "Owner")
    cid, conv = api.contact()
    r = api.post("/internal/messages/send_text", conversation_id=str(conv), text="Hello there", agent_id=str(owner["id"]), client_ref="k1")
    assert r.status_code == 200, r.text
    assert api.db.one("select sent_by from messages where direction = 'out'") == owner["id"]
    out = api.post("/internal/reports/agents", since=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                   until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()).json()["agents"]
    assert next(a for a in out if a["name"] == "Owner")["replies"] == 1


# ── a number's limits ────────────────────────────────────────────────────────

def test_a_numbers_limits_are_read_from_meta_and_kept(api):
    api.meta_.info = {"verified_name": "Patel Constructions", "name_status": "APPROVED", "quality_rating": "GREEN", "messaging_limit_tier": "TIER_10K",
                      "throughput": {"level": "STANDARD"}, "display_phone_number": "+91 98765 43210", "status": "CONNECTED"}
    out = api.post("/internal/numbers/refresh").json()
    assert out["problems"] == []
    n = out["numbers"][0]
    assert (n["tier"], n["daily_limit"], n["next_limit"], n["name_status"], n["verified_name"], n["throughput"], n["quality"]) == \
        ("TIER_10K", 10000, 100000, "APPROVED", "Patel Constructions", "STANDARD", "GREEN")
    assert n["checked_at"] is not None
    kept = api.post("/internal/numbers/limits").json()
    assert kept["numbers"][0]["tier"] == "TIER_10K" and kept["tiers"]["TIER_UNLIMITED"] is None


def test_an_unlimited_number_has_no_next_step_and_a_refusal_keeps_what_was_known(api):
    api.meta_.info = {"messaging_limit_tier": "TIER_UNLIMITED", "name_status": "APPROVED"}
    n = api.post("/internal/numbers/refresh").json()["numbers"][0]
    assert n["unlimited"] is True and n["daily_limit"] is None and n["next_limit"] is None
    api.meta_.info = {"error": {"message": "(#190) token expired", "code": 190}}
    out = api.post("/internal/numbers/refresh").json()
    assert out["numbers"][0]["tier"] == "TIER_UNLIMITED" and "token expired" in out["problems"][0]["message"]


# ── the business profile ─────────────────────────────────────────────────────

def test_the_profile_is_read_from_meta(api):
    api.meta_.profile = {"about": "We build compound walls", "address": "Alkapuri, Vadodara", "email": "hi@patel.in", "websites": ["https://patel.in"],
                         "vertical": "PROF_SERVICES", "profile_picture_url": "https://pps.whatsapp.net/x"}
    out = api.post("/internal/profile/get").json()
    assert out["profile"]["about"] == "We build compound walls" and out["stale"] is False and "number" in out


def test_a_stale_profile_is_shown_when_meta_cannot_be_reached(api):
    api.meta_.profile = {"about": "Before"}
    api.post("/internal/profile/get")
    api.meta_.fail_profile = True
    out = api.post("/internal/profile/get").json()
    assert out["stale"] is True and out["profile"]["about"] == "Before" and "nope" in out["problem"]
    api.db.q("update phone_numbers set business_profile = null")
    r = api.post("/internal/profile/get")
    assert r.status_code == 502 and r.json()["error"] == "meta_error" and "nope" in r.json()["message"]


def test_the_profile_is_saved_to_meta_and_read_back(api):
    r = api.post("/internal/profile/save", profile={"about": "Walls and gates", "email": "hi@patel.in", "websites": ["https://patel.in", ""], "vertical": "retail"})
    assert r.status_code == 200, r.text
    sent = next(c for c in api.meta_.calls if c["method"] == "POST" and "whatsapp_business_profile" in c["url"])
    assert sent["body"] == {"messaging_product": "whatsapp", "about": "Walls and gates", "email": "hi@patel.in", "websites": ["https://patel.in"], "vertical": "RETAIL"}
    assert r.json()["profile"]["about"] == "Walls and gates"


def test_the_photo_comes_from_the_library_and_goes_to_meta_as_a_handle(api):
    up = api.post("/internal/media/upload", filename="logo.png", mime="image/png", data=base64.b64encode(PNG).decode()).json()["asset"]
    r = api.post("/internal/profile/save", profile={"picture_asset_id": up["id"]})
    assert r.status_code == 200, r.text
    sent = next(c for c in api.meta_.calls if c["method"] == "POST" and "whatsapp_business_profile" in c["url"])
    assert sent["body"]["profile_picture_handle"] == "4::photo-handle"
    pdf = api.post("/internal/media/upload", filename="a.pdf", mime="application/pdf", data=base64.b64encode(b"%PDF-1.4 " + b"1" * 500).decode()).json()["asset"]
    bad = api.post("/internal/profile/save", profile={"picture_asset_id": pdf["id"]})
    assert bad.status_code == 400 and "JPG or PNG" in bad.json()["message"]
    assert api.post_without_app_id("/internal/profile/save", profile={"picture_asset_id": up["id"]}).status_code == 409


@pytest.mark.parametrize("profile,words", [
    ({"about": ""}, "About"), ({"about": "x" * 140}, "About"), ({"address": "x" * 257}, "address"), ({"description": "x" * 513}, "description"),
    ({"email": "not-an-email"}, "email"), ({"websites": ["a", "b", "c"]}, "At most 2"), ({"websites": ["patel.in"]}, "full address"),
    ({"vertical": "SPACESHIPS"}, "category")])
def test_a_profile_meta_would_refuse_is_refused_here_first(api, profile, words):
    r = api.post("/internal/profile/save", profile=profile)
    assert r.status_code == 400 and words in r.json()["message"]
    assert not any(c["method"] == "POST" and "whatsapp_business_profile" in c["url"] for c in api.meta_.calls)


def test_meta_refusing_a_profile_is_explained(api):
    api.meta_.fail_profile = True
    r = api.post("/internal/profile/save", profile={"about": "Fine"})
    assert r.status_code == 400 and "not allowed" in r.json()["message"]


def test_nothing_to_change_is_said_plainly(api):
    r = api.post("/internal/profile/save", profile={})
    assert r.status_code == 400 and r.json()["error"] == "nothing_to_save"


# ── the Inbox, live ──────────────────────────────────────────────────────────

def test_the_long_poll_answers_at_once_when_it_has_no_cursor_and_when_something_changed(api):
    first = api.post("/internal/inbox/changes", cursor="", wait=0).json()
    assert first["ok"] and first["changed"] is False
    _cid, conv = api.contact()
    out = api.post("/internal/inbox/changes", cursor=first["cursor"], wait=5).json()
    assert out["changed"] is True and out["cursor"] != first["cursor"]


def test_the_long_poll_waits_and_then_says_nothing_changed(api):
    api.contact()
    cursor = api.post("/internal/inbox/changes", cursor="", wait=0).json()["cursor"]
    started = time.monotonic()
    out = api.post("/internal/inbox/changes", cursor=cursor, wait=2).json()
    assert out["changed"] is False and out["cursor"] == cursor and 1.5 <= time.monotonic() - started < 6


def test_the_long_poll_wakes_when_a_team_event_happens(api):
    _cid, conv = api.contact()
    cursor = api.post("/internal/inbox/changes", cursor="", wait=0).json()["cursor"]
    time.sleep(0.05)
    api.db.q("insert into conversation_events (tenant_id, conversation_id, kind, at) values (%s, %s, 'requested', now() + interval '1 second')", (api.tid, conv))
    assert api.post("/internal/inbox/changes", cursor=cursor, wait=3).json()["changed"] is True


def test_the_long_poll_checks_its_caller_and_its_request(api):
    client = TestClient(create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(api.db.dsn), internal_token=INTERNAL,
                                   graph=Graph(api.meta_), vault=api.vault_))
    assert client.post("/internal/inbox/changes", json={"tenant_id": str(api.tid)}).status_code == 401
    assert client.post("/internal/inbox/changes", json={}, headers=AUTH).status_code == 400
    bad_wait = client.post("/internal/inbox/changes", json={"tenant_id": str(api.tid), "wait": "abc", "cursor": ""}, headers=AUTH)
    assert bad_wait.status_code == 200 and bad_wait.json()["changed"] is False                  # a nonsense wait means "do not wait"
