"""Run the gateway.

    WA_APP_SECRET=...  WA_VERIFY_TOKEN=...  DATABASE_URL=postgresql://...  \
      uvicorn prism_wa.main:create_app_from_env --factory --port 8090

Always required: WA_APP_SECRET (comma-separate several to rotate), WA_VERIFY_TOKEN,
DATABASE_URL. The process refuses to start without them.

Optional, switching on the rest of the service:
  WA_INTERNAL_TOKEN   shared with the licence server; mounts /internal/* and the connect page
  WA_TOKEN_KEYS       "1:<base64 32-byte key>[,2:...]" — encrypts customers' Meta tokens
  WA_APP_ID           the Meta app id (Embedded Signup)
  WA_ES_CONFIG_ID     the Embedded Signup configuration id from the Meta dashboard
  WA_GRAPH_VERSION    Graph API version (default in graph.py)
  WA_LLM_API_KEY      switches AI agents on (an OpenAI-compatible API key, e.g. Groq's)
  WA_LLM_BASE_URL     default https://api.groq.com/openai/v1
  WA_LLM_MODEL        default llama-3.3-70b-versatile
  LOG_LEVEL           INFO (default), DEBUG, WARNING...
  WA_EMBEDDED_WORKER  yes: run the worker as a thread of this process (a host with no background-worker service; see embedded.py)
  WA_MIGRATE_ON_START yes: apply missing migrations when the service starts (a host with no pre-deploy step; see migrate.py)
  WA_FIRST_RUN        yes: allow that to be the very first thing to touch the database (0001-0003 change live CRM tables). Once, then remove.
  WA_MIGRATE_DATABASE_URL  optional: the connection migrations use (session pooler 5432), when DATABASE_URL is the transaction pooler (6543)
  WA_SEND_CONCURRENCY      messages sent at once (default 4, 1-16): each holds a database connection for a moment, and the database is shared

WA_ENV=prod switches on the checks a live service needs: the three secrets must be long enough to be secrets, and WA_INTERNAL_TOKEN and
WA_TOKEN_KEYS must be set (without them the licence server cannot reach the gateway and no customer's Meta token can be kept). The process
refuses to start and says WHICH variable is wrong, never its value.
"""
from __future__ import annotations

import logging
import os

import psycopg2

from prism_wa import ai_agent, capi, embedded, embeddings, llm, media, media_store, migrate, payments
from prism_wa.app import create_app
from prism_wa.graph import Graph
from prism_wa.vault import Vault


def _required(env) -> tuple:
    secrets = [s.strip() for s in env.get("WA_APP_SECRET", "").split(",") if s.strip()]
    token = env.get("WA_VERIFY_TOKEN", "").strip()
    dsn = env.get("DATABASE_URL", "").strip()
    missing = [name for name, val in (("WA_APP_SECRET", secrets), ("WA_VERIFY_TOKEN", token),
                                      ("DATABASE_URL", dsn)) if not val]
    if missing:
        raise RuntimeError("missing environment: " + ", ".join(missing))
    return secrets, token, dsn


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The shortest a secret may be on a live service. Meta's app secret is 32 characters; the other two we invent (see DEPLOY.md).
MIN_APP_SECRET, MIN_VERIFY_TOKEN, MIN_INTERNAL_TOKEN = 16, 24, 32


def configure_logging(env=None) -> None:
    """Application logs to stdout at LOG_LEVEL (the host collects stdout). Without this only warnings reach the log, and the lines that say what
    the gateway did (a refused signature, a failed delivery, a flow that errored) are the first thing wanted when something goes wrong."""
    env = os.environ if env is None else env
    level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    logging.basicConfig(level=level if level in logging.getLevelNamesMapping() else "INFO", format=LOG_FORMAT)


def _yes(env, name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("yes", "true", "1", "on")


def is_production(env) -> bool:
    return (env.get("WA_ENV") or "").strip().lower() in ("prod", "production")


def check_production(env, secrets, token) -> None:
    """Refuse to start a live service with a weak or missing secret. Names the variables, never the values."""
    wrong = []
    if any(len(s) < MIN_APP_SECRET for s in secrets):
        wrong.append("WA_APP_SECRET is shorter than %d characters (copy Meta's app secret exactly)" % MIN_APP_SECRET)
    if len(token) < MIN_VERIFY_TOKEN:
        wrong.append("WA_VERIFY_TOKEN is shorter than %d characters" % MIN_VERIFY_TOKEN)
    internal = (env.get("WA_INTERNAL_TOKEN") or "").strip()
    if not internal:
        wrong.append("WA_INTERNAL_TOKEN is not set (the licence server could not reach this service)")
    elif len(internal) < MIN_INTERNAL_TOKEN:
        wrong.append("WA_INTERNAL_TOKEN is shorter than %d characters" % MIN_INTERNAL_TOKEN)
    if not (env.get("WA_TOKEN_KEYS") or "").strip():
        wrong.append("WA_TOKEN_KEYS is not set (customers' Meta tokens could not be stored)")
    if wrong:
        raise RuntimeError("refusing to start in production: " + "; ".join(wrong))


def connector(dsn: str):
    return lambda: psycopg2.connect(dsn)


def create_app_from_env(env=None):
    env = os.environ if env is None else env
    configure_logging(env)
    secrets, token, dsn = _required(env)
    if is_production(env):
        check_production(env, secrets, token)
    if env is os.environ and secrets:                                                          # the ads sign-in is the same Meta app as WhatsApp unless said otherwise
        os.environ.setdefault("META_APP_SECRET", secrets[0])
    if _yes(env, "WA_MIGRATE_ON_START"):
        migrate.apply_on_start((env.get("WA_MIGRATE_DATABASE_URL") or "").strip() or dsn, first_run=_yes(env, "WA_FIRST_RUN"))
    vault = Vault.from_env(env) if (env.get("WA_TOKEN_KEYS") or "").strip() else None
    ai_agent.configure(llm.from_env(env), embedder=embeddings.from_env(env))
    graph = Graph()
    payments.configure(vault)                                                                  # a flow that asks for payment needs the gateway's secret
    media.configure(graph=graph, vault=vault, store=media_store.from_env(env, connector(dsn)))   # the Media Library: a folder (WA_MEDIA_DIR) or the database
    capi.configure(graph=graph, vault=vault)                                                    # telling Meta what an ad earned
    app = create_app(app_secrets=secrets, verify_token=token, connect=connector(dsn),
                     internal_token=env.get("WA_INTERNAL_TOKEN", "").strip(),
                     app_id=env.get("WA_APP_ID", "").strip(),
                     es_config_id=env.get("WA_ES_CONFIG_ID", "").strip(),
                     graph=graph, vault=vault)
    if _yes(env, "WA_EMBEDDED_WORKER"):
        embedded.attach(app, connector(dsn), graph=graph, vault=vault)
    return app
