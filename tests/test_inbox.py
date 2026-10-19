"""The team inbox: agents, routing, ownership, visibility (including in the database), auto-replies, monitoring."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import inbox, send
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.inbox import InboxError, in_hours
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

IST = timezone(timedelta(hours=5, minutes=30))
HOURS = {"tz": "Asia/Kolkata", "days": {d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}}


@pytest.fixture
def world(migrated):
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"premium\"}')", (tid,))

    class W:
        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    out = fn(cur, tid, *a, **kw)
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
                cls.db.q("update agents set role = %s where id = %s", (role, a["id"]))
            if changes:
                cls.do(inbox.update_agent, a["id"], **changes)
            return cls.do(inbox._agent, a["id"])

        @classmethod
        def chat(cls, name="Customer", tags=(), status="active", assignee=None, phone=None, last_active=None):
            phone = phone or "9198%08d" % abs(hash((name, time.time())) % 10**8)
            cid = cls.db.one("insert into contacts (tenant_id, name, phone, tags) values (%s,%s,%s,%s) returning id",
                             (tid, name, phone, list(tags)))
            return cid, cls.db.one(
                "insert into conversations (tenant_id, contact_id, channel, status, assignee_id, last_active_at, last_inbound_at, number_id)"
                " values (%s,%s,'whatsapp',%s,%s,%s,%s,%s) returning id",
                (tid, cid, status, assignee, last_active or utcnow(), last_active or utcnow(), number))

    W.db, W.tid, W.nid = migrated, tid, number
    return W


# ── hours ────────────────────────────────────────────────────────────────────

def test_working_hours_follow_the_time_zone_and_exclude_the_closing_minute():
    mon = lambda h, m: datetime(2026, 10, 19, h, m, tzinfo=IST)            # a Monday    # noqa: E731
    assert in_hours(HOURS, mon(9, 0)) and in_hours(HOURS, mon(17, 59))
    assert not in_hours(HOURS, mon(18, 0)) and not in_hours(HOURS, mon(8, 59))
    assert not in_hours(HOURS, datetime(2026, 10, 17, 11, 0, tzinfo=IST))   # a Saturday: no entry means closed
    assert in_hours(HOURS, mon(14, 30).astimezone(timezone.utc))             # 09:00 UTC is 14:30 IST: the zone is converted
    assert not in_hours(HOURS, mon(3, 0).astimezone(timezone.utc))           # 21:30 UTC the evening before is 03:00 IST
    assert in_hours(None, mon(3, 0)) and in_hours({}, mon(3, 0))


@pytest.mark.parametrize("bad", [{"days": []}, {"tz": "Mars/Base", "days": {}}, {"days": {"funday": [["09:00", "10:00"]]}},
                                 {"days": {"mon": [["9:00", "10:00"]]}}, {"days": {"mon": [["10:00", "09:00"]]}},
                                 {"days": {"mon": [["09:00"]]}}, "9 to 5"])
def test_bad_working_hours_are_refused(bad):
    with pytest.raises(InboxError) as e:
        inbox.validate_hours(bad)
    assert e.value.reason == "bad_hours"


# ── agents ───────────────────────────────────────────────────────────────────

def test_the_first_person_is_the_owner_and_a_seat_is_one_agent(world):
    a, b = world.agent("Asha"), world.agent("Bala")
    assert (a["role"], b["role"]) == ("owner", "agent")
    assert world.do(inbox.ensure_agent, "seat-asha", "Renamed")["id"] == a["id"]
    assert [x["name"] for x in world.do(inbox.list_agents)] == ["Asha", "Bala"]


def test_a_workspace_cannot_lose_its_last_owner(world):
    owner = world.agent("Asha")
    for change in ({"role": "agent"}, {"active": False}):
        with pytest.raises(InboxError) as e:
            world.do(inbox.update_agent, owner["id"], **change)
        assert e.value.reason == "last_owner"
    world.agent("Bala", "owner")
    assert world.do(inbox.update_agent, owner["id"], role="manager")["role"] == "manager"


def test_agent_settings_are_validated(world):
    a = world.agent("Asha")
    world.agent("Bala")
    assert world.do(inbox.update_agent, a["id"], masked=True, available=False, working_hours=HOURS)["masked"] is True
    for change, why in (({"role": "boss"}, "bad_role"), ({"working_hours": {"days": []}}, "bad_hours"),
                        ({"rules": "tag"}, "bad_rules"), ({"rules": [{"field": "zzz", "op": "x"}]}, "bad_rules"),
                        ({"name": " "}, "bad_name"), ({"salary": 1}, "bad_field")):
        with pytest.raises(InboxError) as e:
            world.do(inbox.update_agent, a["id"], **change)
        assert e.value.reason == why, change
    with pytest.raises(InboxError):
        world.do(inbox.update_agent, "11111111-1111-4111-8111-111111111111", name="x")


# ── routing ──────────────────────────────────────────────────────────────────

def test_least_busy_gives_the_chat_to_the_agent_with_fewer_open_chats(world):
    world.agent("Owner")
    a, b = world.agent("Asha"), world.agent("Bala")
    for _ in range(3):
        world.chat(status="intervened", assignee=a["id"])
    world.chat(status="intervened", assignee=b["id"])
    contact, conv = world.chat(status="active")
    assert world.do(inbox.request_human, conv)["id"] == b["id"]
    row = H.only(world.db, "select status, assignee_id, bot_paused from conversations where id = %s", (conv,))
    assert row == {"status": "requesting", "assignee_id": b["id"], "bot_paused": True}
    assert {"requested", "assigned"} <= {e["kind"] for e in world.db.q("select kind from conversation_events")}


def test_round_robin_takes_turns(world):
    world.agent("Owner")
    a, b = world.agent("Asha"), world.agent("Bala")
    world.do(inbox.save_settings, {"routing": "round_robin"})
    got = []
    for _ in range(4):
        _, conv = world.chat()
        got.append(world.do(inbox.request_human, conv)["id"])
    assert got == [a["id"], b["id"], a["id"], b["id"]]


def test_unavailable_agents_and_agents_outside_their_hours_are_skipped(world):
    world.agent("Owner", available=False)                                  # (owners are the fallback: switch that off)
    a, b = world.agent("Asha", available=False), world.agent("Bala", working_hours=HOURS)
    _, conv = world.chat()
    monday_10 = datetime(2026, 10, 19, 10, 0, tzinfo=IST)
    assert world.do(inbox.request_human, conv, now=monday_10)["id"] == b["id"]
    _, conv2 = world.chat()
    assert world.do(inbox.request_human, conv2, now=datetime(2026, 10, 19, 22, 0, tzinfo=IST)) is None     # nobody on
    assert world.db.one("select status from conversations where id = %s", (conv2,)) == "requesting"
    assert world.db.one("select assignee_id from conversations where id = %s", (conv2,)) is None


def test_an_agents_rules_limit_which_chats_they_get(world):
    world.agent("Owner")
    builder = world.agent("Asha", rules=[{"field": "tags", "op": "has", "value": "builder"}])
    general = world.agent("Bala")
    world.db.q("update agents set last_assigned_at = now() where id = %s", (general["id"],))     # make Asha first in line
    _, plain = world.chat(tags=["cold"])
    _, builder_chat = world.chat(tags=["builder"])
    assert world.do(inbox.request_human, plain)["id"] == general["id"]                          # Asha's rule excludes it
    assert world.do(inbox.request_human, builder_chat)["id"] == builder["id"]


def test_managers_and_owners_take_chats_only_when_no_agent_can(world):
    owner = world.agent("Owner")
    asha = world.agent("Asha", available=False)
    _, conv = world.chat()
    assert world.do(inbox.request_human, conv)["id"] == owner["id"]
    world.do(inbox.update_agent, asha["id"], available=True)
    _, conv2 = world.chat()
    assert world.do(inbox.request_human, conv2)["id"] == asha["id"]


def test_manual_routing_leaves_chats_unassigned(world):
    world.agent("Owner")
    world.agent("Asha")
    world.do(inbox.save_settings, {"routing": "manual"})
    _, conv = world.chat()
    assert world.do(inbox.request_human, conv) is None
    assert world.db.one("select status from conversations where id = %s", (conv,)) == "requesting"


def test_a_chat_a_person_already_owns_is_not_requested_again(world):
    a = world.agent("Asha")
    _, conv = world.chat(status="intervened", assignee=a["id"])
    assert world.do(inbox.request_human, conv) is None
    assert world.db.one("select status from conversations where id = %s", (conv,)) == "intervened"


# ── ownership ────────────────────────────────────────────────────────────────

def test_taking_a_chat_transferring_handing_back_and_resolving(world):
    owner, asha, bala = world.agent("Owner"), world.agent("Asha"), world.agent("Bala")
    _, conv = world.chat()
    world.do(inbox.intervene, conv, asha["id"])
    row = H.only(world.db, "select status, assignee_id, bot_paused from conversations")
    assert row == {"status": "intervened", "assignee_id": asha["id"], "bot_paused": True}

    with pytest.raises(InboxError) as e:
        world.do(inbox.intervene, conv, bala["id"])                                    # Asha has it
    assert e.value.reason == "taken"
    with pytest.raises(InboxError) as e:
        world.do(inbox.transfer, conv, bala["id"], bala["id"])                         # not Bala's to give away
    assert e.value.reason == "not_yours"

    world.do(inbox.transfer, conv, bala["id"], asha["id"])
    assert world.db.one("select assignee_id from conversations") == bala["id"]
    world.do(inbox.intervene, conv, owner["id"])                                       # an owner may override
    assert world.db.one("select assignee_id from conversations") == owner["id"]

    world.do(inbox.hand_back, conv, owner["id"])
    assert H.only(world.db, "select status, assignee_id, bot_paused from conversations") == \
        {"status": "active", "assignee_id": None, "bot_paused": False}
    world.do(inbox.intervene, conv, asha["id"])
    with pytest.raises(InboxError):
        world.do(inbox.resolve, conv, bala["id"])
    world.do(inbox.resolve, conv, asha["id"])
    row = H.only(world.db, "select status, bot_paused, resolved_at from conversations")
    assert row["status"] == "closed" and row["bot_paused"] is False and row["resolved_at"]
    kinds = [e["kind"] for e in world.db.q("select kind from conversation_events order by at, id")]
    assert {"intervened", "transferred", "handed_back", "resolved"} <= set(kinds)


def test_a_customer_writing_to_a_closed_chat_reopens_it_for_the_bot(world):
    a = world.agent("Asha")
    world.db.q("update tenants set slug = slug")                                         # no-op: keep the fixture honest
    H.deliver(world.db, P.inbound(P.text("hello", "wamid.1", str(int(time.time())))))
    H.work(world.db)
    conv = world.db.one("select id from conversations")
    world.do(inbox.intervene, conv, a["id"])
    world.do(inbox.resolve, conv, a["id"])
    H.deliver(world.db, P.inbound(P.text("one more thing", "wamid.2", str(int(time.time()) + 5))))
    H.work(world.db)
    row = H.only(world.db, "select status, assignee_id, resolved_at, bot_paused from conversations")
    assert row == {"status": "active", "assignee_id": None, "resolved_at": None, "bot_paused": False}
    assert "reopened" in {e["kind"] for e in world.db.q("select kind from conversation_events")}


def test_replying_as_an_agent_takes_the_chat(world):
    a, b = world.agent("Asha"), world.agent("Bala")
    cid, conv = world.chat()
    world.do(inbox.note_reply_by, cid, a["id"])
    assert world.db.one("select status from conversations") == "intervened"
    with pytest.raises(InboxError):
        world.do(inbox.note_reply_by, cid, b["id"])


# ── visibility ───────────────────────────────────────────────────────────────

def test_an_agent_sees_their_chats_and_unassigned_ones_but_not_a_colleagues(world):
    owner = world.agent("Owner")
    asha, bala = world.agent("Asha"), world.agent("Bala", masked=True)
    _, mine = world.chat("Mine", status="intervened", assignee=asha["id"])
    _, theirs = world.chat("Theirs", status="intervened", assignee=bala["id"])
    _, open_ = world.chat("Open", status="requesting")
    seen = lambda who, **kw: {r["contact_name"] for r in world.do(inbox.visible_conversations, who, **kw)}     # noqa: E731
    assert seen(asha) == {"Mine", "Open"}
    assert seen(bala) == {"Theirs", "Open"}
    assert seen(owner) == {"Mine", "Theirs", "Open"}
    assert seen(owner, status="requesting") == {"Open"}


def test_unassigned_chats_are_filtered_by_an_agents_rules_and_numbers_can_be_masked(world):
    world.agent("Owner")
    asha = world.agent("Asha", masked=True, rules=[{"field": "tags", "op": "has", "value": "builder"}])
    world.chat("Builder", tags=["builder"], phone="919876543210")
    world.chat("Cold", tags=["cold"])
    rows = world.do(inbox.visible_conversations, asha)
    assert [r["contact_name"] for r in rows] == ["Builder"] and rows[0]["contact_phone"] == "********3210"
    owner = world.do(inbox.list_agents)[0]
    assert {r["contact_phone"] for r in world.do(inbox.visible_conversations, owner) if r["contact_name"] == "Builder"} == {"919876543210"}


def as_session(world, role, agent_id=None):
    """A database session the way the service opens one for an agent: tenant-scoped, role set."""
    world.db.q("grant wa_app to current_user")
    conn = world.db.connect(role="wa_app", tenant=str(world.tid))
    with conn.cursor() as cur:
        cur.execute("select set_config('app.agent_role', %s, false), set_config('app.agent_id', %s, false)", (role, str(agent_id or "")))
    return conn


def test_the_database_itself_hides_another_agents_chat_its_messages_and_its_notes(world):
    owner, asha, bala = world.agent("Owner"), world.agent("Asha"), world.agent("Bala")
    _, theirs = world.chat("Theirs", status="intervened", assignee=bala["id"])
    _, mine = world.chat("Mine", status="intervened", assignee=asha["id"])
    _, open_ = world.chat("Open")
    for conv in (theirs, mine):
        world.db.q("insert into messages (tenant_id, conversation_id, direction, text) values (%s,%s,'in',%s)", (world.tid, conv, "secret %s" % conv))
        world.db.q("insert into conversation_notes (tenant_id, conversation_id, agent_id, text) values (%s,%s,%s,'private')", (world.tid, conv, bala["id"]))
        world.db.q("insert into conversation_events (tenant_id, conversation_id, kind) values (%s,%s,'assigned')", (world.tid, conv))

    agent = as_session(world, "agent", asha["id"])
    ids = {r["id"] for r in world.db.q("select id from conversations", conn=agent)}
    assert ids == {mine, open_}
    assert {r["conversation_id"] for r in world.db.q("select conversation_id from messages", conn=agent)} == {mine}
    assert {r["conversation_id"] for r in world.db.q("select conversation_id from conversation_notes", conn=agent)} == {mine}
    assert {r["conversation_id"] for r in world.db.q("select conversation_id from conversation_events", conn=agent)} == {mine}
    with pytest.raises(psycopg2.Error):                                              # and cannot write into it either
        world.db.q("update conversations set status = 'closed' where id = %s returning id", (theirs,), conn=agent) or (_ for _ in ()).throw(psycopg2.Error("no row"))

    for role in ("manager", "owner", "service"):
        seen = world.db.q("select id from conversations", conn=as_session(world, role, owner["id"]))
        assert len(seen) == 3, role


def test_an_agent_session_cannot_hand_itself_another_agents_chat(world):
    asha, bala = world.agent("Asha"), world.agent("Bala")
    world.agent("Owner")
    _, theirs = world.chat(status="intervened", assignee=bala["id"])
    agent = as_session(world, "agent", asha["id"])
    updated = world.db.q("update conversations set assignee_id = %s where id = %s returning id", (asha["id"], theirs), conn=agent)
    assert updated == []
    assert world.db.one("select assignee_id from conversations where id = %s", (theirs,)) == bala["id"]


# ── notes and saved replies ──────────────────────────────────────────────────

def test_notes_are_kept_in_order_and_never_sent(world):
    a = world.agent("Asha")
    _, conv = world.chat()
    world.do(inbox.add_note, conv, a["id"], "Wants the red one")
    world.do(inbox.add_note, conv, a["id"], "Called back")
    notes = world.do(inbox.list_notes, conv)
    assert [n["text"] for n in notes] == ["Wants the red one", "Called back"] and notes[0]["agent"] == "Asha"
    assert world.db.one("select count(*) from outbox") == 0
    for bad in ("", "   ", "x" * 4001):
        with pytest.raises(InboxError):
            world.do(inbox.add_note, conv, a["id"], bad)


def test_saved_replies_have_shortcuts_and_fill_in_the_customers_name(world):
    world.do(inbox.save_canned, "/Thanks", "Thank you {{first_name}}, we will call you on {{ mobile }}.")
    world.do(inbox.save_canned, "thanks", "Thanks again {{name}}")                       # same shortcut: replaced
    world.do(inbox.save_canned, "hours", "We are open 9 to 6, {{first_name}}.")
    cid, _ = world.chat("Asha Shah", phone="919876543210")
    assert world.do(inbox.expand, "/thanks", cid) == "Thanks again Asha Shah"
    assert world.do(inbox.expand, "hours", cid) == "We are open 9 to 6, Asha."
    assert [c["shortcut"] for c in world.do(inbox.list_canned)] == ["hours", "thanks"]
    assert world.do(inbox.delete_canned, "/hours") is True and world.do(inbox.delete_canned, "hours") is False
    with pytest.raises(InboxError):
        world.do(inbox.expand, "hours", cid)
    for shortcut, body in (("Bad Shortcut", "x"), ("", "x"), ("ok", " "), ("ok", "x" * 1001)):
        with pytest.raises(InboxError):
            world.do(inbox.save_canned, shortcut, body)


# ── auto-replies ─────────────────────────────────────────────────────────────

def setup_auto(world, **cfg):
    world.do(inbox.save_settings, cfg)


def hear(world, text="hello", wamid="wamid.1", when=None):
    ts = str(int((when or utcnow()).timestamp()))
    H.deliver(world.db, P.inbound(P.text(text, wamid, ts)))
    H.work(world.db)


def autos(world):
    return [r["payload"]["text"]["body"] for r in world.db.q("select payload from outbox order by created_at")]


def test_a_welcome_is_sent_once_in_opening_hours(world):
    setup_auto(world, welcome={"enabled": True, "text": "Welcome!", "once_per_hours": 24})
    now = utcnow()
    hear(world, "hi", "wamid.1", now)
    hear(world, "hello?", "wamid.2", now + timedelta(minutes=5))
    assert autos(world) == ["Welcome!"]
    assert world.db.one("select sender from messages where direction = 'out'") == "autoreply"
    later = now + timedelta(hours=25)
    hear(world, "back again", "wamid.3", later)
    assert autos(world) == ["Welcome!", "Welcome!"]


def test_when_closed_the_off_hours_message_goes_instead_of_the_welcome(world):
    hours = {"tz": "Asia/Kolkata", "days": {"mon": [["09:00", "18:00"]]}}
    setup_auto(world, business_hours=hours, welcome={"enabled": True, "text": "Welcome!", "once_per_hours": 24},
               off_hours={"enabled": True, "text": "We are closed.", "once_per_hours": 12})
    cid, conv = world.chat("Customer")
    sunday = datetime(2026, 10, 18, 11, 0, tzinfo=IST)                       # a Sunday: closed
    monday = datetime(2026, 10, 19, 11, 0, tzinfo=IST)                       # a Monday morning: open

    def reply(when, wamid):
        conn = world.conn()
        try:
            with conn.cursor() as cur:
                cur.execute("update conversations set last_inbound_at = %s where id = %s", (when, conv))
                out = inbox.maybe_auto_reply(cur, world.tid, cid, conv, when, wamid=wamid)
            conn.commit()
            return out
        finally:
            conn.close()

    assert reply(sunday, "w1") == "off_hours"
    assert reply(sunday + timedelta(hours=1), "w2") is None                  # once per 12 hours
    assert reply(monday, "w3") == "welcome"
    assert reply(monday + timedelta(hours=2), "w4") is None
    assert autos(world) == ["We are closed.", "Welcome!"]


def test_no_auto_reply_when_a_person_has_the_chat_or_it_is_switched_off_or_it_was_a_keyword(world):
    a = world.agent("Asha")
    setup_auto(world, welcome={"enabled": False})
    hear(world, "hi", "wamid.1")
    assert autos(world) == []
    setup_auto(world, welcome={"enabled": True, "text": "Welcome!", "once_per_hours": 1})
    conv = world.db.one("select id from conversations")
    world.do(inbox.intervene, conv, a["id"])
    hear(world, "still there?", "wamid.2", utcnow() + timedelta(hours=3))
    assert autos(world) == []
    world.do(inbox.hand_back, conv, a["id"])
    hear(world, "STOP", "wamid.3", utcnow() + timedelta(hours=6))
    assert "Welcome!" not in autos(world)                                                # the opt-out confirmation, not a greeting


# ── housekeeping ─────────────────────────────────────────────────────────────

def test_idle_chats_close_themselves_after_a_day_by_default(world):
    a = world.agent("Asha")
    old = utcnow() - timedelta(hours=30)
    _, stale = world.chat("Stale", last_active=old)
    _, stale_owned = world.chat("Owned", status="intervened", assignee=a["id"], last_active=old)
    _, fresh = world.chat("Fresh")
    _, done = world.chat("Done", status="closed", last_active=old)
    assert inbox.auto_resolve(world.conn) == 2
    states = {r["name"]: r["status"] for r in world.db.q("select c.name, v.status from conversations v join contacts c on c.id = v.contact_id")}
    assert states == {"Stale": "closed", "Owned": "closed", "Fresh": "active", "Done": "closed"}
    assert world.db.one("select count(*) from conversation_events where kind = 'resolved'") == 2
    assert inbox.auto_resolve(world.conn) == 0


def test_the_idle_time_is_a_setting_and_zero_means_never(world):
    _, c = world.chat(last_active=utcnow() - timedelta(hours=30))
    world.do(inbox.save_settings, {"auto_resolve_hours": 48})
    assert inbox.auto_resolve(world.conn) == 0
    world.do(inbox.save_settings, {"auto_resolve_hours": 12})
    assert inbox.auto_resolve(world.conn) == 1
    _, c2 = world.chat(last_active=utcnow() - timedelta(days=30))
    world.do(inbox.save_settings, {"auto_resolve_hours": 0})
    assert inbox.auto_resolve(world.conn) == 0


def test_auto_resolve_only_touches_each_workspaces_own_chats_by_its_own_setting(world):
    other = H.tenant(world.db, "shakti")
    oc = world.db.one("insert into contacts (tenant_id, name, phone) values (%s,'X','919811111111') returning id", (other,))
    world.db.q("insert into conversations (tenant_id, contact_id, channel, status, last_active_at) values (%s,%s,'whatsapp','active',%s)",
               (other, oc, utcnow() - timedelta(hours=30)))
    world.db.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"inbox\": {\"auto_resolve_hours\": 0}}')", (other,))
    world.chat(last_active=utcnow() - timedelta(hours=30))
    assert inbox.auto_resolve(world.conn) == 1
    assert world.db.one("select status from conversations where tenant_id = %s", (other,)) == "active"


# ── the manager's view ───────────────────────────────────────────────────────

def test_the_monitor_counts_states_agents_unassigned_and_slow_answers(world):
    owner, asha = world.agent("Owner"), world.agent("Asha")
    now = utcnow()
    _, c1 = world.chat("A", status="requesting")
    _, c2 = world.chat("B", status="intervened", assignee=asha["id"])
    _, c3 = world.chat("C", status="closed")
    world.db.q("update conversations set requested_at = %s where id = %s", (now - timedelta(minutes=40), c1))
    # B: the customer wrote, an agent answered 2 minutes later; another turn answered after 4 minutes
    for ago, direction, sender in ((60, "in", "customer"), (58, "out", "agent"), (30, "in", "customer"), (26, "out", "agent")):
        world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, created_at) values (%s,%s,%s,%s,'x',%s)",
                   (world.tid, c2, direction, sender, now - timedelta(minutes=ago)))
    m = world.do(inbox.monitor, now=now)
    assert m["by_state"] == {"requesting": 1, "intervened": 1, "closed": 1}
    assert m["unassigned_requesting"] == 1 and m["sla_breached"] == 1 and m["sla_minutes"] == 15
    assert m["first_response_seconds"] == 180.0 and m["answered_last_7_days"] == 2
    assert {a["name"]: a["open"] for a in m["agents"]} == {"Asha": 1, "Owner": 0}


def test_a_requesting_chat_that_an_agent_answered_is_not_an_sla_breach(world):
    now = utcnow()
    _, c = world.chat(status="requesting")
    world.db.q("update conversations set requested_at = %s where id = %s", (now - timedelta(minutes=40), c))
    world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, created_at) values (%s,%s,'out','agent','hi',%s)",
               (world.tid, c, now - timedelta(minutes=10)))
    assert world.do(inbox.monitor, now=now)["sla_breached"] == 0


# ── settings ─────────────────────────────────────────────────────────────────

def test_inbox_settings_round_trip_alongside_consent_settings(world):
    from prism_wa import consent
    world.do(consent.save_settings, {"opt_out_keywords": ["stop", "band"]})
    saved = world.do(inbox.save_settings, {"routing": "round_robin", "sla_minutes": 5, "business_hours": HOURS,
                                           "welcome": {"enabled": True, "text": "Hi there"}})
    assert saved["routing"] == "round_robin" and saved["welcome"]["once_per_hours"] == 24 and saved["off_hours"]["enabled"] is False
    assert world.do(inbox.settings)["business_hours"] == HOURS
    assert world.do(consent.settings)["opt_out_keywords"] == ["stop", "band"]            # neither saved over the other
    world.do(consent.save_settings, {"confirm": False})
    assert world.do(inbox.settings)["routing"] == "round_robin"


@pytest.mark.parametrize("patch", [{"routing": "random"}, {"auto_resolve_hours": -1}, {"auto_resolve_hours": "24"}, {"sla_minutes": 0},
                                   {"business_hours": {"days": []}}, {"welcome": {"text": " "}}, {"off_hours": {"once_per_hours": 0}},
                                   {"auto_resolve_hours": True}])
def test_bad_inbox_settings_are_refused(world, patch):
    with pytest.raises(InboxError) as e:
        world.do(inbox.save_settings, patch)
    assert e.value.reason in ("bad_settings", "bad_hours")


# ── over HTTP ────────────────────────────────────────────────────────────────

def test_the_http_api_for_the_inbox_and_agent_replies(world):
    vault = Vault({1: os.urandom(32)})
    app = create_app(app_secrets="s", verify_token="v", connect=world.conn, internal_token="tok", graph=Graph(lambda *a: (404, b"{}")), vault=vault)
    c = TestClient(app)

    def post(path, **body):
        body.setdefault("tenant_id", str(world.tid))
        return c.post(path, json=body, headers={"X-Internal-Token": "tok"})

    owner = post("/internal/inbox/ensure_agent", seat_key="s1", name="Owner").json()["agent"]
    asha = post("/internal/inbox/ensure_agent", seat_key="s2", name="Asha").json()["agent"]
    assert (owner["role"], asha["role"]) == ("owner", "agent")
    cid, conv = world.chat("Customer")

    assert post("/internal/inbox/intervene", conversation_id=str(conv), agent_id=asha["id"]).status_code == 200
    seen = post("/internal/inbox/conversations", agent_id=asha["id"]).json()["conversations"]
    assert [x["contact_name"] for x in seen] == ["Customer"] and seen[0]["status"] == "intervened"
    # the agent replies, the reply is queued, and the chat stays theirs
    r = post("/internal/reply", contact_id=str(cid), text="hello", agent_id=asha["id"], idem_key="r1")
    assert r.status_code == 200, r.json()
    # a colleague replying is refused
    bala = post("/internal/inbox/ensure_agent", seat_key="s3", name="Bala").json()["agent"]
    r = post("/internal/reply", contact_id=str(cid), text="mine now", agent_id=bala["id"], idem_key="r2")
    assert r.status_code == 409 and r.json()["error"] == "taken"
    # only a manager or owner may change people, settings, or watch the team
    assert post("/internal/inbox/agents/update", agent_id=bala["id"], by_agent_id=asha["id"], changes={"role": "manager"}).json()["error"] == "not_allowed"
    assert post("/internal/inbox/agents/update", agent_id=bala["id"], by_agent_id=owner["id"], changes={"role": "manager"}).json()["agent"]["role"] == "manager"
    assert post("/internal/inbox/monitor", by_agent_id=asha["id"]).json()["error"] == "not_allowed"
    assert post("/internal/inbox/monitor", by_agent_id=owner["id"]).json()["monitor"]["by_state"] == {"intervened": 1}
    assert post("/internal/inbox/settings/save", by_agent_id=asha["id"], settings={"routing": "manual"}).json()["error"] == "not_allowed"
    assert post("/internal/inbox/settings/save", by_agent_id=owner["id"], settings={"routing": "manual"}).json()["settings"]["routing"] == "manual"
    post("/internal/inbox/canned/save", shortcut="hi", body="Hello {{first_name}}")
    assert post("/internal/inbox/canned/expand", shortcut="hi", contact_id=str(cid)).json()["text"] == "Hello Customer"
    assert post("/internal/inbox/resolve", conversation_id=str(conv), agent_id=asha["id"]).status_code == 200
