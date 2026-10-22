-- 0001_tenancy.sql — every WhatsApp row belongs to exactly one tenant.
--
-- Closes the cross-tenant exposure in the Prism panel (spec §1.3 item 7):
-- contacts / conversations / messages / segments / campaigns get a NOT NULL
-- tenant_id, existing rows are attributed, and the database itself refuses a
-- row that points at another tenant's parent.
--
-- Safe to run more than once. Additive: no column or row is dropped. The only
-- things removed are the two GLOBAL unique indexes on contacts (phone, email),
-- replaced by per-tenant ones — a phone number is unique within a business,
-- not across every business on the platform.
--
-- Run in the Supabase SQL editor as one script (no BEGIN/COMMIT inside: a
-- multi-statement script is already one transaction, so a failure leaves the
-- database untouched). Read db/README.md and run preflight.sql first.
--
-- Prerequisites (already in your project): tenants (shakti_setup.sql) and the
-- legacy CRM tables (workflows/supabase_schema.sql).

-- ── 0. prerequisites ─────────────────────────────────────────────────────────
do $$
declare
  missing text;
  tags_type text;
begin
  select string_agg(t, ', ') into missing
    from unnest(array['tenants','contacts','conversations','messages','leads',
                      'follow_up_queue','activity_log']) as t
   where to_regclass('public.' || t) is null;
  if missing is not null then
    raise exception '0001_tenancy: these tables are missing: %. Run the legacy schema first.', missing;
  end if;

  select data_type into tags_type from information_schema.columns
   where table_schema = 'public' and table_name = 'contacts' and column_name = 'tags';
  if tags_type is not null and tags_type <> 'ARRAY' then
    raise exception '0001_tenancy: contacts.tags exists as % but the WhatsApp add-on needs text[]. Convert it first.', tags_type;
  end if;
end $$;

-- ── 1. platform config + the tenant that owns today's data ───────────────────
create table if not exists wa_platform_config (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);
alter table wa_platform_config enable row level security;

-- The slug of the tenant that owns today's rows is 'orena' unless the runner says otherwise: db/apply.py sets the session setting wa.legacy_slug from
-- the environment variable WA_LEGACY_TENANT. Use another slug if your own business's tenant row has one (select id, slug, name from tenants; —
-- preflight.sql lists them). Use 'none' on a NEW project with no old writers: no tenant is made, and every row must name its workspace (strict mode).
do $$
declare
  legacy_slug text := coalesce(nullif(current_setting('wa.legacy_slug', true), ''), 'orena');
  legacy_id   uuid;
begin
  if legacy_slug <> 'none' then
    insert into tenants (slug, name) values (legacy_slug, initcap(legacy_slug))
      on conflict (slug) do nothing;
    select id into legacy_id from tenants where slug = legacy_slug;
    -- Legacy writers (the n8n CRM flows) do not send a tenant. Rows they create
    -- are attributed to this tenant. Delete this config row to make tenant_id
    -- mandatory for every writer ("strict mode") once they all send it.
    insert into wa_platform_config (key, value)
      values ('legacy_default_tenant', legacy_id::text)
      on conflict (key) do nothing;
  end if;
end $$;

create or replace function wa_default_tenant() returns uuid
language sql stable as
$$ select nullif(value, '')::uuid from wa_platform_config where key = 'legacy_default_tenant' $$;

-- Meta's wa_id is a phone number as digits with country code and no '+'.
-- Digits-only is the canonical form for identity; parsing "9726063133" or
-- "097260 63133" into a country is the importer's job (it knows the region).
create or replace function wa_norm_phone(p text) returns text
language sql immutable parallel safe as
$$ select nullif(regexp_replace(coalesce(p, ''), '\D', '', 'g'), '') $$;

-- ── 2. columns ───────────────────────────────────────────────────────────────
alter table contacts      add column if not exists tenant_id   uuid references tenants(id);
alter table contacts      add column if not exists tags        text[] not null default '{}';
alter table contacts      add column if not exists bsuid       text;   -- latest business-scoped user id
alter table contacts      add column if not exists wa_username text;   -- display only; mutable, never a key
alter table conversations add column if not exists tenant_id   uuid references tenants(id);
alter table messages      add column if not exists tenant_id   uuid references tenants(id);
alter table leads         add column if not exists tenant_id   uuid references tenants(id);

create table if not exists segments (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid references tenants(id),
  name       text not null,
  filter     jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create table if not exists campaigns (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid references tenants(id),
  template_name text not null,
  language      text not null default 'en_US',
  segment_name  text not null default '',
  total         integer not null default 0,
  status        text not null default 'sending',
  created_at    timestamptz not null default now()
);
-- They may pre-exist from an earlier hand-made version: make sure every column
-- the add-on reads is there.
alter table segments  add column if not exists tenant_id uuid references tenants(id);
alter table segments  add column if not exists filter    jsonb not null default '{}'::jsonb;
alter table segments  add column if not exists created_at timestamptz not null default now();
alter table campaigns add column if not exists created_at    timestamptz not null default now();
alter table campaigns add column if not exists tenant_id     uuid references tenants(id);
alter table campaigns add column if not exists language      text not null default 'en_US';
alter table campaigns add column if not exists segment_name  text not null default '';
alter table campaigns add column if not exists total         integer not null default 0;
alter table campaigns add column if not exists status        text not null default 'sending';

-- ── 3. backfill (idempotent; touches only rows still without a tenant) ───────
-- User triggers are switched off so the one-off attribution does not bump
-- updated_at on every legacy row and erase "last modified".
alter table contacts      disable trigger user;
alter table conversations disable trigger user;
alter table leads         disable trigger user;

do $$
declare legacy uuid := wa_default_tenant();
begin
  if legacy is null then
    -- strict mode (WA_LEGACY_TENANT=none, a new empty project): fine while no row needs a tenant; if one does, there is nobody to give it to.
    if exists (select 1 from contacts where tenant_id is null) or exists (select 1 from segments where tenant_id is null)
       or exists (select 1 from campaigns where tenant_id is null) then
      raise exception '0001_tenancy: some rows have no tenant and no legacy default tenant is configured: set WA_LEGACY_TENANT to the slug of the tenant that owns them';
    end if;
    return;
  end if;
  update contacts      set tenant_id = legacy where tenant_id is null;
  update conversations c set tenant_id = k.tenant_id
    from contacts k where c.contact_id = k.id and c.tenant_id is null;
  update messages m set tenant_id = c.tenant_id
    from conversations c where m.conversation_id = c.id and m.tenant_id is null;
  update leads l set tenant_id = k.tenant_id
    from contacts k where l.contact_id = k.id and l.tenant_id is null;
  update segments      set tenant_id = legacy where tenant_id is null;
  update campaigns     set tenant_id = legacy where tenant_id is null;
end $$;

alter table contacts      enable trigger user;
alter table conversations enable trigger user;
alter table leads         enable trigger user;

-- ── 4. uniqueness is per tenant ──────────────────────────────────────────────
create unique index if not exists contacts_tenant_phone_idx
  on contacts (tenant_id, phone) where phone is not null;
create unique index if not exists contacts_tenant_email_idx
  on contacts (tenant_id, lower(email)) where email is not null;
drop index if exists contacts_phone_idx;
drop index if exists contacts_email_idx;

create unique index if not exists segments_tenant_name_idx
  on segments (tenant_id, lower(name));

-- A contact reached by username/BSUID alone has neither email nor phone; the
-- resolver (0002) guarantees at least one identity instead.
alter table contacts drop constraint if exists contact_has_identifier;

-- ── 5. indexes for tenant-scoped reads ───────────────────────────────────────
create index if not exists contacts_tenant_name_idx     on contacts (tenant_id, name);
create index if not exists contacts_tags_gin            on contacts using gin (tags);
create index if not exists contacts_tenant_normphone_idx
  on contacts (tenant_id, wa_norm_phone(phone)) where phone is not null;
create index if not exists conversations_tenant_active_idx
  on conversations (tenant_id, last_active_at desc);
create index if not exists messages_tenant_conv_idx     on messages (tenant_id, conversation_id, created_at);
create index if not exists campaigns_tenant_created_idx on campaigns (tenant_id, created_at desc);

-- ── 6. the database enforces it, whoever writes ──────────────────────────────
-- Root rows (contacts, segments, campaigns): a missing tenant falls back to the
-- legacy default, or is refused when strict mode is on.
create or replace function wa_tg_fill_default_tenant() returns trigger
language plpgsql as $$
begin
  if new.tenant_id is null then
    new.tenant_id := wa_default_tenant();
  end if;
  if new.tenant_id is null then
    raise exception '%.tenant_id is required', tg_table_name using errcode = '23502';
  end if;
  return new;
end $$;

-- Child rows inherit from, and must agree with, their parent. This is what
-- stops a conversation being filed under one tenant while pointing at another
-- tenant's contact — the failure that made two tenants' data bleed together.
create or replace function wa_tg_conversations_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from contacts where id = new.contact_id;
  if parent is null then
    raise exception 'conversation refers to an unknown contact' using errcode = '23503';
  end if;
  if new.tenant_id is null then
    new.tenant_id := parent;
  elsif new.tenant_id <> parent then
    raise exception 'conversation tenant does not match its contact''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;

create or replace function wa_tg_messages_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from conversations where id = new.conversation_id;
  if parent is null then
    raise exception 'message refers to an unknown conversation' using errcode = '23503';
  end if;
  if new.tenant_id is null then
    new.tenant_id := parent;
  elsif new.tenant_id <> parent then
    raise exception 'message tenant does not match its conversation''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;

-- A row never quietly changes tenant. Re-attributing on purpose (fixing a
-- mis-filed import) is possible: set wa.allow_tenant_change = on for the session.
create or replace function wa_tg_tenant_immutable() returns trigger
language plpgsql as $$
begin
  if old.tenant_id is not null and new.tenant_id is distinct from old.tenant_id
     and coalesce(current_setting('wa.allow_tenant_change', true), '') <> 'on' then
    raise exception '%.tenant_id cannot change (set wa.allow_tenant_change=on to re-attribute deliberately)',
      tg_table_name using errcode = '23514';
  end if;
  return new;
end $$;

drop trigger if exists wa_tenant_default on contacts;
create trigger wa_tenant_default before insert on contacts
  for each row execute function wa_tg_fill_default_tenant();
drop trigger if exists wa_tenant_default on segments;
create trigger wa_tenant_default before insert on segments
  for each row execute function wa_tg_fill_default_tenant();
drop trigger if exists wa_tenant_default on campaigns;
create trigger wa_tenant_default before insert on campaigns
  for each row execute function wa_tg_fill_default_tenant();

drop trigger if exists wa_tenant_parent on conversations;
create trigger wa_tenant_parent before insert or update of contact_id, tenant_id on conversations
  for each row execute function wa_tg_conversations_tenant();
drop trigger if exists wa_tenant_parent on messages;
create trigger wa_tenant_parent before insert or update of conversation_id, tenant_id on messages
  for each row execute function wa_tg_messages_tenant();

drop trigger if exists wa_tenant_immutable on contacts;
create trigger wa_tenant_immutable before update of tenant_id on contacts
  for each row execute function wa_tg_tenant_immutable();
drop trigger if exists wa_tenant_immutable on conversations;
create trigger wa_tenant_immutable before update of tenant_id on conversations
  for each row execute function wa_tg_tenant_immutable();
drop trigger if exists wa_tenant_immutable on messages;
create trigger wa_tenant_immutable before update of tenant_id on messages
  for each row execute function wa_tg_tenant_immutable();
drop trigger if exists wa_tenant_immutable on segments;
create trigger wa_tenant_immutable before update of tenant_id on segments
  for each row execute function wa_tg_tenant_immutable();
drop trigger if exists wa_tenant_immutable on campaigns;
create trigger wa_tenant_immutable before update of tenant_id on campaigns
  for each row execute function wa_tg_tenant_immutable();

-- ── 7. seal: no row without a tenant ─────────────────────────────────────────
alter table contacts      alter column tenant_id set not null;
alter table conversations alter column tenant_id set not null;
alter table messages      alter column tenant_id set not null;
alter table segments      alter column tenant_id set not null;
alter table campaigns     alter column tenant_id set not null;
