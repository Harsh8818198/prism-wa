"""The Media Library: what may be stored, how a file reaches WhatsApp (once per number), how a customer's picture is kept, and
that a saved reply, an auto-reply or a campaign can carry a file."""
import base64
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import chat_send, inbox, jobs, media, plans, send
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.ingest import JOB_HANDLERS
from prism_wa.media import MB, MediaError
from prism_wa.media_store import LocalStore, MediaStoreError
from prism_wa.vault import Vault

TOKEN = "EAAB-test-business-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 2000
JPG = b"\xff\xd8\xff" + b"1" * 3000
PDF = b"%PDF-1.4 " + b"2" * 4000
NOW = datetime.now(timezone.utc)


class FakeMeta:
    """A Graph transport that understands uploads, media lookups, downloads and sends."""

    def __init__(self):
        self.uploads, self.sent, self.lookups, self.downloads, self.fail_download = [], [], [], [], False
        self.lock, self.n = threading.Lock(), 0
        self.inbound = {"IN-IMG": (JPG, "image/jpeg")}

    def __call__(self, method, url, data, headers):
        with self.lock:
            self.n += 1
            n = self.n
        if method == "POST" and url.endswith("/media"):
            body = data or b""
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            self.uploads.append({"filename": name, "size": len(body), "auth": headers.get("Authorization"), "pnid": url.split("/")[-2]})
            return 200, json.dumps({"id": "WAMEDIA%d" % n}).encode()
        if method == "POST" and url.endswith("/messages"):
            body = json.loads(data)
            self.sent.append(body)
            return 200, json.dumps({"messages": [{"id": "wamid.M%d" % n}]}).encode()
        if method == "GET" and url.startswith("https://lookaside/"):
            self.downloads.append(url)
            if self.fail_download:
                return 404, b"gone"
            return 200, self.inbound[url.rsplit("/", 1)[1]][0]
        if method == "GET":
            media_id = url.split("/")[-1].split("?")[0]
            self.lookups.append(media_id)
            if media_id in self.inbound:
                return 200, json.dumps({"id": media_id, "url": "https://lookaside/%s" % media_id, "mime_type": self.inbound[media_id][1]}).encode()
        return 404, b"{}"


@pytest.fixture
def world(migrated, tmp_path):
    vault = Vault({1: os.urandom(32)})
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tenant_id,))
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    meta = FakeMeta()
    store = LocalStore(str(tmp_path / "media"))
    graph = Graph(meta)
    media.configure(graph=graph, vault=vault, store=store)

    class W:
        db, tid, nid, meta_, store_, graph_, vault_ = migrated, tenant_id, number_id, meta, store, graph, vault

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tenant_id, *a, **kw)
                conn.commit()
                return out
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        @classmethod
        def up(cls, name="a.png", mime="image/png", data=PNG, **kw):
            return cls.do(media.upload, filename=name, mime=mime, data=data, **kw)

        @classmethod
        def person(cls, window_open=True):
            cid = migrated.one("insert into contacts (tenant_id, name, phone, opted_in) values (%s,'Asha','919800100001',true) returning id", (tenant_id,))
            migrated.q("insert into conversations (tenant_id, contact_id, channel, last_inbound_at, last_active_at, number_id)"
                       " values (%s,%s,'whatsapp',%s,now(),%s)", (tenant_id, cid, NOW - (timedelta(minutes=5) if window_open else timedelta(days=3)), number_id))
            return cid

        @classmethod
        def deliver(cls):
            return send.send_due(cls.conn, graph=graph, vault=vault)

    yield W
    media.configure(graph=None, vault=None, store=None)


# ── what may be stored ───────────────────────────────────────────────────────

@pytest.mark.parametrize("mime,name,kind", [("image/jpeg", "a.jpg", "image"), ("image/png", "a.png", "image"), ("video/mp4", "v.mp4", "video"),
                                            ("audio/ogg", "n.ogg", "audio"), ("application/pdf", "q.pdf", "document"),
                                            ("application/octet-stream", "sheet.xlsx", "document"), ("image/webp", "s.webp", "sticker")])
def test_a_file_is_a_picture_a_video_a_voice_note_or_a_document(mime, name, kind):
    assert media.kind_for(mime, name)[0] == kind


@pytest.mark.parametrize("mime,name", [("application/zip", "a.zip"), ("image/gif", "a.gif"), ("text/html", "a.html"), ("", "noextension")])
def test_a_kind_whatsapp_cannot_send_is_refused_in_words(mime, name):
    with pytest.raises(MediaError) as e:
        media.kind_for(mime, name)
    assert e.value.reason == "unsupported_type" and "JPG or PNG" in str(e.value)


def test_a_file_is_stored_listed_and_read_back_whole(world):
    a = world.up("Site photo.png")
    assert (a["kind"], a["filename"], a["size"], a["mime"]) == ("image", "Site photo.png", len(PNG), "image/png")
    got, data = world.do(media.read, a["id"])
    assert data == PNG and got["id"] == a["id"]
    listed = world.do(media.list_assets)
    assert [x["filename"] for x in listed["assets"]] == ["Site photo.png"] and listed["total"] == 1 and listed["quota"]["used"] == len(PNG)


def test_the_same_file_again_is_the_file_already_held(world):
    first = world.up("one.png")
    second = world.up("renamed.png")
    assert second["id"] == first["id"] and world.do(media.quota)["count"] == 1
    assert world.up("a.jpg", "image/jpeg", JPG)["id"] != first["id"]


@pytest.mark.parametrize("size,mime,name,reason", [(0, "image/png", "e.png", "empty"), (6 * MB, "image/png", "big.png", "too_big"),
                                                    (17 * MB, "application/pdf", "huge.pdf", "too_big")])
def test_an_empty_or_too_big_file_is_refused_with_the_limit(world, size, mime, name, reason):
    with pytest.raises(MediaError) as e:
        world.up(name, mime, b"x" * size)
    assert e.value.reason == reason
    if reason == "too_big":
        assert "MB" in str(e.value)


def test_the_plan_caps_storage(world, monkeypatch):
    monkeypatch.setitem(plans.LIMITS["pro"], "storage_mb", 1)
    world.up("a.png", data=b"\x89PNG" + b"a" * (600 * 1024))
    with pytest.raises(MediaError) as e:
        world.up("b.png", data=b"\x89PNG" + b"b" * (600 * 1024))
    assert e.value.reason == "storage_full" and "storage is full" in str(e.value)
    assert world.do(media.quota)["limit"] == MB


def test_storage_is_per_plan_in_megabytes():
    assert [plans.LIMITS[p]["storage_mb"] for p in ("basic", "pro", "premium", "unlimited")] == [1024, 5120, 5120, 10240]
    assert plans.LIMITS["enterprise"]["storage_mb"] is None


def test_the_library_lists_by_kind_and_name_and_hides_what_customers_sent(world):
    world.up("brochure.pdf", "application/pdf", PDF)
    world.up("photo.png")
    world.up("customer.jpg", "image/jpeg", JPG, source="inbound")
    assert [a["filename"] for a in world.do(media.list_assets)["assets"]] == ["photo.png", "brochure.pdf"]
    assert [a["filename"] for a in world.do(media.list_assets, kind="document")["assets"]] == ["brochure.pdf"]
    assert [a["filename"] for a in world.do(media.list_assets, search="photo")["assets"]] == ["photo.png"]
    with pytest.raises(MediaError):
        world.do(media.list_assets, kind="gif")


def test_one_workspace_never_sees_anothers_files(world):
    a = world.up("mine.png")
    other = world.db.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    with pytest.raises(MediaError) as e:
        world.do(media.get, a["id"], tenant=None) if False else world.do(lambda cur, t, aid: media.get(cur, other, aid), a["id"])
    assert e.value.reason == "unknown_asset"


def test_the_store_refuses_to_leave_its_folder(tmp_path):
    store = LocalStore(str(tmp_path / "m"))
    with pytest.raises(MediaStoreError):
        store.get("../../etc/passwd")
    path = store.put("11111111-1111-1111-1111-111111111111", "ab222222-2222-2222-2222-222222222222", b"hello")
    assert store.get(path) == b"hello" and not (tmp_path / "m" / path).with_suffix(".part").exists()
    store.delete(path)
    store.delete(path)                                                     # twice is fine
    with pytest.raises(MediaStoreError):
        store.get(path)


def test_the_database_store_keeps_a_file_where_every_process_can_reach_it(world):
    from prism_wa.media_store import DbStore, from_env
    store = DbStore(world.conn)
    a = world.do(lambda cur, t: media.upload(cur, t, filename="shared.png", mime="image/png", data=PNG, store=store))
    path = world.db.one("select path from media_assets where id = %s", (a["id"],))
    assert path.startswith("db/") and world.db.one("select length(data) from media_blobs where path = %s", (path,)) == len(PNG)
    assert store.get(path) == PNG
    assert world.do(lambda cur, t: media.read(cur, t, a["id"], store=store))[1] == PNG
    assert world.do(lambda cur, t: media.delete(cur, t, a["id"], store=store)) is True
    assert world.db.q("select 1 from media_blobs") == []
    with pytest.raises(MediaStoreError):
        store.get(path)
    store.delete(path)                                                    # twice is fine
    assert isinstance(from_env({}, world.conn), DbStore) and from_env({}) is None
    assert isinstance(from_env({"WA_MEDIA_DIR": str(world.store_.root)}, world.conn), LocalStore)


# ── deleting ─────────────────────────────────────────────────────────────────

def test_deleting_removes_the_file_from_disk_and_the_library(world):
    a = world.up("gone.png")
    path = world.db.one("select path from media_assets where id = %s", (a["id"],))
    assert world.do(media.delete, a["id"]) is True
    assert world.db.q("select 1 from media_assets") == [] and not (world.store_.root / path).exists()


def test_a_file_still_in_use_cannot_be_deleted(world):
    a = world.up("logo.png")
    world.do(inbox.save_canned, "hello", "Hi!", None, a["id"])
    with pytest.raises(MediaError) as e:
        world.do(media.delete, a["id"])
    assert e.value.reason == "in_use" and "a saved reply" in str(e.value)
    world.do(inbox.delete_canned, "hello")
    assert world.do(media.delete, a["id"]) is True


def test_a_campaign_or_an_auto_reply_that_uses_a_file_also_holds_it(world):
    a = world.up("offer.png")
    world.db.q("insert into campaigns (tenant_id, name, template_name, status, header_media) values (%s,'c','t','draft',%s)", (world.tid, json.dumps({"asset_id": str(a["id"])})))
    with pytest.raises(MediaError) as e:
        world.do(media.delete, a["id"])
    assert "a campaign that has not finished" in str(e.value)
    world.db.q("update campaigns set status = 'done'")
    world.do(inbox.save_settings, {"welcome": {"enabled": True, "text": "", "asset_id": str(a["id"])}})
    with pytest.raises(MediaError) as e2:
        world.do(media.delete, a["id"])
    assert "an auto-reply" in str(e2.value)


# ── sending a file ───────────────────────────────────────────────────────────

def test_a_typed_file_reaches_meta_uploaded_once_with_its_caption(world):
    a = world.up("quote.pdf", "application/pdf", PDF)
    cid = world.person()
    world.do(send.enqueue_media, contact_id=cid, kind="document", asset_id=a["id"], caption="Your quote", filename="quote.pdf", idem_key="m1")
    assert world.deliver().sent == 1
    assert len(world.meta_.uploads) == 1 and world.meta_.uploads[0]["filename"] == "quote.pdf" and world.meta_.uploads[0]["auth"] == "Bearer " + TOKEN
    body = world.meta_.sent[0]
    assert body["type"] == "document" and body["document"] == {"caption": "Your quote", "filename": "quote.pdf", "id": "WAMEDIA1"} and body["to"] == "919800100001"
    assert world.db.q("select kind, state from outbox")[0] == {"kind": "media", "state": "sent"}
    from prism_wa import inbox_view                                       # and the chat shows the file it carries
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    agent = world.do(inbox.ensure_agent, "seat-owner", "Owner")
    shown = world.do(inbox_view.thread, agent, conv)["messages"][0]
    assert shown["media"]["id"] == a["id"] and shown["media"]["ready"] is True and shown["media"]["filename"] == "quote.pdf"
    assert shown["text"] == "[document] Your quote"


def test_the_same_file_to_many_people_uploads_to_meta_once(world):
    a = world.up("offer.png")
    for i in range(5):
        cid = world.db.one("insert into contacts (tenant_id, name, phone, opted_in) values (%s,%s,%s,true) returning id", (world.tid, "P%d" % i, "91980020%04d" % i))
        world.db.q("insert into conversations (tenant_id, contact_id, channel, last_inbound_at, last_active_at, number_id) values (%s,%s,'whatsapp',now(),now(),%s)",
                   (world.tid, cid, world.nid))
        world.do(send.enqueue_media, contact_id=cid, kind="image", asset_id=a["id"], idem_key="m%d" % i)
    assert world.deliver().sent == 5
    assert len(world.meta_.uploads) == 1 and {b["image"]["id"] for b in world.meta_.sent} == {"WAMEDIA1"}


def test_an_expired_id_is_uploaded_again(world):
    a = world.up("offer.png")
    cid = world.person()
    world.do(send.enqueue_media, contact_id=cid, kind="image", asset_id=a["id"], idem_key="m1")
    world.deliver()
    world.db.q("update media_assets set wa_uploaded_at = now() - interval '26 days'")
    world.do(send.enqueue_media, contact_id=cid, kind="image", asset_id=a["id"], idem_key="m2")
    world.deliver()
    assert len(world.meta_.uploads) == 2 and world.meta_.sent[1]["image"]["id"] != world.meta_.sent[0]["image"]["id"]


def test_a_file_already_on_meta_or_a_link_is_sent_as_it_is(world):
    cid = world.person()
    world.do(send.enqueue_media, contact_id=cid, kind="image", wa_media_id="THEIRS", idem_key="a")
    world.do(send.enqueue_media, contact_id=cid, kind="video", link="https://x.in/v.mp4", caption="Watch", idem_key="b")
    world.deliver()
    assert world.meta_.uploads == [] and {json.dumps(b.get("image") or b.get("video"), sort_keys=True) for b in world.meta_.sent} == \
        {'{"id": "THEIRS"}', '{"caption": "Watch", "link": "https://x.in/v.mp4"}'}


def test_a_file_cannot_start_a_chat_and_a_bad_request_is_refused(world):
    a = world.up("x.png")
    cid = world.person(window_open=False)
    with pytest.raises(send.SendRefused) as e:
        world.do(send.enqueue_media, contact_id=cid, kind="image", asset_id=a["id"], idem_key="w")
    assert e.value.reason == "window_closed"
    open_chat = world.person.__func__(world) if False else None
    del open_chat
    for kw, reason in (({"kind": "gif", "asset_id": a["id"]}, "bad_kind"), ({"kind": "image"}, "empty")):
        with pytest.raises(send.SendRefused) as bad:
            world.do(send.enqueue_media, contact_id=cid, idem_key="x", **kw)
        assert bad.value.reason == reason


def test_a_file_that_has_vanished_fails_the_message_with_a_reason(world):
    a = world.up("lost.png")
    cid = world.person()
    world.do(send.enqueue_media, contact_id=cid, kind="image", asset_id=a["id"], idem_key="m1")
    world.db.q("delete from media_assets")
    world.deliver()
    row = world.db.q("select state, error from outbox")[0]
    assert row["state"] == "failed" and "no longer in the Media Library" in row["error"]["message"]


# ── a customer's picture ─────────────────────────────────────────────────────

def test_a_customers_picture_is_kept_and_shown_in_the_thread(world):
    cid = world.person()
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    mid = world.db.one("insert into messages (tenant_id, conversation_id, direction, sender, text, msg_type, media_id, mime_type, wamid)"
                       " values (%s,%s,'in','customer','[image]','image','IN-IMG','image/jpeg','wamid.IN1') returning id", (world.tid, conv))
    conn = world.conn()
    with conn.cursor() as cur:
        jobs.enqueue(cur, world.tid, "media_fetch", {"message_id": str(mid), "media_id": "IN-IMG", "number_id": str(world.nid)}, "media_fetch:%s" % mid)
    conn.commit()
    conn.close()
    conn = world.conn()
    jobs.run_once(conn, JOB_HANDLERS)
    conn.close()
    asset_id = world.db.one("select media_asset_id from messages where id = %s", (mid,))
    assert asset_id is not None and world.meta_.lookups == ["IN-IMG"] and len(world.meta_.downloads) == 1
    a, data = world.do(media.read, asset_id)
    assert data == JPG and a["source"] == "inbound" and a["kind"] == "image"
    from prism_wa import inbox_view
    agent = world.do(inbox.ensure_agent, "seat-owner", "Owner")
    m = world.do(inbox_view.thread, agent, conv)["messages"][0]
    assert m["media"]["ready"] is True and m["media"]["id"] == asset_id and m["media"]["kind"] == "image" and m["media"]["size"] == len(JPG)
    assert world.do(media.list_assets)["assets"] == []                    # a customer's picture is not in the library


def test_a_picture_meta_will_not_give_leaves_the_message_unready_and_the_job_retries(world):
    cid = world.person()
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    mid = world.db.one("insert into messages (tenant_id, conversation_id, direction, sender, text, msg_type, media_id, mime_type, wamid)"
                       " values (%s,%s,'in','customer','[image]','image','IN-IMG','image/jpeg','wamid.IN2') returning id", (world.tid, conv))
    world.meta_.fail_download = True
    conn = world.conn()
    with conn.cursor() as cur:
        jobs.enqueue(cur, world.tid, "media_fetch", {"message_id": str(mid), "media_id": "IN-IMG", "number_id": str(world.nid)}, "media_fetch:%s" % mid)
    conn.commit()
    jobs.run_once(conn, JOB_HANDLERS)
    conn.close()
    assert world.db.one("select media_asset_id from messages where id = %s", (mid,)) is None
    assert world.db.one("select state from wa_jobs where kind = 'media_fetch'") in ("queued", "failed")
    from prism_wa import inbox_view
    agent = world.do(inbox.ensure_agent, "seat-owner", "Owner")
    m = world.do(inbox_view.thread, agent, conv)["messages"][0]
    assert m["media"]["ready"] is False and m["media"]["wa_id"] == "IN-IMG"


def test_a_picture_that_arrives_over_the_webhook_is_fetched_and_kept(world):
    world.db.q("delete from contacts")                                     # `world` onboarded the tenant; deliver a real inbound picture
    H.deliver(world.db, P.inbound(P.image("IN-IMG", "site photo", "wamid.IMG1", "1760000006")))
    H.work(world.db)
    job = world.db.q("select kind, state, payload from wa_jobs where kind = 'media_fetch'")
    assert len(job) == 1 and job[0]["payload"]["media_id"] == "IN-IMG"
    conn = world.conn()
    jobs.run_once(conn, JOB_HANDLERS)
    conn.close()
    row = world.db.q("select text, media_asset_id from messages where wamid = 'wamid.IMG1'")[0]
    assert row["text"] == "site photo" and row["media_asset_id"] is not None
    assert world.db.q("select source, kind from media_assets")[0] == {"source": "inbound", "kind": "image"}


# ── saved replies and auto-replies that carry a file ─────────────────────────

def test_a_saved_reply_can_be_only_a_file_and_expands_with_it(world):
    a = world.up("catalogue.pdf", "application/pdf", PDF)
    world.do(inbox.save_canned, "catalogue", "", None, a["id"])
    cid = world.db.one("insert into contacts (tenant_id, name, phone) values (%s,'Asha Shah','919800100009') returning id", (world.tid,))
    listed = world.do(inbox.list_canned)
    assert listed == [{"shortcut": "catalogue", "body": "", "asset_id": a["id"], "asset_kind": "document", "asset_name": "catalogue.pdf"}]
    out = world.do(inbox.expand_full, "catalogue", cid)
    assert out["text"] == "" and out["asset"]["id"] == a["id"]
    world.do(inbox.save_canned, "hi", "Hello {{first_name}}!")
    assert world.do(inbox.expand_full, "hi", cid) == {"text": "Hello Asha!", "asset": None}
    with pytest.raises(inbox.InboxError):
        world.do(inbox.save_canned, "empty", "", None, None)
    with pytest.raises(inbox.InboxError) as bad:
        world.do(inbox.save_canned, "ghost", "x", None, "00000000-0000-0000-0000-000000000000")
    assert bad.value.reason == "bad_canned"


def seed_welcome(world, **block):
    world.do(inbox.save_settings, {"welcome": {"enabled": True, "once_per_hours": 24, **block}})


def test_a_welcome_can_be_a_picture_with_words_under_it(world):
    a = world.up("hello.png")
    seed_welcome(world, text="Welcome to Patel Constructions", asset_id=str(a["id"]))
    cid = world.person()
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    kind = world.do(inbox.maybe_auto_reply, cid, conv, NOW, wamid="w1")
    assert kind == "welcome"
    row = world.db.q("select kind, payload from outbox")[0]
    assert row["kind"] == "media" and row["payload"]["image"]["caption"] == "Welcome to Patel Constructions" and row["payload"]["image"]["asset_id"] == str(a["id"])


def test_a_welcome_can_be_a_template(world):
    waba = world.db.one("select id from wabas")
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s,%s,'greet','en_US','UTILITY','APPROVED',%s)",
               (world.tid, waba, json.dumps([{"type": "BODY", "text": "Hello {{1}}, thanks for getting in touch with us today.", "example": {"body_text": [["Asha"]]}}])))
    seed_welcome(world, text="", template={"name": "greet", "language": "en_US", "body": ["there"]})
    cid = world.person()
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    assert world.do(inbox.maybe_auto_reply, cid, conv, NOW, wamid="w2") == "welcome"
    row = world.db.q("select kind, payload from outbox")[0]
    assert row["kind"] == "template" and row["payload"]["template"]["name"] == "greet"


def test_a_welcome_needs_words_a_file_or_a_template(world):
    with pytest.raises(inbox.InboxError) as e:
        world.do(inbox.save_settings, {"welcome": {"enabled": True, "text": "  "}})
    assert "a file or a template" in str(e.value)
    with pytest.raises(inbox.InboxError):
        world.do(inbox.save_settings, {"welcome": {"enabled": True, "text": "x", "asset_id": "00000000-0000-0000-0000-000000000000"}})
    with pytest.raises(inbox.InboxError):
        world.do(inbox.save_settings, {"welcome": {"enabled": True, "text": "x", "template": {"name": " "}}})


# ── a campaign's header ──────────────────────────────────────────────────────

def test_a_campaigns_header_can_be_a_library_file(world):
    from prism_wa import campaigns
    tpl = [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Our new range is in the shop today, come and see.", "example": None}]
    a = world.up("range.png")
    out = campaigns.build_components(tpl, {"header": {"asset_id": str(a["id"])}}, {"name": "Asha", "phone": "9"}, {}, make_link=lambda t: t,
                                     campaign_id="c", recipient_id="r")
    assert out[0] == {"type": "header", "parameters": [{"type": "image", "image": {"asset_id": str(a["id"])}}]}
    assert campaigns._mapping_problem({"asset_id": str(a["id"])}) is None and "Media Library" in campaigns._mapping_problem({"asset_id": "nope"})
    c = chat_send.components_for(tpl, header_asset_id=str(a["id"]))
    assert c[0]["parameters"][0]["image"] == {"asset_id": str(a["id"])}
    with pytest.raises(chat_send.ChatSendError):
        chat_send.components_for(tpl)


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.fixture
def http(world, migrated):
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok", graph=world.graph_, vault=world.vault_)
    client = TestClient(app)

    class H_:
        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(world.tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "tok"})
    H_.w = world
    return H_


def test_the_library_over_http_upload_list_get_usage_delete(http):
    b64 = base64.b64encode(PNG).decode()
    up = http.post("/internal/media/upload", filename="site.png", mime="image/png", data=b64)
    assert up.status_code == 200 and up.json()["asset"]["kind"] == "image" and up.json()["quota"]["used"] == len(PNG)
    aid = up.json()["asset"]["id"]
    assert http.post("/internal/media/list").json()["total"] == 1
    got = http.post("/internal/media/get", asset_id=aid).json()
    assert base64.b64decode(got["data"]) == PNG and got["too_big"] is False
    assert http.post("/internal/media/get", asset_id=aid, max_bytes=10).json()["too_big"] is True
    assert http.post("/internal/media/usage").json()["quota"]["count"] == 1
    assert http.post("/internal/media/delete", asset_id=aid).json()["deleted"] is True
    assert http.post("/internal/media/get", asset_id=aid).status_code == 404


def test_bad_uploads_get_honest_answers(http):
    assert http.post("/internal/media/upload", filename="x.zip", mime="application/zip", data=base64.b64encode(b"zz").decode()).status_code == 400
    assert http.post("/internal/media/upload", filename="x.png", mime="image/png", data="***not base64***").status_code == 400
    assert http.post("/internal/media/upload", filename="x.png", mime="image/png", data=base64.b64encode(b"x" * (6 * MB)).decode()).status_code == 413


def test_a_file_can_be_sent_into_a_chat_over_http(http):
    w = http.w
    up = http.post("/internal/media/upload", filename="site.png", mime="image/png", data=base64.b64encode(PNG).decode()).json()["asset"]
    owner = w.do(inbox.ensure_agent, "seat-owner", "Owner")
    cid = w.person()
    conv = w.db.one("select id from conversations where contact_id = %s", (cid,))
    r = http.post("/internal/messages/send_media", conversation_id=str(conv), asset_id=up["id"], caption="Here you go", agent_id=str(owner["id"]), client_ref="k1")
    assert r.status_code == 200 and r.json()["queued"] is True
    again = http.post("/internal/messages/send_media", conversation_id=str(conv), asset_id=up["id"], caption="Here you go", agent_id=str(owner["id"]), client_ref="k1")
    assert again.json()["outbox_id"] == r.json()["outbox_id"]
    assert w.db.q("select count(*) as n from outbox")[0]["n"] == 1
    assert http.post("/internal/messages/send_media", conversation_id=str(conv), asset_id="00000000-0000-0000-0000-000000000000", agent_id=str(owner["id"])).status_code == 404


def test_saved_replies_with_files_over_http(http):
    up = http.post("/internal/media/upload", filename="c.pdf", mime="application/pdf", data=base64.b64encode(PDF).decode()).json()["asset"]
    assert http.post("/internal/inbox/canned/save", shortcut="catalogue", body="", asset_id=up["id"]).status_code == 200
    listed = http.post("/internal/inbox/canned/list").json()["canned"]
    assert listed[0]["asset_name"] == "c.pdf"
    cid = http.w.db.one("insert into contacts (tenant_id, name, phone) values (%s,'Asha','919800100009') returning id", (http.w.tid,))
    out = http.post("/internal/inbox/canned/expand", shortcut="catalogue", contact_id=str(cid)).json()
    assert out["asset"]["id"] == up["id"] and out["text"] == ""
    assert http.post("/internal/media/delete", asset_id=up["id"]).status_code == 409
