"""Connecting an ad account by signing in with Facebook, instead of pasting an access token.

    start()     the Facebook sign-in address to open, with a one-time `state` that carries the workspace and expires in 30 minutes
    finish()    Meta sends the person back with a `code`: trade it for a token, keep the token encrypted, and list the ad accounts and Pages that person can use
    options()   what the person may choose from (never the token)
    choose()    the account and Page they picked become a connected ad account, exactly as if the token had been pasted

The state looks like "<workspace id>.<random>" so the service that receives Meta's redirect can hand the code to the right workspace without a login. A state that is
expired, used, or not found is refused. The token Meta gives is long-lived (about 60 days); when it stops working the account shows the problem and is signed in again.

Needs the Meta app's id and secret (META_APP_ID, META_APP_SECRET) and the redirect address registered in the app; the permissions asked for (ads_management, ads_read,
pages_show_list, pages_read_engagement, pages_manage_ads, business_management) need Advanced Access from Meta's app review. Tested against a fake Graph server.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Optional
from urllib.parse import urlencode

from psycopg2.extras import Json

from prism_wa import ads
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

SCOPES = ("ads_management", "ads_read", "business_management", "pages_show_list", "pages_read_engagement", "pages_manage_ads", "leads_retrieval")
DIALOG = "https://www.facebook.com/v25.0/dialog/oauth"
LIFE = timedelta(minutes=30)


def _state_id(tenant_id) -> str:
    return "%s.%s" % (tenant_id, secrets.token_urlsafe(24))


def tenant_of(state: str) -> Optional[str]:
    m = re.match(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[A-Za-z0-9_-]{20,}$", state or "")
    return m.group(1) if m else None


def start(cur, tenant_id, *, app_id: str, redirect_uri: str) -> dict:
    if not app_id:
        raise ads.AdsError("not_set_up", "Signing in with Facebook is not set up on this server yet (the Meta app id is missing). Paste an access token instead.")
    if not redirect_uri.startswith("https://"):
        raise ads.AdsError("not_set_up", "Signing in with Facebook needs a secure redirect address.")
    state = _state_id(tenant_id)
    cur.execute("delete from ad_connect_states where tenant_id = %s and (expires_at < now() or status in ('done', 'failed'))", (tenant_id,))
    cur.execute("insert into ad_connect_states (state, tenant_id, redirect_uri) values (%s, %s, %s)", (state, tenant_id, redirect_uri))
    return {"state": state, "url": "%s?%s" % (DIALOG, urlencode({"client_id": app_id, "redirect_uri": redirect_uri, "state": state, "scope": ",".join(SCOPES), "response_type": "code"}))}


def _row(cur, tenant_id, state: str, *, lock: bool = False) -> dict:
    cur.execute("select state, redirect_uri, status, token_enc, key_ver, options, error, expires_at from ad_connect_states where state = %s and tenant_id = %s" + (" for update" if lock else ""),
                (state, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ads.AdsError("unknown_state", "that sign-in was not started here, or it was already used")
    d = dict(zip(("state", "redirect_uri", "status", "token_enc", "key_ver", "options", "error", "expires_at"), row))
    if d["expires_at"] < utcnow():
        raise ads.AdsError("expired", "that sign-in took too long. Start it again.")
    return d


def finish(cur, tenant_id, graph: Graph, vault: Vault, *, state: str, code: str, app_id: str, app_secret: str) -> dict:
    """Meta's redirect arrived. -> {"status": "ready" | "failed"}; the person's screen reads the outcome with options()."""
    st = _row(cur, tenant_id, state, lock=True)
    if st["status"] != "waiting":
        raise ads.AdsError("wrong_state", "that sign-in was already finished")
    try:
        short = graph._call("GET", "oauth/access_token", query={"client_id": app_id, "client_secret": app_secret, "redirect_uri": st["redirect_uri"], "code": code})
        long_ = graph._call("GET", "oauth/access_token", query={"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret, "fb_exchange_token": short["access_token"]})
        token = long_.get("access_token") or short["access_token"]
        me = graph._call("GET", "me", token=token, query={"fields": "name"})
        accounts = graph._call("GET", "me/adaccounts", token=token, query={"fields": "account_id,name,currency,account_status", "limit": 50}).get("data") or []
        pages = graph._call("GET", "me/accounts", token=token, query={"fields": "id,name", "limit": 50}).get("data") or []
    except (GraphError, KeyError) as exc:
        message = "Meta did not let this sign-in through: %s" % (getattr(exc, "details", "") or getattr(exc, "message", "") or exc)
        cur.execute("update ad_connect_states set status = 'failed', error = %s where state = %s", (message[:300], state))
        return {"status": "failed", "error": message}
    blob, ver = vault.encrypt(token, context="connect:" + state[:36])
    options = {"user": me.get("name") or "", "accounts": [{"id": str(a.get("account_id")), "name": a.get("name") or "", "currency": a.get("currency") or ""} for a in accounts if a.get("account_id")],
               "pages": [{"id": str(p.get("id")), "name": p.get("name") or ""} for p in pages if p.get("id")]}
    cur.execute("update ad_connect_states set status = 'ready', token_enc = %s, key_ver = %s, options = %s where state = %s", (blob, ver, Json(options), state))
    return {"status": "ready"}


def status(cur, tenant_id, state: str) -> dict:
    """What the person's screen polls: waiting, ready (with the choices), or failed (with why). Never the token."""
    st = _row(cur, tenant_id, state)
    return {"status": st["status"], "error": st["error"], "options": st["options"] if st["status"] == "ready" else None}


def choose(cur, tenant_id, vault: Vault, *, state: str, ad_account_id: str, page_id: str, name: str = "") -> str:
    st = _row(cur, tenant_id, state, lock=True)
    if st["status"] != "ready":
        raise ads.AdsError("wrong_state", "that sign-in is not ready to choose from")
    options = st["options"] or {}
    ad_account_id = re.sub(r"^act_", "", ad_account_id or "")
    account = next((a for a in options.get("accounts") or [] if a["id"] == ad_account_id), None)
    page = next((p for p in options.get("pages") or [] if p["id"] == str(page_id)), None)
    if account is None or page is None:
        raise ads.AdsError("bad_choice", "choose one of the ad accounts and Pages Meta showed you")
    token = vault.decrypt(bytes(st["token_enc"]), context="connect:" + state[:36])
    account_row = ads.connect_account(cur, tenant_id, vault, ad_account_id=ad_account_id, page_id=str(page_id), token=token, name=name or account["name"])
    cur.execute("update ad_accounts set connected_via = 'facebook', currency = %s where id = %s", (account.get("currency") or None, account_row))
    cur.execute("update ad_connect_states set status = 'done', token_enc = null where state = %s", (state,))
    return account_row
