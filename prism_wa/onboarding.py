"""Embedded Signup v4, server side (spec WS0).

The customer connects their own WhatsApp number from a page we host. That page
hands us `{code, waba_id, phone_number_id}`; this module turns it into a working
number:

    1. exchange the one-time code for the customer's business token
    2. store the token, encrypted, against their WABA (never plaintext)
    3. register the number with a PIN we generate (kept encrypted)
    4. subscribe our app to the WABA's webhooks
    5. record the WABA and number under the customer's tenant

Every step is safe to repeat, so a failure half-way is fixed by running it again.
A WABA that already belongs to another tenant is refused outright: nobody can
take over a number by replaying someone else's ids.

Who is allowed to onboard whom: the licence server (which knows the seat and
the licence) mints a short-lived signed `state` naming the tenant; the page
carries it, and only a valid unexpired state lets a tenant be written. The
state is HMAC-signed with WA_INTERNAL_TOKEN, a secret the licence server and
this service share.

The page normally hands us the ids; when it cannot (a number connected from the
WhatsApp Business app finishes with an event that may name neither the account
nor the number) we ask Meta with the customer's own token, and only act on an
unambiguous answer: one shared account, one number on it.

NOT yet verified against Meta live: the exact Embedded Signup v4 message the
page receives and the config_id it needs come from the Meta dashboard. The calls
here follow the spec (§1.2) and Meta's coexistence docs, and are tested against a
fake Graph.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as pysecrets
import time
from dataclasses import dataclass
from typing import Optional

from prism_wa.graph import Graph, GraphError
from prism_wa.vault import Vault

STATE_TTL = 30 * 60


class OnboardingError(Exception):
    """The onboarding could not proceed. `reason` is machine-readable."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# ── the signed state that names the tenant ───────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_state(secret: str, tenant_id, *, ttl: int = STATE_TTL, now: Optional[float] = None) -> str:
    body = _b64(json.dumps({"t": str(tenant_id), "exp": int((now if now is not None else time.time()) + ttl)},
                           separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + _b64(sig)


def read_state(secret: str, state: str, *, now: Optional[float] = None) -> str:
    """-> the tenant id, or raises OnboardingError('bad_state')."""
    try:
        body, _, sig = (state or "").partition(".")
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not secret or not hmac.compare_digest(expected, _unb64(sig)):
            raise ValueError("signature")
        claims = json.loads(_unb64(body))
        if float(claims["exp"]) < (now if now is not None else time.time()):
            raise ValueError("expired")
        return str(claims["t"])
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise OnboardingError("bad_state", "this connect link is invalid or has expired") from None


# ── the signup itself ────────────────────────────────────────────────────────

@dataclass
class SignupResult:
    waba_id: str
    phone_number_id: str
    registered: bool
    subscribed: bool
    errors: list


def new_pin() -> str:
    return "%06d" % pysecrets.randbelow(1_000_000)


def _find_what_was_connected(graph: Graph, token: str, app_id: str, app_secret: str, waba_id: str, phone_number_id: str):
    """Connecting a number that already lives in the WhatsApp Business app finishes with an event that may not name the account or the number (Meta sends
    FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING, and its fields are optional). Ask Meta with the customer's own token instead of refusing the signup.
    Only an unambiguous answer is used: one shared account, one number on it."""
    display = ""
    try:
        if not waba_id:
            wabas = graph.shared_wabas(token, app_id, app_secret)
            if not wabas:
                raise OnboardingError("incomplete", "Meta did not say which WhatsApp Business Account was connected; please try again")
            if len(wabas) > 1:
                raise OnboardingError("ambiguous", "more than one WhatsApp Business Account was shared; connect again and choose just one")
            waba_id = wabas[0]
        if not phone_number_id:
            numbers = graph.waba_numbers(token, waba_id)
            if not numbers:
                raise OnboardingError("incomplete", "that WhatsApp Business Account has no number yet; please try again in a minute")
            if len(numbers) > 1:
                raise OnboardingError("ambiguous", "that WhatsApp Business Account has %d numbers and the page did not say which one you just connected" % len(numbers))
            phone_number_id = str(numbers[0]["id"])
            display = str(numbers[0].get("display_phone_number") or "")
    except GraphError as exc:
        raise OnboardingError("incomplete", "could not find the connected number: %s" % exc.message) from exc
    return waba_id, phone_number_id, display


def complete_signup(conn, *, graph: Graph, vault: Vault, tenant_id, app_id: str, app_secret: str,
                    code: str, waba_id: str, phone_number_id: str, display: str = "",
                    override_callback_uri: str = "", verify_token: str = "", coexistence: bool = False) -> SignupResult:
    if not code:
        raise OnboardingError("incomplete", "the signup is missing its code")
    try:
        token = graph.exchange_code(app_id, app_secret, code)
    except GraphError as exc:
        raise OnboardingError("code_rejected", "Meta did not accept that signup code: %s" % exc.message) from exc
    if not (waba_id and phone_number_id):
        waba_id, phone_number_id, found = _find_what_was_connected(graph, token, app_id, app_secret, waba_id, phone_number_id)
        display = display or found

    token_enc, key_ver = vault.encrypt(token, context=waba_id)
    errors = []
    try:
        with conn.cursor() as cur:
            cur.execute("select id, tenant_id from wabas where waba_id = %s", (waba_id,))
            row = cur.fetchone()
            if row and str(row[1]) != str(tenant_id):
                raise OnboardingError("waba_taken", "that WhatsApp Business Account belongs to another workspace")
            if row:
                waba_row = row[0]
                cur.execute("update wabas set token_enc = %s, key_ver = %s where id = %s",
                            (token_enc, key_ver, waba_row))
            else:
                cur.execute("insert into wabas (tenant_id, waba_id, token_enc, key_ver, webhook_override)"
                            " values (%s, %s, %s, %s, %s) returning id",
                            (tenant_id, waba_id, token_enc, key_ver, override_callback_uri or None))
                waba_row = cur.fetchone()[0]

            cur.execute("select id, tenant_id, pin_enc from phone_numbers where phone_number_id = %s",
                        (phone_number_id,))
            row = cur.fetchone()
            if row and str(row[1]) != str(tenant_id):
                raise OnboardingError("number_taken", "that number belongs to another workspace")
            if row:
                number_row, pin_enc = row[0], row[2]
            else:
                cur.execute("insert into phone_numbers (tenant_id, waba_id, phone_number_id, display, status)"
                            " values (%s, %s, %s, %s, 'connecting') returning id",
                            (tenant_id, waba_row, phone_number_id, display or None))
                number_row, pin_enc = cur.fetchone()[0], None

            # Reuse the PIN from an earlier attempt: Meta rejects a re-register with a different one.
            pin = vault.decrypt(pin_enc, context=phone_number_id) if pin_enc else new_pin()
            if not pin_enc:
                blob, _ = vault.encrypt(pin, context=phone_number_id)
                cur.execute("update phone_numbers set pin_enc = %s where id = %s", (blob, number_row))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    registered = subscribed = False
    if coexistence:                                         # the number already lives in the WhatsApp Business app on the phone: Meta connects it, we do not register it
        registered = True
    else:
        try:
            graph.register_number(token, phone_number_id, pin)
            registered = True
        except GraphError as exc:
            errors.append({"step": "register", **exc.as_dict()})
    try:
        graph.subscribe_app(token, waba_id, override_callback_uri=override_callback_uri,
                            verify_token=verify_token)
        subscribed = True
    except GraphError as exc:
        errors.append({"step": "subscribe", **exc.as_dict()})

    with conn.cursor() as cur:
        cur.execute("update phone_numbers set status = %s, registered_at = case when %s then now()"
                    " else registered_at end where id = %s",
                    ("connected" if registered else "needs_attention", registered, number_row))
        if coexistence:
            cur.execute("update phone_numbers set coexistence = true, coexistence_state = coalesce(coexistence_state, '{}'::jsonb) where id = %s", (number_row,))
        cur.execute("update wabas set subscribed_at = case when %s then now() else subscribed_at end"
                    " where id = %s", (subscribed, waba_row))
    conn.commit()
    if coexistence and subscribed:                          # Meta only sends the phone's contacts and chats in the first 24 hours: ask now
        from prism_wa import coexistence as _co
        try:
            with conn.cursor() as cur:
                out = _co.request_sync(cur, tenant_id, graph, vault, number_row)
            conn.commit()
            for key, answer in out.items():
                if answer != "requested":
                    errors.append({"step": "sync_" + key, "message": answer})
        except Exception as why:                            # noqa: BLE001  (connecting worked; the owner can ask again from Settings)
            conn.rollback()
            errors.append({"step": "sync", "message": str(why)})
    return SignupResult(waba_id, phone_number_id, registered, subscribed, errors)


# ── the hosted page ──────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect WhatsApp</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}
button{font-size:1rem;padding:.7rem 1.2rem;border:0;border-radius:.5rem;background:#1a7f4b;color:#fff;cursor:pointer}
#msg{margin-top:1.2rem;white-space:pre-wrap}</style></head>
<body><h1>Connect your WhatsApp number</h1>
<p>You will sign in to Facebook and choose the WhatsApp Business Account and number to connect.</p>
<button id="go" type="button">Connect with Facebook</button><div id="msg" role="status"></div>
<script>
const CFG = __CONFIG__;
let session = {};
let stoppedAt = '';
const say = t => { document.getElementById('msg').textContent = t; };
window.fbAsyncInit = function () {
  FB.init({ appId: CFG.appId, autoLogAppEvents: true, xfbml: false, version: CFG.version });
};
window.addEventListener('message', function (e) {
  if (!/facebook\\.com$/.test(new URL(e.origin).hostname)) return;
  try {
    const data = JSON.parse(e.data);
    if (data.type !== 'WA_EMBEDDED_SIGNUP') return;
    const d = data.data || {};
    // FINISH: a new number. FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING: a number from the WhatsApp Business app (it may name neither account nor number: the server then asks Meta).
    if (data.event === 'FINISH' || data.event === 'FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING') {
      session = { waba_id: d.waba_id || session.waba_id, phone_number_id: d.phone_number_id || session.phone_number_id };
    } else if (data.event === 'CANCEL') {
      stoppedAt = (d.error_message ? d.error_message + ' ' : '') + (d.current_step ? '(at the "' + d.current_step + '" step)' : '');
    }
  } catch (_) {}
});
document.getElementById('go').addEventListener('click', function () {
  FB.login(function (r) {
    if (!r.authResponse || !r.authResponse.code) { say(('Connecting was cancelled. ' + stoppedAt).trim()); return; }
    say('Finishing setup...');
    fetch('/onboarding/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: CFG.state, code: r.authResponse.code, coexistence: CFG.coexistence, ...session }) })
      .then(x => x.json()).then(j => say(j.ok ? 'Connected. You can close this window.'
                                              : 'Could not finish: ' + (j.message || j.error)))
      .catch(() => say('Could not reach the server. Try again.'));
  }, { config_id: CFG.configId, response_type: 'code', override_default_response_type: true,
       extras: CFG.coexistence ? { setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3' }
                               : { setup: {}, sessionInfoVersion: '3' } });
});
</script>
<script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script>
</body></html>
"""


def render_page(*, app_id: str, config_id: str, state: str, version: str, coexistence: bool = False) -> str:
    cfg = json.dumps({"appId": app_id, "configId": config_id, "state": state, "version": version, "coexistence": bool(coexistence)})
    # `<` is escaped so no value can close the script tag
    return PAGE.replace("__CONFIG__", cfg.replace("<", "\\u003c"))
