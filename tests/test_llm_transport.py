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


def _answer(text="ok"):
    return 200, json.dumps({"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()


def _recorder(*replies):
    calls = []
    queue = list(replies)

    def transport(method, url, data, headers):
        calls.append(json.loads(data))
        return queue.pop(0)
    return calls, transport


def test_a_thinking_model_is_given_room_to_think_and_asked_to_think_little_on_groq():
    # gpt-oss thinks first and the thinking counts against max_tokens, so the AI agent's 500 left almost no room for the JSON answer and
    # Groq said "Failed to generate JSON" on anything longer than a greeting.
    calls, transport = _recorder(_answer())
    llm.LLM(api_key="k", model="openai/gpt-oss-120b", transport=transport).chat([{"role": "user", "content": "hi"}], max_tokens=500, json_mode=True)
    assert calls[0]["max_tokens"] == 500 + llm.THINKING_ROOM and calls[0]["reasoning_effort"] == "low"


def test_other_models_and_other_hosts_are_asked_exactly_as_before():
    calls, transport = _recorder(_answer(), _answer())
    llm.LLM(api_key="k", model="llama-3.3-70b-versatile", transport=transport).chat([{"role": "user", "content": "hi"}], max_tokens=500)
    llm.LLM(api_key="k", base_url="https://openrouter.ai/api/v1", model="openai/gpt-oss-120b", transport=transport).chat([{"role": "user", "content": "hi"}], max_tokens=500)
    assert calls[0]["max_tokens"] == 500 and "reasoning_effort" not in calls[0]
    assert calls[1]["max_tokens"] == 500 + llm.THINKING_ROOM and "reasoning_effort" not in calls[1]          # room yes, a Groq-only field no


def test_a_failed_json_generation_is_asked_again_without_json_mode_and_with_more_room():
    failed = (400, json.dumps({"error": {"message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
                                         "type": "invalid_request_error", "code": "json_validate_failed"}}).encode())
    calls, transport = _recorder(failed, _answer('{"reply": "Hello"}'))
    text, _ = llm.LLM(api_key="k", model="openai/gpt-oss-120b", transport=transport).chat([{"role": "user", "content": "hi"}], max_tokens=500, json_mode=True)
    assert text == '{"reply": "Hello"}' and len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"} and "response_format" not in calls[1]
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"]


def test_any_other_refusal_is_not_retried_and_a_second_failure_is_reported():
    calls, transport = _recorder((401, json.dumps({"error": {"message": "bad key"}}).encode()))
    with pytest.raises(llm.LLMError) as why:
        llm.LLM(api_key="k", transport=transport).chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert "bad key" in str(why.value) and len(calls) == 1
    failed = (400, json.dumps({"error": {"message": "Failed to generate JSON"}}).encode())
    calls, transport = _recorder(failed, failed)
    with pytest.raises(llm.LLMError) as again:
        llm.LLM(api_key="k", transport=transport).chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert "Failed to generate JSON" in str(again.value) and len(calls) == 2
