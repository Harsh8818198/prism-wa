-- 0028_ads_v2.sql — more kinds of ad, the audiences they can be shown to, Meta's review state, a Facebook sign-in, and suggestions for the owner to approve.
--
--   ads                 kind (ctwa | website | leadform), Meta's own status and review feedback as last read, when it was read
--   ad_accounts         how it was connected (pasted token or Facebook sign-in), the pixel, the currency
--   ad_audiences        people lists made in Meta from a Prism segment, and look-alikes of them
--   ad_connect_states   one Facebook sign-in in progress: its state, the token Meta gave back (encrypted), and the accounts and Pages the person can choose from
--   ad_suggestions      "pause this", "put more into that" — made from the numbers, applied only when the owner says yes
--
-- Safe to run more than once. Additive. Run after 0027.

do $$
begin
  if to_regclass('public.ads') is null then
    raise exception '0028_ads_v2: run 0014_ads.sql first.';
  end if;
end $$;

alter table ads add column if not exists kind          text not null default 'ctwa';
alter table ads add column if not exists meta_status   text;
alter table ads add column if not exists review        jsonb;
alter table ads add column if not exists synced_at     timestamptz;
alter table ads drop constraint if exists ads_kind_check;
alter table ads add constraint ads_kind_check check (kind in ('ctwa', 'website', 'leadform'));

alter table ad_accounts add column if not exists connected_via text not null default 'manual';
alter table ad_accounts add column if not exists pixel_id      text;
alter table ad_accounts add column if not exists currency      text;

create table if not exists ad_audiences (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  account_id  uuid not null references ad_accounts(id) on delete cascade,
  name        text not null,
  kind        text not null check (kind in ('segment', 'lookalike')),
  meta_id     text not null,
  segment_id  uuid,
  source      jsonb not null default '{}'::jsonb,         -- how many people were sent, which country and ratio a look-alike was made for, ...
  size        integer,
  created_at  timestamptz not null default now(),
  unique (account_id, meta_id)
);
create index if not exists ad_audiences_tenant_idx on ad_audiences (tenant_id, created_at desc);

create table if not exists ad_connect_states (
  state       text primary key,
  tenant_id   uuid not null references tenants(id),
  redirect_uri text not null,
  status      text not null default 'waiting' check (status in ('waiting', 'ready', 'done', 'failed')),
  token_enc   bytea,
  key_ver     integer,
  options     jsonb,                                       -- {"accounts": [{"id", "name"}], "pages": [{"id", "name"}], "user": "..."}
  error       text,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null default (now() + interval '30 minutes')
);

create table if not exists ad_suggestions (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id),
  ad_id       uuid not null references ads(id) on delete cascade,
  kind        text not null check (kind in ('pause', 'budget_up', 'budget_down', 'new_creative')),
  reason      text not null,
  params      jsonb not null default '{}'::jsonb,
  state       text not null default 'open' check (state in ('open', 'applied', 'dismissed')),
  created_at  timestamptz not null default now(),
  decided_at  timestamptz
);
create index if not exists ad_suggestions_open_idx on ad_suggestions (tenant_id, state, created_at desc);

do $$
declare t text;
begin
  foreach t in array array['ad_audiences', 'ad_connect_states', 'ad_suggestions'] loop
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
