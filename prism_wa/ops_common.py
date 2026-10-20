"""Small helpers every ops module shares (kept apart so ops.py and the newer ops_*.py files can both use them)."""
from __future__ import annotations

import datetime as _dt
import uuid

from fastapi.responses import JSONResponse

from prism_wa import inbox


def jsonable(value):
    """UUIDs and datetimes -> strings, so a dict can be returned as JSON."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value


def need(d: dict, *keys) -> None:
    missing = [k for k in keys if d.get(k) in (None, "")]
    if missing:
        raise ValueError("missing: %s" % ", ".join(missing))


def managers_only(cur, t, d, what: str) -> None:
    if d.get("by_agent_id") and inbox._agent(cur, t, d["by_agent_id"])["role"] == "agent":
        raise inbox.InboxError("not_allowed", "only an owner or manager can %s" % what)


DRAIN_LIMIT = 64 * 1024


async def refuse(request) -> JSONResponse:
    """The 401 for a caller who does not hold the internal token.

    The body the caller is still sending is read first (up to a small cap, so a refusal never costs us a large upload). Answering and closing while a
    client is mid-write makes it see a reset instead of the answer, and then the licence server reports "could not reach the WhatsApp service" for what
    is really a wrong shared token: the one mistake this answer exists to point at."""
    seen = 0
    try:
        async for chunk in request.stream():
            seen += len(chunk)
            if seen > DRAIN_LIMIT:
                break
    except Exception:                                  # noqa: BLE001  (a client that went away mid-body still gets refused)
        pass
    return JSONResponse({"ok": False, "error": "unauthorised"}, status_code=401)
