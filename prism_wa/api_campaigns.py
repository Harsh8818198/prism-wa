"""API campaigns: a named template, always on, that other software sends through — the way a shop's order system or a Zapier zap
tells a customer "your order shipped" without anyone opening Prism.

It is shaped like AiSensy's, so a customer who already has an integration can switch by changing the address and the key:

    POST <gateway>/campaign/t1/api/v2
    {"apiKey": "pk_live_…", "campaignName": "order_shipped", "destination": "919812345678", "userName": "Asha",
     "source": "Website", "media": {"url": "https://…/a.jpg", "filename": "a.jpg"}, "templateParams": ["Asha", "A-1"],
     "tags": ["customer"], "attributes": {"City": "Surat"}}

An API campaign is an ordinary row in `campaigns` with kind = 'api', status 'sending' while live and 'paused' when switched
off, so its sends, deliveries, reads and failures are counted by the same report as any other campaign.
"""
from __future__ import annotations

import re
import uuid

from prism_wa import chat_send, contact_book, contacts, plans, send

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")


class ApiCampaignError(Exception):
    def __init__(self, reason: str, message: str, status: int = 400):
        super().__init__(message)
        self.reason = reason
        self.status = status


def create(cur, tenant_id, name: str, template_name: str, language: str = "en_US") -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ApiCampaignError("bad_name", "Name it with letters, numbers, spaces, - _ or . (80 characters at most).")
    plans.require(cur, tenant_id, "template_api", "API campaigns")
    cur.execute("select status from templates where tenant_id = %s and name = %s and language = %s", (tenant_id, template_name, language))
    row = cur.fetchone()
    if row is None:
        raise ApiCampaignError("unknown_template", "That template is not known for this account. Update your templates from Meta first.", 404)
    if row[0] != "APPROVED":
        raise ApiCampaignError("template_not_approved", "The template is %s; only an approved template can be sent." % str(row[0]).lower(), 409)
    cur.execute("select 1 from campaigns where tenant_id = %s and kind = 'api' and name = %s", (tenant_id, name))
    if cur.fetchone():
        raise ApiCampaignError("name_taken", "You already have an API campaign called %r." % name, 409)
    cur.execute("insert into campaigns (tenant_id, name, template_name, language, kind, status, audience, variables, total, segment_name)"
                " values (%s, %s, %s, %s, 'api', 'sending', '{}'::jsonb, '{}'::jsonb, 0, '') returning id", (tenant_id, name, template_name, language))
    return str(cur.fetchone()[0])


def list_api(cur, tenant_id) -> list:
    cur.execute("select id, name, template_name, language, status, sent, failed, total, created_at from campaigns where tenant_id = %s and kind = 'api'"
                " order by created_at desc", (tenant_id,))
    cols = ("id", "name", "template_name", "language", "status", "sent", "failed", "total", "created_at")
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["live"] = d["status"] == "sending"
        out.append(d)
    return out


def set_live(cur, tenant_id, campaign_id, live: bool) -> None:
    cur.execute("update campaigns set status = %s, updated_at = now() where id = %s and tenant_id = %s and kind = 'api' returning id",
                ("sending" if live else "paused", campaign_id, tenant_id))
    if cur.fetchone() is None:
        raise ApiCampaignError("unknown_campaign", "That API campaign is not in this workspace.", 404)


def delete(cur, tenant_id, campaign_id) -> bool:
    cur.execute("delete from campaigns where id = %s and tenant_id = %s and kind = 'api'", (campaign_id, tenant_id))
    return cur.rowcount > 0


def endpoint_example(base_url: str, name: str, template_params: int = 0) -> dict:
    """What to paste into the other software: the address and a sample body (the key is theirs to add)."""
    return {"url": base_url.rstrip("/") + "/campaign/t1/api/v2",
            "body": {"apiKey": "<your API key>", "campaignName": name, "destination": "919812345678", "userName": "Customer name",
                     "templateParams": ["value %d" % (i + 1) for i in range(template_params)], "tags": [], "attributes": {}}}


def send_one(cur, tenant_id, key_name: str, body: dict) -> dict:
    """One message through an API campaign. Raises ApiCampaignError with the HTTP status the caller should answer."""
    name = str(body.get("campaignName") or "").strip()
    if not name:
        raise ApiCampaignError("campaign_required", "`campaignName` is required")
    cur.execute("select id, template_name, language, status from campaigns where tenant_id = %s and kind = 'api' and name = %s", (tenant_id, name))
    row = cur.fetchone()
    if row is None:
        raise ApiCampaignError("unknown_campaign", "there is no API campaign called %r" % name, 404)
    campaign_id, template_name, language, status = row
    if status != "sending":
        raise ApiCampaignError("campaign_not_live", "the API campaign %r is not live; switch it on in Prism" % name, 409)
    phone = contacts.normalise_phone(body.get("destination"))
    if phone is None:
        raise ApiCampaignError("bad_destination", "`destination` must be a phone number with its country code, like 919812345678")
    user = str(body.get("userName") or "").strip()
    if not user:
        raise ApiCampaignError("username_required", "`userName` is required")
    cur.execute("select components, status from templates where tenant_id = %s and name = %s and language = %s", (tenant_id, template_name, language))
    tpl = cur.fetchone()
    if tpl is None or tpl[1] != "APPROVED":
        raise ApiCampaignError("template_not_approved", "the template behind this campaign is not approved right now", 409)
    params = body.get("templateParams") or []
    if not isinstance(params, list):
        raise ApiCampaignError("bad_params", "`templateParams` must be a list of values")
    media = body.get("media") if isinstance(body.get("media"), dict) else {}
    try:
        components = chat_send.components_for(tpl[0], body=[str(p) for p in params], header_link=str(media.get("url") or ""),
                                              buttons={})
    except chat_send.ChatSendError as exc:
        raise ApiCampaignError(exc.reason, str(exc)) from None
    cur.execute("select contact_id, created from wa_resolve_contact(%s, %s, null, null, null, %s, %s)",
                (tenant_id, phone, user, str(body.get("source") or "API")[:60]))
    contact_id, created = cur.fetchone()

    defs = contacts.attribute_defs(cur, tenant_id)                      # an attribute or tag nobody defined is ignored, as AiSensy does
    ignored = []
    for k, v in (body.get("attributes") or {}).items() if isinstance(body.get("attributes"), dict) else []:
        if k in defs and v not in (None, ""):
            try:
                contacts.set_attribute(cur, tenant_id, contact_id, k, v, defs)
            except ValueError:
                ignored.append(k)
        else:
            ignored.append(k)
    known = {t["name"] for t in contact_book.list_tags(cur, tenant_id)}
    wanted = [t for t in (body.get("tags") or []) if isinstance(t, str) and t in known]
    if wanted:
        contact_book.set_tags(cur, tenant_id, [contact_id], add=wanted)

    try:
        outbox_id = send.enqueue_template(cur, tenant_id, contact_id=contact_id, name=template_name, language=language, components=components or None,
                                          idem_key="apicmp:%s:%s:%s" % (campaign_id, contact_id, uuid.uuid4().hex), campaign_id=campaign_id, sender="api")
    except send.SendRefused as exc:
        raise ApiCampaignError(exc.reason, str(exc), 409) from None
    cur.execute("insert into campaign_recipients (tenant_id, campaign_id, contact_id, status, outbox_id) values (%s, %s, %s, 'queued', %s)"
                " on conflict (campaign_id, contact_id) do update set status = 'queued', outbox_id = excluded.outbox_id", (tenant_id, campaign_id, contact_id, outbox_id))
    cur.execute("update campaigns set total = total + 1 where id = %s", (campaign_id,))
    return {"success": True, "message": "queued", "id": str(outbox_id), "contact_created": bool(created), "ignored": ignored}
