-- 0008_contacts.sql — contacts as a CRM (spec WS5): attributes, consent, settings.
--
--   attribute_defs    the custom fields a workspace has defined (typed)
--   contact_attributes the values, one row per contact and key
--   consent_ledger    every opt-in and opt-out with where it came from and the
--                     evidence — the record to show if anyone asks "why did you
--                     message me?". contacts.opt_out / opted_in are the CURRENT
--                     answer; this is the history behind it.
--   tenant_settings   per-workspace configuration (opt-in/out keywords and their
--                     replies today; business hours and auto-replies later)
--   contacts          + opted_in, blocked, first_msg_tag, last_seen
--
-- Safe to run more than once. Additive. Run after 0007.

do $$
begin
  if to_regclass('public.templates') is null then
    raise exception '0008_contacts: templates is missing. Run 0007_templates.sql first.';
  end if;
end $$;

alter table contacts add column if not exists opted_in      boolean not null default false;
alter table contacts add column if not exists blocked       boolean not null default false;
alter table contacts add column if not exists first_msg_tag text;
alter table contacts add column if not exists last_seen     timestamptz;

create table if not exists attribute_defs (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id),
  key        text not null check (key ~ '^[A-Za-z][A-Za-z0-9_]{0,39}$'),
  type       text not null default 'string' check (type in ('string', 'number', 'date', 'bool', 'enum')),
  is_global  boolean not null default false,        -- one value for the whole workspace (e.g. company name)
  options    jsonb,                                  -- the allowed values of an enum
  created_at timestamptz not null default now(),
  unique (tenant_id, key)
);

create table if not exists contact_attributes (
  contact_id uuid not null references contacts(id) on delete cascade,
  tenant_id  uuid not null references tenants(id),
  key        text not null,
  value      jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (contact_id, key),
  foreign key (tenant_id, key) references attribute_defs (tenant_id, key) on delete cascade
);
create index if not exists contact_attributes_tenant_key_idx on contact_attributes (tenant_id, key);

create table if not exists consent_ledger (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id),
  contact_id uuid not null references contacts(id) on delete cascade,
  action     text not null check (action in ('opt_in', 'opt_out')),
  source     text not null,                          -- csv_import | keyword | button | meta_131050 | manual | api | form
  evidence   text,                                   -- the words they sent, the file name, who clicked
  at         timestamptz not null default now()
);
create index if not exists consent_ledger_contact_idx on consent_ledger (contact_id, at desc);
create index if not exists consent_ledger_tenant_idx on consent_ledger (tenant_id, at desc);

create table if not exists tenant_settings (
  tenant_id  uuid primary key references tenants(id),
  settings   jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

-- a child row must agree with its contact's tenant (same guard as conversations)
create or replace function wa_tg_contact_child_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from contacts where id = new.contact_id;
  if parent is null then
    raise exception '% refers to an unknown contact', tg_table_name using errcode = '23503';
  end if;
  if new.tenant_id <> parent then
    raise exception '% tenant does not match its contact''s tenant', tg_table_name using errcode = '23514';
  end if;
  return new;
end $$;
drop trigger if exists wa_contact_attributes_tenant on contact_attributes;
create trigger wa_contact_attributes_tenant before insert or update on contact_attributes
  for each row execute function wa_tg_contact_child_tenant();
drop trigger if exists wa_consent_ledger_tenant on consent_ledger;
create trigger wa_consent_ledger_tenant before insert or update on consent_ledger
  for each row execute function wa_tg_contact_child_tenant();

do $$
declare t text;
begin
  foreach t in array array['attribute_defs', 'contact_attributes', 'consent_ledger', 'tenant_settings']
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

revoke all on table attribute_defs, contact_attributes, consent_ledger, tenant_settings from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table attribute_defs, contact_attributes, consent_ledger, tenant_settings to service_role;
  end if;
end $$;
