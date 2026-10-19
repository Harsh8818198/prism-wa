-- 0004_gateway.sql — what the webhook gateway (spec WS2) writes into.
--
-- Four tables:
--   wabas, phone_numbers  who a webhook belongs to (phone_number_id -> waba ->
--                         tenant). Filled by Embedded Signup (WS0) later; until
--                         then rows are inserted by hand for a test number.
--   wa_events             every normalised webhook event, raw fragment kept, one
--                         row per event, unique on dedupe_key so a redelivery
--                         is stored once.
--   wa_jobs               the work queue (FOR UPDATE SKIP LOCKED). The gateway
--                         only ever ENQUEUES; nothing is processed inline.
--
-- An event for a number we do not know yet is still stored (tenant_id NULL):
-- Meta will not resend it, and once the number is onboarded it can be replayed.
-- Such rows are invisible to a tenant-scoped session.
--
-- Safe to run more than once. Additive: nothing is dropped. Run after 0003.

do $$
begin
  if to_regclass('public.tenants') is null then
    raise exception '0004_gateway: tenants is missing. Run the legacy schema and 0001-0003 first.';
  end if;
  if to_regprocedure('wa_current_tenant()') is null then
    raise exception '0004_gateway: wa_current_tenant() is missing. Run 0003_rls.sql first.';
  end if;
end $$;

-- ── who a webhook belongs to ─────────────────────────────────────────────────
create table if not exists wabas (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  waba_id          text not null unique,          -- Meta's WhatsApp Business Account id
  token_enc        bytea,                         -- envelope-encrypted customer-scoped token (WS17); never plaintext
  key_ver          integer,
  webhook_override text,                          -- per-WABA override_callback_uri, if one is set
  quality          text,
  tier             text,
  created_at       timestamptz not null default now()
);
create index if not exists wabas_tenant_idx on wabas (tenant_id);

create table if not exists phone_numbers (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  waba_id         uuid not null references wabas(id) on delete cascade,
  phone_number_id text not null unique,           -- Meta's id, not the phone number
  display         text,
  status          text,
  quality         text,
  mps_limit       integer not null default 80,    -- messages per second; Meta's default
  created_at      timestamptz not null default now()
);
create index if not exists phone_numbers_tenant_idx on phone_numbers (tenant_id);

-- A phone number's tenant must be its WABA's tenant: one row cannot file a
-- number under tenant A while hanging it off tenant B's account.
create or replace function wa_tg_phone_numbers_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from wabas where id = new.waba_id;
  if parent is null then
    raise exception 'phone number refers to an unknown WABA' using errcode = '23503';
  end if;
  if new.tenant_id <> parent then
    raise exception 'phone number tenant does not match its WABA''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;
drop trigger if exists wa_phone_numbers_tenant on phone_numbers;
create trigger wa_phone_numbers_tenant before insert or update of tenant_id, waba_id
  on phone_numbers for each row execute function wa_tg_phone_numbers_tenant();

-- ── events ───────────────────────────────────────────────────────────────────
create table if not exists wa_events (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid references tenants(id),    -- NULL = arrived for a number we do not know (yet)
  dedupe_key      text not null unique,           -- wamid / wamid+status+time / hash of the change
  kind            text not null,                  -- message | status | template_status | phone_quality | ...
  waba_id         text,
  phone_number_id text,
  occurred_at     timestamptz,                    -- Meta's timestamp, when it sent one
  canonical       jsonb not null,                 -- the normalised fields (see prism_wa/events.py)
  raw             jsonb not null,                 -- exactly what Meta sent for this event
  received_at     timestamptz not null default now()
);
create index if not exists wa_events_tenant_received_idx on wa_events (tenant_id, received_at desc);
create index if not exists wa_events_number_received_idx on wa_events (phone_number_id, received_at);
create index if not exists wa_events_unrouted_idx on wa_events (phone_number_id) where tenant_id is null;

-- ── queue ────────────────────────────────────────────────────────────────────
create table if not exists wa_jobs (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid references tenants(id),
  kind       text not null,
  payload    jsonb not null default '{}'::jsonb,
  idem_key   text not null unique,                -- enqueueing the same work twice is a no-op
  run_at     timestamptz not null default now(),
  attempts   integer not null default 0,
  state      text not null default 'queued' check (state in ('queued', 'running', 'done', 'dead')),
  locked_by  text,
  locked_at  timestamptz,
  error      text,
  created_at timestamptz not null default now()
);
create index if not exists wa_jobs_ready_idx on wa_jobs (run_at) where state = 'queued';
create index if not exists wa_jobs_tenant_idx on wa_jobs (tenant_id, created_at desc);

-- ── a tenant-scoped session sees only its own rows (see 0003) ────────────────
do $$
declare t text;
begin
  foreach t in array array['wabas', 'phone_numbers', 'wa_events', 'wa_jobs']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists wa_tenant_isolation on %I', t);
    execute format(
      'create policy wa_tenant_isolation on %I for all to wa_app '
      'using (tenant_id = wa_current_tenant()) '
      'with check (tenant_id = wa_current_tenant())', t);
    execute format('grant select, insert, update, delete on %I to wa_app', t);
  end loop;
end $$;

-- Like every other table in this project, none of these is reachable through
-- the public publishable key. wabas holds (encrypted) Meta tokens.
revoke all on table wabas, phone_numbers, wa_events, wa_jobs from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table wabas, phone_numbers, wa_events, wa_jobs to service_role;
  end if;
end $$;
