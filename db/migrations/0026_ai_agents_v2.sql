-- 0026_ai_agents_v2.sql — a structured AI agent: profile, voice, skills, actions, settings, a draft apart from what is live, a knowledge base
-- that shows its state, meaning-based search, and a record of what customers asked that the agent could not answer.
--
--   ai_agents        profile / voice / skills / actions / settings (all JSON), `live` = the published copy (null = not published), published_version
--   ai_knowledge     status (processing | ready | failed), error, group_id (the pages of one website crawl), refreshed_at
--   ai_chunks        embedding real[]  — filled when a meaning model is configured; search works without it
--   ai_gaps          questions the agent could not answer, counted, until someone teaches it or ignores them
--   ai_events        what the agent did in each chat: answered, handed over, ran an action, finished a skill — the numbers behind its analytics
--   ai_usage.kind    gains 'aux': extra model calls (search help, action wording) that are not replies and do not count toward the allowance
--
-- Safe to run more than once. Additive (the one constraint it replaces is widened, never narrowed). Run after 0025.

do $$
begin
  if to_regclass('public.ai_agents') is null then
    raise exception '0026_ai_agents_v2: run 0016_ai_agents.sql first.';
  end if;
end $$;

alter table ai_agents add column if not exists profile         jsonb not null default '{}';
alter table ai_agents add column if not exists voice           jsonb not null default '{}';
alter table ai_agents add column if not exists skills          jsonb not null default '[]';
alter table ai_agents add column if not exists actions         jsonb not null default '[]';
alter table ai_agents add column if not exists settings        jsonb not null default '{}';
alter table ai_agents add column if not exists live            jsonb;
alter table ai_agents add column if not exists published_at    timestamptz;
alter table ai_agents add column if not exists published_version integer not null default 0;

-- an agent that was already on before this change keeps answering exactly as it was: its present settings become its first published copy
update ai_agents
   set live = jsonb_build_object('instructions', instructions, 'handoff_words', to_jsonb(handoff_words), 'max_replies', max_replies,
                                 'business_hours_only', business_hours_only, 'profile', profile, 'voice', voice, 'skills', skills,
                                 'actions', actions, 'settings', settings),
       published_at = now(), published_version = 1
 where enabled and live is null and published_version = 0;

alter table ai_knowledge add column if not exists status       text not null default 'ready';
alter table ai_knowledge add column if not exists error        text;
alter table ai_knowledge add column if not exists group_id     uuid;
alter table ai_knowledge add column if not exists refreshed_at timestamptz;
alter table ai_knowledge drop constraint if exists ai_knowledge_status_check;
alter table ai_knowledge add constraint ai_knowledge_status_check check (status in ('processing', 'ready', 'failed'));
create index if not exists ai_knowledge_group_idx on ai_knowledge (group_id) where group_id is not null;

alter table ai_chunks add column if not exists embedding real[];

alter table ai_usage drop constraint if exists ai_usage_kind_check;
alter table ai_usage add constraint ai_usage_kind_check check (kind in ('reply', 'test', 'aux'));

create table if not exists ai_gaps (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  agent_id    uuid not null references ai_agents(id) on delete cascade,
  key         text not null,                                   -- the question, lower-cased and squeezed, so the same question counts once
  question    text not null,                                   -- as the customer first wrote it
  reason      text,
  times       integer not null default 1,
  status      text not null default 'open' check (status in ('open', 'answered', 'ignored')),
  first_at    timestamptz not null default now(),
  last_at     timestamptz not null default now(),
  unique (agent_id, key)
);
create index if not exists ai_gaps_agent_idx on ai_gaps (agent_id, status, times desc);

create table if not exists ai_events (
  id              bigserial primary key,
  tenant_id       uuid not null references tenants(id),
  agent_id        uuid references ai_agents(id) on delete cascade,
  conversation_id uuid,
  kind            text not null check (kind in ('answered', 'handoff', 'action', 'skill', 'gap', 'failed')),
  reason          text,
  at              timestamptz not null default now()
);
create index if not exists ai_events_agent_idx on ai_events (agent_id, at);
create index if not exists ai_events_tenant_idx on ai_events (tenant_id, at);

drop trigger if exists wa_ai_gaps_tenant on ai_gaps;
create trigger wa_ai_gaps_tenant before insert or update on ai_gaps
  for each row execute function wa_tg_ai_child_tenant();

do $$
declare t text;
begin
  foreach t in array array['ai_gaps', 'ai_events'] loop
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
  execute 'grant usage, select on sequence ai_events_id_seq to wa_app';
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant usage, select on sequence ai_events_id_seq to service_role';
  end if;
end $$;
