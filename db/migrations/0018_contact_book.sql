-- 0018_contact_book.sql — the Contacts screen as a CRM (gap analysis, Contacts and Inbox).
--
--   tag_defs       tags a workspace has made on purpose (a tag can exist before anyone has it)
--   import_log     every CSV import: what it did and why rows failed, downloadable later
--   contact_events what happened to a person: tag added, attribute changed, opted in...
--                  (with the chat events and campaigns sent, this is the Customer Journey)
--
-- Safe to run more than once. Additive. Run after 0017.

do $$
begin
  if to_regclass('public.campaign_schedules') is null then
    raise exception '0018_contact_book: campaign_schedules is missing. Run 0017_scheduling.sql first.';
  end if;
end $$;

create table if not exists tag_defs (
  tenant_id  uuid not null references tenants(id),
  name       text not null check (length(name) between 1 and 60),
  color      text,
  created_at timestamptz not null default now(),
  primary key (tenant_id, name)
);

create table if not exists import_log (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  file_name   text,
  source      text,
  by_name     text,
  rows        integer not null default 0,
  created     integer not null default 0,
  updated     integer not null default 0,
  invalid     integer not null default 0,
  report      jsonb not null default '{}'::jsonb,        -- errors (row + reason), warnings, consent counts
  at          timestamptz not null default now()
);
create index if not exists import_log_tenant_idx on import_log (tenant_id, at desc);

create table if not exists contact_events (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id),
  contact_id uuid not null references contacts(id) on delete cascade,
  kind       text not null,                              -- created | imported | tag_added | tag_removed | attribute_set | blocked | unblocked | note
  actor_id   uuid references agents(id) on delete set null,
  actor_name text,
  detail     jsonb,
  at         timestamptz not null default now()
);
create index if not exists contact_events_contact_idx on contact_events (contact_id, at desc);

do $$
declare t text;
begin
  foreach t in array array['tag_defs', 'import_log', 'contact_events'] loop
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
end $$;
