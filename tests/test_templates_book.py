"""The Templates tab's backend beyond create/sync: message validity, a sample file for a media header, the AI writer and rejection
helper, per-template stats, and the library by industry. Real Postgres, a fake Graph, a fake language model."""
import json
import os
import threading

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import ai_agent, media, template_ai, templates
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.llm import LLM
from prism_wa.media_store import LocalStore
from prism_wa.vault import Vault

INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}
TOKEN = "EAAB-book-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 2000
PDF = b"%PDF-1.4 " + b"1" * 3000
GOOD = [{"type": "BODY", "text": "Hello {{1}}, your order {{2}} is packed and leaves our warehouse tomorrow morning.",
         "example": {"body_text": [["Asha", "A-1"]]}}]


class Meta:
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, method, url, data, headers):
        body = None
        if data and headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(data)
        with self.lock:
            self.calls.append({"method": method, "url": url, "body": body, "size": len(data or b""), "auth": headers.get("Authorization")})
        if method == "POST" and url.endswith("/message_templates"):
            return 200, json.dumps({"id": "T1", "status": "PENDING", "category": body["category"]}).encode()
        if method == "POST" and "/uploads" in url and "upload:" not in url:
            return 200, json.dumps({"id": "upload:SESSION1"}).encode()
        if method == "POST" and "upload:SESSION1" in url:
            return 200, json.dumps({"h": "4::handle-xyz"}).encode()
        return 404, b"{}"


class FakeLLM:
    """A chat-completions endpoint that answers with whatever the test set."""

    def __init__(self):
        self.answer, self.asked = "", []

    def __call__(self, method, url, data, headers):
        body = json.loads(data)
        self.asked.append(body)
        return 200, json.dumps({"choices": [{"message": {"content": self.answer}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}).encode()


def suggestion(**kw):
    out = {"name": "Diwali Offer!", "category": "MARKETING", "header": None,
           "body": "Hello {{1}}, our Diwali offer on steel gates ends on Friday so please reply to this message soon.",
           "samples": ["Asha"], "footer": None, "buttons": [{"type": "QUICK_REPLY", "text": "Tell me more"}, {"type": "QUICK_REPLY", "text": "Stop messages"}]}
    out.update(kw)
    return out


@pytest.fixture
def api(migrated, tmp_path):
    vault = Vault({1: os.urandom(32)})
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tenant_id,))
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s, tier = 'TIER_10K'", (psycopg2.Binary(blob), ver))
    meta, llm_wire = Meta(), FakeLLM()
    graph = Graph(meta)
    media.configure(graph=graph, vault=vault, store=LocalStore(str(tmp_path / "media")))
    ai_agent.configure(LLM(api_key="k", transport=llm_wire))

    def build(app_id="APP1"):
        app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token=INTERNAL,
                         graph=graph, vault=vault, app_id=app_id)
        return TestClient(app, follow_redirects=False)
    client = build()

    class A:
        db, tid, nid, meta_, llm_, vault_ = migrated, tenant_id, number_id, meta, llm_wire, vault

        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tenant_id))
            return client.post(path, json=body, headers=AUTH)

        @staticmethod
        def without_app_id(path, **body):
            body.setdefault("tenant_id", str(tenant_id))
            return build("").post(path, json=body, headers=AUTH)

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def upload(cls, name, mime, data):
            import base64
            r = cls.post("/internal/media/upload", filename=name, mime=mime, data=base64.b64encode(data).decode())
            assert r.status_code == 200, r.text
            return r.json()["asset"]
    yield A
    media.configure(graph=None, vault=None, store=None)
    ai_agent.configure(None)


# ── the library ──────────────────────────────────────────────────────────────

def test_the_library_comes_with_its_industries(api):
    out = api.post("/internal/templates/library").json()
    assert len(out["library"]) >= 100 and "Construction" in out["industries"] and "Healthcare and clinics" in out["industries"]
    assert all(t["industry"] in out["industries"] for t in out["library"])


# ── message validity ─────────────────────────────────────────────────────────

def test_a_template_can_be_given_a_validity_and_meta_hears_of_it(api):
    r = api.post("/internal/templates/create", name="order_ready", category="UTILITY", components=GOOD, ttl_seconds=3600, plan="premium")
    assert r.status_code == 200, r.text
    sent = next(c for c in api.meta_.calls if c["url"].endswith("/message_templates"))
    assert sent["body"]["message_send_ttl_seconds"] == 3600
    assert api.db.one("select ttl_seconds from templates where name = 'order_ready'") == 3600
    assert api.post("/internal/templates/list").json()["templates"][0]["ttl_seconds"] == 3600


def test_a_template_without_a_validity_says_nothing_to_meta_about_it(api):
    api.post("/internal/templates/create", name="order_ready", category="UTILITY", components=GOOD, plan="premium")
    sent = next(c for c in api.meta_.calls if c["url"].endswith("/message_templates"))
    assert "message_send_ttl_seconds" not in sent["body"] and api.db.one("select ttl_seconds from templates") is None
    api.post("/internal/templates/create", name="order_two", category="UTILITY", components=GOOD, ttl_seconds=-1, plan="basic")
    assert api.db.one("select ttl_seconds from templates where name = 'order_two'") is None                     # Meta's default is free on every plan


def test_a_validity_outside_the_range_is_refused_with_the_range(api):
    r = api.post("/internal/templates/create", name="order_ready", category="UTILITY", components=GOOD, ttl_seconds=90000, plan="premium")
    assert r.status_code == 422 and any(i["code"] == "bad_ttl" for i in r.json()["issues"])
    assert api.db.q("select 1 from templates") == []
    lint = api.post("/internal/templates/lint", name="order_ready", language="en_US", category="UTILITY", components=GOOD, ttl_seconds=90000).json()
    assert lint["valid"] is False and "12 hours" in lint["issues"][0]["message"]


def test_setting_a_validity_needs_the_plan_that_includes_it(api):
    r = api.post("/internal/templates/create", name="order_ready", category="UTILITY", components=GOOD, ttl_seconds=3600, plan="pro")
    assert r.status_code == 403 and "template" in r.json()["message"].lower() and r.json()["needs"] == "premium"
    assert api.db.q("select 1 from templates") == []


# ── a sample file for a media header ─────────────────────────────────────────

def test_a_library_picture_becomes_a_sample_handle_from_meta(api):
    a = api.upload("offer.png", "image/png", PNG)
    r = api.post("/internal/templates/upload_sample", asset_id=a["id"], format="image")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "handle": "4::handle-xyz", "asset_id": a["id"], "kind": "image"}
    opened = next(c for c in api.meta_.calls if "/uploads" in c["url"] and "upload:" not in c["url"])
    assert "APP1/uploads" in opened["url"] and "file_length=%d" % len(PNG) in opened["url"] and "file_type=image%2Fpng" in opened["url"]
    sent = next(c for c in api.meta_.calls if "upload:SESSION1" in c["url"])
    assert sent["size"] == len(PNG) and sent["auth"] == "OAuth " + TOKEN


def test_a_sample_must_be_the_kind_the_header_shows(api):
    a = api.upload("brochure.pdf", "application/pdf", PDF)
    r = api.post("/internal/templates/upload_sample", asset_id=a["id"], format="image")
    assert r.status_code == 400 and "that file is a document" in r.json()["message"]
    assert api.post("/internal/templates/upload_sample", asset_id=a["id"], format="document").status_code == 200
    audio = api.upload("note.ogg", "audio/ogg", b"OggS" + b"0" * 500)
    assert api.post("/internal/templates/upload_sample", asset_id=audio["id"]).status_code == 400


def test_a_sample_needs_a_known_file_and_the_meta_app(api):
    assert api.post("/internal/templates/upload_sample", asset_id="00000000-0000-0000-0000-000000000000").status_code == 404
    a = api.upload("offer.png", "image/png", PNG)
    r = api.without_app_id("/internal/templates/upload_sample", asset_id=a["id"])
    assert r.status_code == 409 and "WA_APP_ID" in r.json()["message"]


# ── the AI writer ────────────────────────────────────────────────────────────

def test_the_writer_returns_checked_templates_named_and_categorised(api):
    api.llm_.answer = json.dumps({"templates": [suggestion(), suggestion(body="Hello {{1}}, our offer ends soon so do reply to this message today.",
                                                                          samples=["Ravi"])]})
    r = api.post("/internal/templates/generate", description="Diwali offer on steel gates for builders", tone="warm", goal="replies", industry="Construction")
    assert r.status_code == 200, r.text
    out = r.json()["templates"]
    assert [t["name"] for t in out] == ["diwali_offer", "diwali_offer_2"]                        # snake_case, and never the same twice
    assert out[0]["category"] == "MARKETING" and out[0]["valid"] is True and out[0]["issues"] == []
    assert out[0]["components"][0]["example"] == {"body_text": [["Asha"]]}
    assert out[0]["components"][-1]["buttons"][1]["text"] == "Stop messages"
    asked = api.llm_.asked[0]
    assert asked["response_format"] == {"type": "json_object"} and "Construction" in asked["messages"][1]["content"] and "warm" in asked["messages"][1]["content"]
    assert "NEVER be the first or last" in asked["messages"][0]["content"]


def test_a_suggestion_that_breaks_a_rule_is_shown_with_its_problems_not_hidden(api):
    api.llm_.answer = json.dumps({"templates": [suggestion(body="{{1}} thanks for your order {{2}}", samples=["Asha", "A-1"])]})
    out = api.post("/internal/templates/generate", description="thank people for orders please").json()["templates"][0]
    assert out["valid"] is False and {"variable_at_start", "variable_at_end"} <= {i["code"] for i in out["issues"]}


def test_the_chosen_category_wins_and_a_blank_one_is_left_to_the_writer(api):
    api.llm_.answer = json.dumps({"templates": [suggestion(category="UTILITY")]})
    assert api.post("/internal/templates/generate", description="order updates for customers", category="MARKETING").json()["templates"][0]["category"] == "MARKETING"
    assert api.post("/internal/templates/generate", description="order updates for customers").json()["templates"][0]["category"] == "UTILITY"
    assert api.post("/internal/templates/generate", description="order updates for customers", category="AUTHENTICATION").status_code == 400


def test_the_writer_survives_a_fenced_or_chatty_answer_and_refuses_nonsense(api):
    api.llm_.answer = "Sure, here you go:\n```json\n" + json.dumps({"templates": [suggestion()]}) + "\n```"
    assert api.post("/internal/templates/generate", description="a festival greeting for customers").status_code == 200
    api.llm_.answer = "I cannot help with that"
    r = api.post("/internal/templates/generate", description="a festival greeting for customers")
    assert r.status_code == 502 and r.json()["error"] == "ai_failed"
    api.llm_.answer = json.dumps({"templates": []})
    assert api.post("/internal/templates/generate", description="a festival greeting for customers").status_code == 502


def test_the_writer_needs_a_real_description_and_a_configured_model(api):
    assert api.post("/internal/templates/generate", description="hi").status_code == 400
    assert api.post("/internal/templates/generate", description="x" * 601).status_code == 400
    ai_agent.configure(None)
    r = api.post("/internal/templates/generate", description="a festival greeting for customers")
    assert r.status_code == 409 and "WA_LLM_API_KEY" in r.json()["message"]


def test_how_many_templates_are_asked_for_is_bounded(api):
    api.llm_.answer = json.dumps({"templates": [suggestion(name="a%d" % i) for i in range(9)]})
    out = api.post("/internal/templates/generate", description="a festival greeting for customers", count=99).json()["templates"]
    assert len(out) == 5 and "Write 5 different" in api.llm_.asked[0]["messages"][1]["content"]


# ── the rejection helper ─────────────────────────────────────────────────────

def seed_rejected(api, reason="TAG_CONTENT_MISMATCH"):
    waba = api.db.one("select id from wabas")
    return api.db.one("insert into templates (tenant_id, waba_id, name, language, category, status, reject_reason, components)"
                      " values (%s, %s, 'diwali_offer', 'en_US', 'UTILITY', 'REJECTED', %s, %s) returning id",
                      (api.tid, waba, reason, json.dumps(GOOD)))


def test_a_rejected_template_gets_plain_hints_and_a_rewrite(api):
    tid = seed_rejected(api)
    api.llm_.answer = json.dumps({"templates": [suggestion(category="MARKETING")]})
    out = api.post("/internal/templates/rewrite", template_id=str(tid)).json()
    assert out["reason"] == "TAG_CONTENT_MISMATCH" and "category" in out["hints"][0].lower() and out["ai"] is True
    s = out["suggestion"]
    assert s["name"] == "diwali_offer" and s["category"] == "MARKETING" and s["valid"] is True            # the same name: Meta replaces the rejected one
    assert "TAG_CONTENT_MISMATCH" in api.llm_.asked[0]["messages"][1]["content"]


def test_without_the_model_the_hints_alone_are_given(api):
    tid = seed_rejected(api, "INVALID_FORMAT")
    ai_agent.configure(None)
    out = api.post("/internal/templates/rewrite", template_id=str(tid)).json()
    assert out["suggestion"] is None and out["ai"] is False and "variables" in out["hints"][0]
    tid2 = seed_rejected_named(api)
    assert api.post("/internal/templates/rewrite", template_id=str(tid2)).json()["hints"] == template_ai.hints_for("NONE")


def seed_rejected_named(api):
    waba = api.db.one("select id from wabas")
    return api.db.one("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s, %s, 'other_one', 'en_US', 'UTILITY', 'REJECTED', %s)"
                      " returning id", (api.tid, waba, json.dumps(GOOD)))


def test_a_model_that_fails_leaves_the_hints_standing(api):
    tid = seed_rejected(api)
    api.llm_.answer = "not json at all"
    out = api.post("/internal/templates/rewrite", template_id=str(tid)).json()
    assert out["suggestion"] is None and out["hints"]


def test_an_unknown_or_malformed_template_id_is_refused(api):
    assert api.post("/internal/templates/rewrite", template_id="00000000-0000-0000-0000-000000000000").status_code == 409
    assert api.post("/internal/templates/rewrite", template_id="nope").status_code == 400


# ── per-template stats ───────────────────────────────────────────────────────

def test_stats_count_what_each_template_has_sent_delivered_read_and_failed(api):
    cid = api.db.one("insert into contacts (tenant_id, name, phone, opted_in) values (%s, 'Asha', '919800100001', true) returning id", (api.tid,))
    conv = api.db.one("insert into conversations (tenant_id, contact_id, channel, last_active_at, number_id) values (%s, %s, 'whatsapp', now(), %s) returning id",
                      (api.tid, cid, api.nid))
    rows = [("diwali_offer", "read"), ("diwali_offer", "delivered"), ("diwali_offer", "sent"), ("diwali_offer", "failed"), ("order_ready", "read")]
    for i, (name, status) in enumerate(rows):
        mid = api.db.one("insert into messages (tenant_id, conversation_id, direction, sender, text, msg_type, status) values (%s, %s, 'out', 'agent', 'x', 'template', %s)"
                         " returning id", (api.tid, conv, status))
        state = "failed" if status == "failed" else "sent"
        api.db.q("insert into outbox (tenant_id, number_id, to_phone, kind, payload, idem_key, message_id, state) values (%s, %s, '919800100001', 'template', %s, %s, %s, %s)",
                 (api.tid, api.nid, json.dumps({"type": "template", "template": {"name": name, "language": {"code": "en_US"}}}), "k%d" % i, mid, state))
    out = {s["name"]: s for s in api.post("/internal/templates/stats").json()["stats"]}
    d = out["diwali_offer"]
    assert (d["sent"], d["delivered"], d["read"], d["failed"]) == (3, 2, 1, 1) and d["read_rate"] == 33.3
    assert out["order_ready"]["read"] == 1 and out["order_ready"]["read_rate"] == 100.0 and out["order_ready"]["clicks"] == 0


def test_a_workspace_that_has_sent_nothing_has_empty_stats(api):
    assert api.post("/internal/templates/stats").json() == {"ok": True, "stats": []}
