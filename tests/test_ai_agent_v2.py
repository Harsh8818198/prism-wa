"""The structured AI agent: draft and published copies, test numbers, profile / voice / skills / actions / settings, the questions it could not answer, its numbers,
reading a whole website, and finding a note by meaning. Against a real Postgres, with a stand-in for the language model, the embeddings service and the web."""
import json
import os
import time

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient
from test_ai_agent import Model, in_db, model, world  # noqa: F401  (fixtures)

from prism_wa import ai_actions, ai_agent, ai_insights, ai_knowledge, ai_profile, embeddings, flow_engine
from prism_wa.app import create_app
from prism_wa.embeddings import Embedder
from prism_wa.graph import Graph
from prism_wa.llm import LLMError
from prism_wa.vault import Vault


@pytest.fixture(autouse=True)
def _fresh_web():
    yield
    ai_agent.FETCH = flow_engine.default_http
    ai_agent.configure(None)


def say_json(model, **fields):
    model.answers.append({"choices": [{"message": {"content": json.dumps(fields)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def agent_row(world, aid="x"):
    return world.db.q("select * from ai_agents")[0]


def update(world, aid, **changes):
    return in_db(world, lambda cur: ai_agent.update(cur, world.tid, aid, **changes))


def publish(world, aid):
    return in_db(world, lambda cur: ai_agent.publish(cur, world.tid, aid))


def system_of(model, n=-1):
    return model.requests[n]["body"]["messages"][0]["content"]


# ── a draft apart from what is live ──────────────────────────────────────────

def test_switching_an_agent_on_for_the_first_time_publishes_it(world):
    aid = world.agent(instructions="Be brief.")
    row = agent_row(world)
    assert row["published_version"] == 1 and row["live"]["instructions"] == "Be brief." and row["published_at"] is not None
    shown = in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))
    assert shown["status"] == "published" and shown["unpublished_changes"] is False and "live" not in shown


def test_editing_the_draft_changes_nothing_customers_meet_until_it_is_published(world):
    aid = world.agent(instructions="Speak warmly.")
    update(world, aid, instructions="Speak like a pirate.")
    shown = in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))
    assert shown["unpublished_changes"] is True and shown["instructions"] == "Speak like a pirate."
    world.llm.say("Hello.")
    world.say("hi", "wamid.D1")
    assert "Speak warmly." in system_of(world.llm) and "pirate" not in system_of(world.llm)
    out = publish(world, aid)
    assert out["version"] == 2 and in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))["unpublished_changes"] is False
    world.llm.say("Ahoy.")
    world.say("hello again", "wamid.D2")
    assert "pirate" in system_of(world.llm)


def test_a_paused_agent_says_nothing_to_customers_and_comes_back_when_published(world):
    aid = world.agent()
    in_db(world, lambda cur: ai_agent.unpublish(cur, world.tid, aid))
    assert in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))["status"] == "paused"
    world.say("hello", "wamid.P1")
    assert world.llm.requests == [] and world.out() == []
    publish(world, aid)
    world.llm.say("Back again.")
    world.say("hello?", "wamid.P2")
    assert world.out() == ["Back again."]


def test_switching_a_paused_agent_off_and_on_does_not_publish_it_again(world):
    aid = world.agent()
    in_db(world, lambda cur: ai_agent.unpublish(cur, world.tid, aid))
    in_db(world, lambda cur: ai_agent.set_enabled(cur, world.tid, aid, False))
    in_db(world, lambda cur: ai_agent.set_enabled(cur, world.tid, aid, True))
    assert in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))["status"] == "paused" and agent_row(world)["published_version"] == 1


def test_publishing_says_what_is_missing_without_refusing(world):
    aid = world.agent(instructions="")
    out = publish(world, aid)
    assert out["warnings"] and any("not been told" in w for w in out["warnings"]) and any("nothing to read" in w for w in out["warnings"])
    world.learn(aid, "Hours", "Open nine to six.")
    update(world, aid, instructions="We build walls.")
    assert publish(world, aid)["warnings"] == []


def test_a_test_number_talks_to_the_draft_and_everyone_else_to_the_live_copy(world):
    aid = world.agent(instructions="Live words.")
    update(world, aid, instructions="Draft words.")
    world.llm.say("one")
    world.say("hi", "wamid.T1")
    assert "Live words." in system_of(world.llm)
    phone = world.db.one("select phone from contacts")
    update(world, aid, settings={"test_numbers": [phone]})
    world.llm.say("two")
    world.say("hi again", "wamid.T2")
    assert "Draft words." in system_of(world.llm)


def test_a_test_number_meets_the_draft_even_when_the_agent_was_never_taken_live(world):
    aid = world.agent()
    in_db(world, lambda cur: ai_agent.unpublish(cur, world.tid, aid))
    world.say("hi", "wamid.T3")
    assert world.llm.requests == []
    update(world, aid, settings={"test_numbers": [world.db.one("select phone from contacts")]})
    world.llm.say("I am in training.")
    world.say("hi again", "wamid.T4")
    assert world.out() == ["I am in training."]


def test_an_agent_that_covers_one_number_ignores_chats_on_another(world):
    aid = world.agent()
    update(world, aid, settings={"numbers": ["00000000-0000-0000-0000-0000000000aa"]})
    publish(world, aid)
    world.say("hello", "wamid.N1")
    assert world.llm.requests == [] and world.out() == []
    update(world, aid, settings={"numbers": [str(world.nid)]})
    publish(world, aid)
    world.llm.say("Hello!")
    world.say("hello again", "wamid.N2")
    assert world.out() == ["Hello!"]


def test_agents_that_were_on_before_this_change_keep_answering_from_their_live_copy(world):
    aid = world.agent(instructions="Old words.")
    world.db.q("update ai_agents set live = null, published_version = 0, published_at = null")
    world.db.q("""update ai_agents set live = jsonb_build_object('instructions', instructions, 'handoff_words', to_jsonb(handoff_words), 'max_replies', max_replies,
                  'business_hours_only', business_hours_only, 'profile', profile, 'voice', voice, 'skills', skills, 'actions', actions, 'settings', settings),
                  published_version = 1 where enabled""")
    world.llm.say("Still here.")
    world.say("hi", "wamid.O1")
    assert world.out() == ["Still here."]


# ── what the owner may put in, and what is kept secret ───────────────────────

@pytest.mark.parametrize("field,value,reason", [
    ("profile", {"website": "http://plain.example"}, "bad_website"), ("profile", {"description": "x" * 1501}, "bad_description"),
    ("profile", {"ground_rules": ["a"] * 11}, "bad_rules"), ("voice", {"tone": "angry"}, "bad_tone"), ("voice", {"length": "novel"}, "bad_length"),
    ("skills", [{"name": "A", "kind": "dancing"}], "bad_skill"), ("skills", [{"name": ""}], "bad_name"),
    ("skills", [{"name": "A", "variables": [{"name": "City"}, {"name": "city"}]}], "bad_variable"),
    ("skills", [{"name": "A", "variables": [{"name": "city", "attribute": "9bad"}]}], "bad_variable"),
    ("actions", [{"name": "Stock", "description": "d", "url": "http://x.example"}], "bad_url"), ("actions", [{"name": "Stock", "description": "d", "url": "https://x.example", "method": "PUT"}], "bad_action"),
    ("actions", [{"name": "Stock", "url": "https://x.example"}], "bad_description"), ("actions", [{"name": "S", "description": "d", "url": "https://x.example", "headers": {"bad header": "v"}}], "bad_headers"),
    ("settings", {"test_numbers": ["1", "2", "3", "4"]}, "bad_test_numbers"), ("settings", {"test_numbers": ["123"]}, "bad_test_numbers"),
    ("settings", {"numbers": ["not-a-uuid"]}, "bad_numbers"), ("settings", {"handoff_webhook": {"url": "http://x.example"}}, "bad_handoff_webhook"),
])
def test_what_an_agent_is_told_is_checked_in_words(world, field, value, reason):
    aid = world.agent()
    with pytest.raises(ai_agent.AIError) as why:
        update(world, aid, **{field: value})
    assert why.value.reason == reason and str(why.value)


def test_good_values_are_cleaned_and_kept(world):
    aid = world.agent()
    out = update(world, aid, profile={"business_name": " Shakti Builders ", "ground_rules": ["Never promise dates", "", "No discounts"]},
                 voice={"tone": "professional", "length": "medium", "language": "Hindi"},
                 skills=[{"name": "Book a visit", "kind": "booking", "when": "they want a site visit", "variables": [{"name": "City", "required": False}] and [{"name": "city"}]}],
                 settings={"test_numbers": ["+91 98765 43210", "919876543210"]})
    assert out["profile"]["business_name"] == "Shakti Builders" and out["profile"]["ground_rules"] == ["Never promise dates", "No discounts"]
    assert out["voice"] == {"tone": "professional", "length": "medium", "language": "hindi"}
    skill = out["skills"][0]
    assert skill["id"] == "book_a_visit" and skill["variables"][0] == {"name": "city", "label": "City", "description": "", "required": True, "attribute": "city"}
    assert out["settings"]["test_numbers"] == ["919876543210"]


def test_two_skills_with_the_same_name_get_their_own_ids(world):
    aid = world.agent()
    out = update(world, aid, skills=[{"name": "Help"}, {"name": "Help"}, {"name": "Help"}])
    assert [s["id"] for s in out["skills"]] == ["help", "help_2", "help_3"]


def test_a_header_with_a_secret_is_shown_hidden_and_kept_when_sent_back_hidden(world):
    aid = world.agent()
    action = {"name": "Order status", "description": "Look up an order", "url": "https://shop.example/orders/{{order}}",
              "headers": {"Authorization": "Bearer abc123456"}, "parameters": [{"name": "order"}]}
    update(world, aid, actions=[action], settings={"handoff_webhook": {"url": "https://crm.example/hook", "headers": {"X-Key": "secret-key-9"}}})
    shown = in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))
    assert shown["actions"][0]["headers"]["Authorization"] == "Bea••••" and shown["settings"]["handoff_webhook"]["headers"]["X-Key"] == "sec••••"
    update(world, aid, actions=shown["actions"], settings=shown["settings"])                      # sent back as shown
    row = agent_row(world)
    assert row["actions"][0]["headers"]["Authorization"] == "Bearer abc123456" and row["settings"]["handoff_webhook"]["headers"]["X-Key"] == "secret-key-9"
    shown["actions"][0]["headers"]["Authorization"] = "Bearer changed"
    update(world, aid, actions=shown["actions"])
    assert agent_row(world)["actions"][0]["headers"]["Authorization"] == "Bearer changed"


# ── the prompt ───────────────────────────────────────────────────────────────

def test_the_prompt_carries_the_profile_the_voice_the_skills_and_the_actions(world):
    aid = world.agent(instructions="Never discuss rivals.")
    update(world, aid, profile={"business_name": "Shakti Builders", "description": "We build boundary walls in Vadodara.", "website": "https://shakti.example",
                                "greeting": "Namaste! How can we help?", "ground_rules": ["Never promise a date"]},
           voice={"tone": "empathetic", "length": "short", "language": "gujarati"},
           skills=[{"name": "Quote", "kind": "lead", "when": "they ask for a price", "variables": [{"name": "city", "label": "City", "description": "where the project is"}]}],
           actions=[{"name": "Order status", "description": "Look up an order", "url": "https://shop.example/o/{{order}}", "parameters": [{"name": "order"}]}])
    publish(world, aid)
    world.llm.say("Namaste.")
    world.say("hi", "wamid.PR1")
    system = system_of(world.llm)
    for fragment in ("Shakti Builders", "boundary walls", "https://shakti.example", "Namaste! How can we help?", "Never promise a date", "empathetic",
                     "Always reply in Gujarati", "Never discuss rivals.", "Quote", "City [city]", "Order status [order_status]", "Needs: order", '"collected"', '"action"'):
        assert fragment in system, fragment


def test_a_plain_agent_reads_exactly_as_it_always_did():
    system = ai_agent.build_messages({"name": "Aria", "instructions": "Speak warmly."}, [], [], "hi")[0]["content"]
    assert system.startswith("You are Aria, a WhatsApp assistant for a business.\nSpeak warmly.\n\nRules:") and system.endswith("(nothing relevant was found)")


# ── details a skill collects ─────────────────────────────────────────────────

def lead_skill(**done):
    return [{"name": "Take a quote request", "kind": "lead", "when": "they want a quote", "on_complete": done,
             "variables": [{"name": "city", "label": "City", "attribute": "City"}, {"name": "work", "label": "Kind of work", "attribute": "Work"}]}]


def test_what_the_customer_tells_the_agent_is_kept_on_the_contact_and_asked_for_no_more(world):
    aid = world.agent()
    update(world, aid, skills=lead_skill())
    publish(world, aid)
    say_json(world.llm, reply="Which city is the project in?", handoff=False, collected={"work": "boundary wall"})
    world.say("I want a quote for a boundary wall", "wamid.S1")
    assert world.db.one("select value from contact_attributes where key = 'Work'") == "boundary wall"
    say_json(world.llm, reply="Thanks!", handoff=False, collected={"city": "Surat", "nonsense": "ignored", "work": ""})
    world.say("Surat", "wamid.S2")
    assert world.db.one("select value from contact_attributes where key = 'City'") == "Surat" and world.db.one("select count(*) from contact_attributes") == 2
    assert "Kind of work: boundary wall" in system_of(world.llm)                                  # the second turn was told what is already known


def test_when_every_needed_detail_is_in_the_contact_is_tagged_and_the_chat_can_go_to_a_person(world):
    aid = world.agent()
    update(world, aid, skills=lead_skill(tag="quote-asked", handoff=True, reason="quote details complete"))
    publish(world, aid)
    say_json(world.llm, reply="Thanks, noted.", handoff=False, collected={"city": "Surat", "work": "house"})
    world.say("house in Surat", "wamid.S3")
    assert world.db.one("select tags from contacts") == ["quote-asked"]
    assert world.db.one("select status from conversations") == "requesting"
    assert sorted(world.out()) == sorted(["Thanks, noted.", ai_agent.HANDOFF_TEXT])             # both in one transaction: no order between them
    assert world.db.one("select count(*) from ai_events where kind = 'skill'") == 1


def test_a_detail_the_attribute_refuses_is_skipped_not_fatal(world):
    aid = world.agent()
    update(world, aid, skills=[{"name": "Budget", "variables": [{"name": "budget", "attribute": "Budget"}]}])
    publish(world, aid)
    world.db.q("insert into attribute_defs (tenant_id, key, type) values (%s, 'Budget', 'number')", (world.tid,))
    say_json(world.llm, reply="Noted.", handoff=False, collected={"budget": "plenty"})
    world.say("plenty", "wamid.S4")
    assert world.out() == ["Noted."] and world.db.one("select count(*) from contact_attributes") == 0


def test_trying_the_agent_shows_what_it_would_collect_but_saves_nothing(world):
    aid = world.agent()
    update(world, aid, skills=lead_skill())
    say_json(world.llm, reply="Which city?", handoff=False, collected={"work": "house"})
    out = in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "a house please"}]))
    assert out["collected"] == {"work": "house"} and world.db.one("select count(*) from contact_attributes") == 0


# ── things the agent can do ──────────────────────────────────────────────────

class Web:
    def __init__(self, status=200, body="{}"):
        self.calls, self.status, self.body = [], status, body

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        return self.status, self.body


ORDER_ACTION = {"name": "Order status", "description": "Look up an order by its number", "url": "https://shop.example/orders/{{order}}?c={{mobile}}",
                "headers": {"X-Key": "k-{{order}}"}, "parameters": [{"name": "order"}], "response_path": "order.status"}


def test_the_agent_can_look_something_up_and_tell_the_customer_what_came_back(world):
    aid = world.agent()
    update(world, aid, actions=[ORDER_ACTION])
    publish(world, aid)
    web = Web(body=json.dumps({"order": {"status": "packed, ships tomorrow"}}))
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=web)
    say_json(world.llm, reply="", action={"id": "order_status", "args": {"order": "A 17"}})
    say_json(world.llm, reply="Your order is packed and ships tomorrow.", handoff=False)
    world.say("where is my order A 17", "wamid.A1")
    assert world.out() == ["Your order is packed and ships tomorrow."]
    method, url, headers, _ = web.calls[0]
    assert method == "GET" and url.startswith("https://shop.example/orders/A%2017?c=") and headers == {"X-Key": "k-A 17"}
    follow = world.llm.requests[1]["body"]["messages"]
    assert "ACTION RESULT" in follow[-1]["content"] and "packed, ships tomorrow" in follow[-1]["content"]
    assert [r["kind"] for r in world.db.q("select kind from ai_usage order by at")].count("reply") == 1          # two model calls, one reply
    assert world.db.one("select tokens_in from ai_usage where kind = 'reply'") == 200
    assert world.db.one("select reason from ai_events where kind = 'action'") == "Order status worked"


def test_an_action_that_fails_is_told_to_the_model_which_hands_over(world):
    aid = world.agent()
    update(world, aid, actions=[ORDER_ACTION])
    publish(world, aid)
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=Web(status=500, body="boom"))
    say_json(world.llm, reply="", action={"id": "order_status", "args": {"order": "A17"}})
    say_json(world.llm, reply="Let me ask a colleague.", handoff=True, reason="the order system is down")
    world.say("where is my order", "wamid.A2")
    assert "failed (it answered 500)" in world.llm.requests[1]["body"]["messages"][-1]["content"]
    assert world.db.one("select status from conversations") == "requesting" and world.db.one("select reason from ai_events where kind = 'action'") == "Order status failed"


def test_an_action_without_the_details_it_needs_is_not_called(world):
    web = Web()
    result = ai_actions.run(ai_profile.clean_actions([ORDER_ACTION])[0], {}, fetch=web)
    assert result["ran"] is False and "missing: order" in result["error"] and web.calls == []


def test_the_model_cannot_call_an_address_the_owner_did_not_define(world):
    aid = world.agent()
    update(world, aid, actions=[ORDER_ACTION])
    publish(world, aid)
    web = Web()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=web)
    say_json(world.llm, reply="Ok then", action={"id": "delete_everything", "args": {}})
    world.say("hello", "wamid.A3")
    assert web.calls == [] and world.out() == ["Ok then"] and len(world.llm.requests) == 1


def test_a_switched_off_action_is_not_offered_or_run(world):
    aid = world.agent()
    update(world, aid, actions=[{**ORDER_ACTION, "enabled": False}])
    publish(world, aid)
    world.llm.say("hi")
    world.say("hi", "wamid.A4")
    assert "Order status" not in system_of(world.llm)


def test_trying_the_agent_runs_only_actions_that_read(world):
    aid = world.agent()
    book = {"name": "Book a visit", "description": "Book a slot", "method": "POST", "url": "https://shop.example/book", "body": '{"who": "{{name}}"}', "parameters": []}
    update(world, aid, actions=[ORDER_ACTION, book])
    web = Web(body=json.dumps({"order": {"status": "ok"}}))
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=web)
    say_json(world.llm, reply="", action={"id": "book_a_visit", "args": {}})
    say_json(world.llm, reply="I would book it.", handoff=False)
    out = in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "book me"}]))
    assert web.calls == [] and out["ran_action"]["ran"] is False and "not run in a test" in world.llm.requests[1]["body"]["messages"][-1]["content"]
    say_json(world.llm, reply="", action={"id": "order_status", "args": {"order": "A1"}})
    say_json(world.llm, reply="It is ok.", handoff=False)
    out = in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "my order A1"}]))
    assert len(web.calls) == 1 and out["ran_action"]["ok"] is True


def test_a_post_action_fills_its_body_and_the_owner_can_try_it_once(world):
    book = ai_profile.clean_actions([{"name": "Book", "description": "d", "method": "POST", "url": "https://s.example/b", "body": '{"who": "{{name}}", "day": "{{day}}"}',
                                      "parameters": [{"name": "day"}]}])[0]
    web = Web(body="{}")
    out = ai_actions.run(book, {"day": "Sat"}, fetch=web, contact={"name": "Asha", "phone": "9198"})
    assert out["ok"] and web.calls[0][3] == '{"who": "Asha", "day": "Sat"}'


def test_the_owner_can_try_an_action_over_the_api_with_a_hidden_header_kept(migrated, world):
    aid = world.agent()
    update(world, aid, actions=[ORDER_ACTION])
    web = Web(body=json.dumps({"order": {"status": "packed"}}))
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=web)
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="t", graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}))
    client = TestClient(app, follow_redirects=False)
    shown = in_db(world, lambda cur: ai_agent.get(cur, world.tid, aid))["actions"][0]
    r = client.post("/internal/ai/actions/test", json={"tenant_id": str(world.tid), "agent_id": str(aid), "action": shown, "args": {"order": "A17"}}, headers={"X-Internal-Token": "t"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "ran": True, "status": 200, "text": "packed", "error": ""} and web.calls[0][2] == {"X-Key": "k-A17"}


# ── settings: what is said, and who is told ──────────────────────────────────

def test_the_owners_words_are_used_when_the_agent_hands_over_or_cannot_answer(world):
    aid = world.agent()
    update(world, aid, settings={"handoff_message": "One moment, a human is coming.", "fallback_text": "Sorry, I cannot read that. A person will help."})
    publish(world, aid)
    world.say("I want to speak to someone please", "wamid.H1")
    assert world.out() == ["One moment, a human is coming."]
    world.db.q("update conversations set status = 'active', bot_paused = false")
    H.deliver(world.db, P.inbound(P.image("m1", "photo", "wamid.H2", str(int(time.time()) + 30))))
    H.work(world.db)
    assert world.out()[-1] == "Sorry, I cannot read that. A person will help."


def test_the_owners_fallback_replaces_the_default_when_the_model_is_down(world):
    aid = world.agent()
    update(world, aid, settings={"fallback_text": "We are having trouble. A person will reply."})
    publish(world, aid)
    world.llm.status = 500
    world.say("hello", "wamid.H3")
    assert world.out() == ["We are having trouble. A person will reply."]
    assert world.db.one("select count(*) from ai_events where kind = 'failed'") == 1


def test_every_handover_is_told_to_the_owners_system_and_a_dead_hook_stops_nothing(world):
    aid = world.agent()
    update(world, aid, settings={"handoff_webhook": {"url": "https://crm.example/hook", "headers": {"X-Key": "k1"}}})
    publish(world, aid)
    web = Web()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=web)
    world.say("I want to speak to someone please", "wamid.W1")
    method, url, headers, body = web.calls[0]
    payload = json.loads(body)
    assert (method, url) == ("POST", "https://crm.example/hook") and headers["X-Key"] == "k1" and headers["Content-Type"] == "application/json"
    assert payload["event"] == "ai.handoff" and payload["reason"] == "the customer asked for a person" and payload["contact"]["phone"] and payload["messages"]
    assert world.db.one("select status from conversations") == "requesting"

    def down(*a):
        raise flow_engine.FlowRuntimeError("the request failed")
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=down)
    world.db.q("update conversations set status = 'active', bot_paused = false")
    world.say("I need a human", "wamid.W2")
    assert world.db.one("select status from conversations") == "requesting"


# ── what it could not answer, and what it did ────────────────────────────────

def test_a_question_the_agent_could_not_answer_is_kept_and_counted_each_time_it_is_asked(world):
    aid = world.agent()
    for n, text in enumerate(("What is the price per sqft?", "what is the price per SQFT", "What is the price per sqft?!")):
        say_json(world.llm, reply="Let me ask a colleague.", handoff=True, reason="not in the knowledge", unknown=True)
        world.say(text, "wamid.G%d" % n)
        world.db.q("update conversations set status = 'active', bot_paused = false")
    gaps = in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid))
    assert len(gaps) == 1 and gaps[0]["times"] == 3 and gaps[0]["question"] == "What is the price per sqft?" and gaps[0]["reason"] == "not in the knowledge"


def test_a_customer_asking_for_a_person_is_not_a_gap_but_a_model_that_found_nothing_is(world):
    aid = world.agent()
    world.say("I want to speak to someone please", "wamid.G5")
    assert in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid)) == []
    world.db.q("update conversations set status = 'active', bot_paused = false")
    say_json(world.llm, reply="I will ask a colleague.", handoff=True, reason="no answer")
    world.say("do you do solar panels", "wamid.G6")
    assert [g["question"] for g in in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid))] == ["do you do solar panels"]


def test_answering_a_gap_teaches_the_agent_and_the_question_leaves_the_list(world):
    aid = world.agent()
    in_db(world, lambda cur: ai_insights.record_gap(cur, world.tid, aid, "Do you deliver to Surat?", "not in the knowledge"))
    gid = in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid))[0]["id"]
    in_db(world, lambda cur: ai_insights.answer_gap(cur, world.tid, gid, "Yes, within 300 km of Vadodara."))
    assert in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid)) == []
    assert [g["question"] for g in in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid, "answered"))] == ["Do you deliver to Surat?"]
    hits = in_db(world, lambda cur: ai_agent.search(cur, world.tid, aid, "deliver to surat"))
    assert hits and "300 km" in hits[0]["text"]
    in_db(world, lambda cur: ai_insights.record_gap(cur, world.tid, aid, "do you deliver to surat", ""))                      # asked again later: open again
    assert len(in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid))) == 1


def test_a_gap_can_be_ignored_and_one_workspace_cannot_touch_anothers(world, migrated):
    aid = world.agent()
    in_db(world, lambda cur: ai_insights.record_gap(cur, world.tid, aid, "what is your wifi password", ""))
    gid = in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid))[0]["id"]
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    with pytest.raises(ai_agent.AIError):
        in_db(world, lambda cur: ai_insights.ignore_gap(cur, other, gid))
    with pytest.raises(ai_agent.AIError):
        in_db(world, lambda cur: ai_insights.gaps(cur, other, aid))
    in_db(world, lambda cur: ai_insights.ignore_gap(cur, world.tid, gid))
    assert in_db(world, lambda cur: ai_insights.gaps(cur, world.tid, aid)) == []


def test_the_numbers_count_chats_replies_handovers_and_reasons(world):
    aid = world.agent()
    world.llm.say("Mon-Sat.")
    world.say("what days", "wamid.AN1")
    say_json(world.llm, reply="Let me ask.", handoff=True, reason="pricing is not in the knowledge", unknown=True)
    world.say("what is the price", "wamid.AN2")
    out = in_db(world, lambda cur: ai_insights.analytics(cur, world.tid, aid))
    t = out["totals"]
    assert (t["replies"], t["chats"], t["handed_over"], t["settled"]) == (1, 1, 1, 0) and t["handover_rate"] == 100.0
    assert out["reasons"] == [{"reason": "pricing is not in the knowledge", "count": 1}] and len(out["days"]) == 1 and out["gaps"][0]["question"] == "what is the price"
    assert t["tokens_in"] == 220


def test_a_chat_the_agent_settled_alone_counts_as_settled(world):
    aid = world.agent()
    world.llm.say("Mon-Sat.")
    world.say("what days", "wamid.AN3")
    t = in_db(world, lambda cur: ai_insights.analytics(cur, world.tid, aid))["totals"]
    assert (t["chats"], t["handed_over"], t["settled"], t["settled_rate"]) == (1, 0, 1, 100.0)


# ── reading a whole website ──────────────────────────────────────────────────

SITE = {
    "https://shakti.example/": '<html><head><title>Shakti Builders</title></head><body><p>We build boundary walls and houses across Vadodara and Surat.</p>'
                               '<a href="/services">Services</a><a href="/contact#top">Contact</a><a href="https://other.example/x">Other</a><a href="/brochure.pdf">PDF</a>'
                               '<a href="mailto:a@b.c">Mail</a><a href="https://www.shakti.example/faq">FAQ</a></body></html>',
    "https://shakti.example/services": '<html><head><title>Services</title></head><body><p>Boundary walls, house construction and renovation work.</p><a href="/deep">Deep</a></body></html>',
    "https://shakti.example/contact": "<html><head><title>Contact us</title></head><body><p>Our office is at Alkapuri, Vadodara. Call us any day.</p></body></html>",
    "https://www.shakti.example/faq": "<html><head><title>FAQ</title></head><body><p>Yes we deliver within three hundred kilometres of Vadodara.</p></body></html>",
    "https://shakti.example/deep": "<html><head><title>Deep</title></head><body><p>A page two links away from the start of the website.</p><a href='/deeper'>x</a></body></html>",
    "https://shakti.example/deeper": "<html><head><title>Too deep</title></head><body><p>A page three links away from the start of the website.</p></body></html>",
}


def site_fetch(pages=SITE, log=None):
    def fetch(method, url, headers, body):
        if log is not None:
            log.append(url)
        if url not in pages:
            return 404, "no"
        return 200, pages[url]
    return fetch


def test_the_links_of_a_page_are_the_pages_of_the_same_site_without_fragments_files_or_mail():
    links = ai_knowledge.same_site_links(SITE["https://shakti.example/"], "https://shakti.example/", "https://shakti.example/")
    assert links == ["https://shakti.example/services", "https://shakti.example/contact", "https://www.shakti.example/faq"]


def test_a_website_is_read_in_the_background_and_every_page_becomes_a_note_in_one_group(world):
    aid = world.agent()
    log = []
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch(log=log))
    started = in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/", max_pages=10))
    row = world.db.q("select status, title from ai_knowledge")[0]
    assert row["status"] == "processing" and log == []
    H.work(world.db)
    notes = in_db(world, lambda cur: ai_agent.list_knowledge(cur, world.tid, aid))
    assert {n["title"] for n in notes} == {"Shakti Builders", "Services", "Contact us", "FAQ", "Deep"} and all(n["status"] == "ready" for n in notes)
    assert len({n["group_id"] for n in notes}) == 1 and notes[0]["group_id"] == started["group_id"]
    assert "https://shakti.example/deeper" not in log                                          # two links deep and no further
    hits = in_db(world, lambda cur: ai_agent.search(cur, world.tid, aid, "office alkapuri"))
    assert hits and "Alkapuri" in hits[0]["text"]


def test_the_page_limit_stops_a_crawl(world):
    aid = world.agent()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch())
    in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/", max_pages=2))
    H.work(world.db)
    assert world.db.one("select count(*) from ai_knowledge where status = 'ready'") == 2


def test_a_website_that_cannot_be_read_shows_why_and_a_bad_address_is_refused(world):
    aid = world.agent()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (404, "gone"))
    in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/", max_pages=3))
    H.work(world.db)
    row = world.db.q("select status, error from ai_knowledge")[0]
    assert row["status"] == "failed" and "error (404)" in row["error"]
    with pytest.raises(ai_agent.AIError) as why:
        in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "http://shakti.example/"))
    assert why.value.reason == "bad_url"


def test_one_page_that_fails_does_not_lose_the_rest(world):
    aid = world.agent()
    pages = dict(SITE)
    del pages["https://shakti.example/services"]
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch(pages))
    in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/"))
    H.work(world.db)
    assert {n["title"] for n in in_db(world, lambda cur: ai_agent.list_knowledge(cur, world.tid, aid))} == {"Shakti Builders", "Contact us", "FAQ"}


def test_a_note_removed_while_its_site_waited_is_left_alone(world):
    aid = world.agent()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch())
    started = in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/"))
    in_db(world, lambda cur: ai_agent.delete_knowledge(cur, world.tid, started["knowledge_id"]))
    H.work(world.db)
    assert world.db.one("select count(*) from ai_knowledge") == 0 and world.db.one("select count(*) from wa_jobs where state = 'dead'") == 0


def test_a_page_can_be_read_again_and_keeps_its_old_text_if_it_cannot(world):
    aid = world.agent()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (200, "<title>Hours</title><p>We open at ten every morning of the week.</p>"))
    kid = in_db(world, lambda cur: ai_agent.add_url(cur, world.tid, aid, "https://shakti.example/hours", "Hours"))
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (200, "<title>Hours</title><p>We now open at nine every morning of the week.</p>"))
    in_db(world, lambda cur: ai_knowledge.refresh(cur, world.tid, kid))
    assert world.db.one("select status from ai_knowledge") == "processing"
    H.work(world.db)
    row = world.db.q("select status, body, title from ai_knowledge")[0]
    assert row["status"] == "ready" and "nine every morning" in row["body"] and row["title"] == "Hours"
    assert world.db.one("select count(*) from ai_chunks where body like '%%at ten%%'") == 0
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (500, "down"))
    in_db(world, lambda cur: ai_knowledge.refresh(cur, world.tid, kid))
    H.work(world.db)
    row = world.db.q("select status, error, body from ai_knowledge")[0]
    assert row["status"] == "failed" and "error (500)" in row["error"] and "nine every morning" in row["body"]


def test_only_a_web_page_can_be_read_again(world):
    aid = world.agent()
    kid = world.learn(aid, "Hours", "Open nine to six.")
    with pytest.raises(ai_agent.AIError) as why:
        in_db(world, lambda cur: ai_knowledge.refresh(cur, world.tid, kid))
    assert why.value.reason == "not_a_page"


def test_a_note_still_being_read_is_not_searched(world):
    aid = world.agent()
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch())
    in_db(world, lambda cur: ai_knowledge.add_site(cur, world.tid, aid, "https://shakti.example/"))
    assert in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "shakti.example")) == []


# ── finding a note ───────────────────────────────────────────────────────────

class Meaning:
    """A stand-in for an embeddings service: a word list decides the direction of a text, so 'return' and 'send back' point the same way."""
    GROUPS = (("return", "refund", "send", "back", "exchange"), ("hours", "open", "close", "timing"), ("deliver", "delivery", "ship", "courier"))

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, data, headers):
        body = json.loads(data)
        self.calls.append(body["input"])
        rows = []
        for i, text in enumerate(body["input"]):
            low = text.lower()
            rows.append({"index": i, "embedding": [float(sum(low.count(w) for w in group)) for group in self.GROUPS] + [0.001]})
        return 200, json.dumps({"data": rows}).encode()


def test_the_embeddings_client_sends_batches_and_reads_the_vectors_in_order():
    meaning = Meaning()
    client = Embedder(api_key="k", base_url="https://e.example/v1", model="m", transport=meaning)
    vectors = client.embed(["return it", "open now"] + ["x"] * 70)
    assert len(vectors) == 72 and len(meaning.calls) == 2 and vectors[0][0] > 0 and vectors[1][1] > 0
    bad = Embedder(api_key="k", transport=lambda *a: (429, b'{"error": {"message": "slow down"}}'))
    with pytest.raises(LLMError) as why:
        bad.embed(["x"])
    assert "slow down" in str(why.value)
    with pytest.raises(LLMError):
        Embedder(api_key="k", transport=lambda *a: (200, b'{"data": []}')).embed(["x"])
    assert embeddings.from_env({}) is None and embeddings.from_env({"WA_EMBED_API_KEY": "k"}).model == "text-embedding-3-small"
    assert embeddings.cosine([1, 0], [1, 0]) == 1.0 and embeddings.cosine([1, 0], [0, 1]) == 0.0 and embeddings.cosine([0, 0], [1, 1]) == 0.0


def test_a_question_in_other_words_finds_the_note_by_meaning_once_the_notes_are_embedded(world):
    aid = world.agent()
    world.learn(aid, "Return policy", "Returns and refunds are accepted within seven days. An exchange is free.")
    world.learn(aid, "Opening hours", "We open at nine and close at six on all days of the week.")
    question = "Can I send the product back?"
    assert in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, question)) == []       # no word is shared with any note
    meaning = Meaning()
    ai_agent.configure(ai_agent.LLM_CLIENT, embedder=Embedder(api_key="k", transport=meaning))
    in_db(world, lambda cur: [ai_agent.index_for_meaning(cur, world.tid, k["id"]) for k in ai_agent.list_knowledge(cur, world.tid, aid)])
    H.work(world.db)
    assert world.db.one("select count(*) from ai_chunks where embedding is null") == 0
    hits = in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, question))
    assert [h["title"] for h in hits] == ["Return policy"]
    assert [s["embedded"] for s in in_db(world, lambda cur: ai_agent.list_knowledge(cur, world.tid, aid))] == [1, 1]


def test_new_notes_are_embedded_by_the_worker_not_inside_the_request(world):
    aid = world.agent()
    meaning = Meaning()
    ai_agent.configure(ai_agent.LLM_CLIENT, embedder=Embedder(api_key="k", transport=meaning))
    world.learn(aid, "Delivery", "We deliver by courier within three days.")
    assert meaning.calls == [] and world.db.one("select count(*) from wa_jobs where kind = 'ai_embed'") == 1
    H.work(world.db)
    assert len(meaning.calls) == 1 and world.db.one("select count(*) from ai_chunks where embedding is not null") == 1


def test_a_search_by_words_and_one_by_meaning_are_merged_with_the_note_both_found_first(world):
    aid = world.agent()
    world.learn(aid, "Return policy", "Returns and refunds are accepted within seven days.")
    world.learn(aid, "Delivery", "We deliver by courier. Returns are not collected from the doorstep.")
    ai_agent.configure(ai_agent.LLM_CLIENT, embedder=Embedder(api_key="k", transport=Meaning()))
    in_db(world, lambda cur: [ai_agent.index_for_meaning(cur, world.tid, k["id"]) for k in ai_agent.list_knowledge(cur, world.tid, aid)])
    H.work(world.db)
    hits = in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "can I return it for a refund"))
    assert hits[0]["title"] == "Return policy"


def test_an_embeddings_service_that_is_down_falls_back_to_words(world):
    aid = world.agent()
    world.learn(aid, "Hours", "We open at nine and close at six.")
    ai_agent.configure(ai_agent.LLM_CLIENT, embedder=Embedder(api_key="k", transport=lambda *a: (500, b"{}")))
    world.db.q("update ai_chunks set embedding = '{1,0,0,0}'")
    assert [h["title"] for h in in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "when do you open"))] == ["Hours"]


def test_when_words_find_nothing_the_model_is_asked_for_synonyms_and_they_are_searched(world):
    aid = world.agent()
    world.learn(aid, "Return policy", "Returns and refunds are accepted within seven days.")
    say_json(world.llm, keywords=["return", "refund", "exchange"])
    hits = in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "Can I send the product back?"))
    assert [h["title"] for h in hits] == ["Return policy"] and len(world.llm.requests) == 1
    assert world.db.one("select count(*) from ai_usage where kind = 'aux'") == 1 and world.db.one("select count(*) from ai_usage where kind = 'reply'") == 0


def test_the_model_is_not_asked_for_synonyms_when_words_found_something_or_there_is_nothing_to_search(world):
    aid = world.agent()
    assert in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "anything at all")) == [] and world.llm.requests == []
    world.learn(aid, "Hours", "We open at nine and close at six.")
    in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "when do you open"))
    assert world.llm.requests == []


def test_synonyms_that_are_not_json_or_a_model_that_fails_just_find_nothing(world):
    aid = world.agent()
    world.learn(aid, "Return policy", "Returns and refunds are accepted within seven days.")
    world.llm.say(raw="not json at all")
    assert in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "Can I send the product back?")) == []
    world.llm.status = 500
    assert in_db(world, lambda cur: ai_knowledge.retrieve(cur, world.tid, aid, "Can I send the product back?")) == []


def test_the_synonym_call_does_not_use_up_the_monthly_allowance(world):
    aid = world.agent()
    world.learn(aid, "Return policy", "Returns and refunds are accepted within seven days.")
    say_json(world.llm, keywords=["return"])
    say_json(world.llm, reply="Yes, within seven days.", handoff=False)
    world.say("Can I send the product back?", "wamid.R1")
    assert world.out() == ["Yes, within seven days."]
    use = in_db(world, lambda cur: ai_agent.usage(cur, world.tid))
    assert use["replies"] == 1


# ── drafting the profile from the business's own site ────────────────────────

def test_a_profile_is_drafted_from_the_site_and_cleaned_like_anything_typed():
    from prism_wa.llm import LLM
    sent = []

    def transport(method, url, data, headers):
        sent.append(json.loads(data))
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps({
            "business_name": "Shakti Builders", "description": "We build walls. " * 200, "greeting": "Namaste!", "ground_rules": ["Never promise a date", "", "r2", "r3", "r4", "r5", "r6"]})}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 60}}).encode()
    profile, tokens = ai_profile.draft_from_page(LLM(api_key="k", transport=transport), "Boundary walls in Vadodara. " * 300, "https://shakti.example")
    assert profile["business_name"] == "Shakti Builders" and profile["website"] == "https://shakti.example" and len(profile["description"]) <= 1500
    assert profile["ground_rules"] == ["Never promise a date", "r2", "r3", "r4"] and tokens == {"tokens_in": 400, "tokens_out": 60}
    assert len(sent[0]["messages"][1]["content"]) < 6200
    ai_profile.clean_profile(profile)


def test_the_page_for_a_profile_must_be_https_and_readable():
    with pytest.raises(ai_profile.ProfileError):
        ai_profile.fetch_page_text(lambda *a: (200, "<p>hi</p>"), "http://x.example")
    with pytest.raises(ai_profile.ProfileError) as why:
        ai_profile.fetch_page_text(lambda *a: (404, ""), "https://x.example")
    assert "error (404)" in str(why.value)
    assert "Boundary" in ai_profile.fetch_page_text(lambda *a: (200, "<p>Boundary walls built across Vadodara and Surat every year.</p>"), "https://x.example")


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.fixture
def api(migrated, world):
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="t", graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}))
    client = TestClient(app, follow_redirects=False)

    class A:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(world.tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "t"})
    A.world = world
    return A


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_the_routes_publish_pause_and_report_the_state_of_an_agent(api):
    aid = str(api.world.agent())
    got = ok(api.post("/internal/ai/agents/get", agent_id=aid))["agent"]
    assert got["status"] == "published" and got["unpublished_changes"] is False and got["profile"] == {}
    ok(api.post("/internal/ai/agents/update", agent_id=aid, changes={"voice": {"tone": "casual"}}))
    assert ok(api.post("/internal/ai/agents/get", agent_id=aid))["agent"]["unpublished_changes"] is True
    assert ok(api.post("/internal/ai/agents/publish", agent_id=aid))["version"] == 2
    ok(api.post("/internal/ai/agents/unpublish", agent_id=aid))
    assert ok(api.post("/internal/ai/agents/list"))["agents"][0]["status"] == "paused"
    bad = api.post("/internal/ai/agents/update", agent_id=aid, changes={"voice": {"tone": "angry"}})
    assert bad.status_code == 409 and bad.json()["error"] == "bad_tone"


def test_the_routes_crawl_a_site_list_its_pages_and_read_one_again(api):
    aid = str(api.world.agent())
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=site_fetch())
    out = ok(api.post("/internal/ai/knowledge/add_site", agent_id=aid, url="https://shakti.example/", max_pages=3))
    assert out["knowledge_id"] and out["group_id"]
    assert ok(api.post("/internal/ai/knowledge/list", agent_id=aid))["knowledge"][0]["status"] == "processing"
    H.work(api.world.db)
    listed = ok(api.post("/internal/ai/knowledge/list", agent_id=aid))["knowledge"]
    assert len(listed) == 3 and {k["status"] for k in listed} == {"ready"}
    ok(api.post("/internal/ai/knowledge/refresh", knowledge_id=listed[-1]["id"]))
    assert api.post("/internal/ai/knowledge/add_site", agent_id=aid, url="http://insecure.example").status_code == 409


def test_the_routes_list_answer_and_ignore_what_the_agent_could_not_answer(api):
    w = api.world
    aid = str(w.agent())
    in_db(w, lambda cur: [ai_insights.record_gap(cur, w.tid, aid, q, "") for q in ("what is the price", "do you do solar", "what is the price")])
    gaps = ok(api.post("/internal/ai/gaps/list", agent_id=aid))["gaps"]
    assert [(g["question"], g["times"]) for g in gaps] == [("what is the price", 2), ("do you do solar", 1)]
    assert ok(api.post("/internal/ai/gaps/answer", gap_id=gaps[0]["id"], answer="Rs 1,800 per sq ft."))["knowledge_id"]
    ok(api.post("/internal/ai/gaps/ignore", gap_id=gaps[1]["id"]))
    assert ok(api.post("/internal/ai/gaps/list", agent_id=aid))["gaps"] == []
    assert [g["status"] for g in ok(api.post("/internal/ai/gaps/list", agent_id=aid, status="answered"))["gaps"]] == ["answered"]
    assert api.post("/internal/ai/gaps/answer", gap_id=gaps[0]["id"], answer="").status_code in (400, 409)


def test_the_route_for_numbers_takes_a_range(api):
    w = api.world
    aid = str(w.agent())
    w.llm.say("Mon-Sat.")
    w.say("what days", "wamid.RT1")
    out = ok(api.post("/internal/ai/analytics", agent_id=aid, since="2020-01-01T00:00:00Z"))
    assert out["totals"]["replies"] == 1 and out["range"]["since"].startswith("2020-01-01")
    assert ok(api.post("/internal/ai/analytics", agent_id=aid, since="2020-01-01T00:00:00Z", until="2020-02-01T00:00:00Z"))["totals"]["replies"] == 0
    assert api.post("/internal/ai/analytics", agent_id=aid, since="yesterday-ish").status_code == 400


def test_a_profile_is_drafted_over_the_route_or_refused_when_there_is_no_model(api):
    w = api.world
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (200, "<title>Shakti</title><p>We build boundary walls and houses across Vadodara and Surat.</p>"))
    say_json(w.llm, business_name="Shakti Builders", description="We build walls.", greeting="Namaste!", ground_rules=[])
    out = ok(api.post("/internal/ai/agents/draft_profile", url="https://shakti.example"))["profile"]
    assert out["business_name"] == "Shakti Builders" and out["website"] == "https://shakti.example"
    assert w.db.one("select count(*) from ai_usage where kind = 'aux' and agent_id is null") == 1
    ai_agent.configure(None)
    assert api.post("/internal/ai/agents/draft_profile", url="https://shakti.example").json()["error"] == "ai_not_configured"
