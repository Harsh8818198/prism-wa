"""Segments: the filter language, its SQL, and proof the two agree on random data."""
import random
from datetime import datetime, timedelta, timezone

import helpers as H
import psycopg2
import pytest

from prism_wa import contacts, segments
from prism_wa.segments import FilterError, compile_filter, matches, normalise

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
DEFS = {"City": {"type": "string", "options": None}, "Visits": {"type": "number", "options": None},
        "Joined": {"type": "date", "options": None}, "Vip": {"type": "bool", "options": None},
        "Tier": {"type": "enum", "options": ["Gold", "Silver", "Bronze"]}}


def comp(f, **kw):
    return compile_filter(f, defs=DEFS, now=NOW, **kw)


# ── the language (no database) ───────────────────────────────────────────────

def test_the_legacy_tags_shape_means_any_of_those_tags():
    assert normalise({"tags": ["vip"]}) == {"field": "tags", "op": "has_any", "value": ["vip"]}
    assert normalise({"tags": []}) == {"all": []} and normalise(None) == {"all": []} and normalise({}) == {"all": []}
    assert comp({"tags": ["vip", "cold"]}).params == [["vip", "cold"]]


def test_an_empty_all_matches_everyone_and_an_empty_any_nobody():
    assert comp({"all": []}).sql == "true" and comp({"any": []}).sql == "false"


@pytest.mark.parametrize("bad,why", [
    ("x", "object"), ({"field": "tags"}, "field and an op"), ({"field": "nope", "op": "eq", "value": 1}, "unknown field"),
    ({"field": "tags", "op": "eq", "value": ["a"]}, "cannot use"), ({"field": "tags", "op": "has", "value": []}, "needs a value"),
    ({"field": "opted_out", "op": "is", "value": "yes"}, "true or false"), ({"field": "mobile", "op": "eq", "value": "abc"}, "digits"),
    ({"field": "created", "op": "within_days", "value": -1}, "between"), ({"field": "created", "op": "before", "value": "soon"}, "date"),
    ({"field": "attr:Nope", "op": "eq", "value": 1}, "no attribute"), ({"field": "attr:City; drop", "op": "eq", "value": 1}, "no attribute"),
    ({"field": "attr:Visits", "op": "eq", "value": "many"}, "not a number"), ({"field": "attr:City", "op": "gt", "value": 1}, "cannot use"),
    ({"field": "attr:Visits", "op": "between", "value": [1]}, "two values"), ({"field": "attr:Tier", "op": "eq", "value": "Platinum"}, "not one of"),
    ({"field": "campaign", "op": "sent", "campaign": "x"}, "campaign id"), ({"field": "campaign", "op": "bounced", "campaign": "x"}, "cannot use"),
    ({"field": "segment", "op": "in", "value": "11111111-1111-4111-8111-111111111111"}, "cannot be used here"),
    ({"all": "nope"}, "list")])
def test_invalid_filters_say_what_is_wrong(bad, why):
    with pytest.raises(FilterError) as e:
        comp(bad)
    assert why in str(e.value)


def test_filters_have_a_depth_and_size_limit():
    deep = {"field": "tags", "op": "has", "value": "a"}
    for _ in range(segments.MAX_DEPTH + 2):
        deep = {"not": deep}
    with pytest.raises(FilterError):
        comp(deep)
    with pytest.raises(FilterError):
        comp({"all": [{"field": "tags", "op": "has", "value": "a"}] * (segments.MAX_NODES + 1)})


def test_values_are_always_bound_never_written_into_the_sql():
    nasty = "x'); drop table contacts; --"
    c = comp({"all": [{"field": "name", "op": "contains", "value": nasty}, {"field": "tags", "op": "has_any", "value": [nasty]},
                      {"field": "attr:City", "op": "eq", "value": nasty}]})
    assert "drop table" not in c.sql and nasty in c.params and [nasty] in c.params


# ── against the database ─────────────────────────────────────────────────────

def seed(db, tid, n=120, seed_=7):
    """Random contacts with deterministic data; returns the attribute defs in use."""
    rnd = random.Random(seed_)
    conn = psycopg2.connect(db.dsn)
    with conn.cursor() as cur:
        for key, d in DEFS.items():
            contacts.define_attribute(cur, tid, key, d["type"], options=d["options"])
        names = ["Asha Shah", "Bala Patel", "Chirag", "Divya Rao", "Esha", "Farhan Khan", "", "Gita Shah"]
        tags = ["vip", "builder", "cold", "repeat", "north", "south"]
        for i in range(n):
            tg = rnd.sample(tags, rnd.randint(0, 3))
            created = NOW - timedelta(days=rnd.randint(0, 400), hours=rnd.randint(0, 23))
            seen = None if rnd.random() < 0.25 else NOW - timedelta(days=rnd.randint(0, 200))
            cur.execute(
                "insert into contacts (tenant_id, name, phone, email, tags, source_first_seen, first_msg_tag, opted_in,"
                " opt_out, blocked, created_at, last_seen) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (tid, rnd.choice(names) or "Unknown Lead", "91%010d" % (9000000000 + i),
                 rnd.choice([None, "a%d@x.in" % i, "b%d@shah.in" % i]), tg,
                 rnd.choice([None, "IMPORT", "whatsapp", "whatsapp_ad"]), rnd.choice([None, "quote please", "ad:77", "hello"]),
                 rnd.random() < 0.5, rnd.random() < 0.15, rnd.random() < 0.05, created, seen))
            cid = cur.fetchone()[0]
            attrs = {}
            if rnd.random() < 0.7:
                attrs["City"] = rnd.choice(["Vadodara", "Surat", "Mumbai"])
            if rnd.random() < 0.6:
                attrs["Visits"] = rnd.randint(0, 9)
            if rnd.random() < 0.5:
                attrs["Joined"] = (NOW - timedelta(days=rnd.randint(0, 900))).date().isoformat()
            if rnd.random() < 0.5:
                attrs["Vip"] = rnd.random() < 0.5
            if rnd.random() < 0.5:
                attrs["Tier"] = rnd.choice(["Gold", "Silver", "Bronze"])
            for k, v in attrs.items():
                contacts.set_attribute(cur, tid, cid, k, v if not isinstance(v, bool) else str(v))
            if rnd.random() < 0.4:
                cur.execute("insert into conversations (tenant_id, contact_id, channel, status) values (%s,%s,'whatsapp',%s)",
                            (tid, cid, rnd.choice(["new", "closed", "intervened"])))
    conn.commit()
    conn.close()


def rows_of(db, tid):
    out = {}
    for r in db.q("select id, name, phone, email, tags, source_first_seen as source, first_msg_tag, opted_in, opt_out,"
                  " blocked, created_at, last_seen from contacts where tenant_id = %s", (tid,)):
        r["attrs"], r["conversation_statuses"] = {}, []
        out[r["id"]] = r
    for a in db.q("select contact_id, key, value from contact_attributes where tenant_id = %s", (tid,)):
        out[a["contact_id"]]["attrs"][a["key"]] = a["value"]
    for v in db.q("select contact_id, status from conversations where tenant_id = %s", (tid,)):
        out[v["contact_id"]]["conversation_statuses"].append(v["status"])
    return out


def resolve(db, tid, f, **kw):
    conn = psycopg2.connect(db.dsn)
    try:
        with conn.cursor() as cur:
            return set(segments.resolve(cur, tid, f, now=NOW, **kw))
    finally:
        conn.close()


def random_filter(rnd, depth=0):
    if depth < 3 and rnd.random() < 0.45:
        kind = rnd.choice(["all", "any", "not"])
        if kind == "not":
            return {"not": random_filter(rnd, depth + 1)}
        return {kind: [random_filter(rnd, depth + 1) for _ in range(rnd.randint(1, 3))]}
    tags = ["vip", "builder", "cold", "repeat", "north", "south"]
    choices = [
        lambda: {"field": "name", "op": rnd.choice(["eq", "ne", "contains", "starts_with"]), "value": rnd.choice(["asha", "Shah", "e", "Unknown Lead", "kh"])},
        lambda: {"field": "name", "op": rnd.choice(["is_set", "is_not_set"])},
        lambda: {"field": "email", "op": rnd.choice(["contains", "starts_with", "eq"]), "value": rnd.choice(["shah", "a1", "x.in"])},
        lambda: {"field": "email", "op": rnd.choice(["is_set", "is_not_set"])},
        lambda: {"field": "first_msg_tag", "op": rnd.choice(["eq", "contains"]), "value": rnd.choice(["quote", "ad:77", "hello"])},
        lambda: {"field": "mobile", "op": rnd.choice(["starts_with", "contains"]), "value": rnd.choice(["919000", "00005", "9"])},
        lambda: {"field": "mobile", "op": "eq", "value": "91%010d" % rnd.randint(9000000000, 9000000119)},
        lambda: {"field": "tags", "op": rnd.choice(["has", "has_any", "has_all", "has_none"]),
                 "value": rnd.sample(tags, rnd.randint(1, 2))},
        lambda: {"field": "source", "op": rnd.choice(["eq", "in"]), "value": rnd.sample(["IMPORT", "whatsapp", "whatsapp_ad"], 1)},
        lambda: {"field": rnd.choice(["opted_in", "opted_out", "blocked"]), "op": "is", "value": rnd.random() < 0.5},
        lambda: {"field": rnd.choice(["created", "last_seen"]), "op": rnd.choice(["within_days", "older_than_days"]), "value": rnd.randint(0, 300)},
        lambda: {"field": rnd.choice(["created", "last_seen"]), "op": rnd.choice(["before", "after"]),
                 "value": (NOW - timedelta(days=rnd.randint(0, 300))).date().isoformat()},
        lambda: {"field": "conversation_status", "op": rnd.choice(["eq", "in"]), "value": rnd.sample(["new", "closed", "intervened"], 1)},
        lambda: {"field": "attr:City", "op": rnd.choice(["eq", "ne", "contains", "starts_with", "in"]),
                 "value": rnd.choice(["Vadodara", "vad", "Surat", "mum"])},
        lambda: {"field": "attr:City", "op": rnd.choice(["is_set", "is_not_set"])},
        lambda: {"field": "attr:Visits", "op": rnd.choice(["eq", "ne", "gt", "gte", "lt", "lte"]), "value": rnd.randint(0, 9)},
        lambda: {"field": "attr:Visits", "op": "between", "value": sorted([rnd.randint(0, 9), rnd.randint(0, 9)])},
        lambda: {"field": "attr:Joined", "op": rnd.choice(["eq", "gt", "gte", "lt", "lte"]),
                 "value": (NOW - timedelta(days=rnd.randint(0, 900))).date().isoformat()},
        lambda: {"field": "attr:Vip", "op": rnd.choice(["eq", "ne"]), "value": rnd.choice(["yes", "no"])},
        lambda: {"field": "attr:Tier", "op": rnd.choice(["eq", "ne", "in"]), "value": rnd.choice(["gold", "Silver", "BRONZE"])},
        lambda: {"field": "attr:Tier", "op": "is_set"},
    ]
    f = rnd.choice(choices)()
    if f["field"] == "attr:City" and f["op"] == "in":
        f["value"] = [f["value"], "Surat"]
    return f


def test_the_sql_and_the_plain_english_meaning_agree_on_random_filters_over_random_data(migrated):
    tid = H.tenant(migrated, "orena")
    seed(migrated, tid)
    rows = rows_of(migrated, tid)
    assert len(rows) == 120
    rnd = random.Random(2026)
    nonempty = 0
    for n in range(250):
        f = random_filter(rnd)
        expected = {cid for cid, row in rows.items() if matches(row, f, now=NOW, defs=DEFS)}
        got = resolve(migrated, tid, f)
        assert got == expected, (n, f, sorted(map(str, got ^ expected))[:3])
        nonempty += bool(expected)
    assert nonempty > 60                      # the filters really select people, not just nobody


def test_a_filter_only_ever_sees_its_own_workspace(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    seed(migrated, a, 20, 1)
    seed(migrated, b, 20, 2)
    got = resolve(migrated, a, {"all": []})
    assert len(got) == 20 and all(r["tenant_id"] == a for r in migrated.q("select tenant_id from contacts where id = any(%s)", (list(got),)))


def test_opted_in_only_leaves_out_opt_outs_blocked_and_unreachable_contacts(migrated):
    tid = H.tenant(migrated, "orena")
    for name, phone, bsuid, out, blocked in (("ok", "919800000001", None, False, False), ("out", "919800000002", None, True, False),
                                             ("blocked", "919800000003", None, False, True), ("bsuid", None, "IN.x", False, False),
                                             ("nobody", None, None, False, False)):
        migrated.q("insert into contacts (tenant_id, name, phone, email, bsuid, opt_out, blocked) values (%s,%s,%s,%s,%s,%s,%s)",
                   (tid, name, phone, None if phone or bsuid else "x@y.in", bsuid, out, blocked))
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        ids = segments.resolve(cur, tid, {"all": []}, opted_in_only=True)
        cur.execute("select name from contacts where id = any(%s)", (ids,))
        assert sorted(r[0] for r in cur.fetchall()) == ["bsuid", "ok"]
        assert segments.count(cur, tid, {"all": []}) == 5
    conn.close()


def test_injection_attempts_match_nothing_and_harm_nothing(migrated):
    tid = H.tenant(migrated, "orena")
    seed(migrated, tid, 10)
    nasty = "x'); drop table contacts; --"
    assert resolve(migrated, tid, {"field": "tags", "op": "has", "value": nasty}) == set()
    assert resolve(migrated, tid, {"field": "name", "op": "contains", "value": nasty}) == set()
    assert migrated.one("select count(*) from contacts") == 10


# ── segments that refer to segments ──────────────────────────────────────────

def segment(db, tid, name, filter_, dynamic=True):
    import json
    return db.one("insert into segments (tenant_id, name, filter, dynamic) values (%s,%s,%s,%s) returning id",
                  (tid, name, json.dumps(filter_), dynamic))


def contact(db, tid, name, tags=()):
    return db.one("insert into contacts (tenant_id, name, phone, tags) values (%s,%s,%s,%s) returning id",
                  (tid, name, "91%010d" % (9000000000 + abs(hash(name)) % 99999999), list(tags)))


def test_a_segment_can_include_or_exclude_another_dynamic_segment(migrated):
    tid = H.tenant(migrated, "orena")
    a, b, c = contact(migrated, tid, "A", ["vip"]), contact(migrated, tid, "B", ["vip", "cold"]), contact(migrated, tid, "C", ["cold"])
    vip = segment(migrated, tid, "VIP", {"field": "tags", "op": "has", "value": "vip"})
    assert resolve(migrated, tid, {"field": "segment", "op": "in", "value": str(vip)}) == {a, b}
    cold_not_vip = {"all": [{"field": "tags", "op": "has", "value": "cold"},
                            {"not": {"field": "segment", "op": "in", "value": str(vip)}}]}
    assert resolve(migrated, tid, cold_not_vip) == {c}


def test_a_static_segment_is_a_frozen_list_even_if_the_contacts_change(migrated):
    tid = H.tenant(migrated, "orena")
    a, b = contact(migrated, tid, "A", ["vip"]), contact(migrated, tid, "B")
    frozen = segment(migrated, tid, "Frozen", {"field": "tags", "op": "has", "value": "vip"}, dynamic=False)
    migrated.q("insert into segment_members (segment_id, contact_id, tenant_id) values (%s, %s, %s)", (frozen, a, tid))
    migrated.q("update contacts set tags = '{}' where id = %s", (a,))
    migrated.q("update contacts set tags = '{vip}' where id = %s", (b,))
    assert resolve(migrated, tid, {"field": "segment", "op": "in", "value": str(frozen)}) == {a}


def test_a_segment_that_refers_to_itself_or_to_another_workspaces_is_refused(migrated):
    a, b = H.tenant(migrated, "orena"), H.tenant(migrated, "shakti")
    me = segment(migrated, a, "Me", {"all": []})
    migrated.q("update segments set filter = %s where id = %s", ('{"field": "segment", "op": "in", "value": "%s"}' % me, me))
    with pytest.raises(FilterError):
        resolve(migrated, a, {"field": "segment", "op": "in", "value": str(me)})
    theirs = segment(migrated, b, "Theirs", {"all": []})
    with pytest.raises(FilterError):
        resolve(migrated, a, {"field": "segment", "op": "in", "value": str(theirs)})


def test_validate_returns_the_message_or_none(migrated):
    tid = H.tenant(migrated, "orena")
    conn = psycopg2.connect(migrated.dsn)
    with conn.cursor() as cur:
        assert segments.validate(cur, tid, {"field": "tags", "op": "has", "value": "x"}) is None
        assert "unknown field" in segments.validate(cur, tid, {"field": "zzz", "op": "eq", "value": 1})
    conn.close()
