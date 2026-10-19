-- 0014_ads.sql — click-to-WhatsApp ads made from Prism (spec WS13).
--
--   ad_accounts   a workspace's Meta ad account and Facebook Page, with the access token
--                 encrypted (vault.py) like every other token
--   ads           every ad Prism created: the Meta ids of its campaign / ad set / creative / ad,
--                 what was asked for, and its state. Ads are ALWAYS created paused; turning one on is
--                 a separate, explicit step.
--
-- Safe to run more than once. Additive. Run after 0013.

do $$
begin
  if to_regclass('public.cart_checkouts') is null then
    raise exception '0014_ads: cart_checkouts is missing. Run 0012_integrations.sql first.';
  end if;
end $$;

create table if not exists ad_accounts (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  ad_account_id text not null check (ad_account_id ~ '^[0-9]{5,20}$'),   -- digits only, without "act_"
  page_id       text not null check (page_id ~ '^[0-9]{5,25}$'),
  name          text,
  token_enc     bytea not null,
  key_ver       integer,
  created_at    timestamptz not null default now(),
  unique (tenant_id, ad_account_id)
);

create table if not exists ads (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  account_id  uuid not null references ad_accounts(id) on delete cascade,
  name        text not null,
  meta_ids    jsonb not null default '{}'::jsonb,         -- {"campaign": "...", "adset": "...", "creative": "...", "ad": "..."}
  spec        jsonb not null default '{}'::jsonb,
  status      text not null default 'PAUSED',             -- PAUSED | ACTIVE | FAILED
  last_error  jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists ads_tenant_idx on ads (tenant_id, created_at desc);

do $$
declare t text;
begin
  foreach t in array array['ad_accounts', 'ads']
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

revoke all on table ad_accounts, ads from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table ad_accounts, ads to service_role;
  end if;
end $$;
