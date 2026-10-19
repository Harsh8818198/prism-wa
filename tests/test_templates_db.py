"""Templates against a real Postgres and a fake Graph: create, sync, webhooks, and
the rule that an unapproved template is never queued."""
import json
import os
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import send, templates
from prism_wa.graph import Graph
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

TOKEN = "EAAB-templates-token"
GOOD = [{"type": "BODY", "text": "Hello {{1}}, your order {{2}} is packed and leaves our warehouse tomorrow morning.",
         "example": {"body_text": [["Asha", "A-1"]]}}]


class Meta:
    def __init__(self):
        self.calls, self.reject, self.pages = [], None, []

    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "url": url, "body": body, "auth": headers.get("Authorization")})
        if method == "POST" and url.endswith("/message_templates"):
            if self.reject:
                return 400, json.dumps({"error": {"message": "Invalid parameter", "code": 100,
                                                  "error_user_msg": self.reject}}).encode()
            return 200, json.dumps({"id": "tmpl-%d" % len(self.calls), "status": "PENDING",
                                    "category": body["category"]}).encode()
        if method == "DELETE" and "/message_templates" in url:
            return 200, json.dumps({"success": True}).encode()
        if method == "GET" and "/message_templates" in url:
            page = self.pages.pop(0) if self.pages else {"data": []}
            return 200, json.dumps(page).encode()
        return 404, b"{}"


@pytest.fixture
def vault():
    return Vault({1: os.urandom(32)})


@pytest.fixture
def world(migrated, vault):
    tenant, number = H.onboard(migrated)
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    waba_row = migrated.one("select id from wabas")

    class W:
        db, tid, nid, waba, meta, vault_ = migrated, tenant, number, waba_row, Meta(), vault

        @classmethod
        def conn(cls):
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def create(cls, **kw):
            args = dict(tenant_id=tenant, waba_row_id=waba_row, name="order_ready", language="en_US",
                        category="UTILITY", components=GOOD)
            args.update(kw)
            conn = cls.conn()
            try:
                return templates.create_template(conn, graph=Graph(cls.meta, version="v25.0"), vault=vault, **args)
            finally:
                conn.close()

        @classmethod
        def sync(cls):
            conn = cls.conn()
            try:
                return templates.sync_templates(conn, graph=Graph(cls.meta, version="v25.0"), vault=vault,
                                                tenant_id=tenant, waba_row_id=waba_row)
            finally:
                conn.close()

    return W


# ── create ───────────────────────────────────────────────────────────────────

def test_creating_a_template_records_it_and_submits_it_to_meta(world):
    out = world.create()
    assert out["status"] == "PENDING" and out["meta_id"].startswith("tmpl-")
    row = H.only(world.db, "select * from templates")
    assert (row["name"], row["language"], row["category"], row["status"], row["tenant_id"]) == \
        ("order_ready", "en_US", "UTILITY", "PENDING", world.tid)
    assert row["meta_id"] == out["meta_id"] and row["components"] == GOOD and row["last_error"] is None
    call = world.meta.calls[0]
    assert call["url"].endswith("/%s/message_templates" % P.WABA) and call["auth"] == "Bearer " + TOKEN
    assert call["body"] == {"name": "order_ready", "language": "en_US", "category": "UTILITY", "components": GOOD}


def test_a_template_that_fails_lint_is_not_recorded_or_submitted(world):
    with pytest.raises(templates.TemplateInvalid) as e:
        world.create(name="Order Ready!")
    assert "bad_name" in {i.code for i in e.value.issues}
    assert world.db.one("select count(*) from templates") == 0 and not world.meta.calls


def test_a_template_meta_refuses_is_kept_as_a_draft_with_metas_reason_and_can_be_resubmitted(world):
    world.meta.reject = "The content of this template looks promotional"
    with pytest.raises(templates.TemplateRejected) as e:
        world.create()
    assert "promotional" in str(e.value)
    row = H.only(world.db, "select status, last_error from templates")
    assert row["status"] == "DRAFT" and "promotional" in row["last_error"]["details"]

    world.meta.reject = None
    world.create(category="MARKETING")
    row = H.only(world.db, "select status, category, last_error from templates")      # same row, not a second one
    assert (row["status"], row["category"], row["last_error"]) == ("PENDING", "MARKETING", None)


def test_a_name_already_in_use_by_a_live_template_is_refused(world):
    world.create()
    world.db.q("update templates set status = 'APPROVED'")
    with pytest.raises(templates.TemplateInvalid) as e:
        world.create()
    assert "exists" in {i.code for i in e.value.issues}
    world.create(language="hi")                                                       # another language is fine
    assert world.db.one("select count(*) from templates") == 2


def test_no_more_than_a_hundred_templates_an_hour_per_account(world):
    for i in range(templates.CREATED_PER_HOUR):
        world.db.q("insert into templates (tenant_id, waba_id, name, language, category) values (%s, %s, %s, 'en_US', 'UTILITY')",
                   (world.tid, world.waba, "t%d" % i))
    with pytest.raises(templates.TemplateLimit):
        world.create()
    assert not world.meta.calls


def test_another_workspaces_account_cannot_be_used(world):
    other = H.tenant(world.db, "shakti")
    conn = world.conn()
    try:
        with pytest.raises(LookupError):
            templates.create_template(conn, graph=Graph(world.meta), vault=world.vault_, tenant_id=other,
                                      waba_row_id=world.waba, name="x", language="en_US", category="UTILITY",
                                      components=GOOD)
    finally:
        conn.close()


# ── sync ─────────────────────────────────────────────────────────────────────

def item(name, status="APPROVED", **kw):
    return {"id": "m-" + name, "name": name, "language": "en_US", "category": "MARKETING", "status": status,
            "components": GOOD, "quality_score": {"score": "GREEN"}, "rejected_reason": "NONE", **kw}


def test_sync_pulls_every_page_and_is_idempotent(world):
    world.meta.pages = [{"data": [item("one"), item("two", "REJECTED", rejected_reason="INVALID_FORMAT")],
                         "paging": {"cursors": {"after": "CUR1"}, "next": "https://graph/next"}},
                        {"data": [item("three", "PAUSED")], "paging": {"cursors": {"after": "CUR2"}}}]
    assert world.sync() == 3
    assert "after=CUR1" in world.meta.calls[1]["url"]
    rows = {r["name"]: r for r in world.db.q("select * from templates")}
    assert {k: v["status"] for k, v in rows.items()} == {"one": "APPROVED", "two": "REJECTED", "three": "PAUSED"}
    assert rows["one"]["quality"] == "GREEN" and rows["one"]["reject_reason"] is None
    assert rows["two"]["reject_reason"] == "INVALID_FORMAT" and rows["one"]["meta_id"] == "m-one"

    world.meta.pages = [{"data": [item("one", "PAUSED")]}]
    world.sync()
    assert world.db.one("select count(*) from templates") == 3
    assert world.db.one("select status from templates where name = 'one'") == "PAUSED"


def test_sync_ignores_items_it_cannot_use(world):
    world.meta.pages = [{"data": [{"id": "x"}, "junk", item("fine")]}]
    assert world.sync() == 1


# ── webhooks ─────────────────────────────────────────────────────────────────

def test_a_status_webhook_updates_our_copy_by_meta_id_or_by_name(world):
    world.create()
    meta_id = world.db.one("select meta_id from templates")
    H.deliver(world.db, P.template_status("APPROVED", "order_ready", meta_id.replace("tmpl-", "") and 7))
    H.work(world.db)
    assert world.db.one("select status from templates") == "APPROVED"          # matched by name + language

    H.deliver(world.db, P.template_status("REJECTED", "order_ready", 7, reason="INVALID_FORMAT"))
    H.work(world.db)
    row = H.only(world.db, "select status, reject_reason from templates")
    assert (row["status"], row["reject_reason"]) == ("REJECTED", "INVALID_FORMAT")

    H.deliver(world.db, P.template_status("REINSTATED", "order_ready", 7, reason="NONE"))
    H.work(world.db)
    row = H.only(world.db, "select status, reject_reason from templates")
    assert (row["status"], row["reject_reason"]) == ("APPROVED", None)


def test_a_status_for_a_template_made_elsewhere_creates_a_stub_so_it_can_be_checked(world):
    H.deliver(world.db, P.template_status("APPROVED", "made_in_whatsapp_manager", 99))
    H.work(world.db)
    row = H.only(world.db, "select name, status, meta_id from templates")
    assert (row["name"], row["status"], row["meta_id"]) == ("made_in_whatsapp_manager", "APPROVED", "99")


def test_quality_and_category_changes_are_applied(world):
    world.create()
    world.db.q("update templates set meta_id = '555'")
    H.deliver(world.db, P.envelope({"message_template_id": 555, "message_template_name": "order_ready",
                                    "message_template_language": "en_US", "new_quality_score": "RED"},
                                   field="message_template_quality_update"))
    H.deliver(world.db, P.envelope({"message_template_id": 555, "message_template_name": "order_ready",
                                    "message_template_language": "en_US", "new_category": "MARKETING"},
                                   field="template_category_update"))
    H.work(world.db)
    row = H.only(world.db, "select quality, category from templates")
    assert row == {"quality": "RED", "category": "MARKETING"}


# ── the send engine respects them ────────────────────────────────────────────

def queue_template(world, name="order_ready", key="k"):
    cid = world.db.one("insert into contacts (tenant_id, name, phone) values (%s, 'A', %s) returning id"
                       " ", (world.tid, "9198%08d" % abs(hash(key) % 10**8)))
    conn = world.conn()
    try:
        with conn.cursor() as cur:
            send.enqueue_template(cur, world.tid, contact_id=cid, name=name, language="en_US", idem_key=key)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED", "DRAFT"])
def test_a_template_that_is_not_approved_is_never_queued(world, status):
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status)"
               " values (%s, %s, 'order_ready', 'en_US', 'UTILITY', %s)", (world.tid, world.waba, status))
    with pytest.raises(send.SendRefused) as e:
        queue_template(world)
    assert e.value.reason == "template_not_approved" and status.lower() in str(e.value)
    assert world.db.one("select count(*) from outbox") == 0


def test_an_approved_template_and_one_we_have_never_seen_are_both_queued(world):
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status)"
               " values (%s, %s, 'order_ready', 'en_US', 'UTILITY', 'APPROVED')", (world.tid, world.waba))
    queue_template(world, key="a")
    queue_template(world, name="unknown_to_us", key="b")
    assert world.db.one("select count(*) from outbox") == 2


# ── deleting ─────────────────────────────────────────────────────────────────

def _delete(world, template_id):
    conn = world.conn()
    try:
        return templates.delete_template(conn, graph=Graph(world.meta, version="v25.0"), vault=world.vault_,
                                         tenant_id=world.tid, template_id=template_id)
    finally:
        conn.close()


def test_deleting_removes_that_one_language_at_meta_and_here(world):
    made = world.create()
    world.meta.calls.clear()
    assert _delete(world, made["id"]) == {"deleted": True}
    call = [c for c in world.meta.calls if c["method"] == "DELETE"][0]
    assert "hsm_id=%s" % made["meta_id"] in call["url"] and "name=order_ready" in call["url"]
    assert world.db.one("select count(*) from templates") == 0


def test_a_draft_that_never_reached_meta_is_only_removed_here(world):
    world.meta.reject = "Nope"
    with pytest.raises(templates.TemplateRejected):
        world.create()
    template_id = world.db.one("select id from templates")
    world.meta.calls.clear()
    _delete(world, template_id)
    assert not [c for c in world.meta.calls if c["method"] == "DELETE"]
    assert world.db.one("select count(*) from templates") == 0


def test_a_template_a_live_campaign_uses_cannot_be_deleted(world):
    made = world.create()
    world.db.q("insert into campaigns (tenant_id, name, template_name, language, template_id, audience, status)"
               " values (%s, 'Diwali', 'order_ready', 'en_US', %s, '{\"tags\": []}', 'scheduled')", (world.tid, made["id"]))
    with pytest.raises(templates.TemplateInvalid) as exc:
        _delete(world, made["id"])
    assert "in_use" in [i.code for i in exc.value.issues]
    assert world.db.one("select count(*) from templates") == 1


def test_deleting_someone_elses_template_is_refused(world):
    with pytest.raises(LookupError):
        _delete(world, "00000000-0000-0000-0000-000000000001")


def test_meta_refusing_the_delete_keeps_the_template(world):
    made = world.create()
    world.meta.calls.clear()
    real = world.meta.__call__

    def refuse(method, url, data, headers):
        if method == "DELETE":
            return 400, json.dumps({"error": {"message": "Template is in use", "code": 100}}).encode()
        return real(method, url, data, headers)
    world.meta = type("M", (), {"calls": [], "reject": None, "pages": [], "__call__": staticmethod(refuse)})()
    with pytest.raises(templates.TemplateRejected):
        _delete(world, made["id"])
    assert world.db.one("select count(*) from templates") == 1
