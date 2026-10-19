"""What an AI agent IS, beyond one box of instructions: who the business is, how it speaks, what tasks it does, what it may call, and how it behaves
when it cannot help.

  profile   the business name, website, description, greeting and ground rules
  voice     tone, answer length and language
  skills    tasks (answer questions, hand over, qualify a lead, book an appointment...) — a skill may ask the customer for details and keep each on
            the contact as an attribute, then tag the contact and/or pass the chat to a person when it has them all
  actions   the owner's own web addresses the agent may call (order status, free slots, a booking) — see ai_actions.py
  settings  what to say when it cannot answer, what to say when it hands over, a web address told about every hand-over, up to three numbers that
            talk to the unpublished draft, and which of the workspace's numbers it covers

Everything here is plain data and plain checks; nothing is sent to the model until `system_prompt` writes it into the instructions. A bad value is
refused with a sentence the owner can act on, never stored half-right. Secrets (a header such as `Authorization`) are stored as written and shown
masked, and a masked value sent back keeps the stored one.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Optional

from prism_wa import contacts, flow_engine, plans

TONES = ("friendly", "professional", "casual", "formal", "enthusiastic", "empathetic")
LENGTHS = ("short", "medium", "detailed")
SKILL_KINDS = ("faq", "support", "handoff", "lead", "booking", "enquiry", "feedback", "custom")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
ATTR = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
MASK = "••••"
MAX_SKILLS, MAX_ACTIONS, MAX_VARIABLES, MAX_PARAMS = 12, 10, 8, 6

TONE_SENTENCE = {"friendly": "Be warm and friendly.", "professional": "Be professional and courteous.", "casual": "Be relaxed and conversational, like a helpful friend.",
                 "formal": "Be formal and respectful.", "enthusiastic": "Be upbeat and enthusiastic, without exaggerating.",
                 "empathetic": "Be patient and empathetic; acknowledge how the customer feels before you help."}
LENGTH_SENTENCE = {"short": "Keep every answer to one or two short sentences.", "medium": "Answer in three or four short sentences.",
                   "detailed": "Give a complete answer, but never more than six sentences."}


class ProfileError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _text(value, name: str, maximum: int, *, required: bool = False) -> str:
    s = "" if value is None else str(value).strip()
    if required and not s:
        raise ProfileError("bad_%s" % name, "%s is needed." % name.replace("_", " ").capitalize())
    if len(s) > maximum:
        raise ProfileError("bad_%s" % name, "%s is up to %d characters." % (name.replace("_", " ").capitalize(), maximum))
    return s


def _https(value, name: str) -> str:
    s = _text(value, name, 500)
    if s and not s.startswith("https://"):
        raise ProfileError("bad_%s" % name, "%s must start with https://" % name.replace("_", " ").capitalize())
    return s


# ── cleaning what the owner sends ────────────────────────────────────────────

def clean_profile(value) -> dict:
    v = value if isinstance(value, dict) else {}
    rules = v.get("ground_rules") or []
    if not (isinstance(rules, list) and len(rules) <= 10):
        raise ProfileError("bad_rules", "Up to 10 ground rules.")
    return {"business_name": _text(v.get("business_name"), "business_name", 80), "website": _https(v.get("website"), "website"),
            "description": _text(v.get("description"), "description", 1500), "greeting": _text(v.get("greeting"), "greeting", 300),
            "ground_rules": [_text(r, "ground_rule", 200) for r in rules if str(r or "").strip()]}


def clean_voice(value) -> dict:
    v = value if isinstance(value, dict) else {}
    tone, length = v.get("tone") or "friendly", v.get("length") or "short"
    if tone not in TONES:
        raise ProfileError("bad_tone", "Tone is one of: %s." % ", ".join(TONES))
    if length not in LENGTHS:
        raise ProfileError("bad_length", "Answer length is one of: %s." % ", ".join(LENGTHS))
    return {"tone": tone, "length": length, "language": _text(v.get("language") or "match", "language", 20).lower() or "match"}


def _slug_list(items, name: str, maximum: int) -> list:
    if not isinstance(items, list) or len(items) > maximum:
        raise ProfileError("bad_%s" % name, "Up to %d %s." % (maximum, name))
    return items


def _unique_id(item: dict, taken: set, fallback: str) -> str:
    wanted = str(item.get("id") or "").strip().lower()
    if wanted and not SLUG.match(wanted):
        raise ProfileError("bad_id", "A name for internal use starts with a letter and uses lowercase letters, numbers and underscores.")
    wanted = wanted or re.sub(r"[^a-z0-9]+", "_", str(item.get("name") or fallback).lower()).strip("_")[:30] or fallback
    if not SLUG.match(wanted):
        wanted = fallback
    out, n = wanted, 2
    while out in taken:
        out = "%s_%d" % (wanted[:26], n)
        n += 1
    taken.add(out)
    return out


def clean_skills(value) -> list:
    taken: set = set()
    out = []
    for s in _slug_list(value or [], "skills", MAX_SKILLS):
        s = s if isinstance(s, dict) else {}
        kind = s.get("kind") or "custom"
        if kind not in SKILL_KINDS:
            raise ProfileError("bad_skill", "A skill is one of: %s." % ", ".join(SKILL_KINDS))
        variables, seen = [], set()
        for v in _slug_list(s.get("variables") or [], "details a skill collects", MAX_VARIABLES):
            v = v if isinstance(v, dict) else {}
            name = str(v.get("name") or "").strip().lower()
            if not SLUG.match(name) or name in seen:
                raise ProfileError("bad_variable", "Each detail needs its own short name (letters, numbers, underscores).")
            seen.add(name)
            attr = _text(v.get("attribute"), "attribute", 40) or name
            if not ATTR.match(attr):
                raise ProfileError("bad_variable", "A contact detail name starts with a letter and uses letters, numbers and underscores.")
            variables.append({"name": name, "label": _text(v.get("label"), "label", 60) or name.replace("_", " ").capitalize(),
                              "description": _text(v.get("description"), "description", 200), "required": bool(v.get("required", True)), "attribute": attr})
        done = s.get("on_complete") if isinstance(s.get("on_complete"), dict) else {}
        out.append({"id": _unique_id(s, taken, "skill"), "kind": kind, "name": _text(s.get("name"), "name", 60, required=True),
                    "enabled": bool(s.get("enabled", True)), "when": _text(s.get("when"), "when", 400), "instructions": _text(s.get("instructions"), "instructions", 1000),
                    "variables": variables,
                    "on_complete": {"tag": _text(done.get("tag"), "tag", 40), "handoff": bool(done.get("handoff")), "reason": _text(done.get("reason"), "reason", 120)}})
    return out


def _headers(value, limit: int) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict) or len(value) > limit:
        raise ProfileError("bad_headers", "Up to %d headers." % limit)
    out = {}
    for k, v in value.items():
        k = str(k).strip()
        if not re.match(r"^[A-Za-z0-9-]{1,40}$", k):
            raise ProfileError("bad_headers", "A header name is letters, numbers and dashes.")
        out[k] = _text(v, "header", 300)
    return out


def clean_actions(value) -> list:
    taken: set = set()
    out = []
    for a in _slug_list(value or [], "actions", MAX_ACTIONS):
        a = a if isinstance(a, dict) else {}
        method = str(a.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            raise ProfileError("bad_action", "An action uses GET or POST.")
        url = _https(a.get("url"), "url")
        if not url:
            raise ProfileError("bad_url", "An action needs the web address it calls (https://...).")
        params, seen = [], set()
        for p in _slug_list(a.get("parameters") or [], "details an action needs", MAX_PARAMS):
            p = p if isinstance(p, dict) else {}
            name = str(p.get("name") or "").strip().lower()
            if not SLUG.match(name) or name in seen:
                raise ProfileError("bad_parameter", "Each detail an action needs has its own short name (letters, numbers, underscores).")
            seen.add(name)
            params.append({"name": name, "description": _text(p.get("description"), "description", 200), "required": bool(p.get("required", True))})
        out.append({"id": _unique_id(a, taken, "action"), "name": _text(a.get("name"), "name", 60, required=True), "enabled": bool(a.get("enabled", True)),
                    "description": _text(a.get("description"), "description", 300, required=True), "method": method, "url": url,
                    "headers": _headers(a.get("headers"), 10), "body": _text(a.get("body"), "body", 2000), "parameters": params,
                    "response_path": _text(a.get("response_path"), "response_path", 120)})
    return out


def clean_settings(value) -> dict:
    v = value if isinstance(value, dict) else {}
    hook = v.get("handoff_webhook") if isinstance(v.get("handoff_webhook"), dict) else {}
    hook_url = _https(hook.get("url"), "handoff_webhook")
    numbers = v.get("test_numbers") or []
    if not (isinstance(numbers, list) and len(numbers) <= 3):
        raise ProfileError("bad_test_numbers", "Up to 3 test numbers.")
    tests = []
    for n in numbers:
        digits = re.sub(r"\D", "", str(n or ""))
        if digits:
            if not 8 <= len(digits) <= 15:
                raise ProfileError("bad_test_numbers", "A test number is a full phone number with its country code.")
            tests.append(digits)
    covered = v.get("numbers") or []
    if not (isinstance(covered, list) and len(covered) <= 10):
        raise ProfileError("bad_numbers", "Up to 10 numbers.")
    for n in covered:
        try:
            uuid.UUID(str(n))
        except ValueError:
            raise ProfileError("bad_numbers", "Choose numbers from the workspace's list.") from None
    return {"fallback_text": _text(v.get("fallback_text"), "fallback_text", 400), "handoff_message": _text(v.get("handoff_message"), "handoff_message", 400),
            "handoff_webhook": {"url": hook_url, "headers": _headers(hook.get("headers"), 5)} if hook_url else None,
            "test_numbers": list(dict.fromkeys(tests)), "numbers": [str(n) for n in covered]}


CLEANERS = {"profile": clean_profile, "voice": clean_voice, "skills": clean_skills, "actions": clean_actions, "settings": clean_settings}


# ── secrets: shown masked, kept when sent back masked ────────────────────────

def mask(value: str) -> str:
    return MASK if len(value or "") <= 4 else value[:3] + MASK


def masked(config: dict) -> dict:
    """A copy of an agent's config safe to show: the values of headers hidden."""
    out = copy.deepcopy(config)
    for a in out.get("actions") or []:
        a["headers"] = {k: mask(v) for k, v in (a.get("headers") or {}).items()}
    hook = (out.get("settings") or {}).get("handoff_webhook")
    if hook:
        hook["headers"] = {k: mask(v) for k, v in (hook.get("headers") or {}).items()}
    return out


def unmask(new_actions, old_actions) -> list:
    """Actions as sent back: a header whose value is still the mask keeps what was stored for that action and header."""
    old = {a.get("id"): a for a in old_actions or []}
    out = copy.deepcopy(new_actions or [])
    for a in out:
        before = (old.get(a.get("id")) or {}).get("headers") or {}
        a["headers"] = {k: (before.get(k, v) if str(v).endswith(MASK) else v) for k, v in (a.get("headers") or {}).items()}
    return out


def unmask_settings(new_settings, old_settings) -> dict:
    out = copy.deepcopy(new_settings or {})
    hook, before = out.get("handoff_webhook"), ((old_settings or {}).get("handoff_webhook") or {}).get("headers") or {}
    if isinstance(hook, dict):
        hook["headers"] = {k: (before.get(k, v) if str(v).endswith(MASK) else v) for k, v in (hook.get("headers") or {}).items()}
    return out


# ── the prompt ───────────────────────────────────────────────────────────────

def system_prompt(agent: dict, chunks: list, known: Optional[dict], rules: str) -> str:
    """Everything the model is told before the customer's message. With only a name and instructions it reads exactly like the first version."""
    profile, voice = agent.get("profile") or {}, agent.get("voice") or {}
    skills = [s for s in agent.get("skills") or [] if s.get("enabled", True)]
    actions = [a for a in agent.get("actions") or [] if a.get("enabled", True)]
    business = profile.get("business_name") or "a business"
    parts = ["You are %s, a WhatsApp assistant for %s." % (agent["name"], business)]
    about = " ".join(x for x in (profile.get("description"), ("Website: %s." % profile["website"]) if profile.get("website") else "") if x)
    if about:
        parts.append(about)
    if voice:
        parts.append(" ".join(x for x in (TONE_SENTENCE.get(voice.get("tone")), LENGTH_SENTENCE.get(voice.get("length")),
                                          ("Always reply in %s." % voice["language"].capitalize()) if voice.get("language") not in (None, "", "match") else "") if x))
    parts.append((agent.get("instructions") or "").strip())
    if profile.get("greeting"):
        parts.append("When a chat is new and the customer only says hello, open with: %s" % profile["greeting"])
    if profile.get("ground_rules"):
        parts.append("Ground rules — never break these:\n" + "\n".join("- %s" % r for r in profile["ground_rules"]))
    if skills:
        lines = []
        for s in skills:
            line = "- %s%s%s" % (s["name"], (": use when %s." % s["when"].rstrip(".")) if s.get("when") else "", (" " + s["instructions"]) if s.get("instructions") else "")
            if s.get("variables"):
                line += " Ask for, one or two at a time, and remember: " + "; ".join(
                    "%s [%s]%s%s" % (v["label"], v["name"], " (needed)" if v.get("required") else "", (" — " + v["description"]) if v.get("description") else "") for v in s["variables"])
            lines.append(line)
        parts.append("Things you can do:\n" + "\n".join(lines))
    if actions:
        parts.append("Things you can look up or do for the customer (use one only when it is really needed, and only with details the customer has given):\n" + "\n".join(
            "- %s [%s]: %s%s" % (a["name"], a["id"], a["description"], (" Needs: " + ", ".join(p["name"] for p in a["parameters"])) if a.get("parameters") else "") for a in actions))
    if known:
        parts.append("Already known about this customer (do not ask again):\n" + "\n".join("- %s: %s" % (k, v) for k, v in known.items()))
    extra = []
    if any(s.get("variables") for s in skills):
        extra.append('When the customer gives you any of the details above, put them in "collected" as {"<name in brackets>": "<value>"}.')
    if actions:
        extra.append('To use a thing you can look up or do, answer {"action": {"id": "<id in brackets>", "args": {"<detail>": "<value>"}}, "reply": ""} and wait for its result; '
                     "never invent a result.")
    knowledge = "\n\n".join("[%d] %s" % (i + 1, c["text"]) for i, c in enumerate(chunks)) or "(nothing relevant was found)"
    return "%s\n\n%s\n\nKNOWLEDGE:\n%s" % ("\n".join(p for p in parts if p), rules + ("\n" + "\n".join(extra) if extra else ""), knowledge)


# ── details a skill collects ─────────────────────────────────────────────────

def variables_of(agent: dict) -> dict:
    """{name: variable} over the enabled skills."""
    out = {}
    for s in agent.get("skills") or []:
        if s.get("enabled", True):
            for v in s.get("variables") or []:
                out.setdefault(v["name"], {**v, "skill": s["id"]})
    return out


def known_values(cur, tenant_id, contact_id, agent: dict) -> dict:
    """What the contact already has for the details this agent collects, as {label: value}."""
    wanted = variables_of(agent)
    if not wanted or not contact_id:
        return {}
    cur.execute("select key, value from contact_attributes where contact_id = %s and key = any(%s)", (contact_id, [v["attribute"] for v in wanted.values()]))
    have = {k: v for k, v in cur.fetchall()}
    return {v["label"]: have[v["attribute"]] for v in wanted.values() if str(have.get(v["attribute"]) or "").strip()}


def save_collected(cur, tenant_id, contact_id, agent: dict, collected) -> dict:
    """Keep what the customer told the agent, on the contact. -> {attribute: value} for those saved. Unknown names and values the attribute refuses are skipped."""
    wanted = variables_of(agent)
    saved = {}
    if not isinstance(collected, dict) or not contact_id:
        return saved
    defs = contacts.attribute_defs(cur, tenant_id)
    for name, value in collected.items():
        v = wanted.get(str(name).strip().lower())
        if v is None or not str(value or "").strip():
            continue
        try:
            if v["attribute"] not in defs:
                contacts.define_attribute(cur, tenant_id, v["attribute"], "string")
                defs = contacts.attribute_defs(cur, tenant_id)
            contacts.set_attribute(cur, tenant_id, contact_id, v["attribute"], str(value).strip()[:500], defs)
            saved[v["attribute"]] = str(value).strip()[:500]
        except (plans.PlanLimit, ValueError):
            continue
    return saved


def finished_skills(cur, tenant_id, contact_id, agent: dict, just_saved: dict) -> list:
    """The skills whose last needed detail was given just now: those with every required detail on the contact and at least one of them in `just_saved`."""
    out = []
    for s in agent.get("skills") or []:
        needed = [v for v in s.get("variables") or [] if v.get("required")]
        if not s.get("enabled", True) or not needed or not (s.get("on_complete") or {}).get("tag") and not (s.get("on_complete") or {}).get("handoff"):
            continue
        if not any(v["attribute"] in just_saved for v in s["variables"]):
            continue
        cur.execute("select count(*) from contact_attributes where contact_id = %s and key = any(%s) and coalesce(value::text, '') not in ('', '\"\"', 'null')",
                    (contact_id, [v["attribute"] for v in needed]))
        if cur.fetchone()[0] >= len(needed):
            out.append(s)
    return out


def add_tag(cur, tenant_id, contact_id, tag: str) -> None:
    cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t) where id = %s", ([tag], contact_id))


# ── a draft of the profile from the business's own website ───────────────────

DRAFT_PROMPT = ('Read the text of a business website and answer JSON only: {"business_name": "<name>", "description": "<what the business does and for whom, '
                'two or three sentences, under 400 characters>", "greeting": "<a warm one-sentence hello a WhatsApp assistant would open with>", '
                '"ground_rules": ["<up to 4 short rules such as what it must never promise>"]}. Use only what the text says; if something is not there, leave it "".')


def draft_from_page(llm, text: str, url: str) -> tuple:
    """-> (profile draft, tokens). The model is asked for JSON; whatever it sends is cleaned like anything the owner types."""
    raw, tokens = llm.chat([{"role": "system", "content": DRAFT_PROMPT}, {"role": "user", "content": "Website: %s\n\n%s" % (url, text[:6000])}], json_mode=True, max_tokens=600)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        data = json.loads(cleaned)
    except ValueError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        data = json.loads(m.group(0)) if m else {}
    if not isinstance(data, dict):
        data = {}
    rules = [str(r).strip()[:200] for r in (data.get("ground_rules") or []) if str(r or "").strip()][:4] if isinstance(data.get("ground_rules"), list) else []
    return {"business_name": str(data.get("business_name") or "").strip()[:80], "website": url, "description": str(data.get("description") or "").strip()[:1500],
            "greeting": str(data.get("greeting") or "").strip()[:300], "ground_rules": rules}, tokens


def fetch_page_text(fetch, url: str) -> str:
    """The readable text of one https page, or ProfileError saying why not."""
    from prism_wa import ai_agent                                   # the page reader lives next to the knowledge code
    if not url.startswith("https://"):
        raise ProfileError("bad_url", "Give a web address starting with https://")
    try:
        status, markup = fetch("GET", url, {}, None)
    except flow_engine.FlowRuntimeError as why:
        raise ProfileError("fetch_failed", str(why)) from why
    if status >= 400:
        raise ProfileError("fetch_failed", "That page answered with an error (%s)." % status)
    text = ai_agent.page_text(markup)
    if len(text) < 40:
        raise ProfileError("empty", "Nothing readable was found on that page.")
    return text
