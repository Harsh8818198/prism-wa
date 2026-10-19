"""Time zones, a test to your own phone, repeating campaigns and per-recipient local send time."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import helpers as H
import payloads as P
import psycopg2
import pytest

from prism_wa import campaigns, contacts, schedules, timezones
from prism_wa.campaigns import CampaignError

BODY = {"type": "BODY", "text": "Hello {{1}}, our Diwali offer for {{2}} ends on {{3}} so please do reply soon.",
        "example": {"body_text": [["Asha", "Vadodara", "Friday"]]}}
MAPPING = {"1": {"source": "field", "field": "first_name"},
           "2": {"source": "attribute", "key": "City", "default": "your city"},
           "3": {"source": "static", "value": "Friday"}}
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RULE_WEEKLY = {"freq": "weekly", "time": "10:00", "days": ["mon"], "tz": "Asia/Kolkata"}


# ── zones ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phone,zone", [("919812345678", "Asia/Kolkata"), ("971501234567", "Asia/Dubai"), ("447911123456", "Europe/London"),
                                        ("12025550123", "America/New_York"), ("6591234567", "Asia/Singapore"), ("999123", "Asia/Kolkata"),
                                        ("", "Asia/Kolkata"), (None, "Asia/Kolkata")])
def test_a_number_says_which_zone_it_lives_in(phone, zone):
    assert timezones.zone_name_for_phone(phone) == zone


def test_a_three_digit_code_beats_a_shorter_one_and_an_unknown_one_takes_the_default():
    assert timezones.zone_name_for_phone("97701234567") == "Asia/Kathmandu"        # 977, not 97
    assert timezones.zone_name_for_phone("99912345", default="Europe/Paris") == "Europe/Paris"


def test_the_next_ten_oclock_in_a_zone():
    after = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)                                   # 08:30 in Kolkata
    assert timezones.next_at_local("10:00", IST, after) == datetime(2026, 10, 17, 4, 30, tzinfo=UTC)
    late = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)                                    # 11:30 in Kolkata: tomorrow's
    assert timezones.next_at_local("10:00", IST, late) == datetime(2026, 10, 18, 4, 30, tzinfo=UTC)
    assert timezones.next_at_local("10:00", ZoneInfo("Asia/Dubai"), after) == datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


# ── rules ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", [None, {}, {"freq": "hourly", "time": "10:00"}, {"freq": "daily", "time": "25:00"}, {"freq": "daily", "time": "9:00"},
                                  {"freq": "daily", "time": "10:00", "tz": "Mars/Base"}, {"freq": "weekly", "time": "10:00"},
                                  {"freq": "weekly", "time": "10:00", "days": ["funday"]}, {"freq": "monthly", "time": "10:00"},
                                  {"freq": "monthly", "time": "10:00", "day": 31}, {"freq": "daily", "time": "10:00", "until": "tomorrow"},
                                  {"freq": "daily", "time": "10:00", "max_runs": 0}])
def test_nonsense_rules_are_refused(rule):
    with pytest.raises(schedules.ScheduleError):
        schedules.validate_rule(rule)


def test_a_good_rule_is_tidied():
    out = schedules.validate_rule({"freq": "weekly", "time": "10:00", "days": ["thu", "mon", "mon"], "extra": 1})
    assert out == {"freq": "weekly", "time": "10:00", "tz": "Asia/Kolkata", "days": ["mon", "thu"]}


def test_daily_weekly_and_monthly_rules_find_their_next_moment():
    after = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)                                   # Saturday 08:30 IST
    daily = schedules.validate_rule({"freq": "daily", "time": "10:00"})
    assert schedules.next_occurrence(daily, after) == datetime(2026, 10, 17, 4, 30, tzinfo=UTC)
    assert schedules.next_occurrence(daily, datetime(2026, 10, 17, 5, 0, tzinfo=UTC)) == datetime(2026, 10, 18, 4, 30, tzinfo=UTC)
    weekly = schedules.validate_rule({"freq": "weekly", "time": "10:00", "days": ["mon", "thu"]})
    assert schedules.next_occurrence(weekly, after) == datetime(2026, 10, 19, 4, 30, tzinfo=UTC)       # the coming Monday
    monthly = schedules.validate_rule({"freq": "monthly", "time": "09:00", "day": 1})
    assert schedules.next_occurrence(monthly, after) == datetime(2026, 11, 1, 3, 30, tzinfo=UTC)


def test_a_rule_is_strictly_after_the_moment_given_so_it_cannot_fire_twice():
    daily = schedules.validate_rule({"freq": "daily", "time": "10:00"})
    fired = datetime(2026, 10, 17, 4, 30, tzinfo=UTC)
    assert schedules.next_occurrence(daily, fired) == datetime(2026, 10, 18, 4, 30, tzinfo=UTC)


def test_a_rule_stops_at_its_end_date():
    rule = schedules.validate_rule({"freq": "daily", "time": "10:00", "until": "2026-10-18"})
    assert schedules.next_occurrence(rule, datetime(2026, 10, 17, 5, 0, tzinfo=UTC)) == datetime(2026, 10, 18, 4, 30, tzinfo=UTC)
    assert schedules.next_occurrence(rule, datetime(2026, 10, 18, 5, 0, tzinfo=UTC)) is None


def test_the_clock_change_does_not_move_a_morning_ten_oclock():
    rule = schedules.validate_rule({"freq": "daily", "time": "10:00", "tz": "Europe/London"})
    before = schedules.next_occurrence(rule, datetime(2026, 10, 23, 12, 0, tzinfo=UTC))       # 24 Oct: BST (UTC+1)
    after = schedules.next_occurrence(rule, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))        # GMT (UTC+0)
    assert (before.hour, after.hour) == (9, 10)


# ── against a real database ──────────────────────────────────────────────────

@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tenant_id,))
    waba = migrated.one("select id from wabas")
    migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
               " values (%s, %s, 'diwali', 'en_US', 'MARKETING', 'APPROVED', %s)", (tenant_id, waba, json.dumps([BODY])))

    class W:
        db, tid, nid = migrated, tenant_id, number_id

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
        def people(cls, phones=("919800100001", "919800100002"), tags=("vip",)):
            conn = cls.conn()
            with conn.cursor() as cur:
                contacts.define_attribute(cur, tenant_id, "City")
                for i, phone in enumerate(phones):
                    cur.execute("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s,%s,%s,%s,true) returning id",
                                (tenant_id, "Asha Shah %d" % i, phone, list(tags)))
                    contacts.set_attribute(cur, tenant_id, cur.fetchone()[0], "City", "Vadodara")
            conn.commit()
            conn.close()

        @classmethod
        def recipe(cls, **kw):
            return {"template_name": "diwali", "audience": {"tags": ["vip"]}, "variables": MAPPING, **kw}

        @classmethod
        def outbox(cls):
            return cls.db.q("select o.to_phone, o.run_at, o.kind, o.payload, o.campaign_id, o.state from outbox o order by o.created_at, o.to_phone")

        @classmethod
        def fire(cls, now):
            conn = cls.conn()
            try:
                with conn.cursor() as cur:
                    n = schedules.run_due(cur, now=now)
                conn.commit()
                return n
            finally:
                conn.close()

    return W


# ── a test to your own phone ─────────────────────────────────────────────────

def test_a_test_goes_to_one_number_with_the_values_the_campaign_would_use(world):
    out = world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="+91 98765 43210", sample_name="Owner")
    row = world.outbox()[0]
    assert row["to_phone"] == "919876543210" and row["kind"] == "template" and row["campaign_id"] is None
    params = [p["text"] for p in row["payload"]["template"]["components"][0]["parameters"]]
    assert params == ["Owner", "your city", "Friday"] and out["to"] == "919876543210"
    assert world.db.one("select tags from contacts where phone = '919876543210'") == ["test"]
    assert world.db.one("select sender from messages") == "test"


def test_a_test_to_an_opted_out_number_or_with_a_missing_value_is_refused_with_the_reason(world):
    world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="9876543210")
    world.db.q("update contacts set opt_out = true")
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="9876543210")
    assert e.value.reason == "opted_out"
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables={"1": MAPPING["1"]}, to="9876543211")
    assert e.value.reason == "variable_unmapped"
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables={**MAPPING, "3": {"source": "static", "value": ""}}, to="9876543211")
    assert e.value.reason == "variable_invalid"
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="not a number")
    assert e.value.reason == "bad_number"
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="nothing", variables=MAPPING, to="9876543212")
    assert e.value.reason == "template_unknown"


def test_a_test_of_a_template_that_is_not_approved_is_refused(world):
    world.db.q("update templates set status = 'PENDING'")
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="9876543210")
    assert e.value.reason == "template_not_approved"


def test_test_messages_are_limited_per_hour(world, monkeypatch):
    monkeypatch.setattr(campaigns, "TESTS_PER_HOUR", 2)
    for n in range(2):
        world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="98765432%02d" % n)
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.send_test, template_name="diwali", variables=MAPPING, to="9876543299")
    assert e.value.reason == "test_limit"


# ── each person at their own ten o'clock ─────────────────────────────────────

def test_a_local_time_campaign_holds_each_message_until_its_recipients_ten_oclock(world):
    world.people(phones=("919800100001", "971501234567"))
    cid = world.do(campaigns.create_campaign, name="Morning offer", **world.recipe(), local_time="10:00")
    now = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
    world.do(campaigns.launch, cid, now=now)
    runs = {r["to_phone"]: r["run_at"].astimezone(UTC) for r in world.outbox()}
    assert runs["919800100001"] == datetime(2026, 10, 17, 4, 30, tzinfo=UTC)         # 10:00 in Kolkata
    assert runs["971501234567"] == datetime(2026, 10, 17, 6, 0, tzinfo=UTC)          # 10:00 in Dubai


def test_a_campaign_without_a_local_time_goes_straight_away(world):
    world.people()
    cid = world.do(campaigns.create_campaign, name="Now", **world.recipe())
    now = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)
    world.do(campaigns.launch, cid, now=now)
    assert {r["run_at"].astimezone(UTC) for r in world.outbox()} == {now}


def test_a_bad_local_time_or_a_plan_without_scheduling_is_refused(world):
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.create_campaign, name="x", **world.recipe(), local_time="10am")
    assert e.value.reason == "bad_local_time"
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"basic\"}'")
    with pytest.raises(Exception) as e:
        world.do(campaigns.create_campaign, name="x", **world.recipe(), local_time="10:00")
    assert "plan" in str(e.value).lower()


# ── repeating campaigns ──────────────────────────────────────────────────────

def test_a_repeating_campaign_fires_on_its_day_and_makes_an_ordinary_campaign(world):
    world.people()
    sid = world.do(schedules.create, "Monday offer", world.recipe(), RULE_WEEKLY, now=datetime(2026, 10, 17, 3, 0, tzinfo=UTC))
    assert world.db.one("select next_run_at from campaign_schedules").astimezone(UTC) == datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
    assert world.fire(datetime(2026, 10, 18, 12, 0, tzinfo=UTC)) == 0                # Sunday: nothing yet
    assert world.fire(datetime(2026, 10, 19, 4, 31, tzinfo=UTC)) == 1                # Monday 10:01
    camp = world.db.q("select name, status, schedule_id, total from campaigns")[0]
    assert camp["name"] == "Monday offer · 19 Oct" and camp["status"] == "sending" and camp["schedule_id"] == sid and camp["total"] == 2
    assert len(world.outbox()) == 2
    s = world.db.q("select runs, failures, last_error, enabled, next_run_at from campaign_schedules")[0]
    assert (s["runs"], s["failures"], s["last_error"], s["enabled"]) == (1, 0, None, True)
    assert s["next_run_at"].astimezone(UTC) == datetime(2026, 10, 26, 4, 30, tzinfo=UTC)


def test_a_repeat_fires_once_even_if_the_worker_was_down_for_days(world):
    world.people()
    world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    assert world.fire(datetime(2026, 10, 17, 9, 0, tzinfo=UTC)) == 1
    assert world.db.one("select count(*) from campaigns") == 1
    assert world.db.one("select next_run_at from campaign_schedules").astimezone(UTC) == datetime(2026, 10, 18, 4, 30, tzinfo=UTC)


def test_a_run_the_preflight_refuses_is_recorded_and_three_in_a_row_switch_the_schedule_off(world):
    world.people()
    world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    world.db.q("update templates set status = 'PAUSED'")
    for day in (11, 12):
        assert world.fire(datetime(2026, 10, day, 6, 0, tzinfo=UTC)) == 0
        s = world.db.q("select failures, last_error, enabled from campaign_schedules")[0]
        assert s["enabled"] and "paused" in s["last_error"].lower()
    world.fire(datetime(2026, 10, 13, 6, 0, tzinfo=UTC))
    s = world.db.q("select failures, enabled, next_run_at from campaign_schedules")[0]
    assert s["failures"] == 3 and s["enabled"] is False and s["next_run_at"] is None
    assert world.db.one("select count(*) from outbox") == 0


def test_a_good_run_after_a_failure_clears_the_count(world):
    world.people()
    world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    world.db.q("update templates set status = 'PAUSED'")
    world.fire(datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
    world.db.q("update templates set status = 'APPROVED'")
    world.fire(datetime(2026, 10, 12, 6, 0, tzinfo=UTC))
    s = world.db.q("select failures, last_error, runs from campaign_schedules")[0]
    assert (s["failures"], s["last_error"], s["runs"]) == (0, None, 1)


def test_a_schedule_ends_after_its_maximum_runs_or_its_end_date(world):
    world.people()
    world.do(schedules.create, "Twice", world.recipe(), {"freq": "daily", "time": "10:00", "max_runs": 2}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    world.fire(datetime(2026, 10, 10, 6, 0, tzinfo=UTC))
    world.fire(datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
    s = world.db.q("select runs, enabled, next_run_at from campaign_schedules")[0]
    assert (s["runs"], s["enabled"], s["next_run_at"]) == (2, False, None)
    assert world.fire(datetime(2026, 10, 12, 6, 0, tzinfo=UTC)) == 0


def test_a_paused_schedule_does_not_fire_and_resuming_it_looks_ahead(world):
    world.people()
    sid = world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    world.do(schedules.set_enabled, sid, False)
    assert world.fire(datetime(2026, 10, 12, 6, 0, tzinfo=UTC)) == 0
    back = world.do(schedules.set_enabled, sid, True, now=datetime(2026, 10, 12, 6, 0, tzinfo=UTC))
    assert back["enabled"] and back["next_run_at"].astimezone(UTC) == datetime(2026, 10, 13, 4, 30, tzinfo=UTC)


def test_a_plan_that_no_longer_includes_scheduling_stops_the_repeat(world):
    world.people()
    world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"basic\"}'")
    assert world.fire(datetime(2026, 10, 11, 6, 0, tzinfo=UTC)) == 0
    assert "plan" in world.db.one("select last_error from campaign_schedules").lower() and world.db.one("select count(*) from campaigns") == 0


def test_a_recipe_is_checked_when_it_is_saved(world):
    for bad in ({}, {"template_name": "diwali"}, {**{"template_name": "diwali", "audience": {"tags": []}}, "schedule_at": "2026-12-01T10:00:00"},
                {"template_name": "diwali", "audience": {"tags": []}, "variables": {"1": {"source": "nope"}}}):
        with pytest.raises(schedules.ScheduleError):
            world.do(schedules.create, "Bad", bad, RULE_WEEKLY)
    with pytest.raises(schedules.ScheduleError):
        world.do(schedules.create, "", world.recipe(), RULE_WEEKLY)


def test_a_workspace_can_have_only_so_many_repeats_and_basic_gets_none(world, monkeypatch):
    monkeypatch.setattr(schedules, "MAX_SCHEDULES", 2)
    for n in range(2):
        world.do(schedules.create, "R%d" % n, world.recipe(), RULE_WEEKLY)
    with pytest.raises(schedules.ScheduleError) as e:
        world.do(schedules.create, "R3", world.recipe(), RULE_WEEKLY)
    assert e.value.reason == "too_many"
    world.db.q("update tenant_settings set settings = settings || '{\"plan\": \"basic\"}'")
    with pytest.raises(Exception) as e:
        world.do(schedules.create, "R4", world.recipe(), RULE_WEEKLY)
    assert "plan" in str(e.value).lower()


def test_another_workspace_cannot_see_or_change_a_schedule(world, migrated):
    sid = world.do(schedules.create, "Mine", world.recipe(), RULE_WEEKLY)
    other = migrated.one("insert into tenants (slug, name) values ('other', 'Other') returning id")
    conn = world.conn()
    with conn.cursor() as cur:
        assert schedules.list_schedules(cur, other) == []
        with pytest.raises(schedules.ScheduleError):
            schedules.set_enabled(cur, other, sid, False)
        assert schedules.delete(cur, other, sid) is False
    conn.close()
    assert world.do(schedules.delete, sid) is True


def test_the_worker_cycle_runs_due_schedules(world):
    from prism_wa import worker
    from prism_wa.graph import Graph
    from prism_wa.vault import Vault
    import os
    world.people()
    world.do(schedules.create, "Daily", world.recipe(), {"freq": "daily", "time": "10:00"}, now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC))
    done = worker.cycle(world.conn, graph=Graph(lambda *a: (200, b"{}")), vault=Vault({1: os.urandom(32)}), reap=False,
                        now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC))
    assert done.scheduled == 1 and done.busy
