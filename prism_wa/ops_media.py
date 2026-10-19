"""Routes for the Media Library and for sending a file in a chat.

    media      upload  list  get  delete  usage
    messages   send_media
    inbox      canned/save (with a file), canned/expand (words + the file)

Files travel between the app and this service as base64 inside JSON, up to 16 MB each. The licence server forwards them untouched.
"""
from __future__ import annotations

import base64
import binascii

from prism_wa import inbox, inbox_view, media
from prism_wa.ops_common import jsonable, managers_only, need

GET_CAP = 12 * media.MB                       # the most one `media/get` hands back (a thumbnail, a voice note, a PDF to open)


def _actor(cur, t, d):
    who = d.get("by_agent_id") or d.get("agent_id")
    if not who:
        return None
    try:
        return inbox._agent(cur, t, who)
    except inbox.InboxError:
        return None


def media_upload(cur, t, d):
    need(d, "filename", "data")
    try:
        data = base64.b64decode(d["data"], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the file did not arrive intact (it is not valid base64)") from None
    actor = _actor(cur, t, d)
    asset = media.upload(cur, t, filename=d["filename"], mime=d.get("mime") or "", data=data, source=d.get("source") or "upload",
                         actor_id=actor["id"] if actor else None)
    return {"asset": jsonable(asset), "quota": media.quota(cur, t)}


def media_list(cur, t, d):
    return jsonable(media.list_assets(cur, t, kind=d.get("kind") or None, search=d.get("search") or "", limit=int(d.get("limit") or 60),
                                      offset=int(d.get("offset") or 0)))


def media_get(cur, t, d):
    need(d, "asset_id")
    asset, data = media.read(cur, t, d["asset_id"])
    limit = min(int(d.get("max_bytes") or GET_CAP), GET_CAP)
    if len(data) > limit:
        return {"asset": jsonable(asset), "data": None, "too_big": True}
    return {"asset": jsonable(asset), "data": base64.b64encode(data).decode("ascii"), "too_big": False}


def media_delete(cur, t, d):
    need(d, "asset_id")
    managers_only(cur, t, d, "delete files from the library")
    return {"deleted": media.delete(cur, t, d["asset_id"])}


def media_usage(cur, t, d):
    return {"quota": media.quota(cur, t)}


def messages_send_media(cur, t, d):
    need(d, "asset_id")
    if not (d.get("conversation_id") or d.get("contact_id")):
        raise ValueError("missing: conversation_id")
    who = d.get("agent_id") or d.get("by_agent_id")
    if not who:
        raise ValueError("missing: agent_id")
    return inbox_view.send_media(cur, t, inbox._agent(cur, t, who), conversation_id=d.get("conversation_id"), contact_id=d.get("contact_id"),
                                 asset_id=d["asset_id"], caption=d.get("caption") or "", client_ref=str(d.get("client_ref") or ""))


def inbox_canned_save(cur, t, d):
    need(d, "shortcut")
    inbox.save_canned(cur, t, d["shortcut"], d.get("body") or "", d.get("agent_id") or d.get("by_agent_id"), d.get("asset_id") or None)
    return {}


def inbox_canned_expand(cur, t, d):
    need(d, "shortcut", "contact_id")
    out = inbox.expand_full(cur, t, d["shortcut"], d["contact_id"])
    return {"text": out["text"], "asset": jsonable(out["asset"])}


OPS = {"/internal/media/upload": media_upload, "/internal/media/list": media_list, "/internal/media/get": media_get,
       "/internal/media/delete": media_delete, "/internal/media/usage": media_usage, "/internal/messages/send_media": messages_send_media,
       "/internal/inbox/canned/save": inbox_canned_save, "/internal/inbox/canned/expand": inbox_canned_expand}
