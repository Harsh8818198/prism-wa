"""Routes for the structured AI agent: publishing, reading a whole website, re-reading a page, the questions it could not answer, its numbers, trying an action,
and drafting its profile from the business's own website.

    ai/agents      publish  unpublish  draft_profile
    ai/knowledge   add_site  refresh
    ai/gaps        list  answer  ignore
    ai/analytics   one agent's numbers for a range
    ai/actions     test      make one of the agent's calls once, so its owner sees what comes back

The older agent and knowledge routes (list, get, create, update, add_text, test...) are in ops.py; `update` and `get` carry the new fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prism_wa import ai_actions, ai_agent, ai_insights, ai_knowledge, ai_profile
from prism_wa.ops_common import jsonable, managers_only, need


def _when(value, default):
    if not value:
        return default
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("that date does not look right") from None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def publish(cur, t, d):
    need(d, "agent_id")
    managers_only(cur, t, d, "publish AI agents")
    return ai_agent.publish(cur, t, d["agent_id"])


def unpublish(cur, t, d):
    need(d, "agent_id")
    managers_only(cur, t, d, "take AI agents down")
    ai_agent.unpublish(cur, t, d["agent_id"])
    return {}


def draft_profile(cur, t, d):
    need(d, "url")
    managers_only(cur, t, d, "write an AI agent's profile")
    if ai_agent.LLM_CLIENT is None:
        raise ai_agent.AIError("ai_not_configured", "AI writing is not switched on for this server yet.")
    try:
        text = ai_profile.fetch_page_text(ai_agent.FETCH, str(d["url"]).strip())
    except ai_profile.ProfileError as why:
        raise ai_agent.AIError(why.reason, str(why)) from why
    profile, tokens = ai_profile.draft_from_page(ai_agent.LLM_CLIENT, text, str(d["url"]).strip())
    ai_agent.record_usage(cur, t, None, None, "aux", tokens)
    return {"profile": profile}


def add_site(cur, t, d):
    need(d, "agent_id", "url")
    managers_only(cur, t, d, "teach AI agents")
    out = ai_knowledge.add_site(cur, t, d["agent_id"], d["url"], int(d.get("max_pages") or ai_knowledge.DEFAULT_PAGES), d.get("title") or "")
    return {"knowledge_id": str(out["knowledge_id"]), "group_id": str(out["group_id"])}


def refresh(cur, t, d):
    need(d, "knowledge_id")
    managers_only(cur, t, d, "teach AI agents")
    ai_knowledge.refresh(cur, t, d["knowledge_id"])
    return {}


def gaps_list(cur, t, d):
    need(d, "agent_id")
    return {"gaps": [jsonable(g) for g in ai_insights.gaps(cur, t, d["agent_id"], d.get("status") or "open", min(int(d.get("limit") or 50), 200))]}


def gaps_answer(cur, t, d):
    need(d, "gap_id", "answer")
    managers_only(cur, t, d, "teach AI agents")
    return {"knowledge_id": str(ai_insights.answer_gap(cur, t, d["gap_id"], d["answer"]))}


def gaps_ignore(cur, t, d):
    need(d, "gap_id")
    managers_only(cur, t, d, "teach AI agents")
    ai_insights.ignore_gap(cur, t, d["gap_id"])
    return {}


def analytics(cur, t, d):
    need(d, "agent_id")
    until = _when(d.get("until"), datetime.now(timezone.utc))
    since = _when(d.get("since"), until - timedelta(days=30))
    return jsonable(ai_insights.analytics(cur, t, d["agent_id"], since, until))


def actions_test(cur, t, d):
    need(d, "action")
    managers_only(cur, t, d, "test an AI agent's actions")
    try:
        action = ai_profile.clean_actions([d["action"]])[0]
        if d.get("agent_id"):                                     # a header the form still shows masked is the one already stored
            stored = ai_agent._agent(cur, t, d["agent_id"])["actions"]
            action = ai_profile.clean_actions(ai_profile.unmask([d["action"]], stored))[0]
    except ai_profile.ProfileError as why:
        raise ai_agent.AIError(why.reason, str(why)) from why
    result = ai_actions.run(action, d.get("args") or {}, fetch=ai_agent.FETCH, contact={"name": "Test customer", "phone": "919800000000"})
    return {"ok": result["ok"] and result["ran"], "ran": result["ran"], "status": result["status"], "text": result["text"], "error": result["error"]}


OPS = {"/internal/ai/agents/publish": publish, "/internal/ai/agents/unpublish": unpublish, "/internal/ai/agents/draft_profile": draft_profile,
       "/internal/ai/knowledge/add_site": add_site, "/internal/ai/knowledge/refresh": refresh, "/internal/ai/gaps/list": gaps_list,
       "/internal/ai/gaps/answer": gaps_answer, "/internal/ai/gaps/ignore": gaps_ignore, "/internal/ai/analytics": analytics, "/internal/ai/actions/test": actions_test}
