-- 0017_scheduling.sql — recurring campaigns and per-recipient local send time (spec WS7).
--
-- A recurring campaign is a saved campaign recipe plus a rule ("every Monday at 10:00"). Each time
-- the rule fires a worker makes an ordinary campaign from the recipe and launches it, so a repeat is
-- reported, paused and cancelled like any other. `campaigns.local_time` makes one campaign go out
-- at the same wall-clock time in each recipient's own time zone (worked out from the country code).
--
-- Safe to run more than once. Additive. Run after 0016.

do $$
begin
  if to_regclass('public.campaigns') is null then
    raise exception '0017_scheduling: campaigns is missing. Run 0009_campaigns.sql first.';
  end if;
end $$;

alter table campaigns add column if not exists local_time    text;        -- "10:00": each person at 10:00 their time
alter table campaigns add column if not exists schedule_id   uuid;        -- the recurring recipe that made it

create table if not exists campaign_schedules (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  name          text not null,
  config        jsonb not null,                            -- what create_campaign takes (template, audience, variables, ...)
  rule          jsonb not null,                            -- {"freq": "weekly", "time": "10:00", "days": ["mon"], "tz": "Asia/Kolkata"}
  enabled       boolean not null default true,
  next_run_at   timestamptz,
  last_run_at   timestamptz,
  runs          integer not null default 0,
  failures      integer not null default 0,                -- in a row; three turn the schedule off
  last_error    text,
  created_by    uuid,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists campaign_schedules_due_idx on campaign_schedules (next_run_at) where enabled;
create index if not exists campaign_schedules_tenant_idx on campaign_schedules (tenant_id);

alter table campaign_schedules enable row level security;
drop policy if exists wa_tenant_isolation on campaign_schedules;
create policy wa_tenant_isolation on campaign_schedules for all to wa_app
  using (tenant_id = wa_current_tenant()) with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on campaign_schedules to wa_app;
revoke all on table campaign_schedules from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table campaign_schedules to service_role;
  end if;
end $$;
