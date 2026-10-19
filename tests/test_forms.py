"""Forms (WhatsApp Flows): the model and its checks, the Flow JSON, publishing to a fake Meta, sending, keeping answers, and the encrypted live endpoint — against a real Postgres.

This proves the sequence, the safety rules and the cryptography round trip (a test plays Meta: it seals a request with the workspace's public key and opens the answer). It does
NOT prove Meta accepts every property name in the Flow JSON; Meta's own validation answer is stored with the form and shown to the owner.
"""
import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import form_endpoint, forms
from prism_wa.app import create_app
from prism_wa.forms import FormError
from prism_wa.graph import Graph
from prism_wa.jobs import utcnow
from prism_wa.signature import sign
from prism_wa.vault import Vault

IST = timezone(timedelta(hours=5, minutes=30))
APP_SECRET = "s"
PUBLIC = "https://licence.example"


class FakeMeta:
    def __init__(self):
        self.calls, self.asset_errors, self.counter, self.statuses = [], [], 0, {}

    def __call__(self, method, url, data, headers):
        parts = urlsplit(url)
        path = parts.path.split("/v25.0/", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(data) if data and headers.get("Content-Type", "").startswith("application/json") else None
        call = {"method": method, "path": path, "query": query, "body": body}
        if data and headers.get("Content-Type", "").startswith("multipart"):
            m = re.search(rb"(\{.*\})", data, re.S)
            call["asset"] = json.loads(m.group(1)) if m else None
        self.calls.append(call)
        if method == "POST" and path.endswith("/flows"):
            self.counter += 1
            return 200, json.dumps({"id": "FLOW%d" % self.counter, "success": True}).encode()
        if method == "POST" and path.endswith("/assets"):
            return 200, json.dumps({"success": True, "validation_errors": self.asset_errors}).encode()
        if method == "GET" and path.endswith("/flows"):
            return 200, json.dumps({"data": [{"id": k, "status": v} for k, v in self.statuses.items()]}).encode()
        return 200, json.dumps({"success": True}).encode()

    def of(self, suffix):
        return [c for c in self.calls if c["path"].endswith(suffix)]


@pytest.fixture
def world(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"pro\"}')", (tid,))
    blob, ver = vault.encrypt("EAAB-business-token-for-the-waba", context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    meta = FakeMeta()
    graph = Graph(meta)
    app = create_app(app_secrets=APP_SECRET, verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok", graph=graph, vault=vault)
    client = TestClient(app)

    class W:
        db, tid_, vault_, meta_, graph_, number_ = migrated, tid, vault, meta, graph, number

        @staticmethod
        def post(path, **body):
            body.setdefault("tenant_id", str(tid))
            return client.post(path, json=body, headers={"X-Internal-Token": "tok"})

        @staticmethod
        def conn():
            return psycopg2.connect(migrated.dsn)

        @classmethod
        def do(cls, fn, *a, **kw):
            c = cls.conn()
            try:
                with c.cursor() as cur:
                    out = fn(cur, tid, *a, **kw)
                c.commit()
                return out
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

        @classmethod
        def person(cls, name="Asha Shah", phone="919800000001"):
            c = cls.conn()
            with c.cursor() as cur:
                cur.execute("insert into contacts (tenant_id, name, phone, opted_in) values (%s, %s, %s, true) returning id", (tid, name, phone))
                cid = cur.fetchone()[0]
                cur.execute("insert into conversations (tenant_id, contact_id, channel, last_active_at, last_inbound_at, number_id) values (%s, %s, 'whatsapp', now(), now(), %s)",
                             (tid, cid, number))
            c.commit()
            c.close()
            return cid

        @classmethod
        def published(cls, template="lead_capture", **kw):
            f = cls.do(forms.create_form, {"template": template, **kw})
            return cls.do(forms.publish, cls.graph_, vault, f["id"], public_url=PUBLIC)

        @classmethod
        def outbox(cls):
            return [dict(r) for r in migrated.q("select kind, payload, to_phone from outbox order by created_at, id")]

        @classmethod
        def deliver(cls, payload):
            H.deliver(migrated, payload)
            H.work(migrated)

    return W


# ── the starting points and the checks ───────────────────────────────────────

def test_every_starting_point_is_a_form_that_passes_its_own_checks():
    assert {t["key"] for t in forms.template_list()} == {"lead_capture", "appointment", "feedback", "site_visit", "quotation", "kyc_lite"}
    for key, t in forms.TEMPLATES.items():
        f = {"name": t["name"], "categories": t["categories"], "endpoint": t["endpoint"], "mapping": t["mapping"], "settings": forms.clean_settings(t["settings"]),
             "screens": forms.clean_screens(t["screens"])}
        assert forms.check(f) == [], key
        json.dumps(forms.to_flow_json(f))


def form_of(key, **over):
    t = forms.TEMPLATES[key]
    f = {"name": t["name"], "categories": t["categories"], "endpoint": t["endpoint"], "mapping": dict(t["mapping"]), "settings": forms.clean_settings(t["settings"]),
         "screens": forms.clean_screens(t["screens"])}
    f.update(over)
    return f


def test_a_plain_form_becomes_flow_json_whose_last_button_completes_with_every_answer():
    flow = forms.to_flow_json(form_of("quotation"))
    assert flow["version"] == forms.FLOW_VERSION and [s["id"] for s in flow["screens"]] == ["PROJECT", "CONTACT"] and "routing_model" not in flow
    first, last = flow["screens"]
    footer1 = first["layout"]["children"][-1]["children"][-1]["on-click-action"]
    assert footer1["name"] == "navigate" and footer1["next"] == {"type": "screen", "name": "CONTACT"} and footer1["payload"]["project"] == "${form.project}"
    assert set(last["data"]) == {"project", "area", "city"}                                  # the second screen is told what the first collected
    footer2 = last["layout"]["children"][-1]["children"][-1]["on-click-action"]
    assert footer2["name"] == "complete" and footer2["payload"]["city"] == "${data.city}" and footer2["payload"]["name"] == "${form.name}"
    assert last["terminal"] is True and last["success"] is True and "terminal" not in first
    kinds = [c["type"] for c in last["layout"]["children"][-1]["children"]]
    assert kinds == ["TextInput", "TextInput", "TextInput", "OptIn", "Footer"]


def test_each_field_kind_becomes_the_component_meta_expects():
    screens = forms.clean_screens([{"id": "A", "title": "All", "button": "Go", "fields": [
        {"kind": "text", "name": "t", "label": "Text"}, {"kind": "email", "name": "e", "label": "Email"}, {"kind": "phone", "name": "p", "label": "Phone"},
        {"kind": "number", "name": "n", "label": "Number", "required": False}, {"kind": "paragraph", "name": "g", "label": "Long"}, {"kind": "date", "name": "d", "label": "Day"},
        {"kind": "dropdown", "name": "dd", "label": "Pick", "options": ["One", "Two"]}, {"kind": "radio", "name": "r", "label": "Radio", "options": ["A", "B"]},
        {"kind": "checkbox", "name": "c", "label": "Check", "options": ["X", "Y"]}, {"kind": "opt_in", "name": "o", "label": "Agree", "helper": "Read the terms"}]}])
    comps = forms.to_flow_json({"screens": screens, "settings": {}, "endpoint": False})["screens"][0]["layout"]["children"][0]["children"]
    assert [(c["type"], c.get("input-type")) for c in comps[:4]] == [("TextInput", "text"), ("TextInput", "email"), ("TextInput", "phone"), ("TextInput", "number")]
    assert [c["type"] for c in comps[4:]] == ["TextArea", "DatePicker", "Dropdown", "RadioButtonsGroup", "CheckboxGroup", "OptIn", "Footer"]
    assert comps[6]["data-source"] == [{"id": "one", "title": "One"}, {"id": "two", "title": "Two"}] and comps[3]["required"] is False and comps[9]["name"] == "o"


def test_a_live_form_is_routed_and_ends_on_a_thank_you_screen():
    flow = forms.to_flow_json(form_of("appointment"))
    assert flow["data_api_version"] == forms.DATA_API_VERSION and flow["routing_model"] == {"BOOK": ["SUCCESS"], "SUCCESS": []}
    book, done = flow["screens"]
    comps = book["layout"]["children"][-1]["children"]
    date = [c for c in comps if c["type"] == "DatePicker"][0]
    assert date["on-select-action"] == {"name": "data_exchange", "payload": {"step": "date", "date": "${form.date}"}} and date["min-date"] == "${data.min_date}"
    slot = [c for c in comps if c.get("name") == "slot"][0]
    assert slot["data-source"] == "${data.slots}" and book["data"]["slots"]["type"] == "array"
    submit = comps[-1]["on-click-action"]
    assert submit["name"] == "data_exchange" and submit["payload"]["step"] == "submit" and submit["payload"]["slot"] == "${form.slot}"
    assert done["id"] == "SUCCESS" and done["terminal"] and "Booked" in done["layout"]["children"][1]["text"]


@pytest.mark.parametrize("change,fragment", [
    ({"name": ""}, "name"), ({"screens": []}, "at least one screen"), ({"categories": []}, "category"), ({"categories": ["NONSENSE"]}, "categories from"),
    ({"mapping": {"ghost": "City"}}, "not a field"),
])
def test_form_level_problems_are_found_in_words(change, fragment):
    problems = forms.check(form_of("lead_capture", **change))
    assert any(fragment in p["message"] for p in problems), problems


def tweak(key, fn):
    f = form_of("lead_capture") if key is None else form_of(key)
    fn(f)
    return forms.check(f)


@pytest.mark.parametrize("fn,fragment", [
    (lambda f: f["screens"][0]["fields"][0].update(name="Bad Name"), "lowercase letters"),
    (lambda f: f["screens"][0]["fields"][0].update(label="A label that is far too long to fit"), "up to 20 characters"),
    (lambda f: f["screens"][0]["fields"][0].update(label=""), "label"),
    (lambda f: f["screens"][0]["fields"][1].update(name="name"), "Two fields are called"),
    (lambda f: f["screens"][0].update(id="book visit"), "capital letters"),
    (lambda f: f["screens"][0].update(title=""), "title"),
    (lambda f: f["screens"][0].update(button="x" * 40), "button"),
    (lambda f: f["screens"][0]["fields"][3].update(options=[]), "at least one choice"),
    (lambda f: f["screens"][0]["fields"][3].update(options=[{"id": "a", "title": "x" * 31}]), "title of up to 30"),
    (lambda f: f["screens"][0]["fields"][3].update(options=[{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]), "share an id"),
    (lambda f: f["screens"][0].update(fields=f["screens"][0]["fields"] * 5), "up to 20 fields"),
    (lambda f: f["screens"][0].update(fields=[], texts=[]), "at least a field or some text"),
    (lambda f: f["screens"][0]["fields"][0].update(kind="hologram"), "not a kind"),
])
def test_field_and_screen_problems_are_found_in_words_and_name_the_place(fn, fragment):
    problems = tweak(None, fn)
    assert any(fragment in p["message"] for p in problems) and all(p["where"] for p in problems), problems


def test_a_radio_group_holds_twenty_choices_and_a_dropdown_two_hundred():
    f = form_of("lead_capture")
    f["screens"][0]["fields"][3].update(kind="radio", options=[{"id": "o%d" % i, "title": "Option %d" % i} for i in range(21)])
    assert any("up to 20" in p["message"] for p in forms.check(f))
    f["screens"][0]["fields"][3].update(kind="dropdown", options=[{"id": "o%d" % i, "title": "Option %d" % i} for i in range(200)])
    assert forms.check(f) == []


def test_a_live_form_with_times_needs_opening_hours_and_the_fields_they_refer_to():
    f = form_of("appointment")
    f["settings"] = {}
    assert any("opening days and hours" in p["message"] for p in forms.check(f))
    f["settings"] = forms.clean_settings(forms.TEMPLATES["appointment"]["settings"])
    f["settings"]["availability"]["slot_field"] = "when"
    assert any("'when'" in p["message"] for p in forms.check(f))
    f = form_of("appointment")
    f["screens"][0]["id"] = "SUCCESS"
    assert any("SUCCESS is kept" in p["message"] for p in forms.check(f))


@pytest.mark.parametrize("availability", [{"weekdays": []}, {"start": "17:00", "end": "10:00"}, {"start": "9am"}, {"slot_minutes": 5}, {"capacity": 0}, {"window_days": 500},
                                          {"tz": "Mars/Base"}, {"min_notice_hours": "soon"}])
def test_opening_hours_are_checked_in_words(availability):
    base = forms.TEMPLATES["appointment"]["settings"]["availability"]
    with pytest.raises(FormError) as why:
        forms.clean_settings({"availability": {**base, **availability}})
    assert why.value.reason == "bad_availability" and str(why.value)


def test_a_contact_detail_name_must_be_plain(world):
    with pytest.raises(FormError) as why:
        world.do(forms.create_form, {"template": "lead_capture", "mapping": {"name": "x; drop table"}})
    assert why.value.reason == "bad_mapping"


# ── keeping forms ────────────────────────────────────────────────────────────

def test_a_form_is_made_from_a_starting_point_changed_copied_and_removed(world):
    f = world.do(forms.create_form, {"template": "lead_capture", "name": "Walls enquiry"})
    assert f["name"] == "Walls enquiry" and f["status"] == "draft" and f["problems_now"] == [] and f["mapping"]["email"] == "@email" and f["tag_on_submit"] == "form-lead"
    g = world.do(forms.update_form, f["id"], {"screens": [{**f["screens"][0], "title": "Hello"}], "tag_on_submit": "walls"})
    assert g["screens"][0]["title"] == "Hello" and g["tag_on_submit"] == "walls"
    c = world.do(forms.duplicate_form, f["id"])
    assert c["name"] == "Walls enquiry (copy)" and c["id"] != f["id"] and len(world.do(forms.list_forms)) == 2
    assert world.do(forms.delete_form, f["id"]) is True and len(world.do(forms.list_forms)) == 1


def test_an_unknown_starting_point_or_form_is_refused(world):
    with pytest.raises(FormError) as why:
        world.do(forms.create_form, {"template": "nope"})
    assert why.value.reason == "unknown_template"
    with pytest.raises(FormError) as why:
        world.do(forms.get_form, "00000000-0000-0000-0000-000000000000")
    assert why.value.reason == "unknown_form"


# ── giving a form to Meta ────────────────────────────────────────────────────

def test_publishing_creates_the_form_at_meta_uploads_its_json_and_publishes_it(world):
    f = world.published("lead_capture")
    assert f["status"] == "published" and f["meta_flow_id"] == "FLOW1" and f["version"] == 1 and f["published_at"]
    assert [c["path"].split("/")[-1] for c in world.meta_.calls if c["method"] == "POST"] == ["flows", "assets", "publish"]
    created = world.meta_.of("/flows")[0]["body"]
    assert created["name"] == "Lead capture" and created["categories"] == ["LEAD_GENERATION"] and "endpoint_uri" not in created
    uploaded = world.meta_.of("/assets")[0]["asset"]
    assert uploaded["version"] == forms.FLOW_VERSION and uploaded["screens"][0]["id"] == "ENQUIRY"


def test_what_meta_finds_wrong_is_kept_with_the_form_and_the_form_stays_a_draft(world):
    world.meta_.asset_errors = [{"error": "INVALID_PROPERTY", "message": "Property 'label' is too long.", "line_start": 12}]
    f = world.do(forms.create_form, {"template": "lead_capture"})
    c = world.conn()                                  # (the route commits what Meta said before it answers 422; do the same here)
    with c.cursor() as cur, pytest.raises(FormError) as why:
        forms.publish(cur, world.tid_, world.graph_, world.vault_, f["id"], public_url=PUBLIC)
    c.commit()
    c.close()
    assert why.value.reason == "meta_rejected" and "too long" in str(why.value) and why.value.problems[0]["where"] == "Meta"
    got = world.do(forms.get_form, f["id"])
    assert got["status"] == "draft" and got["problems"][0]["message"].startswith("Property 'label'") and got["meta_flow_id"] == "FLOW1"
    assert not world.meta_.of("/publish")


def test_a_form_that_fails_prisms_own_checks_never_reaches_meta(world):
    f = world.do(forms.create_form, {"template": "lead_capture", "name": "x"})
    world.do(forms.update_form, f["id"], {"screens": [{"id": "a b", "title": "", "fields": []}]})
    with pytest.raises(FormError) as why:
        world.do(forms.publish, world.graph_, world.vault_, f["id"], public_url=PUBLIC)
    assert why.value.reason == "form_invalid" and len(why.value.problems) >= 2 and not world.meta_.calls


def test_changing_a_published_form_makes_a_new_version_at_meta_and_retires_the_old_one(world):
    f = world.published("lead_capture")
    world.do(forms.update_form, f["id"], {"name": "Lead capture v2"})
    again = world.do(forms.publish, world.graph_, world.vault_, f["id"], public_url=PUBLIC)
    assert again["meta_flow_id"] == "FLOW2" and again["version"] == 2 and again["status"] == "published"
    assert world.meta_.of("FLOW1/deprecate")


def test_a_live_form_sends_meta_the_endpoint_address_and_its_key_once(world):
    f = world.published("appointment")
    created = world.meta_.of("/flows")[0]["body"]
    assert created["endpoint_uri"] == "%s/v1/whatsapp/forms/endpoint/%s/%s" % (PUBLIC, world.tid_, f["id"])
    key = world.meta_.of("/whatsapp_business_encryption")[0]
    assert key["path"].startswith(P.PNID) and key["query"]["business_public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert world.do(forms.key_status)["has_key"] and world.do(forms.key_status)["uploaded_at"]
    assert world.db.one("select count(*) from flow_keys") == 1


def test_a_live_form_needs_a_public_https_address(world):
    f = world.do(forms.create_form, {"template": "appointment"})
    with pytest.raises(FormError) as why:
        world.do(forms.publish, world.graph_, world.vault_, f["id"], public_url="http://localhost")
    assert why.value.reason == "not_set_up"


def test_the_private_key_is_encrypted_at_rest(world):
    world.published("appointment")
    raw = bytes(world.db.one("select private_enc from flow_keys"))
    assert b"PRIVATE KEY" not in raw and world.do(forms.private_key, world.vault_).startswith("-----BEGIN PRIVATE KEY-----")


def test_a_form_can_be_retired_and_meta_can_be_asked_what_it_thinks(world):
    f = world.published("lead_capture")
    assert world.do(forms.retire, world.graph_, world.vault_, f["id"])["status"] == "deprecated"
    with pytest.raises(FormError) as why:
        world.do(forms.retire, world.graph_, world.vault_, f["id"])
    assert why.value.reason == "not_published"
    g = world.published("feedback")
    world.meta_.statuses = {g["meta_flow_id"]: "THROTTLED"}
    assert world.do(forms.sync_status, world.graph_, world.vault_) == 1 and world.do(forms.get_form, g["id"])["status"] == "throttled"


# ── sending ──────────────────────────────────────────────────────────────────

def test_a_form_that_is_not_published_cannot_be_sent(world):
    f = world.do(forms.create_form, {"template": "lead_capture"})
    with pytest.raises(FormError) as why:
        world.do(forms.send_form, f["id"], contact_id=world.person(), idem_key="k")
    assert why.value.reason == "not_published"


def test_a_plain_form_is_sent_as_a_flow_message_that_opens_on_its_first_screen(world):
    f = world.published("lead_capture")
    who = world.person()
    world.do(forms.send_form, f["id"], contact_id=who, body="Tell us what you need", button="Open", idem_key="k1")
    i = world.outbox()[-1]["payload"]["interactive"]
    p = i["action"]["parameters"]
    assert i["type"] == "flow" and i["action"]["name"] == "flow" and p["flow_id"] == "FLOW1" and p["flow_cta"] == "Open" and p["mode"] == "published"
    assert p["flow_action"] == "navigate" and p["flow_action_payload"] == {"screen": "ENQUIRY"} and p["flow_message_version"] == "3"
    assert forms.read_token(p["flow_token"]) == (str(f["id"]), str(who))


def test_a_live_form_is_opened_by_asking_prism(world):
    f = world.published("appointment")
    world.do(forms.send_form, f["id"], contact_id=world.person(), idem_key="k2")
    p = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]
    assert p["flow_action"] == "data_exchange" and "flow_action_payload" not in p


@pytest.mark.parametrize("token", ["", "x", "pf1.a.b.c", "pf1.%s.%s.ff" % ("z" * 32, "y" * 32), None])
def test_a_flow_token_we_did_not_make_is_not_trusted(token):
    assert forms.read_token(token) == (None, None)


# ── what customers fill in ───────────────────────────────────────────────────

def reply(answers, token, wamid="wamid.FR1"):
    return P.inbound({"from": P.ASHA, "id": wamid, "timestamp": "1760000300", "type": "interactive", "interactive": {"type": "nfm_reply", "nfm_reply": {
        "name": "flow", "body": "Sent", "response_json": json.dumps({"flow_token": token, **answers})}}})


def test_answers_are_kept_matched_to_the_contact_and_acted_on(world):
    f = world.published("lead_capture")
    who = world.person(name="WhatsApp Name", phone=P.ASHA)
    world.do(forms.send_form, f["id"], contact_id=who, idem_key="k3")
    token = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    world.deliver(reply({"name": "Asha Shah", "phone": "9800000001", "email": "asha@shakti.example", "need": "gate", "note": "Corner plot"}, token))
    r = world.do(forms.list_responses, f["id"])
    assert r["total"] == 1 and r["responses"][0]["answers"]["need"] == "gate" and r["responses"][0]["contact_id"] == who
    row = world.db.q("select name, email, tags from contacts where id = %s", (who,))[0]
    assert row["name"] == "Asha Shah" and row["email"] == "asha@shakti.example" and "form-lead" in row["tags"]
    assert world.db.one("select value from contact_attributes where contact_id = %s and key = 'Interest'", (who,)) == "gate"
    assert world.do(forms.get_form, f["id"])["responses"] == 1
    assert "Thank you" in world.outbox()[-1]["payload"]["text"]["body"]


def test_the_same_answers_delivered_twice_count_once(world):
    f = world.published("lead_capture")
    who = world.person(phone=P.ASHA)
    world.do(forms.send_form, f["id"], contact_id=who, idem_key="k4")
    token = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    world.deliver(reply({"name": "Asha", "need": "gate"}, token))
    world.deliver(reply({"name": "Asha", "need": "gate"}, token, wamid="wamid.FR2"))
    assert world.do(forms.list_responses, f["id"])["total"] == 1 and len([o for o in world.outbox() if o["kind"] == "text"]) == 1


def test_answers_with_no_token_are_matched_to_the_last_form_sent_to_that_person(world):
    f = world.published("feedback")
    who = world.person(phone=P.ASHA)
    world.do(forms.send_form, f["id"], contact_id=who, idem_key="k5")
    world.deliver(reply({"rating": "5", "comment": "Great work"}, ""))
    got = world.do(forms.list_responses, f["id"])
    assert got["total"] == 1 and got["responses"][0]["answers"]["rating"] == "5"


def test_a_form_we_never_sent_still_keeps_its_answers(world):
    world.person(phone=P.ASHA)
    world.deliver(reply({"q": "a"}, "someone-elses-token"))
    got = world.do(forms.list_responses)
    assert got["total"] == 1 and got["responses"][0]["form_id"] is None


def test_the_answers_download_as_a_sheet_that_cannot_run_as_a_formula(world):
    f = world.published("lead_capture")
    who = world.person(phone=P.ASHA)
    world.do(forms.send_form, f["id"], contact_id=who, idem_key="k6")
    token = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    world.deliver(reply({"name": "=HYPERLINK(\"http://evil\")", "need": "gate"}, token))
    text = world.do(forms.export_csv, f["id"])
    lines = text.strip().splitlines()
    assert lines[0].startswith("received,contact,phone,name,phone,email,need") and "'=HYPERLINK" in lines[1]


# ── the live endpoint ────────────────────────────────────────────────────────

class Meta:
    """Plays Meta against the endpoint: seals a request with the public key, opens the answer."""

    def __init__(self, world, form):
        self.world, self.form = world, form
        self.public = world.do(forms.ensure_key, world.vault_)

    def call(self, request, *, sign_with=APP_SECRET, tamper=False):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = serialization.load_pem_public_key(self.public.encode())
        aes, iv = os.urandom(16), os.urandom(16)
        enc = Cipher(algorithms.AES(aes), modes.GCM(iv)).encryptor()
        sealed = enc.update(json.dumps(request).encode()) + enc.finalize() + enc.tag
        body = {"encrypted_flow_data": base64.b64encode(sealed).decode(), "initial_vector": base64.b64encode(iv).decode(),
                "encrypted_aes_key": base64.b64encode(key.encrypt(aes, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()}
        if tamper:
            body["encrypted_aes_key"] = base64.b64encode(os.urandom(256)).decode()
        raw = json.dumps(body).encode()
        r = self.world.post("/internal/forms/endpoint", form_id=str(self.form["id"]), body=raw.decode(), signature=sign(sign_with, raw))
        if r.status_code != 200:
            return r
        flipped = bytes(b ^ 0xFF for b in iv)
        blob = base64.b64decode(r.json()["response"])
        dec = Cipher(algorithms.AES(aes), modes.GCM(flipped, blob[-16:])).decryptor()
        return json.loads((dec.update(blob[:-16]) + dec.finalize()).decode())


@pytest.fixture
def booking(world):
    f = world.published("appointment")
    who = world.person(phone=P.ASHA)
    world.do(forms.send_form, f["id"], contact_id=who, idem_key="kb")
    token = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    return world, f, Meta(world, f), who, token


def next_weekday(weekday, days_ahead=3):
    d = datetime.now(IST).date() + timedelta(days=days_ahead)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def test_metas_health_check_is_answered(booking):
    _w, _f, meta, _who, _t = booking
    assert meta.call({"version": "3.0", "action": "ping"}) == {"data": {"status": "active"}}


def test_opening_the_form_returns_the_first_screen_and_the_days_that_can_be_picked(booking):
    _w, _f, meta, _who, token = booking
    out = meta.call({"version": "3.0", "action": "INIT", "flow_token": token})
    assert out["screen"] == "BOOK" and out["data"]["slots"] == [] and out["data"]["min_date"] < out["data"]["max_date"] and out["data"]["has_error"] is False


def test_picking_a_day_returns_only_the_times_that_are_free(booking):
    world, f, meta, who, token = booking
    day = next_weekday(2)
    out = meta.call({"version": "3.0", "action": "data_exchange", "screen": "BOOK", "flow_token": token, "data": {"step": "date", "date": day.isoformat()}})
    ids = [s["id"] for s in out["data"]["slots"]]
    assert out["screen"] == "BOOK" and ids == ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"] and out["data"]["slots"][0]["title"] == "10:00 AM"
    assert out["data"]["slots"][3]["title"] == "1:00 PM" and out["data"]["has_error"] is False


def test_a_sunday_or_a_day_too_far_off_has_no_times_and_says_so(booking):
    _w, _f, meta, _who, token = booking
    for day in (next_weekday(6), datetime.now(IST).date() + timedelta(days=90), datetime.now(IST).date() - timedelta(days=2)):
        out = meta.call({"version": "3.0", "action": "data_exchange", "screen": "BOOK", "flow_token": token, "data": {"step": "date", "date": day.isoformat()}})
        assert out["data"]["slots"] == [] and out["data"]["has_error"] is True and "pick another day" in out["data"]["error_message"]


def book_request(token, day, slot="11:00", **extra):
    return {"version": "3.0", "action": "data_exchange", "screen": "BOOK", "flow_token": token,
            "data": {"step": "submit", "name": "Asha Shah", "date": day.isoformat(), "slot": slot, "note": "", **extra}}


def test_confirming_keeps_the_booking_and_shows_the_thank_you_screen(booking):
    world, f, meta, who, token = booking
    day = next_weekday(1)
    out = meta.call(book_request(token, day))
    assert out["screen"] == "SUCCESS" and out["data"]["extension_message_response"]["params"]["slot"] == "11:00"
    assert out["data"]["extension_message_response"]["params"]["flow_token"] == token
    got = world.do(forms.list_responses, f["id"])
    assert got["total"] == 1 and got["responses"][0]["answers"]["date"] == day.isoformat() and got["responses"][0]["contact_id"] == who
    assert world.db.one("select value from contact_attributes where contact_id = %s and key = 'Visit_time'", (who,)) == "11:00"
    assert "booked" in world.db.one("select tags from contacts where id = %s", (who,))


def test_the_booked_time_is_taken_and_is_no_longer_offered(booking):
    world, f, meta, who, token = booking
    day = next_weekday(1)
    meta.call(book_request(token, day))
    out = meta.call({"version": "3.0", "action": "data_exchange", "screen": "BOOK", "flow_token": token, "data": {"step": "date", "date": day.isoformat()}})
    assert "11:00" not in [s["id"] for s in out["data"]["slots"]] and len(out["data"]["slots"]) == 6


def test_two_people_cannot_book_the_same_time_and_the_second_is_told_why(booking):
    world, f, meta, who, token = booking
    day = next_weekday(1)
    meta.call(book_request(token, day))
    other = world.person("Ravi", "919800000002")
    world.do(forms.send_form, f["id"], contact_id=other, idem_key="kb2")
    token2 = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    out = meta.call(book_request(token2, day))
    assert out["screen"] == "BOOK" and out["data"]["has_error"] is True and "just taken" in out["data"]["error_message"] and "11:00" not in [s["id"] for s in out["data"]["slots"]]
    assert world.do(forms.list_responses, f["id"])["total"] == 1


def test_a_time_that_does_not_exist_or_a_missing_answer_is_refused(booking):
    _w, f, meta, _who, token = booking
    day = next_weekday(1)
    assert meta.call(book_request(token, day, slot="03:00"))["data"]["has_error"] is True
    out = meta.call({**book_request(token, day), "data": {"step": "submit", "name": "", "date": day.isoformat(), "slot": "10:00"}})
    assert out["screen"] == "BOOK" and "Your name" in out["data"]["error_message"]


def test_a_bigger_capacity_lets_several_people_share_a_time(world):
    f = world.published("appointment", settings={"availability": {**forms.TEMPLATES["appointment"]["settings"]["availability"], "capacity": 2}})
    meta = Meta(world, f)
    day = next_weekday(1)
    for n in range(3):
        who = world.person("P%d" % n, "91980000010%d" % n)
        world.do(forms.send_form, f["id"], contact_id=who, idem_key="kc%d" % n)
        tok = world.outbox()[-1]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
        out = meta.call(book_request(tok, day, slot="10:00"))
        assert (out["screen"] == "SUCCESS") == (n < 2)


def test_the_answers_a_booking_leaves_are_not_counted_twice_when_meta_also_sends_them(booking):
    world, f, meta, who, token = booking
    day = next_weekday(1)
    out = meta.call(book_request(token, day))
    world.deliver(reply(out["data"]["extension_message_response"]["params"], token))
    assert world.do(forms.list_responses, f["id"])["total"] == 1


def test_a_request_that_is_not_signed_by_meta_is_refused(booking):
    _w, _f, meta, _who, _t = booking
    assert meta.call({"action": "ping"}, sign_with="not-the-app-secret").status_code == 432


def test_a_request_that_cannot_be_decrypted_is_answered_421_so_meta_fetches_the_key_again(booking):
    _w, _f, meta, _who, _t = booking
    r = meta.call({"action": "ping"}, tamper=True)
    assert r.status_code == 421 and r.json()["error"] == "decrypt_failed"


def test_a_workspace_with_no_key_answers_421(world):
    f = world.do(forms.create_form, {"template": "appointment"})
    raw = json.dumps({"encrypted_flow_data": "AA==", "initial_vector": "AA==", "encrypted_aes_key": "AA=="}).encode()
    r = world.post("/internal/forms/endpoint", form_id=str(f["id"]), body=raw.decode(), signature=sign(APP_SECRET, raw))
    assert r.status_code == 421 and r.json()["error"] == "no_key"


def test_an_error_metas_phone_reports_is_acknowledged(booking):
    _w, _f, meta, _who, token = booking
    assert meta.call({"version": "3.0", "action": "data_exchange", "flow_token": token, "data": {"error": "invalid-screen", "error_message": "x"}}) == {"data": {"acknowledged": True}}


def test_the_free_times_follow_the_notice_and_the_hours(world):
    f = world.do(forms.create_form, {"template": "appointment", "settings": {"availability": {**forms.TEMPLATES["appointment"]["settings"]["availability"], "min_notice_hours": 4,
                                                                                                      "start": "09:00", "end": "12:00", "slot_minutes": 30}}})
    day = next_weekday(0, 5)

    def at(hour, back_days=0):
        return datetime.combine(day - timedelta(days=back_days), datetime.min.time(), tzinfo=IST).replace(hour=hour)

    def free(now):
        c = world.conn()
        with c.cursor() as cur:
            out = [s["id"] for s in form_endpoint.slots_for(cur, world.tid_, f, day.isoformat(), now=now)]
        c.close()
        return out
    assert free(at(9, back_days=2)) == ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
    assert free(at(6)) == ["10:00", "10:30", "11:00", "11:30"]                  # four hours' notice from 06:00
    assert free(at(10)) == []                                                 # too late for anything that day


# ── over HTTP ────────────────────────────────────────────────────────────────

def test_the_routes_build_check_publish_and_send_a_form(world):
    assert {t["key"] for t in world.post("/internal/forms/templates").json()["templates"]} >= {"lead_capture", "appointment"}
    form = world.post("/internal/forms/create", template="lead_capture").json()["form"]
    assert world.post("/internal/forms/check", form_id=form["id"]).json()["problems"] == []
    bad = world.post("/internal/forms/check", name="x", screens=[{"id": "A", "title": "T", "fields": [{"kind": "text", "name": "a", "label": "L" * 30}]}]).json()["problems"]
    assert bad and bad[0]["where"]
    preview = world.post("/internal/forms/preview", form_id=form["id"]).json()["flow_json"]
    assert preview["screens"][0]["id"] == "ENQUIRY"
    published = world.post("/internal/forms/publish", form_id=form["id"], public_url=PUBLIC)
    assert published.status_code == 200 and published.json()["form"]["status"] == "published"
    who = world.person()
    assert world.post("/internal/forms/send", form_id=form["id"], contact_id=str(who), body="Hi").json()["queued"] is True
    assert world.post("/internal/forms/list").json()["forms"][0]["fields_count"] == 5


def test_a_form_meta_rejects_is_a_422_with_the_problems_and_they_stay_on_the_form(world):
    world.meta_.asset_errors = [{"message": "Bad thing"}]
    form = world.post("/internal/forms/create", template="lead_capture").json()["form"]
    r = world.post("/internal/forms/publish", form_id=form["id"], public_url=PUBLIC)
    assert r.status_code == 422 and r.json()["error"] == "meta_rejected" and r.json()["problems"][0]["message"] == "Bad thing"
    assert world.post("/internal/forms/get", form_id=form["id"]).json()["form"]["problems"][0]["message"] == "Bad thing"


def test_an_unknown_form_is_a_404_and_forms_are_a_paid_feature(world):
    assert world.post("/internal/forms/get", form_id="00000000-0000-0000-0000-000000000000").status_code == 404
    world.db.q("update tenant_settings set settings = '{\"plan\": \"basic\"}'")
    r = world.post("/internal/forms/list")
    assert r.status_code == 403 and r.json()["needs"] == "pro"
