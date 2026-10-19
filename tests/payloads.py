"""Webhook payloads for the gateway tests.

Hand-written in the shape Meta documents (spec §1.2, WS2). They are NOT copies
of Meta's own sample payloads — spec §9 item 4 says to check the exact status
shape under the 2026 pricing model against those. The normaliser is built to
tolerate drift, and these keep the contract honest until the samples are
dropped in alongside.
"""
WABA = "100200300400500"
PNID = "109876543210987"
OTHER_PNID = "100000000000001"
ASHA = "919800000001"


def envelope(value, *, field="messages", waba=WABA, time=1760000000):
    return {"object": "whatsapp_business_account",
            "entry": [{"id": waba, "time": time, "changes": [{"field": field, "value": value}]}]}


def _meta(pnid):
    return {"messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "919726063133", "phone_number_id": pnid}}


def inbound(*messages, contact=None, pnid=PNID, waba=WABA):
    value = {**_meta(pnid),
             "contacts": [contact or {"profile": {"name": "Asha"}, "wa_id": ASHA}],
             "messages": list(messages)}
    return envelope(value, waba=waba)


def statuses(*sts, pnid=PNID, waba=WABA):
    return envelope({**_meta(pnid), "statuses": list(sts)}, waba=waba)


def _base(wamid, ts, frm=ASHA):
    return {"from": frm, "id": wamid, "timestamp": ts}


def text(body="hello", wamid="wamid.T1", ts="1760000001", frm=ASHA):
    return {**_base(wamid, ts, frm), "type": "text", "text": {"body": body}}


def button_reply(id_="yes_1", title="Yes", wamid="wamid.B1", ts="1760000002"):
    return {**_base(wamid, ts), "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": id_, "title": title}}}


def list_reply(id_="row_7", title="Site visit", wamid="wamid.L1", ts="1760000003"):
    return {**_base(wamid, ts), "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": id_, "title": title}}}


def flow_reply(name="booking", wamid="wamid.F1", ts="1760000004"):
    return {**_base(wamid, ts), "type": "interactive",
            "interactive": {"type": "nfm_reply", "nfm_reply": {
                "name": name, "body": "Sent", "response_json": "{\"slot\":\"10:00\"}"}}}


def template_button(payload="optout", text_="Stop", wamid="wamid.Q1", ts="1760000005"):
    return {**_base(wamid, ts), "type": "button", "button": {"payload": payload, "text": text_}}


def image(media_id="media-1", caption="site photo", wamid="wamid.I1", ts="1760000006"):
    return {**_base(wamid, ts), "type": "image",
            "image": {"id": media_id, "mime_type": "image/jpeg", "caption": caption}}


def document(media_id="media-2", filename="boq.xlsx", wamid="wamid.D1", ts="1760000007"):
    return {**_base(wamid, ts), "type": "document",
            "document": {"id": media_id, "mime_type": "application/vnd.ms-excel", "filename": filename}}


def location(wamid="wamid.G1", ts="1760000008"):
    return {**_base(wamid, ts), "type": "location",
            "location": {"latitude": 22.30, "longitude": 73.18, "name": "Site", "address": "Vadodara"}}


def reaction(emoji="\U0001F44D", to="wamid.OUT1", wamid="wamid.R1", ts="1760000009"):
    return {**_base(wamid, ts), "type": "reaction", "reaction": {"message_id": to, "emoji": emoji}}


def ad_click(wamid="wamid.A1", ts="1760000010"):
    m = text("Hi, saw your ad", wamid, ts)
    m["referral"] = {"source_url": "https://fb.me/abc", "source_id": "ad-77", "source_type": "ad",
                     "ctwa_clid": "clid-123"}
    return m


def reply_to_message(to="wamid.OUT2", wamid="wamid.C1", ts="1760000011"):
    m = text("this one", wamid, ts)
    m["context"] = {"from": "919726063133", "id": to}
    return m


def username_only_sender():
    """No phone number at all: the sender is known only by a business-scoped id."""
    msg = {"from_user_id": "IN.abc123XYZ", "id": "wamid.U1", "timestamp": "1760000012",
           "type": "text", "text": {"body": "hi from a username"}}
    contact = {"profile": {"name": "Ravi", "username": "@ravi"}, "user_id": "IN.abc123XYZ"}
    return inbound(msg, contact=contact)


def status(wamid="wamid.OUT1", state="delivered", ts="1760000020", **extra):
    s = {"id": wamid, "status": state, "timestamp": ts, "recipient_id": ASHA}
    s.update(extra)
    return s


PRICING = {"billable": True, "pricing_model": "PMP", "category": "marketing", "type": "regular"}
CONVERSATION = {"id": "conv-1", "origin": {"type": "marketing"}}


def failed_status(wamid="wamid.OUT3", code=131049, ts="1760000030"):
    return status(wamid, "failed", ts,
                  errors=[{"code": code, "title": "Message not delivered to maintain healthy ecosystem engagement"}])


def template_status(event="APPROVED", name="diwali_offer", tid=555, waba=WABA, reason="NONE"):
    return envelope({"event": event, "message_template_id": tid, "message_template_name": name,
                     "message_template_language": "en_US", "reason": reason},
                    field="message_template_status_update", waba=waba)


def phone_quality(event="UPGRADE", limit="TIER_10K", waba=WABA):
    """Account-level: carries a display number but no phone_number_id."""
    return envelope({"display_phone_number": "919726063133", "event": event, "current_limit": limit},
                    field="phone_number_quality_update", waba=waba)
