-- 0003_rls.sql — a second lock: the database itself scopes a tenant's session.
--
-- 0001 makes every row carry a tenant. This makes a connection that IS scoped
-- to a tenant physically unable to read or write another's rows, even if the
-- application forgets a WHERE clause — the bug that was exposing every
-- tenant's data to every licence.
--
-- How the new prism-wa service uses it (nothing else does yet):
--
--     connect as a login role that is a member of wa_app
--     begin;
--     select set_config('app.tenant_id', '<tenant uuid>', true);   -- per transaction
--     ...queries...
--     commit;
--
-- With no app.tenant_id set, wa_app sees no rows at all (fails closed).
--
-- What this does NOT do, stated plainly: a process holding wa_app credentials
-- can set app.tenant_id to anything. It defends against application bugs, not
-- against a compromised service — that is what per-tenant credentials and the
-- licence server's token check are for. service_role (n8n, the licence server
-- today) bypasses RLS by design; those callers scope in code, and are tested
-- to.
--
-- Run after 0001 and 0002. Idempotent.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'wa_app') then
    create role wa_app nologin;
  end if;
end $$;

create or replace function wa_current_tenant() returns uuid
language sql stable as
$$ select nullif(current_setting('app.tenant_id', true), '')::uuid $$;

grant usage on schema public to wa_app;
grant execute on function wa_current_tenant() to wa_app;
grant execute on function wa_default_tenant() to wa_app;
grant execute on function wa_norm_phone(text) to wa_app;
grant execute on function wa_heal_contact(uuid) to wa_app;
grant execute on function wa_identity_conflict(uuid, uuid, text, text) to wa_app;
grant execute on function wa_merge_contacts(uuid, uuid, text) to wa_app;
grant execute on function wa_resolve_contact(uuid, text, text, text, text, text, text) to wa_app;
grant select on wa_platform_config to wa_app;      -- RLS on, no policy: reads as empty

do $$
declare t text;
begin
  foreach t in array array['contacts', 'conversations', 'messages', 'segments',
                           'campaigns', 'contact_identities', 'contact_identity_audit']
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

grant select on contact_identity_conflicts to wa_app;

-- segments and campaigns are new tables: like everything else in this project
-- they must not be reachable through the public publishable key. (The legacy
-- tables keep whatever grants they already had.)
revoke all on table segments, campaigns, wa_platform_config from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table segments, campaigns, wa_platform_config to service_role;
  end if;
end $$;
