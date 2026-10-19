-- 0005_messaging.sql — what the ingest worker and the send engine write to.
--
--   messages       + wamid, type, delivery status, error, pricing, media, reply-to,
--                    client_ref (idempotent outbound). Unique per tenant so a
--                    status webhook finds exactly one row.
--   conversations  + last_inbound_at (the 24-hour window is measured from it)
--                    and the number the chat is on.
--   campaigns      + sent / failed counters (the Broadcasts tab already reads them)
--   outbox         every outbound message, queued -> sending -> sent | failed.
--                    The ONLY way anything is sent (spec WS3): replies,
--                    broadcasts, bots and the API all enqueue here.
--
-- Safe to run more than once. Additive. Run after 0004.

do $$
begin
  if to_regclass('public.wa_jobs') is null then
    raise exception '0005_messaging: wa_jobs is missing. Run 0004_gateway.sql first.';
  end if;
end $$;

-- ── messages / conversations / campaigns ─────────────────────────────────────
alter table messages add column if not exists wamid      text;
alter table messages add column if not exists msg_type   text not null default 'text';
alter table messages add column if not exists status     text;            -- outbound: queued|sent|delivered|read|failed
alter table messages add column if not exists error      jsonb;
alter table messages add column if not exists pricing    jsonb;
alter table messages add column if not exists media_id   text;
alter table messages add column if not exists mime_type  text;
alter table messages add column if not exists reply_to   text;
alter table messages add column if not exists client_ref text;            -- caller's idempotency key for an outbound message

create unique index if not exists messages_tenant_wamid_idx
  on messages (tenant_id, wamid) where wamid is not null;
create unique index if not exists messages_tenant_client_ref_idx
  on messages (tenant_id, client_ref) where client_ref is not null;

alter table conversations add column if not exists last_inbound_at timestamptz;
alter table conversations add column if not exists number_id uuid references phone_numbers(id);

alter table campaigns add column if not exists sent   integer not null default 0;
alter table campaigns add column if not exists failed integer not null default 0;

-- ── outbox ───────────────────────────────────────────────────────────────────
create table if not exists outbox (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  number_id   uuid not null references phone_numbers(id),
  to_phone    text,                                   -- digits, as Meta wants them
  to_bsuid    text,                                   -- used when there is no phone number
  kind        text not null check (kind in ('text', 'template', 'interactive', 'media')),
  payload     jsonb not null,                         -- the type-specific part of the Cloud API body
  idem_key    text not null,
  message_id  uuid references messages(id),           -- the row the inbox shows for this send
  campaign_id uuid references campaigns(id),
  state       text not null default 'queued'
              check (state in ('queued', 'sending', 'sent', 'failed', 'skipped')),
  attempts    integer not null default 0,
  run_at      timestamptz not null default now(),
  error_class text,                                   -- retry | needs_template | opted_out | fix_setup | fatal | unknown_outcome
  error       jsonb,
  wamid       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (tenant_id, idem_key),
  check (to_phone is not null or to_bsuid is not null)
);
create index if not exists outbox_ready_idx on outbox (number_id, run_at) where state = 'queued';
create index if not exists outbox_sending_idx on outbox (updated_at) where state = 'sending';
create index if not exists outbox_campaign_idx on outbox (campaign_id) where campaign_id is not null;

-- ── a tenant-scoped session sees only its own rows (see 0003) ────────────────
alter table outbox enable row level security;
drop policy if exists wa_tenant_isolation on outbox;
create policy wa_tenant_isolation on outbox for all to wa_app
  using (tenant_id = wa_current_tenant())
  with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on outbox to wa_app;

revoke all on table outbox from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table outbox to service_role;
  end if;
end $$;
