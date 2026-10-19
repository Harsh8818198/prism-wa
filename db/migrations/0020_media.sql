-- 0020_media.sql — pictures, videos, audio and documents (the Media Library, and what a chat or a campaign can carry).
--
--   media_assets            a file a workspace holds: uploaded by a person, or received from a customer. The bytes live in the
--                           media store (a folder on the server); this row says what and where, and when Meta last took a copy
--   messages.media_asset_id the stored copy of a message's picture / video / audio / document
--   canned_messages         + asset_id: a saved reply can carry a file
--
-- Safe to run more than once. Additive. Run after 0019.

do $$
begin
  if to_regclass('public.campaigns') is null or not exists (select 1 from information_schema.columns where table_name = 'campaigns' and column_name = 'header_media') then
    raise exception '0020_media: run 0019_campaign_book.sql first.';
  end if;
end $$;

create table if not exists media_assets (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id),
  kind           text not null check (kind in ('image', 'video', 'audio', 'document', 'sticker')),
  mime           text not null,
  filename       text not null,
  size_bytes     bigint not null check (size_bytes >= 0),
  sha256         text not null,
  path           text not null,                              -- where the media store keeps it (relative)
  source         text not null default 'upload',             -- upload | inbound | canned | flow
  wa_media_id    text,                                       -- Meta's id for it, once uploaded (they expire after about 30 days)
  wa_number_id   uuid references phone_numbers(id) on delete set null,
  wa_uploaded_at timestamptz,
  created_by     uuid references agents(id) on delete set null,
  created_at     timestamptz not null default now(),
  unique (tenant_id, sha256, kind)
);
create index if not exists media_assets_tenant_idx on media_assets (tenant_id, created_at desc);

alter table messages         add column if not exists media_asset_id uuid references media_assets(id) on delete set null;
alter table canned_messages  add column if not exists asset_id       uuid references media_assets(id) on delete set null;

-- a saved reply may be only a file: its words become optional
alter table canned_messages drop constraint if exists canned_messages_body_check;
alter table canned_messages add constraint canned_messages_body_check check (length(body) between 0 and 1000);

alter table media_assets enable row level security;
drop policy if exists wa_tenant_isolation on media_assets;
create policy wa_tenant_isolation on media_assets for all to wa_app
  using (tenant_id = wa_current_tenant()) with check (tenant_id = wa_current_tenant());
grant select, insert, update, delete on media_assets to wa_app;
revoke all on table media_assets from anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table media_assets to service_role;
  end if;
end $$;
