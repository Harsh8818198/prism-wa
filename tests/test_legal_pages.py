"""The public privacy and data-deletion pages Meta needs before an app goes live. They must answer without a login or a database, and what they promise must match the code."""
import pytest
from fastapi.testclient import TestClient

from prism_wa import audit, legal
from prism_wa.app import create_app


def client(**kw):
    def no_database():
        raise AssertionError("a public page must not touch the database")
    return TestClient(create_app(app_secrets="s", verify_token="v", connect=no_database, **kw))


@pytest.mark.parametrize("kw", [{}, {"internal_token": "tok"}])
@pytest.mark.parametrize("path", ["/privacy", "/data-deletion"])
def test_the_pages_are_public_html_with_or_without_the_internal_api(path, kw):
    r = client(**kw).get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>" in r.text and legal.CONTACT in r.text


def test_the_privacy_policy_names_what_meta_reviewers_look_for():
    text = legal.PRIVACY
    for needed in ("whatsapp_business_management", "whatsapp_business_messaging", "Singapore", "AES-256-GCM", "Render", "Supabase", "Meta", "/data-deletion"):
        assert needed in text, needed
    assert "%(" not in text and "%s" not in text                                # every placeholder was filled


def test_what_the_policy_promises_matches_the_code():
    assert "%d days" % audit.KEEP_DAYS in legal.PRIVACY                         # the activity log's retention
    from prism_wa import consent
    for word in consent.DEFAULTS["opt_out_keywords"]:                           # the words a person can reply to stop messages
        assert word in (legal.PRIVACY + legal.DELETION).lower()


def test_the_deletion_page_says_how_to_ask_and_by_when():
    assert "Delete my workspace" in legal.DELETION and "Delete my data" in legal.DELETION and "30 days" in legal.DELETION
