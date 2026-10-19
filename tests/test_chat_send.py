"""A template sent to one person by hand: the blanks, the refusals, and that it goes through the ordinary outbox."""
import json

import helpers as H
import psycopg2
import pytest

from prism_wa import chat_send, send
from prism_wa.chat_send import ChatSendError

BODY = {"type": "BODY", "text": "Hello {{1}}, quotation {{2}} is ready for your project and we would love to go through it.",
        "example": {"body_text": [["Asha", "Q-1"]]}}
BUTTONS = {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Yes"}, {"type": "URL", "text": "Open", "url": "https://x.in/q/{{1}}"}]}


@pytest.fixture
def world(migrated):
    tenant_id, number_id = H.onboard(migrated)
    waba = migrated.one("select id from wabas")
    for name, status, comps in (("quote_ready", "APPROVED", [BODY, BUTTONS]), ("plain", "APPROVED", [{"type": "BODY", "text": "Thanks for visiting us today.", "example": None}]),
                                ("draft", "PENDING", [BODY])):
        migrated.q("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s,%s,%s,'en_US','UTILITY',%s,%s)",
                   (tenant_id, waba, name, status, json.dumps(comps)))
    asha = migrated.one("insert into contacts (tenant_id, name, phone, opted_in) values (%s,'Asha','919800100001',true) returning id", (tenant_id,))

    class W:
        db, tid, asha_id = migrated, tenant_id, asha

        @classmethod
        def do(cls, fn, *a, **kw):
            conn = psycopg2.connect(migrated.dsn)
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

    return W


def queued(world):
    return world.db.q("select kind, payload, to_phone from outbox where tenant_id = %s order by created_at", (world.tid,))


def test_the_blanks_become_the_templates_parameters(world):
    out = world.do(chat_send.send_template, world.asha_id, name="quote_ready", body=["Asha", "Q-2041"], buttons={"1": "2041"})
    assert out["queued"] is True
    (row,) = queued(world)
    assert row["kind"] == "template" and row["to_phone"] == "919800100001"
    comps = row["payload"]["template"]["components"]
    assert comps[0] == {"type": "body", "parameters": [{"type": "text", "text": "Asha"}, {"type": "text", "text": "Q-2041"}]}
    assert {"type": "button", "sub_type": "url", "index": "1", "parameters": [{"type": "text", "text": "2041"}]} in comps
    assert {"type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [{"type": "payload", "payload": "direct:0"}]} in comps


def test_a_template_without_blanks_sends_as_it_is(world):
    world.do(chat_send.send_template, world.asha_id, name="plain")
    assert "components" not in queued(world)[0]["payload"]["template"] or queued(world)[0]["payload"]["template"]["components"] == []


@pytest.mark.parametrize("body,buttons", [([], {"1": "x"}), (["Asha"], {"1": "x"}), (["Asha", ""], {"1": "x"}), (["Asha", "Q"], {})])
def test_a_blank_left_empty_is_asked_for(world, body, buttons):
    with pytest.raises(ChatSendError) as e:
        world.do(chat_send.send_template, world.asha_id, name="quote_ready", body=body, buttons=buttons)
    assert e.value.reason == "missing_value" and queued(world) == []


def test_only_an_approved_known_template_goes(world):
    with pytest.raises(ChatSendError) as pending:
        world.do(chat_send.send_template, world.asha_id, name="draft", body=["a", "b"])
    assert pending.value.reason == "template_not_approved" and "pending" in str(pending.value)
    with pytest.raises(ChatSendError) as unknown:
        world.do(chat_send.send_template, world.asha_id, name="nope")
    assert unknown.value.reason == "unknown_template"


def test_someone_who_opted_out_is_not_written_to_first(world):
    world.db.q("update contacts set opt_out = true where id = %s", (world.asha_id,))
    with pytest.raises(send.SendRefused) as e:
        world.do(chat_send.send_template, world.asha_id, name="plain")
    assert e.value.reason == "opted_out"


def test_a_header_file_needs_its_address(world):
    world.db.q("insert into templates (tenant_id, waba_id, name, language, category, status, components) values (%s,(select id from wabas),'with_pic','en_US','MARKETING','APPROVED',%s)",
               (world.tid, json.dumps([{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "A new look at our work for you.", "example": None}])))
    with pytest.raises(ChatSendError) as e:
        world.do(chat_send.send_template, world.asha_id, name="with_pic")
    assert e.value.reason == "missing_media"
    world.do(chat_send.send_template, world.asha_id, name="with_pic", header_link="https://x.in/a.jpg")
    assert queued(world)[0]["payload"]["template"]["components"][0] == {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://x.in/a.jpg"}}]}
