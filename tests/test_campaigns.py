"""The campaign engine: preflight, launch, schedule, pause, cancel, retries, tracking, reports."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import helpers as H
import payloads as P
import psycopg2
import pytest
from test_send import FakeGraph, TOKEN

from prism_wa import campaigns, contacts, send, segments
from prism_wa.campaigns import CampaignError
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

BODY = {"type": "BODY", "text": "Hello {{1}}, our Diwali offer for {{2}} ends on {{3}} so please do reply soon.",
        "example": {"body_text": [["Asha", "Vadodara", "Friday"]]}}
MAPPING = {"1": {"source": "field", "field": "first_name"},
           "2": {"source": "attribute", "key": "City", "default": "your city"},
           "3": {"source": "static", "value": "Friday"}}


@pytest.fixture
def vault():
    return Vault({1: os.urandom(32)})


@pytest.fixture
def world(migrated, vault):
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s, tier = 'TIER_10K'", (psycopg2.Binary(blob), ver))
    waba = migrated.one("select id from wabas")
    migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
               " values (%s, %s, 'diwali', 'en_US', 'MARKETING', 'APPROVED', %s)", (tid, waba, json.dumps([BODY])))

    class W:
        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def people(cls, n=3, tags=("vip",), city="Vadodara"):
            ids = []
            conn = cls.conn()
            with conn.cursor() as cur:
                contacts.define_attribute(cur, tid, "City")
                for i in range(n):
                    cur.execute("insert into contacts (tenant_id, name, phone, tags, opted_in) values (%s,%s,%s,%s,true)"
                                " returning id", (tid, "Asha Shah %d" % i, "91980010%04d" % i, list(tags)))
                    cid = cur.fetchone()[0]
                    if city:
                        contacts.set_attribute(cur, tid, cid, "City", city)
                    ids.append(cid)
            conn.commit()
            conn.close()
            return ids

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
        def make(cls, **kw):
            args = dict(name="Diwali", template_name="diwali", audience={"tags": ["vip"]}, variables=MAPPING)
            args.update(kw)
            return cls.do(campaigns.create_campaign, **args)

        @classmethod
        def deliver(cls, **kw):
            return send.send_due(cls.conn, graph=Graph(cls.graph), vault=vault, **kw)

        @classmethod
        def status(cls, cid):
            return cls.db.one("select status from campaigns where id = %s", (cid,))

    W.db, W.tid, W.nid, W.graph, W.vault_ = migrated, tid, number, FakeGraph(), vault
    return W


def codes(report):
    return {b["code"] for b in report.blocking}


# ── preflight ────────────────────────────────────────────────────────────────

def test_a_good_campaign_passes_preflight_with_a_cost_estimate(world):
    world.people(4)
    r = world.do(campaigns.preflight, world.make())
    assert r.ok and r.recipients == 4 and r.blocking == []
    assert r.cost["category"] == "MARKETING" and r.cost["is_estimate"] is True
    assert r.cost["total"] == round(4 * 0.8631 * 1.18, 2)
    assert world.db.one("select preflight->>'ok' from campaigns") == "true"


def test_preflight_blocks_for_every_documented_reason(world):
    world.people(2)
    ok = world.make()
    assert "template_unknown" in codes(world.do(campaigns.preflight, world.make(template_name="nope")))
    world.db.q("update templates set status = 'PAUSED'")
    assert "template_not_approved" in codes(world.do(campaigns.preflight, ok))
    world.db.q("update templates set status = 'APPROVED', quality = 'RED'")
    assert "template_quality_red" in codes(world.do(campaigns.preflight, ok))
    world.db.q("update templates set quality = 'YELLOW'")
    r = world.do(campaigns.preflight, ok)
    assert r.ok and {w["code"] for w in r.warnings} >= {"template_quality_yellow"}
    world.db.q("update templates set quality = null")

    assert "variable_unmapped" in codes(world.do(campaigns.preflight, world.make(variables={"1": MAPPING["1"]})))
    assert "variable_invalid" in codes(world.do(campaigns.preflight, world.make(variables={**MAPPING, "2": {"source": "field", "field": "salary"}})))
    assert "variable_invalid" in codes(world.do(campaigns.preflight, world.make(variables={**MAPPING, "3": {"source": "static", "value": " "}})))
    assert "no_recipients" in codes(world.do(campaigns.preflight, world.make(audience={"tags": ["nobody"]})))
    assert "bad_audience" in codes(world.do(campaigns.preflight, world.make(audience={"filter": {"field": "zzz", "op": "eq"}})))

    world.db.q("update phone_numbers set quality = 'RED'")
    assert "number_quality_red" in codes(world.do(campaigns.preflight, ok))
    world.db.q("update phone_numbers set quality = null")
    world.db.q("delete from phone_numbers")
    assert "no_number" in codes(world.do(campaigns.preflight, ok))


def test_preflight_enforces_metas_daily_limit_counting_what_was_already_sent(world):
    world.people(3)
    world.db.q("update wabas set tier = 'TIER_250'")
    c = world.make(audience={"contact_ids": [str(i) for i in world.people(0)] or []})  # placeholder, replaced below
    # 250 contacts already messaged today on this account
    conn = world.conn()
    with conn.cursor() as cur:
        for i in range(250):
            cur.execute("insert into outbox (tenant_id, number_id, to_phone, kind, payload, idem_key, state, updated_at)"
                        " values (%s,%s,%s,'template','{}',%s,'sent', now())", (world.tid, world.nid, "9199%08d" % i, "old%d" % i))
    conn.commit()
    conn.close()
    r = world.do(campaigns.preflight, world.make())
    assert "tier_limit" in codes(r) and "250" in [b for b in r.blocking if b["code"] == "tier_limit"][0]["message"]
    world.db.q("update outbox set updated_at = now() - interval '25 hours'")           # yesterday's don't count
    assert world.do(campaigns.preflight, world.make()).ok


def test_an_unknown_tier_assumes_the_lowest_and_says_so(world):
    world.people(2)
    world.db.q("update wabas set tier = null")
    r = world.do(campaigns.preflight, world.make())
    assert r.ok and "tier_unknown" in {w["code"] for w in r.warnings}


def test_people_who_cannot_be_messaged_are_left_out_and_counted(world):
    ids = world.people(5)
    world.db.q("update contacts set opt_out = true where id = %s", (ids[0],))
    world.db.q("update contacts set blocked = true where id = %s", (ids[1],))
    world.db.q("update contacts set phone = null where id = %s", (ids[2],))
    world.db.q("update contacts set email = 'x@y.in' where id = %s", (ids[2],))
    r = world.do(campaigns.preflight, world.make())
    assert (r.recipients, r.opted_out_removed) == (2, 3)
    assert "opted_out_removed" in {w["code"] for w in r.warnings}


@pytest.mark.parametrize("audience", [
    {"tags": ["vip"]}, {"filter": {"field": "tags", "op": "has", "value": "vip"}},
    {"filter": {"field": "attr:City", "op": "eq", "value": "vadodara"}}])
def test_an_audience_can_be_tags_a_filter_or_an_attribute(world, audience):
    world.people(3)
    assert world.do(campaigns.preflight, world.make(audience=audience)).recipients == 3


def test_an_audience_can_be_a_saved_segment_or_a_list_of_contacts(world):
    ids = world.people(4)
    seg = world.db.one("insert into segments (tenant_id, name, filter) values (%s, 'VIP', %s) returning id",
                       (world.tid, json.dumps({"tags": ["vip"]})))
    assert world.do(campaigns.preflight, world.make(audience={"segment_id": str(seg)})).recipients == 4
    assert world.do(campaigns.preflight, world.make(audience={"contact_ids": [str(ids[0]), str(ids[1])]})).recipients == 2


def test_create_validates_what_it_is_given(world):
    for kw, why in (({"name": " "}, "name_required"), ({"audience": {}}, "audience_required"),
                    ({"retry": {"max": 9}}, "bad_retry"), ({"retry": {"delay_minutes": 0}}, "bad_retry"),
                    ({"schedule_at": datetime.now(timezone.utc) + timedelta(days=61)}, "too_far"),
                    ({"schedule_at": "2026-10-20T10:00", "timezone_name": "Mars/Phobos"}, "bad_timezone")):
        with pytest.raises(CampaignError) as e:
            world.make(**kw)
        assert e.value.reason == why, kw


# ── launch ───────────────────────────────────────────────────────────────────

def test_launching_snapshots_the_audience_with_each_persons_own_values_and_queues_the_sends(world):
    ids = world.people(3)
    world.db.q("update contact_attributes set value = '\"Surat\"' where contact_id = %s", (ids[1],))
    world.db.q("delete from contact_attributes where contact_id = %s", (ids[2],))
    cid = world.make()
    out = world.do(campaigns.launch, cid)
    assert out["status"] == "sending" and out["queued"] == 3 and out["skipped"] == 0
    assert world.status(cid) == "sending" and world.db.one("select total from campaigns") == 3
    assert world.db.one("select count(*) from campaign_recipients where status = 'queued'") == 3

    bodies = {r["name"]: r["p"] for r in world.db.q(
        "select c.name, o.payload #> '{template,components,0,parameters}' as p from outbox o"
        " join messages m on m.id = o.message_id join conversations v on v.id = m.conversation_id"
        " join contacts c on c.id = v.contact_id")}
    texts = {n: [x["text"] for x in p] for n, p in bodies.items()}
    assert texts["Asha Shah 0"] == ["Asha", "Vadodara", "Friday"]
    assert texts["Asha Shah 1"] == ["Asha", "Surat", "Friday"]
    assert texts["Asha Shah 2"] == ["Asha", "your city", "Friday"]            # no value: the default


def test_the_sends_go_out_and_the_campaign_closes_itself(world):
    world.people(3)
    cid = world.make()
    world.do(campaigns.launch, cid)
    stats = world.deliver()
    assert stats.sent == 3 and len(world.graph.calls) == 3
    row = H.only(world.db, "select status, sent, failed, total, finished_at from campaigns")
    assert (row["status"], row["sent"], row["failed"], row["total"]) == ("done", 3, 0, 3) and row["finished_at"]
    body = world.graph.calls[0]["body"]
    assert body["type"] == "template" and body["template"]["name"] == "diwali"
    assert body["template"]["components"][0]["parameters"][0] == {"type": "text", "text": "Asha"}


def test_launching_twice_does_not_send_twice(world):
    world.people(3)
    cid = world.make()
    world.do(campaigns.launch, cid)
    world.do(campaigns.start, cid)
    assert world.db.one("select count(*) from outbox") == 3 and world.db.one("select count(*) from campaign_recipients") == 3


def test_a_campaign_with_blockers_cannot_launch_and_says_why(world):
    cid = world.make(audience={"tags": ["nobody"]})
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.launch, cid)
    assert e.value.reason == "preflight_failed" and "Nobody" in str(e.value) and e.value.report.blocking
    assert world.status(cid) == "draft" and world.db.one("select count(*) from outbox") == 0


def test_if_the_template_goes_bad_between_preflight_and_start_everyone_is_skipped_not_sent(world):
    world.people(3)
    cid = world.make()
    assert world.do(campaigns.preflight, cid).ok
    world.db.q("update templates set status = 'PAUSED'")
    out = world.do(campaigns.start, cid)
    assert out["queued"] == 0 and out["skipped"] == 3 and out["skipped_reasons"] == {"template_not_approved": 3}
    assert world.status(cid) == "done" and world.db.one("select count(*) from outbox") == 0


def test_only_a_draft_scheduled_or_failed_campaign_can_launch(world):
    world.people(2)
    cid = world.make()
    world.do(campaigns.launch, cid)
    with pytest.raises(CampaignError) as e:
        world.do(campaigns.launch, cid)
    assert e.value.reason == "wrong_state"


# ── scheduling ───────────────────────────────────────────────────────────────

def test_a_local_time_is_converted_to_utc_and_a_job_waits_for_it(world):
    world.people(2)
    when = utcnow() + timedelta(days=3)
    local = when.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).replace(tzinfo=None, microsecond=0)
    cid = world.make(schedule_at=local, timezone_name="Asia/Kolkata")
    out = world.do(campaigns.launch, cid)
    assert out["status"] == "scheduled" and world.status(cid) == "scheduled"
    stored = world.db.one("select schedule_at from campaigns")
    assert abs((stored - when.replace(microsecond=0)).total_seconds()) < 2
    job = H.only(world.db, "select kind, run_at, state from wa_jobs where kind = 'launch_campaign'")
    assert job["state"] == "queued" and abs((job["run_at"] - stored).total_seconds()) < 1
    assert world.db.one("select count(*) from outbox") == 0


def test_the_scheduled_job_sends_only_once_its_time_has_come(world):
    world.people(2)
    cid = world.make(schedule_at=utcnow() + timedelta(hours=2))
    world.do(campaigns.launch, cid)
    handlers = {"launch_campaign": campaigns.handle_launch_job}
    H.work(world.db, handlers, now=utcnow() + timedelta(hours=1))                 # not due: the job is not even claimed
    assert world.status(cid) == "scheduled" and world.db.one("select count(*) from outbox") == 0
    world.db.q("update campaigns set schedule_at = now() - interval '1 minute'")  # time passes
    H.work(world.db, handlers, now=utcnow() + timedelta(hours=3))
    assert world.status(cid) == "sending" and world.db.one("select count(*) from outbox") == 2


def test_rescheduling_moves_the_launch_and_the_old_job_does_nothing(world):
    world.people(2)
    cid = world.make(schedule_at=utcnow() + timedelta(hours=2))
    world.do(campaigns.launch, cid)
    world.do(campaigns.reschedule, cid, utcnow() + timedelta(days=2))
    assert world.db.one("select count(*) from wa_jobs where kind = 'launch_campaign'") == 2
    handlers = {"launch_campaign": campaigns.handle_launch_job}
    H.work(world.db, handlers, now=utcnow() + timedelta(hours=3))                 # the old job comes due
    assert world.status(cid) == "scheduled" and world.db.one("select count(*) from outbox") == 0


def test_a_cancelled_scheduled_campaign_never_launches(world):
    world.people(2)
    cid = world.make(schedule_at=utcnow() + timedelta(hours=1))
    world.do(campaigns.launch, cid)
    world.do(campaigns.cancel, cid)
    world.db.q("update wa_jobs set run_at = now() - interval '1 hour'")
    H.work(world.db, {"launch_campaign": campaigns.handle_launch_job})
    assert world.status(cid) == "cancelled" and world.db.one("select count(*) from outbox") == 0


# ── pause, resume, cancel ────────────────────────────────────────────────────

def test_pause_stops_the_remaining_sends_within_the_next_batch_and_resume_finishes_them(world):
    world.people(10)
    cid = world.make()
    world.do(campaigns.launch, cid)
    assert world.deliver(batch=4).sent == 4
    world.do(campaigns.pause, cid)
    assert world.deliver().claimed == 0 and len(world.graph.calls) == 4
    world.do(campaigns.resume, cid)
    assert world.deliver().sent == 6
    assert world.status(cid) == "done" and len(world.graph.calls) == 10


def test_cancel_drops_what_is_unsent_and_keeps_what_went(world):
    world.people(6)
    cid = world.make()
    world.do(campaigns.launch, cid)
    world.deliver(batch=2)
    assert world.do(campaigns.cancel, cid) == 4
    assert world.deliver().claimed == 0 and len(world.graph.calls) == 2
    assert world.status(cid) == "cancelled"
    counts = {r["state"]: r["n"] for r in world.db.q("select state, count(*) n from outbox group by state")}
    assert counts == {"sent": 2, "skipped": 4}
    assert world.db.one("select count(*) from messages where status = 'cancelled'") == 4
    assert world.db.one("select count(*) from campaign_recipients where skip_reason = 'cancelled'") == 4
    with pytest.raises(CampaignError):
        world.do(campaigns.cancel, cid)


def test_a_cancelled_message_is_not_revived_by_a_late_status(world):
    world.people(1)
    cid = world.make()
    world.do(campaigns.launch, cid)
    world.do(campaigns.cancel, cid)
    world.db.q("update messages set wamid = 'wamid.X'")
    H.deliver(world.db, P.statuses(P.status("wamid.X", "delivered", "9")))
    H.work(world.db)
    assert world.db.one("select status from messages") == "cancelled"


def test_control_is_refused_in_the_wrong_state(world):
    world.people(1)
    cid = world.make()
    for fn in (campaigns.pause, campaigns.resume):
        with pytest.raises(CampaignError):
            world.do(fn, cid)


# ── retries ──────────────────────────────────────────────────────────────────

def test_a_campaign_uses_its_own_retry_policy(world):
    world.people(1)
    cid = world.make(retry={"max": 1, "delay_minutes": 10})
    world.do(campaigns.launch, cid)
    world.graph.fail["919800100000"] = GraphError("connection reset")
    now = utcnow()
    assert world.deliver(now=now).retried == 1
    row = H.only(world.db, "select state, attempts, run_at from outbox")
    assert (row["state"], row["attempts"]) == ("queued", 1) and row["run_at"] == now + timedelta(minutes=10)
    assert world.deliver(now=now + timedelta(minutes=11)).failed == 1               # 1 retry allowed, then it fails
    assert H.only(world.db, "select state, attempts from outbox") == {"state": "failed", "attempts": 2}
    assert H.only(world.db, "select sent, failed, status from campaigns") == {"sent": 0, "failed": 1, "status": "done"}


def test_no_retries_means_one_attempt(world):
    world.people(1)
    cid = world.make(retry={"max": 0})
    world.do(campaigns.launch, cid)
    world.graph.fail["919800100000"] = GraphError("connection reset")
    assert world.deliver().failed == 1


def test_metas_own_wait_for_special_codes_beats_the_campaigns_delay(world):
    world.people(1)
    cid = world.make(retry={"max": 2, "delay_minutes": 600})
    world.do(campaigns.launch, cid)
    world.graph.fail["919800100000"] = (131056, 400)                                  # pair rate limit: one minute
    now = utcnow()
    world.deliver(now=now)
    assert world.db.one("select run_at from outbox") == now + timedelta(seconds=60)


# ── tracking and reports ─────────────────────────────────────────────────────

def test_the_report_counts_sent_delivered_read_replied_clicked_and_failed(world):
    ids = world.people(5)
    cid = world.make()
    world.do(campaigns.launch, cid)
    world.graph.fail["919800100004"] = (131026, 400)
    world.deliver()
    wamids = {r["phone"]: r["wamid"] for r in world.db.q(
        "select c.phone, m.wamid from messages m join conversations v on v.id = m.conversation_id join contacts c on c.id = v.contact_id")}
    for phone, state, ts in (("919800100000", "delivered", "10"), ("919800100001", "delivered", "10"),
                             ("919800100001", "read", "11"), ("919800100002", "delivered", "10")):
        H.deliver(world.db, P.statuses(P.status(wamids[phone], state, ts, pricing=P.PRICING)))
    H.work(world.db)
    # 919800100001 writes back after the campaign went out
    msg = P.text("interested!", "wamid.REPLY", str(int(time.time()) + 60), frm="919800100001")
    H.deliver(world.db, P.inbound(msg, contact={"profile": {"name": "Asha"}, "wa_id": "919800100001"}))
    H.work(world.db)
    link = world.db.one("select id from campaign_recipients limit 1")
    world.db.q("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind) values (%s,%s,%s,%s,'link')",
               (world.tid, cid, link, ids[0]))

    rep = world.do(campaigns.report, cid)
    c = rep["counts"]
    assert (c["recipients"], c["sent"], c["delivered"], c["read"], c["failed"], c["replied"], c["clicked"]) == (5, 4, 3, 1, 1, 1, 1)
    assert rep["rates"] == {"delivered": 75.0, "read": 25.0, "replied": 25.0, "clicked": 25.0}
    assert rep["billable"] == {"marketing": 3}
    assert rep["spend_estimate"] == {"inr": round(3 * 0.8631 * 1.18, 2), "is_estimate": True}
    assert rep["campaign"]["status"] == "done"


def test_a_tracked_link_gets_a_short_token_and_the_redirect_records_the_click(world):
    world.people(2)
    cid = world.make(variables={**MAPPING, "3": {"source": "tracked_link", "target": "https://example.com/offer"}})
    world.do(campaigns.launch, cid)
    links = world.db.q("select token, target, contact_id, recipient_id from short_links")
    assert len(links) == 2 and len({l["token"] for l in links}) == 2 and {l["target"] for l in links} == {"https://example.com/offer"}
    sent_values = {x["text"] for r in world.db.q("select payload #> '{template,components,0,parameters}' as p from outbox") for x in r["p"]}
    assert {l["token"] for l in links} <= sent_values                                # the person receives the token
    target = world.do(lambda cur, t, tok: campaigns.record_click(cur, tok), links[0]["token"])
    assert target == "https://example.com/offer"
    click = H.only(world.db, "select kind, url, contact_id, campaign_id from click_events")
    assert click == {"kind": "link", "url": "https://example.com/offer", "contact_id": links[0]["contact_id"], "campaign_id": cid}
    assert world.do(lambda cur, t, tok: campaigns.record_click(cur, tok), "no-such-token") is None


def test_a_campaigns_quick_reply_buttons_carry_tracking_and_a_press_counts_as_a_click(world):
    components = [BODY, {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Yes"}, {"type": "QUICK_REPLY", "text": "No"}]}]
    world.db.q("update templates set components = %s", (json.dumps(components),))
    world.people(1)
    cid = world.make()
    world.do(campaigns.launch, cid)
    comps = world.db.one("select payload #> '{template,components}' from outbox")
    buttons = [c for c in comps if c["type"] == "button"]
    assert [b["index"] for b in buttons] == ["0", "1"]
    payload = buttons[0]["parameters"][0]["payload"]
    recipient = world.db.one("select id from campaign_recipients")
    assert payload == "cmp:%s:%s:0" % (cid, recipient)

    press = P.template_button(payload, "Yes", "wamid.BTN", str(int(time.time())))
    press["from"] = "919800100000"
    H.deliver(world.db, P.inbound(press, contact={"profile": {"name": "Asha"}, "wa_id": "919800100000"}))
    H.work(world.db)
    click = H.only(world.db, "select kind, card, campaign_id from click_events")
    assert click == {"kind": "button", "card": "button:0", "campaign_id": cid}
    assert world.do(campaigns.report, cid)["counts"]["clicked"] == 1


def test_a_forged_button_payload_for_another_workspace_counts_for_nobody(world):
    world.people(1)
    cid = world.make()
    world.do(campaigns.launch, cid)
    recipient = world.db.one("select id from campaign_recipients")
    other = H.tenant(world.db, "shakti")
    conn = world.conn()
    with conn.cursor() as cur:
        assert campaigns.record_button(cur, other, "cmp:%s:%s:0" % (cid, recipient)) is False
        assert campaigns.record_button(cur, world.tid, "cmp:not-a-uuid:x:0") is False
    conn.close()
    assert world.db.one("select count(*) from click_events") == 0


def test_retargeting_finds_who_read_replied_or_clicked_and_who_did_not(world):
    ids = world.people(4)
    cid = world.make()
    world.do(campaigns.launch, cid)
    world.deliver()
    wam = {r["phone"]: r["wamid"] for r in world.db.q(
        "select c.phone, m.wamid from messages m join conversations v on v.id = m.conversation_id join contacts c on c.id = v.contact_id")}
    for phone, state in (("919800100000", "read"), ("919800100001", "delivered")):
        H.deliver(world.db, P.statuses(P.status(wam[phone], state, "10")))
    H.work(world.db)
    link = world.db.one("select id from campaign_recipients where contact_id = %s", (ids[2],))
    world.db.q("insert into click_events (tenant_id, campaign_id, recipient_id, contact_id, kind) values (%s,%s,%s,%s,'link')",
               (world.tid, cid, link, ids[2]))

    def who(f):
        conn = world.conn()
        with conn.cursor() as cur:
            got = set(segments.resolve(cur, world.tid, f))
        conn.close()
        return got

    assert who(campaigns.retarget_filter(cid, "sent")) == set(ids)
    assert who(campaigns.retarget_filter(cid, "read")) == {ids[0]}
    assert who(campaigns.retarget_filter(cid, "delivered")) == {ids[0], ids[1]}
    assert who(campaigns.retarget_filter(cid, "clicked")) == {ids[2]}
    assert who(campaigns.retarget_filter(cid, "read", negate=True)) == {ids[1], ids[2], ids[3]}
    # and a new campaign can be aimed at them
    nxt = world.make(name="Follow-up", audience={"filter": campaigns.retarget_filter(cid, "read", negate=True)})
    assert world.do(campaigns.preflight, nxt).recipients == 3
