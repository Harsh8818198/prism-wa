# Putting prism-wa live on Render

Two things run: the **gateway** (a web service: Meta's webhooks, the licence server's calls, the connect page, the public API) and the **worker**
(a background service: sends queued messages, runs campaigns, delivers webhooks, checks payments, answers for AI agents). Both are built from the
one `Dockerfile`. The database is a Supabase project of its own (Step 3).

Cost: Render's paid tier for both (a web service and a background worker; about the price of two small instances). Not the free tier: it sleeps,
Meta gives up on a webhook that is not answered quickly, and a sleeping worker sends nothing.

Do the steps in this order. **Nothing here changes what customers have today until step 8** (the licence server keeps using n8n until it is
given prism-wa's address).

## Step 1 — Put the code on GitHub

`prism-wa` is its own repository. Render deploys from GitHub, so it needs a (private) repository of its own, beside `prism-license-server`.

    git remote add origin https://github.com/<you>/prism-wa.git
    git push -u origin main

## Step 2 — Make the secrets (on your own machine)

    python tools/gen_secrets.py

It prints three lines. Nothing is stored or sent. Put them in a password manager **now**:

| Line | What it is | If you lose it |
|---|---|---|
| `WA_VERIFY_TOKEN` | what Meta echoes back when you subscribe the webhook | make another, change it on Render and in Meta |
| `WA_INTERNAL_TOKEN` | the shared secret between the licence server and prism-wa | make another, change it in both places |
| `WA_TOKEN_KEYS` | encrypts every customer's Meta token | **every stored token becomes unreadable and every customer must reconnect.** Keep a copy that is not Render and not the database |

You also need: `WA_APP_SECRET` and `WA_APP_ID` (developers.facebook.com → your app → App settings → Basic) and `DATABASE_URL` (Supabase → Connect →
**Session pooler**, port 5432, of prism-wa's own Supabase project (Step 3 makes it); never the transaction pooler on 6543).

## Step 3 — The database, once, by hand

prism-wa keeps its data in a Supabase project **of its own**. Never in the licence server's project (it holds every customer's licence) and never in another
business's (for example the old Orena CRM). No deploy ever does this step first.

### 3A. A new, empty project (use this)

1. supabase.com → your organisation → **New project**: name `prism-wa`, region **Southeast Asia (Singapore)** (next to Render: every query is about 20–50 times faster
   than from Sydney), plan **Free**. Choose a strong database password and keep it in your password manager. The free plan allows 2 *active* projects (paused ones
   do not count) and pauses a project after a week of no use; prism-wa's worker talks to the database all day, which counts as use, but look at it after the first
   weeks, or move it to Pro ($25).
2. In the new project open the **SQL editor**, paste the whole of `db/baseline/0000_fresh_project.sql` and run it. It creates the base tables prism-wa builds on
   (no data, no tenant) and locks them against the public Supabase keys.
3. Project → **Connect** → *Session pooler* (port 5432). That string is `DATABASE_URL`.
4. From your machine, in the prism-wa folder (the connection string comes from the environment, so it is not in your shell history):

        $env:DATABASE_URL = "<that string>"      (PowerShell; in bash: export DATABASE_URL=...)
        $env:WA_LEGACY_TENANT = "none"
        python db/apply.py --status
        python db/apply.py --pending --first-run

   `none` means there is no old writer: no tenant is invented and every row must name its workspace. `--status` should list all 29 as "waiting" and say
   "name collisions: none".
5. Make the first workspace (a customer), in the SQL editor: `insert into tenants (slug, name) values ('acme', 'Acme Co') returning id;`. That id is what a
   licence is linked to in step 8. There is nothing to back up: the project is empty.

From then on every deploy runs `python db/apply.py --pending` by itself (the blueprint's pre-deploy step, or at start-up on the free tier): it applies only what the
database has not got, and if a migration fails the old version keeps serving.

### 3B. An existing project that already has the old CRM tables (only if you must)

For a database that already holds a CRM (`tenants`, `contacts`, `conversations`, `messages`, `leads`…) and that you are sure belongs to you.

The database is the Supabase project the licence server and the CRM already use, so this step is done with care and by hand; no deploy ever does it first.
The first migrations (0001–0003) change the live CRM tables. Nothing is deleted, but do these in order:

1. Read `db/README.md` and the section "What the first migration does to everything else" below.
2. **Copy the tables it changes.** In the Supabase SQL editor run `db/backup_before_first_run.sql`. It makes `bak_contacts`, `bak_messages`… (locked like the
   rest), and the last query shows the two counts side by side; they must match. The free Supabase plan has no automatic backups, so this is your only undo.
3. **Preflight.** Run `db/preflight.sql` and read the result: your own business has a row in `tenants`; `rows_without_tenant` is understood; and
   **`name_collisions` is `[]`**. Anything listed there is a table prism-wa wants to create that another Prism service already has: stop and tell me.
4. Run (the connection string comes from the environment, so it is not in your shell history):

        $env:DATABASE_URL = "<the session pooler string>"      (PowerShell; in bash: export DATABASE_URL=...)
        python db/apply.py --status                            what would be applied, and "name collisions: none"; changes nothing
        python db/apply.py --pending --first-run

From then on every deploy runs `python db/apply.py --pending` by itself: it applies only what the database has not got, and if a migration fails the old
version keeps serving. (If you ever put the running service on Supabase's transaction pooler, port 6543, give migrations their own connection with
`WA_MIGRATE_DATABASE_URL` set to the session pooler, port 5432.)

#### What the first migration does to everything else that uses this database

Checked against the other Prism services' code and tables (22-Oct-2026):

* **The licence server's tables are not touched.** None of prism-wa's tables has a licence-server table's name. (One did: `audit_log`. It is `wa_audit_log` now, and
  the first run refuses, naming them, if any table it would create already exists. A test keeps the list honest.)
* **Existing CRM rows are kept.** 0001 adds columns, fills in `tenant_id` on rows that have none (with the legacy default tenant, slug `orena`: check it in the
  preflight), and swaps the two global unique indexes on contacts for per-tenant ones. 0003 turns on row-level security.
* **n8n and the licence server keep working.** Both use the Supabase `service_role` key (the n8n credential guide says so), which bypasses row-level security. A writer
  that sends no tenant (the n8n flows) gets the default one from a trigger, and conversations and messages inherit their contact's. Something that used the public
  `anon` key on those tables would stop seeing rows: nothing Prism ships does, and no customer's computer holds that key.
* **Every new table is locked** against the public Supabase key (row-level security on and access revoked from `anon` and `authenticated`): 69 tables, none open.
* **The shared connection limit.** Supabase's session pooler gives about 15 connections in all, to everything. The service sends 4 messages at a time (not 8) so a
  broadcast cannot use them up; set `WA_SEND_CONCURRENCY` to change it. If the licence server ever logs "max clients reached", move the service to the
  transaction pooler (port 6543) and set `WA_MIGRATE_DATABASE_URL` for migrations.
* **Undoing it.** `db/rollback/0001_0003_down.sql` removes the enforcement and keeps every row; the `bak_*` copies from step 2 are the safety net.

## Step 4 — Create the service (or services)

Pick one blueprint: **`render.single.yaml`** (one paid service, $7 a month: the one to use for the first paying customers), `render.free.yaml` (one free
service, sleeps; see "The free option") or `render.yaml` (two paid services, $14: gateway and worker apart). Set it as the **Blueprint Path**.

Render dashboard → **workspace home** (not inside a project page) → **New → Blueprint** → pick the `prism-wa` repository. Render reads `render.yaml`
and asks for each value marked `sync: false`. Put it in the **Prism** project beside the licence server. Fill in:

| Both services | Gateway only |
|---|---|
| `DATABASE_URL`, `WA_TOKEN_KEYS` (the **same** values on both) | `WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_INTERNAL_TOKEN`, `WA_APP_ID`, `WA_ES_CONFIG_ID` (leave empty until step 6) |

If you would rather click the two services together by hand (project page → New): a **Web Service** (Docker, region Singapore, health check path
`/healthz`, pre-deploy command `python db/apply.py --pending`) and a **Background Worker** (Docker, region Singapore, start command
`python -m prism_wa.worker`), with the same environment variables as `render.yaml`, plus `WA_ENV=prod` on the gateway.

The gateway refuses to start in production (`WA_ENV=prod`) with a weak or missing secret and the log says which variable is wrong.

## Step 5 — Check it from outside

    $env:WA_VERIFY_TOKEN = "..."; $env:WA_INTERNAL_TOKEN = "..."        (PowerShell; in bash: export ...)
    python tools/smoke.py https://<your gateway>.onrender.com

It checks, and changes nothing: the gateway answers; the database answers; every migration is applied; the worker has been seen (`/readyz`);
forged and unsigned webhooks are refused; Meta's handshake works with your token; the licence server's door is shut without the token and open with it.
Every line must say PASS. `https://<your gateway>/readyz` is the same check for a browser or an uptime monitor.

## Step 6 — Tell Meta

1. App dashboard → WhatsApp → Configuration: **Callback URL** `https://<your gateway>/webhook/wa`, **Verify token** = `WA_VERIFY_TOKEN`.
   "Verify and save" must succeed.
2. Subscribe to the fields in section 4 below (messages, templates, phone number, account; and `smb_message_echoes`, `history`,
   `smb_app_state_sync` for coexistence).
3. Embedded Signup: create the configuration, copy its id into `WA_ES_CONFIG_ID` on the gateway.
4. Until the app is **published** and App Review is granted, Meta sends only test webhooks and customers cannot connect numbers of their own.

## Step 7 — Put the licence server's side in

On the licence server (Render → `prism-licence` → Environment): `PRISM_WA_URL` = `https://<your gateway>.onrender.com` (no trailing slash) and
`PRISM_WA_INTERNAL_TOKEN` = the same value as `WA_INTERNAL_TOKEN`. Deploy the licence server's new code (it carries `/v1/whatsapp/p/*`, the form,
payment and sign-in callbacks and the connection-pool fix). Then `https://<licence server>/health` shows `"whatsapp_platform": true`.

## Step 8 — Link a customer and look

1. Link a licence to its workspace (`POST /admin/licenses/<id>/whatsapp-tenant`, see `db/README.md` step 4) and give the licence the WhatsApp tier key
   it was sold (`wa_pro`, `wa_premium`, `wa_unlimited`, `wa_enterprise`; none = Basic).
2. In the desktop app's WhatsApp tab: Settings → Connection → connect a number. Send it a message from a phone; it should appear in the Inbox,
   and a reply should reach the phone. `/readyz` shows the worker running.

**Going back.** Unset `PRISM_WA_URL` on the licence server and the app is on n8n again, with no deploy. The migrations only add things, so rolling
the code back to an older deploy is safe too.

## The one-service option ($7 a month)

`render.single.yaml` is the free option on a paid size: ONE web service (0.5 CPU, 512 MB) that is both the gateway and the worker. It is what to use for the
first paying customers.

* Do steps 1–3 as written (GitHub, secrets, the first migration by hand). In step 4 use **Blueprints → New Blueprint Instance**, pick the repository and set
  **Blueprint Path** to `render.single.yaml`. You fill in seven variables. Render shows $7 a month.
* Steps 5–8 are the same. The service is called `prism-wa`, so its address is `https://prism-wa.onrender.com` (or with a suffix if the name is taken).
* **Always on:** it never sleeps, so you need no pinger. A paid service uses none of the 750 free hours a month shared by the free services of the workspace.
  Deploys have no downtime, and the migration step runs before the new version goes live: a failure leaves the old one serving.
* **How much it carries:** roughly 3–5 small business customers (about 500–1,500 incoming messages an hour; a broadcast of 1,000 people in a couple of minutes).
  The limit is the distance to the database (Supabase is in Sydney), not the CPU. When replies start arriving minutes late after a busy burst, give the
  worker its own service: delete this one and use `render.yaml` (two services, $14). Never run both; and the biggest speed-up of all is moving the Supabase
  project to Singapore.

## The free option (one service, no cost)

Render's free tier has web services but no background workers, so `render.free.yaml` runs the worker as a thread of the gateway (`WA_EMBEDDED_WORKER`) and
applies migrations when the service starts (`WA_MIGRATE_ON_START`). It is the same code and the same database as the paid setup.

* Do steps 1–3 as written (GitHub, secrets, the first migration by hand). Then in step 4 use **Blueprints → New Blueprint Instance**, pick the repository and
  set **Blueprint Path** to `render.free.yaml`. You fill in seven variables (the gateway's) instead of nine.
* Steps 5–8 are the same. The service is called `prism-wa`, so its address is `https://prism-wa.onrender.com` (or with a suffix if the name is taken).
* **A free service sleeps** after 15 minutes without a request and takes about a minute to wake. While asleep nothing is sent or scheduled; the next
  webhook or request wakes it and the backlog goes out. `/readyz` shows `"worker": "stale"` while it has been asleep.
* **Keeping it awake in working hours (free).** A scheduler such as cron-job.org (free) can visit `https://<your service>/healthz` every 10 minutes, which
  stops the sleep. Do it **only in working hours**, never 24 hours a day: the 750 free hours a month are shared by every free service in the workspace, and
  when they run out Render suspends all of them until the next month, the licence server included (nobody could activate or renew). Example: every 10
  minutes from 09:00 to 19:00 India time, Monday to Saturday, is about 260 hours, which leaves room for the licence server if it is also free. Outside
  those hours the service sleeps and the next Meta webhook or app request wakes it (a customer who writes at night waits about a minute for the wake).
  Look at Render's billing page now and then to see how many free hours the workspace has used.
* **Moving to paid later** changes nothing in the database or in Meta: delete this service, create the two in `render.yaml`, set the same variables
  (and remove `WA_EMBEDDED_WORKER` / `WA_MIGRATE_ON_START`; never run the worker in both places).

## Keeping it healthy

* **Logs** are on Render (gateway and worker, INFO). The first thing to look at when "nothing is being sent" is `/readyz`: `"worker": "stale"` means
  the worker has stopped (queued messages, campaigns, webhooks out and payment checks all wait on it).
* **Scale.** Each request opens its own database connection. If you grow past a few hundred active workspaces, put Supabase's pooler between them
  (it already is, via the session pooler) and raise the instance size before adding more of them. Add a second worker freely: workers never take the
  same message.
* **Backups** are Supabase's. Turn on point-in-time recovery for that project before the first paying WhatsApp customer.
* **Rotating a secret.** `WA_APP_SECRET` takes two values separated by a comma, so Meta's new secret can be added before the old is dropped.
  `WA_TOKEN_KEYS` rotates by adding `2:<new key>` (see section 2 below).
* **Deploys** run migrations first, then swap the service; a failed migration leaves the old version serving. Each deploy takes a minute or two.

What still needs Meta or a real account before it is proven live is listed at the bottom of this file.

---

# Reference: what each part is

The steps above are the order to do things in. Below is what each piece is, section by section. Nothing here has been run against production or
against Meta live: the code is tested against a real Postgres and a fake Graph API, and what cannot be tested without Meta is listed at the end.

## 1. The database (first, and carefully)

The gateway writes into the same Postgres as the CRM tables. Follow
`db/README.md` exactly: run `preflight.sql`, read it, then apply the migrations in
order — **0001 to 0003** (tenancy, identity, row-level security; they touch the
live CRM tables, so do them in a quiet moment) and then **0004 to 0029** (gateway,
messaging, onboarding, templates, contacts and consent, campaigns, inbox, API keys,
store integrations, the contact-merge update, ads, flows, AI agents, scheduled
and local-time campaigns, the contact book (tag and attribute catalogues, import log,
contact timeline), the campaign book, the media library and the media files themselves, a template's message
validity, what Meta reports about each number, outbound webhooks and who sent a reply, flow analytics (0025), AI agents with a
draft and a published copy, a knowledge base with embeddings (0026), automatic birthday / reminder / drip messages (0027), ads of three kinds,
audiences and Facebook sign-in (0028), and the shop, forms, audit log, single sign-on, resellers and phone-app coexistence (0029);
all new tables or additive columns).

    python db/apply.py "<postgres connection string>"          every file, in order
    python db/apply.py --pending                               only what is missing (reads DATABASE_URL); what each deploy runs
    python db/apply.py --pending --first-run                   the very first time on a database
    python db/apply.py --status                                what is applied and waiting; changes nothing

Every file is idempotent and atomic. 0004-0029 have no rollback script: they add
tables and columns only, and dropping them would delete stored events, consent
history, messages and files. 0013 and 0021 replace `wa_merge_contacts` (0021 teaches it the contact
timeline), so run them in order, after the files they build on. 0029 also adds two functions that run with the table owner's rights
(`wa_redeem_partner_code`, and `wa_child_activity` / `wa_child_name` that give a reseller message *counts* of a linked customer and nothing
else); read them before you run it. It adds a column to `agents` (`email`), `phone_numbers` (`coexistence`, `coexistence_state`).

## 2. Secrets to create

| Variable | What it is | Where it comes from |
|---|---|---|
| `DATABASE_URL` | Postgres connection string | your database |
| `WA_APP_SECRET` | the Meta app secret | developers.facebook.com -> your app -> App settings -> Basic |
| `WA_VERIFY_TOKEN` | a string Meta echoes back when you subscribe | invent one (32+ random characters) |
| `WA_INTERNAL_TOKEN` | shared with the licence server | invent one; the same value is `PRISM_WA_INTERNAL_TOKEN` there |
| `WA_TOKEN_KEYS` | encrypts customers' Meta tokens | `python -c "from prism_wa.vault import generate_key as g; print('1:'+g())"` |
| `WA_ENV` | `prod` on a live service: the gateway then refuses to start with a weak or missing secret (app secret 16+ characters, verify token 24+, internal token 32+, token keys set) and says which | set by `render.yaml` |
| `LOG_LEVEL` | `INFO` (default), `DEBUG`, `WARNING` | optional |
| `WA_EMBEDDED_WORKER`, `WA_MIGRATE_ON_START` | `yes` runs the worker inside the gateway, and applies missing migrations when it starts: for a host with no background-worker service or pre-deploy step (the free option below) | `render.free.yaml` sets them |
| `WA_MIGRATE_DATABASE_URL` | optional: the connection migrations use (Supabase session pooler, port 5432) when `DATABASE_URL` is the transaction pooler (6543) | only if you switch pooler |
| `WA_SEND_CONCURRENCY` | messages sent at once, 1–16 (default 4). Each takes a database connection for a moment (if the database is ever shared with another service, it keeps their share free) | optional |
| `WA_LEGACY_TENANT` | read by the first migration only: `none` on a new project with no old writers (Step 3A); unset = `orena`, the old CRM's tenant (Step 3B) | Step 3 |
| `WA_FIRST_RUN` | `yes` lets a deploy's migration step be the first to touch a database (see step 3). Normally not used: run `--first-run` by hand | optional, once |
| `PRISM_WA_URL`, `PRISM_WA_INTERNAL_TOKEN`, `THREADPOOL_SIZE` (licence server) | where the licence server finds this service, the shared secret, and how many requests it works on at once (default 100) | see step 7 |
| `WA_APP_ID`, `WA_ES_CONFIG_ID` | for the connect page | Meta dashboard (Embedded Signup configuration) |
| `WA_MEDIA_DIR` | optional: a folder the gateway AND the worker can both reach, for the Media Library's files | leave it unset and the files live in Postgres (`media_blobs`), which both processes already share. Set it only if you mount one shared disk |
| `META_APP_ID`, `META_APP_SECRET` | optional: "Continue with Facebook" for ads, and the check on a form's live requests | the same Meta app as `WA_APP_ID`; `META_APP_SECRET` may be the same value as `WA_APP_SECRET`. Without them the Facebook sign-in says it is not set up and the token paste still works, and live forms refuse requests |
| `PUBLIC_URL` (licence server) | the licence server's own public address | used to build the addresses other servers call: Facebook's and the company sign-in's return address, Razorpay's webhook, and Meta's live form requests. Live forms and the sign-in features refuse to start without an `https://` one |
| `WA_EMBED_API_KEY`, `WA_EMBED_BASE_URL`, `WA_EMBED_MODEL` | optional: makes AI agents find answers by meaning, not only by words | any OpenAI-compatible embeddings endpoint. Without a key the search is by words only and still works |
| `WA_PAYMENT_LOOKUP_PATH` | optional: where Meta's payment status is read from | default `{phone_number_id}/payments/{reference_id}`; change it if Meta's current documentation says otherwise |
| `WA_LLM_API_KEY` | optional: lets AI agents answer | any OpenAI-compatible provider (Groq by default). Without it every AI agent hands the chat to a person. `WA_LLM_BASE_URL` and `WA_LLM_MODEL` override the provider and model |

Keep `WA_TOKEN_KEYS` somewhere safe and separate from the database: lose it and
every stored customer token is unreadable; leak it together with the database and
they are readable. To rotate, add `2:<new key>` (keeping `1:`), restart, run
`Vault.reencrypt` over `wabas.token_enc`, then retire key 1.

## 3. Deploy

`render.yaml` describes both processes (gateway = web, worker = background) from
the one `Dockerfile` (steps 4 and 5 above). Any host that runs a container works: the gateway needs a
public HTTPS address; the worker needs none. Run one gateway and one or more
workers; workers never take the same message (`FOR UPDATE SKIP LOCKED`).

## 3a. Things that behave differently from a plain web service

* **Webhooks out.** The worker (not the gateway) delivers them, on every cycle, so the worker must be able to reach the internet and must
  stay running. A receiver that is down is retried after 1 min, 5 min, 30 min, 2 h and 6 h, and the endpoint is switched off after 20
  deliveries in a row run out of tries. Addresses must be https and must not resolve to a private address (checked when saved and again at
  every delivery, so a name that is later pointed inside the network is refused).
* **The Inbox's live updates** are a long poll: `POST /internal/inbox/changes` answers the moment a chat changes, or after up to 25
  seconds. It does not hold a gateway worker thread while it waits. The licence server's forwarding call to the gateway does hold one of
  *its* threads for that time per open Inbox, so give the licence server enough workers (about one thread per desktop that has the Inbox
  open) or the app falls back to its 15-second look-again, which still works.
* **Money figures are estimates.** The Analytics tab and Plan and usage add up the price Meta reports on each delivered message at a rate
  card (`campaigns.RATE_CARD_ESTIMATE`, 18% GST on top) that each workspace can correct. Meta's invoice is the real figure.

## 4. Tell Meta

In the app's WhatsApp configuration page:

1. **Callback URL** `https://<your gateway>/webhook/wa`, **Verify token** =
   `WA_VERIFY_TOKEN`. "Verify and save" must succeed.
2. Subscribe to the `messages` field, and the template (`message_template_status_update`,
   `message_template_quality_update`, `template_category_update`), phone
   (`phone_number_quality_update`, `phone_number_name_update`) and account
   (`account_update`, `account_alerts`, `account_review_update`) fields.
3. Until the app is **published**, Meta only sends test webhooks from the
   dashboard. Real customer messages need the app published (privacy-policy URL,
   icon, category, then App Review for advanced access).

## 5. Point the licence server at it

Set `PRISM_WA_URL` (`https://<your gateway>`) and `PRISM_WA_INTERNAL_TOKEN` on the
licence server. From then on a reply or broadcast from the Prism panel is handed
to prism-wa instead of n8n. Unset either to go back to n8n at once; no deploy.
Do this only after step 4 works and a number is connected, or sends will be
refused for want of a number.

The licence server also exposes `POST /v1/whatsapp/p/<area>/<action>` (templates,
campaigns and their schedules, segments, contacts, consent, the team inbox, flows,
AI agents, API keys, store connections, ads, a connect link). The desktop app's
WhatsApp tab uses all of them: Inbox, Contacts, Templates, Campaigns, Automations
(flows and AI agents), Ads and Settings (connect a number, team, quick replies,
stores, API keys). The licence server strips any `tenant_id`, `by_agent_id`,
`seat_key` or `plan` the client sends and fills them from the licence.

**Plans.** A workspace's WhatsApp plan comes from its licence's features:
`wa_pro`, `wa_premium`, `wa_unlimited` or `wa_enterprise` (none = Basic). Issue the
licence with the key that matches what the customer bought. The limits are in
`prism_wa/plans.py` and mirror the pricing page in the spec (§7.1).

## 5c. Stores, ads and API keys

* **Shopify / WooCommerce.** Created through the licence server's
  `integrations/create` (owner or manager only). The reply carries the webhook
  address, `https://<gateway>/hooks/<shopify|woocommerce>/<token>`; paste it into the
  store's webhook settings with the same signing secret. Each event the workspace
  turned on becomes a template message; the store's retries are deduplicated.
* **API keys.** `api_keys/create` returns the key once. Customers call
  `https://<gateway>/api/v1/...` with `Authorization: Bearer pk_live_...`.
* **Ads.** `ads/connect` stores an ad account's token encrypted; `ads/create`
  makes a paused click-to-WhatsApp ad; `ads/activate` needs an explicit confirmation.
  Not tested against Meta (see `prism_wa/ads.py`).

## 5d. Flows, AI agents and schedules

* **Order of answers.** An inbound message goes to a flow whose trigger fits, then
  to the default AI agent, then to the inbox auto-reply. A person who takes a chat
  stops both. Flows need the `flows` feature (Pro and up); AI agents need `ai_agent`
  (Premium and up) and a monthly `ai_messages` allowance.
* **Flows** keep a draft and published versions; one live run per chat. Waiting
  steps use `flow_timeout` jobs, so the worker must be running. API-request steps
  refuse private and internal addresses.
* **AI agents** answer only from their instructions and what they were taught
  (text, questions and answers, a web page, a file), found with Postgres full-text
  search. Without `WA_LLM_API_KEY` they never reply on their own.
* **Schedules** (recurring campaigns) and per-recipient local send times run from the
  worker cycle (`schedules.run_due`); a schedule that fails three times in a row turns itself off.

## 5e. The shop, forms, sign-in and resellers (migration 0029)

* **Shop.** Products are kept in Postgres and can be pushed to / pulled from a Meta catalogue. A customer's cart (an inbound `order` message)
  becomes a draft order. Payment is either Razorpay links (needs only a Razorpay account: key id, key secret and a webhook secret saved under
  Shop → Settings, and the webhook address shown there added in Razorpay for `payment_link.paid`, `payment_link.expired`,
  `payment_link.cancelled`, `payment.failed`) or Meta's own payments (needs the payment configuration approved in WhatsApp Manager). Meta's
  payment results arrive as status webhooks of type `payment`, and the **worker** checks every open payment with the gateway every few
  minutes in case a webhook was dropped, and expires the ones that ran out of time — so the worker must stay running. Gateway secrets are
  stored encrypted with `WA_TOKEN_KEYS`; the worker needs the same key as the gateway (a chatbot flow that asks for payment runs in the worker).
* **Forms.** Publishing gives the form to Meta (create, upload, publish). A *live* form (booking with free times) also needs a key pair: Prism
  makes one per workspace on first use, stores the private half encrypted, sends Meta the public half, and Meta then calls
  `https://<licence server>/v1/whatsapp/forms/endpoint/<workspace>/<form>`. That route forwards to the gateway, which checks the app secret's
  signature first, then opens the request. A request it cannot open is answered 421 so Meta fetches the key again.
* **Audit log.** Every plan records; only Enterprise can read and download. Lines older than 400 days are deleted by the worker.
* **Single sign-on** is OpenID Connect with RS256 tokens (Google Workspace, Microsoft Entra, Okta…). The owner gives the issuer, client id
  and secret; the provider's redirect address to register is `https://<licence server>/v1/whatsapp/sso/callback`. Team members are matched
  by the e-mail set on their profile. "Require it" makes every non-owner request need a live 12-hour session; the owner is exempt so a
  mistake can never lock the workspace out. SAML is not supported.
* **Resellers.** A customer workspace makes a one-time code; the reseller redeems it. The reseller sees message counts and contacts of a
  linked customer, never the contents. Creating and billing the customer's workspace stays with the licence server.
* **Coexistence.** "Connect and keep using the phone app" in Settings → Connection opens Embedded Signup in the WhatsApp Business app mode.
  The number is not re-registered; Meta is asked for the phone's contacts and the last six months of chats (allowed only in the first 24
  hours), and messages typed on the phone arrive as echoes. Subscribe the app to the `smb_message_echoes`, `history` and `smb_app_state_sync`
  fields for this.

## 5b. Connect a number

Either insert rows by hand for a number you already control (a `wabas` row, a
`phone_numbers` row, and the WABA's token encrypted with `Vault.encrypt(token,
context=<waba id>)`), or use Embedded Signup: the licence server asks
`POST /internal/onboarding/link {tenant_id}` for a link and gives it to the
customer; they connect their number on the hosted page.

## Done in code, not yet proven live

* **Embedded Signup v4's exact browser message and `config_id`.** The page and
  server flow follow the spec; the shape of the message Meta posts to the page
  and the configuration you must create in the dashboard are unchecked.
* **Meta's current status-webhook shape and the Graph API version.** The
  normaliser tolerates drift and keeps everything it cannot read in `raw`; check
  against Meta's own sample payloads (spec §9 item 4). The default version is
  `v25.0` (`WA_GRAPH_VERSION` overrides it).
* **Flows, AI agents and schedules against real traffic.** All three are tested on
  a real Postgres with a fake Graph and a fake LLM; none has answered a real
  customer, and the AI replies have not been tried with a live model.
* **The shop, forms, sign-in and coexistence against Meta, Razorpay and a real provider.** All are tested on a real Postgres with fake
  versions of Meta, Razorpay and an OpenID Connect provider that really signs tokens, and a test plays Meta for the form endpoint's
  encryption. Not proven live: the exact field names of Meta's `order_details`, catalogue batch and payment-status shapes, the Flow JSON
  version and component limits (the form checks are Prism's own and Meta's answer is final and shown beside the form), the payment
  status lookup path, and the coexistence webhook shapes. Each is written from Meta's documentation as read in October 2026.
* **Sending at scale.** The send path is tested for the 24-hour window,
  opt-out, rate limiting, retries and at-most-once behaviour against a fake
  Graph; it has not been run at campaign volume.
