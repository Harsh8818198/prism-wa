-- backup_before_first_run.sql — a copy of every table the first migrations (0001-0003) change, taken BEFORE they run.
--
-- Why: the shared Supabase database holds the CRM tables other Prism services use, and the free Supabase plan has no automatic backups. 0001 adds
-- columns to contacts / conversations / messages / leads / segments / campaigns, fills in tenant_id on existing rows, and swaps two unique indexes. Nothing
-- is deleted, but a copy costs seconds and means a mistake can always be undone by hand.
--
-- What it does: for each table below that exists, `bak_<name>` = a copy of its rows (idempotent: re-running keeps the FIRST copy). The copies are locked
-- like everything else in this project (row-level security on, no access for the public Supabase keys). It changes nothing that exists today.
-- Run it in the Supabase SQL editor. To undo it afterwards: drop the bak_* tables (a month after everything has been working).
--
-- A copy keeps the rows, not the indexes, triggers or defaults. Restoring is a manual job (insert the rows back); the point is that nothing is lost.

do $$
declare
  t text;
  made text[] := '{}';
begin
  foreach t in array array['tenants', 'contacts', 'conversations', 'messages', 'leads', 'segments', 'campaigns', 'follow_up_queue', 'activity_log']
  loop
    if to_regclass('public.' || t) is not null and to_regclass('public.bak_' || t) is null then
      execute format('create table public.%I as select * from public.%I', 'bak_' || t, t);
      execute format('alter table public.%I enable row level security', 'bak_' || t);
      execute format('revoke all on table public.%I from anon, authenticated', 'bak_' || t);
      made := made || t;
    end if;
  end loop;
  raise notice 'copied: %', coalesce(array_to_string(made, ', '), '(nothing new: already copied, or the tables are not there)');
end $$;

-- What was copied, and that the copy is complete (the two counts must match for every row).
select t.table_name as copy,
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', t.table_name), false, true, '')))[1]::text::bigint as rows_in_copy,
       (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', substr(t.table_name, 5)), false, true, '')))[1]::text::bigint as rows_in_original
  from information_schema.tables t
 where t.table_schema = 'public' and t.table_name like 'bak\_%'
 order by 1;
