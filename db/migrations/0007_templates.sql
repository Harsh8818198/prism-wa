-- 0007_templates.sql — message templates (spec WS4).
--
-- A template is created on Meta, reviewed there, and only then may a business
-- start a conversation with it. This table is our copy: what we submitted, what
-- Meta's status webhooks and a periodic sync say about it now. Broadcasts check
-- it (a template that is not APPROVED is never sent).
--
-- Safe to run more than once. Additive. Run after 0006.

do $$
begin
  if to_regclass('public.wabas') is null then
    raise exception '0007_templates: wabas is missing. Run 0004_gateway.sql first.';
  end if;
end $$;

create table if not exists templates (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  waba_id       uuid not null references wabas(id) on delete cascade,
  meta_id       text,                                   -- Meta's template id, once it has one
  name          text not null,
  language      text not null,
  category      text not null check (category in ('MARKETING', 'UTILITY', 'AUTHENTICATION')),
  status        text not null default 'DRAFT',          -- DRAFT | PENDING | APPROVED | REJECTED | PAUSED | DISABLED | ...
  quality       text,                                   -- GREEN | YELLOW | RED | UNKNOWN
  components    jsonb not null default '[]'::jsonb,
  reject_reason text,
  last_error    jsonb,                                  -- why the last submit to Meta failed, if it did
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (waba_id, name, language)
);
create index if not exists templates_tenant_status_idx on templates (tenant_id, status);
create index if not exists templates_meta_idx on templates (waba_id, meta_id) where meta_id is not null;

create or replace function wa_tg_templates_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from wabas where id = new.waba_id;
  if parent is null then
    raise exception 'template refers to an unknown WABA' using errcode = '23503';
  end if;
  if new.tenant_id <> parent then
    raise exception 'template tenant does not match its WABA''s tenant' using errcode = '23514';
  end if;
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists wa_templates_tenant on templates;
create trigger wa_templates_tenant before insert or update on templates
  for each row execute function wa_tg_templates_tenant();

alter table templates enable row level security;
drop policy if exists wa_tenant_isolation on templates;
create policy wa_tenant_isolation on templates for all to wa_app
  using (tenant_id = wa_current_tenant())
  with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on templates to wa_app;

revoke all on table templates from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table templates to service_role;
  end if;
end $$;
