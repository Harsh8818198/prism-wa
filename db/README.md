# Database migrations (WS1 — tenancy and identity)

Three idempotent SQL files, run in order, each pasted into the **Supabase SQL
editor as one script** (or `python db/apply.py "<dsn>"`). Each is atomic: if any
statement fails the database is left exactly as it was.

| File | What it does |
|---|---|
| `0001_tenancy.sql` | `tenant_id` on contacts / conversations / messages / segments / campaigns, existing rows attributed, per-tenant uniqueness, triggers that refuse cross-tenant links, `NOT NULL` seal |
| `0002_identity.sql` | `contact_identities` (phone / bsuid / parent_bsuid), `wa_resolve_contact()`, `wa_merge_contacts()`, audit trail |
| `0003_rls.sql` | `wa_app` role + row-level security so a tenant-scoped session cannot read another tenant |

`preflight.sql` (read-only) and `rollback/0001_0003_down.sql` (removes enforcement,
keeps every row) sit alongside.

## Order of operations in production

0. **Back up the tables first**: run `backup_before_first_run.sql` (copies of what 0001–0003 change; the free Supabase plan has no backups).
1. **Preflight.** Run `preflight.sql`, read the JSON. Check: `tenants` (is there a
   row for *your own* business? `0001` assumes slug `orena` — edit the constant at
   the top of the second block in `0001` if yours differs), `rows_without_tenant`,
   `contacts_by_tenant` (any Shakti contacts stuck in `null` would be attributed to
   your business), `name_collisions` (must be `[]`: a table prism-wa would create that already exists), `phones_that_normalise_to_the_same_number`,
   `contacts_tags_type` (must be `ARRAY` or null).
2. **Run 0001, then 0002, then 0003.** This takes a lock on the CRM tables for a
   few seconds; n8n writes wait, then continue.
3. **Then deploy the licence server** (its new code reads the new columns, so the
   database must come first). Render runs `alembic upgrade head` on deploy.
4. **Link each licence to its workspace** — until you do, the WhatsApp panel says
   *"This licence isn't connected to a WhatsApp workspace yet"*. That refusal is
   deliberate (there is no default workspace). Do 3 and 4 back to back:

   ```
   curl -X POST https://<licence-server>/admin/licenses/<license_id>/whatsapp-tenant \
        -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
        -d '{"tenant_id":"<tenants.id>","reason":"onboarding"}'
   ```
5. **Check**: `select * from wa_backfill_identities();` (identities added, conflicts),
   `select * from contact_identity_conflicts;` (duplicate contacts to review —
   never auto-merged by the migration).

## Things to know

* **Legacy writers keep working.** The n8n CRM flows do not send a tenant; their
  rows are attributed to the `legacy_default_tenant` (a row in `wa_platform_config`).
  Delete that row to make `tenant_id` mandatory ("strict mode") once every writer
  sends it.
* **The n8n flows still key contacts on phone.** They are not tenant-aware and do
  not use `wa_resolve_contact()` yet, so username-only users are not handled there
  and, if two tenants ever share a phone number, the flow's "fetch the contact"
  step can pick the wrong one. Replacing that step with one call to
  `POST /rest/v1/rpc/wa_resolve_contact` is the next change; nothing else needs to
  move first.
* **Not yet tenant-scoped:** `tasks`, `follow_up_queue`, `activity_log`,
  `qualification_events` (the legacy sales-pipeline tables; only reachable with
  the service key). `leads.tenant_id` is backfilled but not enforced.
* **RLS protects against bugs, not against a compromised service** — a process
  holding `wa_app` credentials can set `app.tenant_id` to anything. The licence
  server (service_role, bypasses RLS) scopes in code and is tested to.
* **Audiences are capped at 2,000 contacts** in the licence-server proxy
  (pre-existing; the campaign engine, WS7, replaces it).

## Running the tests

```
pip install -r requirements-dev.txt
PG_BIN=<folder containing initdb, pg_ctl, postgres> python -m pytest tests
```

The tests start a private throwaway Postgres in a temp folder (or use
`PRISM_WA_TEST_ADMIN_DSN` for a disposable server — never Supabase; the harness
refuses it). Without either they **skip with a message saying so** rather than
passing silently.
