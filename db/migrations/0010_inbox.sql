-- 0010_inbox.sql — a team inbox (spec WS10): agents, ownership, notes, canned replies.
--
--   agents                who works the inbox: a role (owner / manager / agent), whether they
--                         are taking chats now, working hours, and routing rules ("this agent
--                         handles chats tagged X"). Each Prism seat maps to one agent.
--   conversations         + assignee, when it was assigned/requested/resolved, and when the
--                         welcome and off-hours auto-replies last went (so they are not repeated)
--                         conversations.status takes: new | active | requesting | intervened | closed
--                         (legacy `human_takeover` is treated as intervened)
--   conversation_events   an audit trail: assigned, transferred, took over, resolved, reopened
--   conversation_notes    internal notes, never sent to the customer
--   canned_messages       saved replies with a /shortcut and {{variables}}
--
-- Visibility is enforced HERE, not only in the app: a session that says it is an agent
-- (app.agent_role = 'agent', app.agent_id = <id>) can read only conversations that are
-- unassigned or assigned to it — and therefore only their messages and notes. Managers,
-- owners and the service itself are unaffected. A restrictive policy is added next to
-- 0003's tenant isolation, so BOTH must allow a row.
--
-- Safe to run more than once. Additive. Run after 0009.

do $$
begin
  if to_regclass('public.click_events') is null then
    raise exception '0010_inbox: click_events is missing. Run 0009_campaigns.sql first.';
  end if;
end $$;

create table if not exists agents (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  seat_key         text not null,                         -- the licence seat this person uses
  name             text not null,
  role             text not null default 'agent' check (role in ('owner', 'manager', 'agent')),
  active           boolean not null default true,         -- false = removed (kept for history)
  available        boolean not null default true,         -- "I am taking chats now"
  masked           boolean not null default false,        -- sees only the last four digits of numbers
  working_hours    jsonb,                                 -- {"tz": "Asia/Kolkata", "days": {"mon": [["09:00", "18:00"]]}}
  rules            jsonb not null default '[]'::jsonb,    -- segment filters: handles only chats that match one
  last_assigned_at timestamptz,
  created_at       timestamptz not null default now(),
  unique (tenant_id, seat_key)
);

alter table conversations add column if not exists assignee_id     uuid references agents(id) on delete set null;
alter table conversations add column if not exists assigned_at     timestamptz;
alter table conversations add column if not exists requested_at    timestamptz;
alter table conversations add column if not exists resolved_at     timestamptz;
alter table conversations add column if not exists last_welcome_at timestamptz;
alter table conversations add column if not exists last_offhours_at timestamptz;
create index if not exists conversations_assignee_idx on conversations (tenant_id, assignee_id, status);

create table if not exists conversation_events (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  conversation_id uuid not null references conversations(id) on delete cascade,
  kind            text not null,                          -- assigned | transferred | intervened | requested | resolved | reopened | note
  actor_id        uuid references agents(id) on delete set null,
  detail          jsonb,
  at              timestamptz not null default now()
);
create index if not exists conversation_events_conv_idx on conversation_events (conversation_id, at);

create table if not exists conversation_notes (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  conversation_id uuid not null references conversations(id) on delete cascade,
  agent_id        uuid references agents(id) on delete set null,
  text            text not null check (length(text) between 1 and 4000),
  at              timestamptz not null default now()
);
create index if not exists conversation_notes_conv_idx on conversation_notes (conversation_id, at);

create table if not exists canned_messages (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id),
  shortcut   text not null check (shortcut ~ '^[a-z0-9_-]{1,30}$'),
  body       text not null check (length(body) between 1 and 1000),
  created_by uuid references agents(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (tenant_id, shortcut)
);

-- who is asking: set per transaction alongside app.tenant_id (0003)
create or replace function wa_current_role() returns text
language sql stable as
$$ select coalesce(nullif(current_setting('app.agent_role', true), ''), 'service') $$;
create or replace function wa_current_agent() returns uuid
language sql stable as
$$ select nullif(current_setting('app.agent_id', true), '')::uuid $$;
grant execute on function wa_current_role() to wa_app;
grant execute on function wa_current_agent() to wa_app;

-- ── tenant isolation on the new tables ───────────────────────────────────────
do $$
declare t text;
begin
  foreach t in array array['agents', 'conversation_events', 'conversation_notes', 'canned_messages']
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

-- ── an agent sees only what is theirs or nobody's ────────────────────────────
drop policy if exists wa_agent_visibility on conversations;
create policy wa_agent_visibility on conversations as restrictive for all to wa_app
  using (wa_current_role() <> 'agent' or assignee_id is null or assignee_id = wa_current_agent())
  with check (wa_current_role() <> 'agent' or assignee_id is null or assignee_id = wa_current_agent());

-- messages, notes and events follow their conversation (the subquery is itself filtered by the policy above)
do $$
declare t text;
begin
  foreach t in array array['messages', 'conversation_notes', 'conversation_events']
  loop
    execute format('drop policy if exists wa_agent_visibility on %I', t);
    execute format(
      'create policy wa_agent_visibility on %I as restrictive for all to wa_app '
      'using (wa_current_role() <> ''agent'' or exists (select 1 from conversations v where v.id = conversation_id)) '
      'with check (wa_current_role() <> ''agent'' or exists (select 1 from conversations v where v.id = conversation_id))', t);
  end loop;
end $$;

revoke all on table agents, conversation_events, conversation_notes, canned_messages from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table agents, conversation_events, conversation_notes, canned_messages to service_role;
  end if;
end $$;
