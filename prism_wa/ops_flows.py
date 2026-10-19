"""Routes for the Automations tab beyond the first flow routes: the clock in the test console, flow analytics, writing a flow with AI, the
ready-made library by topic, and the Conversions API.

    flows      test (with a pretend clock and holidays)  analytics  generate  library
    capi       get  save  test  send
"""
from __future__ import annotations

import datetime as dt

from prism_wa import ai_agent, capi, flow_ai, flow_engine, flow_library, flows
from prism_wa.ops_common import jsonable, managers_only, need


def _local(d: dict):
    """The pretend "now" for the test console: an ISO time in the workspace's zone (or none = the real clock)."""
    raw = d.get("local")
    if not raw:
        return None
    try:
        out = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("that date and time do not look right") from None
    return out if out.tzinfo else out.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))


def flows_test(cur, t, d):
    need(d, "graph")
    holidays = [str(h.get("date") if isinstance(h, dict) else h) for h in (d.get("holidays") or [])][:60]
    return flow_engine.test(d["graph"], d.get("trigger"), list(d.get("inputs") or []), name=d.get("name") or "Test customer", attrs=d.get("attrs") or {},
                            open_now=bool(d.get("open_now", True)), local=_local(d), holidays=holidays)


def flows_analytics(cur, t, d):
    need(d, "flow_id")
    return jsonable(flow_ai.analytics(cur, t, d["flow_id"], d.get("since") or None, d.get("until") or None))


def flows_generate(cur, t, d):
    need(d, "description")
    managers_only(cur, t, d, "create chatbot flows")
    return jsonable(flow_ai.generate(ai_agent.LLM_CLIENT, description=d["description"], business=d.get("business") or "", language=d.get("language") or "English"))


def flows_test_request(cur, t, d):
    need(d, "node")
    node = d["node"] if isinstance(d["node"], dict) else {}
    problems = [i.as_dict() for i in flows.validate({"entry": "a", "nodes": {"a": {**node, "type": "api_request"}}}) if i.severity == "error" and i.node == "a"]
    if problems:
        return {"ok": False, "error": problems[0]["message"], "issues": problems}
    return flow_engine.try_request({**node, "type": "api_request"}, name=d.get("name") or "Test customer", attrs=d.get("attrs") or {},
                                   values=d.get("values") or {}, message=str(d.get("message") or ""))


def flows_library(cur, t, d):
    topics = sorted({i.get("topic") or "General" for i in flow_library.LIBRARY})
    return {"library": [dict(item) for item in flow_library.LIBRARY], "topics": topics}


def capi_get(cur, t, d):
    return {"settings": capi.settings(cur, t), "events": list(capi.EVENTS)}


def capi_save(cur, t, d):
    managers_only(cur, t, d, "change how events are sent to Meta")
    return {"settings": capi.save_settings(cur, t, dataset_id=d.get("dataset_id") or "", test_code=d.get("test_code") or "", enabled=bool(d.get("enabled", True)))}


def capi_test(cur, t, d):
    managers_only(cur, t, d, "send a test event to Meta")
    return capi.send_test(cur, t)


def capi_send(cur, t, d):
    need(d, "contact_id", "event")
    managers_only(cur, t, d, "tell Meta about a customer")
    return capi.send_for_contact(cur, t, d["contact_id"], event=d["event"], value=d.get("value"), currency=d.get("currency") or "INR")


OPS = {"/internal/flows/test": flows_test, "/internal/flows/analytics": flows_analytics, "/internal/flows/generate": flows_generate,
       "/internal/flows/library": flows_library, "/internal/flows/test_request": flows_test_request, "/internal/capi/get": capi_get, "/internal/capi/save": capi_save, "/internal/capi/test": capi_test,
       "/internal/capi/send": capi_send}
