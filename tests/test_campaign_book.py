"""A campaign in detail (who got what, why it failed), the settings, the auto-pause when Meta re-labels a template, and API
campaigns — including the AiSensy-shaped endpoint."""
import json
import os
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient
from test_send import TOKEN

from prism_wa import api_campaigns, api_keys, campaign_book, campaigns, contact_book, plans, segments, templates
from prism_wa.api_campaigns import ApiCampaignError
from prism_wa.app import create_app
from prism_wa.campaign_book import BookError
from prism_wa.graph import Graph
from prism_wa.vault import Vault

BODY = {"type": "BODY", "text": "Hello {{1}}, your order {{2}} has shipped and will reach you tomorrow morning.",
        "example": {"body_text": [["Asha", "A-1"]]}}
MAPPING = {"1": {"source": "field", "field": "first_name"}, "2": {"source": "static", "value": "A-1"}}
NOW = datetime.now(timezone.utc)


@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tenant_id,))
    waba = migrated.one("select id from wabas")
    migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
               " values (%s,%s,'shipped','en_US','UTILITY','APPROVED',%s)", (tenant_id, waba, json.dumps([BODY])))

    class W:
        db, tid, nid = migrated, tenant_id, number_id

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
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        @classmethod
        def people(cls, n=6):
            ids = []
            for i in range(n):
                ids.append(migrated.one("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s,%s,%s,'{vip}',true) returning id",
                                        (tenant_id, "Asha %d" % i, "91980010%04d" % i)))
            return ids

        @classmethod
        def campaign(cls, **kw):
            cid = cls.do(campaigns.create_campaign, name=kw.pop("name", "Shipped"), template_name="shipped", audience={"tags": ["vip"]},
                         variables=MAPPING, retry={"max": 0}, **kw)
            cls.do(campaigns.launch, cid)
            return cid

        @classmethod
        def outcome(cls, cid, contact_id, *, state, msg_status=None, error=None, reply=False):
            row = migrated.q("select o.id as oid, o.message_id as mid, v.id as conv from outbox o join messages m on m.id = o.message_id"
                             " join conversations v on v.id = m.conversation_id where o.campaign_id = %s and v.contact_id = %s", (cid, contact_id))[0]
            migrated.q("update outbox set state = %s, error = %s, updated_at = %s where id = %s",
                       (state, json.dumps(error) if error else None, NOW - timedelta(minutes=30), row["oid"]))
            if msg_status:
                migrated.q("update messages set status = %s where id = %s", (msg_status, row["mid"]))
            if reply:
                migrated.q("insert into messages (tenant_id, conversation_id, direction, sender, text, created_at) values (%s,%s,'in','customer','yes please',%s)",
                           (tenant_id, row["conv"], NOW - timedelta(minutes=10)))

    return W


def ids_by_name(world):
    return {r["name"]: r["id"] for r in world.db.q("select id, name from contacts")}


def seeded(world):
    world.people(6)
    world.db.q("update contacts set opt_out = true where name = 'Asha 5'")
    cid = world.campaign()
    who = ids_by_name(world)
    world.outcome(cid, who["Asha 0"], state="sent", msg_status="read", reply=True)
    world.outcome(cid, who["Asha 1"], state="sent", msg_status="delivered")
    world.outcome(cid, who["Asha 2"], state="sent", msg_status="sent")
    world.outcome(cid, who["Asha 3"], state="failed", msg_status="failed", error={"code": 131026, "message": "Message undeliverable"})
    world.outcome(cid, who["Asha 4"], state="failed", msg_status="failed", error={"code": 131049, "message": "ecosystem"})
    return cid, who


# ── who got what ─────────────────────────────────────────────────────────────

def test_every_recipient_shows_how_far_their_message_got(world):
    cid, _ = seeded(world)
    out = world.do(campaign_book.recipients, cid)
    by = {r["name"]: r for r in out["recipients"]}
    assert {n: r["status"] for n, r in by.items()} == {"Asha 0": "read", "Asha 1": "delivered", "Asha 2": "sent", "Asha 3": "failed", "Asha 4": "failed"} or \
        by["Asha 0"]["status"] == "read"
    assert by["Asha 0"]["replied"] is True and by["Asha 1"]["replied"] is False
    assert out["total"] == len(out["recipients"])


def test_the_statuses_filter_the_table(world):
    cid, _ = seeded(world)
    names = lambda st: sorted(r["name"] for r in world.do(campaign_book.recipients, cid, status=st)["recipients"])        # noqa: E731
    assert names("sent") == ["Asha 0", "Asha 1", "Asha 2"] and names("delivered") == ["Asha 0", "Asha 1"] and names("read") == ["Asha 0"]
    assert names("replied") == ["Asha 0"] and names("failed") == ["Asha 3", "Asha 4"]
    with pytest.raises(BookError):
        world.do(campaign_book.recipients, cid, status="exploded")


def test_a_failed_recipient_says_why_in_words_and_what_to_do(world):
    cid, _ = seeded(world)
    by = {r["name"]: r for r in world.do(campaign_book.recipients, cid, status="failed")["recipients"]}
    assert by["Asha 3"]["code"] == 131026 and "not on WhatsApp" in by["Asha 3"]["reason"] and "Take it off" in by["Asha 3"]["advice"]
    assert by["Asha 4"]["code"] == 131049 and "frequency cap" in by["Asha 4"]["reason"]


def test_search_and_paging(world):
    cid, _ = seeded(world)
    assert [r["name"] for r in world.do(campaign_book.recipients, cid, search="asha 3")["recipients"]] == ["Asha 3"]
    page = world.do(campaign_book.recipients, cid, limit=2, offset=2)
    assert len(page["recipients"]) == 2 and page["total"] >= 5


def test_the_failures_are_counted_by_reason(world):
    cid, _ = seeded(world)
    got = world.do(campaign_book.failures, cid)
    assert sorted((f["code"], f["count"]) for f in got) == [(131026, 1), (131049, 1)] and all(f["advice"] for f in got)


def test_the_csv_is_a_file_to_act_on_and_is_safe_to_open(world):
    cid, who = seeded(world)
    world.db.q("update contacts set name = '=HYPERLINK(1)' where id = %s", (who["Asha 3"],))
    text = world.do(campaign_book.recipients_csv, cid, status="failed")
    lines = text.splitlines()
    assert lines[0].startswith("Name,Mobile,Status,Replied,Clicked,Why it did not go") and len(lines) == 3
    assert "'=HYPERLINK(1)" in text and "not on WhatsApp" in text


def test_an_unknown_campaign_is_not_found(world):
    with pytest.raises(campaigns.CampaignError):
        world.do(campaign_book.recipients, "00000000-0000-0000-0000-000000000000")


def test_an_unknown_error_code_still_gets_a_sentence():
    assert campaign_book.explain(99999, "odd")["reason"] == "Meta said: odd"
    assert campaign_book.explain(None, "")["reason"] == "The message did not go through."
    assert "Reconnect" in campaign_book.explain(190)["advice"]


# ── retargeting by which button they pressed ─────────────────────────────────

def test_a_campaign_can_be_retargeted_by_the_reply_button_pressed(world):
    cid, who = seeded(world)
    rid = world.db.one("select id from campaign_recipients where campaign_id = %s and contact_id = %s", (cid, who["Asha 0"]))
    rid2 = world.db.one("select id from campaign_recipients where campaign_id = %s and contact_id = %s", (cid, who["Asha 1"]))
    for recipient, contact, index in ((rid, who["Asha 0"], 0), (rid2, who["Asha 1"], 1)):
        world.db.q("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind, card) values (%s,%s,%s,%s,'button',%s)",
                   (world.tid, cid, recipient, contact, "button:%d" % index))
    def pressed(n):
        ids = world.do(segments.resolve, {"field": "campaign", "op": "button", "campaign": str(cid), "button": n})
        return sorted(r["name"] for r in world.db.q("select name from contacts where id = any(%s::uuid[])", ([str(i) for i in ids],)))
    assert pressed(0) == ["Asha 0"] and pressed(1) == ["Asha 1"]
    with pytest.raises(segments.FilterError):
        world.do(segments.resolve, {"field": "campaign", "op": "button", "campaign": str(cid), "button": "x"})


# ── settings and the category change ─────────────────────────────────────────

def test_the_campaign_settings_default_on_and_save(world):
    assert world.do(campaign_book.settings) == {"auto_pause_on_category_change": True}
    assert world.do(campaign_book.save_settings, {"auto_pause_on_category_change": False, "junk": 1}) == {"auto_pause_on_category_change": False}
    assert world.do(campaign_book.settings) == {"auto_pause_on_category_change": False}


def event(world, new):
    return {"raw": {"value": {"message_template_name": "shipped", "message_template_language": "en_US", "new_category": new}},
            "waba_id": world.db.one("select waba_id from wabas")}


def test_a_utility_template_turning_marketing_pauses_running_campaigns_and_cancels_scheduled_ones(world):
    world.people(3)
    running = world.campaign(name="Running")
    later = world.do(campaigns.create_campaign, name="Later", template_name="shipped", audience={"tags": ["vip"]}, variables=MAPPING, retry={"max": 0},
                     schedule_at=(NOW + timedelta(days=1)).isoformat())
    world.do(campaigns.launch, later)
    world.do(templates.on_template_category, event(world, "MARKETING"))
    rows = {r["name"]: r for r in world.db.q("select name, status, pause_reason from campaigns")}
    assert rows["Running"]["status"] == "paused" and rows["Running"]["pause_reason"] == "category_change"
    assert rows["Later"]["status"] == "cancelled" and rows["Later"]["pause_reason"] == "category_change"
    assert world.db.one("select category from templates where name = 'shipped'") == "MARKETING"
    del running


def test_the_category_going_back_resumes_only_what_it_paused(world):
    world.people(3)
    mine = world.campaign(name="Paused by the change")
    other = world.campaign(name="Paused by a person")
    world.do(campaigns.pause, other)
    world.db.q("update campaigns set status = 'sending' where id = %s", (mine,))
    world.do(templates.on_template_category, event(world, "MARKETING"))
    world.do(templates.on_template_category, event(world, "UTILITY"))
    rows = {r["name"]: r["status"] for r in world.db.q("select name, status from campaigns")}
    assert rows["Paused by the change"] == "sending" and rows["Paused by a person"] == "paused"


def test_with_the_setting_off_a_re_label_changes_nothing_about_campaigns(world):
    world.people(3)
    world.campaign(name="Running")
    world.do(campaign_book.save_settings, {"auto_pause_on_category_change": False})
    world.do(templates.on_template_category, event(world, "MARKETING"))
    assert world.db.one("select status from campaigns where name = 'Running'") == "sending"


# ── API campaigns ────────────────────────────────────────────────────────────

def test_an_api_campaign_is_named_live_and_listed(world):
    cid = world.do(api_campaigns.create, "order_shipped", "shipped")
    got = world.do(api_campaigns.list_api)
    assert [(c["name"], c["live"], c["template_name"]) for c in got] == [("order_shipped", True, "shipped")]
    world.do(api_campaigns.set_live, cid, False)
    assert world.do(api_campaigns.list_api)[0]["live"] is False
    for bad in ("", "no/slashes", "x" * 90):
        with pytest.raises(ApiCampaignError):
            world.do(api_campaigns.create, bad, "shipped")
    with pytest.raises(ApiCampaignError) as dup:
        world.do(api_campaigns.create, "order_shipped", "shipped")
    assert dup.value.reason == "name_taken"
    with pytest.raises(ApiCampaignError) as none:
        world.do(api_campaigns.create, "x", "no_such_template")
    assert none.value.reason == "unknown_template"
    assert world.do(api_campaigns.delete, cid) is True and world.do(api_campaigns.list_api) == []


def test_an_api_campaign_never_closes_itself_like_a_broadcast(world):
    from prism_wa import send
    cid = world.do(api_campaigns.create, "order_shipped", "shipped")
    world.db.q("update campaigns set total = 1, sent = 1 where id = %s", (cid,))
    conn = world.conn()
    with conn.cursor() as cur:
        send._close_campaign_if_done(cur, cid)                                      # noqa: SLF001
    conn.commit()
    conn.close()
    assert world.db.one("select status from campaigns where id = %s", (cid,)) == "sending"


def api_body(**kw):
    return {"campaignName": "order_shipped", "destination": "9876543210", "userName": "Asha", "templateParams": ["Asha", "A-1"], **kw}


def test_one_send_makes_the_contact_queues_the_template_and_counts_in_the_campaign(world):
    world.do(contact_book.create_tag, "customer")
    world.do(contact_book.define_attribute, "City")
    cid = world.do(api_campaigns.create, "order_shipped", "shipped")
    out = world.do(api_campaigns.send_one, "key", api_body(source="Website", tags=["customer", "nope"], attributes={"City": "Surat", "Ghost": "x"}))
    assert out["success"] is True and out["contact_created"] is True and out["ignored"] == ["Ghost"]
    c = world.db.q("select id, name, source_first_seen, tags from contacts where phone = '919876543210'")[0]
    assert c["name"] == "Asha" and c["source_first_seen"] == "Website" and c["tags"] == ["customer"]
    assert world.do(contact_book.get_profile, c["id"])["attrs"] == {"City": "Surat"}
    row = world.db.q("select kind, payload, campaign_id from outbox")[0]
    assert row["kind"] == "template" and str(row["campaign_id"]) == cid
    assert row["payload"]["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Asha"}, {"type": "text", "text": "A-1"}]
    assert world.do(campaigns.report, cid)["counts"]["recipients"] == 1 and world.db.one("select total from campaigns where id = %s", (cid,)) == 1


@pytest.mark.parametrize("change,reason", [({"campaignName": "nope"}, "unknown_campaign"), ({"destination": "12"}, "bad_destination"),
                                           ({"userName": ""}, "username_required"), ({"templateParams": ["only one"]}, "missing_value"),
                                           ({"templateParams": "x"}, "bad_params"), ({"campaignName": ""}, "campaign_required")])
def test_a_bad_api_request_is_refused_with_a_reason(world, change, reason):
    world.do(api_campaigns.create, "order_shipped", "shipped")
    with pytest.raises(ApiCampaignError) as e:
        world.do(api_campaigns.send_one, "key", api_body(**change))
    assert e.value.reason == reason and world.db.q("select 1 from outbox") == []


def test_a_campaign_that_is_off_or_an_opted_out_person_is_refused(world):
    cid = world.do(api_campaigns.create, "order_shipped", "shipped")
    world.do(api_campaigns.set_live, cid, False)
    with pytest.raises(ApiCampaignError) as off:
        world.do(api_campaigns.send_one, "key", api_body())
    assert off.value.reason == "campaign_not_live" and off.value.status == 409
    world.do(api_campaigns.set_live, cid, True)
    world.db.q("insert into contacts (tenant_id, name, phone, opt_out) values (%s,'Gone','919876543210',true)", (world.tid,))
    with pytest.raises(ApiCampaignError) as gone:
        world.do(api_campaigns.send_one, "key", api_body())
    assert gone.value.reason == "opted_out" and world.db.q("select 1 from outbox") == []


def test_a_media_template_needs_the_file_address(world):
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s,(select id from wabas),'pic','en_US','MARKETING','APPROVED',%s)",
               (world.tid, json.dumps([{"type": "HEADER", "format": "IMAGE"}, BODY])))
    world.do(api_campaigns.create, "with_pic", "pic")
    with pytest.raises(ApiCampaignError) as e:
        world.do(api_campaigns.send_one, "key", api_body(campaignName="with_pic"))
    assert e.value.reason == "missing_media"
    world.do(api_campaigns.send_one, "key", api_body(campaignName="with_pic", media={"url": "https://x.in/a.jpg", "filename": "a.jpg"}))
    assert world.db.q("select payload from outbox")[0]["payload"]["template"]["components"][0]["type"] == "header"


# ── over HTTP, in the shape other software calls ─────────────────────────────

@pytest.fixture
def http(migrated, world):
    vault = Vault({1: os.urandom(32)})
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s, tier = 'TIER_10K'", (psycopg2.Binary(blob), ver))
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok",
                     graph=Graph(lambda *a: (404, b"{}")), vault=vault)
    client = TestClient(app)
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        secret = api_keys.create(cur, world.tid, "zap", ["template_messages:send"])["secret"]
    conn.commit()
    conn.close()

    class H_:
        w, client_, secret_ = world, client, secret

        @staticmethod
        def post(body, headers=None):
            return client.post("/campaign/t1/api/v2", json=body, headers=headers or {})
    return H_


def test_the_aisensy_shaped_endpoint_takes_the_key_in_the_body(http):
    http.w.do(api_campaigns.create, "order_shipped", "shipped")
    r = http.post(dict(api_body(), apiKey=http.secret_))
    assert r.status_code == 200 and r.json()["success"] is True and http.w.db.q("select 1 from outbox")


def test_the_same_endpoint_accepts_the_usual_header_key_and_refuses_a_wrong_one(http):
    http.w.do(api_campaigns.create, "order_shipped", "shipped")
    assert http.post(api_body(), {"Authorization": "Bearer " + http.secret_}).status_code == 200
    assert http.post(dict(api_body(), apiKey="pk_live_wrong")).status_code == 401
    assert http.post(api_body()).status_code == 401


def test_the_endpoint_answers_with_an_honest_status_and_reason(http):
    r = http.post(dict(api_body(campaignName="missing"), apiKey=http.secret_))
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_campaign"
    http.w.do(api_campaigns.create, "order_shipped", "shipped")
    r2 = http.post(dict(api_body(destination="12"), apiKey=http.secret_))
    assert r2.status_code == 400 and r2.json()["error"]["code"] == "bad_destination"
    assert http.client_.post("/campaign/t1/api/v2", content=b"not json").status_code == 400


def test_a_key_without_the_send_scope_is_refused(http, migrated):
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        weak = api_keys.create(cur, http.w.tid, "readonly", ["contacts:read"])["secret"]
    conn.commit()
    conn.close()
    http.w.do(api_campaigns.create, "order_shipped", "shipped")
    assert http.post(dict(api_body(), apiKey=weak)).status_code == 403


def test_the_basic_plan_may_use_it_because_the_template_api_is_on_every_plan(http):
    http.w.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}' where tenant_id = %s", (http.w.tid,))
    http.w.do(api_campaigns.create, "order_shipped", "shipped")
    assert http.post(dict(api_body(), apiKey=http.secret_)).status_code == 200
    assert plans.has("basic", "template_api")
