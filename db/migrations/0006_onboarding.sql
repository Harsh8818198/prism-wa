-- 0006_onboarding.sql — what Embedded Signup records about a connected number.
--
--   phone_numbers.pin_enc         the two-step verification PIN we set when we
--                                 registered the number, encrypted (see
--                                 prism_wa/vault.py). Needed to re-register it.
--   phone_numbers.registered_at   when Meta accepted the registration
--   wabas.subscribed_at           when our app was subscribed to its webhooks
--
-- Safe to run more than once. Additive. Run after 0005.

do $$
begin
  if to_regclass('public.outbox') is null then
    raise exception '0006_onboarding: outbox is missing. Run 0005_messaging.sql first.';
  end if;
end $$;

alter table phone_numbers add column if not exists pin_enc       bytea;
alter table phone_numbers add column if not exists registered_at timestamptz;
alter table wabas         add column if not exists subscribed_at timestamptz;
