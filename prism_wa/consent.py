"""Who has agreed to be messaged, and the proof (spec WS5).

`contacts.opt_out` / `opted_in` are the current answer; `consent_ledger` is the
history behind it: every change with its source and the exact words or file that
caused it. A business has to be able to show why it messaged someone, and that it
stopped when asked.

Rules this module holds to:
  * an opt-out always wins and always takes effect at once;
  * an import or a form can never quietly undo a person's opt-out — only that
    person (a START keyword, a button) or a deliberate manual action can;
  * keywords match the WHOLE message ("stop"), never a word inside a sentence
    ("please stop by tomorrow" is a customer talking, not an opt-out).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from psycopg2.extras import Json

MAX_KEYWORDS = 5
KEYWORD_RE = re.compile(r"^[a-z0-9][a-z0-9 ]{0,29}$")
# Quick-reply buttons on our own templates carry these ids.
OPT_OUT_PAYLOADS = {"optout", "opt_out", "stop"}

DEFAULTS = {
    "opt_out_keywords": ["stop", "unsubscribe", "cancel"],
    "opt_in_keywords": ["start", "subscribe"],
    "confirm": True,
    "opt_out_reply": "You have been unsubscribed and will not receive further messages from us. "
                     "Reply START to subscribe again.",
    "opt_in_reply": "Thank you, you are subscribed again. Reply STOP at any time to opt out.",
}


class SettingsError(ValueError):
    pass


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def settings(cur, tenant_id) -> dict:
    cur.execute("select settings from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    stored = (row[0] if row else None) or {}
    return {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}


def save_settings(cur, tenant_id, patch: dict) -> dict:
    """Validate and store keyword settings. Raises SettingsError with a message a person can act on."""
    current = settings(cur, tenant_id)
    new = {**current, **{k: v for k, v in (patch or {}).items() if k in DEFAULTS}}
    for key in ("opt_out_keywords", "opt_in_keywords"):
        words = [_clean(w) for w in (new[key] or [])]
        if not words or len(words) > MAX_KEYWORDS:
            raise SettingsError("Choose between 1 and %d keywords." % MAX_KEYWORDS)
        if any(not KEYWORD_RE.match(w) for w in words) or len(set(words)) != len(words):
            raise SettingsError("Keywords must be short, different, and use letters and numbers only.")
        new[key] = words
    if set(new["opt_out_keywords"]) & set(new["opt_in_keywords"]):
        raise SettingsError("A word cannot both opt a person in and out.")
    for key in ("opt_out_reply", "opt_in_reply"):
        if not isinstance(new[key], str) or not new[key].strip() or len(new[key]) > 1000:
            raise SettingsError("The confirmation message must be between 1 and 1000 characters.")
    new["confirm"] = bool(new["confirm"])
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, %s)"
                " on conflict (tenant_id) do update set settings = tenant_settings.settings || excluded.settings,"
                " updated_at = now()", (tenant_id, Json({k: new[k] for k in DEFAULTS})))
    return new


def classify(msg_type: str, text: str, payload_id: str, cfg: dict) -> Optional[str]:
    """'opt_out', 'opt_in' or None for an inbound message."""
    if msg_type == "button" and (payload_id or "").strip().lower() in OPT_OUT_PAYLOADS:
        return "opt_out"
    if msg_type not in ("text", "button"):
        return None
    said = _clean(text)
    if not said:
        return None
    if said in cfg["opt_out_keywords"]:
        return "opt_out"
    if said in cfg["opt_in_keywords"]:
        return "opt_in"
    return None


def record(cur, tenant_id, contact_id, action: str, source: str, evidence: str = "",
           at: Optional[datetime] = None, *, by_person: bool = True) -> bool:
    """Write the ledger row and set the contact's current state. Returns whether
    the state changed.

    `by_person=False` marks an automated source (an import, a form) that may opt
    someone IN but must never override an existing opt-out."""
    if action not in ("opt_in", "opt_out"):
        raise ValueError(action)
    cur.execute("select opt_out, opted_in from contacts where id = %s and tenant_id = %s for update",
                (contact_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        return False
    opted_out, opted_in = row
    if action == "opt_in" and opted_out and not by_person:
        return False
    cur.execute("insert into consent_ledger (tenant_id, contact_id, action, source, evidence, at)"
                " values (%s, %s, %s, %s, %s, coalesce(%s, now()))",
                (tenant_id, contact_id, action, source, (evidence or "")[:500] or None, at))
    if action == "opt_out":
        cur.execute("update contacts set opt_out = true, opted_in = false where id = %s", (contact_id,))
        changed = not opted_out
    else:
        cur.execute("update contacts set opt_out = false, opted_in = true where id = %s", (contact_id,))
        changed = opted_out or not opted_in
    if changed:
        from prism_wa import webhooks_out
        webhooks_out.emit(cur, tenant_id, "contact.opt_out" if action == "opt_out" else "contact.opt_in",
                          lambda: {"contact_id": str(contact_id), "source": source, "evidence": (evidence or "")[:200] or None})
    return changed
