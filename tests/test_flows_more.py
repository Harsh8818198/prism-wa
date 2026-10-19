"""Flows beyond the first six steps: sending a file and a map pin, asking for a location, sections in a list, conditions on the day, the date,
the time and holidays, a webhook step, telling Meta about a lead, how a flow is doing, writing one with AI, and the ready-made library.
Pretend walks need no database; the live ones run on a real Postgres."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import ai_agent, capi, flow_ai, flow_engine, flow_library, flows, inbox, media, webhooks_out
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.llm import LLM, LLMError
from prism_wa.media_store import LocalStore
from prism_wa.vault import Vault

IST = timezone(timedelta(hours=5, minutes=30))
FILE = "11111111-1111-1111-1111-111111111111"
INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}


def walk(graph, inputs=("hi",), **kw):
    return flow_engine.test(graph, None, list(inputs), **kw)


def said(result):
    return [t for t in result["transcript"] if t["from"] != "customer"]


def codes(issues, severity="error"):
    return {i.code for i in issues if i.severity == severity}


def flow(entry, **nodes):
    return {"entry": entry, "nodes": nodes}


# ── sending a file and a map pin ─────────────────────────────────────────────

def test_a_file_step_sends_the_file_with_its_words():
    g = flow("a", a={"type": "media", "kind": "document", "asset_id": FILE, "caption": "Hello {{first_name}}, here is the rate card.", "next": "b"}, b={"type": "end"})
    out = said(walk(g, name="Asha Patel"))
    assert out[0] == {"from": "bot", "kind": "media", "media": "document", "text": "Hello Asha, here is the rate card.", "asset_id": FILE}


@pytest.mark.parametrize("node,problem", [
    ({"type": "media", "kind": "gif", "asset_id": FILE}, "bad_media"), ({"type": "media", "kind": "image", "asset_id": "nope"}, "bad_media"),
    ({"type": "media", "kind": "image", "asset_id": FILE, "caption": "x" * 1025}, "bad_text"),
    ({"type": "send_location", "latitude": 91, "longitude": 0}, "bad_location"), ({"type": "send_location", "latitude": 1, "longitude": 181}, "bad_location"),
    ({"type": "send_location", "latitude": "1", "longitude": 1}, "bad_location"), ({"type": "send_location", "latitude": 1, "longitude": 1, "name": "x" * 101}, "bad_location"),
    ({"type": "webhook", "url": "http://x.in"}, "bad_request"), ({"type": "webhook", "url": "https://x.in", "method": "PUT"}, "bad_request"),
    ({"type": "capi_event", "event": "Nonsense"}, "bad_event"), ({"type": "capi_event", "event": "Lead", "currency": "rupees"}, "bad_event"),
    ({"type": "location_request", "text": ""}, "bad_text")])
def test_each_new_step_checks_its_own_fields(node, problem):
    assert problem in codes(flows.validate(flow("a", a=node)))


def test_a_map_pin_is_sent_with_its_name_and_address():
    g = flow("a", a={"type": "send_location", "latitude": 22.3072, "longitude": 73.1812, "name": "Patel Constructions", "address": "Alkapuri", "next": "b"}, b={"type": "end"})
    assert said(walk(g))[0] == {"from": "bot", "kind": "location", "latitude": 22.3072, "longitude": 73.1812, "name": "Patel Constructions", "address": "Alkapuri"}


# ── asking for a location ────────────────────────────────────────────────────

LOC = flow("ask", ask={"type": "location_request", "text": "Please share your location.", "next": "thanks", "timeout_next": "typed", "timeout_minutes": 30},
           thanks={"type": "send_text", "text": "Got it: {{var.location_name}} at {{var.location_lat}}, {{var.location_lng}}.", "next": "end"},
           typed={"type": "send_text", "text": "No pin, no problem.", "next": "end"}, end={"type": "end"})


def test_a_location_request_waits_for_a_pin_and_keeps_where_it_is():
    out = walk(LOC, ["hi", {"text": "", "location": {"latitude": 22.31, "longitude": 73.18, "name": "Alkapuri", "address": "Vadodara"}}])
    assert [t["kind"] for t in said(out)] == ["location_request", "text"] and said(out)[1]["text"] == "Got it: Alkapuri at 22.31, 73.18."
    assert out["state"] == "done"


def test_something_that_is_not_a_pin_is_asked_for_once_more_then_times_out():
    out = walk(LOC, ["hi", "I am near the station", "still near the station"])
    assert [t["kind"] for t in said(out)] == ["location_request", "location_request", "text"] and said(out)[-1]["text"] == "No pin, no problem."


def test_a_shared_pin_reaches_the_flow_as_a_location_not_as_text():
    from prism_wa import events
    ev = events.normalise(P.inbound({**P.text("x", "wamid.L2", "1760000008"), "type": "location", "location": {"latitude": 22.3, "longitude": 73.1, "name": "Shop", "address": "Alkapuri"}}))[0]
    c = ev.canonical()
    assert c["msg_type"] == "location" and c["location"] == {"latitude": 22.3, "longitude": 73.1, "name": "Shop", "address": "Alkapuri"} and c["text"] == "Shop Alkapuri"
    plain = events.normalise(P.inbound(P.text("hello", "wamid.T7", "1760000001")))[0].canonical()
    assert plain["location"] == {}


# ── a list with sections ─────────────────────────────────────────────────────

SECTIONED = flow("menu", menu={"type": "list", "text": "What do you need?", "button_label": "Choose", "sections": [
    {"title": "Walls", "rows": [{"id": "boundary", "title": "Boundary wall", "next": "wall"}, {"id": "retaining", "title": "Retaining wall", "next": "wall"}]},
    {"title": "Sheds", "rows": [{"id": "shed", "title": "Industrial shed", "next": "shed"}]}]},
    wall={"type": "send_text", "text": "Walls: noted.", "next": "end"}, shed={"type": "send_text", "text": "Sheds: noted.", "next": "end"}, end={"type": "end"})


def test_a_list_can_have_sections_and_a_row_in_any_of_them_can_be_chosen():
    assert flows.errors(flows.validate(SECTIONED)) == []
    out = walk(SECTIONED, ["hi", {"text": "", "payload_id": "shed"}])
    first = said(out)[0]
    assert first["kind"] == "list" and first["sections"] == [{"title": "Walls", "rows": ["boundary", "retaining"]}, {"title": "Sheds", "rows": ["shed"]}]
    assert said(out)[1]["text"] == "Sheds: noted."


def test_sections_obey_the_limits_and_their_targets_must_exist():
    def with_sections(sections):
        return flow("m", m={"type": "list", "text": "x", "button_label": "Pick", "sections": sections})
    assert "bad_options" in codes(flows.validate(with_sections([{"title": "A", "rows": []}])))
    assert "bad_options" in codes(flows.validate(with_sections([{"title": "x" * 25, "rows": [{"id": "1", "title": "t"}]}])))
    assert "bad_options" in codes(flows.validate(with_sections([{"title": "A", "rows": [{"id": str(i), "title": "t"} for i in range(6)]},
                                                                {"title": "B", "rows": [{"id": str(i + 9), "title": "t"} for i in range(6)]}])))
    assert "dangling" in codes(flows.validate(with_sections([{"title": "A", "rows": [{"id": "1", "title": "t", "next": "gone"}]}])))


# ── conditions on the day, the date, the time and holidays ───────────────────

def clock(**when):
    return datetime(2026, 11, when.get("day", 12), when.get("hour", 10), when.get("minute", 0), tzinfo=IST)


def cond(field, op, value="", yes="yes", no="no"):
    return flow("c", c={"type": "condition", "rules": [{"field": field, "op": op, "value": value, "next": "yes"}], "else": "no"},
                yes={"type": "send_text", "text": yes}, no={"type": "send_text", "text": no})


@pytest.mark.parametrize("field,op,value,day,hour,holidays,expected", [
    ("weekday", "in", "sat,sun", 14, 10, (), True), ("weekday", "in", "sat,sun", 12, 10, (), False), ("weekday", "eq", "thu", 12, 10, (), True),
    ("weekday", "ne", "thu", 12, 10, (), False),
    ("date", "eq", "2026-11-12", 12, 10, (), True), ("date", "gt", "2026-11-12", 13, 10, (), True), ("date", "lt", "2026-11-12", 11, 10, (), True),
    ("date", "between", "2026-11-10..2026-11-15", 12, 10, (), True), ("date", "between", "2026-11-10..2026-11-15", 16, 10, (), False),
    ("date", "eq", "today", 12, 10, (), True),
    ("time", "between", "09:00..18:00", 12, 10, (), True), ("time", "between", "09:00..18:00", 12, 18, (), False), ("time", "gt", "17:00", 12, 18, (), True),
    ("time", "lt", "09:00", 12, 8, (), True),
    ("holiday", "yes", "", 12, 10, ("2026-11-12",), True), ("holiday", "yes", "", 12, 10, (), False), ("holiday", "no", "", 12, 10, (), True)])
def test_a_condition_on_today_reads_the_workspaces_clock(field, op, value, day, hour, holidays, expected):
    out = walk(cond(field, op, value), local=clock(day=day, hour=hour), holidays=holidays)
    assert said(out)[0]["text"] == ("yes" if expected else "no")


@pytest.mark.parametrize("rule", [
    {"field": "weekday", "op": "in", "value": "someday"}, {"field": "weekday", "op": "gt", "value": "mon"}, {"field": "date", "op": "eq", "value": "12/11/2026"},
    {"field": "date", "op": "between", "value": "2026-11-10"}, {"field": "time", "op": "eq", "value": "09:00"}, {"field": "time", "op": "gt", "value": "9am"},
    {"field": "time", "op": "between", "value": "09:00..25:00"}, {"field": "holiday", "op": "eq", "value": "x"}])
def test_a_calendar_condition_that_could_never_work_is_refused_in_words(rule):
    issues = flows.validate(flow("c", c={"type": "condition", "rules": [{**rule, "next": "c"}], "else": "c"}))
    assert "bad_rules" in codes(issues)


def test_the_test_console_takes_a_pretend_time_and_holidays_over_the_route():
    client_walk = flow_engine.test(cond("holiday", "yes"), None, ["hi"], local=clock(), holidays=["2026-11-12"])
    assert said(client_walk)[0]["text"] == "yes"


# ── a webhook step and telling Meta ──────────────────────────────────────────

def test_a_webhook_step_posts_and_never_holds_the_flow_up():
    calls = []

    def http(method, url, headers, body):
        calls.append((method, url, headers, body))
        return 500, "down"
    g = flow("a", a={"type": "webhook", "url": "https://hook.example.com/x", "body": "{\"name\": \"{{first_name}}\"}", "headers": {"X-Key": "k"}, "next": "b"},
             b={"type": "send_text", "text": "Thanks."})
    out = flow_engine.test(g, None, ["hi"], name="Asha Patel", http=http)
    assert calls == [("POST", "https://hook.example.com/x", {"X-Key": "k"}, "{\"name\": \"Asha\"}")] and said(out)[-1]["text"] == "Thanks."


def test_the_tell_meta_step_is_only_a_note_in_a_test():
    g = flow("a", a={"type": "capi_event", "event": "Lead", "next": "b"}, b={"type": "end"})
    out = walk(g)
    assert "would tell Meta about a Lead" in said(out)[0]["text"]


# ── live ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def world(migrated, tmp_path):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tenant_id,))
    vault = Vault({1: os.urandom(32)})
    blob, ver = vault.encrypt("EAAB-flow-token", context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    media.configure(graph=None, vault=vault, store=LocalStore(str(tmp_path / "m")))

    class W:
        db, tid, nid, vault_ = migrated, tenant_id, number_id, vault

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
        def flow(cls, graph, trigger=None, name="Flow"):
            trigger = trigger or {"type": "keyword", "match": "contains", "keywords": ["hi", "hello"]}
            fid = cls.do(flows.create, name, trigger=trigger, graph=graph)
            cls.do(flows.publish, fid)
            cls.do(flows.set_enabled, fid, True)
            return fid

        @classmethod
        def say(cls, text, wamid):
            H.deliver(migrated, P.inbound(P.text(text, wamid, ts=str(int(time.time())))))
            H.work(migrated)

        @classmethod
        def pin(cls, wamid, **where):
            msg = {**P.text("x", wamid, ts=str(int(time.time()))), "type": "location", "location": {"latitude": 22.3, "longitude": 73.1, **where}}
            msg.pop("text", None)
            H.deliver(migrated, P.inbound(msg))
            H.work(migrated)

        @classmethod
        def asset(cls):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = media.upload(cur, tenant_id, filename="rates.pdf", mime="application/pdf", data=b"%PDF-1.4 " + b"1" * 400)
                conn.commit()
                return out
            finally:
                conn.close()

        @classmethod
        def sent(cls):
            return migrated.q("select kind, payload, state from outbox order by created_at, id")
    yield W
    media.configure(graph=None, vault=None, store=None)


def test_a_file_step_queues_the_library_file_and_a_pin_queues_a_map(world):
    a = world.asset()
    world.flow(flow("a", a={"type": "media", "kind": "document", "asset_id": str(a["id"]), "caption": "Rates for {{first_name}}", "next": "b"},
                    b={"type": "send_location", "latitude": 22.3072, "longitude": 73.1812, "name": "Office", "next": "c"}, c={"type": "end"}))
    world.say("hello", "wamid.A1")
    sent = {s["payload"]["type"]: s for s in world.sent()}                                     # (both are queued in one transaction: no order between them)
    assert set(sent) == {"document", "location"} and all(s["kind"] == "media" for s in sent.values())
    assert sent["document"]["payload"] == {"type": "document", "document": {"asset_id": str(a["id"]), "caption": "Rates for Asha", "filename": "rates.pdf"}}
    assert sent["location"]["payload"] == {"type": "location", "location": {"latitude": 22.3072, "longitude": 73.1812, "name": "Office"}}


def test_a_flow_that_sends_a_file_that_has_been_deleted_cannot_be_published(world):
    fid = world.do(flows.create, "F", trigger={"type": "keyword", "match": "contains", "keywords": ["hi"]},
                   graph=flow("a", a={"type": "media", "kind": "image", "asset_id": FILE, "next": "b"}, b={"type": "end"}))
    issues = world.do(flows.check, fid)
    assert "unknown_asset" in codes(issues)
    with pytest.raises(flows.FlowError) as e:
        world.do(flows.publish, fid)
    assert e.value.reason == "flow_invalid" and "Media Library" in str(e.value)
    a = world.asset()
    world.do(flows.update, fid, graph=flow("a", a={"type": "media", "kind": "document", "asset_id": str(a["id"]), "next": "b"}, b={"type": "end"}))
    assert world.do(flows.publish, fid)["version"] == 1


def test_a_customers_pin_is_kept_by_a_location_request(world):
    world.flow(flow("ask", ask={"type": "location_request", "text": "Share your location", "next": "thanks"},
                    thanks={"type": "send_text", "text": "Thanks, {{var.location_name}}.", "next": "end"}, end={"type": "end"}))
    world.say("hi", "wamid.A1")
    assert world.sent()[0]["payload"]["interactive"] == {"type": "location_request_message", "body": {"text": "Share your location"}, "action": {"name": "send_location"}}
    world.pin("wamid.A2", name="Alkapuri")
    assert world.sent()[-1]["payload"]["text"]["body"] == "Thanks, Alkapuri."
    assert world.db.one("select state from flow_runs") == "done" and world.db.one("select vars->>'location_lat' from flow_runs") == "22.3"


def test_a_sectioned_list_goes_out_as_sections(world):
    world.flow(SECTIONED)
    world.say("hi", "wamid.A1")
    sections = world.sent()[0]["payload"]["interactive"]["action"]["sections"]
    assert [s["title"] for s in sections] == ["Walls", "Sheds"] and [r["id"] for r in sections[0]["rows"]] == ["boundary", "retaining"]


def test_a_holiday_is_read_from_the_workspace_settings(world):
    today = datetime.now(IST).date().isoformat()
    world.do(inbox.save_settings, {"holidays": [{"date": today, "name": "Diwali"}, {"date": "2026-01-01"}]})
    assert world.do(inbox.settings)["holidays"] == [{"date": "2026-01-01"}, {"date": today, "name": "Diwali"}]
    world.flow(cond("holiday", "yes", yes="We are closed today.", no="We are open."))
    world.say("hello", "wamid.A1")
    assert world.sent()[0]["payload"]["text"]["body"] == "We are closed today."


@pytest.mark.parametrize("bad", ["2026-13-45", "tomorrow", [{"date": "x"}], "x" * 5, 5])
def test_a_holiday_list_that_is_not_dates_is_refused(world, bad):
    value = bad if isinstance(bad, list) else [bad]
    with pytest.raises(inbox.InboxError):
        world.do(inbox.save_settings, {"holidays": value})


def test_holidays_are_sorted_each_day_once_and_bounded(world):
    out = inbox.clean_holidays(["2026-11-12", {"date": "2026-11-12", "name": "dup"}, "2026-03-01"])
    assert [h["date"] for h in out] == ["2026-03-01", "2026-11-12"]
    with pytest.raises(inbox.InboxError):
        inbox.clean_holidays(["2026-01-%02d" % (i % 28 + 1) for i in range(61)])
    assert inbox.clean_holidays(None) == [] and inbox.clean_holidays("") == []


def test_a_finished_flow_is_told_to_the_workspaces_webhook_with_its_answers(world):
    ep = world.do(webhooks_out.create, url="https://hooks.example.com/x", events=["flow.response"]) if False else None
    world.db.q("insert into webhook_endpoints (tenant_id, url, secret, events) values (%s, 'https://hooks.example.com/x', 's', '{flow.response}')", (world.tid,))
    world.flow(flow("ask", ask={"type": "ask", "text": "Your city?", "save_as": "var:city", "next": "done"}, done={"type": "send_text", "text": "Thanks."}))
    world.say("hello", "wamid.A1")
    assert world.db.q("select 1 from webhook_deliveries") == []
    world.say("Surat", "wamid.A2")
    (d,) = world.db.q("select event, payload from webhook_deliveries")
    assert d["event"] == "flow.response" and d["payload"]["answers"] == {"city": "Surat"} and d["payload"]["handed_to_person"] is False and d["payload"]["run_id"]


def test_each_step_a_customer_reaches_is_recorded(world):
    fid = world.flow(flow("ask", ask={"type": "ask", "text": "City?", "save_as": "var:c", "next": "done"}, done={"type": "send_text", "text": "Thanks."}))
    world.say("hello", "wamid.A1")
    world.say("Surat", "wamid.A2")
    assert [(r["node"], r["kind"]) for r in world.db.q("select node, kind from flow_run_steps order by id")] == [("ask", "ask"), ("done", "send_text")]
    assert world.db.one("select count(*) from flow_run_steps where flow_id = %s", (fid,)) == 2


# ── the ad click behind a contact, and telling Meta ──────────────────────────

class Meta:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, data, headers):
        self.calls.append({"method": method, "url": url, "body": json.loads(data) if data else None, "auth": headers.get("Authorization")})
        return 200, json.dumps({"events_received": 1, "fbtrace_id": "T1"}).encode()


@pytest.fixture
def ads(world):
    meta = Meta()
    capi.configure(graph=Graph(meta), vault=world.vault_)
    yield world, meta
    capi.configure(graph=None, vault=None)


def ad_click_first_message(world, clid="ARAbc123"):
    msg = P.ad_click("wamid.AD1", ts=str(int(time.time())))
    msg["referral"]["ctwa_clid"] = clid
    H.deliver(world.db, P.inbound(msg))
    H.work(world.db)


def test_the_ad_click_id_is_kept_on_the_contact_the_first_time_only(ads):
    world, _meta = ads
    ad_click_first_message(world, "FIRST")
    ad_click_first_message_again = P.ad_click("wamid.AD2", ts=str(int(time.time())))
    ad_click_first_message_again["referral"]["ctwa_clid"] = "SECOND"
    H.deliver(world.db, P.inbound(ad_click_first_message_again))
    H.work(world.db)
    assert world.db.one("select ctwa_clid from contacts") == "FIRST"


def test_a_lead_is_told_to_meta_with_the_click_it_came_from(ads):
    world, meta = ads
    world.do(capi.save_settings, dataset_id="123456789012", test_code="TEST1")
    ad_click_first_message(world)
    contact = world.db.one("select id from contacts")
    out = world.do(capi.send_for_contact, contact, event="Purchase", value="12,500", currency="INR", now=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))
    assert out == {"events_received": 1, "fbtrace_id": "T1"}
    call = meta.calls[0]
    assert call["url"].endswith("/123456789012/events") and call["auth"] == "Bearer EAAB-flow-token" and call["body"]["test_event_code"] == "TEST1"
    row = call["body"]["data"][0]
    assert row == {"event_name": "Purchase", "event_time": int(datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()), "action_source": "business_messaging",
                   "messaging_channel": "whatsapp", "user_data": {"whatsapp_business_account_id": P.WABA, "ctwa_clid": "ARAbc123"}, "custom_data": {"value": 12500.0, "currency": "INR"}}


def test_a_flow_step_tells_meta_and_a_person_who_did_not_come_from_an_ad_is_left_alone(ads):
    world, meta = ads
    world.do(capi.save_settings, dataset_id="123456789012")
    world.flow(flow("a", a={"type": "capi_event", "event": "Lead", "next": "b"}, b={"type": "send_text", "text": "Thanks."}))
    world.say("hello", "wamid.A1")                                                           # an ordinary chat: no click to credit
    assert meta.calls == [] and world.sent()[-1]["payload"]["text"]["body"] == "Thanks."
    world.db.q("update contacts set ctwa_clid = 'CLICK9'")
    world.db.q("update flow_runs set state = 'done'")
    world.say("hello again", "wamid.A2")
    assert len(meta.calls) == 1 and meta.calls[0]["body"]["data"][0]["user_data"]["ctwa_clid"] == "CLICK9"


@pytest.mark.parametrize("settings,contact_click,reason", [
    ({"dataset_id": ""}, "x", "off"), ({"dataset_id": "123456789012"}, None, "no_click")])
def test_telling_meta_says_why_it_cannot(ads, settings, contact_click, reason):
    world, _meta = ads
    world.do(capi.save_settings, **settings)
    world.db.q("insert into contacts (tenant_id, name, phone, ctwa_clid) values (%s, 'A', '919800000001', %s)", (world.tid, contact_click))
    with pytest.raises(capi.CapiError) as e:
        world.do(capi.send_for_contact, world.db.one("select id from contacts"), event="Lead")
    assert e.value.reason == reason


def test_the_dataset_id_is_digits_and_an_event_must_be_a_known_one(ads):
    world, _meta = ads
    with pytest.raises(capi.CapiError) as e:
        world.do(capi.save_settings, dataset_id="not-a-number")
    assert e.value.reason == "bad_dataset"
    world.do(capi.save_settings, dataset_id="123456789012")
    world.db.q("insert into contacts (tenant_id, name, phone, ctwa_clid) values (%s, 'A', '919800000001', 'c')", (world.tid,))
    with pytest.raises(capi.CapiError) as e2:
        world.do(capi.send_for_contact, world.db.one("select id from contacts"), event="Nonsense")
    assert e2.value.reason == "bad_event"
    with pytest.raises(capi.CapiError) as e3:
        world.do(capi.send_for_contact, world.db.one("select id from contacts"), event="Lead", value="lots")
    assert e3.value.reason == "bad_value"


def test_a_test_event_needs_the_code_and_goes_with_it(ads):
    world, meta = ads
    with pytest.raises(capi.CapiError) as e:
        world.do(capi.send_test)
    assert e.value.reason == "off"
    world.do(capi.save_settings, dataset_id="123456789012")
    with pytest.raises(capi.CapiError) as e2:
        world.do(capi.send_test)
    assert e2.value.reason == "no_test_code"
    world.do(capi.save_settings, dataset_id="123456789012", test_code="TEST9")
    assert world.do(capi.send_test)["events_received"] == 1 and meta.calls[0]["body"]["test_event_code"] == "TEST9"
    assert meta.calls[0]["body"]["data"][0]["user_data"]["ctwa_clid"] == "prism-test-click"


# ── how a flow is doing ──────────────────────────────────────────────────────

def test_the_numbers_say_how_many_finished_where_people_stop_and_who_was_handed_over(world):
    fid = world.flow(flow("menu", menu={"type": "buttons", "text": "Hi", "options": [{"id": "q", "title": "Quote", "next": "city"}, {"id": "p", "title": "Person", "next": "person"}]},
                          city={"type": "ask", "text": "City?", "save_as": "var:c", "next": "thanks"}, thanks={"type": "send_text", "text": "Thanks."},
                          person={"type": "handoff", "text": "Coming.", "reason": "asked"}))
    for i, (who, steps) in enumerate((("919800000001", ["hello", "button:q", "Surat"]), ("919800000002", ["hello", "button:p"]), ("919800000003", ["hello", "button:q"]), ("919800000004", ["hello"]))):
        for j, step in enumerate(steps):
            wamid = "wamid.%d%d" % (i, j)
            frm = who
            if step.startswith("button:"):
                msg = P.button_reply(step[7:], "x", wamid, ts=str(int(time.time())))
                msg["from"] = frm
            else:
                msg = P.text(step, wamid, ts=str(int(time.time())), frm=frm)
            H.deliver(world.db, P.inbound(msg))
            H.work(world.db)
    out = world.do(flow_ai.analytics, fid)
    t = out["totals"]
    assert t["started"] == 4 and t["done"] == 2 and t["in_progress"] == 2 and t["handed_to_person"] == 1 and t["completion_rate"] == 50.0
    nodes = {n["node"]: n for n in out["nodes"]}
    assert [n["node"] for n in out["nodes"]] == ["menu", "city", "person", "thanks"]
    assert nodes["menu"]["reached"] == 4 and nodes["menu"]["stopped"] == 1 and nodes["menu"]["stopped_pct"] == 25.0
    assert nodes["city"]["reached"] == 2 and nodes["city"]["stopped"] == 1 and nodes["thanks"]["reached"] == 1 and nodes["person"]["reached"] == 1
    assert nodes["menu"]["text"] == "Hi" and sum(d["runs"] for d in out["days"]) == 4


def test_a_flow_nobody_has_used_has_empty_numbers(world):
    fid = world.flow(flow("a", a={"type": "send_text", "text": "Hi"}))
    out = world.do(flow_ai.analytics, fid)
    assert out["totals"]["started"] == 0 and out["totals"]["completion_rate"] == 0.0 and out["nodes"][0]["reached"] == 0 and out["days"] == []


def test_analytics_belong_to_one_workspace(world):
    other = world.db.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    conn = world.conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(flows.FlowError):
                flow_ai.analytics(cur, other, world.flow(flow("a", a={"type": "send_text", "text": "Hi"})))
    finally:
        conn.close()


# ── writing a flow with AI ───────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, method, url, data, headers):
        body = json.loads(data)
        self.asked.append(body)
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return 200, json.dumps({"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode()


GOOD = {"name": "Quote bot", "trigger": {"type": "keyword", "match": "contains", "keywords": ["quote"]},
        "graph": {"entry": "name", "nodes": {"name": {"type": "ask", "text": "What is your name?", "save_as": "attr:FullName", "next": "city"},
                                              "city": {"type": "ask", "text": "Which city?", "save_as": "attr:City", "next": "done"},
                                              "done": {"type": "handoff", "text": "Thanks {{attr.FullName}}, we will call you.", "reason": "quote"}}}}


def test_a_described_flow_comes_back_valid_and_checked():
    llm = FakeLLM(json.dumps(GOOD))
    out = flow_ai.generate(LLM(api_key="k", transport=llm), description="A bot that takes a quote request: name, city, then hand over", business="Patel Constructions")
    assert out["valid"] is True and out["issues"] == [] and out["name"] == "Quote bot" and out["repairs"] == 0 and out["tokens"]["tokens_in"] == 100
    assert "Patel Constructions" in llm.asked[0]["messages"][1]["content"] and "never loops" in llm.asked[0]["messages"][0]["content"]
    assert flows.errors(flows.validate(out["graph"], out["trigger"])) == []


def test_small_mistakes_are_repaired_without_asking_again():
    bad = {"name": "Menu", "trigger": {"type": "first_message"}, "graph": {"entry": "Welcome Step", "nodes": {
        "Welcome Step": {"type": "buttons", "text": "Hello there, what would you like today?", "options": [
            {"id": "a", "title": "A very long button title indeed", "next": "gone"}, {"id": "b", "title": "Talk", "next": "Person Step"}]},
        "Person Step": {"type": "handoff", "text": "A colleague is coming."}}}}
    llm = FakeLLM(json.dumps(bad))
    out = flow_ai.generate(LLM(api_key="k", transport=llm), description="A welcome menu with two buttons and a hand over")
    assert out["valid"] is True and out["repairs"] == 0 and len(llm.asked) == 1
    g = out["graph"]
    assert g["entry"] == "Welcome_Step" and set(g["nodes"]) == {"Welcome_Step", "Person_Step"}
    opts = g["nodes"]["Welcome_Step"]["options"]
    assert opts[0]["title"] == "A very long button t" and opts[0]["next"] is None and opts[1]["next"] == "Person_Step"


def test_a_flow_that_still_breaks_a_rule_is_sent_back_to_the_model_with_the_problems():
    loop = {"name": "Loop", "trigger": {"type": "first_message"}, "graph": {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "b"}, "b": {"type": "send_text", "text": "y", "next": "a"}}}}
    llm = FakeLLM(json.dumps(loop), json.dumps(GOOD))
    out = flow_ai.generate(LLM(api_key="k", transport=llm), description="A bot that answers questions without end")
    assert out["valid"] is True and out["repairs"] == 1 and len(llm.asked) == 2
    assert "problems" in llm.asked[1]["messages"][-1]["content"] and "answer itself forever" in llm.asked[1]["messages"][-1]["content"]


def test_a_model_that_never_gets_it_right_returns_the_problems_not_a_flow_to_publish():
    loop = {"name": "Loop", "trigger": {"type": "first_message"}, "graph": {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "b"}, "b": {"type": "send_text", "text": "y", "next": "a"}}}}
    out = flow_ai.generate(LLM(api_key="k", transport=FakeLLM(json.dumps(loop))), description="A bot that answers questions without end", rounds=1)
    assert out["valid"] is False and any(i["code"] == "loop" for i in out["issues"])


def test_the_writer_survives_fences_and_refuses_nonsense():
    llm = FakeLLM("Sure!\n```json\n" + json.dumps(GOOD) + "\n```")
    assert flow_ai.generate(LLM(api_key="k", transport=llm), description="A quote bot please, name and city")["valid"] is True
    with pytest.raises(LLMError):
        flow_ai.generate(LLM(api_key="k", transport=FakeLLM("I cannot do that")), description="A bot that does something", rounds=0)
    with pytest.raises(ValueError):
        flow_ai.generate(LLM(api_key="k", transport=FakeLLM("{}")), description="hi")
    with pytest.raises(ValueError):
        flow_ai.generate(LLM(api_key="k", transport=FakeLLM("{}")), description="x" * 1501)
    with pytest.raises(LookupError):
        flow_ai.generate(None, description="A bot that does something useful")


def test_the_autofix_renames_trims_and_closes_without_changing_meaning():
    fixed = flow_ai.autofix({"entry": "x y", "nodes": {"x y": {"type": "list", "text": "t" * 2000, "button_label": "b" * 30, "rows": [
        {"id": "1", "title": "t" * 40, "description": "d" * 100, "next": "z z"}]}, "z z": {"type": "end"}}})
    node = fixed["nodes"]["x_y"]
    assert fixed["entry"] == "x_y" and len(node["text"]) == 1024 and len(node["button_label"]) == 20 and len(node["rows"][0]["title"]) == 24
    assert len(node["rows"][0]["description"]) == 72 and node["rows"][0]["next"] == "z_z" and "z_z" in fixed["nodes"]
    assert flow_ai.autofix({"nodes": {"a": {"type": "end"}}})["entry"] == "a" and flow_ai.autofix(None) == {}


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(world, migrated):
    llm = FakeLLM(json.dumps(GOOD))
    ai_agent.configure(LLM(api_key="k", transport=llm))
    meta = Meta()
    client = TestClient(create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token=INTERNAL,
                                   graph=Graph(meta), vault=world.vault_))

    capi.configure(graph=Graph(meta), vault=world.vault_)

    class A:
        world_, llm_, meta_ = world, llm, meta

        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(world.tid))
            return client.post(path, json=body, headers=AUTH)
    yield A
    ai_agent.configure(None)
    capi.configure(graph=None, vault=None)


def test_the_generate_route_returns_a_checked_flow_to_a_manager_only(api):
    r = api.post("/internal/flows/generate", description="A bot that takes a quote request with name and city")
    assert r.status_code == 200 and r.json()["valid"] is True and r.json()["graph"]["entry"] == "name"
    assert api.post("/internal/flows/generate", description="hi").status_code == 400
    ai_agent.configure(None)
    off = api.post("/internal/flows/generate", description="A bot that takes a quote request with name and city")
    assert off.status_code == 409 and "WA_LLM_API_KEY" in off.json()["message"]


def test_the_test_route_takes_a_pretend_time_and_holidays(api):
    r = api.post("/internal/flows/test", graph=cond("holiday", "yes"), inputs=["hi"], holidays=[{"date": "2026-11-12"}], local="2026-11-12T10:00:00+05:30")
    assert r.status_code == 200 and r.json()["transcript"][-1]["text"] == "yes"
    assert api.post("/internal/flows/test", graph=cond("holiday", "yes"), inputs=["hi"], local="yesterday").status_code == 400
    assert api.post("/internal/flows/test", graph=cond("weekday", "in", "sat"), inputs=["hi"], local="2026-11-12T10:00:00").json()["transcript"][-1]["text"] == "no"


def test_the_analytics_and_library_routes_answer(api):
    fid = api.world_.flow(flow("a", a={"type": "send_text", "text": "Hi"}))
    assert api.post("/internal/flows/analytics", flow_id=str(fid)).json()["totals"]["started"] == 0
    lib = api.post("/internal/flows/library").json()
    assert len(lib["library"]) >= 30 and "Retail" in lib["topics"] and "Construction" in lib["topics"]


def test_the_conversions_api_routes_save_test_and_send(api):
    assert api.post("/internal/capi/get").json()["settings"] == {"dataset_id": "", "test_code": "", "enabled": False}
    assert api.post("/internal/capi/save", dataset_id="123456789012", test_code="T1").json()["settings"]["enabled"] is True
    assert api.post("/internal/capi/save", dataset_id="abc").status_code == 400
    assert api.post("/internal/capi/test").json()["events_received"] == 1 and api.meta_.calls[-1]["body"]["test_event_code"] == "T1"
    cid = api.world_.db.one("insert into contacts (tenant_id, name, phone, ctwa_clid) values (%s, 'A', '919800000001', 'C1') returning id", (api.world_.tid,))
    assert api.post("/internal/capi/send", contact_id=str(cid), event="Lead").status_code == 200
    plain = api.world_.db.one("insert into contacts (tenant_id, name, phone) values (%s, 'B', '919800000002') returning id", (api.world_.tid,))
    r = api.post("/internal/capi/send", contact_id=str(plain), event="Lead")
    assert r.status_code == 409 and r.json()["error"] == "no_click"
    owner = api.world_.do(inbox.ensure_agent, "seat-owner", "Owner")
    bala = api.world_.do(inbox.ensure_agent, "seat-bala", "Bala")
    assert api.post("/internal/capi/save", dataset_id="123456789012", by_agent_id=str(bala["id"])).status_code == 409
    assert api.post("/internal/capi/save", dataset_id="123456789012", by_agent_id=str(owner["id"])).status_code == 200


# ── the library ──────────────────────────────────────────────────────────────

def test_every_library_flow_passes_the_rules_prism_applies_before_publishing():
    assert len(flow_library.LIBRARY) >= 30
    keys = [i["key"] for i in flow_library.LIBRARY]
    assert len(keys) == len(set(keys))
    for item in flow_library.LIBRARY:
        issues = flows.validate(item["graph"], item["trigger"])
        assert issues == [], (item["key"], [i.message for i in issues])


def test_the_library_covers_a_dozen_topics_and_marks_what_needs_a_person():
    topics = {i["topic"] for i in flow_library.LIBRARY}
    assert {"Construction", "General", "Retail", "Education", "Healthcare", "Real estate", "Restaurant", "Travel", "Finance", "Events"} <= topics
    needs = {i["key"] for i in flow_library.LIBRARY if i.get("needs_files")}
    assert {"catalogue_on_request", "rate_card_share", "brochure_request", "menu_share"} <= needs
    assert all(i["key"] in {k for k in needs} or not any(n.get("type") == "media" for n in i["graph"]["nodes"].values()) for i in flow_library.LIBRARY)
    assert {"business_hours_router", "ad_lead_to_meta", "lead_to_your_crm", "request_customer_location", "share_location_pin"} <= {i["key"] for i in flow_library.LIBRARY}


def test_a_library_flow_that_sends_a_file_cannot_be_published_until_a_real_file_is_chosen(world):
    item = flow_library.get("catalogue_on_request")
    fid = world.do(flows.create, "Catalogue", trigger=item["trigger"], graph=item["graph"])
    assert "unknown_asset" in codes(world.do(flows.check, fid))
    a = world.asset()
    graph = json.loads(json.dumps(item["graph"]))
    graph["nodes"]["file"]["asset_id"] = str(a["id"])
    world.do(flows.update, fid, graph=graph)
    assert world.do(flows.publish, fid)["version"] == 1


# ── an API step: keep the answer on the contact, and try the call first ──────

def test_an_api_answer_can_be_kept_on_the_contact_as_well_as_in_the_flow():
    def http(method, url, headers, body):
        return 200, json.dumps({"customer": {"city": "Surat", "tier": "gold"}})
    g = flow("a", a={"type": "api_request", "url": "https://crm.example.com/c", "save": {"attr:City": "customer.city", "tier": "customer.tier"}, "next": "b"},
             b={"type": "send_text", "text": "{{attr.City}} / {{var.tier}}"})
    assert said(flow_engine.test(g, None, ["hi"], http=http))[-1]["text"] == "Surat / gold"


@pytest.mark.parametrize("key", ["attr:", "9lives", "a b", "attr:9x"])
def test_a_kept_value_must_have_a_proper_name(key):
    g = flow("a", a={"type": "api_request", "url": "https://x.example.com/c", "save": {key: "x"}, "next": "b"}, b={"type": "end"})
    assert "bad_request" in codes(flows.validate(g))


def test_trying_a_call_shows_the_answer_and_what_would_be_kept():
    def http(method, url, headers, body):
        assert (method, url) == ("GET", "https://crm.example.com/c/Asha")
        return 200, json.dumps({"order": {"status": "packed"}})
    node = {"type": "api_request", "url": "https://crm.example.com/c/{{first_name}}", "save": {"state": "order.status", "missing": "order.nope"}}
    out = flow_engine.try_request(node, name="Asha Patel", http=http)
    assert out["ok"] and out["status"] == 200 and out["kept"] == {"state": "packed", "missing": None} and out["json"] is True


def test_trying_a_call_to_a_private_address_is_refused_in_words():
    out = flow_engine.try_request({"type": "api_request", "url": "https://localhost/x"})
    assert out["ok"] is False and "cannot be called" in out["error"]


def test_a_call_that_is_not_json_still_shows_what_came_back():
    out = flow_engine.try_request({"type": "api_request", "url": "https://x.example.com/a", "save": {"x": "a"}}, http=lambda *a: (200, "plain words"))
    assert out["ok"] and out["json"] is False and out["body"] == "plain words" and out["kept"] == {}
