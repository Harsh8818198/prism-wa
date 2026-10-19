-- 0023_template_book.sql — a template's message validity (how long Meta keeps trying to deliver it).
--
-- templates.ttl_seconds   Meta's message_send_ttl_seconds, when it was set at creation; null means Meta's default.
--
-- Safe to run more than once. Additive. Run after 0022.

do $$
begin
  if to_regclass('public.templates') is null then
    raise exception '0023_template_book: run the earlier migrations first.';
  end if;
end $$;

alter table templates add column if not exists ttl_seconds integer check (ttl_seconds is null or ttl_seconds > 0);
