"""Where the bytes of a media file live.

One small interface, so a folder on the server today can be an object store tomorrow without anything above it changing:

    put(tenant_id, asset_id, data) -> a relative path to keep in the database
    get(path) -> bytes
    delete(path)

`LocalStore` keeps files under a root folder, one sub-folder per workspace; it refuses any path that tries to leave the root
(a path comes from our own database, but a bug or a restored backup should still never read /etc/passwd).

`DbStore` keeps them in Postgres (table media_blobs). The gateway (which takes an upload) and the worker (which sends it to Meta,
and saves what a customer sends) are separate processes that usually cannot share a disk, but always share the database, so this is
the default and needs nothing set up. Point WA_MEDIA_DIR at a shared folder to use `LocalStore` instead.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import psycopg2


class MediaStoreError(Exception):
    pass


class LocalStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root != target and self.root not in target.parents:
            raise MediaStoreError("that path is outside the media folder")
        return target

    def put(self, tenant_id, asset_id, data: bytes) -> str:
        rel = "%s/%s/%s" % (re.sub(r"[^0-9a-f-]", "", str(tenant_id)), str(asset_id)[:2], re.sub(r"[^0-9a-f-]", "", str(asset_id)))
        target = self._safe(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)                         # a half-written file is never the file
        return rel

    def get(self, path: str) -> bytes:
        try:
            return self._safe(path).read_bytes()
        except OSError as exc:
            raise MediaStoreError("the stored file could not be read: %s" % exc) from exc

    def delete(self, path: str) -> None:
        try:
            self._safe(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise MediaStoreError("the stored file could not be removed: %s" % exc) from exc

    def total_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())


class DbStore:
    def __init__(self, connect):
        self.connect = connect

    def put(self, tenant_id, asset_id, data: bytes) -> str:
        path = "db/%s/%s" % (re.sub(r"[^0-9a-f-]", "", str(tenant_id)), re.sub(r"[^0-9a-f-]", "", str(asset_id)))
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("insert into media_blobs (path, data) values (%s, %s) on conflict (path) do update set data = excluded.data",
                            (path, psycopg2.Binary(data)))
            conn.commit()
        except psycopg2.Error as exc:
            conn.rollback()
            raise MediaStoreError("the file could not be stored: %s" % exc) from exc
        finally:
            conn.close()
        return path

    def get(self, path: str) -> bytes:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("select data from media_blobs where path = %s", (path,))
                row = cur.fetchone()
        except psycopg2.Error as exc:
            raise MediaStoreError("the stored file could not be read: %s" % exc) from exc
        finally:
            conn.close()
        if row is None:
            raise MediaStoreError("the stored file could not be read: it is not there")
        return bytes(row[0])

    def delete(self, path: str) -> None:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("delete from media_blobs where path = %s", (path,))
            conn.commit()
        except psycopg2.Error as exc:
            conn.rollback()
            raise MediaStoreError("the stored file could not be removed: %s" % exc) from exc
        finally:
            conn.close()


def from_env(env, connect=None):
    """A folder if WA_MEDIA_DIR is set, otherwise the database (when a connection is given)."""
    root = (env.get("WA_MEDIA_DIR") or "").strip()
    if root:
        return LocalStore(root)
    return DbStore(connect) if connect is not None else None
