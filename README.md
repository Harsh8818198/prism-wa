# prism-wa

Prism's WhatsApp platform: the service behind the WhatsApp add-on in the Prism desktop app (replacing AiSensy). The desktop app never talks to it
directly: it asks the licence server (`prism-license-server`, `/v1/whatsapp/p/<area>/<action>`), which decides who may ask and forwards the call here.
Plan: `prism_gui/docs/WHATSAPP_PLATFORM_SPEC.md`. What was built against AiSensy, tab by tab: `prism_gui/docs/WHATSAPP_TAB_GAP_ANALYSIS.md`.
**How to put it live on Render: `DEPLOY.md`.**

**Status (19-Oct-2026): built, tested against a real Postgres with fake Meta, Razorpay, LLM and sign-in providers, and ready to deploy.
Not yet run against Meta, Razorpay or a real identity provider: `DEPLOY.md` ends with the exact list of what is unproven live.**

## What is here

| Area | Modules |
|---|---|
| Webhook gateway: signature check, dedupe, store, answer | `app.py` `signature.py` `events.py` `store.py` |
| Queue, workers, heartbeat | `jobs.py` `worker.py` `health.py` |
| Inbound to contacts, conversations, messages, statuses | `ingest.py` |
| Send engine: 24-hour window, opt-out, rate limits, retries, at-most-once | `send.py` `graph.py` `chat_send.py` |
| Encrypted customer tokens | `vault.py` |
| Connecting a number (Embedded Signup), phone-app coexistence | `onboarding.py` `coexistence.py` |
| Templates: lint, create, sync, library, AI drafting | `templates.py` `template_library.py` `template_ai.py` |
| Contacts: CSV in/out, attributes, tags, timeline, consent ledger, merge, erasure | `contacts.py` `contact_book.py` `consent.py` |
| Segments, campaigns (one-off, repeating, local send time), reports | `segments.py` `campaigns.py` `campaign_book.py` `schedules.py` `reports.py` |
| Team inbox: ownership, routing, notes, saved replies, auto-replies | `inbox.py` `inbox_view.py` |
| Chatbot flows (23 step types), AI agents (knowledge base, actions), automatic messages | `flows.py` `flow_engine.py` `ai_*.py` `autos.py` |
| Shop: catalogue, orders, Razorpay and Meta payments, refunds | `commerce.py` `payments.py` `razorpay.py` |
| Forms (WhatsApp Flows) with an encrypted live endpoint | `forms.py` `form_endpoint.py` |
| Ads (click-to-WhatsApp, always created paused), audiences, conversion events | `ads*.py` `capi.py` |
| Media library, usage and cost estimates, outbound webhooks | `media*.py` `usage.py` `webhooks_out.py` |
| Public API with API keys, Shopify and WooCommerce | `public_api.py` `api_keys.py` `integrations.py` `hooks.py` |
| Plans and their limits, audit log, single sign-on (OIDC), resellers | `plans.py` `audit.py` `sso.py` `partner.py` |
| Service-to-service API for the licence server | `internal.py` `ops*.py` |

`db/migrations/0001-0029` hold the schema (read `db/README.md`; `0013`, `0021` and `0029` re-point everything that hangs off a contact when two
contacts are merged, and the guard test `tests/test_identity.py` fails if a new table references `contacts` without it).

## Run the tests

```
pip install -r requirements-dev.txt
PG_BIN=<folder with initdb and pg_ctl> python -m pytest tests
```

The tests start a throwaway Postgres (or use `PRISM_WA_TEST_ADMIN_DSN` for a disposable server, never Supabase). The whole suite takes about ten
minutes; running it as four parallel shards (split the files) takes a quarter of that. Two `preflight` tests need a Postgres built with libxml and
fail on builds without it.

## Run it

```
python tools/gen_secrets.py              # the three secrets nobody hands you; prints to your terminal only
WA_APP_SECRET=... WA_VERIFY_TOKEN=... DATABASE_URL=postgresql://... WA_INTERNAL_TOKEN=... WA_TOKEN_KEYS=1:...   uvicorn prism_wa.main:create_app_from_env --factory --port 8090
DATABASE_URL=... WA_TOKEN_KEYS=1:... python -m prism_wa.worker
python tools/smoke.py http://localhost:8090      # checks a running gateway from the outside
```

`GET /healthz` is liveness (never touches the database); `GET /readyz` checks the database, that it has every migration this build ships with,
and that the worker has been seen lately. `WA_ENV=prod` makes the gateway refuse to start with a weak or missing secret.

## Things to know before going live

* The payload shapes are hand-written to Meta's documented format and tolerate drift (what cannot be read is kept in `raw`); check them against
  Meta's own samples once the app is live. The Embedded Signup page, the ads calls, order and payment messages, Flow JSON and the coexistence
  webhooks are written from Meta's documentation and untested against Meta.
* The rate card behind cost estimates is third-party: confirm it against Meta's CSV. Meta's invoice is the real figure.
* One database connection is opened per request. Fine for the first customers; a pool is the next scaling change.
* CSV import runs at about 500 rows a second.
* Not supported, because code cannot do them: SAML sign-in (OpenID Connect is), a reseller's own domain (DNS), WhatsApp Status ads, AI-generated
  ad pictures, virtual numbers, and Meta's own approvals.
