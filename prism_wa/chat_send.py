"""Messages a person sends by hand, one at a time: a template to one contact (from the Inbox's History or the profile).

A campaign builds the same message for many people through `campaigns.build_components`; here a person fills the
blanks themselves, so the values arrive ready — body values in order, header values, a header file link, URL-button endings.
Everything goes through `send.enqueue_*`, so the opt-out, 24-hour window, rate limits and the at-most-once outbox apply
exactly as they do to a campaign.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from prism_wa import campaigns, send, templates

VARIABLE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


class ChatSendError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _template(cur, tenant_id, name: str, language: str) -> Optional[dict]:
    cur.execute("select components, status from templates where tenant_id = %s and name = %s and language = %s", (tenant_id, name, language))
    row = cur.fetchone()
    return {"components": row[0], "status": row[1]} if row else None


def components_for(template_components: list, *, body=(), header=(), header_link: str = "", buttons: Optional[dict] = None, header_asset_id: str = "") -> list:
    """The `components` array Meta wants, from what a person typed. Raises ChatSendError when a blank was left."""
    used = campaigns._template_variables(template_components)                       # noqa: SLF001 (one place knows the template's shape)
    out = []
    if used["header_media"]:
        link = (header_link or "").strip()
        kind = used["header_media"]
        if header_asset_id:                                              # a Media Library file: uploaded to Meta when it goes out
            out.append({"type": "header", "parameters": [{"type": kind, kind: {"asset_id": str(header_asset_id)}}]})
        elif link.startswith(("http://", "https://")):
            out.append({"type": "header", "parameters": [{"type": kind, kind: {"link": link}}]})
        else:
            raise ChatSendError("missing_media", "This template starts with a %s. Choose the file to send." % kind)
    elif used["header"]:
        vals = [str(v).strip() for v in header]
        if len(vals) != len(used["header"]) or not all(vals):
            raise ChatSendError("missing_value", "Fill in the value for the header.")
        out.append({"type": "header", "parameters": [{"type": "text", "text": v} for v in vals]})
    if used["body"]:
        vals = [str(v).strip() for v in body]
        if len(vals) != len(used["body"]) or not all(vals):
            raise ChatSendError("missing_value", "Fill in all %d values for the message." % len(used["body"]))
        out.append({"type": "body", "parameters": [{"type": "text", "text": v} for v in vals]})
    for i in used["url_buttons"]:
        tail = str((buttons or {}).get(str(i)) or "").strip()
        if not tail:
            raise ChatSendError("missing_value", "Fill in the end of the link for button %d." % (i + 1))
        out.append({"type": "button", "sub_type": "url", "index": str(i), "parameters": [{"type": "text", "text": tail}]})
    n = 0
    for c in template_components or []:
        if c.get("type") == "BUTTONS":
            for i, b in enumerate(c.get("buttons") or []):
                if b.get("type") == "QUICK_REPLY":
                    out.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                                "parameters": [{"type": "payload", "payload": "direct:%d" % n}]})
                    n += 1
    return out


def send_template(cur, tenant_id, contact_id, *, name: str, language: str = "en_US", body=(), header=(), header_link: str = "",
                  buttons: Optional[dict] = None, number_id=None, sender: str = "agent", now=None, header_asset_id: str = "") -> dict:
    tpl = _template(cur, tenant_id, name, language)
    if tpl is None:
        raise ChatSendError("unknown_template", "That template is not known for this account. Update your templates from Meta first.")
    if tpl["status"] != "APPROVED":
        raise ChatSendError("template_not_approved", "The template is %s; only an approved template can be sent." % str(tpl["status"]).lower())
    comps = components_for(tpl["components"], body=body, header=header, header_link=header_link, buttons=buttons, header_asset_id=header_asset_id)
    sent = send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=name, language=language, components=comps,
                                 idem_key="direct:%s" % uuid.uuid4(), number_id=number_id, sender=sender, now=now)
    return {"queued": True, "outbox_id": str(sent)}
