-- 0019_campaign_book.sql — campaign detail: API campaigns, pause reasons, and what a campaign's header file is.
--
--   campaigns.kind          broadcast (the usual) | api (a named, always-on campaign other software sends through)
--   campaigns.pause_reason  why a campaign is paused when Prism paused it (a template's category changed)
--   campaigns.header_media  the file a template's image / video / document header sends: {"link": "https://…"}
--
-- Safe to run more than once. Additive. Run after 0018.

do $$
begin
  if to_regclass('public.contact_events') is null then
    raise exception '0019_campaign_book: contact_events is missing. Run 0018_contact_book.sql first.';
  end if;
end $$;

alter table campaigns add column if not exists kind         text not null default 'broadcast';
alter table campaigns add column if not exists pause_reason text;
alter table campaigns add column if not exists header_media jsonb;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'campaigns_kind_check') then
    alter table campaigns add constraint campaigns_kind_check check (kind in ('broadcast', 'api'));
  end if;
end $$;

-- an API campaign is found by its name, so the name is unique among a workspace's API campaigns
create unique index if not exists campaigns_api_name_idx on campaigns (tenant_id, name) where kind = 'api';
