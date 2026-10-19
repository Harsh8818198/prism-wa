-- 0027_autos.sql — automatic messages: a message on a date a contact has (a birthday, an appointment, a due payment) and drip sequences.
--
--   autos             what to send and when: kind 'date' (a template on, before or after a date held in a contact attribute) or 'drip' (a series of templates
--                     spaced out after something happens to a contact)
--   auto_log          every message an automatic message sent or skipped for a contact, once per (automatic message, contact, occasion)
--   auto_enrolments   where each contact is in a drip: the next step, when, and why it ended
--
-- Safe to run more than once. Additive. Run after 0026.

do $$
begin
  if to_regclass('public.contacts') is null or to_regclass('public.contact_attributes') is null then
    raise exception '0027_autos: contacts are missing. Run 0008_contacts.sql first.';
  end if;
end $$;

create table if not exists autos (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id),
  kind         text not null check (kind in ('date', 'drip')),
  name         text not null,
  enabled      boolean not null default false,
  config       jsonb not null,
  next_run_at  timestamptz,                                  -- date kind: the next daily check
  last_run_at  timestamptz,
  runs         integer not null default 0,
  last_error   text,
  created_by   uuid,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists autos_due_idx on autos (next_run_at) where enabled and kind = 'date';
create index if not exists autos_tenant_idx on autos (tenant_id, kind);

create table if not exists auto_log (
  id          bigserial primary key,
  tenant_id   uuid not null references tenants(id),
  auto_id     uuid not null references autos(id) on delete cascade,
  contact_id  uuid not null references contacts(id) on delete cascade,
  occasion    text not null,                                 -- "2026" for a yearly date, the date itself for a one-off, "step-1" for a drip step
  state       text not null check (state in ('sent', 'skipped', 'failed')),
  reason      text,
  at          timestamptz not null default now(),
  unique (auto_id, contact_id, occasion)
);
create index if not exists auto_log_auto_idx on auto_log (auto_id, at desc);

create table if not exists auto_enrolments (
  auto_id      uuid not null references autos(id) on delete cascade,
  contact_id   uuid not null references contacts(id) on delete cascade,
  tenant_id    uuid not null references tenants(id),
  state        text not null default 'active' check (state in ('active', 'done', 'stopped')),
  step         integer not null default 0,                   -- the next step to send
  next_at      timestamptz,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  reason       text,
  primary key (auto_id, contact_id)
);
create index if not exists auto_enrolments_due_idx on auto_enrolments (next_at) where state = 'active';

do $$
declare t text;
begin
  foreach t in array array['autos', 'auto_log', 'auto_enrolments'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists wa_tenant_isolation on %I', t);
    execute format('create policy wa_tenant_isolation on %I for all to wa_app'
                   ' using (tenant_id = wa_current_tenant()) with check (tenant_id = wa_current_tenant())', t);
    execute format('grant select, insert, update, delete on %I to wa_app', t);
    execute format('revoke all on table %I from anon, authenticated', t);
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant all on table %I to service_role', t);
    end if;
  end loop;
  execute 'grant usage, select on sequence auto_log_id_seq to wa_app';
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant usage, select on sequence auto_log_id_seq to service_role';
  end if;
end $$;
