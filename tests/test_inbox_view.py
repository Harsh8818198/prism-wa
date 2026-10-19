"""What the Inbox screen shows: states and their counts, History (closed windows), visibility, the thread, a typed reply."""
from datetime import datetime, timedelta, timezone

import helpers as H
import psycopg2
import pytest

from prism_wa import inbox, inbox_view, send
from prism_wa.inbox import InboxError

NOW = datetime.now(timezone.utc)


@pytest.fixture
def world(migrated):
    tenant_id, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tenant_id,))

    class W:
        db, tid = migrated, tenant_id

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
        def agent(cls, name, role=None, **changes):
            a = cls.do(inbox.ensure_agent, "seat-" + name.lower(), name)
            if role and role != a["role"]:
                migrated.q("update agents set role = %s where id = %s", (role, a["id"]))
            if changes:
                cls.do(inbox.update_agent, a["id"], **changes)
            return cls.do(inbox._agent, a["id"])

        @classmethod
        def chat(cls, name, phone, *, status="active", assignee=None, inbound_ago=timedelta(hours=1), tags=(), texts=(("in", "hello"),), opt_out=False):
            cid = migrated.one("insert into contacts (tenant_id, name, phone, tags, opt_out) values (%s,%s,%s,%s,%s) returning id",
                               (tenant_id, name, phone, list(tags), opt_out))
            last_in = (NOW - inbound_ago) if inbound_ago is not None else None
            conv = migrated.one("insert into conversations (tenant_id, contact_id, channel, status, assignee_id, last_active_at, last_inbound_at)"
                                " values (%s,%s,'whatsapp',%s,%s,%s,%s) returning id",
                                (tenant_id, cid, status, assignee, last_in or NOW - timedelta(days=30), last_in))
            for i, (direction, text) in enumerate(texts):
                migrated.q("insert into messages (tenant_id, conversation_id, direction, sender, text, created_at) values (%s,%s,%s,%s,%s,%s)",
                           (tenant_id, conv, direction, "customer" if direction == "in" else "agent", text, NOW - timedelta(minutes=60 - i)))
            return conv, cid

    return W


def names(out):
    return [c["contact_name"] for c in out["conversations"]]


def test_an_owner_sees_every_chat_newest_first_with_the_last_message(world):
    owner = world.agent("Owner")
    world.chat("Old", "919800000001", inbound_ago=timedelta(hours=30), texts=(("in", "ping"),))
    world.chat("New", "919800000002", inbound_ago=timedelta(minutes=5), texts=(("in", "price?"), ("out", "Rs 450 a foot")))
    out = world.do(inbox_view.list_threads, owner)
    assert names(out) == ["New", "Old"] and out["total"] == 2
    new = out["conversations"][0]
    assert (new["last_text"], new["last_direction"], new["window_open"], new["status"]) == ("Rs 450 a foot", "out", True, "active")
    assert out["conversations"][1]["window_open"] is False


def test_the_tabs_count_what_each_state_holds(world):
    owner = world.agent("Owner")
    bala = world.agent("Bala")
    world.chat("Bot1", "919800000001", status="active")
    world.chat("Bot2", "919800000002", status="new")
    world.chat("Wants1", "919800000003", status="requesting")
    world.chat("Mine", "919800000004", status="intervened", assignee=owner["id"])
    world.chat("Theirs", "919800000005", status="intervened", assignee=bala["id"])
    world.chat("Done", "919800000006", status="closed", assignee=owner["id"])
    c = world.do(inbox_view.list_threads, owner)["counts"]
    assert c == {"all": 6, "requesting": 1, "mine": 1, "unassigned": 3, "bot": 2, "intervened": 2, "closed": 1}
    for state, who in (("requesting", ["Wants1"]), ("mine", ["Mine"]), ("bot", ["Bot1", "Bot2"]), ("intervened", ["Mine", "Theirs"]), ("closed", ["Done"])):
        got = world.do(inbox_view.list_threads, owner, state=state)
        assert sorted(names(got)) == sorted(who), state
        assert got["total"] == len(who) and got["counts"]["all"] == 6                        # the tabs do not move with the tab
    assert sorted(names(world.do(inbox_view.list_threads, owner, state="unassigned"))) == ["Bot1", "Bot2", "Wants1"]
    with pytest.raises(ValueError):
        world.do(inbox_view.list_threads, owner, state="nonsense")


def test_history_is_the_chats_whose_window_has_closed(world):
    owner = world.agent("Owner")
    world.chat("Fresh", "919800000001", inbound_ago=timedelta(hours=2))
    world.chat("Stale", "919800000002", inbound_ago=timedelta(hours=48))
    world.chat("NeverWrote", "919800000003", inbound_ago=None, texts=(("out", "hello from us"),))
    assert names(world.do(inbox_view.list_threads, owner, window="open")) == ["Fresh"]
    assert sorted(names(world.do(inbox_view.list_threads, owner, window="closed"))) == ["NeverWrote", "Stale"]
    out = world.do(inbox_view.list_threads, owner, window="closed")
    assert out["counts"]["all"] == 2                                                         # counts follow the window too


def test_an_agent_sees_their_own_chats_and_the_unassigned_ones_their_rules_allow(world):
    owner = world.agent("Owner")
    bala = world.agent("Bala", masked=True, rules=[{"field": "tags", "op": "has", "value": ["gujarati"]}])
    world.chat("Mine", "919800000001", status="intervened", assignee=bala["id"])
    world.chat("Open match", "919800000002", tags=["gujarati"])
    world.chat("Open other", "919800000003", tags=["hindi"])
    world.chat("Owners", "919800000004", status="intervened", assignee=owner["id"], tags=["gujarati"])
    out = world.do(inbox_view.list_threads, bala)
    assert sorted(names(out)) == ["Mine", "Open match"] and out["counts"]["all"] == 2
    assert {c["contact_phone"] for c in out["conversations"]} == {"********0001", "********0002"}          # masked
    assert len(world.do(inbox_view.list_threads, owner)["conversations"]) == 4


def test_search_finds_a_name_a_number_or_something_said(world):
    owner = world.agent("Owner")
    world.chat("Asha", "919800100001", texts=(("in", "need 40 enclosures"),))
    world.chat("Ravi", "919800100002", texts=(("in", "invoice please"),))
    find = lambda t: names(world.do(inbox_view.list_threads, owner, search=t))                # noqa: E731
    assert find("asha") == ["Asha"] and find("enclosures") == ["Asha"] and find("0100002") == ["Ravi"] and find("zzz") == []


def test_a_filter_narrows_the_chats_by_what_is_known_about_the_person(world):
    owner = world.agent("Owner")
    world.chat("VIP", "919800000001", tags=["vip"])
    world.chat("Plain", "919800000002")
    out = world.do(inbox_view.list_threads, owner, filter_={"field": "tags", "op": "has", "value": ["vip"]})
    assert names(out) == ["VIP"] and out["counts"]["all"] == 1


def test_the_thread_is_oldest_first_with_delivery_state_and_media(world):
    owner = world.agent("Owner")
    conv, _cid = world.chat("Asha", "919800000001", texts=(("in", "hi"), ("out", "hello Asha")))
    world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, msg_type, media_id, mime_type, created_at)"
               " values (%s,%s,'in','customer','[image]','image','MEDIA1','image/jpeg', now())", (world.tid, conv))
    world.db.q("update messages set status = 'delivered' where conversation_id = %s and direction = 'out'", (conv,))
    out = world.do(inbox_view.thread, owner, conv)
    assert [m["text"] for m in out["messages"]] == ["hi", "hello Asha", "[image]"]
    assert out["messages"][1]["status"] == "delivered" and out["messages"][0]["media"] is None
    # Meta's id is known, our own copy is not fetched yet: the app shows "fetching" until the media_fetch job has run
    assert out["messages"][2]["media"] == {"id": None, "wa_id": "MEDIA1", "mime_type": "image/jpeg", "kind": "image", "filename": None, "size": None,
                                           "ready": False}
    assert out["conversation"]["window_open"] is True and out["conversation"]["contact_name"] == "Asha"


def test_another_agents_chat_cannot_be_read_and_an_unknown_one_is_not_there(world):
    world.agent("Owner")
    bala, meera = world.agent("Bala"), world.agent("Meera")
    conv, _ = world.chat("Theirs", "919800000001", status="intervened", assignee=meera["id"])
    with pytest.raises(InboxError) as e:
        world.do(inbox_view.thread, bala, conv)
    assert e.value.reason == "not_yours"
    with pytest.raises(InboxError) as e2:
        world.do(inbox_view.thread, bala, "00000000-0000-0000-0000-000000000000")
    assert e2.value.reason == "unknown_conversation"


def test_a_typed_reply_is_queued_once_and_takes_the_chat(world):
    world.agent("Owner")
    bala = world.agent("Bala")
    conv, cid = world.chat("Asha", "919800000001", status="requesting")
    out = world.do(inbox_view.send_text, bala, conversation_id=conv, text="Sure, sending the quote", client_ref="abc")
    assert out["queued"] is True
    again = world.do(inbox_view.send_text, bala, conversation_id=conv, text="Sure, sending the quote", client_ref="abc")
    assert again["outbox_id"] == out["outbox_id"]                                          # a retry is not a second message
    rows = world.db.q("select kind, payload from outbox where tenant_id = %s", (world.tid,))
    assert len(rows) == 1 and rows[0]["payload"]["text"]["body"] == "Sure, sending the quote"
    st = world.db.q("select status, assignee_id from conversations where id = %s", (conv,))[0]
    assert st["status"] == "intervened" and str(st["assignee_id"]) == str(bala["id"])


def test_a_reply_outside_the_window_is_refused_in_words(world):
    world.agent("Owner")
    bala = world.agent("Bala")
    conv, _ = world.chat("Stale", "919800000001", inbound_ago=timedelta(hours=40))
    with pytest.raises(send.SendRefused) as e:
        world.do(inbox_view.send_text, bala, conversation_id=conv, text="Hello?")
    assert e.value.reason == "window_closed"
    with pytest.raises(send.SendRefused):
        world.do(inbox_view.send_text, bala, conversation_id=conv, text="   ")
    with pytest.raises(ValueError):
        world.do(inbox_view.send_text, bala, text="no target")


def test_live_and_history_split_the_chats_the_way_a_team_works(world):
    owner = world.agent("Owner")
    world.chat("Fresh", "919800000001", inbound_ago=timedelta(hours=2))
    world.chat("Stale bot", "919800000002", inbound_ago=timedelta(hours=48))
    world.chat("Stale but wanted", "919800000003", inbound_ago=timedelta(hours=48), status="requesting")
    world.chat("Stale with agent", "919800000004", inbound_ago=timedelta(hours=48), status="intervened", assignee=owner["id"])
    assert sorted(names(world.do(inbox_view.list_threads, owner, window="live"))) == ["Fresh", "Stale but wanted", "Stale with agent"]
    assert names(world.do(inbox_view.list_threads, owner, window="history")) == ["Stale bot"]
    live = world.do(inbox_view.list_threads, owner, window="live")
    assert live["counts"]["all"] == 3 and live["counts"]["requesting"] == 1
