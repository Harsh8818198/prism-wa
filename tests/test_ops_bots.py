"""The service API for flows, AI agents, repeating campaigns and test sends, over HTTP; and the ready-made flows."""
import json
import os

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import ai_agent, flow_library, flows
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.llm import LLM
from prism_wa.vault import Vault

INTERNAL = "internal-shared-secret"
AUTH = {"X-Internal-Token": INTERNAL}
BODY = {"type": "BODY", "text": "Hello {{1}}, our Diwali offer is on until Friday so please do reply soon.",
        "example": {"body_text": [["Asha"]]}}
GRAPH = {"entry": "a", "nodes": {"a": {"type": "ask", "text": "Your name?", "save_as": "attr:Name", "next": "b"},
                                 "b": {"type": "send_text", "text": "Thanks {{attr.Name}}"}}}
HELLO = {"type": "keyword", "match": "exact", "keywords": ["hello"]}


class Model:
    def __init__(self):
        self.reply = "We open at ten."

    def __call__(self, method, url, data, headers):
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps({"reply": self.reply, "handoff": False})}}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()


@pytest.fixture
def api(migrated):
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tid,))
    waba = migrated.one("select id from wabas")
    migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
               " values (%s, %s, 'diwali', 'en_US', 'MARKETING', 'APPROVED', %s)", (tid, waba, json.dumps([BODY])))
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn),
                     internal_token=INTERNAL, graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}))
    client = TestClient(app, follow_redirects=False)
    model = Model()
    ai_agent.configure(LLM(api_key="k", transport=model))

    class A:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers=AUTH)

    A.db, A.tid, A.client_, A.model = migrated, tid, client, model
    yield A
    ai_agent.configure(None)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def seat(api, role):
    return ok(api.post("/internal/inbox/ensure_agent", seat_key="seat-%s" % role, name=role.title()))["agent"]["id"], role


# ── the ready-made flows ─────────────────────────────────────────────────────

@pytest.mark.parametrize("item", flow_library.LIBRARY, ids=lambda i: i["key"])
def test_every_ready_made_flow_is_one_prism_would_publish(item):
    issues = flows.validate(item["graph"], item["trigger"])
    assert flows.errors(issues) == [] and [i.code for i in issues if i.severity == "warning"] == [], issues


def test_the_ready_made_flows_can_be_walked_in_the_test_console():
    from prism_wa import flow_engine
    lead = flow_library.get("lead_capture")
    out = flow_engine.test(lead["graph"], lead["trigger"], ["hi", "Get a quote", "Asha Patel", "Vadodara"], name="Asha")
    assert out["state"] == "done" and out["handoff"] == "new enquiry"
    assert [t["text"] for t in out["transcript"] if t["from"] == "bot"][-1] == "Thank you Asha Patel. Our team will message you shortly."
    faq = flow_library.get("faq_menu")
    out = flow_engine.test(faq["graph"], faq["trigger"], ["menu", "Opening hours", "Speak to a person"])
    assert out["state"] == "done" and any("Monday to Saturday" in str(t.get("text")) for t in out["transcript"])


# ── flows over HTTP ──────────────────────────────────────────────────────────

def test_a_flow_goes_from_draft_to_published_to_running_over_http(api):
    fid = ok(api.post("/internal/flows/create", name="Greeting", trigger=HELLO, graph=GRAPH))["flow_id"]
    got = ok(api.post("/internal/flows/get", flow_id=fid))
    assert got["flow"]["name"] == "Greeting" and got["issues"] == [] and got["flow"]["version"] == 0
    refused = api.post("/internal/flows/set_enabled", flow_id=fid, enabled=True)
    assert refused.status_code == 409 and refused.json()["error"] == "not_published"
    assert ok(api.post("/internal/flows/publish", flow_id=fid))["version"] == 1
    ok(api.post("/internal/flows/set_enabled", flow_id=fid, enabled=True))
    listed = ok(api.post("/internal/flows/list"))["flows"]
    assert listed[0]["enabled"] is True and listed[0]["version"] == 1 and listed[0]["live"] == 0
    assert ok(api.post("/internal/flows/versions", flow_id=fid))["versions"][0]["version"] == 1


def test_publishing_a_broken_flow_returns_the_problems_in_plain_words(api):
    fid = ok(api.post("/internal/flows/create", name="Broken", trigger=HELLO,
                      graph={"entry": "a", "nodes": {"a": {"type": "send_text", "text": "x", "next": "nowhere"}}}))["flow_id"]
    r = api.post("/internal/flows/publish", flow_id=fid)
    assert r.status_code == 422 and r.json()["error"] == "flow_invalid"
    assert any(i["code"] == "dangling" for i in r.json()["issues"]) and "nowhere" in r.json()["message"]
    checked = ok(api.post("/internal/flows/check", flow_id=fid))
    assert checked["valid"] is False and checked["issues"]


def test_editing_a_flow_returns_what_is_wrong_so_the_screen_can_show_it_at_once(api):
    fid = ok(api.post("/internal/flows/create", name="Greeting", trigger=HELLO, graph=GRAPH))["flow_id"]
    out = ok(api.post("/internal/flows/update", flow_id=fid, graph={"entry": "x", "nodes": {"a": {"type": "end"}}}))
    assert {i["code"] for i in out["issues"]} >= {"no_entry"}


def test_the_test_console_needs_no_saved_flow(api):
    out = ok(api.post("/internal/flows/test", graph=GRAPH, trigger=HELLO, inputs=["hello", "Asha"], name="Asha"))
    assert out["state"] == "done" and [t["text"] for t in out["transcript"] if t["from"] == "bot"] == ["Your name?", "Thanks Asha"]
    assert ok(api.post("/internal/flows/test", graph={}, inputs=["x"]))["state"] == "invalid"


def test_a_ready_made_flow_is_copied_into_the_workspace_and_an_unknown_one_is_refused(api):
    lib = ok(api.post("/internal/flows/library"))["library"]
    assert {i["key"] for i in lib} >= {"lead_capture", "faq_menu", "feedback"} and lib[0]["graph"]["entry"]
    fid = ok(api.post("/internal/flows/from_library", key="faq_menu"))["flow_id"]
    assert ok(api.post("/internal/flows/get", flow_id=fid))["flow"]["name"] == "Answer common questions"
    assert api.post("/internal/flows/from_library", key="nope").status_code == 400


def test_a_flow_can_be_rolled_back_and_deleted(api):
    fid = ok(api.post("/internal/flows/create", name="G", trigger=HELLO, graph=GRAPH))["flow_id"]
    ok(api.post("/internal/flows/publish", flow_id=fid))
    ok(api.post("/internal/flows/update", flow_id=fid, graph={"entry": "z", "nodes": {"z": {"type": "send_text", "text": "v2"}}}))
    ok(api.post("/internal/flows/publish", flow_id=fid))
    assert ok(api.post("/internal/flows/rollback", flow_id=fid, version=1))["version"] == 3
    assert api.post("/internal/flows/rollback", flow_id=fid, version=9).status_code == 409
    assert ok(api.post("/internal/flows/delete", flow_id=fid))["deleted"] is True
    assert api.post("/internal/flows/get", flow_id=fid).status_code == 409


def test_only_an_owner_or_manager_may_change_the_bots_but_anyone_may_look(api):
    seat(api, "owner")                                  # the first seat in a workspace is its owner
    agent_id, _ = seat(api, "agent")
    fid = ok(api.post("/internal/flows/create", name="G", trigger=HELLO, graph=GRAPH))["flow_id"]           # no identity sent: the licence server's job
    for route, body in (("flows/create", {"name": "x"}), ("flows/publish", {"flow_id": fid}), ("flows/delete", {"flow_id": fid}),
                        ("flows/set_enabled", {"flow_id": fid, "enabled": True}), ("ai/agents/create", {"name": "A"}),
                        ("schedules/create", {"name": "S", "config": {}, "rule": {}})):
        r = api.post("/internal/" + route, by_agent_id=agent_id, **body)
        assert r.status_code == 409 and r.json()["error"] == "not_allowed", route
    assert ok(api.post("/internal/flows/list", by_agent_id=agent_id))["flows"]
    manager_id, _ = seat(api, "manager")
    api.db.q("update agents set role = 'manager' where id = %s", (manager_id,))
    assert api.post("/internal/flows/create", by_agent_id=manager_id, name="ok", trigger=HELLO, graph=GRAPH).status_code == 200


def test_a_basic_plan_is_told_which_plan_has_flows(api):
    api.db.q("update tenant_settings set settings = settings || '{\"plan\": \"basic\"}'")
    r = api.post("/internal/flows/create", name="x")
    assert r.status_code == 403 and r.json()["error"] == "plan_feature" and r.json()["needs"] == "pro"


# ── AI agents over HTTP ──────────────────────────────────────────────────────

def test_an_agent_is_built_taught_tried_and_switched_on_over_http(api):
    aid = ok(api.post("/internal/ai/agents/create", name="Aria", instructions="Be brief."))["agent_id"]
    ok(api.post("/internal/ai/knowledge/add_faq", agent_id=aid, question="When do you open?", answer="At ten every day."))
    ok(api.post("/internal/ai/knowledge/add_text", agent_id=aid, title="Delivery", body="We deliver within fifty kilometres of Vadodara."))
    docs = ok(api.post("/internal/ai/knowledge/list", agent_id=aid))["knowledge"]
    assert sorted(d["title"] for d in docs) == ["Delivery", "When do you open?"]
    answer = ok(api.post("/internal/ai/test", agent_id=aid, messages=[{"from": "customer", "text": "when do you open"}]))["answer"]
    assert answer["reply"] == "We open at ten." and answer["sources"] == ["When do you open?"] and answer["handoff"] is False
    ok(api.post("/internal/ai/agents/set_enabled", agent_id=aid, enabled=True))
    ok(api.post("/internal/ai/agents/set_default", agent_id=aid))
    agent = ok(api.post("/internal/ai/agents/get", agent_id=aid))["agent"]
    assert agent["enabled"] and agent["is_default"] and agent["documents"] == 2
    ok(api.post("/internal/ai/agents/update", agent_id=aid, changes={"max_replies": 5, "instructions": "Speak Gujarati."}))
    assert ok(api.post("/internal/ai/agents/list"))["agents"][0]["max_replies"] == 5
    status = ok(api.post("/internal/ai/status"))
    assert status["included"] is True and status["usage"]["configured"] is True and status["usage"]["limit"] == 1000
    assert ok(api.post("/internal/ai/knowledge/delete", knowledge_id=docs[0]["id"]))["deleted"] is True
    assert ok(api.post("/internal/ai/agents/delete", agent_id=aid))["deleted"] is True


def test_ai_errors_come_back_as_reasons_a_screen_can_show(api):
    aid = ok(api.post("/internal/ai/agents/create", name="Aria"))["agent_id"]
    r = api.post("/internal/ai/agents/set_default", agent_id=aid)
    assert r.status_code == 409 and r.json()["error"] == "not_enabled"
    r = api.post("/internal/ai/knowledge/add_url", agent_id=aid, url="http://insecure.example")
    assert r.status_code == 409 and r.json()["error"] == "bad_url"
    ai_agent.configure(None)
    r = api.post("/internal/ai/test", agent_id=aid, messages=[{"text": "hi"}])
    assert r.status_code == 409 and r.json()["error"] == "ai_not_configured"
    assert api.post("/internal/ai/agents/create", name="").status_code == 400                       # missing, not an AI error


def test_a_plan_without_ai_is_told_so(api):
    api.db.q("update tenant_settings set settings = settings || '{\"plan\": \"pro\"}'")
    assert ok(api.post("/internal/ai/status"))["included"] is False
    r = api.post("/internal/ai/agents/create", name="Aria")
    assert r.status_code == 403 and r.json()["needs"] == "premium"


# ── repeating campaigns and a test over HTTP ─────────────────────────────────

CONFIG = {"template_name": "diwali", "audience": {"tags": ["vip"]}, "variables": {"1": {"source": "field", "field": "first_name"}}}


def test_a_repeating_campaign_is_created_listed_paused_and_deleted(api):
    sid = ok(api.post("/internal/schedules/create", name="Monday offer", config=CONFIG,
                      rule={"freq": "weekly", "time": "10:00", "days": ["mon"]}))["schedule_id"]
    rows = ok(api.post("/internal/schedules/list"))["schedules"]
    assert rows[0]["name"] == "Monday offer" and rows[0]["enabled"] is True and rows[0]["next_run_at"]
    assert ok(api.post("/internal/schedules/set_enabled", schedule_id=sid, enabled=False))["schedule"]["enabled"] is False
    assert ok(api.post("/internal/schedules/delete", schedule_id=sid))["deleted"] is True


def test_a_bad_repeat_rule_is_a_clear_refusal(api):
    r = api.post("/internal/schedules/create", name="x", config=CONFIG, rule={"freq": "weekly", "time": "10:00"})
    assert r.status_code == 400 and r.json()["error"] == "bad_rule" and "days" in r.json()["message"]


def test_a_test_send_goes_over_http_and_its_refusals_are_reasons(api):
    out = ok(api.post("/internal/campaigns/test", template_name="diwali", to="9876543210", variables=CONFIG["variables"]))
    assert out["result"]["to"] == "919876543210"
    assert api.db.one("select kind from outbox") == "template"
    r = api.post("/internal/campaigns/test", template_name="diwali", to="9876543210", variables={})
    assert r.status_code == 409 and r.json()["error"] == "variable_unmapped"


def test_every_new_route_needs_the_secret(api):
    for path in ("/internal/flows/list", "/internal/ai/agents/list", "/internal/schedules/list", "/internal/campaigns/test"):
        assert api.client_.post(path, json={"tenant_id": str(api.tid)}).status_code == 401
        assert api.client_.post(path, json={}, headers=AUTH).status_code == 400


def test_the_team_list_says_which_teammate_is_asking(api):
    me, _ = seat(api, "owner")
    out = ok(api.post("/internal/inbox/agents/list", agent_id=me))
    assert out["me"] == str(me) and [a["id"] for a in out["agents"]] == [me]
    assert ok(api.post("/internal/inbox/agents/list"))["me"] is None
