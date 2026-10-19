"""The service-to-service API over HTTP: templates, campaigns, segments, contacts, consent."""
import json
import os

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.vault import Vault

INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}
TOKEN = "EAAB-ops-token"
BODY = {"type": "BODY", "text": "Hello {{1}}, our Diwali offer is on until Friday so please do reply soon.",
        "example": {"body_text": [["Asha"]]}}


class Meta:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        self.calls.append((method, url, body))
        if method == "POST" and url.endswith("/message_templates"):
            return 200, json.dumps({"id": "T1", "status": "PENDING", "category": body["category"]}).encode()
        if method == "DELETE" and "/message_templates" in url:
            return 200, json.dumps({"success": True}).encode()
        if method == "GET" and "/message_templates" in url:
            return 200, json.dumps({"data": [{"id": "T9", "name": "from_meta", "language": "en_US", "category": "UTILITY",
                                              "status": "APPROVED", "components": [BODY]}]}).encode()
        if url.endswith("/messages"):
            return 200, json.dumps({"messages": [{"id": "wamid.S%d" % len(self.calls)}]}).encode()
        return 404, b"{}"


@pytest.fixture
def api(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s, tier = 'TIER_10K'", (psycopg2.Binary(blob), ver))
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn),
                     internal_token=INTERNAL, graph=Graph(Meta()), vault=vault)
    client = TestClient(app, follow_redirects=False)

    class A:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers=AUTH)

    A.db, A.tid, A.nid, A.client_ = migrated, tid, number, client
    return A


def contact(api, n=3):
    for i in range(n):
        api.db.q("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s,%s,%s,'{vip}',true)",
                 (api.tid, "Asha Shah %d" % i, "91980020%04d" % i))


# ── the gate ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/internal/templates/list", "/internal/campaigns/create", "/internal/segments/count",
                                  "/internal/contacts/import", "/internal/consent/get", "/internal/contacts/erase"])
def test_every_route_needs_the_secret_and_a_workspace(api, path):
    assert api.client_.post(path, json={"tenant_id": str(api.tid)}).status_code == 401
    assert api.client_.post(path, json={"tenant_id": str(api.tid)}, headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert api.client_.post(path, json={}, headers=AUTH).status_code == 400
    assert api.client_.post(path, json={"tenant_id": "nope"}, headers=AUTH).status_code == 400
    assert api.client_.post(path, content=b"{bad", headers=AUTH).status_code == 400


# ── templates ────────────────────────────────────────────────────────────────

def test_template_lint_create_sync_list_and_library(api):
    bad = api.post("/internal/templates/lint", name="Bad Name", language="en_US", category="UTILITY", components=[BODY])
    assert bad.json()["valid"] is False and bad.json()["issues"][0]["code"] == "bad_name"
    assert api.post("/internal/templates/lint", name="ok_name", language="en_US", category="UTILITY", components=[BODY]).json()["valid"]

    refused = api.post("/internal/templates/create", name="Bad Name", category="UTILITY", components=[BODY])
    assert refused.status_code == 422 and refused.json()["error"] == "template_invalid" and refused.json()["issues"]
    made = api.post("/internal/templates/create", name="diwali_offer", category="MARKETING", components=[BODY])
    assert made.status_code == 200 and made.json()["status"] == "PENDING"

    assert api.post("/internal/templates/sync").json()["synced"] == 1
    names = {t["name"]: t["status"] for t in api.post("/internal/templates/list").json()["templates"]}
    assert names == {"diwali_offer": "PENDING", "from_meta": "APPROVED"}
    lib = api.post("/internal/templates/library").json()["library"]
    assert len(lib) >= 14 and {"key", "title", "category", "components"} <= set(lib[0])


def test_a_template_can_be_deleted_and_the_numbers_listed(api):
    made = api.post("/internal/templates/create", name="diwali_offer", category="MARKETING", components=[BODY]).json()
    gone = api.post("/internal/templates/delete", template_id=made["id"])
    assert gone.status_code == 200 and gone.json()["deleted"] is True
    assert api.post("/internal/templates/list").json()["templates"] == []
    nums = api.post("/internal/numbers/list").json()["numbers"]
    assert len(nums) == 1 and set(nums[0]) == {"id", "display", "status", "quality"}


# ── campaigns ────────────────────────────────────────────────────────────────

def make_template_approved(api):
    api.post("/internal/templates/create", name="diwali", category="MARKETING", components=[BODY])
    api.db.q("update templates set status = 'APPROVED' where name = 'diwali'")


def test_a_campaign_from_creation_to_report_over_http(api):
    contact(api, 3)
    mapping = {"1": {"source": "field", "field": "first_name"}}
    created = api.post("/internal/campaigns/create", name="Diwali", template_name="diwali", audience={"tags": ["vip"]}, variables=mapping)
    assert created.status_code == 200
    cid = created.json()["campaign_id"]

    pre = api.post("/internal/campaigns/preflight", campaign_id=cid).json()["preflight"]
    assert pre["ok"] is False and pre["blocking"][0]["code"] == "template_unknown"
    blocked = api.post("/internal/campaigns/launch", campaign_id=cid)
    assert blocked.status_code == 409 and blocked.json()["error"] == "preflight_failed" and blocked.json()["preflight"]["blocking"]

    make_template_approved(api)
    api.db.q("update campaigns set template_id = (select id from templates where name = 'diwali')")
    ok = api.post("/internal/campaigns/preflight", campaign_id=cid).json()["preflight"]
    assert ok["ok"] and ok["recipients"] == 3 and ok["cost"]["total"] > 0
    launched = api.post("/internal/campaigns/launch", campaign_id=cid)
    assert launched.status_code == 200 and launched.json()["queued"] == 3

    listing = api.post("/internal/campaigns/list").json()["campaigns"]
    assert [(c["name"], c["status"], c["total"]) for c in listing] == [("Diwali", "sending", 3)]
    assert api.post("/internal/campaigns/pause", campaign_id=cid).status_code == 200
    assert api.post("/internal/campaigns/pause", campaign_id=cid).json()["error"] == "wrong_state"
    assert api.post("/internal/campaigns/resume", campaign_id=cid).status_code == 200
    cancelled = api.post("/internal/campaigns/cancel", campaign_id=cid)
    assert cancelled.json()["dropped"] == 3
    rep = api.post("/internal/campaigns/report", campaign_id=cid).json()["report"]
    assert rep["campaign"]["status"] == "cancelled" and rep["counts"]["recipients"] == 3 and rep["counts"]["skipped"] == 3


def test_bad_campaign_requests_get_clear_answers(api):
    assert api.post("/internal/campaigns/create", name="x", template_name="t").status_code == 400            # no audience
    r = api.post("/internal/campaigns/create", name=" ", template_name="t", audience={"tags": ["a"]})
    assert r.status_code == 409 and r.json()["error"] == "name_required"
    r = api.post("/internal/campaigns/report", campaign_id="11111111-1111-4111-8111-111111111111")
    assert r.status_code == 409 and r.json()["error"] == "unknown_campaign"


def test_one_workspace_cannot_touch_anothers_campaign(api):
    other = H.tenant(api.db, "shakti")
    mine = api.post("/internal/campaigns/create", name="Mine", template_name="t", audience={"tags": ["a"]}).json()["campaign_id"]
    for action in ("preflight", "launch", "pause", "cancel", "report"):
        r = api.post("/internal/campaigns/" + action, tenant_id=str(other), campaign_id=mine)
        assert r.status_code == 409 and r.json()["error"] == "unknown_campaign", action
    assert api.post("/internal/campaigns/list", tenant_id=str(other)).json()["campaigns"] == []


# ── segments ─────────────────────────────────────────────────────────────────

def test_segment_validate_and_count(api):
    contact(api, 4)
    ok = api.post("/internal/segments/validate", filter={"field": "tags", "op": "has", "value": "vip"}).json()
    assert ok["valid"] is True and ok["message"] is None
    bad = api.post("/internal/segments/validate", filter={"field": "zzz", "op": "eq"}).json()
    assert bad["valid"] is False and "unknown field" in bad["message"]
    assert api.post("/internal/segments/count", filter={"tags": ["vip"]}).json()["count"] == 4
    assert api.post("/internal/segments/count", filter={"field": "zzz", "op": "eq"}).status_code == 400


# ── contacts and consent ─────────────────────────────────────────────────────

def test_import_export_and_erase_over_http(api):
    csv_text = "Name,Mobile,City\nAsha,9876543210,Vadodara\nBala,98765,Surat\n"
    r = api.post("/internal/contacts/import", csv=csv_text, attest_consent=True, evidence="oct.csv", tags=["oct"])
    rep = r.json()["report"]
    assert r.status_code == 200 and (rep["created"], rep["invalid"], rep["consent_recorded"]) == (1, 1, 1)
    assert api.post("/internal/contacts/import", csv="Name,City\nA,B\n").status_code == 400

    exported = api.post("/internal/contacts/export", mask=True).json()["csv"]
    assert "Asha" in exported and "********3210" in exported and "9876543210" not in exported
    assert "Asha" in api.post("/internal/contacts/export", tags=["oct"]).json()["csv"]

    cid = api.db.one("select id from contacts")
    assert api.post("/internal/contacts/erase", contact_id=str(cid)).json()["erased"] is True
    assert api.post("/internal/contacts/erase", contact_id=str(cid)).json()["erased"] is False
    assert api.db.one("select count(*) from contacts") == 0


def test_consent_settings_round_trip_and_bad_ones_are_refused(api):
    assert "stop" in api.post("/internal/consent/get").json()["settings"]["opt_out_keywords"]
    saved = api.post("/internal/consent/save", settings={"opt_out_keywords": ["stop", "band karo"], "confirm": False})
    assert saved.json()["settings"]["opt_out_keywords"] == ["stop", "band karo"]
    assert api.post("/internal/consent/get").json()["settings"]["confirm"] is False
    bad = api.post("/internal/consent/save", settings={"opt_out_keywords": []})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_settings"


# ── tracked links ────────────────────────────────────────────────────────────

def test_a_tracked_link_redirects_and_records_the_click_and_unknown_ones_are_404(api):
    contact(api, 1)
    cid = api.db.one("insert into campaigns (tenant_id, template_name, total) values (%s,'t',1) returning id", (api.tid,))
    person = api.db.one("select id from contacts")
    api.db.q("insert into short_links (token, tenant_id, campaign_id, contact_id, target) values ('abc123', %s, %s, %s, 'https://example.com/offer')",
             (api.tid, cid, person))
    r = api.client_.get("/r/abc123")
    assert r.status_code == 302 and r.headers["location"] == "https://example.com/offer"
    assert api.db.one("select count(*) from click_events where campaign_id = %s and contact_id = %s", (cid, person)) == 1
    assert api.client_.get("/r/nope").status_code == 404
    assert api.client_.get("/r/" + "x" * 65).status_code == 404
    api.db.q("update short_links set target = 'javascript:alert(1)'")
    assert api.client_.get("/r/abc123").status_code == 404                          # only web addresses are ever followed


# ── the contact book over HTTP ───────────────────────────────────────────────

def test_the_contact_book_over_http(api):
    r = api.post("/internal/contacts/create", name="Asha", phone="9876543210", tags=["vip"])
    assert r.status_code == 200 and r.json()["created"] is True
    cid = r.json()["contact_id"]
    assert api.post("/internal/contacts/create", name="X", phone="12").status_code == 400
    listed = api.post("/internal/contacts/list", search="asha", sort="name").json()
    assert listed["total"] == 1 and listed["contacts"][0]["id"] == cid and listed["contacts"][0]["tags"] == ["vip"]
    assert api.post("/internal/contacts/get", contact_id=cid).json()["contact"]["phone"] == "919876543210"
    assert api.post("/internal/contacts/get", contact_id="00000000-0000-0000-0000-000000000000").status_code == 404

    assert api.post("/internal/contacts/tags/set", contact_ids=[cid], add=["builder"], remove=["vip"]).json()["changed"] >= 1
    assert api.post("/internal/contacts/tags/set", contact_ids=[cid]).status_code == 400          # nothing to do
    assert {t["name"] for t in api.post("/internal/tags/list").json()["tags"]} >= {"builder"}
    assert api.post("/internal/tags/rename", old="builder", new="contractor").json()["changed"] == 1
    assert api.post("/internal/tags/create", name="a,b").status_code == 400

    assert api.post("/internal/attributes/define", key="City").json()["attribute"]["filled"] == 0
    api.post("/internal/contacts/update", contact_id=cid, changes={"attributes": {"City": "Surat"}})
    assert api.post("/internal/attributes/list").json()["attributes"][0]["filled"] == 1
    assert api.post("/internal/contacts/journey", contact_id=cid).json()["journey"]
    assert api.post("/internal/contacts/block", contact_ids=[cid], blocked=True).json()["changed"] == 1
    assert api.post("/internal/contacts/delete", contact_ids=[cid]).json()["deleted"] == 1


def test_a_mapped_import_is_logged_and_its_failures_can_be_downloaded(api):
    text = "Customer,Ph,Town\nAsha,9876543210,Surat\nBad,12,Vadodara\n"
    r = api.post("/internal/contacts/import", csv=text, file_name="leads.csv",
                 mapping={"Customer": "name", "Ph": "mobile", "Town": "attr:City"})
    assert r.status_code == 200
    report = r.json()["report"]
    assert (report["created"], report["invalid"]) == (1, 1) and report["import_id"]
    listed = api.post("/internal/contacts/imports").json()["imports"]
    assert [(i["file_name"], i["created"], i["invalid"]) for i in listed] == [("leads.csv", 1, 1)]
    got = api.post("/internal/contacts/imports/get", import_id=report["import_id"]).json()
    assert got["failures_csv"].startswith("Row,Why it did not import") and "valid phone number" in got["failures_csv"]


def test_an_export_can_be_a_selection_or_everyone_a_filter_matches(api):
    for name, phone, tags in (("Asha", "919800300001", "{vip}"), ("Ravi", "919800300002", "{}"), ("Meera", "919800300003", "{vip}")):
        api.db.q("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s,%s,%s,%s,true)", (api.tid, name, phone, tags))
    ids = [str(r["id"]) for r in api.db.q("select id from contacts where name = 'Ravi'")]
    by_ids = api.post("/internal/contacts/export", contact_ids=ids).json()["csv"]
    assert "Ravi" in by_ids and "Asha" not in by_ids
    by_filter = api.post("/internal/contacts/export", filter={"field": "tags", "op": "has", "value": ["vip"]}).json()["csv"]
    assert "Asha" in by_filter and "Meera" in by_filter and "Ravi" not in by_filter
    assert all(n in api.post("/internal/contacts/export").json()["csv"] for n in ("Asha", "Ravi", "Meera"))
