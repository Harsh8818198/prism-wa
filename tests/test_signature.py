"""X-Hub-Signature-256 — a forged or unsigned webhook must never pass."""
from prism_wa.signature import PREFIX, sign, verify_signature

SECRET = "app-secret-one"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def test_a_correct_signature_passes():
    assert verify_signature(SECRET, BODY, sign(SECRET, BODY))


def test_the_signature_is_over_the_raw_bytes_not_the_parsed_json():
    reformatted = b'{"object": "whatsapp_business_account", "entry": []}'
    assert not verify_signature(SECRET, reformatted, sign(SECRET, BODY))


def test_a_tampered_body_fails():
    assert not verify_signature(SECRET, BODY + b" ", sign(SECRET, BODY))


def test_the_wrong_secret_fails():
    assert not verify_signature(SECRET, BODY, sign("someone-elses-secret", BODY))


def test_a_missing_or_malformed_header_fails():
    for header in (None, "", "sha256", "sha1=" + sign(SECRET, BODY)[len(PREFIX):],
                   sign(SECRET, BODY)[len(PREFIX):], PREFIX, PREFIX + "zz", PREFIX + "éé"):
        assert not verify_signature(SECRET, BODY, header), header


def test_hex_case_does_not_matter():
    assert verify_signature(SECRET, BODY, PREFIX + sign(SECRET, BODY)[len(PREFIX):].upper())


def test_either_of_two_secrets_passes_so_a_rotation_has_no_gap():
    both = ["new-secret", SECRET]
    assert verify_signature(both, BODY, sign(SECRET, BODY))
    assert verify_signature(both, BODY, sign("new-secret", BODY))
    assert not verify_signature(both, BODY, sign("third", BODY))


def test_no_secret_configured_rejects_everything():
    for secrets in ("", [], [""], [None]):
        assert not verify_signature(secrets, BODY, sign(SECRET, BODY))
        assert not verify_signature(secrets, BODY, sign("", BODY))
