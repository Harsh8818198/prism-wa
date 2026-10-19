-- 0001_0003_down.sql — undo the ENFORCEMENT of 0001–0003 without losing data.
--
-- Keeps every column, row, table and function the migrations added (tenant_id,
-- contact_identities, the resolver…): dropping them would destroy data and
-- nothing depends on them being absent. It removes what could change how the
-- existing flows behave — the triggers, the NOT NULL seal, the app role's
-- policies — and restores the two global unique indexes on contacts.
--
-- The index restore FAILS, on purpose, if two tenants now hold the same phone
-- or email: that is the collision the per-tenant indexes exist to allow, and
-- silently dropping a contact to make the index build would be worse.

drop policy if exists wa_tenant_isolation on contacts;
drop policy if exists wa_tenant_isolation on conversations;
drop policy if exists wa_tenant_isolation on messages;
drop policy if exists wa_tenant_isolation on segments;
drop policy if exists wa_tenant_isolation on campaigns;
drop policy if exists wa_tenant_isolation on contact_identities;
drop policy if exists wa_tenant_isolation on contact_identity_audit;

drop trigger if exists wa_tenant_default   on contacts;
drop trigger if exists wa_tenant_default   on segments;
drop trigger if exists wa_tenant_default   on campaigns;
drop trigger if exists wa_tenant_parent    on conversations;
drop trigger if exists wa_tenant_parent    on messages;
drop trigger if exists wa_tenant_immutable on contacts;
drop trigger if exists wa_tenant_immutable on conversations;
drop trigger if exists wa_tenant_immutable on messages;
drop trigger if exists wa_tenant_immutable on segments;
drop trigger if exists wa_tenant_immutable on campaigns;
drop trigger if exists wa_identity_tenant  on contact_identities;

alter table contacts      alter column tenant_id drop not null;
alter table conversations alter column tenant_id drop not null;
alter table messages      alter column tenant_id drop not null;
alter table segments      alter column tenant_id drop not null;
alter table campaigns     alter column tenant_id drop not null;

create unique index if not exists contacts_phone_idx on contacts (phone) where phone is not null;
create unique index if not exists contacts_email_idx on contacts (lower(email)) where email is not null;
drop index if exists contacts_tenant_phone_idx;
drop index if exists contacts_tenant_email_idx;
