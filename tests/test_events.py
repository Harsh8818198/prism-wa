"""The normaliser: Meta's payloads -> canonical events. No database."""
import payloads as P
from prism_wa.events import normalise


def one(payload):
    events = normalise(payload)
    assert len(events) == 1, events
    return events[0]


def test_a_text_message():
    ev = one(P.inbound(P.text("hello there", "wamid.T9")))
    assert (ev.kind, ev.msg_type, ev.text, ev.wamid) == ("message", "text", "hello there", "wamid.T9")
    assert (ev.waba_id, ev.phone_number_id) == (P.WABA, P.PNID)
    assert (ev.wa_id, ev.profile_name) == (P.ASHA, "Asha")
    assert ev.dedupe_key == "msg:wamid.T9"
    assert ev.occurred_at.isoformat() == "2025-10-09T08:53:21+00:00"
    assert ev.raw["message"]["text"]["body"] == "hello there"


def test_interactive_replies_carry_their_id_and_title():
    b = one(P.inbound(P.button_reply("yes_1", "Yes")))
    assert (b.msg_type, b.payload_id, b.text) == ("button_reply", "yes_1", "Yes")
    l = one(P.inbound(P.list_reply("row_7", "Site visit")))
    assert (l.msg_type, l.payload_id, l.text) == ("list_reply", "row_7", "Site visit")
    f = one(P.inbound(P.flow_reply("booking")))
    assert (f.msg_type, f.payload_id) == ("flow_reply", "booking")
    assert "slot" in f.raw["message"]["interactive"]["nfm_reply"]["response_json"]   # answers kept in raw


def test_a_template_quick_reply_button():
    ev = one(P.inbound(P.template_button("optout", "Stop")))
    assert (ev.msg_type, ev.payload_id, ev.text) == ("button", "optout", "Stop")


def test_media_location_and_reaction():
    img = one(P.inbound(P.image("media-1", "site photo")))
    assert (img.msg_type, img.media_id, img.mime_type, img.text) == ("image", "media-1", "image/jpeg", "site photo")
    doc = one(P.inbound(P.document("media-2", "boq.xlsx")))
    assert (doc.msg_type, doc.media_id, doc.text) == ("document", "media-2", "boq.xlsx")
    loc = one(P.inbound(P.location()))
    assert (loc.msg_type, loc.text) == ("location", "Site Vadodara")
    rx = one(P.inbound(P.reaction("\U0001F44D", "wamid.OUT1")))
    assert (rx.msg_type, rx.text, rx.reply_to) == ("reaction", "\U0001F44D", "wamid.OUT1")


def test_a_click_to_whatsapp_ad_referral_is_kept():
    ev = one(P.inbound(P.ad_click()))
    assert ev.referral["ctwa_clid"] == "clid-123"
    assert ev.referral["source_id"] == "ad-77"


def test_replying_to_a_message_records_which():
    assert one(P.inbound(P.reply_to_message("wamid.OUT2"))).reply_to == "wamid.OUT2"


def test_a_sender_known_only_by_username_has_no_phone_number():
    ev = one(P.username_only_sender())
    assert ev.wa_id == ""
    assert ev.bsuid == "IN.abc123XYZ"
    assert (ev.username, ev.profile_name) == ("@ravi", "Ravi")


def test_each_message_in_a_delivery_is_its_own_event():
    events = normalise(P.inbound(P.text("a", "wamid.1"), P.text("b", "wamid.2")))
    assert [e.text for e in events] == ["a", "b"]
    assert len({e.dedupe_key for e in events}) == 2


def test_a_status_keeps_pricing_and_conversation():
    ev = one(P.statuses(P.status("wamid.OUT1", "delivered", "1760000020",
                                 pricing=P.PRICING, conversation=P.CONVERSATION)))
    assert (ev.kind, ev.status, ev.wamid, ev.wa_id) == ("status", "delivered", "wamid.OUT1", P.ASHA)
    assert ev.pricing["category"] == "marketing" and ev.pricing["billable"] is True
    assert ev.conversation["id"] == "conv-1"
    assert ev.dedupe_key == "st:wamid.OUT1:delivered:1760000020"


def test_the_same_message_moving_sent_delivered_read_is_three_events():
    keys = {normalise(P.statuses(P.status("wamid.OUT1", s, t)))[0].dedupe_key
            for s, t in (("sent", "1"), ("delivered", "2"), ("read", "3"))}
    assert len(keys) == 3


def test_a_failed_status_carries_the_error_code():
    ev = one(P.statuses(P.failed_status("wamid.OUT3", 131049)))
    assert ev.status == "failed"
    assert ev.errors[0]["code"] == 131049


def test_a_status_to_a_username_user_has_only_a_bsuid():
    s = P.status("wamid.OUT9", "sent", "5")
    del s["recipient_id"]
    s["recipient_user_id"] = "IN.zzz"
    ev = one(P.statuses(s))
    assert (ev.wa_id, ev.bsuid) == ("", "IN.zzz")


def test_template_and_phone_events_are_account_level():
    t = one(P.template_status("REJECTED", "diwali_offer", 555, reason="INVALID_FORMAT"))
    assert (t.kind, t.status, t.waba_id) == ("template_status", "REJECTED", P.WABA)
    assert t.raw["value"]["reason"] == "INVALID_FORMAT"
    q = one(P.phone_quality("UPGRADE", "TIER_10K"))
    assert (q.kind, q.status, q.phone_number_id) == ("phone_quality", "UPGRADE", "")


def test_a_redelivered_account_event_has_the_same_key_but_a_new_one_does_not():
    a = one(P.template_status("APPROVED", "x", 1)).dedupe_key
    assert one(P.template_status("APPROVED", "x", 1)).dedupe_key == a
    assert one(P.template_status("REJECTED", "x", 1)).dedupe_key != a


def test_an_unknown_field_is_kept_not_dropped():
    ev = one(P.envelope({"anything": "Meta adds later"}, field="some_new_field"))
    assert ev.kind == "other" and ev.raw["value"]["anything"] == "Meta adds later"


def test_things_that_are_not_whatsapp_notifications_yield_nothing():
    for payload in (None, [], "x", {}, {"object": "page", "entry": []},
                    {"object": "whatsapp_business_account"}, {"object": "whatsapp_business_account", "entry": [1, None]}):
        assert normalise(payload) == []


def test_malformed_pieces_do_not_cost_the_good_ones():
    good = P.text("kept", "wamid.OK")
    payload = P.inbound("not a dict", 7, None, good)
    events = normalise(payload)
    assert [e.text for e in events if e.wamid == "wamid.OK"] == ["kept"]
    assert len(events) == 4                       # the junk became harmless events, nothing raised


def test_canonical_is_plain_json():
    import json
    ev = one(P.inbound(P.ad_click()))
    c = ev.canonical()
    json.dumps(c)
    assert "raw" not in c and c["referral"]["ctwa_clid"] == "clid-123"
    assert c["occurred_at"].startswith("2025-10-09")
