-- 0011_api_keys.sql — keys for a customer's own systems to call the public API (spec WS14).
--
-- A key is shown once, when it is made. Only its SHA-256 is stored, so a leaked database does not leak
-- working keys; `prefix` (the first few characters) is kept so a person can tell which key is which.
-- Scopes say what a key may do; revoking a key stops it at once.
--
-- Safe to run more than once. Additive. Run after 0010.

do $$
begin
  if to_regclass('public.agents') is null then
    raise exception '0011_api_keys: agents is missing. Run 0010_inbox.sql first.';
  end if;
end $$;

create table if not exists api_keys (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id),
  name                  text not null check (length(name) between 1 and 80),
  prefix                text not null,
  key_hash              text not null unique,
  scopes                text[] not null default '{}',
  rate_limit_per_minute integer not null default 600 check (rate_limit_per_minute between 1 and 100000),
  created_by            uuid references agents(id) on delete set null,
  created_at            timestamptz not null default now(),
  last_used_at          timestamptz,
  revoked_at            timestamptz
);
create index if not exists api_keys_tenant_idx on api_keys (tenant_id, created_at desc);

alter table api_keys enable row level security;
drop policy if exists wa_tenant_isolation on api_keys;
create policy wa_tenant_isolation on api_keys for all to wa_app
  using (tenant_id = wa_current_tenant())
  with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on api_keys to wa_app;

revoke all on table api_keys from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table api_keys to service_role;
  end if;
end $$;
