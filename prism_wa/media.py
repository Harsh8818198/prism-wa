"""The Media Library: files a workspace holds, how they get to WhatsApp, and how a customer's picture gets here.

    upload / list / read / delete   the library itself (checked against the plan's storage and WhatsApp's own limits)
    quota                           what is used and what the plan allows
    resolve_payload                 at send time: a message that names one of OUR files (`asset_id`) is given Meta's id for it,
                                    uploading it first if Meta has not got a fresh copy
    fetch_inbound                   when a customer sends a picture, video, voice note or document: pull it from Meta at once
                                    (their link expires in minutes) and keep our own copy

A file is stored once per workspace (same bytes, same kind = same file), so sending the same picture to ten thousand people
uploads it to Meta once, not ten thousand times.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from prism_wa import plans
from prism_wa.graph import Graph, GraphError
from prism_wa.media_store import MediaStoreError

log = logging.getLogger("prism_wa.media")

MB = 1024 * 1024
UPLOAD_CAP = 16 * MB                       # one file through the app; WhatsApp allows more for documents, the proxy does not carry more
WA_ID_LIFETIME = timedelta(days=25)        # Meta keeps an uploaded id about 30 days: renew a little early

MIME_KIND = {
    "image/jpeg": "image", "image/png": "image", "image/webp": "sticker",
    "video/mp4": "video", "video/3gpp": "video",
    "audio/aac": "audio", "audio/mp4": "audio", "audio/mpeg": "audio", "audio/amr": "audio", "audio/ogg": "audio",
    "application/pdf": "document", "text/plain": "document", "application/msword": "document",
    "application/vnd.ms-excel": "document", "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
}
KIND_MAX = {"image": 5 * MB, "video": 16 * MB, "audio": 16 * MB, "document": 100 * MB, "sticker": 500 * 1024}
KINDS = ("image", "video", "audio", "document", "sticker")

_CFG: dict = {"graph": None, "vault": None, "store": None}
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


class MediaError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def configure(*, graph: Optional[Graph] = None, vault=None, store=None) -> None:
    """Called once by the worker / app on start: what fetching and sending media needs."""
    _CFG.update(graph=graph, vault=vault, store=store)


def store_or_raise(store=None):
    got = store or _CFG["store"]
    if got is None:
        raise MediaError("no_storage", "Media storage is not set up on this server (WA_MEDIA_DIR).")
    return got


# ── what a file is ───────────────────────────────────────────────────────────

def kind_for(mime: str, filename: str = "") -> tuple:
    """-> (kind, mime) or raises MediaError saying what WhatsApp accepts."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime not in MIME_KIND:
        guessed = (mimetypes.guess_type(filename or "")[0] or "").lower()
        mime = guessed if guessed in MIME_KIND else mime
    if mime not in MIME_KIND:
        raise MediaError("unsupported_type", "WhatsApp cannot send that kind of file. Use a JPG or PNG picture, an MP4 video, an MP3 / OGG / AAC voice file, "
                                             "or a PDF, Word, Excel or PowerPoint document.")
    return MIME_KIND[mime], mime


def _asset_row(r) -> dict:
    cols = ("id", "kind", "mime", "filename", "size_bytes", "sha256", "source", "wa_media_id", "created_at")
    d = dict(zip(cols, r))
    d["size"] = d.pop("size_bytes")
    return d


_COLS = "id, kind, mime, filename, size_bytes, sha256, source, wa_media_id, created_at"


# ── the library ──────────────────────────────────────────────────────────────

def quota(cur, tenant_id) -> dict:
    cur.execute("select coalesce(sum(size_bytes), 0), count(*) from media_assets where tenant_id = %s", (tenant_id,))
    used, count = cur.fetchone()
    mb = plans.limit(cur, tenant_id, "storage_mb")
    return {"used": int(used), "count": int(count), "limit": None if mb is None else int(mb) * MB}


def upload(cur, tenant_id, *, filename: str, mime: str, data: bytes, source: str = "upload", actor_id=None, store=None, cap: int = UPLOAD_CAP) -> dict:
    store = store_or_raise(store)
    filename = (filename or "file").replace("\\", "/").split("/")[-1].strip()[:150] or "file"
    kind, mime = kind_for(mime, filename)
    size = len(data or b"")
    if size == 0:
        raise MediaError("empty", "That file is empty.")
    if size > min(cap, KIND_MAX[kind]):
        limit = min(cap, KIND_MAX[kind])
        raise MediaError("too_big", "That %s is %.1f MB; the most that can be sent is %d MB." % (kind, size / MB, limit // MB))
    digest = hashlib.sha256(data).hexdigest()
    cur.execute("select %s from media_assets where tenant_id = %%s and sha256 = %%s and kind = %%s" % _COLS, (tenant_id, digest, kind))
    row = cur.fetchone()
    if row:
        return _asset_row(row)                                          # the same file again: the one we hold
    q = quota(cur, tenant_id)
    if q["limit"] is not None and q["used"] + size > q["limit"]:
        raise MediaError("storage_full", "Your media storage is full (%d MB of %d MB). Delete files you no longer use." % (q["used"] // MB, q["limit"] // MB))
    cur.execute("insert into media_assets (tenant_id, kind, mime, filename, size_bytes, sha256, path, source, created_by)"
                " values (%s, %s, %s, %s, %s, %s, '', %s, %s) returning id", (tenant_id, kind, mime, filename, size, digest, source, actor_id))
    asset_id = cur.fetchone()[0]
    try:
        path = store.put(tenant_id, asset_id, data)
    except (MediaStoreError, OSError) as exc:
        raise MediaError("storage_error", "The file could not be saved: %s" % exc) from exc
    cur.execute("update media_assets set path = %s where id = %s", (path, asset_id))
    cur.execute("select %s from media_assets where id = %%s" % _COLS, (asset_id,))
    return _asset_row(cur.fetchone())


def list_assets(cur, tenant_id, *, kind: Optional[str] = None, search: str = "", limit: int = 60, offset: int = 0) -> dict:
    where, params = ["tenant_id = %s", "source <> 'inbound'"], [tenant_id]
    if kind:
        if kind not in KINDS:
            raise MediaError("bad_kind", "kind must be one of: %s" % ", ".join(KINDS))
        where.append("kind = %s")
        params.append(kind)
    if (search or "").strip():
        where.append("filename ilike %s")
        params.append("%" + search.strip() + "%")
    cur.execute("select count(*) from media_assets where " + " and ".join(where), params)
    total = cur.fetchone()[0]
    cur.execute("select %s from media_assets where %s order by created_at desc limit %%s offset %%s" % (_COLS, " and ".join(where)),
                params + [max(1, min(int(limit or 60), 200)), max(int(offset or 0), 0)])
    return {"assets": [_asset_row(r) for r in cur.fetchall()], "total": total, "quota": quota(cur, tenant_id)}


def get(cur, tenant_id, asset_id) -> dict:
    cur.execute("select %s from media_assets where id = %%s and tenant_id = %%s" % _COLS, (asset_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise MediaError("unknown_asset", "That file is not in this workspace.")
    return _asset_row(row)


def read(cur, tenant_id, asset_id, *, store=None) -> tuple:
    store = store_or_raise(store)
    a = get(cur, tenant_id, asset_id)
    cur.execute("select path from media_assets where id = %s", (asset_id,))
    try:
        return a, store.get(cur.fetchone()[0])
    except MediaStoreError as exc:
        raise MediaError("storage_error", str(exc)) from exc


def references(cur, tenant_id, asset_id) -> list:
    """What still uses this file: ['a saved reply', 'a campaign that has not finished', ...] — so deleting it cannot quietly break them."""
    out = []
    cur.execute("select count(*) from canned_messages where asset_id = %s", (asset_id,))
    if cur.fetchone()[0]:
        out.append("a saved reply")
    cur.execute("select count(*) from campaigns where tenant_id = %s and header_media->>'asset_id' = %s and status in ('draft', 'scheduled', 'sending', 'paused')",
                (tenant_id, str(asset_id)))
    if cur.fetchone()[0]:
        out.append("a campaign that has not finished")
    cur.execute("select count(*) from tenant_settings where tenant_id = %s and settings::text like %s", (tenant_id, "%" + str(asset_id) + "%"))
    if cur.fetchone()[0]:
        out.append("an auto-reply")
    cur.execute("select count(*) from flow_versions fv join flows f on f.id = fv.flow_id where f.tenant_id = %s and fv.graph::text like %s", (tenant_id, "%" + str(asset_id) + "%"))
    if cur.fetchone()[0]:
        out.append("a chatbot flow")
    return out


def delete(cur, tenant_id, asset_id, *, store=None) -> bool:
    store = store_or_raise(store)
    get(cur, tenant_id, asset_id)
    used = references(cur, tenant_id, asset_id)
    if used:
        raise MediaError("in_use", "That file is still used by %s. Remove it from there first." % " and ".join(used))
    cur.execute("delete from media_assets where id = %s and tenant_id = %s returning path", (asset_id, tenant_id))
    row = cur.fetchone()
    if row and row[0]:
        try:
            store.delete(row[0])
        except MediaStoreError:
            log.warning("could not remove stored file %s", row[0])
    return row is not None


# ── sending: a message that names one of our files ───────────────────────────

def _lock_for(asset_id) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(asset_id), threading.Lock())


def ensure_wa_media(connect, item: dict, token: str, asset_id: str, *, graph: Optional[Graph] = None, store=None) -> str:
    """Meta's id for this file on this number, uploading it first if there is none or it is about to expire."""
    graph = graph or _CFG["graph"]
    if graph is None:
        raise GraphError("media sending is not set up on this server")
    with _lock_for(asset_id):
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("select wa_media_id, wa_number_id, wa_uploaded_at, path, mime, filename from media_assets where id = %s and tenant_id = %s",
                            (asset_id, item["tenant_id"]))
                row = cur.fetchone()
                if row is None:
                    raise GraphError("the file this message carries is no longer in the Media Library", http_status=404)   # permanent: do not retry
                wa_id, wa_number, at, path, mime, filename = row
                if wa_id and str(wa_number) == str(item["number_id"]) and at and datetime.now(timezone.utc) - at < WA_ID_LIFETIME:
                    return wa_id
                try:
                    data = store_or_raise(store).get(path)
                except (MediaStoreError, MediaError) as exc:
                    raise GraphError("the stored file could not be read: %s" % exc, http_status=404) from exc
        finally:
            conn.close()
        new_id = graph.upload_media(token, item["phone_number_id"], filename, mime, data)
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("update media_assets set wa_media_id = %s, wa_number_id = %s, wa_uploaded_at = now() where id = %s",
                            (new_id, item["number_id"], asset_id))
            conn.commit()
        finally:
            conn.close()
        return new_id


def resolve_payload(connect, item: dict, token: str, *, graph: Optional[Graph] = None, store=None) -> dict:
    """The outbox payload with every `{"asset_id": …}` turned into `{"id": <Meta's id>}`."""
    def walk(node):
        if isinstance(node, dict):
            if "asset_id" in node:
                rest = {k: v for k, v in node.items() if k != "asset_id"}
                rest["id"] = ensure_wa_media(connect, item, token, node["asset_id"], graph=graph, store=store)
                return rest
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(item["payload"])


def has_assets(payload) -> bool:
    return "asset_id" in repr(payload)


# ── receiving: a customer's file ─────────────────────────────────────────────

def fetch_inbound(cur, tenant_id, *, message_id, media_id: str, number_id, graph: Optional[Graph] = None, vault=None, store=None) -> Optional[dict]:
    """Download what a customer sent and attach our copy to the message. Returns the asset, or None when it could not be had."""
    graph, vault = graph or _CFG["graph"], vault or _CFG["vault"]
    store = store or _CFG["store"]
    if graph is None or vault is None or store is None:
        return None
    cur.execute("select w.waba_id, w.token_enc from phone_numbers p join wabas w on w.id = p.waba_id where p.id = %s", (number_id,))
    row = cur.fetchone()
    if row is None:
        return None
    token = vault.decrypt(row[1], context=row[0])
    info = graph.media_info(token, media_id)
    if not info.get("url"):
        raise GraphError("Meta gave no link for that file")
    data = graph.download_media(token, info["url"])
    mime = info.get("mime_type") or "application/octet-stream"
    try:
        asset = upload(cur, tenant_id, filename="%s.%s" % (str(media_id)[:20], (mimetypes.guess_extension(mime.split(";")[0]) or ".bin").lstrip(".")),
                       mime=mime, data=data, source="inbound", store=store, cap=100 * MB)
    except MediaError as exc:
        log.info("kept no copy of media %s: %s", media_id, exc)
        return None
    cur.execute("update messages set media_asset_id = %s where id = %s", (asset["id"], message_id))
    return asset


def handle_fetch_job(cur, tenant_id, payload: dict) -> None:
    fetch_inbound(cur, tenant_id, message_id=payload["message_id"], media_id=payload["media_id"], number_id=payload["number_id"])
