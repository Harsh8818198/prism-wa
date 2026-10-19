"""Single sign-on with a company account (OpenID Connect).

An owner enters the provider's address and the app's client id and secret; a team member then signs in on the provider's own page, and Prism learns only who they are (their
e-mail and name), checks the answer is genuine and meant for Prism, matches the e-mail to a team member, optionally takes their role from a claim, and hands the desktop app a
12-hour session token. With "require single sign-on" on, every request from a team member must carry a live token; the workspace owner is exempt, so a mistake in the provider's
settings can never lock the owner out.

Only OpenID Connect with RS256-signed ID tokens is supported. SAML needs an XML-signature library and an agreement with the provider and is not part of this module.
The provider is reached through `HTTP` (the same shape as Graph's transport) so a test can stand one in.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlencode

from psycopg2.extras import Json

from prism_wa import audit, plans
from prism_wa.graph import urllib_transport
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

log = logging.getLogger("prism_wa.sso")
HTTP: Optional[Callable] = None
SESSION_HOURS = 12
STATE_MINUTES = 15
ROLES = ("agent", "manager")
EXEMPT_PREFIXES = ("/internal/sso/",)
EXEMPT_PATHS = ("/internal/inbox/ensure_agent",)


class SsoError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _http(method: str, url: str, form: Optional[dict] = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        status, raw = (HTTP or urllib_transport)(method, url, data, headers)
    except Exception as why:                                              # noqa: BLE001
        raise SsoError("provider_unreachable", "Could not reach the sign-in provider: %s" % why.__class__.__name__) from None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    if status >= 400:
        raise SsoError("provider_refused", "The sign-in provider answered %d%s" % (status, (": " + str(parsed.get("error_description") or parsed.get("error"))) if isinstance(parsed, dict) and (parsed.get("error_description") or parsed.get("error")) else ""))
    return parsed if isinstance(parsed, dict) else {}


# ── settings ─────────────────────────────────────────────────────────────────

COLS = ("enabled", "issuer", "client_id", "client_secret_enc", "authorize_url", "token_url", "jwks_url", "allowed_domains", "role_claim", "role_map", "default_role", "require_sso", "updated_at")


def get_config(cur, tenant_id) -> dict:
    cur.execute("select " + ", ".join(COLS) + " from sso_config where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    c = dict(zip(COLS, row)) if row else {"enabled": False, "issuer": None, "client_id": None, "client_secret_enc": None, "authorize_url": None, "token_url": None, "jwks_url": None,
                                           "allowed_domains": [], "role_claim": None, "role_map": {}, "default_role": "agent", "require_sso": False, "updated_at": None}
    c["client_secret_set"] = bool(c.pop("client_secret_enc"))
    c["ready"] = bool(c["issuer"] and c["client_id"] and c["client_secret_set"])
    return c


def _https(url: str, what: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https://[^\s/]+", url):
        raise SsoError("bad_url", "%s must start with https://" % what)
    return url.rstrip("/") if what == "The issuer" else url


def save_config(cur, tenant_id, vault: Optional[Vault], d: dict) -> dict:
    plans.require(cur, tenant_id, "sso", "Single sign-on")
    cur_cfg = get_config(cur, tenant_id)
    new = dict(cur_cfg)
    for key, what in (("issuer", "The issuer"), ("authorize_url", "The sign-in address"), ("token_url", "The token address"), ("jwks_url", "The keys address")):
        if key in d:
            new[key] = _https(d[key], what) or None
    if "client_id" in d:
        new["client_id"] = (str(d["client_id"] or "").strip()[:200]) or None
    if "allowed_domains" in d:
        domains = []
        for x in d["allowed_domains"] or []:
            x = str(x).strip().lower().lstrip("@")
            if x and not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", x):
                raise SsoError("bad_domain", "%r is not an e-mail domain (like company.com)" % x)
            if x and x not in domains:
                domains.append(x)
        new["allowed_domains"] = domains
    if "role_claim" in d:
        new["role_claim"] = (str(d["role_claim"] or "").strip()[:60]) or None
    if "role_map" in d:
        rm = {}
        for k, v in (d["role_map"] or {}).items():
            if v not in ROLES:
                raise SsoError("bad_role", "A provider group can map to: %s. (Owners are never set by the provider.)" % ", ".join(ROLES))
            rm[str(k)[:120]] = v
        new["role_map"] = rm
    if "default_role" in d:
        if d["default_role"] not in ROLES:
            raise SsoError("bad_role", "The default role is one of: %s." % ", ".join(ROLES))
        new["default_role"] = d["default_role"]
    for flag in ("enabled", "require_sso"):
        if flag in d:
            new[flag] = bool(d[flag])
    secret_blob = None
    secret = (d.get("client_secret") or "").strip() if isinstance(d.get("client_secret"), str) else ""
    if secret:
        if vault is None:
            raise LookupError("this server has no token key, so a secret cannot be stored")
        secret_blob = vault.encrypt(secret, context="sso:%s" % tenant_id)
    if (new["enabled"] or new["require_sso"]) and not (new["issuer"] and new["client_id"] and (cur_cfg["client_secret_set"] or secret_blob)):
        raise SsoError("not_ready", "Add the issuer, client id and client secret before switching single sign-on on.")
    if new["require_sso"] and not new["enabled"]:
        raise SsoError("not_ready", "Switch single sign-on on before requiring it.")
    cols = ["issuer", "client_id", "authorize_url", "token_url", "jwks_url", "role_claim", "default_role", "enabled", "require_sso"]
    vals = [new[c] for c in cols] + [Json(new["allowed_domains"]), Json(new["role_map"])]
    sql_cols = cols + ["allowed_domains", "role_map"]
    if secret_blob:
        sql_cols += ["client_secret_enc", "key_ver"]
        vals += [secret_blob[0], secret_blob[1]]
    cur.execute("insert into sso_config (tenant_id, " + ", ".join(sql_cols) + ") values (%s" + ", %s" * len(sql_cols) + ") on conflict (tenant_id) do update set "
                + ", ".join("%s = excluded.%s" % (c, c) for c in sql_cols) + ", updated_at = now()", [tenant_id] + vals)
    return get_config(cur, tenant_id)


def _secret(cur, tenant_id, vault: Vault) -> str:
    cur.execute("select client_secret_enc from sso_config where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    return vault.decrypt(bytes(row[0]), context="sso:%s" % tenant_id) if row and row[0] else ""


# ── talking to the provider ──────────────────────────────────────────────────

def discover(issuer: str) -> dict:
    doc = _http("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
    need = ("authorization_endpoint", "token_endpoint", "jwks_uri")
    if not all(doc.get(k) for k in need):
        raise SsoError("bad_discovery", "The provider's address did not describe itself as an OpenID Connect provider.")
    return {"authorize_url": doc["authorization_endpoint"], "token_url": doc["token_endpoint"], "jwks_url": doc["jwks_uri"], "issuer": doc.get("issuer") or issuer}


def _endpoints(cur, tenant_id) -> dict:
    cfg = get_config(cur, tenant_id)
    if not cfg["ready"]:
        raise SsoError("not_ready", "Single sign-on is not set up yet.")
    if cfg["authorize_url"] and cfg["token_url"] and cfg["jwks_url"]:
        return {k: cfg[k] for k in ("authorize_url", "token_url", "jwks_url")}
    return discover(cfg["issuer"])


def test(cur, tenant_id, vault: Vault) -> dict:
    """Look up the provider and its signing keys, so the owner learns now, not at 9 on Monday, that the address is wrong."""
    plans.require(cur, tenant_id, "sso", "Single sign-on")
    cfg = get_config(cur, tenant_id)
    if not cfg["issuer"] and not cfg["authorize_url"]:
        raise SsoError("not_ready", "Enter the issuer address first.")
    ep = discover(cfg["issuer"]) if cfg["issuer"] and not (cfg["authorize_url"] and cfg["token_url"] and cfg["jwks_url"]) else {k: cfg[k] for k in ("authorize_url", "token_url", "jwks_url")}
    keys = _http("GET", ep["jwks_url"]).get("keys") or []
    usable = [k for k in keys if k.get("kty") == "RSA"]
    if not usable:
        raise SsoError("no_keys", "The provider published no RSA signing keys, which is what Prism checks sign-ins with.")
    return {"authorize_url": ep["authorize_url"], "token_url": ep["token_url"], "keys": len(usable), "has_secret": cfg["client_secret_set"]}


# ── signing in ───────────────────────────────────────────────────────────────

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def start(cur, tenant_id, redirect_uri: str, now: Optional[datetime] = None) -> dict:
    cfg = get_config(cur, tenant_id)
    if not (cfg["enabled"] and cfg["ready"]):
        raise SsoError("not_enabled", "Single sign-on is not switched on for this workspace.")
    if not redirect_uri.startswith("https://"):
        raise SsoError("not_set_up", "Single sign-on needs this server's public address (PUBLIC_URL).")
    ep = _endpoints(cur, tenant_id)
    now = now or utcnow()
    state, nonce, verifier = "%s.%s" % (tenant_id, secrets.token_urlsafe(18)), secrets.token_urlsafe(18), secrets.token_urlsafe(48)
    cur.execute("insert into sso_states (state, tenant_id, nonce, verifier, redirect_uri, expires_at) values (%s, %s, %s, %s, %s, %s)",
                (state, tenant_id, nonce, verifier, redirect_uri, now + timedelta(minutes=STATE_MINUTES)))
    query = urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri, "scope": "openid email profile", "state": state, "nonce": nonce,
                       "code_challenge": _b64u(hashlib.sha256(verifier.encode("ascii")).digest()), "code_challenge_method": "S256"})
    return {"state": state, "url": ep["authorize_url"] + ("&" if "?" in ep["authorize_url"] else "?") + query}


def tenant_of(state: str) -> str:
    return (state or "").split(".", 1)[0]


def verify_id_token(token: str, jwks: dict, *, issuer: str, audience: str, nonce: str, now: Optional[datetime] = None, leeway: int = 120) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header, claims = json.loads(_b64d(head_b64)), json.loads(_b64d(body_b64))
    except Exception:                                                     # noqa: BLE001
        raise SsoError("bad_token", "The sign-in answer was not a valid token.") from None
    if header.get("alg") != "RS256":
        raise SsoError("unsupported_alg", "The provider signs sign-ins with %r; Prism accepts RS256 only." % header.get("alg"))
    keys = [k for k in jwks.get("keys") or [] if k.get("kty") == "RSA" and (k.get("use") in (None, "sig"))]
    match = [k for k in keys if k.get("kid") == header.get("kid")] or (keys if len(keys) == 1 and not header.get("kid") else [])
    if not match:
        raise SsoError("unknown_key", "The sign-in was signed with a key the provider does not publish.")
    key = rsa.RSAPublicNumbers(int.from_bytes(_b64d(match[0]["e"]), "big"), int.from_bytes(_b64d(match[0]["n"]), "big")).public_key()
    try:
        key.verify(_b64d(sig_b64), (head_b64 + "." + body_b64).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise SsoError("bad_signature", "The sign-in's signature does not match the provider's key.") from None
    ts = (now or utcnow()).timestamp()
    if str(claims.get("iss", "")).rstrip("/") != issuer.rstrip("/"):
        raise SsoError("wrong_issuer", "The sign-in came from a different provider than the one set up.")
    aud = claims.get("aud")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise SsoError("wrong_audience", "The sign-in was not meant for Prism.")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < ts - leeway:
        raise SsoError("expired", "The sign-in has expired. Try again.")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > ts + leeway:
        raise SsoError("bad_token", "The sign-in is dated in the future.")
    if claims.get("nonce") != nonce:
        raise SsoError("bad_nonce", "The sign-in does not belong to this attempt.")
    return claims


def _role_for(cfg: dict, claims: dict) -> str:
    claim = cfg["role_claim"]
    if not claim:
        return ""
    value = claims.get(claim)
    values = value if isinstance(value, list) else [value]
    mapped = [cfg["role_map"].get(str(v)) for v in values if v is not None]
    if "manager" in mapped:
        return "manager"
    return "agent" if "agent" in mapped else cfg["default_role"]


def complete(cur, tenant_id, vault: Vault, *, state: str, code: str, now: Optional[datetime] = None) -> dict:
    """Finish a sign-in the person started. Looks the attempt up by `state`, checks everything, and files the outcome for the app to collect with `status`."""
    now = now or utcnow()
    if tenant_of(state) != str(tenant_id):
        raise SsoError("unknown_state", "That sign-in was not started here, has run out of time, or was already used.")
    cur.execute("select nonce, verifier, redirect_uri, status, expires_at from sso_states where state = %s and tenant_id = %s for update", (state, tenant_id))
    row = cur.fetchone()
    if row is None or row[3] != "waiting" or row[4] <= now:
        raise SsoError("unknown_state", "That sign-in was not started here, has run out of time, or was already used.")
    nonce, verifier, redirect_uri = row[0], row[1], row[2]
    try:
        result = _finish(cur, vault, tenant_id, code, nonce, verifier, redirect_uri, now)
    except SsoError as why:
        cur.execute("update sso_states set status = 'failed', error = %s where state = %s", (str(why), state))
        audit.record(cur, tenant_id, "sso.login_failed", summary="A sign-in with the company account failed: %s" % why.reason, source="sso", now=now)
        return {"status": "failed", "error": str(why), "reason": why.reason}
    cur.execute("update sso_states set status = 'ready', result = %s where state = %s", (Json(result), state))
    return {"status": "ready"}


def _finish(cur, vault: Vault, tenant_id, code: str, nonce: str, verifier: str, redirect_uri: str, now: datetime) -> dict:
    cfg = get_config(cur, tenant_id)
    if not (cfg["enabled"] and cfg["ready"]):
        raise SsoError("not_enabled", "Single sign-on is not switched on for this workspace.")
    ep = _endpoints(cur, tenant_id)
    reply = _http("POST", ep["token_url"], form={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": cfg["client_id"],
                                                    "client_secret": _secret(cur, tenant_id, vault), "code_verifier": verifier})
    if not reply.get("id_token"):
        raise SsoError("no_id_token", "The provider did not send back who signed in.")
    claims = verify_id_token(reply["id_token"], _http("GET", ep["jwks_url"]), issuer=cfg["issuer"] or "", audience=cfg["client_id"], nonce=nonce, now=now)
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is False:
        raise SsoError("no_email", "The provider did not give a verified e-mail address for this person.")
    if cfg["allowed_domains"] and email.rsplit("@", 1)[-1] not in cfg["allowed_domains"]:
        raise SsoError("domain_refused", "Accounts at %s may not sign in to this workspace." % email.rsplit("@", 1)[-1])
    cur.execute("select id, name, role from agents where tenant_id = %s and active and lower(email) = %s", (tenant_id, email))
    agent = cur.fetchone()
    if agent is None:
        raise SsoError("no_such_member", "There is no team member with the e-mail %s. Ask the workspace owner to add it to your profile." % email)
    agent_id, name, role = agent
    wanted = _role_for(cfg, claims)
    if wanted and role != "owner" and wanted != role:
        cur.execute("update agents set role = %s where id = %s", (wanted, agent_id))
        role = wanted
    token = secrets.token_urlsafe(32)
    cur.execute("insert into sso_sessions (token_hash, tenant_id, agent_id, email, expires_at) values (%s, %s, %s, %s, %s)",
                (hashlib.sha256(token.encode("ascii")).hexdigest(), tenant_id, agent_id, email, now + timedelta(hours=SESSION_HOURS)))
    audit.record(cur, tenant_id, "sso.login", summary="%s signed in with the company account" % name, actor_id=agent_id, source="sso", now=now)
    return {"agent_id": str(agent_id), "name": name, "email": email, "role": role, "token": token, "expires_hours": SESSION_HOURS}


def status(cur, tenant_id, state: str, now: Optional[datetime] = None) -> dict:
    cur.execute("select status, result, error, expires_at from sso_states where state = %s and tenant_id = %s for update", (state, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise SsoError("unknown_state", "That sign-in was not started here, or was already used.")
    st, result, error, expires = row
    if st == "waiting":
        return {"status": "expired" if expires <= (now or utcnow()) else "waiting"}
    if st == "failed":
        return {"status": "failed", "error": error}
    if st == "ready":                                                      # the token is handed over once
        cur.execute("update sso_states set status = 'used', result = null where state = %s", (state,))
        return {"status": "ready", "result": result}
    return {"status": "used"}


# ── making it count ──────────────────────────────────────────────────────────

def enforce(cur, tenant_id, data: dict, path: str, *, now: Optional[datetime] = None) -> None:
    """Called before every request that names a team member. With sign-in required, a member needs a live session; the owner and the sign-in routes themselves do not."""
    agent_id = data.get("by_agent_id") or data.get("sso_agent_id")        # the second is set by the licence server for routes that otherwise carry no identity
    if not agent_id or path.startswith(EXEMPT_PREFIXES) or path in EXEMPT_PATHS:
        return
    cur.execute("select 1 from sso_config where tenant_id = %s and enabled and require_sso", (tenant_id,))
    if cur.fetchone() is None:
        return
    cur.execute("select role from agents where id = %s and tenant_id = %s", (agent_id, tenant_id))
    row = cur.fetchone()
    if row is None or row[0] == "owner":
        return
    token = data.get("sso_token")
    if isinstance(token, str) and token:
        cur.execute("select 1 from sso_sessions where token_hash = %s and tenant_id = %s and agent_id = %s and expires_at > %s",
                    (hashlib.sha256(token.encode("utf-8", "replace")).hexdigest(), tenant_id, agent_id, now or utcnow()))
        if cur.fetchone():
            return
    raise SsoError("sso_required", "Your workspace requires signing in with your company account.")


def required(cur, tenant_id) -> bool:
    """Does this workspace make its team sign in with the company account? (The licence server asks, so it can name the caller on every route.)"""
    cur.execute("select 1 from sso_config where tenant_id = %s and enabled and require_sso", (tenant_id,))
    return cur.fetchone() is not None


def purge(cur, tenant_id=None, *, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    cur.execute("delete from sso_sessions where expires_at < %s", (now - timedelta(days=1),))
    n = cur.rowcount
    cur.execute("delete from sso_states where expires_at < %s", (now - timedelta(days=1),))
    return n + cur.rowcount
