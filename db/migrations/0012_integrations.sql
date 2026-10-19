-- 0012_integrations.sql — Shopify and WooCommerce stores that message customers (spec WS14).
--
--   integrations        one connected store: its public webhook token, its webhook signing secret
--                       (encrypted, never plaintext), and what to send for which event
--   integration_events  every webhook received, once: the dedupe key (a retry from the store is a
--                       no-op) and an audit of what became of it
--   cart_checkouts      a started-but-not-finished checkout waiting out its delay before a reminder;
--                       an order for the same checkout turns it to "converted" and the reminder never goes
--
-- Safe to run more than once. Additive. Run after 0011.

do $$
begin
  if to_regclass('public.api_keys') is null then
    raise exception '0012_integrations: api_keys is missing. Run 0011_api_keys.sql first.';
  end if;
end $$;

create table if not exists integrations (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  kind          text not null check (kind in ('shopify', 'woocommerce')),
  name          text not null check (length(name) between 1 and 80),
  token         text not null unique,                 -- the secret part of the webhook URL: /hooks/<kind>/<token>
  secret_enc    bytea not null,                       -- the store's webhook signing secret, encrypted (vault.py)
  key_ver       integer,
  config        jsonb not null default '{}'::jsonb,
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  last_event_at timestamptz
);
create index if not exists integrations_tenant_idx on integrations (tenant_id);

create table if not exists integration_events (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id),
  integration_id uuid not null references integrations(id) on delete cascade,
  topic          text not null,
  external_id    text not null,
  status         text not null default 'received',    -- received | queued | skipped | ignored | converted
  detail         jsonb,
  received_at    timestamptz not null default now(),
  unique (integration_id, topic, external_id)
);
create index if not exists integration_events_recent_idx on integration_events (integration_id, received_at desc);

create table if not exists cart_checkouts (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id),
  integration_id uuid not null references integrations(id) on delete cascade,
  checkout_id    text not null,
  contact_id     uuid references contacts(id) on delete cascade,
  payload        jsonb not null,
  due_at         timestamptz not null,
  state          text not null default 'waiting' check (state in ('waiting', 'sent', 'converted', 'skipped')),
  created_at     timestamptz not null default now(),
  unique (integration_id, checkout_id)
);
create index if not exists cart_checkouts_due_idx on cart_checkouts (due_at) where state = 'waiting';

do $$
declare t text;
begin
  foreach t in array array['integrations', 'integration_events', 'cart_checkouts']
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

revoke all on table integrations, integration_events, cart_checkouts from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table integrations, integration_events, cart_checkouts to service_role;
  end if;
end $$;
