"""What an AI agent knows, and how it finds the right piece: taking in a whole website, keeping each source's state, refreshing a page, and searching by words
and by meaning.

A source is `ready` (searchable), `processing` (being read — a website crawl or a re-read) or `failed` (with the reason). A website is read in the worker,
not in the owner's click: the root page is added at once as `processing`, a job reads it and the pages it links to on the same site (up to a limit, at most
two links deep) and each becomes its own source in one group, so one page that fails does not lose the rest.

Search has three layers, each used only if the one before found too little:
  1. words      Postgres full-text search, any word may match, better overlap ranks higher
  2. meaning    when an embeddings service is set (WA_EMBED_API_KEY): the question and every note become vectors and the nearest win; merged with 1 by rank
  3. synonyms   when 1 and 2 found nothing at all, the agent has notes, and a language model is set: the model lists words a good answer would contain (in English, and Hindi or
                Gujarati written in English letters), and those are searched
Each model call after the first is recorded as `aux` usage — it costs tokens, it is not a reply and does not use the monthly allowance.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urldefrag, urljoin, urlsplit

from prism_wa import ai_agent, embeddings, flow_engine, jobs
from prism_wa.llm import LLMError

log = logging.getLogger("prism_wa.ai")

MAX_PAGES = 30
DEFAULT_PAGES = 15
MAX_DEPTH = 2
CRAWL_SECONDS = 60
SKIP_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3", ".css", ".js", ".ico", ".xml", ".docx", ".xlsx")
LINK = re.compile(r'(?is)<a\s[^>]*?href\s*=\s*["\']([^"\'#][^"\']*)["\']')
TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
RRF_K = 60
STOP = frozenset("""the and for are you your yours with that this these those what when where which who whom how can will would could should have has had not but from
they them their there here was were been does did doing our out any all its about into than then just also may might shall let get got please hello thanks thank""".split())


# ── statuses ─────────────────────────────────────────────────────────────────

def _set_status(cur, knowledge_id, status: str, error: str = "") -> None:
    cur.execute("update ai_knowledge set status = %s, error = %s, refreshed_at = now() where id = %s", (status, error[:300] or None, knowledge_id))


def list_sources(cur, tenant_id, agent_id) -> list:
    """The agent's sources, newest first, each with its state and whether every piece of it can be searched by meaning."""
    ai_agent._agent(cur, tenant_id, agent_id)
    cur.execute("select k.id, k.title, k.kind, k.source, length(k.body), k.created_at, k.status, k.error, k.group_id, k.refreshed_at,"
                " (select count(*) from ai_chunks c where c.knowledge_id = k.id),"
                " (select count(*) from ai_chunks c where c.knowledge_id = k.id and c.embedding is not null)"
                " from ai_knowledge k where k.agent_id = %s order by k.created_at desc, k.title", (agent_id,))
    cols = ("id", "title", "kind", "source", "characters", "created_at", "status", "error", "group_id", "refreshed_at", "chunks", "embedded")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── reading a website ────────────────────────────────────────────────────────

def _title_of(markup: str, fallback: str) -> str:
    m = TITLE.search(markup or "")
    t = re.sub(r"\s+", " ", ai_agent.html.unescape(m.group(1))).strip() if m else ""
    return (t or fallback)[:120]


def same_site_links(markup: str, page_url: str, root: str) -> list:
    """The pages a page links to on the same website, as absolute https addresses without #fragments, in the order they appear, each once."""
    def bare(h: str) -> str:
        return h[4:] if h.startswith("www.") else h
    host = bare(urlsplit(root).netloc.lower())
    seen, out = set(), []
    for href in LINK.findall(markup or ""):
        if href.startswith(("mailto:", "tel:", "javascript:", "whatsapp:")):
            continue
        url = urldefrag(urljoin(page_url, ai_agent.html.unescape(href).strip()))[0]
        parts = urlsplit(url)
        if parts.scheme != "https" or bare(parts.netloc.lower()) != host or parts.path.lower().endswith(SKIP_SUFFIXES) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def add_site(cur, tenant_id, agent_id, url: str, max_pages: int = DEFAULT_PAGES, title: str = "") -> dict:
    """Start reading a whole website. -> {"knowledge_id", "group_id"}. The pages arrive as the worker reads them (`processing` until then)."""
    ai_agent._agent(cur, tenant_id, agent_id)
    url = (url or "").strip()
    if not url.startswith("https://") or not urlsplit(url).netloc:
        raise ai_agent.AIError("bad_url", "Give a web address starting with https://")
    max_pages = max(1, min(int(max_pages or DEFAULT_PAGES), MAX_PAGES))
    group = uuid.uuid4()
    kid = ai_agent.add_knowledge(cur, tenant_id, agent_id, title or url[:100], url, kind="url", source=url, status="processing")
    cur.execute("update ai_knowledge set group_id = %s where id = %s", (group, kid))
    jobs.enqueue(cur, tenant_id, "ai_crawl", {"knowledge_id": str(kid), "agent_id": str(agent_id), "url": url, "group_id": str(group), "max_pages": max_pages}, "ai_crawl:%s" % kid)
    return {"knowledge_id": kid, "group_id": group}


def _replace_body(cur, tenant_id, agent_id, knowledge_id, title: str, body: str) -> None:
    cur.execute("update ai_knowledge set title = %s, body = %s where id = %s", (title, body, knowledge_id))
    cur.execute("delete from ai_chunks where knowledge_id = %s", (knowledge_id,))
    ai_agent.store_chunks(cur, tenant_id, agent_id, knowledge_id, body)


def handle_crawl_job(cur, tenant_id, payload: dict) -> None:
    """Read the root page, then the pages it links to on the same site, breadth first, until the page limit, the depth limit or the time runs out."""
    kid, agent_id, root, group = payload["knowledge_id"], payload["agent_id"], payload["url"], payload["group_id"]
    cur.execute("select 1 from ai_knowledge where id = %s", (kid,))
    if cur.fetchone() is None:
        return                                                          # the owner removed it while it waited
    limit, started = int(payload.get("max_pages") or DEFAULT_PAGES), time.monotonic()
    queue, seen, done = [(root, 0)], {root}, 0
    while queue and done < limit and time.monotonic() - started < CRAWL_SECONDS:
        url, depth = queue.pop(0)
        try:
            status, markup = ai_agent.FETCH("GET", url, {}, None)
        except flow_engine.FlowRuntimeError as why:
            if done == 0:
                _set_status(cur, kid, "failed", str(why))
                return
            continue
        text = ai_agent.page_text(markup) if status < 400 else ""
        if len(text) < 40:
            if done == 0:
                _set_status(cur, kid, "failed", "Nothing readable was found on that page." if status < 400 else "That page answered with an error (%s)." % status)
                return
            continue
        title = _title_of(markup, url[:100])
        if done == 0:
            _replace_body(cur, tenant_id, agent_id, kid, title, text[:ai_agent.MAX_DOC_CHARS])
            _set_status(cur, kid, "ready")
            ai_agent.index_for_meaning(cur, tenant_id, kid)
        else:
            try:
                page = ai_agent.add_knowledge(cur, tenant_id, agent_id, title, text[:ai_agent.MAX_DOC_CHARS], kind="url", source=url)
                cur.execute("update ai_knowledge set group_id = %s where id = %s", (group, page))
            except ai_agent.AIError as why:
                if why.reason == "kb_full":
                    break
                continue
        done += 1
        if depth < MAX_DEPTH:
            for link in same_site_links(markup, url, root):
                if link not in seen and len(seen) < limit * 4:
                    seen.add(link)
                    queue.append((link, depth + 1))
    cur.execute("update ai_knowledge set refreshed_at = now() where id = %s", (kid,))


def refresh(cur, tenant_id, knowledge_id) -> None:
    """Read a web source again: it shows `processing` until the worker has the page, then `ready` with the new text (or `failed` and the old text kept)."""
    cur.execute("select agent_id, kind, source from ai_knowledge where id = %s and tenant_id = %s for update", (knowledge_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise ai_agent.AIError("unknown_source", "that source is not in this workspace")
    agent_id, kind, source = row
    if kind != "url" or not source:
        raise ai_agent.AIError("not_a_page", "Only a web page can be read again. Delete this note and add it afresh.")
    _set_status(cur, knowledge_id, "processing")
    jobs.enqueue(cur, tenant_id, "ai_refresh", {"knowledge_id": str(knowledge_id), "agent_id": str(agent_id), "url": source}, "ai_refresh:%s:%s" % (knowledge_id, uuid.uuid4().hex[:8]))


def handle_refresh_job(cur, tenant_id, payload: dict) -> None:
    kid, agent_id, url = payload["knowledge_id"], payload["agent_id"], payload["url"]
    cur.execute("select 1 from ai_knowledge where id = %s", (kid,))
    if cur.fetchone() is None:
        return
    try:
        status, markup = ai_agent.FETCH("GET", url, {}, None)
    except flow_engine.FlowRuntimeError as why:
        _set_status(cur, kid, "failed", str(why))
        return
    text = ai_agent.page_text(markup) if status < 400 else ""
    if len(text) < 40:
        _set_status(cur, kid, "failed", "Nothing readable was found on that page." if status < 400 else "That page answered with an error (%s)." % status)
        return
    _replace_body(cur, tenant_id, agent_id, kid, _title_of(markup, url[:100]), text[:ai_agent.MAX_DOC_CHARS])
    _set_status(cur, kid, "ready")
    ai_agent.index_for_meaning(cur, tenant_id, kid)


# ── meaning vectors ──────────────────────────────────────────────────────────

def handle_embed_job(cur, tenant_id, payload: dict) -> None:
    """Give every piece of a source its vector. Pieces already done are left alone, so a retry only does what is missing."""
    embedder = ai_agent.EMBEDDER
    if embedder is None:
        return
    cur.execute("select id, body from ai_chunks where knowledge_id = %s and embedding is null order by ord", (payload["knowledge_id"],))
    rows = cur.fetchall()
    if not rows:
        return
    vectors = embedder.embed([r[1] for r in rows])                       # LLMError here makes the job retry, as any failed job does
    for (cid, _body), vec in zip(rows, vectors):
        cur.execute("update ai_chunks set embedding = %s where id = %s", (vec, cid))


# ── search ───────────────────────────────────────────────────────────────────

def _fts(cur, tenant_id, agent_id, words: list, limit: int) -> list:
    """Any word may match, and a word matches the longer words it begins ("visit" finds "visits"). Words everyone uses ("the", "when") are left out."""
    clean = [re.sub(r"[^\w]", "", w.lower()) for w in words]
    words = [w for w in dict.fromkeys(clean) if len(w) > 2 and w not in STOP][:12]
    if not words:
        return []
    cur.execute("select c.id, c.body, k.title, ts_rank(c.tsv, q) from ai_chunks c join ai_knowledge k on k.id = c.knowledge_id,"
                " to_tsquery('simple', %s) q where c.tenant_id = %s and c.agent_id = %s and k.status = 'ready' and c.tsv @@ q order by 4 desc limit %s",
                (" | ".join(w + ":*" for w in words), tenant_id, agent_id, limit))
    return [{"id": r[0], "text": r[1], "title": r[2]} for r in cur.fetchall()]


def _semantic(cur, tenant_id, agent_id, query: str, embedder, limit: int) -> list:
    cur.execute("select c.id, c.body, k.title, c.embedding from ai_chunks c join ai_knowledge k on k.id = c.knowledge_id"
                " where c.tenant_id = %s and c.agent_id = %s and c.embedding is not null and k.status = 'ready'", (tenant_id, agent_id))
    rows = cur.fetchall()
    if not rows:
        return []
    q = embedder.embed([query])[0]
    scored = sorted(((embeddings.cosine(q, list(r[3])), r) for r in rows), key=lambda x: -x[0])
    return [{"id": r[0], "text": r[1], "title": r[2]} for s, r in scored[:limit] if s >= 0.25]


def merge(*lists, limit: int) -> list:
    """Reciprocal-rank fusion: a note that two searches both rank highly beats one that only one of them found."""
    score, keep = {}, {}
    for lst in lists:
        for rank, item in enumerate(lst):
            score[item["id"]] = score.get(item["id"], 0.0) + 1.0 / (RRF_K + rank)
            keep.setdefault(item["id"], item)
    return [keep[i] for i in sorted(score, key=lambda k: -score[k])][:limit]


EXPAND_PROMPT = ('A customer wrote a question to a business. List up to 8 single words a document that answers it would contain — synonyms, related terms, and the '
                 'same idea in Hindi or Gujarati written in English letters. Answer JSON only: {"keywords": ["..."]}.')


def expand(llm, query: str) -> tuple:
    """-> (words, tokens)."""
    import json
    raw, tokens = llm.chat([{"role": "system", "content": EXPAND_PROMPT}, {"role": "user", "content": query[:500]}], json_mode=True, max_tokens=120)
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()))
        words = [str(w) for w in (data.get("keywords") or [])] if isinstance(data, dict) else []
    except ValueError:
        words = []
    return [w for w in (re.findall(r"\w+", " ".join(words).lower())) if len(w) > 2][:16], tokens


def _has_knowledge(cur, agent_id) -> bool:
    cur.execute("select exists (select 1 from ai_chunks where agent_id = %s)", (agent_id,))
    return cur.fetchone()[0]


def retrieve(cur, tenant_id, agent_id, query: str, limit: int = ai_agent.TOP_CHUNKS, *, conversation_id=None) -> list:
    """The pieces of the agent's knowledge that best answer a customer's words. Never raises because of a model: a search that cannot use one falls back."""
    words = re.findall(r"\w+", (query or "").lower())
    found = _fts(cur, tenant_id, agent_id, words, limit * 3)
    embedder, llm = ai_agent.EMBEDDER, ai_agent.LLM_CLIENT
    meaning: list = []
    if embedder is not None and (query or "").strip():
        try:
            meaning = _semantic(cur, tenant_id, agent_id, query, embedder, limit * 3)
        except LLMError as why:
            log.info("meaning search skipped: %s", why)
    merged = merge(found, meaning, limit=limit)
    if not merged and llm is not None and len((query or "").strip()) > 3 and _has_knowledge(cur, agent_id):
        try:
            more_words, tokens = expand(llm, query)
            ai_agent.record_usage(cur, tenant_id, agent_id, conversation_id, "aux", tokens)
            merged = merge(merged, _fts(cur, tenant_id, agent_id, more_words, limit * 3), limit=limit)
        except LLMError as why:
            log.info("synonym search skipped: %s", why)
    return merged
