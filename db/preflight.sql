-- preflight.sql — READ-ONLY. Run in the Supabase SQL editor BEFORE the migrations
-- and paste the single JSON result back. It changes nothing, and works whether
-- or not the migrations (or even tenant_id) exist yet.
--
-- It answers: which tenants exist, which of the tables the migrations need are
-- there, how many rows have no tenant, and whether anything would collide when
-- uniqueness becomes per-tenant.

with
col as (
  select table_name, column_name, data_type
    from information_schema.columns where table_schema = 'public'
),
has_tbl(t) as (
  select unnest(array['tenants','contacts','conversations','messages','leads',
                      'follow_up_queue','activity_log','segments','campaigns'])
),
tenant_null(tbl, n) as (
  -- query_to_xml runs the string only for branches the CASE reaches, so a
  -- missing column/table is reported as null instead of failing the report.
  select t,
    case when exists (select 1 from col where table_name = t and column_name = 'tenant_id')
         then (xpath('/row/c/text()', query_to_xml(
                 format('select count(*) as c from public.%I where tenant_id is null', t),
                 false, true, '')))[1]::text::bigint end
    from unnest(array['contacts','conversations','messages','leads','segments','campaigns']) as t
   where to_regclass('public.' || t) is not null
)
select jsonb_pretty(jsonb_build_object(
  'tenants', coalesce((select jsonb_agg(to_jsonb(t) - 'wa_token' - 'wa_api_key' - 'fb_page_token'
                                         - 'li_token' - 'li_refresh_token' - 'bot_config' - 'branding')
                         from tenants t), '[]'::jsonb),
  'tables_present', (select jsonb_object_agg(t, to_regclass('public.' || t) is not null) from has_tbl),
  'contacts_tags_type', (select data_type from col where table_name = 'contacts' and column_name = 'tags'),
  'contacts_has_tenant_id', exists (select 1 from col where table_name = 'contacts' and column_name = 'tenant_id'),
  'rows_without_tenant', (select coalesce(jsonb_object_agg(tbl, n), '{}'::jsonb) from tenant_null),
  'contacts_total', (select count(*) from contacts),
  'contacts_by_tenant', case
      when exists (select 1 from col where table_name = 'contacts' and column_name = 'tenant_id')
      then (xpath('/row/j/text()', query_to_xml(
             'select coalesce(jsonb_object_agg(coalesce(tenant_id::text, ''null''), n), ''{}''::jsonb)::text as j
                from (select tenant_id, count(*) n from public.contacts group by 1) s',
             false, true, '')))[1]::text::jsonb end,
  'phones_that_normalise_to_the_same_number',
      (select count(*) from (select regexp_replace(phone, '\D', '', 'g') from contacts
                              where phone is not null
                              group by 1 having count(*) > 1) d),
  'legacy_global_unique_indexes', (select coalesce(jsonb_agg(indexname), '[]'::jsonb)
                                     from pg_indexes
                                    where schemaname = 'public'
                                      and indexname in ('contacts_phone_idx', 'contacts_email_idx')),
  'segments_duplicate_names', case when to_regclass('public.segments') is not null
      then (xpath('/row/c/text()', query_to_xml(
             'select count(*) as c from (select 1 from public.segments group by lower(name) having count(*) > 1) d',
             false, true, '')))[1]::text::bigint end,
  'postgres_version', current_setting('server_version')
));
