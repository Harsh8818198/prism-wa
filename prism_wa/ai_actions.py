"""Things an AI agent can DO: call the owner's own web addresses (order status, free slots, a booking), and tell the owner's system when it hands a chat over.

An action is the owner's definition — a name, what it is for, GET or POST, an https address, headers, a body, and the details the agent must have before it
calls it. The agent never makes up an address: it can only name an action by its id and give values for its details, and the call is made here, by the same
guarded HTTP every flow uses (https only, no private addresses). The answer is cut down and shown to the model, which writes the reply.

In a test an action runs only if it only reads (GET): a booking must not be made by trying the agent out.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional
from urllib.parse import quote

from prism_wa import flow_engine

log = logging.getLogger("prism_wa.ai")

MAX_RESULT = 1500
TOKEN = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def _fill(text: str, values: dict, *, quoted: bool = False) -> str:
    return TOKEN.sub(lambda m: (quote(str(values.get(m.group(1), "")), safe="") if quoted else str(values.get(m.group(1), ""))), text or "")


def json_path(data, path: str):
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


def read_only(action: dict) -> bool:
    return str(action.get("method") or "GET").upper() == "GET"


def run(action: dict, args: dict, *, fetch: Callable, contact: Optional[dict] = None, allow_changes: bool = True) -> dict:
    """Make the call. -> {"ran": bool, "ok": bool, "status": int, "text": str, "error": str}. Never raises."""
    args = {str(k): str(v) for k, v in (args or {}).items()}
    missing = [p["name"] for p in action.get("parameters") or [] if p.get("required", True) and not args.get(p["name"], "").strip()]
    if missing:
        return {"ran": False, "ok": False, "status": 0, "text": "", "error": "missing: %s" % ", ".join(missing)}
    if not allow_changes and not read_only(action):
        return {"ran": False, "ok": True, "status": 0, "text": "(not run in a test: it would change something)", "error": ""}
    values = {**args, "name": (contact or {}).get("name") or "", "mobile": (contact or {}).get("phone") or ""}
    url = _fill(action["url"], values, quoted=True)
    headers = {k: _fill(str(v), values) for k, v in (action.get("headers") or {}).items()}
    body = _fill(action["body"], values) if action.get("body") else None
    try:
        status, text = fetch(action.get("method", "GET"), url, headers, body)
    except flow_engine.FlowRuntimeError as why:
        return {"ran": True, "ok": False, "status": 0, "text": "", "error": str(why)[:200]}
    ok = 200 <= int(status) < 300
    shown = text or ""
    if ok and action.get("response_path"):
        try:
            found = json_path(json.loads(shown), action["response_path"])
            if found is not None:
                shown = found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)
        except ValueError:
            pass
    return {"ran": True, "ok": ok, "status": int(status), "text": shown[:MAX_RESULT], "error": "" if ok else "it answered %s" % status}


def result_message(action: dict, result: dict) -> str:
    """What the model is told after the call, so it can answer the customer."""
    if result.get("error") and not result.get("ran"):
        said = "could not be run (%s)" % result["error"]
    elif not result.get("ok"):
        said = "failed (%s)" % (result.get("error") or "no answer")
    else:
        said = "returned: %s" % (result.get("text") or "(nothing)")
    return ('ACTION RESULT — %s %s\nNow write your reply to the customer as JSON. Use only this result. If it failed or says nothing useful, '
            'say you will ask a colleague and set "handoff" to true. Do not call another action.' % (action["name"], said))


def notify_handoff(fetch: Callable, hook: Optional[dict], payload: dict) -> bool:
    """Tell the owner's system a chat was handed to a person. Best effort: a hook that is down never stops the hand-over."""
    if not hook or not hook.get("url"):
        return False
    headers = {"Content-Type": "application/json", **{k: str(v) for k, v in (hook.get("headers") or {}).items()}}
    try:
        status, _ = fetch("POST", hook["url"], headers, json.dumps(payload, ensure_ascii=False, default=str))
    except flow_engine.FlowRuntimeError as why:
        log.info("hand-over webhook not delivered: %s", why)
        return False
    return 200 <= int(status) < 300
