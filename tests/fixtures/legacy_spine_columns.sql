-- shakti_multitenant_spine.sql: the tenant_id columns on contacts/leads.
alter table contacts add column if not exists tenant_id uuid references tenants(id);
alter table leads    add column if not exists tenant_id uuid references tenants(id);
create index if not exists idx_contacts_tenant on contacts(tenant_id);
create index if not exists idx_leads_tenant    on leads(tenant_id);
