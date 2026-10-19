-- 0016_ai_agents.sql — AI agents and their knowledge (spec WS9).
--
-- An agent answers customers on the owner's behalf from what the owner has told it: instructions
-- (who it is, how it speaks, what it must not do) and a knowledge base (pasted text, question and
-- answer pairs, a web page, an uploaded file's text). Knowledge is cut into chunks and searched with
-- Postgres full-text search, so nothing leaves the database but the few chunks a reply needs.
-- `ai_usage` counts every model call, for the plan's monthly allowance and for the owner to see.
--
-- Safe to run more than once. Additive. Run after 0015.

do $$
begin
  if to_regclass('public.conversations') is null then
    raise exception '0016_ai_agents: conversations is missing. Run 0001_tenancy.sql first.';
  end if;
end $$;

create table if not exists ai_agents (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  name            text not null,
  enabled         boolean not null default false,
  is_default      boolean not null default false,        -- answers chats no flow has taken (one per workspace)
  instructions    text not null default '',
  handoff_words   text[] not null default '{human,agent,person,complaint,refund,speak to someone}',
  max_replies     integer not null default 12,           -- replies in one chat before a person is asked in
  business_hours_only boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create unique index if not exists ai_agents_one_default_idx on ai_agents (tenant_id) where is_default;
create index if not exists ai_agents_tenant_idx on ai_agents (tenant_id);

create table if not exists ai_knowledge (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  agent_id    uuid not null references ai_agents(id) on delete cascade,
  title       text not null,
  kind        text not null default 'text' check (kind in ('text', 'faq', 'url', 'file')),
  source      text,                                       -- the address or file name it came from
  body        text not null,
  created_at  timestamptz not null default now()
);
create index if not exists ai_knowledge_agent_idx on ai_knowledge (agent_id);

create table if not exists ai_chunks (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  agent_id      uuid not null references ai_agents(id) on delete cascade,
  knowledge_id  uuid not null references ai_knowledge(id) on delete cascade,
  ord           integer not null,
  body          text not null,
  tsv           tsvector generated always as (to_tsvector('simple', body)) stored
);
create index if not exists ai_chunks_tsv_idx on ai_chunks using gin (tsv);
create index if not exists ai_chunks_agent_idx on ai_chunks (agent_id);

create table if not exists ai_usage (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id),
  agent_id        uuid references ai_agents(id) on delete set null,
  conversation_id uuid,
  kind            text not null default 'reply' check (kind in ('reply', 'test')),
  tokens_in       integer not null default 0,
  tokens_out      integer not null default 0,
  at              timestamptz not null default now()
);
create index if not exists ai_usage_tenant_at_idx on ai_usage (tenant_id, at);

-- which agent (if any) is talking in a conversation, and how many times it has replied
alter table conversations add column if not exists ai_agent_id uuid references ai_agents(id) on delete set null;
alter table conversations add column if not exists ai_replies  integer not null default 0;

create or replace function wa_tg_ai_child_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from ai_agents where id = new.agent_id;
  if parent is null then
    raise exception 'AI row refers to an unknown agent' using errcode = '23503';
  end if;
  if new.tenant_id <> parent then
    raise exception 'AI row tenant does not match its agent''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;
drop trigger if exists wa_ai_knowledge_tenant on ai_knowledge;
create trigger wa_ai_knowledge_tenant before insert or update on ai_knowledge
  for each row execute function wa_tg_ai_child_tenant();
drop trigger if exists wa_ai_chunks_tenant on ai_chunks;
create trigger wa_ai_chunks_tenant before insert or update on ai_chunks
  for each row execute function wa_tg_ai_child_tenant();

do $$
declare t text;
begin
  foreach t in array array['ai_agents', 'ai_knowledge', 'ai_chunks', 'ai_usage'] loop
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
