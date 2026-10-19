-- 0022_media_blobs.sql — where the bytes of a media file live by default: Postgres.
--
-- 0020 added media_assets (what a file is). The gateway takes an upload and the worker sends it to Meta and keeps what a customer
-- sends; they are separate processes that usually cannot share a disk, but always share the database. So the default media store is
-- this table (media_store.DbStore). WA_MEDIA_DIR still selects a shared folder instead.
--
-- Not row-level-secured on purpose: it is reached only through media_store, by a path our own media_assets row holds, and the path
-- carries the workspace id. Safe to run more than once. Additive. Run after 0020.

do $$
begin
  if to_regclass('public.media_assets') is null then
    raise exception '0022_media_blobs: run 0020_media.sql first.';
  end if;
end $$;

create table if not exists media_blobs (
  path text primary key,
  data bytea not null
);

grant select, insert, update, delete on media_blobs to wa_app;
revoke all on table media_blobs from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table media_blobs to service_role;
  end if;
end $$;
