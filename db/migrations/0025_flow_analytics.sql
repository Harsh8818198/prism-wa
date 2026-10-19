-- 0025_flow_analytics.sql — what customers did inside a flow, and the ad click behind a contact.
--
--   flow_run_steps       one row each time a run reaches a step: so a flow can show where customers stop and how many finish
--   contacts.ctwa_clid   the click id Meta gives a click-to-WhatsApp ad: the Conversions API needs it to say which ad earned a lead
--
-- Safe to run more than once. Additive. Run after 0024.

do $$
begin
  if to_regclass('public.flow_runs') is null then
    raise exception '0025_flow_analytics: run 0015_flows.sql first.';
  end if;
end $$;

create table if not exists flow_run_steps (
  id        bigserial primary key,
  tenant_id uuid not null references tenants(id),
  flow_id   uuid not null references flows(id) on delete cascade,
  run_id    uuid not null references flow_runs(id) on delete cascade,
  version   integer not null,
  node      text not null,
  kind      text not null,
  at        timestamptz not null default now()
);
create index if not exists flow_run_steps_flow_idx on flow_run_steps (flow_id, node);
create index if not exists flow_run_steps_run_idx on flow_run_steps (run_id, id);

alter table contacts add column if not exists ctwa_clid text;

alter table flow_run_steps enable row level security;
drop policy if exists wa_tenant_isolation on flow_run_steps;
create policy wa_tenant_isolation on flow_run_steps for all to wa_app
  using (tenant_id = wa_current_tenant()) with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on flow_run_steps to wa_app;
grant usage, select on sequence flow_run_steps_id_seq to wa_app;
revoke all on table flow_run_steps from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table flow_run_steps to service_role;
    grant usage, select on sequence flow_run_steps_id_seq to service_role;
  end if;
end $$;
