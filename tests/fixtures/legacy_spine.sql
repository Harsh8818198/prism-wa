-- The tenant spine as it stands in the live project: shakti_setup.sql's tenants
-- table and shakti_multitenant_spine.sql's tenant_id on contacts/leads.
-- Applied on top of legacy_schema.sql for the "live-like" template; the
-- "pre-spine" template skips the two ALTERs so both starting states are tested.

create table if not exists tenants (
  id                 uuid primary key default gen_random_uuid(),
  slug               text unique not null,
  name               text not null,
  wa_phone_number_id text unique,
  wa_waba_id         text,
  wa_token           text,
  bot_config         jsonb,
  active             boolean default true,
  created_at         timestamptz default now()
);
insert into tenants (slug, name, wa_phone_number_id, active)
values ('shakti', 'Shakti Sports Club', null, true)
on conflict (slug) do nothing;
