"""The language-model client must say who it is: Groq's gateway answers Python's default request signature with the plain text "error code: 1010", which showed
up in the app as "the language model sent something unreadable" on every AI call."""
import json

import pytest

from prism_wa import llm


class FakeResponse:
    status = 200

    def __init__(self, body=b"{}"):
        self._body = body

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_every_call_carries_a_named_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResponse(json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    client = llm.LLM(api_key="k")
    text, _ = client.chat([{"role": "user", "content": "hello"}])
    assert text == "hi"
    assert seen["headers"]["user-agent"] == llm.USER_AGENT and "python-urllib" not in seen["headers"]["user-agent"].lower()
    assert seen["headers"]["authorization"] == "Bearer k" and seen["headers"]["content-type"] == "application/json"


def test_an_answer_that_is_not_json_says_what_it_was():
    client = llm.LLM(api_key="k", transport=lambda *a: (403, b"error code: 1010\n"))
    with pytest.raises(llm.LLMError) as why:
        client.chat([{"role": "user", "content": "hello"}])
    assert "unreadable" in str(why.value) and "HTTP 403" in str(why.value) and "error code: 1010" in str(why.value)
    assert why.value.status == 403
