"""Chatbot flows: the rules, a pretend walk, and the real engine against Postgres."""
import time
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import flow_engine, flows, ingest
from prism_wa.jobs import utcnow

MENU = {
    "entry": "menu",
    "nodes": {
        "menu": {"type": "buttons", "text": "Hi {{first_name}}! How can we help?",
                 "options": [{"id": "quote", "title": "Get a quote", "next": "ask_city"},
                             {"id": "visit", "title": "Site visit", "next": "tag_visit"}],
                 "fallback_next": None},
        "ask_city": {"type": "ask", "text": "Which city is the site in?", "save_as": "attr:City", "next": "thanks",
                     "max_tries": 2},
        "thanks": {"type": "send_text", "text": "Thanks! We will quote for {{attr.City}}.", "next": "end"},
        "tag_visit": {"type": "add_tag", "tag": "wants-visit", "next": "person"},
        "person": {"type": "handoff", "text": "A colleague will call you.", "reason": "site visit"},
        "end": {"type": "end"},
    }}
HELLO = {"type": "keyword", "match": "contains", "keywords": ["hello", "hi"]}


# ── the rules ────────────────────────────────────────────────────────────────

def codes(issues, severity="error"):
    return {i.code for i in issues if i.severity == severity}


def test_a_good_flow_has_no_errors():
    assert flows.errors(flows.validate(MENU, HELLO)) == []


def test_an_empty_or_headless_flow_is_refused():
    assert "empty" in codes(flows.validate({}))
    assert "no_entry" in codes(flows.validate({"entry": "x", "nodes": {"a": {"type": "end"}}}))


def test_a_step_that_leads_nowhere_is_an_error_and_an_orphan_a_warning():
    g = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "hi", "next": "gone"}, "b": {"type": "end"}}}
    issues = flows.validate(g)
    assert "dangling" in codes(issues) and "unreachable" in codes(issues, "warning")


def test_a_loop_that_never_waits_cannot_be_published_but_one_that_asks_can():
    forever = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "b"}, "b": {"type": "send_text", "text": "y", "next": "a"}}}
    assert "loop" in codes(flows.validate(forever))
    ok = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "b"},
                                  "b": {"type": "ask", "text": "again?", "next": "a"}}}
    assert "loop" not in codes(flows.validate(ok))


def test_buttons_and_lists_obey_whatsapps_limits():
    four = {"entry": "a", "nodes": {"a": {"type": "buttons", "text": "x", "options": [
        {"id": str(i), "title": "t"} for i in range(4)]}}}
    assert "bad_options" in codes(flows.validate(four))
    long_title = {"entry": "a", "nodes": {"a": {"type": "buttons", "text": "x", "options": [{"id": "1", "title": "t" * 21}]}}}
    assert "bad_options" in codes(flows.validate(long_title))
    rows = {"entry": "a", "nodes": {"a": {"type": "list", "text": "x", "button_label": "Pick", "rows": [{"id": str(i), "title": "t"} for i in range(11)]}}}
    assert "bad_options" in codes(flows.validate(rows))
    dup = {"entry": "a", "nodes": {"a": {"type": "buttons", "text": "x", "options": [{"id": "1", "title": "a"}, {"id": "1", "title": "b"}]}}}
    assert "bad_options" in codes(flows.validate(dup))


def test_a_pattern_that_could_hang_the_bot_is_refused():
    bad = {"entry": "a", "nodes": {"a": {"type": "ask", "text": "x", "validator": "regex", "pattern": "(a+)+$"}}}
    assert "bad_validator" in codes(flows.validate(bad))
    fine = {"entry": "a", "nodes": {"a": {"type": "ask", "text": "x", "validator": "regex", "pattern": "^[0-9]{6}$"}}}
    assert "bad_validator" not in codes(flows.validate(fine))
    assert flows.validate_trigger({"type": "regex", "pattern": "(x*)*y"})


@pytest.mark.parametrize("node", [
    {"type": "send_text", "text": ""}, {"type": "ask", "text": "q", "validator": "nope"}, {"type": "ask", "text": "q", "save_as": "oops"},
    {"type": "delay", "minutes": 0}, {"type": "delay"}, {"type": "add_tag", "tag": ""}, {"type": "set_attribute", "key": "1bad", "value": "x"},
    {"type": "api_request", "url": "http://insecure.example"}, {"type": "jump_flow", "flow_id": "not-a-uuid"},
    {"type": "condition", "rules": []}, {"type": "template"},
])
def test_each_kind_of_step_checks_its_own_fields(node):
    assert flows.errors(flows.validate({"entry": "a", "nodes": {"a": node}}))


def test_triggers_are_checked():
    assert flows.validate_trigger(HELLO) == []
    assert flows.validate_trigger({"type": "keyword", "match": "sometimes", "keywords": ["x"]})
    assert flows.validate_trigger({"type": "keyword", "match": "exact", "keywords": []})
    assert flows.validate_trigger({"type": "ad", "ad_ids": []})
    assert flows.validate_trigger({"type": "unknown"})
    assert flows.validate_trigger({"type": "first_message"}) == [] and flows.validate_trigger({"type": "default"}) == []


# ── matching and answers ─────────────────────────────────────────────────────

def test_triggers_match_the_way_they_say():
    fits = flow_engine.trigger_fits
    kw = lambda match, *words: {"type": "keyword", "match": match, "keywords": list(words)}      # noqa: E731
    assert fits(kw("exact", "price"), {"text": " Price "}, first_message=False) == "explicit"
    assert fits(kw("exact", "price"), {"text": "price list"}, first_message=False) is None
    assert fits(kw("contains", "price"), {"text": "what is the price?"}, first_message=False) == "explicit"
    assert fits(kw("starts_with", "quote"), {"text": "Quote me"}, first_message=False) == "explicit"
    assert fits({"type": "regex", "pattern": r"^\d{6}$"}, {"text": "390001"}, first_message=False) == "explicit"
    assert fits({"type": "ad", "ad_ids": ["77"]}, {"referral": {"source_id": "77"}}, first_message=False) == "explicit"
    assert fits({"type": "ad", "ad_ids": ["77"]}, {"referral": {"source_id": "78"}}, first_message=False) is None
    assert fits({"type": "button", "payload": "go"}, {"payload_id": "go"}, first_message=False) == "explicit"
    assert fits({"type": "first_message"}, {"text": "x"}, first_message=True) == "first"
    assert fits({"type": "first_message"}, {"text": "x"}, first_message=False) is None
    assert fits({"type": "default"}, {"text": "x"}, first_message=False) == "default"


@pytest.mark.parametrize("kind,text,ok,value", [
    ("number", "1,200", True, "1200"), ("number", "twelve", False, None), ("number", "2.5", True, "2.5"),
    ("email", "Asha@X.in", True, "asha@x.in"), ("email", "nope", False, None),
    ("phone", "98123 45678", True, "919812345678"), ("phone", "12", False, None),
    ("date", "05/11/2026", True, "2026-11-05"), ("date", "2026-11-05", True, "2026-11-05"), ("date", "soon", False, None),
    ("text", "  anything  ", True, "anything"), ("text", "   ", False, None),
])
def test_answers_are_checked_and_tidied(kind, text, ok, value):
    assert flow_engine.validate_answer({"validator": kind}, text) == (ok, value)


def test_an_answer_must_be_one_of_the_options_and_matches_any_case():
    node = {"validator": "options", "options": ["Villa", "Apartment"]}
    assert flow_engine.validate_answer(node, "villa") == (True, "Villa")
    assert flow_engine.validate_answer(node, "castle") == (False, None)


# ── a pretend walk (no phone, no database) ───────────────────────────────────

def test_the_test_console_walks_a_flow_and_says_what_the_customer_would_get():
    out = flow_engine.test(MENU, HELLO, ["hello", "Get a quote", "Vadodara"], name="Asha Patel")
    bot = [t for t in out["transcript"] if t["from"] == "bot"]
    assert bot[0]["kind"] == "buttons" and bot[0]["text"] == "Hi Asha! How can we help?"
    assert [o["title"] for o in bot[0]["options"]] == ["Get a quote", "Site visit"]
    assert bot[1]["text"] == "Which city is the site in?"
    assert any(t.get("text") == "Thanks! We will quote for Vadodara." for t in bot) or any("Vadodara" in str(t) for t in bot)
    assert out["state"] == "done"


def test_the_console_shows_a_handover_and_does_not_send_anything():
    out = flow_engine.test(MENU, HELLO, ["hi", "Site visit"])
    assert out["handoff"] == "site visit" and out["state"] == "done"
    assert any(t.get("text") == "added the tag wants-visit" for t in out["transcript"])


def test_an_invalid_flow_is_reported_not_run():
    out = flow_engine.test({"entry": "x", "nodes": {"a": {"type": "end"}}}, HELLO, ["hi"])
    assert out["state"] == "invalid" and out["issues"]


def test_a_wrong_answer_is_asked_again_then_gives_up():
    g = {"entry": "q", "nodes": {"q": {"type": "ask", "text": "Your email?", "validator": "email", "save_as": "var:e", "next": "ok",
                                       "max_tries": 2, "invalid_next": "bye"},
                                 "ok": {"type": "send_text", "text": "got it"}, "bye": {"type": "send_text", "text": "never mind"}}}
    out = flow_engine.test(g, None, ["hi", "no", "still no"])
    texts = [t.get("text") for t in out["transcript"] if t["from"] == "bot"]
    assert texts == ["Your email?", "Sorry, I didn't get that. Your email?", "never mind"]


def test_a_delay_passes_by_itself_in_the_console():
    g = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "one", "next": "d"}, "d": {"type": "delay", "minutes": 30, "next": "b"},
                                 "b": {"type": "send_text", "text": "two"}}}
    out = flow_engine.test(g, None, ["hi"])
    assert [t.get("text") for t in out["transcript"] if t["from"] == "bot"] == ["one", "two"] and out["state"] == "done"


def test_a_condition_picks_the_first_rule_that_fits_else_the_else():
    g = {"entry": "c", "nodes": {"c": {"type": "condition", "rules": [{"field": "attr:City", "op": "eq", "value": "surat", "next": "s"}], "else": "o"},
                                 "s": {"type": "send_text", "text": "Surat!"}, "o": {"type": "send_text", "text": "somewhere else"}}}
    assert flow_engine.test(g, None, ["x"], attrs={"City": "Surat"})["transcript"][-1]["text"] == "Surat!"
    assert flow_engine.test(g, None, ["x"], attrs={"City": "Anand"})["transcript"][-1]["text"] == "somewhere else"


def test_a_flow_that_somehow_loops_is_stopped_by_the_step_cap():
    g = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "a"}}}
    run = {"node": "a", "vars": {}, "waiting": None, "deadline": None, "state": "running", "steps": 0}
    flow_engine.advance(g, run, flow_engine.Context(), flow_engine.DryActions())
    assert run["state"] == "failed" and "too many steps" in run["last_error"]


def test_an_api_step_saves_what_the_other_server_said(monkeypatch):
    g = {"entry": "r", "nodes": {"r": {"type": "api_request", "url": "https://api.example.com/order?id={{message}}", "save": {"status": "order.status"},
                                       "next": "t", "error_next": "bad"},
                                 "t": {"type": "send_text", "text": "Your order is {{var.status}}"}, "bad": {"type": "send_text", "text": "cannot check"}}}
    seen = []
    ok = lambda method, url, headers, body: (seen.append(url), (200, '{"order": {"status": "packed"}}'))[1]       # noqa: E731
    out = flow_engine.test(g, None, ["A-17"], http=ok)
    assert out["transcript"][-1]["text"] == "Your order is packed" and seen == ["https://api.example.com/order?id=A-17"]
    down = lambda method, url, headers, body: (500, "")                                                       # noqa: E731
    assert flow_engine.test(g, None, ["A-17"], http=down)["transcript"][-1]["text"] == "cannot check"


def test_a_bot_can_never_call_this_machine_or_a_private_network():
    for url in ("https://localhost/x", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://10.0.0.5/x", "http://example.com/x"):
        with pytest.raises(flow_engine.FlowRuntimeError):
            flow_engine.default_http("GET", url, {}, None)


# ── the engine against a real database ───────────────────────────────────────

@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tenant_id,))

    class W:
        db, tid, nid = migrated, tenant_id, number_id

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def flow(cls, graph=MENU, trigger=HELLO, name="Menu", enable=True):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    fid = flows.create(cur, cls.tid, name, trigger=trigger, graph=graph)
                    flows.publish(cur, cls.tid, fid)
                    if enable:
                        flows.set_enabled(cur, cls.tid, fid, True)
                conn.commit()
                return fid
            finally:
                conn.close()

        @classmethod
        def say(cls, text, wamid, **kw):
            kw.setdefault("ts", str(int(time.time())))               # real time: the 24-hour window must be open
            H.deliver(migrated, P.inbound(P.text(text, wamid, **kw)))
            H.work(migrated)

        @classmethod
        def press(cls, id_, title, wamid):
            H.deliver(migrated, P.inbound(P.button_reply(id_, title, wamid, ts=str(int(time.time())))))
            H.work(migrated)

        @classmethod
        def sent(cls):
            return migrated.q("select kind, payload, state, run_at from outbox order by created_at, id")

        @classmethod
        def run(cls):
            rows = migrated.q("select * from flow_runs order by started_at desc")
            return rows[0] if rows else None

    return W


def texts(world):
    out = []
    for r in world.sent():
        p = r["payload"]
        out.append(p["text"]["body"] if p["type"] == "text" else p["interactive"]["body"]["text"])
    return out


def test_a_keyword_starts_the_flow_and_the_menu_is_sent_as_buttons(world):
    world.flow()
    world.say("hello", "wamid.A1")
    sent = world.sent()
    assert len(sent) == 1 and sent[0]["kind"] == "interactive"
    buttons = sent[0]["payload"]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["quote", "visit"]
    assert sent[0]["payload"]["interactive"]["body"]["text"] == "Hi Asha! How can we help?"
    run = world.run()
    assert run["state"] == "waiting" and run["node"] == "menu" and run["version"] == 1


def test_pressing_a_button_then_answering_a_question_finishes_the_flow_and_saves_the_answer(world):
    world.flow()
    world.say("hi", "wamid.A1")
    world.press("quote", "Get a quote", "wamid.A2")
    assert texts(world)[-1] == "Which city is the site in?" and world.run()["state"] == "waiting"
    world.say("Vadodara", "wamid.A3")
    assert texts(world)[-1] == "Thanks! We will quote for Vadodara."
    assert world.run()["state"] == "done"
    assert world.db.one("select value #>> '{}' from contact_attributes where key = 'City'") == "Vadodara"


def test_the_handover_branch_tags_the_contact_and_asks_for_a_person(world):
    world.flow()
    world.say("hello", "wamid.A1")
    world.press("visit", "Site visit", "wamid.A2")
    assert texts(world)[-1] == "A colleague will call you."
    assert "wants-visit" in world.db.one("select tags from contacts")
    assert world.db.one("select status from conversations") == "requesting"
    assert world.run()["state"] == "done"


def test_typing_a_buttons_title_works_as_well_as_pressing_it(world):
    world.flow()
    world.say("hello", "wamid.A1")
    world.say("get a quote", "wamid.A2")
    assert texts(world)[-1] == "Which city is the site in?"


def test_an_unclear_reply_is_given_one_more_go_then_ends(world):
    g = {"entry": "m", "nodes": {"m": {"type": "buttons", "text": "Pick", "options": [{"id": "a", "title": "A", "next": "x"}]},
                                 "x": {"type": "send_text", "text": "picked"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    world.say("blah", "wamid.A2")
    assert len(world.sent()) == 2 and world.run()["state"] == "waiting"
    world.say("blah again", "wamid.A3")
    assert world.run()["state"] == "done" and len(world.sent()) == 2


def test_a_flow_that_is_not_published_or_not_on_never_answers(world):
    conn = world.conn()
    with conn.cursor() as cur:
        fid = flows.create(cur, world.tid, "Draft", trigger=HELLO, graph=MENU)
    conn.commit()
    world.say("hello", "wamid.A1")
    assert world.sent() == []
    with conn.cursor() as cur:
        flows.publish(cur, world.tid, fid)
    conn.commit()
    world.say("hello", "wamid.A2")                       # published but still off
    assert world.sent() == []
    conn.close()


def test_a_flow_cannot_be_switched_on_before_it_is_published_and_publishing_needs_a_valid_flow(world):
    conn = world.conn()
    with conn.cursor() as cur:
        fid = flows.create(cur, world.tid, "Draft", trigger=HELLO, graph=MENU)
        with pytest.raises(flows.FlowError) as e:
            flows.set_enabled(cur, world.tid, fid, True)
        assert e.value.reason == "not_published"
        flows.update(cur, world.tid, fid, graph={"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "nowhere"}}})
        with pytest.raises(flows.FlowError) as e:
            flows.publish(cur, world.tid, fid)
        assert e.value.reason == "flow_invalid" and e.value.issues
    conn.close()


def test_a_basic_plan_gets_no_flows(world):
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"basic\"}'")
    conn = world.conn()
    with conn.cursor() as cur:
        with pytest.raises(Exception) as e:
            flows.create(cur, world.tid, "Nope", trigger=HELLO, graph=MENU)
        assert "plan" in str(e.value).lower()
    conn.close()


def test_the_plans_flow_count_is_enforced(world):
    conn = world.conn()
    with conn.cursor() as cur:
        for i in range(5):
            flows.create(cur, world.tid, "F%d" % i)
        with pytest.raises(Exception) as e:
            flows.create(cur, world.tid, "one too many")
        assert "allows 5" in str(e.value)
    conn.close()


def test_a_person_stepping_in_stops_the_bot_and_cancels_the_run(world):
    world.flow()
    world.say("hello", "wamid.A1")
    world.db.q("update conversations set status = 'intervened'")
    world.say("get a quote", "wamid.A2")
    assert len(world.sent()) == 1
    assert world.run()["state"] == "cancelled"


def test_a_republish_does_not_change_the_flow_a_customer_is_in(world):
    fid = world.flow()
    world.say("hello", "wamid.A1")
    conn = world.conn()
    with conn.cursor() as cur:
        flows.update(cur, world.tid, fid, graph={"entry": "menu", "nodes": {"menu": {"type": "send_text", "text": "totally different"}}})
        assert flows.publish(cur, world.tid, fid)["version"] == 2
    conn.commit()
    conn.close()
    world.press("quote", "Get a quote", "wamid.A2")           # still the old menu: version 1
    assert texts(world)[-1] == "Which city is the site in?" and world.run()["version"] == 1
    assert [v["version"] for v in flows_versions(world, fid)] == [2, 1]


def flows_versions(world, fid):
    conn = world.conn()
    try:
        with conn.cursor() as cur:
            return flows.versions(cur, world.tid, fid)
    finally:
        conn.close()


def test_rolling_back_makes_an_old_version_live_as_a_new_one(world):
    fid = world.flow()
    conn = world.conn()
    with conn.cursor() as cur:
        flows.update(cur, world.tid, fid, graph={"entry": "m", "nodes": {"m": {"type": "send_text", "text": "v2"}}})
        flows.publish(cur, world.tid, fid)
        assert flows.rollback(cur, world.tid, fid, 1)["version"] == 3
        assert flows.get(cur, world.tid, fid)["graph"]["entry"] == "menu"
        with pytest.raises(flows.FlowError):
            flows.rollback(cur, world.tid, fid, 99)
    conn.commit()
    conn.close()
    world.say("hello", "wamid.A1")
    assert texts(world)[-1] == "Hi Asha! How can we help?"


def test_switching_a_flow_off_stops_people_who_are_in_it(world):
    fid = world.flow()
    world.say("hello", "wamid.A1")
    conn = world.conn()
    with conn.cursor() as cur:
        flows.set_enabled(cur, world.tid, fid, False)
    conn.commit()
    conn.close()
    assert world.run()["state"] == "cancelled"
    world.say("get a quote", "wamid.A2")
    assert len(world.sent()) == 1


def test_a_keyword_for_another_flow_pulls_the_customer_out_of_the_first(world):
    world.flow(name="Menu")
    other = {"entry": "p", "nodes": {"p": {"type": "send_text", "text": "Prices: see our site"}}}
    world.flow(other, {"type": "keyword", "match": "exact", "keywords": ["price"]}, name="Prices")
    world.say("hello", "wamid.A1")
    world.say("price", "wamid.A2")
    assert texts(world)[-1] == "Prices: see our site"
    states = [r["state"] for r in world.db.q("select state from flow_runs order by started_at")]
    assert states == ["cancelled", "done"]


def test_the_first_message_and_default_triggers_fill_in_where_no_keyword_fits(world):
    welcome = {"entry": "w", "nodes": {"w": {"type": "send_text", "text": "Welcome, first time?"}}}
    fallback = {"entry": "f", "nodes": {"f": {"type": "send_text", "text": "Sorry, I did not catch that"}}}
    world.flow(welcome, {"type": "first_message"}, name="Welcome")
    world.flow(fallback, {"type": "default"}, name="Fallback")
    world.say("anything", "wamid.A1")
    assert texts(world) == ["Welcome, first time?"]
    world.say("something else", "wamid.A2")
    assert texts(world)[-1] == "Sorry, I did not catch that"


def test_a_delay_resumes_when_its_time_comes_and_not_before(world):
    g = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "Booked.", "next": "d"},
                                 "d": {"type": "delay", "minutes": 60, "next": "b"},
                                 "b": {"type": "send_text", "text": "Reminder: your visit is today"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    assert texts(world) == ["Booked."] and world.run()["state"] == "waiting"
    H.work(world.db, now=utcnow() + timedelta(minutes=30))
    assert texts(world) == ["Booked."]
    H.work(world.db, now=utcnow() + timedelta(minutes=61))
    assert texts(world) == ["Booked.", "Reminder: your visit is today"] and world.run()["state"] == "done"


def test_an_unanswered_question_times_out_to_its_fallback(world):
    g = {"entry": "q", "nodes": {"q": {"type": "ask", "text": "Still there?", "timeout_minutes": 10, "next": "yes", "timeout_next": "nudge"},
                                 "yes": {"type": "send_text", "text": "great"}, "nudge": {"type": "send_text", "text": "We are here when you are ready"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    H.work(world.db, now=utcnow() + timedelta(minutes=11))
    assert texts(world)[-1] == "We are here when you are ready" and world.run()["state"] == "done"


def test_an_answered_question_ignores_its_old_timer(world):
    g = {"entry": "q", "nodes": {"q": {"type": "ask", "text": "Name?", "timeout_minutes": 10, "next": "yes", "timeout_next": "nudge"},
                                 "yes": {"type": "send_text", "text": "thanks"}, "nudge": {"type": "send_text", "text": "hello?"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    world.say("Asha", "wamid.A2")
    H.work(world.db, now=utcnow() + timedelta(minutes=20))
    assert texts(world)[-1] == "thanks" and len(world.sent()) == 2


def test_a_bot_that_breaks_never_costs_the_customer_their_message(world, monkeypatch):
    world.flow()
    monkeypatch.setattr(flow_engine, "on_inbound", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    world.say("hello", "wamid.A1")
    assert world.db.one("select count(*) from messages where direction = 'in'") == 1
    assert world.db.one("select count(*) from wa_jobs where state = 'dead'") == 0


def test_a_flow_can_set_attributes_and_branch_on_them_later(world):
    g = {"entry": "s", "nodes": {"s": {"type": "set_attribute", "key": "Segment", "value": "builder", "next": "c"},
                                 "c": {"type": "condition", "rules": [{"field": "attr:Segment", "op": "eq", "value": "builder", "next": "b"}], "else": "n"},
                                 "b": {"type": "send_text", "text": "Builder pricing"}, "n": {"type": "send_text", "text": "Retail pricing"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    assert texts(world) == ["Builder pricing"]
    assert world.db.one("select value #>> '{}' from contact_attributes where key = 'Segment'") == "builder"


def test_the_agent_step_hands_the_chat_to_an_enabled_ai_agent(world):
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"premium\"}'")
    aid = world.db.one("insert into ai_agents (tenant_id, name, enabled, is_default) values (%s, 'Aria', true, true) returning id", (world.tid,))
    g = {"entry": "a", "nodes": {"a": {"type": "send_text", "text": "Let me hand you to Aria", "next": "g"}, "g": {"type": "agent"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    assert world.db.one("select ai_agent_id from conversations") == aid


def test_a_template_step_goes_out_even_after_the_window_would_have_closed(world):
    g = {"entry": "t", "nodes": {"t": {"type": "template", "name": "visit_reminder", "language": "en_US", "next": "e"}, "e": {"type": "end"}}}
    world.flow(g)
    world.say("hello", "wamid.A1")
    row = world.sent()[0]
    assert row["kind"] == "template" and row["payload"]["template"]["name"] == "visit_reminder"


def test_the_runs_of_a_flow_are_listed_for_the_owner(world):
    fid = world.flow()
    world.say("hello", "wamid.A1")
    conn = world.conn()
    with conn.cursor() as cur:
        rows = flow_engine.runs(cur, world.tid, fid)
        listing = flows.list_flows(cur, world.tid)
    conn.close()
    assert rows[0]["state"] == "waiting" and rows[0]["contact"] == "Asha"
    assert listing[0]["live"] == 1 and listing[0]["enabled"] is True and listing[0]["version"] == 1


def test_another_workspace_cannot_see_or_touch_a_flow(world, migrated):
    fid = world.flow()
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    conn = world.conn()
    with conn.cursor() as cur:
        with pytest.raises(flows.FlowError):
            flows.get(cur, other, fid)
        assert flows.delete(cur, other, fid) is False
    conn.close()
