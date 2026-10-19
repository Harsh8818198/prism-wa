"""Plans: the ceilings and the features, enforced where the work happens."""
import os
from datetime import timedelta

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient
from test_send import FakeGraph, TOKEN

from prism_wa import campaigns, contacts, inbox, plans, segments, send
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.jobs import utcnow
from prism_wa.plans import PlanLimit
from prism_wa.vault import Vault

BODY = {"type": "BODY", "text": "Hello {{1}}, our Diwali offer is on until Friday so please do reply soon.",
        "example": {"body_text": [["Asha"]]}}


@pytest.fixture
def world(migrated):
    tid, number = H.onboard(migrated)

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
        def plan(cls, name):
            cls.do(plans.set_plan, name)

        @classmethod
        def import_(cls, text, **kw):
            conn = cls.conn()
            try:
                return contacts.import_csv(conn, tid, text, **kw)
            finally:
                conn.close()

    W.db, W.tid, W.nid = migrated, tid, number
    return W


# ── the table itself ─────────────────────────────────────────────────────────

def test_the_numbers_match_the_pricing_page():
    L = plans.LIMITS
    assert [(L[p]["tags"], L[p]["attributes"], L[p]["segments"], L[p]["mps"], L[p]["webhooks"])
            for p in ("basic", "pro", "premium", "unlimited")] == [
        (10, 5, 2, 40, 0), (100, 20, 10, 40, 0), (250, 50, 50, 250, 1), (500, 100, 50, 1000, 3)]
    assert [L[p]["agents"] for p in ("basic", "pro", "premium", "unlimited")] == [6, 6, 10, 201]   # owner included
    assert all(v is None for v in L["enterprise"].values())
    for lower, higher in zip(plans.ORDER, plans.ORDER[1:]):                                        # each plan includes the one below
        assert plans.FEATURES[lower] <= plans.FEATURES[higher]
    assert plans.lowest_with("scheduler") == "pro" and plans.lowest_with("number_masking") == "premium"
    assert plans.lowest_with("template_api") == "basic" and plans.lowest_with("nonsense") is None


def test_an_unknown_plan_is_basic_and_the_plan_is_stored_without_losing_other_settings(world):
    assert world.do(plans.plan_of) == "basic"
    for said, got in (("PRO", "pro"), (" Premium ", "premium"), ("platinum", "basic"), ("", "basic"), (None, "basic")):
        assert world.do(plans.set_plan, said) == got and world.do(plans.plan_of) == got
    world.do(inbox.save_settings, {"sla_minutes": 30}) if False else None
    from prism_wa import consent
    world.do(consent.save_settings, {"opt_out_keywords": ["stop", "band"]})
    world.plan("pro")
    assert world.do(consent.settings)["opt_out_keywords"] == ["stop", "band"] and world.do(plans.plan_of) == "pro"


def test_the_error_names_the_plan_that_has_it(world):
    with pytest.raises(PlanLimit) as e:
        world.do(plans.require, "scheduler", "Scheduling a campaign")
    assert e.value.reason == "plan_feature" and e.value.needs == "pro"
    assert "Basic" in str(e.value) and "starts on Pro" in str(e.value)
    with pytest.raises(PlanLimit) as e:
        world.do(plans.check_count, "attributes", 5, 1, "custom attributes")
    assert e.value.reason == "plan_limit" and e.value.needs == "pro" and "5 custom attributes" in str(e.value)
    world.plan("enterprise")
    world.do(plans.check_count, "attributes", 10**6, 1)
    world.do(plans.require, "sso")


# ── custom attributes and tags ───────────────────────────────────────────────

def test_the_attribute_ceiling_is_the_plans(world):
    def add(n):
        return world.do(lambda cur, t: [contacts.define_attribute(cur, t, "A%d" % i) for i in range(n)])
    add(5)
    world.do(contacts.define_attribute, "A0")                             # re-defining one you have is free
    with pytest.raises(PlanLimit) as e:
        world.do(contacts.define_attribute, "Six")
    assert "Pro" in str(e.value)
    world.plan("pro")
    world.do(contacts.define_attribute, "Six")
    assert world.db.one("select count(*) from attribute_defs") == 6


def test_an_import_past_the_attribute_ceiling_still_imports_and_says_what_it_left_out(world):
    cols = "".join(",Col%d" % i for i in range(8))
    row = "".join(",v%d" % i for i in range(8))
    r = world.import_("Name,Mobile" + cols + "\nAsha,9876543210" + row + "\n")
    assert r.created == 1
    assert world.db.one("select count(*) from attribute_defs") == 5
    assert sum("allows 5 custom attributes" in w for w in r.warnings) == 3
    assert world.db.one("select count(*) from contact_attributes") == 5


def test_an_import_past_the_tag_ceiling_keeps_the_first_tags_and_says_so(world):
    world.db.q("insert into contacts (tenant_id, name, phone, tags) values (%s,'Old','919800000001','{t0,t1}')", (world.tid,))
    text = "Name,Mobile,Tags\n" + "".join("N%d,9811%06d,t%d|t0\n" % (i, i, i) for i in range(2, 14))
    r = world.import_(text)
    tags = {t for (t,) in [(x["t"],) for x in world.db.q("select distinct unnest(tags) as t from contacts")]}
    assert len(tags) == 10 and {"t0", "t1"} <= tags
    assert any("tag limit" in w for w in r.warnings)
    assert world.db.one("select count(*) from contacts") == 13                  # every contact was still imported
    world.plan("pro")
    world.import_("Name,Mobile,Tags\nZed,9822222222,brand-new-tag\n")
    assert "brand-new-tag" in {x["t"] for x in world.db.q("select distinct unnest(tags) as t from contacts")}


# ── saved segments ───────────────────────────────────────────────────────────

def test_saved_segments_are_capped_replaced_by_name_frozen_listed_and_deleted(world):
    f = {"field": "tags", "op": "has", "value": "vip"}
    for i in range(3):
        world.db.q("insert into contacts (tenant_id, name, phone, tags) values (%s,%s,%s,'{vip}')", (world.tid, "C%d" % i, "9198000000%02d" % i))
    a = world.do(segments.save, "VIPs", f)
    b = world.do(segments.save, "Everyone", {"all": []})
    with pytest.raises(PlanLimit) as e:
        world.do(segments.save, "Third", f)
    assert "2 saved segments" in str(e.value)
    assert world.do(segments.save, "vips", {"field": "tags", "op": "has", "value": "cold"}) == a     # same name: replaced, not counted
    assert world.db.one("select count(*) from segments") == 2

    world.plan("pro")
    frozen = world.do(segments.save, "Frozen VIPs", f, dynamic=False)
    assert world.db.one("select count(*) from segment_members where segment_id = %s", (frozen,)) == 3
    assert world.do(segments.delete, b) is True and world.do(segments.delete, b) is False
    assert world.db.one("select count(*) from segments") == 2                          # the slot is free again
    with pytest.raises(segments.FilterError):
        world.do(segments.save, "Bad", {"field": "zzz", "op": "eq"})
    with pytest.raises(segments.FilterError):
        world.do(segments.save, " ", f)


def test_a_static_segment_remembers_who_matched_when_it_was_saved(world):
    world.plan("pro")
    for i in range(3):
        world.db.q("insert into contacts (tenant_id, name, phone, tags) values (%s,%s,%s,'{vip}')", (world.tid, "C%d" % i, "9198000000%02d" % i))
    frozen = world.do(segments.save, "Frozen", {"field": "tags", "op": "has", "value": "vip"}, dynamic=False)
    world.db.q("update contacts set tags = '{}'")
    assert world.db.one("select count(*) from segment_members where segment_id = %s", (frozen,)) == 3
    assert {s_["name"]: s_["size"] for s_ in world.do(segments.list_segments)} == {"Frozen": 3}


# ── the team ─────────────────────────────────────────────────────────────────

def test_the_number_of_people_in_the_inbox_is_capped_by_the_plan(world):
    for i in range(6):
        world.do(inbox.ensure_agent, "seat-%d" % i, "P%d" % i)
    assert world.do(inbox.ensure_agent, "seat-0", "P0")["role"] == "owner"            # an existing seat is always fine
    with pytest.raises(PlanLimit) as e:
        world.do(inbox.ensure_agent, "seat-7", "Seventh")
    assert "6 team members" in str(e.value) and e.value.needs == "premium"
    world.plan("premium")
    for i in range(7, 11):                                                              # 6 + 4 = 10, the Premium ceiling
        world.do(inbox.ensure_agent, "seat-%d" % i, "P%d" % i)
    with pytest.raises(PlanLimit):
        world.do(inbox.ensure_agent, "seat-x", "Eleventh")


def test_basic_has_manual_assignment_only_and_pro_unlocks_routing_rules_and_roles(world):
    owner = world.do(inbox.ensure_agent, "s0", "Owner")
    asha = world.do(inbox.ensure_agent, "s1", "Asha")
    cid = world.db.one("insert into contacts (tenant_id, name, phone) values (%s,'C','919800000001') returning id", (world.tid,))
    conv = world.db.one("insert into conversations (tenant_id, contact_id, channel, status) values (%s,%s,'whatsapp','active') returning id", (world.tid, cid))
    assert world.do(inbox.request_human, conv) is None                                  # no automatic routing on Basic
    assert world.db.one("select status from conversations") == "requesting"
    for what, call in (("routing", lambda cur, t: inbox.save_settings(cur, t, {"routing": "least_busy"})),
                       ("rules", lambda cur, t: inbox.update_agent(cur, t, asha["id"], rules=[{"field": "tags", "op": "has", "value": "x"}])),
                       ("roles", lambda cur, t: inbox.update_agent(cur, t, asha["id"], role="manager"))):
        with pytest.raises(PlanLimit) as e:
            world.do(call)
        assert e.value.needs == "pro", what
    assert world.do(lambda cur, t: inbox.save_settings(cur, t, {"routing": "manual", "sla_minutes": 20}))["sla_minutes"] == 20

    world.plan("pro")
    world.do(lambda cur, t: inbox.save_settings(cur, t, {"routing": "least_busy"}))
    world.do(lambda cur, t: inbox.update_agent(cur, t, asha["id"], rules=[{"field": "tags", "op": "has", "value": "x"}]))
    assert world.do(lambda cur, t: inbox.update_agent(cur, t, asha["id"], role="manager"))["role"] == "manager"
    with pytest.raises(PlanLimit) as e:
        world.do(lambda cur, t: inbox.update_agent(cur, t, asha["id"], masked=True))
    assert e.value.needs == "premium"


# ── campaigns ────────────────────────────────────────────────────────────────

def make_campaign(world, **kw):
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status, components)"
               " values (%s, (select id from wabas), 'diwali', 'en_US', 'MARKETING', 'APPROVED', %s::jsonb)"
               " on conflict (waba_id, name, language) do nothing",
               (world.tid, '[%s]' % __import__("json").dumps(BODY)))
    args = dict(name="C", template_name="diwali", audience={"tags": ["vip"]}, variables={"1": {"source": "field", "field": "first_name"}})
    args.update(kw)
    return world.do(campaigns.create_campaign, **args)


def test_scheduling_click_tracking_and_retries_need_pro(world):
    for kw in ({"schedule_at": utcnow() + timedelta(days=1)},
               {"variables": {"1": {"source": "tracked_link", "target": "https://example.com"}}},
               {"retry": {"max": 2}}):
        with pytest.raises(PlanLimit) as e:
            make_campaign(world, **kw)
        assert e.value.needs == "pro", kw
    cid = make_campaign(world)                                                          # a plain campaign is fine
    assert world.db.one("select retry->>'max' from campaigns where id = %s", (cid,)) == "0"      # and gets one attempt
    with pytest.raises(PlanLimit):
        world.do(campaigns.reschedule, cid, utcnow() + timedelta(days=1))

    world.plan("pro")
    pro = make_campaign(world, schedule_at=utcnow() + timedelta(days=1), retry=None)
    assert world.db.one("select retry->>'max' from campaigns where id = %s", (pro,)) == "3"
    make_campaign(world, variables={"1": {"source": "tracked_link", "target": "https://example.com"}})


# ── how fast ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plan,expected", [("basic", 40), ("pro", 40), ("premium", 80), ("unlimited", 80)])
def test_the_send_speed_is_the_lower_of_the_numbers_limit_and_the_plans(world, plan, expected):
    vault = Vault({1: os.urandom(32)})
    blob, ver = vault.encrypt(TOKEN, context=P.WABA)
    world.db.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    world.plan(plan)
    world.db.q("update phone_numbers set mps_limit = 80")
    for i in range(100):
        cid = world.db.one("insert into contacts (tenant_id, name, phone) values (%s,'C',%s) returning id", (world.tid, "9198100%05d" % i))
        conn = world.conn()
        with conn.cursor() as cur:
            send.enqueue_template(cur, world.tid, contact_id=cid, name="promo", idem_key="k%d" % i)
        conn.commit()
        conn.close()
    clock = [100.0]
    stats = send.send_due(world.conn, graph=Graph(FakeGraph()), vault=vault, batch=100, limiter=send.RateLimiter(clock=lambda: clock[0]))
    assert stats.sent == expected and stats.throttled == 100 - expected


# ── over HTTP ────────────────────────────────────────────────────────────────

def test_the_api_stores_the_plan_the_licence_server_sends_and_answers_a_limit_with_403(world):
    app = create_app(app_secrets="s", verify_token="v", connect=world.conn, internal_token="tok",
                     graph=Graph(lambda *a: (404, b"{}")), vault=Vault({1: os.urandom(32)}))
    c = TestClient(app)

    def post(path, **body):
        body.setdefault("tenant_id", str(world.tid))
        return c.post(path, json=body, headers={"X-Internal-Token": "tok"})

    r = post("/internal/campaigns/create", name="C", template_name="t", audience={"tags": ["a"]},
             schedule_at=(utcnow() + timedelta(days=1)).isoformat(), plan="basic")
    assert r.status_code == 403
    assert r.json()["error"] == "plan_feature" and r.json()["needs"] == "pro" and "Pro" in r.json()["message"]
    r = post("/internal/campaigns/create", name="C", template_name="t", audience={"tags": ["a"]},
             schedule_at=(utcnow() + timedelta(days=1)).isoformat(), plan="pro")
    assert r.status_code == 200
    assert world.do(plans.plan_of) == "pro"
    assert post("/internal/templates/list", plan="platinum").status_code == 200 and world.do(plans.plan_of) == "basic"
