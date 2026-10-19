"""Token encryption and the Graph client. No database, no network."""
import json
import os

import pytest

from prism_wa.graph import Graph, GraphError
from prism_wa.send import backoff_seconds, classify
from prism_wa.vault import Vault, VaultError, generate_key


# ── vault ────────────────────────────────────────────────────────────────────

def make_vault(*versions):
    return Vault({v: os.urandom(32) for v in versions})


def test_a_token_round_trips_and_is_not_stored_in_the_clear():
    v = make_vault(1)
    blob, ver = v.encrypt("EAAB-secret-token", context="W1")
    assert ver == 1 and b"EAAB-secret-token" not in blob
    assert v.decrypt(blob, context="W1") == "EAAB-secret-token"


def test_the_same_token_encrypts_differently_each_time():
    v = make_vault(1)
    assert v.encrypt("t", context="W1")[0] != v.encrypt("t", context="W1")[0]


def test_a_token_copied_to_another_waba_does_not_decrypt():
    v = make_vault(1)
    blob, _ = v.encrypt("t", context="W1")
    with pytest.raises(VaultError):
        v.decrypt(blob, context="W2")


def test_a_tampered_or_garbage_blob_is_refused():
    v = make_vault(1)
    blob, _ = v.encrypt("t", context="W1")
    flipped = blob[:-1] + bytes([blob[-1] ^ 1])
    for bad in (flipped, b"", None, b"not a token at all, just bytes", blob[:10]):
        with pytest.raises(VaultError):
            v.decrypt(bad, context="W1")


def test_a_different_key_cannot_decrypt():
    blob, _ = make_vault(1).encrypt("t", context="W1")
    with pytest.raises(VaultError):
        make_vault(1).decrypt(blob, context="W1")


def test_rotation_new_key_encrypts_old_key_still_decrypts_until_retired():
    keys = {1: os.urandom(32), 2: os.urandom(32)}
    old, _ = Vault({1: keys[1]}).encrypt("t", context="W1")
    both = Vault(keys)
    assert both.current == 2 and both.decrypt(old, context="W1") == "t"
    moved, ver = both.reencrypt(old, context="W1")
    assert ver == 2 and Vault({2: keys[2]}).decrypt(moved, context="W1") == "t"
    with pytest.raises(VaultError):
        Vault({2: keys[2]}).decrypt(old, context="W1")


def test_the_vault_reads_its_keys_from_the_environment_and_checks_them():
    k1, k2 = generate_key(), generate_key()
    v = Vault.from_env({"WA_TOKEN_KEYS": "1:%s, 2:%s" % (k1, k2)})
    assert v.current == 2
    assert Vault.from_env({"WA_TOKEN_KEYS": "1:%s,2:%s" % (k1, k2), "WA_TOKEN_KEY_CURRENT": "1"}).current == 1
    for env in ({}, {"WA_TOKEN_KEYS": ""}, {"WA_TOKEN_KEYS": "oops"}, {"WA_TOKEN_KEYS": "1:AAAA"},
                {"WA_TOKEN_KEYS": "x:" + k1}, {"WA_TOKEN_KEYS": "1:!!!!"}):
        with pytest.raises(VaultError):
            Vault.from_env(env)


# ── graph ────────────────────────────────────────────────────────────────────

class Transport:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body if body is not None else {}, []

    def __call__(self, method, url, data, headers):
        self.calls.append({"method": method, "url": url, "body": json.loads(data) if data else None,
                           "headers": headers})
        return self.status, json.dumps(self.body).encode()


def test_send_message_posts_to_the_numbers_messages_endpoint_with_a_bearer_token():
    t = Transport(body={"messages": [{"id": "wamid.X"}]})
    out = Graph(t, version="v25.0").send_message("TOKEN", "1263819723486063", {"type": "text"})
    call = t.calls[0]
    assert out["messages"][0]["id"] == "wamid.X"
    assert (call["method"], call["url"]) == ("POST", "https://graph.facebook.com/v25.0/1263819723486063/messages")
    assert call["headers"]["Authorization"] == "Bearer TOKEN" and call["body"] == {"type": "text"}


def test_a_meta_error_becomes_a_graph_error_with_its_code():
    body = {"error": {"message": "Re-engagement message", "type": "OAuthException", "code": 131047,
                      "error_data": {"details": "More than 24 hours"}, "fbtrace_id": "AbC"}}
    with pytest.raises(GraphError) as e:
        Graph(Transport(400, body)).send_message("T", "1", {})
    err = e.value
    assert (err.code, err.http_status, err.fbtrace_id, err.details) == (131047, 400, "AbC", "More than 24 hours")


def test_an_error_object_with_a_200_status_is_still_an_error():
    with pytest.raises(GraphError):
        Graph(Transport(200, {"error": {"message": "x", "code": 1}})).send_message("T", "1", {})


def test_a_non_json_failure_is_still_a_graph_error():
    class Html:
        def __call__(self, *a):
            return 502, b"<html>bad gateway</html>"
    with pytest.raises(GraphError) as e:
        Graph(Html()).send_message("T", "1", {})
    assert e.value.http_status == 502 and e.value.code is None


def test_embedded_signup_calls():
    t = Transport(body={"access_token": "BIZTOKEN"})
    g = Graph(t, version="v25.0")
    assert g.exchange_code("APPID", "APPSECRET", "the-code") == "BIZTOKEN"
    assert "client_id=APPID" in t.calls[0]["url"] and "code=the-code" in t.calls[0]["url"]
    assert t.calls[0]["method"] == "GET" and "Authorization" not in t.calls[0]["headers"]

    g.register_number("T", "PN1", "123456")
    assert t.calls[1]["url"].endswith("/v25.0/PN1/register")
    assert t.calls[1]["body"] == {"messaging_product": "whatsapp", "pin": "123456"}

    g.subscribe_app("T", "WABA1")
    assert t.calls[2]["url"].endswith("/v25.0/WABA1/subscribed_apps") and t.calls[2]["body"] is None
    g.subscribe_app("T", "WABA1", override_callback_uri="https://x/hook", verify_token="v")
    assert t.calls[3]["body"] == {"override_callback_uri": "https://x/hook", "verify_token": "v"}


def test_exchange_without_a_token_in_the_reply_fails():
    with pytest.raises(GraphError):
        Graph(Transport(body={})).exchange_code("A", "S", "c")


# ── error classes ────────────────────────────────────────────────────────────

def err(code, status=400):
    return GraphError("x", code=code, http_status=status)


def test_errors_are_classified_the_way_the_spec_says():
    assert classify(err(131047)) == "needs_template"
    assert classify(err(131050)) == "opted_out"
    for code in (190, 368, 130403, 132000, 132001, 132016):
        assert classify(err(code)) == "fix_setup", code
    for code in (1, 2, 4, 80007, 130429, 131000, 131016, 131049, 131056, 131057, 133004, 2494100):
        assert classify(err(code)) == "retry", code
    assert classify(err(131026)) == "fatal" and classify(err(999999)) == "fatal"
    assert classify(GraphError("net")) == "retry"                       # no response at all
    assert classify(err(None, 503)) == "retry" and classify(err(None, 429)) == "retry"


def test_backoff_grows_and_special_codes_have_their_own_waits():
    assert [backoff_seconds(err(1), n) for n in (1, 2, 3, 4, 5, 6)] == [5, 30, 120, 600, 3600, 3600]
    assert backoff_seconds(err(131056), 1) == 60
    assert backoff_seconds(err(130429), 3) == 5
    assert backoff_seconds(err(131049), 1) == 86400
