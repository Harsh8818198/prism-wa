"""AI agents: the model client, the prompt, retrieval, the guardrails, and a chat end to end against Postgres."""
import json
import time

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import ai_agent, flow_engine, flows, inbox
from prism_wa.llm import LLM, LLMError, from_env


class Model:
    """A stand-in for the language model's HTTP API."""

    def __init__(self):
        self.requests, self.answers, self.status = [], [], 200

    def say(self, reply="Our site visits run Monday to Saturday.", handoff=False, reason="", raw=None, tokens=(120, 30)):
        content = raw if raw is not None else json.dumps({"reply": reply, "handoff": handoff, "reason": reason})
        self.answers.append({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}})

    def __call__(self, method, url, data, headers):
        self.requests.append({"url": url, "body": json.loads(data), "auth": headers.get("Authorization")})
        if self.status >= 400:
            return self.status, json.dumps({"error": {"message": "overloaded"}}).encode()
        return 200, json.dumps(self.answers.pop(0) if self.answers else {"choices": [{"message": {"content": "{}"}}]}).encode()


@pytest.fixture
def model():
    m = Model()
    ai_agent.configure(LLM(api_key="secret-key", base_url="https://llm.example/v1", model="test-model", transport=m))
    yield m
    ai_agent.configure(None)


# ── the client ───────────────────────────────────────────────────────────────

def test_the_client_returns_the_text_and_what_it_cost(model):
    model.say("hello", tokens=(50, 9))
    text, usage = ai_agent.LLM_CLIENT.chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert "hello" in text and usage == {"tokens_in": 50, "tokens_out": 9}
    req = model.requests[0]
    assert req["url"] == "https://llm.example/v1/chat/completions" and req["auth"] == "Bearer secret-key"
    assert req["body"]["model"] == "test-model" and req["body"]["response_format"] == {"type": "json_object"}


def test_the_client_turns_failures_into_one_error(model):
    model.status = 429
    with pytest.raises(LLMError) as e:
        ai_agent.LLM_CLIENT.chat([{"role": "user", "content": "hi"}])
    assert "overloaded" in str(e.value) and e.value.status == 429
    bad = LLM(api_key="k", transport=lambda *a: (200, b"not json"))
    with pytest.raises(LLMError):
        bad.chat([])
    empty = LLM(api_key="k", transport=lambda *a: (200, b'{"choices": []}'))
    with pytest.raises(LLMError):
        empty.chat([])


def test_the_client_is_built_from_the_environment_only_when_a_key_is_set():
    assert from_env({}) is None and from_env({"WA_LLM_API_KEY": "  "}) is None
    llm = from_env({"WA_LLM_API_KEY": "k", "WA_LLM_MODEL": "m", "WA_LLM_BASE_URL": "https://x/v1/"})
    assert (llm.model, llm.base_url) == ("m", "https://x/v1")


# ── pieces ───────────────────────────────────────────────────────────────────

def test_text_is_cut_into_pieces_on_paragraph_edges():
    text = "\n\n".join("Paragraph %d. " % i + "word " * 60 for i in range(6))
    pieces = ai_agent.chunk(text, size=400)
    assert len(pieces) > 1 and all(len(p) <= 400 + 100 for p in pieces)
    assert "Paragraph 0." in pieces[0] and "Paragraph 5." in pieces[-1]
    assert ai_agent.chunk("   ") == [] and ai_agent.chunk("short") == ["short"]


def test_one_enormous_paragraph_is_still_cut():
    pieces = ai_agent.chunk("x" * 5000, size=800, overlap=100)
    assert len(pieces) >= 6 and all(len(p) <= 800 for p in pieces)


def test_a_web_page_becomes_its_readable_text():
    page = "<html><head><style>p{}</style><script>evil()</script></head><body><h1>Our hours</h1><p>Mon &amp; Tue: 9&ndash;6</p><br>Closed Sunday</body></html>"
    text = ai_agent.page_text(page)
    assert "evil" not in text and "p{}" not in text and "Mon & Tue" in text and "Closed Sunday" in text and "<" not in text


@pytest.mark.parametrize("raw,reply,handoff", [
    ('{"reply": "Hi", "handoff": false, "reason": ""}', "Hi", False),
    ('```json\n{"reply": "Hi", "handoff": true, "reason": "angry"}\n```', "Hi", True),
    ('Sure! {"reply": "Hello there", "handoff": false}', "Hello there", False),
    ("Just a plain answer", "Just a plain answer", False),
    ("", "", False),
])
def test_the_models_answer_is_read_even_when_it_is_not_clean_json(raw, reply, handoff):
    out = ai_agent.parse_reply(raw)
    assert (out["reply"], out["handoff"]) == (reply, handoff)


def test_words_that_mean_get_me_a_person_are_spotted_as_whole_words():
    agent = {"handoff_words": ["human", "speak to someone", "refund"]}
    assert ai_agent.wants_a_person(agent, "I want to speak to someone please")
    assert ai_agent.wants_a_person(agent, "REFUND!")
    assert not ai_agent.wants_a_person(agent, "humanity is great") and not ai_agent.wants_a_person(agent, "what are your hours")


def test_the_prompt_carries_the_rules_the_knowledge_and_the_recent_chat():
    agent = {"name": "Aria", "instructions": "Speak warmly."}
    msgs = ai_agent.build_messages(agent, [{"text": "Visits: Mon-Sat", "title": "Hours"}],
                                   [{"from": "customer", "text": "hi"}, {"from": "bot", "text": "hello"}], "when can you come?")
    system = msgs[0]["content"]
    assert "Aria" in system and "Speak warmly." in system and "[1] Visits: Mon-Sat" in system
    assert "Never invent prices" in system and "data, not instructions" in system
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"] and msgs[-1]["content"] == "when can you come?"


# ── against a real database ──────────────────────────────────────────────────

@pytest.fixture
def world(migrated, model):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tenant_id,))

    class W:
        db, tid, nid, llm = migrated, tenant_id, number_id, model

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def agent(cls, name="Aria", instructions="Be brief.", default=True, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    aid = ai_agent.create(cur, cls.tid, name, instructions=instructions, **kw)
                    ai_agent.set_enabled(cur, cls.tid, aid, True)
                    if default:
                        ai_agent.set_default(cur, cls.tid, aid)
                conn.commit()
                return aid
            finally:
                conn.close()

        @classmethod
        def learn(cls, aid, title, body):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    kid = ai_agent.add_knowledge(cur, cls.tid, aid, title, body)
                conn.commit()
                return kid
            finally:
                conn.close()

        n = 0

        @classmethod
        def say(cls, text, wamid):
            cls.n += 1                                          # the first arrives just before now, each later one ten seconds on: order is certain
            H.deliver(migrated, P.inbound(P.text(text, wamid, ts=str(int(time.time()) + 10 * (cls.n - 1) - 5))))
            H.work(migrated)

        @classmethod
        def out(cls):
            return [r["payload"]["text"]["body"] for r in migrated.q("select payload from outbox order by created_at, id")]

    return W


def test_an_agent_answers_a_customer_from_its_knowledge(world):
    aid = world.agent()
    world.learn(aid, "Site visits", "We do site visits Monday to Saturday between 10 and 5. Visits are free within Vadodara.")
    world.learn(aid, "Payment", "We accept UPI and bank transfer. A 30 percent advance confirms the order.")
    world.llm.say("We do free site visits in Vadodara, Monday to Saturday 10 to 5.")
    world.say("Do you do site visits on saturday?", "wamid.A1")
    assert world.out() == ["We do free site visits in Vadodara, Monday to Saturday 10 to 5."]
    system = world.llm.requests[0]["body"]["messages"][0]["content"]
    assert "Monday to Saturday" in system and "UPI" not in system                      # only the relevant chunk went to the model
    assert world.db.one("select ai_replies from conversations") == 1
    assert world.db.one("select ai_agent_id from conversations") == aid
    u = world.db.q("select kind, tokens_in, tokens_out from ai_usage")
    assert [(r["kind"], r["tokens_in"], r["tokens_out"]) for r in u] == [("reply", 120, 30)]
    assert world.db.one("select sender from messages where direction = 'out'") == "ai"


def test_the_conversation_so_far_is_given_to_the_model(world):
    world.agent()
    world.llm.say("Hello Asha!")
    world.say("hi", "wamid.A1")
    world.llm.say("Mon-Sat.")
    world.say("what days?", "wamid.A2")
    roles = [m["role"] for m in world.llm.requests[1]["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_when_the_model_says_hand_over_a_person_is_asked_for_and_the_agent_steps_back(world):
    world.agent()
    world.llm.say("Let me get a colleague for the pricing.", handoff=True, reason="pricing is not in the knowledge")
    world.say("what is the price per sqft?", "wamid.A1")
    assert world.out() == ["Let me get a colleague for the pricing."]
    assert world.db.one("select status from conversations") == "requesting" and world.db.one("select ai_agent_id from conversations") is None


def test_asking_for_a_person_goes_straight_to_one_without_asking_the_model(world):
    world.agent()
    world.say("I want to speak to someone please", "wamid.A1")
    assert world.llm.requests == [] and world.out() == [ai_agent.HANDOFF_TEXT]
    assert world.db.one("select status from conversations") == "requesting"


def test_after_its_reply_limit_the_agent_hands_the_chat_to_a_person(world):
    world.agent(max_replies=1)
    world.llm.say("First answer.")
    world.say("hi", "wamid.A1")
    world.say("and another thing", "wamid.A2")
    assert world.out() == ["First answer.", ai_agent.CAP_TEXT] and len(world.llm.requests) == 1
    assert world.db.one("select status from conversations") == "requesting"


def test_a_model_that_is_down_never_leaves_the_customer_unanswered_or_guessed_at(world):
    world.agent()
    world.llm.status = 500
    world.say("hello", "wamid.A1")
    assert world.out() == [ai_agent.HANDOFF_TEXT] and world.db.one("select status from conversations") == "requesting"
    assert world.db.one("select count(*) from wa_jobs where state = 'dead'") == 0


def test_without_a_language_model_configured_chats_go_to_a_person(world):
    world.agent()
    ai_agent.configure(None)
    world.say("hello", "wamid.A1")
    assert world.out() == [ai_agent.HANDOFF_TEXT]


def test_the_monthly_allowance_is_enforced_and_enterprise_has_none(world):
    world.agent()
    for _ in range(1000):
        pass
    world.db.q("insert into ai_usage (tenant_id, kind) select %s, 'reply' from generate_series(1, 1000)", (world.tid,))
    world.say("hello", "wamid.A1")
    assert world.llm.requests == [] and world.out() == [ai_agent.CAP_TEXT]


def test_a_plan_without_ai_agents_gets_none(world):
    world.agent()
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"pro\"}'")
    world.say("hello", "wamid.A1")
    assert world.llm.requests == [] and world.out() == []


def test_a_person_who_owns_the_chat_is_never_talked_over(world):
    world.agent()
    world.llm.say("first")
    world.say("hi", "wamid.A1")
    world.db.q("update conversations set status = 'intervened'")
    world.say("anyone there?", "wamid.A2")
    assert world.out() == ["first"] and len(world.llm.requests) == 1


def test_an_unread_attachment_is_handed_to_a_person(world):
    world.agent()
    H.deliver(world.db, P.inbound(P.image("m1", "site photo", "wamid.I1", str(int(time.time())))))
    H.work(world.db)
    assert world.llm.requests == [] and world.db.one("select status from conversations") == "requesting"


def test_an_agent_set_to_cover_the_closed_hours_stays_quiet_while_people_are_in(world, monkeypatch):
    world.agent(business_hours_only=True)
    monkeypatch.setattr(inbox, "in_hours", lambda hours, now: True)
    world.say("hello", "wamid.A1")
    assert world.llm.requests == [] and world.out() == []
    monkeypatch.setattr(inbox, "in_hours", lambda hours, now: False)
    world.llm.say("We are closed, but here is what I can tell you.")
    world.say("are you open?", "wamid.A2")
    assert world.out() == ["We are closed, but here is what I can tell you."]


def test_a_flow_that_takes_the_message_keeps_the_agent_out_of_it(world):
    world.agent()
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"premium\"}'")
    conn = world.conn()
    with conn.cursor() as cur:
        fid = flows.create(cur, world.tid, "Price", trigger={"type": "keyword", "match": "exact", "keywords": ["price"]},
                           graph={"entry": "a", "nodes": {"a": {"type": "send_text", "text": "Prices are on our site"}}})
        flows.publish(cur, world.tid, fid)
        flows.set_enabled(cur, world.tid, fid, True)
    conn.commit()
    conn.close()
    world.say("price", "wamid.A1")
    assert world.out() == ["Prices are on our site"] and world.llm.requests == []


def test_the_welcome_message_is_not_sent_on_top_of_an_agents_answer(world):
    world.agent()
    conn = world.conn()
    with conn.cursor() as cur:
        inbox.save_settings(cur, world.tid, {"routing": "manual", "welcome": {"enabled": True, "text": "WELCOME!"}})
    conn.commit()
    conn.close()
    world.llm.say("Hello Asha, how can I help?")
    world.say("hi", "wamid.A1")
    assert world.out() == ["Hello Asha, how can I help?"]


def test_the_same_message_delivered_twice_is_answered_once(world):
    world.agent()
    world.llm.say("once")
    world.say("hi", "wamid.A1")
    world.say("hi", "wamid.A1")
    assert world.out() == ["once"] and len(world.llm.requests) == 1


# ── looking after agents and knowledge ───────────────────────────────────────

def in_db(world, fn):
    conn = world.conn()
    try:
        with conn.cursor() as cur:
            out = fn(cur)
        conn.commit()
        return out
    finally:
        conn.close()


def test_the_basic_and_pro_plans_cannot_create_agents(world):
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"pro\"}'")
    with pytest.raises(Exception) as e:
        in_db(world, lambda cur: ai_agent.create(cur, world.tid, "Aria"))
    assert "plan" in str(e.value).lower()


def test_only_one_agent_is_the_default_and_switching_off_clears_it(world):
    a, b = world.agent("A"), world.agent("B")
    rows = {r["name"]: r["is_default"] for r in world.db.q("select name, is_default from ai_agents")}
    assert rows == {"A": False, "B": True}
    in_db(world, lambda cur: ai_agent.set_enabled(cur, world.tid, b, False))
    assert world.db.one("select count(*) from ai_agents where is_default") == 0
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.set_default(cur, world.tid, b))
    assert e.value.reason == "not_enabled"


def test_the_fields_of_an_agent_are_checked(world):
    for bad in ({"name": ""}, {"name": "x" * 61}, {"instructions": "x" * 4001}, {"max_replies": 0}, {"handoff_words": ["a"] * 21}):
        with pytest.raises(ai_agent.AIError):
            in_db(world, lambda cur, bad=bad: ai_agent.create(cur, world.tid, **{"name": "Aria", **bad}))
    aid = world.agent()
    with pytest.raises(ai_agent.AIError):
        in_db(world, lambda cur: ai_agent.update(cur, world.tid, aid, enabled=True))
    out = in_db(world, lambda cur: ai_agent.update(cur, world.tid, aid, name=" Nova ", max_replies=3))
    assert out["name"] == "Nova" and world.db.one("select max_replies from ai_agents") == 3


def test_knowledge_can_be_pasted_asked_as_questions_or_read_from_a_page(world):
    aid = world.agent()
    in_db(world, lambda cur: ai_agent.add_faq(cur, world.tid, aid, "Do you deliver?", "Yes, within 50 km of Vadodara."))
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda method, url, headers, body: (200, "<html><body><p>Our office is at Alkapuri, Vadodara. We open at ten.</p></body></html>"))
    in_db(world, lambda cur: ai_agent.add_url(cur, world.tid, aid, "https://example.com/contact", "Contact page"))
    docs = in_db(world, lambda cur: ai_agent.list_knowledge(cur, world.tid, aid))
    assert sorted((d["kind"], d["title"]) for d in docs) == [("faq", "Do you deliver?"), ("url", "Contact page")]
    hits = in_db(world, lambda cur: ai_agent.search(cur, world.tid, aid, "where is your office located"))
    assert hits and "Alkapuri" in hits[0]["text"]
    assert in_db(world, lambda cur: ai_agent.search(cur, world.tid, aid, "zzzz qqqq")) == []


def test_a_page_that_cannot_be_read_or_an_insecure_one_is_refused(world):
    aid = world.agent()
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.add_url(cur, world.tid, aid, "http://example.com"))
    assert e.value.reason == "bad_url"
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (404, "nope"))
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.add_url(cur, world.tid, aid, "https://example.com/x"))
    assert e.value.reason == "fetch_failed"
    ai_agent.configure(ai_agent.LLM_CLIENT, fetch=lambda *a: (200, "<p>hi</p>"))
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.add_url(cur, world.tid, aid, "https://example.com/x"))
    assert e.value.reason == "empty"


def test_knowledge_has_size_limits(world, monkeypatch):
    aid = world.agent()
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.add_knowledge(cur, world.tid, aid, "Big", "x" * (ai_agent.MAX_DOC_CHARS + 1)))
    assert e.value.reason == "too_long"
    monkeypatch.setattr(ai_agent, "MAX_DOCS", 2)
    in_db(world, lambda cur: ai_agent.add_knowledge(cur, world.tid, aid, "One", "first text here"))
    in_db(world, lambda cur: ai_agent.add_knowledge(cur, world.tid, aid, "Two", "second text here"))
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.add_knowledge(cur, world.tid, aid, "Three", "third text here"))
    assert e.value.reason == "kb_full"


def test_deleting_knowledge_or_an_agent_takes_its_chunks_with_it(world):
    aid = world.agent()
    kid = world.learn(aid, "Hours", "Open nine to six every day of the week.")
    assert world.db.one("select count(*) from ai_chunks") >= 1
    assert in_db(world, lambda cur: ai_agent.delete_knowledge(cur, world.tid, kid)) is True
    assert world.db.one("select count(*) from ai_chunks") == 0
    world.learn(aid, "More", "Some more text to learn.")
    assert in_db(world, lambda cur: ai_agent.delete(cur, world.tid, aid)) is True
    assert world.db.one("select count(*) from ai_chunks") == 0 and world.db.one("select count(*) from ai_knowledge") == 0


def test_another_workspace_cannot_reach_an_agent_or_its_knowledge(world, migrated):
    aid = world.agent()
    kid = world.learn(aid, "Secret", "internal pricing for builders only")
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    with pytest.raises(ai_agent.AIError):
        in_db(world, lambda cur: ai_agent.get(cur, other, aid))
    with pytest.raises(ai_agent.AIError):
        in_db(world, lambda cur: ai_agent.add_knowledge(cur, other, aid, "x", "y"))
    assert in_db(world, lambda cur: ai_agent.delete_knowledge(cur, other, kid)) is False
    assert in_db(world, lambda cur: ai_agent.search(cur, other, aid, "pricing builders")) == []


def test_the_owner_can_try_an_agent_without_whatsapp_and_it_is_counted_apart(world):
    aid = world.agent()
    world.learn(aid, "Hours", "Open nine to six every day of the week including Sunday.")
    world.llm.say("We are open nine to six, every day.")
    out = in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"from": "customer", "text": "when are you open?"}]))
    assert out["reply"].startswith("We are open") and out["sources"] == ["Hours"] and not out["handoff"]
    use = in_db(world, lambda cur: ai_agent.usage(cur, world.tid))
    assert use["replies"] == 0 and use["limit"] == 1000 and use["configured"] is True
    assert world.db.one("select count(*) from ai_usage where kind = 'test'") == 1


def test_trying_an_agent_needs_a_model_a_message_and_is_limited_per_day(world, monkeypatch):
    aid = world.agent()
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, []))
    assert e.value.reason == "empty"
    monkeypatch.setattr(ai_agent, "TESTS_PER_DAY", 1)
    world.llm.say("ok")
    in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "hi"}]))
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "hi"}]))
    assert e.value.reason == "test_limit"
    ai_agent.configure(None)
    with pytest.raises(ai_agent.AIError) as e:
        in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "hi"}]))
    assert e.value.reason == "ai_not_configured"


def test_trying_an_agent_with_a_request_for_a_person_does_not_call_the_model(world):
    aid = world.agent()
    out = in_db(world, lambda cur: ai_agent.test_chat(cur, world.tid, aid, [{"text": "I need a human"}]))
    assert out["handoff"] and world.llm.requests == []
