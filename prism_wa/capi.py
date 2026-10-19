"""Telling Meta what a click-to-WhatsApp ad earned: the Conversions API.

A person who clicks an ad that opens WhatsApp carries a `ctwa_clid` (a click id) in the first message's referral. When the business later
decides that person became a lead or a customer, it tells Meta ("Lead", "Purchase"...) with that click id, and Meta's ad system learns which
ads bring real customers and shows them to more people like them. That is all this does; nothing is sent for a person who did not come
from an ad (there is no click to credit), and nothing is sent until a dataset is set up.

    settings / save_settings   the dataset id (from Meta's Events Manager), kept in the workspace settings
    send_for_contact()         one event for one person, used by the flow step "Tell Meta" and by the Ads screen
    send_test()                an event with Meta's test code, so the first one can be checked in Events Manager before it counts

The call goes to https://graph.facebook.com/<version>/<dataset_id>/events with the business's WhatsApp token.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from psycopg2.extras import Json

from prism_wa import templates
from prism_wa.graph import Graph, GraphError
from prism_wa.vault import Vault

EVENTS = ("Lead", "Purchase", "ViewContent", "CompleteRegistration", "Contact", "Schedule", "SubmitApplication", "AddToCart", "InitiateCheckout",
          "Subscribe", "StartTrial")
DATASET_RE = re.compile(r"^\d{5,25}$")
_CFG: dict = {"graph": None, "vault": None}


class CapiError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def configure(*, graph: Optional[Graph] = None, vault: Optional[Vault] = None) -> None:
    _CFG.update(graph=graph, vault=vault)


def settings(cur, tenant_id) -> dict:
    cur.execute("select settings->'capi' from tenant_settings where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    s = (row[0] if row else None) or {}
    return {"dataset_id": s.get("dataset_id") or "", "test_code": s.get("test_code") or "", "enabled": bool(s.get("dataset_id")) and s.get("enabled", True)}


def save_settings(cur, tenant_id, *, dataset_id: str, test_code: str = "", enabled: bool = True) -> dict:
    dataset_id = (dataset_id or "").strip()
    if dataset_id and not DATASET_RE.match(dataset_id):
        raise CapiError("bad_dataset", "The dataset id is the number shown in Meta's Events Manager (digits only).")
    value = {"dataset_id": dataset_id, "test_code": (test_code or "").strip()[:40], "enabled": bool(enabled)}
    cur.execute("insert into tenant_settings (tenant_id, settings) values (%s, jsonb_build_object('capi', %s::jsonb))"
                " on conflict (tenant_id) do update set settings = jsonb_set(tenant_settings.settings, '{capi}', %s::jsonb), updated_at = now()",
                (tenant_id, Json(value), Json(value)))
    return settings(cur, tenant_id)


def _token(cur, tenant_id):
    vault = _CFG["vault"]
    if vault is None:
        raise CapiError("not_set_up", "Sending events to Meta is not set up on this server.")
    cur.execute("select id from wabas where tenant_id = %s order by created_at limit 1", (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise CapiError("no_account", "Connect a WhatsApp number first.")
    try:
        meta_waba, token = templates._waba_token(cur, vault, tenant_id, row[0])
    except LookupError as exc:
        raise CapiError("no_token", str(exc)) from exc
    return meta_waba, token


def _payload(event: str, *, waba: str, clid: str, value=None, currency: str = "INR", now: Optional[float] = None) -> dict:
    custom = {}
    if value not in (None, ""):
        try:
            custom = {"value": float(str(value).replace(",", "")), "currency": currency or "INR"}
        except ValueError:
            raise CapiError("bad_value", "The value %r is not a number." % (value,)) from None
    row = {"event_name": event, "event_time": int(now if now is not None else time.time()), "action_source": "business_messaging",
           "messaging_channel": "whatsapp", "user_data": {"whatsapp_business_account_id": waba, "ctwa_clid": clid}}
    if custom:
        row["custom_data"] = custom
    return row


def send_for_contact(cur, tenant_id, contact_id, *, event: str, value=None, currency: str = "INR", now=None, test_code: str = "") -> dict:
    """One event for one person. Raises CapiError (with the reason in words) if it cannot be sent."""
    if event not in EVENTS:
        raise CapiError("bad_event", "Choose one of: %s." % ", ".join(EVENTS))
    s = settings(cur, tenant_id)
    if not s["enabled"]:
        raise CapiError("off", "Sending events to Meta is switched off (set the dataset id in Settings).")
    cur.execute("select ctwa_clid from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CapiError("unknown_contact", "That contact is not in this workspace.")
    if not row[0]:
        raise CapiError("no_click", "This person did not come from an ad, so there is no ad click to credit.")
    graph = _CFG["graph"]
    if graph is None:
        raise CapiError("not_set_up", "Sending events to Meta is not set up on this server.")
    waba, token = _token(cur, tenant_id)
    body = {"data": [_payload(event, waba=waba, clid=row[0], value=value, currency=currency, now=now.timestamp() if now else None)]}
    code = test_code or s["test_code"]
    if code:
        body["test_event_code"] = code
    try:
        reply = graph.send_conversion_event(token, s["dataset_id"], body)
    except GraphError as exc:
        raise CapiError("meta_refused", "Meta did not accept the event: %s" % (exc.details or exc.message)) from exc
    return {"events_received": int(reply.get("events_received") or 0), "fbtrace_id": reply.get("fbtrace_id")}


def send_test(cur, tenant_id) -> dict:
    """A "Lead" for a made-up click, sent with Meta's test code so it shows in Events Manager → Test events and never counts."""
    s = settings(cur, tenant_id)
    if not s["dataset_id"]:
        raise CapiError("off", "Set the dataset id first.")
    if not s["test_code"]:
        raise CapiError("no_test_code", "Enter the test event code from Events Manager → Test events first.")
    graph = _CFG["graph"]
    if graph is None:
        raise CapiError("not_set_up", "Sending events to Meta is not set up on this server.")
    waba, token = _token(cur, tenant_id)
    body = {"data": [_payload("Lead", waba=waba, clid="prism-test-click")], "test_event_code": s["test_code"]}
    try:
        reply = graph.send_conversion_event(token, s["dataset_id"], body)
    except GraphError as exc:
        raise CapiError("meta_refused", "Meta did not accept the test event: %s" % (exc.details or exc.message)) from exc
    return {"events_received": int(reply.get("events_received") or 0), "fbtrace_id": reply.get("fbtrace_id")}
