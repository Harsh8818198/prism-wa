"""Routes for messages a person sends by hand.

    messages   send_template
"""
from __future__ import annotations

from prism_wa import chat_send, inbox, inbox_view, send
from prism_wa.ops_common import jsonable, need


def _caller(cur, t, d) -> dict:
    """The person asking (the licence server puts their id in `agent_id` for inbox routes, `by_agent_id` elsewhere)."""
    who = d.get("agent_id") or d.get("by_agent_id")
    if not who:
        raise ValueError("missing: agent_id")
    return inbox._agent(cur, t, who)


def inbox_threads(cur, t, d):
    """The chats this person may see, with the counts for the state tabs. `state`, `window` (open|closed), `search`, `filter`."""
    return jsonable(inbox_view.list_threads(cur, t, _caller(cur, t, d), state=d.get("state") or None, window=d.get("window") or None,
                                            search=d.get("search") or "", filter_=d.get("filter"), limit=int(d.get("limit") or 100),
                                            offset=int(d.get("offset") or 0)))


def inbox_thread(cur, t, d):
    need(d, "conversation_id")
    return jsonable(inbox_view.thread(cur, t, _caller(cur, t, d), d["conversation_id"], limit=int(d.get("limit") or 200)))


def messages_send_text(cur, t, d):
    need(d, "text")
    if not (d.get("conversation_id") or d.get("contact_id")):
        raise ValueError("missing: conversation_id")
    return inbox_view.send_text(cur, t, _caller(cur, t, d), conversation_id=d.get("conversation_id"), contact_id=d.get("contact_id"),
                                text=d["text"], client_ref=str(d.get("client_ref") or ""))


def messages_send_template(cur, t, d):
    """One approved template to one contact — the way to start (or restart) a chat once the 24-hour window has closed."""
    need(d, "contact_id", "template_name")
    sender = "agent"
    by = None
    if d.get("by_agent_id"):
        try:
            by = inbox._agent(cur, t, d["by_agent_id"])
        except inbox.InboxError:
            pass
    out = chat_send.send_template(cur, t, d["contact_id"], name=d["template_name"], language=d.get("language") or "en_US",
                                   body=d.get("body") or (), header=d.get("header") or (), header_link=d.get("header_link") or "", header_asset_id=d.get("header_asset_id") or "",
                                   buttons=d.get("buttons"), number_id=d.get("number_id"), sender=sender)
    if by and out.get("outbox_id"):
        send.attribute(cur, out["outbox_id"], by["id"])
    return out


OPS = {"/internal/messages/send_template": messages_send_template, "/internal/messages/send_text": messages_send_text,
       "/internal/inbox/threads": inbox_threads, "/internal/inbox/thread": inbox_thread}
