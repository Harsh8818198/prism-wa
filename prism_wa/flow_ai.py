"""Writing a chatbot flow from a description, and what customers did inside one.

    generate()    "a bot that takes a quote request: ask the name, the city, the size, then hand over" -> a flow, checked the way a published one is
    autofix()     the deterministic repairs (too-long button text, a step that leads nowhere, a missing end) that need no model
    analytics()   per flow: how many started, finished, were handed to a person, and where each step loses people

The model is only asked to write the flow as JSON in the documented shape. What it returns is repaired and run through `flows.validate`, and
if errors remain it is shown them and asked once or twice to fix its own work; a flow that still fails is returned with its problems, never
saved. Nothing is published or switched on here.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from prism_wa import flows
from prism_wa.llm import LLM, LLMError

SCHEMA = """A flow is JSON: {"name": str, "trigger": {...}, "graph": {"entry": "<node id>", "nodes": {"<node id>": {"type": ..., ...}}}}.
Node ids are letters, numbers and underscores (up to 40). Step types and their fields:
- send_text {text, next}
- buttons {text, options: [{id, title (max 20 chars), next}] (1 to 3), timeout_next?, fallback_next?}
- list {text, button_label (max 20), rows: [{id, title (max 24), description? (max 72), next}] (1 to 10)}
- template {name, language, variables?, next}
- ask {text, save_as: "attr:Name" or "var:name", validator: text|number|email|phone|date|options|regex, options?, pattern?, max_tries (1-5), retry_text?, next, invalid_next?}
- condition {rules: [{field: message|tag|hours|weekday|date|time|holiday|attr:Name|var:name, op, value, next}], else}
    ops: eq ne contains starts_with exists not_exists gt lt regex; hours: open closed; weekday: eq ne in (mon..sun); date/time: eq gt lt between ("a..b"); holiday: yes no
- set_attribute {key, value, next}   - add_tag {tag, next}   - remove_tag {tag, next}   - delay {minutes or seconds, next}
- media {kind: image|video|audio|document, asset_id, caption?, next}   - send_location {latitude, longitude, name?, address?, next}
- location_request {text, next, timeout_next?}   - webhook {url (https), method, body?, headers?, next}   - capi_event {event, value?, currency?, next}
- handoff {text?, reason?}  (hands the chat to a person; ends the flow)   - agent {agent_id?}  (hands to the AI agent)   - end {}
Text may use {{first_name}}, {{name}}, {{mobile}}, {{attr.City}}, {{var.name}}. A flow never loops without a question or a delay in the loop.
Trigger is one of: {"type": "keyword", "match": "exact|contains|starts_with", "keywords": [..]}, {"type": "first_message"}, {"type": "default"}, {"type": "regex", "pattern": ..}."""

RULES = ("You design WhatsApp chatbot flows for small businesses in India. Keep every message short and friendly, ask one thing at a time, always give a way to reach a "
         "person (a handoff step), and end every path. Use only the step types listed. Answer with the JSON only.\n" + SCHEMA)


def _snake(text: str, fallback: str = "step") -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_") or fallback)[:40]


def autofix(graph: dict) -> dict:
    """Repairs that need no model: it changes what is wrong in a way that cannot change the meaning. Returns a new graph."""
    g = json.loads(json.dumps(graph or {}))
    nodes = g.get("nodes")
    if not isinstance(nodes, dict):
        return g
    fixed = {}
    for nid, node in nodes.items():                      # names the engine accepts
        fixed[_snake(nid, "step")] = node
    rename = {nid: _snake(nid, "step") for nid in nodes}
    nodes = fixed
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        for key in flows.NEXT_KEYS:
            if isinstance(node.get(key), str):
                node[key] = rename.get(node[key], _snake(node[key]))
        for key in ("options", "rows", "rules"):
            for item in node.get(key) or []:
                if isinstance(item, dict) and isinstance(item.get("next"), str):
                    item["next"] = rename.get(item["next"], _snake(item["next"]))
        for section in node.get("sections") or []:
            for item in (section.get("rows") if isinstance(section, dict) else None) or []:
                if isinstance(item, dict) and isinstance(item.get("next"), str):
                    item["next"] = rename.get(item["next"], _snake(item["next"]))
        for item in node.get("options") or []:            # the limits Meta puts on button and row text
            if isinstance(item, dict) and isinstance(item.get("title"), str):
                item["title"] = item["title"].strip()[:20]
        for item in node.get("rows") or []:
            if isinstance(item, dict):
                if isinstance(item.get("title"), str):
                    item["title"] = item["title"].strip()[:24]
                if isinstance(item.get("description"), str):
                    item["description"] = item["description"][:72]
        if isinstance(node.get("button_label"), str):
            node["button_label"] = node["button_label"].strip()[:20]
        if isinstance(node.get("text"), str):
            node["text"] = node["text"].strip()[:flows.MAX_TEXT]
    if g.get("entry") in rename:
        g["entry"] = rename[g["entry"]]
    for nid, node in list(nodes.items()):                 # a step that leads to nowhere ends there
        if isinstance(node, dict):
            for key in flows.NEXT_KEYS:
                if isinstance(node.get(key), str) and node[key] and node[key] not in nodes:
                    node[key] = None
            for key in ("options", "rows", "rules"):
                for item in node.get(key) or []:
                    if isinstance(item, dict) and isinstance(item.get("next"), str) and item["next"] not in nodes:
                        item["next"] = None
    if nodes and g.get("entry") not in nodes:
        g["entry"] = next(iter(nodes))
    g["nodes"] = nodes
    return g


def _parse(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        out = json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a < 0 or b <= a:
            raise LLMError("the language model did not answer in the expected form") from None
        try:
            out = json.loads(text[a:b + 1])
        except ValueError:
            raise LLMError("the language model did not answer in the expected form") from None
    if not isinstance(out, dict) or not isinstance(out.get("graph"), dict):
        raise LLMError("the language model did not describe a flow")
    return out


def _checked(item: dict) -> dict:
    graph = autofix(item.get("graph"))
    trigger = item.get("trigger") if isinstance(item.get("trigger"), dict) else {"type": "keyword", "match": "exact", "keywords": ["hi"]}
    issues = flows.validate(graph, trigger)
    return {"name": (str(item.get("name") or "New flow").strip() or "New flow")[:80], "trigger": trigger, "graph": graph,
            "issues": [i.as_dict() for i in issues], "valid": not flows.errors(issues)}


def generate(llm: Optional[LLM], *, description: str, business: str = "", language: str = "English", rounds: int = 2) -> dict:
    if llm is None:
        raise LookupError("AI writing is not set up on this server (WA_LLM_API_KEY).")
    description = (description or "").strip()
    if len(description) < 12:
        raise ValueError("describe what the bot should do, in a sentence or two")
    if len(description) > 1500:
        raise ValueError("keep the description under 1500 characters")
    ask = "Design a flow for this.\nWhat it should do: %s\nBusiness: %s\nLanguage of the messages: %s" % (description, business or "a small business in India", language)
    messages = [{"role": "system", "content": RULES}, {"role": "user", "content": ask}]
    tokens = {"tokens_in": 0, "tokens_out": 0}
    best, tries = None, 0
    for attempt in range(max(1, rounds) + 1):
        text, usage = llm.chat(messages, max_tokens=2600, temperature=0.4 if attempt == 0 else 0.2, json_mode=True)
        for k in tokens:
            tokens[k] += usage.get(k, 0)
        try:
            candidate = _checked(_parse(text))
        except LLMError:
            if best is None and attempt >= max(1, rounds):
                raise
            continue
        best, tries = candidate, attempt
        if candidate["valid"]:
            break
        problems = "\n".join("- %s" % i["message"] for i in candidate["issues"] if i["severity"] == "error")[:1500]
        messages = messages + [{"role": "assistant", "content": json.dumps({"name": candidate["name"], "trigger": candidate["trigger"], "graph": candidate["graph"]})[:6000]},
                               {"role": "user", "content": "That flow has these problems. Fix them and answer with the whole corrected JSON.\n" + problems}]
    if best is None:
        raise LLMError("the language model sent no usable flow")
    return {**best, "repairs": tries, "tokens": tokens}


# ── what customers did ───────────────────────────────────────────────────────

def _order(graph: dict) -> list:
    """The steps in the order a customer would meet them: from the entry, breadth first; unreachable ones last."""
    nodes = (graph or {}).get("nodes") or {}
    seen, queue = [], [graph.get("entry")] if (graph or {}).get("entry") in nodes else []
    while queue:
        cur = queue.pop(0)
        if cur in seen or cur not in nodes or not isinstance(nodes[cur], dict):
            continue
        seen.append(cur)
        queue.extend(flows._targets(nodes[cur]))
    return seen + [n for n in nodes if n not in seen]


def _snippet(node: dict) -> str:
    text = " ".join(str(node.get("text") or node.get("name") or node.get("tag") or node.get("url") or "").split())
    return text if len(text) <= 70 else text[:69].rstrip() + "…"


def analytics(cur, tenant_id, flow_id, since=None, until=None, *, version: Optional[int] = None) -> dict:
    """How a flow is doing: runs by outcome, the steps in order with how many reached each and where people stopped, and runs per day."""
    from prism_wa import usage
    f = flows._flow(cur, tenant_id, flow_id)
    since, until = usage._range(since, until)
    live = version if version is not None else f["version"]
    graph = f["graph"]
    if live:
        cur.execute("select graph from flow_versions where flow_id = %s and version = %s", (flow_id, live))
        row = cur.fetchone()
        graph = row[0] if row else graph
    cur.execute("select state, count(*), coalesce(avg(steps), 0) from flow_runs where flow_id = %s and tenant_id = %s and started_at >= %s and started_at < %s group by 1",
                (flow_id, tenant_id, since, until))
    by_state = {}
    steps_avg = 0.0
    total = 0
    for state, n, avg in cur.fetchall():
        by_state[state] = n
        total += n
        steps_avg += float(avg) * n
    cur.execute("select count(distinct r.id) from flow_runs r join flow_run_steps s on s.run_id = r.id and s.kind in ('handoff', 'agent')"
                " where r.flow_id = %s and r.tenant_id = %s and r.started_at >= %s and r.started_at < %s", (flow_id, tenant_id, since, until))
    handed = cur.fetchone()[0]
    cur.execute("select s.node, count(distinct s.run_id) from flow_run_steps s join flow_runs r on r.id = s.run_id"
                " where s.flow_id = %s and s.tenant_id = %s and r.started_at >= %s and r.started_at < %s group by 1", (flow_id, tenant_id, since, until))
    reached = dict(cur.fetchall())
    cur.execute("select node, count(*) from flow_runs where flow_id = %s and tenant_id = %s and started_at >= %s and started_at < %s"
                " and state in ('waiting', 'failed', 'cancelled', 'running') group by 1", (flow_id, tenant_id, since, until))
    stopped = dict(cur.fetchall())
    nodes = []
    for nid in _order(graph):
        node = graph["nodes"][nid]
        seen = int(reached.get(nid, 0))
        gone = int(stopped.get(nid, 0))
        nodes.append({"node": nid, "kind": node.get("type"), "text": _snippet(node), "reached": seen, "stopped": gone,
                      "stopped_pct": round(100.0 * gone / seen, 1) if seen else 0.0})
    cur.execute("select (started_at at time zone 'UTC')::date, count(*) from flow_runs where flow_id = %s and tenant_id = %s and started_at >= %s and started_at < %s group by 1 order by 1",
                (flow_id, tenant_id, since, until))
    days = [{"day": str(d), "runs": n} for d, n in cur.fetchall()]
    done = by_state.get("done", 0)
    return {"flow": {"id": str(flow_id), "name": f["name"], "version": live}, "range": {"since": since.isoformat(), "until": until.isoformat()},
            "totals": {"started": total, "done": done, "failed": by_state.get("failed", 0), "cancelled": by_state.get("cancelled", 0),
                       "in_progress": by_state.get("running", 0) + by_state.get("waiting", 0), "handed_to_person": handed,
                       "completion_rate": round(100.0 * done / total, 1) if total else 0.0, "avg_steps": round(steps_avg / total, 1) if total else 0.0},
            "nodes": nodes, "days": days}
