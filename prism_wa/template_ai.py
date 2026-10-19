"""Writing templates with the language model, and helping with one Meta rejected.

    generate()   "describe what you want to send" -> a few ready-to-submit templates, each already checked against Meta's rules
    rewrite()    a rejected template -> what Meta most likely objected to (from its reason, no model needed) and, when the model is
                 set up, a rewritten version that has been checked the same way

The model is only asked to WRITE. Everything it returns is re-built into components here and run through `templates.lint`, so a
suggestion that breaks a rule is shown with the problems it has, never submitted. Nothing here talks to Meta.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from prism_wa import templates
from prism_wa.llm import LLM, LLMError

GOALS = {"replies": "get the customer to reply", "clicks": "get the customer to tap a link", "reads": "be read and understood quickly",
         "conversions": "move the customer to buy or book"}
TONES = ("friendly", "formal", "urgent", "playful", "warm")

# Meta's reasons for refusing a template, and what to change. The wording is ours; the reasons are Meta's documented ones.
REJECTION_HINTS = {
    "INVALID_FORMAT": "Meta found the format wrong. Check that the variables are {{1}}, {{2}} in order with a sample for each, that none "
                      "sits at the very start or end of the message, and that nothing has extra spaces or odd characters in it.",
    "TAG_CONTENT_MISMATCH": "The words do not match the category you chose. A Utility message must be about something the customer is "
                            "already doing (an order, a booking, a payment) and must not sell. If it reads as a promotion, make it Marketing.",
    "PROMOTIONAL": "Meta read this as a promotion. Make the category Marketing, or take the offer wording out.",
    "ABUSIVE_CONTENT": "Meta found language it treats as abusive or threatening. Make the tone calm and plain, and remove any pressure.",
    "SCAM": "Meta thinks this looks like a scam. Remove urgency, requests for money or codes, and prizes. Name your business clearly.",
    "INCORRECT_CATEGORY": "Meta thinks the category is wrong for what the message says. Change the category to match the content.",
    "NONE": "Meta gave no reason. The usual causes are variables at the start or end, too many variables for the number of words, "
            "or a category that does not match the content.",
}
_NAME = re.compile(r"[^a-z0-9]+")


class AIUnavailable(LookupError):
    pass


def _need(llm: Optional[LLM]) -> LLM:
    if llm is None:
        raise AIUnavailable("AI writing is not set up on this server (WA_LLM_API_KEY).")
    return llm


def _snake(text: str, fallback: str) -> str:
    return (_NAME.sub("_", (text or "").lower()).strip("_") or fallback)[:60]


def _components(item: dict) -> list:
    """The model's plain description -> Meta's components."""
    comps = []
    header = (item.get("header") or "").strip()
    if header:
        part = {"type": "HEADER", "format": "TEXT", "text": header}
        if "{{1}}" in header:
            part["example"] = {"header_text": [str(item.get("header_sample") or "Customer")]}
        comps.append(part)
    body = (item.get("body") or "").strip()
    variables = re.findall(r"\{\{\s*(\d+)\s*\}\}", body)
    samples = [str(x) for x in (item.get("samples") or [])]
    part = {"type": "BODY", "text": body}
    if variables:
        part["example"] = {"body_text": [samples[:len(variables)] + [""] * max(0, len(variables) - len(samples))]}
    comps.append(part)
    footer = (item.get("footer") or "").strip()
    if footer:
        comps.append({"type": "FOOTER", "text": footer})
    buttons = []
    for b in (item.get("buttons") or [])[:3]:
        if not isinstance(b, dict) or not (b.get("text") or "").strip():
            continue
        kind = str(b.get("type") or "QUICK_REPLY").upper()
        if kind == "URL" and str(b.get("url") or "").startswith(("http://", "https://")):
            part = {"type": "URL", "text": b["text"].strip()[:25], "url": b["url"].strip()}
            if "{{1}}" in part["url"]:
                part["example"] = [part["url"].replace("{{1}}", "sample")]
            buttons.append(part)
        elif kind == "PHONE_NUMBER" and re.match(r"^\+\d{6,15}$", str(b.get("phone_number") or "")):
            buttons.append({"type": "PHONE_NUMBER", "text": b["text"].strip()[:25], "phone_number": b["phone_number"]})
        else:
            buttons.append({"type": "QUICK_REPLY", "text": b["text"].strip()[:25]})
    if buttons:
        comps.append({"type": "BUTTONS", "buttons": buttons})
    return comps


def _checked(item: dict, *, language: str, forced_category: str = "") -> dict:
    category = forced_category or str(item.get("category") or "").upper()
    if category not in templates.CATEGORIES or category == "AUTHENTICATION":
        category = "MARKETING" if "offer" in (item.get("body") or "").lower() else "UTILITY"
    name = _snake(item.get("name"), "new_template")
    comps = _components(item)
    issues = templates.lint(name=name, language=language, category=category, components=comps)
    return {"name": name, "language": language, "category": category, "components": comps,
            "issues": [i.as_dict() for i in issues], "valid": not templates.errors(issues)}


def _json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        out = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("the language model did not answer in the expected form") from None
        try:
            out = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("the language model did not answer in the expected form") from None
    if not isinstance(out, dict):
        raise LLMError("the language model did not answer in the expected form")
    return out


RULES = (
    "You write WhatsApp message templates for Indian small businesses, to be approved by Meta. Rules you must keep:\n"
    "- Body at most 1024 characters, plain text, no markdown, no emoji at the very start.\n"
    "- Variables are {{1}}, {{2}}, ... in order, each used once. A variable must NEVER be the first or last thing in the body. "
    "Use few variables and plenty of fixed words around each (at least three words per variable).\n"
    "- Give a realistic sample value for every variable, in the same order, in \"samples\".\n"
    "- UTILITY is for something the customer already did (order, booking, payment, an update). It must not sell or promote. "
    "MARKETING is for offers, news and promotion. Pick the category that is honest for the content.\n"
    "- Header at most 60 characters and one variable at most (optional). Footer at most 60 characters, no variables (optional).\n"
    "- Up to three buttons: QUICK_REPLY (text), URL (text and a full https url, optionally ending in {{1}}), PHONE_NUMBER (text and +91 number). "
    "Button text at most 25 characters.\n"
    "- For marketing messages, offer a quick reply that lets the customer stop receiving messages.\n"
    "Answer with JSON only: {\"templates\": [{\"name\": snake_case, \"category\": \"UTILITY\"|\"MARKETING\", \"header\": str|null, "
    "\"body\": str, \"samples\": [str], \"footer\": str|null, \"buttons\": [{\"type\": ..., \"text\": ..., \"url\"?: ..., \"phone_number\"?: ...}]}]}"
)


def generate(llm: Optional[LLM], *, description: str, category: str = "", tone: str = "friendly", goal: str = "replies",
             language: str = "en_US", count: int = 3, industry: str = "") -> dict:
    llm = _need(llm)
    description = (description or "").strip()
    if len(description) < 8:
        raise ValueError("describe what the message is for, in a sentence")
    if len(description) > 600:
        raise ValueError("keep the description under 600 characters")
    category = (category or "").upper()
    if category and category not in ("UTILITY", "MARKETING"):
        raise ValueError("the category is Utility or Marketing (or leave it blank and we will choose)")
    count = max(1, min(int(count or 3), 5))
    ask = ("Write %d different templates for this. What it is for: %s\nBusiness: %s\nTone: %s\nGoal: the message should %s.\nLanguage: %s.\n%s" % (
        count, description, industry or "a small business in India", tone if tone in TONES else "friendly", GOALS.get(goal, GOALS["replies"]),
        language, ("Category: %s." % category) if category else "Choose the honest category for each."))
    text, usage = llm.chat([{"role": "system", "content": RULES}, {"role": "user", "content": ask}], max_tokens=1800, temperature=0.7, json_mode=True)
    items = _json(text).get("templates")
    if not isinstance(items, list) or not items:
        raise LLMError("the language model sent no templates")
    out = [_checked(i, language=language, forced_category=category) for i in items[:count] if isinstance(i, dict) and (i.get("body") or "").strip()]
    if not out:
        raise LLMError("the language model sent no usable templates")
    seen = set()
    for t in out:                                        # two suggestions must not share a name
        while t["name"] in seen:
            t["name"] += "_2"
        seen.add(t["name"])
    return {"templates": out, "tokens": usage}


def hints_for(reason: Optional[str]) -> list:
    reason = (reason or "NONE").upper()
    return [REJECTION_HINTS.get(reason, REJECTION_HINTS["NONE"])]


def rewrite(llm: Optional[LLM], template: dict) -> dict:
    """`template`: name, language, category, status, reject_reason, components. -> {hints, suggestion|None, reason}."""
    reason = (template.get("reject_reason") or "NONE").upper()
    out = {"reason": reason, "hints": hints_for(reason), "suggestion": None, "ai": llm is not None}
    if llm is None:
        return out
    fields = {"body": "", "header": "", "footer": "", "buttons": []}
    for c in template.get("components") or []:
        if c.get("type") == "BODY":
            fields["body"] = c.get("text") or ""
        elif c.get("type") == "HEADER" and c.get("format") == "TEXT":
            fields["header"] = c.get("text") or ""
        elif c.get("type") == "FOOTER":
            fields["footer"] = c.get("text") or ""
        elif c.get("type") == "BUTTONS":
            fields["buttons"] = [{"type": b.get("type"), "text": b.get("text"), "url": b.get("url"), "phone_number": b.get("phone_number")}
                                 for b in c.get("buttons") or [] if isinstance(b, dict)]
    ask = ("Meta rejected this template (reason: %s). Category it was submitted as: %s.\nCurrent template: %s\n"
           "Rewrite it once so it keeps the same meaning but fixes the likely problem. %s" % (
               reason, template.get("category"), json.dumps(fields, ensure_ascii=False), out["hints"][0]))
    try:
        text, usage = llm.chat([{"role": "system", "content": RULES}, {"role": "user", "content": ask}], max_tokens=900, temperature=0.4, json_mode=True)
        items = _json(text).get("templates")
        one = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else _json(text)
        one = {**one, "name": template.get("name")}
        out["suggestion"] = _checked(one, language=template.get("language") or "en_US")
        out["suggestion"]["name"] = template.get("name")
    except (LLMError, KeyError, ValueError):
        out["suggestion"] = None                         # the hints still stand
    return out
