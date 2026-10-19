"""Automatic messages: a template on, before or after a date a contact has, and drips — against a real Postgres, with the clock under the test's control."""
import json
from datetime import datetime, timedelta, timezone

import helpers as H
import psycopg2
import pytest

from prism_wa import autos, contacts, plans

BODY = {"type": "BODY", "text": "Hello {{1}}, wishing you a very happy day from all of us at Shakti.", "example": {"body_text": [["Asha"]]}}
TEMPLATE = {"name": "greeting", "language": "en_US", "variables": {"1": {"source": "field", "field": "first_name"}}}
IST = timezone(timedelta(hours=5, minutes=30))


def at(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture
def world(migrated):
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    waba = migrated.one("select id from wabas")
    for name, status in (("greeting", "APPROVED"), ("followup", "APPROVED"), ("offer", "APPROVED"), ("draft_one", "PENDING")):
        migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s, %s, %s, 'en_US', 'MARKETING', %s, %s)",
                   (tid, waba, name, status, json.dumps([BODY])))

    class W:
        db = migrated

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
        def person(cls, name="Asha Shah", phone="919800100001", dates=None, tags=(), opt_out=False):
            conn = cls.conn()
            with conn.cursor() as cur:
                for key in (dates or {}):
                    if key not in contacts.attribute_defs(cur, tid):
                        contacts.define_attribute(cur, tid, key, "date")
                cur.execute("insert into contacts (tenant_id, name, phone, tags, opted_in, opt_out) values (%s, %s, %s, %s, true, %s) returning id", (tid, name, phone, list(tags), opt_out))
                cid = cur.fetchone()[0]
                for key, value in (dates or {}).items():
                    contacts.set_attribute(cur, tid, cid, key, value)
            conn.commit()
            conn.close()
            return cid

        @classmethod
        def run(cls, now):
            conn = cls.conn()
            with conn.cursor() as cur:
                n = autos.run_due(cur, now=now)
            conn.commit()
            conn.close()
            return n

        @classmethod
        def sent_to(cls):
            return [r["to_phone"] for r in migrated.q("select to_phone from outbox order by created_at, id")]

        tid_ = tid
    W.tid = tid
    return W


def birthday(world, **kw):
    cfg = {"attribute": "Birthday", "match": "yearly", "offset_days": 0, "time": "10:00", "tz": "Asia/Kolkata", "template": TEMPLATE, **kw}
    return cfg


def make_date(world, enabled=True, now=None, **kw):
    aid = world.do(autos.create, "date", "Birthday", birthday(world, **kw))
    if enabled:
        world.do(autos.set_enabled, aid, True, now=now or at(2026, 10, 1))
    return aid


# ── what may be asked for ────────────────────────────────────────────────────

@pytest.mark.parametrize("config,reason", [
    ({"attribute": "Nope"}, "bad_attribute"), ({"attribute": "Birthday", "match": "weekly"}, "bad_match"), ({"attribute": "Birthday", "offset_days": 90}, "bad_offset"),
    ({"attribute": "Birthday", "offset_days": True}, "bad_offset"), ({"attribute": "Birthday", "time": "10am"}, "bad_time"), ({"attribute": "Birthday", "tz": "Mars/Base"}, "bad_time"),
    ({"attribute": "Birthday", "tags": ["a"] * 6}, "bad_tags"), ({"attribute": "Birthday", "template": {}}, "bad_template"),
    ({"attribute": "Birthday", "template": {"name": "greeting", "variables": {"1": {"source": "tracked_link", "target": "https://x.example"}}}}, "bad_template"),
])
def test_a_date_message_is_checked_in_words(world, config, reason):
    world.person(dates={"Birthday": "1990-05-04"})
    cfg = {"template": TEMPLATE, **config}
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.create, "date", "Birthday", cfg)
    assert why.value.reason == reason and str(why.value)


def test_only_a_detail_that_holds_a_date_can_be_used(world):
    conn = world.conn()
    with conn.cursor() as cur:
        contacts.define_attribute(cur, world.tid, "City", "string")
    conn.commit()
    conn.close()
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.create, "date", "Birthday", birthday(world, attribute="City"))
    assert why.value.reason == "bad_attribute" and "Date" in str(why.value)


@pytest.mark.parametrize("config,reason", [
    ({"trigger": {"type": "carrier_pigeon"}}, "bad_trigger"), ({"trigger": {"type": "tag"}}, "bad_trigger"), ({"steps": []}, "bad_steps"), ({"steps": [{}] * 11}, "bad_steps"),
    ({"steps": [{"delay_minutes": -1, "template": TEMPLATE}]}, "bad_delay"), ({"steps": [{"delay_minutes": 99999999, "template": TEMPLATE}]}, "bad_delay"),
    ({"stop": {"tags": ["a"] * 6}}, "bad_stop"), ({"quiet": {"from": "09:00", "to": "09:00"}}, "bad_quiet"), ({"quiet": {"from": "9am", "to": "6pm"}}, "bad_quiet"),
])
def test_a_drip_is_checked_in_words(world, config, reason):
    cfg = {"trigger": {"type": "manual"}, "steps": [{"delay_minutes": 0, "template": TEMPLATE}], **config}
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.create, "drip", "Welcome", cfg)
    assert why.value.reason == reason


def test_a_name_is_needed_and_a_workspace_has_a_limit(world, monkeypatch):
    world.person(dates={"Birthday": "1990-05-04"})
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.create, "date", " ", birthday(world))
    assert why.value.reason == "bad_name"
    monkeypatch.setattr(autos, "MAX_AUTOS", 1)
    world.do(autos.create, "date", "One", birthday(world))
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.create, "date", "Two", birthday(world))
    assert why.value.reason == "too_many"


def test_a_plan_without_scheduling_cannot_make_one(world):
    world.person(dates={"Birthday": "1990-05-04"})
    world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}'")
    with pytest.raises(plans.PlanLimit):
        world.do(autos.create, "date", "Birthday", birthday(world))


def test_it_cannot_be_switched_on_with_a_template_that_is_not_approved(world):
    world.person(dates={"Birthday": "1990-05-04"})
    aid = world.do(autos.create, "date", "Birthday", birthday(world, template={"name": "draft_one", "language": "en_US"}))
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.set_enabled, aid, True)
    assert why.value.reason == "template_not_approved" and "pending" in str(why.value)
    aid2 = world.do(autos.create, "date", "Birthday 2", birthday(world, template={"name": "ghost", "language": "en_US"}))
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.set_enabled, aid2, True)
    assert why.value.reason == "template_unknown"


# ── birthdays ────────────────────────────────────────────────────────────────

def test_a_birthday_message_goes_to_whoever_has_a_birthday_today_and_nobody_else(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    world.person("Bala", "919800100002", {"Birthday": "1985-10-21"})
    world.person("Chitra", "919800100003", {"Birthday": "2000-03-04"})
    make_date(world)
    assert world.run(at(2026, 10, 19)) == 0                                   # before the check time
    assert world.run(at(2026, 10, 20, 10, 5)) == 1
    assert world.sent_to() == ["919800100001"]
    assert world.db.one("select count(*) from auto_log where state = 'sent' and occasion = '2026'") == 1


def test_the_greeting_is_filled_in_for_each_person_and_goes_out_as_a_template(world):
    world.person("Asha Shah", "919800100001", {"Birthday": "1990-10-20"})
    make_date(world)
    world.run(at(2026, 10, 20, 10, 5))
    payload = world.db.one("select payload from outbox")
    assert payload["template"]["name"] == "greeting" and payload["template"]["components"][0]["parameters"][0]["text"] == "Asha"
    assert world.db.one("select sender from messages where direction = 'out'") == "bot"


def test_nobody_gets_the_same_birthday_message_twice_even_if_the_check_runs_again(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    aid = make_date(world)
    world.run(at(2026, 10, 20, 10, 5))
    world.db.q("update autos set next_run_at = %s", (at(2026, 10, 20, 10, 0),))
    assert world.run(at(2026, 10, 20, 10, 20)) == 0 and len(world.sent_to()) == 1
    assert world.db.one("select runs from autos") == 2 and world.db.one("select next_run_at from autos") > at(2026, 10, 20, 10, 20)
    world.run(at(2027, 10, 20, 10, 5))                                         # next year is a new occasion
    assert len(world.sent_to()) == 2 and aid


def test_someone_who_opted_out_is_never_messaged_and_the_log_says_nothing_was_sent(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"}, opt_out=True)
    make_date(world)
    assert world.run(at(2026, 10, 20, 10, 5)) == 0 and world.sent_to() == []
    assert world.db.one("select count(*) from auto_log") == 0


def test_a_message_the_queue_refuses_is_logged_as_skipped_with_the_reason(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    make_date(world)
    world.db.q("update templates set status = 'PAUSED' where name = 'greeting'")
    assert world.run(at(2026, 10, 20, 10, 5)) == 0
    row = world.db.q("select state, reason from auto_log")[0]
    assert row["state"] == "skipped" and "paused" in row["reason"] and world.sent_to() == []


def test_a_message_can_go_a_few_days_before_or_after_the_date(world):
    world.person("Asha", "919800100001", {"Appointment": "2026-10-25"})
    aid = world.do(autos.create, "date", "Reminder", birthday(world, attribute="Appointment", match="once", offset_days=-1))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    assert world.run(at(2026, 10, 23, 10, 5)) == 0
    assert world.run(at(2026, 10, 24, 10, 5)) == 1
    after = world.do(autos.create, "date", "Chase", birthday(world, attribute="Appointment", match="once", offset_days=3))
    world.do(autos.set_enabled, after, True, now=at(2026, 10, 1))
    assert world.run(at(2026, 10, 28, 10, 5)) == 1
    assert world.db.one("select count(*) from auto_log where occasion = '2026-10-25'") == 2


def test_a_one_off_date_does_not_come_round_next_year(world):
    world.person("Asha", "919800100001", {"Appointment": "2026-10-25"})
    aid = world.do(autos.create, "date", "Reminder", birthday(world, attribute="Appointment", match="once"))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    assert world.run(at(2026, 10, 25, 10, 5)) == 1
    assert world.run(at(2027, 10, 25, 10, 5)) == 0 and len(world.sent_to()) == 1


def test_changing_the_date_sends_a_new_reminder_for_the_new_date(world):
    cid = world.person("Asha", "919800100001", {"Appointment": "2026-10-25"})
    aid = world.do(autos.create, "date", "Reminder", birthday(world, attribute="Appointment", match="once"))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    world.run(at(2026, 10, 25, 10, 5))
    conn = world.conn()
    with conn.cursor() as cur:
        contacts.set_attribute(cur, world.tid, cid, "Appointment", "2026-11-02")
    conn.commit()
    conn.close()
    world.run(at(2026, 11, 2, 10, 5))
    assert len(world.sent_to()) == 2


def test_a_29_february_birthday_is_kept_on_the_28th_in_other_years(world):
    world.person("Leap", "919800100001", {"Birthday": "2000-02-29"})
    make_date(world)
    assert world.run(at(2027, 2, 28, 10, 5)) == 1
    world.db.q("delete from auto_log")
    world.db.q("update autos set next_run_at = %s", (at(2028, 2, 28, 10, 0),))
    assert world.run(at(2028, 2, 29, 10, 5)) == 1                              # in a leap year it is the 29th


def test_only_people_with_a_tag_can_be_chosen(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"}, tags=("vip",))
    world.person("Bala", "919800100002", {"Birthday": "1990-10-20"})
    make_date(world, tags=["vip"])
    world.run(at(2026, 10, 20, 10, 5))
    assert world.sent_to() == ["919800100001"]


def test_a_worker_that_was_down_catches_up_the_last_two_days_only(world):
    for i, day in enumerate(("2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20")):
        world.person("P%d" % i, "91980010000%d" % i, {"Birthday": "1990-" + day[5:]})
    make_date(world)
    world.db.q("update autos set next_run_at = %s", (at(2026, 10, 17, 10, 0),))
    world.run(at(2026, 10, 20, 11, 0))
    assert sorted(world.sent_to()) == ["919800100001", "919800100002", "919800100003"]               # the 18th, 19th and 20th; the 17th is too far back


def test_the_preview_says_who_would_be_reached_on_each_coming_day(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    world.person("Bala", "919800100002", {"Birthday": "1990-10-20"})
    world.person("Chitra", "919800100003", {"Birthday": "1990-10-22"})
    cfg = birthday(world)
    del cfg["template"]                                                        # who it would reach does not depend on the template
    out = world.do(autos.preview, cfg, days=5, now=at(2026, 10, 19))
    assert [d["count"] for d in out["days"]] == [0, 2, 0, 1, 0] and out["total"] == 3 and out["days"][1]["names"] == ["Asha", "Bala"]


def test_a_changed_message_is_checked_again_and_the_next_check_follows_the_new_time(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    aid = make_date(world, now=at(2026, 10, 19, 9, 0))
    world.do(autos.update, aid, name="Birthdays", config=birthday(world, time="08:30"))
    got = world.db.q("select name, config, next_run_at from autos")[0]
    assert got["name"] == "Birthdays" and got["config"]["time"] == "08:30" and got["next_run_at"].astimezone(IST).strftime("%H:%M") == "08:30"


def test_switching_off_stops_the_checks(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    aid = make_date(world)
    world.do(autos.set_enabled, aid, False)
    assert world.run(at(2026, 10, 20, 10, 5)) == 0 and world.db.one("select next_run_at from autos") is None


# ── drips ────────────────────────────────────────────────────────────────────

def drip_config(**kw):
    return {"trigger": {"type": "tag", "tag": "new-lead"}, "tz": "Asia/Kolkata", "stop": {"on_reply": True, "tags": ["customer"]},
            "steps": [{"delay_minutes": 0, "template": {"name": "greeting", "language": "en_US"}},
                      {"delay_minutes": 2 * 24 * 60, "template": {"name": "followup", "language": "en_US"}},
                      {"delay_minutes": 5 * 24 * 60, "template": {"name": "offer", "language": "en_US"}}], **kw}


def sent_names(world):
    return [r["payload"]["template"]["name"] for r in world.db.q("select payload from outbox order by created_at, id")]


def test_people_who_already_have_the_tag_are_not_chased_when_a_drip_is_switched_on(world):
    old = world.person("Old", "919800100001", tags=("new-lead",))
    aid = world.do(autos.create, "drip", "Welcome", drip_config())
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    new = world.person("New", "919800100002")
    conn = world.conn()
    with conn.cursor() as cur:
        cur.execute("update contacts set tags = '{new-lead}' where id = %s", (new,))
    conn.commit()
    conn.close()
    world.run(at(2026, 10, 1, 12))
    assert world.sent_to() == ["919800100002"]
    assert world.db.one("select reason from auto_enrolments where contact_id = %s", (old,)) == "had the tag before this was switched on"


def test_they_can_be_included_when_the_owner_says_so(world):
    world.person("Old", "919800100001", tags=("new-lead",))
    aid = world.do(autos.create, "drip", "Welcome", drip_config(include_existing=True))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    world.run(at(2026, 10, 1, 12))
    assert world.sent_to() == ["919800100001"]


def test_each_message_of_a_drip_goes_out_after_its_own_wait(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config())
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    world.person("Asha", "919800100001", tags=("new-lead",))
    world.run(at(2026, 10, 1, 12))
    assert sent_names(world) == ["greeting"]
    world.run(at(2026, 10, 2, 12))
    assert sent_names(world) == ["greeting"]                                   # two days have not passed
    world.run(at(2026, 10, 3, 12, 5))
    assert sent_names(world) == ["greeting", "followup"]
    world.run(at(2026, 10, 8, 13))
    assert sent_names(world) == ["greeting", "followup", "offer"]
    row = world.db.q("select state, step from auto_enrolments")[0]
    assert (row["state"], row["step"]) == ("done", 3)
    world.run(at(2026, 10, 20))
    assert len(sent_names(world)) == 3                                         # finished means finished


def test_a_reply_stops_the_drip(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config())
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    world.person("Asha", "919800100001", tags=("new-lead",))
    world.run(at(2026, 10, 1, 12))
    cid = world.db.one("select id from contacts")
    conv = world.db.one("select id from conversations where contact_id = %s", (cid,))
    world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, channel, msg_type, status, created_at)"
               " values (%s, %s, 'in', 'customer', 'yes please', 'whatsapp', 'text', 'received', %s)", (world.tid, conv, at(2026, 10, 2, 9)))
    world.run(at(2026, 10, 3, 12, 5))
    assert sent_names(world) == ["greeting"] and world.db.one("select reason from auto_enrolments") == "replied"


def test_a_stop_tag_or_an_opt_out_ends_it(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config())
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    a = world.person("Asha", "919800100001", tags=("new-lead",))
    b = world.person("Bala", "919800100002", tags=("new-lead",))
    world.run(at(2026, 10, 1, 12))
    world.db.q("update contacts set tags = '{new-lead,customer}' where id = %s", (a,))
    world.db.q("update contacts set opt_out = true where id = %s", (b,))
    world.run(at(2026, 10, 3, 12, 5))
    reasons = {r["contact_id"]: r["reason"] for r in world.db.q("select contact_id, reason from auto_enrolments")}
    assert reasons[a] == "got the tag “customer”" and reasons[b] == "opted out" and sent_names(world) == ["greeting", "greeting"]


def test_quiet_hours_hold_a_message_until_the_window_opens(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config(quiet={"from": "09:00", "to": "20:00"}))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    world.person("Asha", "919800100001", tags=("new-lead",))
    world.run(at(2026, 10, 1, 23, 0))
    assert sent_names(world) == []
    due = world.db.one("select next_at from auto_enrolments").astimezone(IST)
    assert (due.day, due.hour, due.minute) == (2, 9, 0)
    world.run(at(2026, 10, 2, 9, 1))
    assert sent_names(world) == ["greeting"]


def test_a_window_that_runs_past_midnight_works(world):
    from prism_wa.autos import _in_window
    q = {"from": "20:00", "to": "06:00"}
    assert _in_window(q, datetime(2026, 10, 1, 23, 0, tzinfo=IST)) is None and _in_window(q, datetime(2026, 10, 1, 3, 0, tzinfo=IST)) is None
    assert _in_window(q, datetime(2026, 10, 1, 12, 0, tzinfo=IST)) == datetime(2026, 10, 1, 20, 0, tzinfo=IST)
    assert _in_window(None, datetime(2026, 10, 1, 12, 0, tzinfo=IST)) is None


def test_people_can_be_added_to_a_drip_by_hand_once_each(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config(trigger={"type": "manual"}))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    a = world.person("Asha", "919800100001", tags=("vip",))
    world.person("Bala", "919800100002", tags=("vip",), opt_out=True)
    out = world.do(autos.enrol, aid, {"tags": ["vip"]}, now=at(2026, 10, 1, 12))
    assert out["enrolled"] == 1 and out["left_out"] == 1
    again = world.do(autos.enrol, aid, {"contact_ids": [str(a)]}, now=at(2026, 10, 1, 12))
    assert again["enrolled"] == 0 and again["already_in"] == 1
    world.run(at(2026, 10, 1, 12))
    assert world.sent_to() == ["919800100001"]


def test_only_a_drip_takes_people_by_hand(world):
    world.person(dates={"Birthday": "1990-10-20"})
    aid = make_date(world, enabled=False)
    with pytest.raises(autos.AutoError) as why:
        world.do(autos.enrol, aid, {"tags": ["x"]})
    assert why.value.reason == "not_a_drip"


def test_opting_in_after_the_drip_is_on_starts_it(world):
    aid = world.do(autos.create, "drip", "Welcome", drip_config(trigger={"type": "opt_in"}))
    world.do(autos.set_enabled, aid, True, now=at(2026, 10, 1))
    before = world.person("Before", "919800100001")
    after = world.person("After", "919800100002")
    world.db.q("insert into consent_ledger (tenant_id, contact_id, action, source, at) values (%s, %s, 'opt_in', 'manual', %s)", (world.tid, before, at(2026, 9, 1)))
    world.db.q("insert into consent_ledger (tenant_id, contact_id, action, source, at) values (%s, %s, 'opt_in', 'keyword', %s)", (world.tid, after, at(2026, 10, 2)))
    world.run(at(2026, 10, 2, 12))
    assert world.sent_to() == ["919800100002"]


# ── looking at them ──────────────────────────────────────────────────────────

def test_the_list_counts_what_was_sent_skipped_and_who_is_still_in_a_drip(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    date_id = make_date(world)
    drip_id = world.do(autos.create, "drip", "Welcome", drip_config())
    world.do(autos.set_enabled, drip_id, True, now=at(2026, 10, 1))
    world.person("Bala", "919800100002", tags=("new-lead",))
    world.run(at(2026, 10, 20, 10, 5))
    rows = {r["id"]: r for r in world.do(autos.list_autos)}
    assert rows[date_id]["sent"] == 1 and rows[drip_id]["active"] == 1 and rows[drip_id]["sent"] == 1 and rows[date_id]["kind"] == "date"
    got = world.do(autos.get, drip_id)
    assert got["active"] == 1 and got["sent"] == 1 and got["done"] == 0
    log = world.do(autos.recent, date_id)
    assert log[0]["name"] == "Asha" and log[0]["state"] == "sent" and log[0]["occasion"] == "2026"


def test_removing_an_automatic_message_takes_its_log_and_people_with_it(world):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    aid = make_date(world)
    world.run(at(2026, 10, 20, 10, 5))
    assert world.do(autos.delete, aid) is True and world.db.one("select count(*) from auto_log") == 0
    with pytest.raises(autos.AutoError):
        world.do(autos.get, aid)


def test_another_workspace_cannot_reach_one(world, migrated):
    world.person(dates={"Birthday": "1990-10-20"})
    aid = make_date(world, enabled=False)
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    conn = world.conn()
    with conn.cursor() as cur:
        with pytest.raises(autos.AutoError):
            autos.get(cur, other, aid)
        assert autos.list_autos(cur, other) == []
    conn.close()


def test_a_broken_automatic_message_does_not_stop_the_others(world, monkeypatch):
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    bad = make_date(world)
    good = make_date(world)
    real = autos._run_date
    calls = []

    def flaky(cur, a, now):
        calls.append(a["id"])
        if a["id"] == bad:
            raise RuntimeError("boom")
        return real(cur, a, now)
    monkeypatch.setattr(autos, "_run_date", flaky)
    world.run(at(2026, 10, 20, 10, 5))
    assert set(calls) == {bad, good} and world.sent_to() == ["919800100001"]
    assert "boom" in world.db.one("select last_error from autos where id = %s", (bad,))


def test_the_worker_runs_them_each_pass(world):
    from prism_wa import worker
    from prism_wa.graph import Graph
    from prism_wa.vault import Vault
    import os
    world.person("Asha", "919800100001", {"Birthday": "1990-10-20"})
    make_date(world)
    out = worker.cycle(world.conn, graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}), now=at(2026, 10, 20, 10, 5))
    assert out.scheduled >= 1 and len(world.sent_to()) == 1
