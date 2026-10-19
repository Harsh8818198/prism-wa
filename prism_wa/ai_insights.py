"""What an AI agent has been doing and what it could not answer.

  event()         one row per thing it did in a chat: answered, handed over, ran an action, finished a skill
  record_gap()    a customer's question the agent could not answer, counted — the same question twice is one gap that happened twice
  gaps()/answer_gap()/ignore_gap()   the list the owner works through: answer one and it becomes a question-and-answer note the agent can use
  analytics()     replies, chats, how many were settled without a person, why chats were handed over, what the agent was asked most

"Settled without a person" means the agent answered and the chat was not handed over in the range. It does not mean the customer was happy; the page says so.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from prism_wa import ai_agent

KINDS = ("answered", "handoff", "action", "skill", "gap", "failed")


def event(cur, tenant_id, agent_id, conversation_id, kind: str, reason: str = "") -> None:
    cur.execute("insert into ai_events (tenant_id, agent_id, conversation_id, kind, reason) values (%s, %s, %s, %s, %s)",
                (tenant_id, agent_id, conversation_id, kind, (reason or "")[:200] or None))


def question_key(question: str) -> str:
    return re.sub(r"\W+", " ", (question or "").lower()).strip()[:200]


def record_gap(cur, tenant_id, agent_id, question: str, reason: str = "") -> None:
    key = question_key(question)
    if not key:
        return
    cur.execute("insert into ai_gaps (tenant_id, agent_id, key, question, reason) values (%s, %s, %s, %s, %s)"
                " on conflict (agent_id, key) do update set times = ai_gaps.times + 1, last_at = now(),"
                " status = case when ai_gaps.status = 'answered' then 'open' else ai_gaps.status end, reason = coalesce(excluded.reason, ai_gaps.reason)",
                (tenant_id, agent_id, key, (question or "").strip()[:500], (reason or "")[:200] or None))


def gaps(cur, tenant_id, agent_id, status: str = "open", limit: int = 50) -> list:
    ai_agent._agent(cur, tenant_id, agent_id)
    cur.execute("select id, question, reason, times, status, first_at, last_at from ai_gaps where tenant_id = %s and agent_id = %s and status = %s"
                " order by times desc, last_at desc limit %s", (tenant_id, agent_id, status if status in ("open", "answered", "ignored") else "open", limit))
    return [dict(zip(("id", "question", "reason", "times", "status", "first_at", "last_at"), r)) for r in cur.fetchall()]


def _gap(cur, tenant_id, gap_id) -> dict:
    cur.execute("select id, agent_id, question from ai_gaps where id = %s and tenant_id = %s for update", (gap_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ai_agent.AIError("unknown_gap", "that question is not in this workspace")
    return dict(zip(("id", "agent_id", "question"), row))


def answer_gap(cur, tenant_id, gap_id, answer: str) -> str:
    """Teach the agent the answer to a question it could not answer. -> the new note's id."""
    g = _gap(cur, tenant_id, gap_id)
    kid = ai_agent.add_faq(cur, tenant_id, g["agent_id"], g["question"], answer)
    cur.execute("update ai_gaps set status = 'answered' where id = %s", (gap_id,))
    return kid


def ignore_gap(cur, tenant_id, gap_id) -> None:
    _gap(cur, tenant_id, gap_id)
    cur.execute("update ai_gaps set status = 'ignored' where id = %s", (gap_id,))


def analytics(cur, tenant_id, agent_id, since: Optional[datetime] = None, until: Optional[datetime] = None) -> dict:
    ai_agent._agent(cur, tenant_id, agent_id)
    until = until or datetime.now(timezone.utc)
    since = since or (until - timedelta(days=30))
    cur.execute("select kind, count(*), count(distinct conversation_id) from ai_events where tenant_id = %s and agent_id = %s and at >= %s and at < %s group by kind",
                (tenant_id, agent_id, since, until))
    by = {k: (n, c) for k, n, c in cur.fetchall()}
    cur.execute("select count(distinct conversation_id) from ai_events where tenant_id = %s and agent_id = %s and at >= %s and at < %s and kind in ('answered', 'handoff')",
                (tenant_id, agent_id, since, until))
    chats = cur.fetchone()[0]
    handed = by.get("handoff", (0, 0))[1]
    cur.execute("select coalesce(reason, 'no reason given'), count(*) from ai_events where tenant_id = %s and agent_id = %s and at >= %s and at < %s and kind = 'handoff'"
                " group by 1 order by 2 desc limit 6", (tenant_id, agent_id, since, until))
    reasons = [{"reason": r, "count": n} for r, n in cur.fetchall()]
    cur.execute("select (at at time zone 'UTC')::date, count(*) from ai_events where tenant_id = %s and agent_id = %s and at >= %s and at < %s and kind = 'answered'"
                " group by 1 order by 1", (tenant_id, agent_id, since, until))
    days = [{"day": str(d), "replies": n} for d, n in cur.fetchall()]
    cur.execute("select coalesce(sum(tokens_in), 0), coalesce(sum(tokens_out), 0) from ai_usage where tenant_id = %s and agent_id = %s and at >= %s and at < %s",
                (tenant_id, agent_id, since, until))
    tin, tout = cur.fetchone()
    cur.execute("select count(*) from ai_usage where tenant_id = %s and agent_id = %s and kind = 'test' and at >= %s and at < %s", (tenant_id, agent_id, since, until))
    tests = cur.fetchone()[0]
    return {"range": {"since": since.isoformat(), "until": until.isoformat()},
            "totals": {"replies": by.get("answered", (0, 0))[0], "chats": chats, "handed_over": handed, "settled": max(chats - handed, 0),
                       "settled_rate": round(100.0 * max(chats - handed, 0) / chats, 1) if chats else 0.0, "handover_rate": round(100.0 * handed / chats, 1) if chats else 0.0,
                       "actions": by.get("action", (0, 0))[0], "skills_finished": by.get("skill", (0, 0))[0], "tests": tests, "tokens_in": int(tin), "tokens_out": int(tout)},
            "reasons": reasons, "days": days, "gaps": gaps(cur, tenant_id, agent_id, "open", 10)}
