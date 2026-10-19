-- 0015_flows.sql — chatbot flows (spec WS8).
--
-- A flow is a small graph of steps a customer walks through by chatting: a question, a
-- menu of buttons, a delay, a handover to a person. `flows` holds the working copy (the
-- draft being edited) and `flow_versions` every published copy, so a bad publish can be
-- rolled back. `flow_runs` is where each conversation currently is: one live run per
-- conversation, advanced idempotently by the inbound message that moves it.
--
-- Safe to run more than once. Additive. Run after 0014.

do $$
begin
  if to_regclass('public.conversations') is null then
    raise exception '0015_flows: conversations is missing. Run 0001_tenancy.sql first.';
  end if;
end $$;

create table if not exists flows (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  name          text not null,
  enabled       boolean not null default false,          -- a flow only runs when it is on AND has been published
  trigger       jsonb not null default '{}'::jsonb,      -- what starts it (keyword, first message, ad, ...)
  graph         jsonb not null default '{}'::jsonb,      -- the draft: {"entry": "n1", "nodes": {"n1": {...}}}
  version       integer not null default 0,              -- the published version, 0 = never published
  published_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists flows_tenant_idx on flows (tenant_id, enabled);

create table if not exists flow_versions (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  flow_id       uuid not null references flows(id) on delete cascade,
  version       integer not null,
  trigger       jsonb not null default '{}'::jsonb,
  graph         jsonb not null,
  published_at  timestamptz not null default now(),
  unique (flow_id, version)
);

create table if not exists flow_runs (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  flow_id         uuid not null references flows(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  version         integer not null,
  state           text not null default 'running'
                  check (state in ('running', 'waiting', 'done', 'failed', 'cancelled')),
  node            text,                                   -- where it is now
  vars            jsonb not null default '{}'::jsonb,     -- what it has collected
  waiting         jsonb,                                  -- what it is waiting for: {"kind": "buttons", ...}
  deadline        timestamptz,                            -- when a wait gives up
  steps           integer not null default 0,
  started_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  finished_at     timestamptz,
  last_error      text
);
-- one live run per conversation: a customer is never in two flows at once
create unique index if not exists flow_runs_one_live_idx on flow_runs (conversation_id)
  where state in ('running', 'waiting');
create index if not exists flow_runs_flow_idx on flow_runs (flow_id, state);

-- A flow's tenant must be its parent's tenant (a row cannot hang under another workspace's flow).
create or replace function wa_tg_flow_child_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from flows where id = new.flow_id;
  if parent is null then
    raise exception 'flow row refers to an unknown flow' using errcode = '23503';
  end if;
  if new.tenant_id <> parent then
    raise exception 'flow row tenant does not match its flow''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;
drop trigger if exists wa_flow_versions_tenant on flow_versions;
create trigger wa_flow_versions_tenant before insert or update on flow_versions
  for each row execute function wa_tg_flow_child_tenant();
drop trigger if exists wa_flow_runs_tenant on flow_runs;
create trigger wa_flow_runs_tenant before insert or update on flow_runs
  for each row execute function wa_tg_flow_child_tenant();

do $$
declare t text;
begin
  foreach t in array array['flows', 'flow_versions', 'flow_runs'] loop
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
