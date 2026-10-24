"""A small client for an OpenAI-compatible chat-completions API (Groq, OpenAI, OpenRouter, a local server).

Only what the AI agents need: send messages, get the reply text and how many tokens it cost. The
HTTP is injectable (the same shape as graph.py's transport) so tests never touch the network.

Configuration is from the environment (main.py / worker.py read it):
    WA_LLM_BASE_URL   default https://api.groq.com/openai/v1
    WA_LLM_API_KEY    required for AI agents to answer; without it they hand every chat to a person
    WA_LLM_MODEL      default llama-3.3-70b-versatile
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

DEFAULT_BASE = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class LLMError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


USER_AGENT = "prism-wa/1.0"


def urllib_transport(method: str, url: str, data: Optional[bytes], headers: dict) -> tuple:
    # Python's own default signature ("Python-urllib/3.x") is refused by Groq's gateway with the plain text "error code: 1010" (Cloudflare), so every call said it was
    # unreadable. Say who we are.
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:                       # noqa: S310  (the configured API address)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LLMError("could not reach the language model: %s" % exc) from exc


class LLM:
    def __init__(self, *, api_key: str, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 transport: Callable = urllib_transport):
        self.api_key, self.base_url, self.model, self._transport = api_key, base_url.rstrip("/"), model, transport

    def chat(self, messages: list, *, model: Optional[str] = None, max_tokens: int = 500, temperature: float = 0.2,
             json_mode: bool = False) -> tuple:
        """-> (reply text, {"tokens_in": n, "tokens_out": n})."""
        body = {"model": model or self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        status, raw = self._transport("POST", self.base_url + "/chat/completions", json.dumps(body).encode("utf-8"),
                                      {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            seen = (raw or b"")[:120].decode("utf-8", "replace").strip().replace("\n", " ")
            raise LLMError("the language model sent something unreadable (HTTP %s%s)" % (status, ": " + seen if seen else ""), status) from None
        if status >= 400:
            message = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None) or "refused (%s)" % status
            raise LLMError("the language model said: %s" % message, status)
        try:
            text = str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise LLMError("the language model sent no answer", status) from None
        usage = data.get("usage") or {}
        return text, {"tokens_in": int(usage.get("prompt_tokens") or 0), "tokens_out": int(usage.get("completion_tokens") or 0)}


def from_env(env) -> Optional[LLM]:
    key = (env.get("WA_LLM_API_KEY") or "").strip()
    if not key:
        return None
    return LLM(api_key=key, base_url=env.get("WA_LLM_BASE_URL") or DEFAULT_BASE, model=env.get("WA_LLM_MODEL") or DEFAULT_MODEL)
