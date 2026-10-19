"""The enterprise and reseller side: the audit log, single sign-on (against a fake OpenID Connect provider that really signs tokens), branding, resellers and commission, and
coexistence with the WhatsApp Business app — against a real Postgres."""
import base64
import hashlib
import json
import os
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import helpers as H
import payloads as P
import psycopg2
import pytest
from fastapi.testclient import TestClient

from prism_wa import audit, coexistence, inbox, onboarding, partner, sso
from prism_wa.app import create_app
from prism_wa.events import normalise
from prism_wa.graph import Graph, GraphError
from prism_wa.jobs import utcnow
from prism_wa.vault import Vault

ISSUER = "https://login.shakti.example"
CLIENT = "prism-client-id"
REDIRECT = "https://licence.example/v1/whatsapp/sso/callback"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Provider:
    """A fake OpenID Connect provider with a real RSA key: discovery, keys, and a token endpoint that signs what it is told to."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls, self.claims, self.alg, self.status = [], {}, "RS256", 200
        self.sign_with = self.key

    def jwks(self):
        n = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": b64u(n.n.to_bytes(256, "big")), "e": b64u(n.e.to_bytes(3, "big"))}]}

    def token(self, nonce, **over):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        claims = {"iss": ISSUER, "aud": CLIENT, "sub": "u1", "exp": int(time.time()) + 600, "iat": int(time.time()), "nonce": nonce, "email": "asha@shakti.example",
                  "email_verified": True, "name": "Asha Shah", **self.claims, **over}
        head, body = b64u(json.dumps({"alg": self.alg, "kid": "k1"}).encode()), b64u(json.dumps(claims).encode())
        sig = self.sign_with.sign((head + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        return head + "." + body + "." + b64u(sig)

    nonce = ""

    def __call__(self, method, url, data, headers):
        self.calls.append((method, url, data))
        if url.endswith("/.well-known/openid-configuration"):
            return 200, json.dumps({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/keys"}).encode()
        if url.endswith("/keys"):
            return 200, json.dumps(self.jwks()).encode()
        if url.endswith("/token"):
            if self.status != 200:
                return self.status, json.dumps({"error": "invalid_grant", "error_description": "code expired"}).encode()
            return 200, json.dumps({"id_token": self.token(self.nonce), "access_token": "x"}).encode()
        return 404, b"{}"


class FakeMeta:
    def __init__(self):
        self.calls, self.refuse = [], None

    def __call__(self, method, url, data, headers):
        parts = urlsplit(url)
        path = parts.path.split("/v25.0/", 1)[-1]
        body = json.loads(data) if data and headers.get("Content-Type", "").startswith("application/json") else None
        self.calls.append({"method": method, "path": path, "body": body})
        if path.endswith("/smb_app_data") and self.refuse:
            return 400, json.dumps({"error": {"message": "Not allowed", "code": 100, "error_user_msg": self.refuse}}).encode()
        if path == "oauth/access_token":
            return 200, json.dumps({"access_token": "EAAB-token-from-signup"}).encode()
        return 200, json.dumps({"success": True}).encode()


@pytest.fixture
def world(migrated):
    vault = Vault({1: os.urandom(32)})
    tid, number = H.onboard(migrated)
    migrated.q("insert into tenant_settings (tenant_id, settings) values (%s, '{\"plan\": \"enterprise\"}')", (tid,))
    blob, ver = vault.encrypt("EAAB-business-token-for-the-waba", context=P.WABA)
    migrated.q("update wabas set token_enc = %s, key_ver = %s", (psycopg2.Binary(blob), ver))
    idp, meta = Provider(), FakeMeta()
    sso.HTTP = idp
    graph = Graph(meta)
    app = create_app(app_secrets="s", verify_token="v", connect=lambda: psycopg2.connect(migrated.dsn), internal_token="tok", graph=graph, vault=vault)
    client = TestClient(app)

    class W:
        db, tid_, vault_, idp_, meta_, graph_, number_ = migrated, tid, vault, idp, meta, graph, number

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
        def team(cls):
            owner = cls.do(inbox.ensure_agent, "seat-owner", "Meena Owner")
            asha = cls.do(inbox.ensure_agent, "seat-asha", "Asha Shah")
            cls.do(inbox.update_agent, owner["id"], email="meena@shakti.example")
            cls.do(inbox.update_agent, asha["id"], email="asha@shakti.example")
            return owner, asha

        @classmethod
        def configure_sso(cls, **kw):
            base = {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "client-secret-value", "enabled": True}
            base.update(kw)
            return cls.do(sso.save_config, vault, base)

        @classmethod
        def sign_in(cls, **claim_overrides):
            """Run a whole sign-in as Asha: start, 'the provider redirects back', complete, collect."""
            c = cls.conn()
            with c.cursor() as cur:
                started = sso.start(cur, tid, REDIRECT)
                cur.execute("select nonce from sso_states where state = %s", (started["state"],))
                idp.nonce = cur.fetchone()[0]
            c.commit()
            c.close()
            idp.claims = claim_overrides
            done = cls.do(sso.complete, vault, state=started["state"], code="auth-code")
            return started, done

    yield W
    sso.HTTP = None


# ── the audit log ────────────────────────────────────────────────────────────

def test_a_change_leaves_a_line_with_who_what_and_to_which_thing_and_no_secrets(world):
    owner, asha = world.team()
    r = world.post("/internal/attributes/define", key="Region", type="string", by_agent_id=str(owner["id"]))
    assert r.status_code == 200
    got = world.do(audit.list_entries)
    line = got["entries"][0]
    assert line["action"] == "contacts.attribute_define" and line["actor"] == "Meena Owner (owner)" and line["target_kind"] == "attribute" and "Region" in line["summary"]
    assert line["source"] == "app"


def test_reading_leaves_no_line_and_a_refused_change_leaves_none_either(world):
    owner, _ = world.team()
    world.post("/internal/attributes/list", by_agent_id=str(owner["id"]))
    world.post("/internal/attributes/define", key="1 bad key", by_agent_id=str(owner["id"]))
    assert world.do(audit.list_entries)["total"] == 0


def test_the_log_is_filtered_searched_and_downloaded(world):
    owner, asha = world.team()
    for key in ("Alpha", "Beta"):
        world.post("/internal/attributes/define", key=key, type="string", by_agent_id=str(owner["id"]))
    world.post("/internal/tags/delete", name="old", by_agent_id=str(owner["id"]))
    assert world.do(audit.list_entries)["total"] == 3
    assert world.do(audit.list_entries, action="contacts.attribute")["total"] == 2
    assert world.do(audit.list_entries, actor="meena")["total"] == 3 and world.do(audit.list_entries, actor="nobody")["total"] == 0
    assert world.do(audit.list_entries, q="Beta")["total"] == 1
    text = world.do(audit.export_csv)
    assert text.splitlines()[0] == "when,who,through,action,kind,target,what happened" and "contacts.attribute_define" in text
    assert "contacts" in world.do(audit.list_entries)["areas"]


def test_a_line_cannot_run_as_a_formula_in_a_spreadsheet(world):
    world.do(audit.record, "x.test", summary="=HYPERLINK(\"http://evil\")", actor_label="+cmd")
    text = world.do(audit.export_csv)
    assert "'=HYPERLINK" in text and "'+cmd" in text


def test_only_the_enterprise_plan_can_read_the_log_but_every_plan_keeps_it(world):
    owner, _ = world.team()
    world.db.q("update tenant_settings set settings = '{\"plan\": \"premium\"}'")
    world.post("/internal/tags/delete", name="x", by_agent_id=str(owner["id"]))
    assert world.db.one("select count(*) from audit_log") == 1
    r = world.post("/internal/audit/list", by_agent_id=str(owner["id"]))
    assert r.status_code == 403 and r.json()["needs"] == "enterprise"


def test_only_owners_and_managers_read_the_log(world):
    _, asha = world.team()
    r = world.post("/internal/audit/list", by_agent_id=str(asha["id"]))
    assert r.status_code == 409 and r.json()["error"] == "not_allowed"


def test_old_lines_are_dropped_after_400_days(world):
    world.do(audit.record, "old.one", summary="old", now=utcnow() - timedelta(days=401))
    world.do(audit.record, "new.one", summary="new", now=utcnow() - timedelta(days=399))
    assert world.do(audit.purge) == 1 and [e["action"] for e in world.do(audit.list_entries)["entries"]] == ["new.one"]


def test_a_receipt_that_cannot_be_filed_never_breaks_the_change(world):
    def broken():
        raise RuntimeError("database away")
    audit.log_route(broken, world.tid_, "/internal/tags/delete", {"name": "x"}, {})              # (must simply return)


# ── single sign-on: settings ─────────────────────────────────────────────────

def test_the_settings_are_saved_the_secret_encrypted_and_never_shown(world):
    world.team()
    cfg = world.configure_sso(allowed_domains=["Shakti.example", "@shakti.example"], role_claim="groups", role_map={"admins": "manager"}, default_role="agent")
    assert cfg["enabled"] and cfg["ready"] and cfg["client_secret_set"] and cfg["allowed_domains"] == ["shakti.example"] and cfg["role_map"] == {"admins": "manager"}
    assert "client-secret-value" not in json.dumps(cfg, default=str)
    assert b"client-secret-value" not in bytes(world.db.one("select client_secret_enc from sso_config"))
    assert world.do(sso.save_config, world.vault_, {"default_role": "manager"})["client_secret_set"]


@pytest.mark.parametrize("change,reason", [({"issuer": "http://insecure.example"}, "bad_url"), ({"allowed_domains": ["not a domain"]}, "bad_domain"), ({"role_map": {"x": "owner"}}, "bad_role"),
                                           ({"default_role": "owner"}, "bad_role"), ({"client_secret": "", "client_id": ""}, "not_ready")])
def test_settings_are_checked_in_words(world, change, reason):
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.save_config, world.vault_, {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "s", "enabled": True, **change} if reason != "not_ready" else {"enabled": True, **change})
    assert why.value.reason == reason and str(why.value)


def test_requiring_sign_in_needs_it_switched_on_and_single_sign_on_is_an_enterprise_feature(world):
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.save_config, world.vault_, {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "x", "enabled": False, "require_sso": True})
    assert why.value.reason == "not_ready"
    world.db.q("update tenant_settings set settings = '{\"plan\": \"premium\"}'")
    from prism_wa import plans
    with pytest.raises(plans.PlanLimit):
        world.configure_sso()


def test_testing_the_provider_finds_its_endpoints_and_keys(world):
    world.team()
    world.configure_sso()
    out = world.do(sso.test, world.vault_)
    assert out["authorize_url"] == ISSUER + "/authorize" and out["keys"] == 1 and out["has_secret"]


def test_a_provider_that_is_not_openid_connect_is_explained(world):
    world.configure_sso(issuer="https://not-a-provider.example")
    sso.HTTP = lambda method, url, data, headers: (200, b"{}")
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.test, world.vault_)
    assert why.value.reason == "bad_discovery"


# ── single sign-on: signing in ───────────────────────────────────────────────

def test_starting_builds_the_providers_address_with_a_one_time_proof_key(world):
    world.team()
    world.configure_sso()
    s = world.do(sso.start, REDIRECT)
    q = {k: v[0] for k, v in parse_qs(urlsplit(s["url"]).query).items()}
    assert s["url"].startswith(ISSUER + "/authorize?") and q["client_id"] == CLIENT and q["redirect_uri"] == REDIRECT and q["scope"] == "openid email profile"
    assert q["state"] == s["state"] and s["state"].startswith(str(world.tid_) + ".") and q["code_challenge_method"] == "S256" and q["nonce"]
    verifier = world.db.one("select verifier from sso_states where state = %s", (s["state"],))
    assert q["code_challenge"] == b64u(hashlib.sha256(verifier.encode()).digest())


def test_starting_needs_sign_in_switched_on_and_a_public_address(world):
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.start, REDIRECT)
    assert why.value.reason == "not_enabled"
    world.configure_sso()
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.start, "http://localhost/callback")
    assert why.value.reason == "not_set_up"


def test_a_person_signs_in_is_matched_by_e_mail_and_gets_a_token_once(world):
    owner, asha = world.team()
    world.configure_sso()
    started, done = world.sign_in()
    assert done == {"status": "ready"}
    got = world.do(sso.status, started["state"])
    assert got["status"] == "ready" and got["result"]["agent_id"] == str(asha["id"]) and got["result"]["email"] == "asha@shakti.example" and got["result"]["token"]
    token = got["result"]["token"]
    assert world.db.one("select count(*) from sso_sessions") == 1 and world.db.one("select token_hash from sso_sessions") == hashlib.sha256(token.encode()).hexdigest()
    assert world.do(sso.status, started["state"])["status"] == "used" and "result" not in world.do(sso.status, started["state"])
    assert any(e["action"] == "sso.login" and e["source"] == "sso" for e in world.do(audit.list_entries)["entries"])


def test_the_provider_was_sent_the_proof_key_and_the_secret_in_the_token_request(world):
    world.team()
    world.configure_sso()
    started, _ = world.sign_in()
    sent = [d for m, u, d in world.idp_.calls if u.endswith("/token")][0].decode()
    form = {k: v[0] for k, v in parse_qs(sent).items()}
    verifier = world.db.one("select verifier from sso_states where state = %s", (started["state"],))
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code" and form["client_secret"] == "client-secret-value" and form["code_verifier"] == verifier


@pytest.mark.parametrize("claims,reason", [
    ({"iss": "https://evil.example"}, "wrong_issuer"), ({"aud": "someone-elses-app"}, "wrong_audience"), ({"exp": 1}, "expired"), ({"nonce": "not-ours"}, "bad_nonce"),
    ({"email_verified": False}, "no_email"), ({"email": ""}, "no_email"), ({"email": "stranger@shakti.example"}, "no_such_member"), ({"iat": 9999999999}, "bad_token"),
])
def test_a_sign_in_that_is_not_right_is_refused_in_words(world, claims, reason):
    world.team()
    world.configure_sso()
    started, done = world.sign_in(**claims)
    assert done["status"] == "failed" and done["reason"] == reason and done["error"]
    assert world.do(sso.status, started["state"])["status"] == "failed" and world.db.one("select count(*) from sso_sessions") == 0
    assert any(e["action"] == "sso.login_failed" for e in world.do(audit.list_entries)["entries"])


def test_a_token_signed_by_someone_elses_key_or_a_weaker_algorithm_is_refused(world):
    from cryptography.hazmat.primitives.asymmetric import rsa
    world.team()
    world.configure_sso()
    world.idp_.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert world.sign_in()[1]["reason"] == "bad_signature"
    world.idp_.sign_with = world.idp_.key
    world.idp_.alg = "none"
    assert world.sign_in()[1]["reason"] == "unsupported_alg"


def test_only_the_allowed_company_domains_may_sign_in(world):
    world.team()
    world.configure_sso(allowed_domains=["other.example"])
    assert world.sign_in()[1]["reason"] == "domain_refused"


def test_a_sign_in_can_be_finished_only_once_and_only_in_time(world):
    world.team()
    world.configure_sso()
    started, _ = world.sign_in()
    with pytest.raises(sso.SsoError) as why:
        world.do(sso.complete, world.vault_, state=started["state"], code="again")
    assert why.value.reason == "unknown_state"
    with pytest.raises(sso.SsoError):
        world.do(sso.complete, world.vault_, state=str(world.tid_) + ".made-up", code="x")
    s2 = world.do(sso.start, REDIRECT)
    world.db.q("update sso_states set expires_at = now() - interval '1 minute' where state = %s", (s2["state"],))
    assert world.do(sso.status, s2["state"])["status"] == "expired"


def test_the_provider_refusing_the_code_is_reported_not_crashed(world):
    world.team()
    world.configure_sso()
    world.idp_.status = 400
    c = world.conn()
    with c.cursor() as cur:
        started = sso.start(cur, world.tid_, REDIRECT)
    c.commit()
    c.close()
    done = world.do(sso.complete, world.vault_, state=started["state"], code="old")
    assert done["status"] == "failed" and done["reason"] == "provider_refused" and "code expired" in done["error"]
    assert world.do(sso.status, started["state"])["status"] == "failed"


def test_the_role_follows_a_provider_group_but_never_makes_or_changes_an_owner(world):
    owner, asha = world.team()
    world.configure_sso(role_claim="groups", role_map={"leads": "manager"}, default_role="agent")
    world.sign_in(groups=["staff", "leads"])
    assert world.db.one("select role from agents where id = %s", (asha["id"],)) == "manager"
    world.sign_in(groups=["staff"])
    assert world.db.one("select role from agents where id = %s", (asha["id"],)) == "agent"
    world.sign_in(groups=["leads"], email="meena@shakti.example")
    assert world.db.one("select role from agents where id = %s", (owner["id"],)) == "owner"


# ── single sign-on: making it count ──────────────────────────────────────────

def test_with_sign_in_required_a_member_needs_a_live_session_but_the_owner_and_sign_in_routes_do_not(world):
    owner, asha = world.team()
    world.configure_sso(require_sso=True)
    refused = world.post("/internal/attributes/list", by_agent_id=str(asha["id"]))
    assert refused.status_code == 401 and refused.json()["error"] == "sso_required"
    assert world.post("/internal/attributes/list", by_agent_id=str(owner["id"])).status_code == 200
    assert world.post("/internal/sso/status", by_agent_id=str(asha["id"]), state="x").status_code == 404                # reached the route: not turned away
    assert world.post("/internal/inbox/ensure_agent", by_agent_id=str(asha["id"]), seat_key="seat-asha", name="Asha Shah").status_code == 200
    started, _ = world.sign_in()
    token = world.do(sso.status, started["state"])["result"]["token"]
    assert world.post("/internal/attributes/list", by_agent_id=str(asha["id"]), sso_token=token).status_code == 200


def test_one_persons_token_does_not_work_for_another_and_an_ended_session_does_not_work(world):
    owner, asha = world.team()
    ravi = world.do(inbox.ensure_agent, "seat-ravi", "Ravi")
    world.configure_sso(require_sso=True)
    started, _ = world.sign_in()
    token = world.do(sso.status, started["state"])["result"]["token"]
    assert world.post("/internal/attributes/list", by_agent_id=str(ravi["id"]), sso_token=token).status_code == 401
    world.db.q("update sso_sessions set expires_at = now() - interval '1 minute'")
    assert world.post("/internal/attributes/list", by_agent_id=str(asha["id"]), sso_token=token).status_code == 401
    assert world.post("/internal/attributes/list", by_agent_id=str(asha["id"]), sso_token="garbage").status_code == 401


def test_without_the_requirement_nothing_changes(world):
    owner, asha = world.team()
    world.configure_sso()
    assert world.post("/internal/attributes/list", by_agent_id=str(asha["id"])).status_code == 200


def test_only_the_owner_changes_the_sign_in_settings(world):
    owner, asha = world.team()
    r = world.post("/internal/sso/save", by_agent_id=str(asha["id"]), issuer=ISSUER, client_id=CLIENT, client_secret="x")
    assert r.status_code == 409 and r.json()["error"] == "not_allowed"
    ok = world.post("/internal/sso/save", by_agent_id=str(owner["id"]), issuer=ISSUER, client_id=CLIENT, client_secret="TOPSECRET123", enabled=True)
    assert ok.status_code == 200 and "TOPSECRET123" not in ok.text and any(e["action"] == "sso.settings" for e in world.do(audit.list_entries)["entries"])


def test_a_team_members_e_mail_is_checked_and_unique(world):
    owner, asha = world.team()
    for bad in ("nope", "a@b"):
        with pytest.raises(inbox.InboxError) as why:
            world.do(inbox.update_agent, asha["id"], email=bad)
        assert why.value.reason == "bad_email"
    with pytest.raises(inbox.InboxError) as why:
        world.do(inbox.update_agent, asha["id"], email="MEENA@shakti.example")
    assert why.value.reason == "email_taken"
    assert world.do(inbox.update_agent, asha["id"], email="")["email"] is None


# ── branding and resellers ───────────────────────────────────────────────────

def other_workspace(world, slug="customer", plan="unlimited"):
    tid = world.db.one("insert into tenants (slug, name) values (%s, %s) returning id", (slug, slug.title() + " Traders"))
    world.db.q("insert into tenant_settings (tenant_id, settings) values (%s, %s)", (tid, json.dumps({"plan": plan})))
    return tid


def as_tenant(world, tid, fn, *a, **kw):
    c = world.conn()
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


def test_branding_is_saved_checked_and_a_paid_feature(world):
    b = world.do(partner.save_branding, {"name": "BuildDesk", "colour": "#1a7f4b", "support_email": "help@builddesk.example", "support_url": "https://builddesk.example/help",
                                         "custom_domain": "Chat.BuildDesk.example", "hide_vendor": True})
    assert b["name"] == "BuildDesk" and b["colour"] == "#1A7F4B" and b["custom_domain"] == "chat.builddesk.example" and b["hide_vendor"] is True
    for bad, reason in (({"colour": "green"}, "bad_colour"), ({"support_email": "x"}, "bad_email"), ({"support_url": "http://x.example"}, "bad_url"),
                        ({"custom_domain": "not a domain"}, "bad_domain"), ({"logo_asset_id": "00000000-0000-0000-0000-000000000000"}, "bad_logo")):
        with pytest.raises(partner.PartnerError) as why:
            world.do(partner.save_branding, bad)
        assert why.value.reason == reason
    from prism_wa import plans
    world.db.q("update tenant_settings set settings = '{\"plan\": \"pro\"}'")
    with pytest.raises(plans.PlanLimit):
        world.do(partner.save_branding, {"name": "X"})


def test_a_customer_workspace_is_linked_with_a_one_time_code_and_shows_its_resellers_branding(world):
    world.do(partner.save_branding, {"name": "BuildDesk", "colour": "#112233"})
    child = other_workspace(world)
    code = as_tenant(world, child, partner.invite)["code"]
    assert len(code.replace("-", "")) == 16
    linked = world.do(partner.redeem, code.lower(), 20, "Vadodara dealer")
    assert linked["child_tenant_id"] == str(child)
    shown = as_tenant(world, child, partner.effective_branding)
    assert shown["name"] == "BuildDesk" and shown["from"] == "reseller"
    own = as_tenant(world, child, partner.save_branding, {"name": "Customer Traders"})
    assert as_tenant(world, child, partner.effective_branding)["from"] == "own" and own["name"] == "Customer Traders"
    assert as_tenant(world, child, partner.reseller_of)["name"] == "BuildDesk"


def test_a_code_works_once_only_for_a_while_and_only_for_the_right_pair(world):
    child = other_workspace(world)
    code = as_tenant(world, child, partner.invite)["code"]
    with pytest.raises(partner.PartnerError) as why:
        world.do(partner.redeem, "AAAA-BBBB-CCCC-DDDD", 10)
    assert why.value.reason == "bad_code"
    world.do(partner.redeem, code, 10)
    with pytest.raises(partner.PartnerError) as why:
        world.do(partner.redeem, code, 10)
    assert why.value.reason == "bad_code"
    with pytest.raises(partner.PartnerError) as why:
        as_tenant(world, child, partner.invite)
    assert why.value.reason == "already_linked"
    old = other_workspace(world, "late")
    stale = as_tenant(world, old, partner.invite)["code"]
    world.db.q("update partner_invites set expires_at = now() - interval '1 hour' where tenant_id = %s", (old,))
    with pytest.raises(partner.PartnerError):
        world.do(partner.redeem, stale, 10)


def test_a_workspace_cannot_be_its_own_reseller_or_have_two(world):
    code = as_tenant(world, world.tid_, partner.invite)["code"]
    with pytest.raises(partner.PartnerError) as why:
        world.do(partner.redeem, code, 10)
    assert why.value.reason == "self_link"
    child = other_workspace(world)
    other_reseller = other_workspace(world, "second", "unlimited")
    as_tenant(world, other_reseller, partner.redeem, as_tenant(world, child, partner.invite)["code"], 5)
    c2 = as_tenant(world, child, partner.reseller_of)
    assert c2["tenant_id"] == other_reseller
    with pytest.raises(partner.PartnerError):
        as_tenant(world, child, partner.invite)


@pytest.mark.parametrize("percent", [-1, 101, "abc"])
def test_a_commission_is_a_percentage(world, percent):
    child = other_workspace(world)
    code = as_tenant(world, child, partner.invite)["code"]
    with pytest.raises(partner.PartnerError) as why:
        world.do(partner.redeem, code, percent)
    assert why.value.reason == "bad_percent"


def test_a_reseller_sees_how_busy_a_customer_is_and_nothing_inside_it(world):
    child = other_workspace(world)
    world.do(partner.redeem, as_tenant(world, child, partner.invite)["code"], 15)
    cid = world.db.one("insert into contacts (tenant_id, name, phone) values (%s, 'Secret Person', '919811111111') returning id", (child,))
    conv = world.db.one("insert into conversations (tenant_id, contact_id, channel, last_active_at) values (%s, %s, 'whatsapp', now()) returning id", (child, cid))
    for direction in ("out", "out", "in"):
        world.db.q("insert into messages (tenant_id, conversation_id, direction, sender, text, channel) values (%s, %s, %s, 'agent', 'private words', 'whatsapp')", (child, conv, direction))
    rows = world.do(partner.customers)
    assert rows == [{"child_tenant_id": str(child), "name": "Customer Traders", "commission_percent": 15.0, "note": None, "linked_at": rows[0]["linked_at"], "sent": 2, "received": 1, "contacts": 1}]
    assert "private words" not in json.dumps(rows, default=str) and "Secret Person" not in json.dumps(rows, default=str)


def test_commission_is_worked_out_in_whole_paise_and_a_paid_month_is_locked(world):
    child = other_workspace(world)
    world.do(partner.redeem, as_tenant(world, child, partner.invite)["code"], "12.5")
    e = world.do(partner.ledger_add, child, "2026-10", "4,999", "October plan")
    assert e["base_paise"] == 499900 and e["percent"] == 12.5 and e["amount_paise"] == 62488
    again = world.do(partner.ledger_add, child, "2026-10", "5000")
    assert again["amount_paise"] == 62500 and world.do(partner.ledger_list)["owed_paise"] == 62500
    assert world.do(partner.ledger_mark_paid, e["id"]) is True and world.do(partner.ledger_mark_paid, e["id"]) is False
    out = world.do(partner.ledger_list)
    assert out["owed_paise"] == 0 and out["paid_paise"] == 62500
    with pytest.raises(partner.PartnerError) as why:
        world.do(partner.ledger_add, child, "2026-10", "1")
    assert why.value.reason == "already_paid"
    for bad, reason in (("2026-13", "bad_period"), ("oct", "bad_period")):
        with pytest.raises(partner.PartnerError) as why:
            world.do(partner.ledger_add, child, bad, "1")
        assert why.value.reason == reason


def test_either_side_can_end_the_link_and_the_percent_can_change(world):
    child = other_workspace(world)
    world.do(partner.redeem, as_tenant(world, child, partner.invite)["code"], 10)
    world.do(partner.set_percent, child, 25)
    assert world.do(partner.customers)[0]["commission_percent"] == 25.0
    with pytest.raises(partner.PartnerError):
        world.do(partner.set_percent, "00000000-0000-0000-0000-000000000000", 5)
    assert as_tenant(world, child, partner.unlink) is True and world.do(partner.customers) == []
    world.do(partner.redeem, as_tenant(world, child, partner.invite)["code"], 10)
    assert world.do(partner.unlink, child) is True and as_tenant(world, child, partner.reseller_of) is None


def test_the_reseller_routes_are_for_the_owner(world):
    owner, asha = world.team()
    assert world.post("/internal/partner/customers", by_agent_id=str(asha["id"])).status_code == 409
    child = other_workspace(world)
    code = as_tenant(world, child, partner.invite)["code"]
    assert world.post("/internal/partner/redeem", by_agent_id=str(asha["id"]), code=code, commission_percent=10).status_code == 409
    ok = world.post("/internal/partner/redeem", by_agent_id=str(owner["id"]), code=code, commission_percent=10)
    assert ok.status_code == 200 and any(e["action"] == "partner.link" for e in world.do(audit.list_entries)["entries"])
    assert world.post("/internal/partner/redeem", by_agent_id=str(owner["id"]), code="NOPE", commission_percent=10).json()["error"] == "bad_code"


# ── coexistence ──────────────────────────────────────────────────────────────

def phone_event(field, value, pnid=P.PNID):
    return P.envelope({"messaging_product": "whatsapp", "metadata": {"display_phone_number": "919726063133", "phone_number_id": pnid}, **value}, field=field)


def test_the_three_coexistence_fields_become_their_own_events():
    echo = normalise(phone_event("smb_message_echoes", {"message_echoes": []}))
    hist = normalise(phone_event("history", {"history": []}))
    sync = normalise(phone_event("smb_app_state_sync", {"state_sync": []}))
    assert [e.kind for e in echo + hist + sync] == ["echo", "history", "app_state"] and all(e.phone_number_id == P.PNID for e in echo + hist + sync)


def test_a_message_typed_in_the_phone_app_shows_in_the_chat_as_sent_from_the_phone_once(world):
    payload = phone_event("smb_message_echoes", {"message_echoes": [{"from": "919726063133", "to": "919800000009", "id": "wamid.ECHO1", "timestamp": "1760000500", "type": "text",
                                                                       "text": {"body": "We will come at 4"}}]})
    H.deliver(world.db, payload)
    H.work(world.db)
    H.deliver(world.db, payload)
    H.work(world.db)
    row = world.db.q("select direction, sender, text, msg_type, status from messages where wamid = 'wamid.ECHO1'")
    assert len(row) == 1 and (row[0]["direction"], row[0]["sender"], row[0]["text"], row[0]["status"]) == ("out", "phone", "We will come at 4", "sent")
    assert world.db.one("select count(*) from contacts where phone = '919800000009'") == 1 and world.db.one("select count(*) from outbox") == 0
    assert world.db.one("select last_inbound_at from conversations") is None                  # a message from the phone does not open the customer's 24 hours


def test_past_chats_are_filed_quietly_with_their_direction_and_state_and_nothing_is_sent(world):
    world.db.q("update phone_numbers set coexistence = true")
    history = {"history": [{"metadata": {"phase": 0, "chunk_order": 1, "progress": 40}, "threads": [{"id": "919800000007", "messages": [
        {"from": "919800000007", "id": "wamid.H1", "timestamp": "1750000000", "type": "text", "text": {"body": "Price of a gate?"}, "history_context": {"status": "READ"}},
        {"from": "919726063133", "id": "wamid.H2", "timestamp": "1750000100", "type": "text", "text": {"body": "Rs 8000"}, "history_context": {"status": "DELIVERED"}},
        {"from": "919726063133", "id": "wamid.H3", "timestamp": "1750000200", "type": "image", "image": {"caption": "photo"}, "history_context": {"status": "ERROR"}}]}]}]}
    H.deliver(world.db, phone_event("history", history))
    H.work(world.db)
    rows = world.db.q("select wamid, direction, sender, status, msg_type from messages order by created_at")
    assert [(r["wamid"], r["direction"], r["sender"], r["status"]) for r in rows] == [("wamid.H1", "in", "customer", None), ("wamid.H2", "out", "phone", "delivered"), ("wamid.H3", "out", "phone", "failed")]
    assert world.db.one("select count(*) from outbox") == 0
    state = world.db.one("select coexistence_state from phone_numbers")
    assert state["history"] == "importing" and state["history_progress"] == 40 and state["history_messages"] == 3
    H.deliver(world.db, phone_event("history", {"history": [{"metadata": {"phase": 0, "chunk_order": 2, "progress": 100}, "threads": []}]}))
    H.work(world.db)
    assert world.db.one("select coexistence_state from phone_numbers")["history"] == "done"


def test_a_person_who_declines_the_history_is_recorded_as_declined(world):
    world.db.q("update phone_numbers set coexistence = true")
    H.deliver(world.db, phone_event("history", {"history": [{"errors": [{"code": 2593109, "message": "history sharing declined"}]}]}))
    H.work(world.db)
    assert world.db.one("select coexistence_state from phone_numbers")["history"] == "declined"


def test_contacts_saved_on_the_phone_are_added_with_their_names(world):
    world.db.q("update phone_numbers set coexistence = true")
    H.deliver(world.db, phone_event("smb_app_state_sync", {"state_sync": [
        {"type": "contact", "contact": {"full_name": "Ravi Patel", "first_name": "Ravi", "phone_number": "+91 98000 00011"}, "action": "add", "metadata": {"timestamp": "1"}},
        {"type": "contact", "contact": {"full_name": "Gone Person", "phone_number": "919800000012"}, "action": "remove"}]}))
    H.work(world.db)
    assert world.db.q("select name, phone from contacts") == [{"name": "Ravi Patel", "phone": "919800000011"}]
    assert world.db.one("select coexistence_state from phone_numbers")["contacts_imported"] == 1


def test_connecting_alongside_the_phone_app_skips_registering_and_asks_for_the_history(world):
    world.db.q("update wabas set token_enc = null")
    c = world.conn()
    result = onboarding.complete_signup(c, graph=world.graph_, vault=world.vault_, tenant_id=world.tid_, app_id="123", app_secret="sec", code="abc", waba_id=P.WABA,
                                        phone_number_id=P.PNID, coexistence=True)
    c.close()
    paths = [x["path"] for x in world.meta_.calls]
    assert result.registered and result.subscribed and not result.errors and not any(p.endswith("/register") for p in paths)
    assert [x["body"]["sync_type"] for x in world.meta_.calls if x["path"].endswith("/smb_app_data")] == ["smb_app_state_sync", "history"]
    row = world.db.q("select coexistence, status, coexistence_state from phone_numbers")[0]
    assert row["coexistence"] is True and row["status"] == "connected" and row["coexistence_state"]["contacts"] == "requested"


def test_an_ordinary_connection_still_registers_the_number(world):
    c = world.conn()
    result = onboarding.complete_signup(c, graph=world.graph_, vault=world.vault_, tenant_id=world.tid_, app_id="123", app_secret="sec", code="abc", waba_id=P.WABA, phone_number_id=P.PNID)
    c.close()
    assert result.registered and any(x["path"].endswith("/register") for x in world.meta_.calls) and world.db.one("select coexistence from phone_numbers") is False


def test_asking_again_passes_on_why_meta_refused(world):
    world.db.q("update phone_numbers set coexistence = true")
    world.meta_.refuse = "The history can only be requested within 24 hours of connecting."
    out = world.do(coexistence.request_sync, world.graph_, world.vault_, world.number_)
    assert out["history"].startswith("refused: The history can only be requested") and world.db.one("select coexistence_state from phone_numbers")["history_request"].startswith("refused")


def test_asking_for_history_on_an_ordinary_number_is_explained(world):
    with pytest.raises(coexistence.CoexistenceError) as why:
        world.do(coexistence.request_sync, world.graph_, world.vault_, world.number_)
    assert why.value.reason == "not_coexistence"


def test_the_onboarding_page_offers_the_phone_app_choice_and_sends_it_back(world):
    page = onboarding.render_page(app_id="123", config_id="456", state="s", version="v25.0", coexistence=True)
    assert "whatsapp_business_app_onboarding" in page and '"coexistence": true' in page
    assert '"coexistence": false' in onboarding.render_page(app_id="123", config_id="456", state="s", version="v25.0")
    link = world.post("/internal/onboarding/link", coexistence=True).json()
    assert link["path"].endswith("&mode=coexistence")


def test_a_route_that_names_no_caller_is_still_refused_when_the_licence_server_names_one(world):
    owner, asha = world.team()
    world.configure_sso(require_sso=True)
    refused = world.post("/internal/templates/list", sso_agent_id=str(asha["id"]))
    assert refused.status_code == 401 and refused.json()["error"] == "sso_required"
    assert world.post("/internal/templates/list", sso_agent_id=str(owner["id"])).status_code == 200
    started, _ = world.sign_in()
    token = world.do(sso.status, started["state"])["result"]["token"]
    assert world.post("/internal/templates/list", sso_agent_id=str(asha["id"]), sso_token=token).status_code == 200


def test_the_licence_server_can_ask_whether_sign_in_is_required(world):
    world.team()
    assert world.post("/internal/sso/required").json()["required"] is False
    world.configure_sso()
    assert world.post("/internal/sso/required").json()["required"] is False
    world.configure_sso(require_sso=True)
    assert world.post("/internal/sso/required").json()["required"] is True
