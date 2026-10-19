-- 0024_usage_webhooks.sql — what Meta says about a number, and outbound webhooks.
--
--   messages.sent_by       the teammate who typed a reply (for the team report)
--   phone_numbers.messaging_tier / name_status / verified_name / throughput / limits_checked_at / business_profile
--                          what Meta last told us about the number: how many people it may start chats with in a day, whether its display name
--                          is approved, how fast it may send; and the business profile (about, address, website...) as last read or saved
--   webhook_endpoints      a URL a workspace wants told when something happens (a message, a status, a hand-off, a campaign ending...)
--   webhook_deliveries     each telling: the event, the body sent, how many tries, and how the last one went (the log, and what is retried)
--
-- (The usage ledger needs no table of its own: Meta's `pricing` on each message status is already kept on messages.pricing.)
--
-- Safe to run more than once. Additive. Run after 0023.

do $$
begin
  if to_regclass('public.phone_numbers') is null or to_regclass('public.agents') is null then
    raise exception '0024_usage_webhooks: run the earlier migrations first.';
  end if;
end $$;

alter table phone_numbers add column if not exists messaging_tier     text;
alter table phone_numbers add column if not exists name_status        text;
alter table phone_numbers add column if not exists verified_name      text;
alter table phone_numbers add column if not exists throughput         text;
alter table phone_numbers add column if not exists limits_checked_at  timestamptz;
alter table phone_numbers add column if not exists business_profile   jsonb;

-- who sent a reply: the reports count what each teammate did
alter table messages add column if not exists sent_by uuid references agents(id) on delete set null;
create index if not exists messages_sent_by_idx on messages (sent_by, created_at) where sent_by is not null;

-- the Inbox asks "has anything changed?" about once a second while it is open: these keep that cheap
create index if not exists conversations_tenant_active_idx on conversations (tenant_id, last_active_at desc);
create index if not exists conversation_events_tenant_at_idx on conversation_events (tenant_id, at desc);

create table if not exists webhook_endpoints (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  url              text not null,
  description      text,
  secret           text not null,                         -- signs every body (HMAC-SHA256); shown once, when the endpoint is made
  events           text[] not null default '{}',
  enabled          boolean not null default true,
  failure_count    integer not null default 0,            -- deliveries that ran out of tries in a row; the endpoint is switched off at 20
  last_status      integer,
  last_error       text,
  last_delivery_at timestamptz,
  disabled_reason  text,
  created_by       uuid references agents(id) on delete set null,
  created_at       timestamptz not null default now()
);
create index if not exists webhook_endpoints_tenant_idx on webhook_endpoints (tenant_id);

create table if not exists webhook_deliveries (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  endpoint_id     uuid not null references webhook_endpoints(id) on delete cascade,
  event           text not null,
  payload         jsonb not null,
  state           text not null default 'pending' check (state in ('pending', 'delivered', 'failed')),
  attempts        integer not null default 0,
  next_at         timestamptz not null default now(),
  response_status integer,
  error           text,
  created_at      timestamptz not null default now(),
  delivered_at    timestamptz
);
create index if not exists webhook_deliveries_due_idx on webhook_deliveries (next_at) where state = 'pending';
create index if not exists webhook_deliveries_endpoint_idx on webhook_deliveries (endpoint_id, created_at desc);

do $$
declare t text;
begin
  foreach t in array array['webhook_endpoints', 'webhook_deliveries'] loop
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
