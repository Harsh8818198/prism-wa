-- 0009_campaigns.sql — segments with members, and the campaign engine (spec WS6, WS7).
--
--   segments.dynamic      a dynamic segment is re-evaluated each time; a static
--                         one is a frozen list (segment_members)
--   campaigns             + the template, audience, variable mapping, schedule,
--                         retry policy, click tracking and lifecycle timestamps
--   campaign_recipients   the audience SNAPSHOT taken at launch: who, with which
--                         variable values, and how it went. Re-running a segment
--                         later never changes what a campaign did.
--   short_links           tracked links: /r/<token> redirects and records the click
--   click_events          who clicked what, from which campaign
--
-- campaigns.status: draft | scheduled | sending | paused | done | cancelled | failed
--
-- Safe to run more than once. Additive. Run after 0008.

do $$
begin
  if to_regclass('public.consent_ledger') is null then
    raise exception '0009_campaigns: consent_ledger is missing. Run 0008_contacts.sql first.';
  end if;
end $$;

-- ── segments ─────────────────────────────────────────────────────────────────
alter table segments add column if not exists dynamic boolean not null default true;

create table if not exists segment_members (
  segment_id uuid not null references segments(id) on delete cascade,
  contact_id uuid not null references contacts(id) on delete cascade,
  tenant_id  uuid not null references tenants(id),
  primary key (segment_id, contact_id)
);
create index if not exists segment_members_contact_idx on segment_members (contact_id);

-- ── campaigns ────────────────────────────────────────────────────────────────
alter table campaigns add column if not exists name         text;
alter table campaigns add column if not exists template_id  uuid references templates(id) on delete set null;
alter table campaigns add column if not exists segment_id   uuid references segments(id) on delete set null;
alter table campaigns add column if not exists number_id    uuid references phone_numbers(id);
alter table campaigns add column if not exists audience     jsonb not null default '{}'::jsonb;
alter table campaigns add column if not exists variables    jsonb not null default '{}'::jsonb;
alter table campaigns add column if not exists schedule_at  timestamptz;
alter table campaigns add column if not exists timezone     text;
alter table campaigns add column if not exists retry        jsonb not null default '{"max": 3, "delay_minutes": 30}'::jsonb;
alter table campaigns add column if not exists track_clicks boolean not null default true;
alter table campaigns add column if not exists started_at   timestamptz;
alter table campaigns add column if not exists finished_at  timestamptz;
alter table campaigns add column if not exists preflight    jsonb;
alter table campaigns add column if not exists updated_at   timestamptz not null default now();
create index if not exists campaigns_due_idx on campaigns (schedule_at) where status = 'scheduled';

create table if not exists campaign_recipients (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  campaign_id uuid not null references campaigns(id) on delete cascade,
  contact_id  uuid not null references contacts(id) on delete cascade,
  params      jsonb not null default '{}'::jsonb,       -- the variable values this person got
  status      text not null default 'pending'
              check (status in ('pending', 'queued', 'sent', 'failed', 'skipped')),
  skip_reason text,                                     -- opted_out | no_number | window | template_not_approved ...
  outbox_id   uuid,
  created_at  timestamptz not null default now(),
  unique (campaign_id, contact_id)
);
create index if not exists campaign_recipients_campaign_idx on campaign_recipients (campaign_id, status);
create index if not exists campaign_recipients_contact_idx on campaign_recipients (contact_id);

-- ── click tracking ───────────────────────────────────────────────────────────
create table if not exists short_links (
  token       text primary key,
  tenant_id   uuid not null references tenants(id),
  campaign_id uuid references campaigns(id) on delete cascade,
  recipient_id uuid references campaign_recipients(id) on delete cascade,
  contact_id  uuid references contacts(id) on delete cascade,
  target      text not null,
  card        text,                                     -- which carousel card or button
  created_at  timestamptz not null default now()
);

create table if not exists click_events (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id),
  campaign_id  uuid references campaigns(id) on delete cascade,
  recipient_id uuid references campaign_recipients(id) on delete cascade,
  contact_id   uuid references contacts(id) on delete cascade,
  kind         text not null check (kind in ('link', 'button')),
  card         text,
  url          text,
  at           timestamptz not null default now()
);
create index if not exists click_events_campaign_idx on click_events (campaign_id, at);
create index if not exists click_events_contact_idx on click_events (contact_id);

-- ── isolation ────────────────────────────────────────────────────────────────
do $$
declare t text;
begin
  foreach t in array array['segment_members', 'campaign_recipients', 'short_links', 'click_events']
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

revoke all on table segment_members, campaign_recipients, short_links, click_events from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table segment_members, campaign_recipients, short_links, click_events to service_role;
  end if;
end $$;
