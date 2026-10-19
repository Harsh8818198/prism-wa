"""Small helpers every ops module shares (kept apart so ops.py and the newer ops_*.py files can both use them)."""
from __future__ import annotations

import datetime as _dt
import uuid

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
