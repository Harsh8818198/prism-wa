"""A small client for an OpenAI-compatible embeddings API, so an agent can find a note by what it MEANS and not only by the words in it.

A customer who writes "can I send the product back?" shares no word with a note titled "Return policy"; a meaning vector puts them side by side.
Embeddings are optional: without a key (WA_EMBED_API_KEY) search still works by words, helped by the language model's synonyms (ai_knowledge.py).

    WA_EMBED_BASE_URL   default https://api.openai.com/v1
    WA_EMBED_API_KEY    required to turn this on
    WA_EMBED_MODEL      default text-embedding-3-small

The HTTP is injectable (the same shape as llm.py), so tests never touch the network.
"""
from __future__ import annotations

import json
import math
from typing import Callable, Optional

from prism_wa.llm import LLMError, urllib_transport

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "text-embedding-3-small"
BATCH = 64


class Embedder:
    def __init__(self, *, api_key: str, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL, transport: Callable = urllib_transport):
        self.api_key, self.base_url, self.model, self._transport = api_key, base_url.rstrip("/"), model, transport

    def embed(self, texts: list) -> list:
        """One vector (a list of floats) per text, in order. Raises LLMError when the service cannot be reached or refuses."""
        out: list = []
        for i in range(0, len(texts), BATCH):
            batch = [t[:6000] for t in texts[i:i + BATCH]]
            status, raw = self._transport("POST", self.base_url + "/embeddings", json.dumps({"model": self.model, "input": batch}).encode("utf-8"),
                                          {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise LLMError("the embeddings service sent something unreadable", status) from None
            if status >= 400:
                message = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None) or "refused (%s)" % status
                raise LLMError("the embeddings service said: %s" % message, status)
            try:
                rows = sorted(data["data"], key=lambda r: r["index"])
                vectors = [[float(x) for x in r["embedding"]] for r in rows]
            except (KeyError, TypeError, ValueError):
                raise LLMError("the embeddings service sent no vectors", status) from None
            if len(vectors) != len(batch):
                raise LLMError("the embeddings service sent the wrong number of vectors", status)
            out.extend(vectors)
        return out


def from_env(env) -> Optional[Embedder]:
    key = (env.get("WA_EMBED_API_KEY") or "").strip()
    if not key:
        return None
    return Embedder(api_key=key, base_url=env.get("WA_EMBED_BASE_URL") or DEFAULT_BASE, model=env.get("WA_EMBED_MODEL") or DEFAULT_MODEL)


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
