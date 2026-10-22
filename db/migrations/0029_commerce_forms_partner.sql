-- 0029_commerce_forms_partner.sql — catalogue, orders and payments; WhatsApp forms; coexistence; the audit log, single sign-on, branding and resellers.
--
--   commerce_config   one row per workspace: the Meta catalogue, currency, tax and shipping, and how customers pay (Meta's own payments, or Razorpay links)
--   products          the products Prism sends in chats (kept here; optionally pushed to / pulled from the Meta catalogue)
--   orders            a cart a customer sent from WhatsApp, or an order made by a person; money is whole paise, never decimals
--   payments          one attempt to collect an order: Meta payment or Razorpay link, its state, and the reply we last saw from the gateway
--   refunds           money sent back, per payment
--   forms             a WhatsApp Flow (a form that opens inside the chat) in Prism's own model plus the Flow JSON it becomes
--   form_responses    what a customer filled in
--   flow_keys         the RSA key pair a workspace's forms use to talk to Meta's endpoint calls (the private half is stored encrypted)
--   wa_audit_log      who changed what, when (enterprise). NOT `audit_log`: the licence server keeps a table of that name in the same database
--   tenant_branding   the name, logo and colour a reseller shows instead of Prism's
--   sso_config / sso_states   single sign-on (OIDC) settings and the sign-ins in progress
--   tenant_parent / commission_ledger   a workspace that is a reseller's customer, and what the reseller earns from it
--   phone_numbers.coexistence   a number that is also in use in the WhatsApp Business app on the owner's phone
--
-- It also replaces wa_merge_contacts (see the section near the end) so orders, form answers and automatic-message rows follow a merged person.
-- Safe to run more than once. Additive. Run after 0028.

do $$
begin
  if to_regclass('public.contacts') is null or to_regclass('public.phone_numbers') is null then
    raise exception '0029_commerce_forms_partner: contacts / phone_numbers are missing. Run the earlier migrations first.';
  end if;
end $$;

-- ── commerce ────────────────────────────────────────────────────────────────

create table if not exists commerce_config (
  tenant_id              uuid primary key references tenants(id),
  catalog_id             text,
  catalog_name           text,
  currency               text not null default 'INR',
  tax_percent            numeric(5,2) not null default 0,
  shipping_paise         bigint not null default 0,
  order_expiry_hours     integer not null default 48,
  gateway                text not null default 'none' check (gateway in ('none', 'meta', 'razorpay')),
  payment_configuration  text,                               -- the name Meta gives the payment setup on the WABA
  razorpay_key_id        text,
  razorpay_secret_enc    bytea,
  razorpay_webhook_enc   bytea,
  key_ver                integer,
  updated_at             timestamptz not null default now()
);

create table if not exists products (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  retailer_id      text not null,                            -- our id for it; Meta's catalogue calls this retailer_id
  name             text not null,
  description      text,
  price_paise      bigint not null check (price_paise >= 0),
  sale_price_paise bigint check (sale_price_paise is null or sale_price_paise >= 0),
  currency         text not null default 'INR',
  image_url        text,
  url              text,
  category         text,
  available        boolean not null default true,
  meta_product_id  text,
  synced_at        timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (tenant_id, retailer_id)
);
create index if not exists products_tenant_idx on products (tenant_id, available, name);

create table if not exists orders (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  contact_id       uuid references contacts(id) on delete set null,
  conversation_id  uuid,
  number_id        uuid references phone_numbers(id) on delete set null,
  reference_id     text not null,                            -- what the customer and the gateway see; at most 35 safe characters
  source           text not null default 'manual' check (source in ('cart', 'manual', 'agent', 'flow')),
  status           text not null default 'draft' check (status in
                     ('draft', 'awaiting_payment', 'paid', 'processing', 'shipped', 'completed', 'cancelled', 'failed', 'refunded')),
  items            jsonb not null default '[]'::jsonb,       -- [{retailer_id, name, quantity, unit_paise, line_paise}]
  currency         text not null default 'INR',
  subtotal_paise   bigint not null default 0,
  tax_paise        bigint not null default 0,
  shipping_paise   bigint not null default 0,
  discount_paise   bigint not null default 0,
  total_paise      bigint not null default 0,
  address          jsonb,
  note             text,
  expires_at       timestamptz,
  created_by       uuid,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (tenant_id, reference_id)
);
create index if not exists orders_tenant_idx on orders (tenant_id, created_at desc);
create index if not exists orders_contact_idx on orders (contact_id);

create table if not exists payments (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  order_id      uuid not null references orders(id) on delete cascade,
  reference_id  text not null,
  provider      text not null check (provider in ('meta', 'razorpay')),
  status        text not null default 'created' check (status in
                  ('created', 'pending', 'captured', 'failed', 'expired', 'cancelled', 'refunded', 'partially_refunded')),
  amount_paise  bigint not null,
  refunded_paise bigint not null default 0,
  currency      text not null default 'INR',
  method        text,
  txn_id        text,                                        -- the gateway's payment id, once there is one
  provider_ref  text,                                        -- Razorpay's payment link id
  link_url      text,
  wamid         text,
  last_error    text,
  checked_at    timestamptz,
  paid_at       timestamptz,
  raw           jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists payments_tenant_idx on payments (tenant_id, created_at desc);
create index if not exists payments_open_idx on payments (tenant_id, status) where status in ('created', 'pending');
create index if not exists payments_ref_idx on payments (tenant_id, reference_id);
create index if not exists payments_provider_ref_idx on payments (provider_ref) where provider_ref is not null;

create table if not exists refunds (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id),
  payment_id    uuid not null references payments(id) on delete cascade,
  amount_paise  bigint not null check (amount_paise > 0),
  speed         text not null default 'normal' check (speed in ('normal', 'instant')),
  status        text not null default 'pending' check (status in ('pending', 'processed', 'failed', 'recorded')),
  provider_ref  text,
  reason        text,
  error         text,
  created_by    uuid,
  created_at    timestamptz not null default now()
);
create index if not exists refunds_payment_idx on refunds (payment_id);

-- ── forms ───────────────────────────────────────────────────────────────────

create table if not exists forms (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id),
  name              text not null,
  template          text,                                    -- which starting point it was made from
  categories        jsonb not null default '["LEAD_GENERATION"]'::jsonb,
  screens           jsonb not null default '[]'::jsonb,      -- Prism's model: [{id, title, fields:[...], button}]
  flow_json         jsonb,                                   -- what the screens become for Meta
  meta_flow_id      text,
  status            text not null default 'draft' check (status in ('draft', 'published', 'deprecated', 'throttled', 'blocked')),
  version           integer not null default 1,
  endpoint          boolean not null default false,          -- true when answers come to us live (slots, products) rather than at the end
  mapping           jsonb not null default '{}'::jsonb,      -- form field -> contact attribute
  tag_on_submit     text,
  settings          jsonb not null default '{}'::jsonb,      -- thank_you, availability (days, hours, slot length, capacity) for booking forms
  problems          jsonb not null default '[]'::jsonb,      -- what Meta said was wrong with the last upload
  responses         integer not null default 0,
  published_at      timestamptz,
  created_by        uuid,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists forms_tenant_idx on forms (tenant_id, status);
create unique index if not exists forms_meta_flow_idx on forms (meta_flow_id) where meta_flow_id is not null;

create table if not exists form_responses (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  form_id          uuid references forms(id) on delete set null,
  contact_id       uuid references contacts(id) on delete set null,
  conversation_id  uuid,
  flow_token       text,
  answers          jsonb not null,
  created_at       timestamptz not null default now()
);
create index if not exists form_responses_form_idx on form_responses (form_id, created_at desc);
create index if not exists form_responses_tenant_idx on form_responses (tenant_id, created_at desc);
create unique index if not exists form_responses_token_idx on form_responses (tenant_id, flow_token) where flow_token is not null;

create table if not exists flow_keys (
  tenant_id     uuid primary key references tenants(id),
  public_pem    text not null,
  private_enc   bytea not null,
  key_ver       integer not null,
  uploaded_at   timestamptz,                                 -- when Meta last accepted the public half
  created_at    timestamptz not null default now()
);

-- ── coexistence ─────────────────────────────────────────────────────────────

alter table phone_numbers add column if not exists coexistence boolean not null default false;
alter table phone_numbers add column if not exists coexistence_state jsonb;

-- ── audit log, SSO, branding, resellers ─────────────────────────────────────

create table if not exists wa_audit_log (
  id           bigserial primary key,
  tenant_id    uuid not null references tenants(id),
  at           timestamptz not null default now(),
  actor_id     uuid,
  actor_label  text,
  source       text not null default 'app' check (source in ('app', 'api', 'system', 'sso')),
  action       text not null,
  target_kind  text,
  target_id    text,
  summary      text,
  detail       jsonb
);
create index if not exists wa_audit_log_tenant_idx on wa_audit_log (tenant_id, at desc);
create index if not exists wa_audit_log_action_idx on wa_audit_log (tenant_id, action, at desc);

create table if not exists tenant_branding (
  tenant_id      uuid primary key references tenants(id),
  name           text,
  colour         text,
  logo_asset_id  uuid,
  support_email  text,
  support_url    text,
  custom_domain  text,
  hide_vendor    boolean not null default false,
  updated_at     timestamptz not null default now()
);

create table if not exists sso_config (
  tenant_id        uuid primary key references tenants(id),
  enabled          boolean not null default false,
  issuer           text,
  client_id        text,
  client_secret_enc bytea,
  key_ver          integer,
  authorize_url    text,
  token_url        text,
  jwks_url         text,
  allowed_domains  jsonb not null default '[]'::jsonb,
  role_claim       text,
  role_map         jsonb not null default '{}'::jsonb,
  default_role     text not null default 'agent',
  require_sso      boolean not null default false,
  updated_at       timestamptz not null default now()
);

create table if not exists sso_states (
  state        text primary key,
  tenant_id    uuid not null references tenants(id),
  nonce        text not null,
  verifier     text not null,
  redirect_uri text not null,
  status       text not null default 'waiting' check (status in ('waiting', 'ready', 'failed', 'used')),
  result       jsonb,
  error        text,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null
);
create index if not exists sso_states_tenant_idx on sso_states (tenant_id, created_at desc);

-- who a signed-in person is (their company e-mail), and the proof of a sign-in: only a hash of the token is kept
alter table agents add column if not exists email text;
create index if not exists agents_email_idx on agents (tenant_id, lower(email)) where email is not null;

create table if not exists sso_sessions (
  token_hash   text primary key,
  tenant_id    uuid not null references tenants(id),
  agent_id     uuid not null references agents(id) on delete cascade,
  email        text,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null
);
create index if not exists sso_sessions_agent_idx on sso_sessions (agent_id, expires_at desc);

-- a customer workspace hands its reseller a one-time code; redeeming it (below) links the two
create table if not exists partner_invites (
  code         text primary key,
  tenant_id    uuid not null references tenants(id),
  expires_at   timestamptz not null,
  used_at      timestamptz,
  created_at   timestamptz not null default now()
);

create table if not exists tenant_parent (
  child_tenant_id   uuid primary key references tenants(id),
  parent_tenant_id  uuid not null references tenants(id),
  commission_percent numeric(5,2) not null default 0,
  note              text,
  created_at        timestamptz not null default now(),
  check (child_tenant_id <> parent_tenant_id)
);
create index if not exists tenant_parent_parent_idx on tenant_parent (parent_tenant_id);

create table if not exists commission_ledger (
  id                bigserial primary key,
  parent_tenant_id  uuid not null references tenants(id),
  child_tenant_id   uuid not null references tenants(id),
  period            text not null,                           -- "2026-10"
  base_paise        bigint not null,
  percent           numeric(5,2) not null,
  amount_paise      bigint not null,
  status            text not null default 'accrued' check (status in ('accrued', 'paid')),
  note              text,
  created_at        timestamptz not null default now(),
  unique (parent_tenant_id, child_tenant_id, period)
);

-- ── merging two contacts must not lose what they ordered or filled in ──────────────────────────────────────────────
-- Same function, same signature, same grants as 0021 (CREATE OR REPLACE keeps them); the body also re-points the tables that now hang off a contact:
-- orders, form_responses (both ON DELETE SET NULL, so a merge would have detached them) and the automatic-message tables of 0027.

create or replace function wa_merge_contacts(p_survivor uuid, p_loser uuid,
                                             p_reason text default 'manual')
returns jsonb
language plpgsql security definer set search_path = public as $$
#variable_conflict use_column
declare
  s contacts;
  l contacts;
  c conversations;
  sc uuid;
  scope text;
  n_conv_moved int := 0;
  n_conv_merged int := 0;
  n_msgs int := 0;
  n int;
  snapshot jsonb;
begin
  if p_survivor is null or p_loser is null or p_survivor = p_loser then
    raise exception 'wa_merge_contacts needs two different contacts' using errcode = '22023';
  end if;

  perform 1 from contacts where id in (p_survivor, p_loser) order by id for update;
  select * into s from contacts where id = p_survivor;
  select * into l from contacts where id = p_loser;
  if s.id is null or l.id is null then
    raise exception 'contact not found' using errcode = 'P0002';
  end if;
  if s.tenant_id <> l.tenant_id then
    raise exception 'cannot merge contacts of different tenants' using errcode = '42501';
  end if;
  scope := nullif(current_setting('app.tenant_id', true), '');
  if scope is not null and s.tenant_id <> scope::uuid then
    raise exception 'contact belongs to another tenant' using errcode = '42501';
  end if;

  snapshot := jsonb_build_object(
    'contact', to_jsonb(l),
    'identities', (select coalesce(jsonb_agg(to_jsonb(ci)), '[]'::jsonb)
                     from contact_identities ci where ci.contact_id = p_loser));

  -- conversations: one per (contact, channel), so colliding ones are folded
  for c in select * from conversations where contact_id = p_loser order by id loop
    select id into sc from conversations
     where contact_id = p_survivor and channel = c.channel;
    if sc is null then
      update conversations set contact_id = p_survivor where id = c.id;
      n_conv_moved := n_conv_moved + 1;
    else
      update messages set conversation_id = sc where conversation_id = c.id;
      get diagnostics n = row_count;
      n_msgs := n_msgs + n;
      update follow_up_queue set conversation_id = sc where conversation_id = c.id;
      update leads set conversation_id = sc where conversation_id = c.id;
      update conversation_notes  set conversation_id = sc where conversation_id = c.id;
      update conversation_events set conversation_id = sc where conversation_id = c.id;
      update conversations sv set
        message_count     = sv.message_count + c.message_count,
        substantive_count = sv.substantive_count + c.substantive_count,
        last_active_at    = greatest(sv.last_active_at, c.last_active_at),
        created_at        = least(sv.created_at, c.created_at),
        -- never let the bot talk over a human who had taken the thread
        bot_paused        = sv.bot_paused or c.bot_paused,
        assignee_id       = coalesce(sv.assignee_id, c.assignee_id),
        last_inbound_at   = greatest(sv.last_inbound_at, c.last_inbound_at),
        status            = case when 'human_takeover' in (sv.status, c.status) then 'human_takeover'
                                 when 'intervened' in (sv.status, c.status) then 'intervened'
                                 else sv.status end
       where sv.id = sc;
      delete from conversations where id = c.id;
      n_conv_merged := n_conv_merged + 1;
    end if;
  end loop;

  update leads           set contact_id = p_survivor where contact_id = p_loser;
  update follow_up_queue set contact_id = p_survivor where contact_id = p_loser;
  update activity_log    set contact_id = p_survivor where contact_id = p_loser;
  update contact_identities set contact_id = p_survivor where contact_id = p_loser;

  -- (0013) everything 0004-0009 hangs off a contact. Nothing may be lost to the loser's DELETE.
  update consent_ledger set contact_id = p_survivor where contact_id = p_loser;
  -- attributes: the survivor's value wins; the loser contributes the keys the survivor lacks
  update contact_attributes la set contact_id = p_survivor
   where la.contact_id = p_loser
     and not exists (select 1 from contact_attributes sa where sa.contact_id = p_survivor and sa.key = la.key);
  -- both took part in one campaign: keep the survivor's row, and point clicks and links at it
  update click_events ce set recipient_id = sr.id
    from campaign_recipients lr join campaign_recipients sr
      on sr.campaign_id = lr.campaign_id and sr.contact_id = p_survivor
   where ce.recipient_id = lr.id and lr.contact_id = p_loser;
  update short_links sl set recipient_id = sr.id
    from campaign_recipients lr join campaign_recipients sr
      on sr.campaign_id = lr.campaign_id and sr.contact_id = p_survivor
   where sl.recipient_id = lr.id and lr.contact_id = p_loser;
  update campaign_recipients lr set contact_id = p_survivor
   where lr.contact_id = p_loser
     and not exists (select 1 from campaign_recipients sr
                      where sr.campaign_id = lr.campaign_id and sr.contact_id = p_survivor);
  update segment_members lm set contact_id = p_survivor
   where lm.contact_id = p_loser
     and not exists (select 1 from segment_members sm
                      where sm.segment_id = lm.segment_id and sm.contact_id = p_survivor);
  update click_events set contact_id = p_survivor where contact_id = p_loser;
  update short_links  set contact_id = p_survivor where contact_id = p_loser;
  update cart_checkouts set contact_id = p_survivor where contact_id = p_loser;
  -- (0021) the timeline of what happened to the person (imports, tags, notes) follows them
  update contact_events set contact_id = p_survivor where contact_id = p_loser;
  -- (0029) what the person ordered, filled in, and where they stand in an automatic message follows them too. A message an automatic rule
  -- already sent the survivor for the same occasion is not sent again, so the loser's duplicate of it is simply dropped with the loser.
  update orders set contact_id = p_survivor where contact_id = p_loser;
  update form_responses set contact_id = p_survivor where contact_id = p_loser;
  update auto_log lg set contact_id = p_survivor
   where lg.contact_id = p_loser
     and not exists (select 1 from auto_log sg where sg.auto_id = lg.auto_id and sg.contact_id = p_survivor and sg.occasion = lg.occasion);
  update auto_enrolments le set contact_id = p_survivor
   where le.contact_id = p_loser
     and not exists (select 1 from auto_enrolments se where se.auto_id = le.auto_id and se.contact_id = p_survivor);

  -- The loser must be gone before the survivor can take over its phone/email
  -- (both are unique per tenant).
  delete from contacts where id = p_loser;

  update contacts set
    tags        = (select coalesce(array_agg(distinct t order by t), '{}')
                     from unnest(coalesce(s.tags, '{}') || coalesce(l.tags, '{}')) as t),
    -- compliance: an opt-out is never lost in a merge
    opt_out     = s.opt_out or l.opt_out,
    -- (0013) consent is only "in" if nobody said "out"; blocked and recency carry over
    opted_in    = (s.opted_in or l.opted_in) and not (s.opt_out or l.opt_out),
    blocked     = s.blocked or l.blocked,
    first_msg_tag = coalesce(s.first_msg_tag, l.first_msg_tag),
    last_seen   = greatest(s.last_seen, l.last_seen),
    -- a placeholder (blank, "Unknown Lead", or just the username) yields to a real name
    name        = case when (coalesce(s.name, '') in ('', 'Unknown Lead') or s.name = s.wa_username)
                        and coalesce(l.name, '') not in ('', 'Unknown Lead')
                        and l.name is distinct from l.wa_username
                       then l.name else s.name end,
    email       = coalesce(s.email, l.email),
    phone       = coalesce(s.phone, l.phone),
    bsuid       = coalesce(s.bsuid, l.bsuid),
    wa_username = coalesce(s.wa_username, l.wa_username),
    company     = coalesce(s.company, l.company),
    job_title   = coalesce(s.job_title, l.job_title),
    location    = coalesce(s.location, l.location),
    corporate_domain  = coalesce(s.corporate_domain, l.corporate_domain),
    source_first_seen = coalesce(s.source_first_seen, l.source_first_seen),
    created_at  = least(s.created_at, l.created_at)
   where id = p_survivor;

  insert into contact_identity_audit (tenant_id, action, contact_id, other_contact_id, reason, detail)
  values (s.tenant_id, 'merge', p_survivor, p_loser, p_reason, snapshot);

  insert into contact_events (tenant_id, contact_id, kind, detail)
  values (s.tenant_id, p_survivor, 'merged', jsonb_build_object('merged_name', l.name, 'merged_phone', l.phone, 'reason', p_reason));

  return jsonb_build_object(
    'survivor', p_survivor, 'merged', p_loser,
    'conversations_moved', n_conv_moved, 'conversations_merged', n_conv_merged,
    'messages_moved', n_msgs);
end $$;

-- ── row-level security: a workspace sees only its own rows ──────────────────

do $$
declare t text;
begin
  foreach t in array array['commerce_config', 'products', 'orders', 'payments', 'refunds', 'forms', 'form_responses', 'flow_keys',
                           'wa_audit_log', 'tenant_branding', 'sso_config', 'sso_states', 'sso_sessions', 'partner_invites'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists wa_tenant_isolation on %I', t);
    execute format('create policy wa_tenant_isolation on %I for all to wa_app'
                   ' using (tenant_id = wa_current_tenant()) with check (tenant_id = wa_current_tenant())', t);
    execute format('grant select, insert, update, delete on %I to wa_app', t);
    execute format('revoke all on table %I from anon, authenticated', t);
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant all on table %I to service_role', t);
    end if;
  end loop;

  -- a reseller sees its customers' links and its own ledger; a customer sees the link that names it
  foreach t in array array['tenant_parent', 'commission_ledger'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists wa_tenant_isolation on %I', t);
    execute format('revoke all on table %I from anon, authenticated', t);
    execute format('grant select, insert, update, delete on %I to wa_app', t);
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant all on table %I to service_role', t);
    end if;
  end loop;
  drop policy if exists wa_tenant_isolation on tenant_parent;
  create policy wa_tenant_isolation on tenant_parent for all to wa_app
    using (parent_tenant_id = wa_current_tenant() or child_tenant_id = wa_current_tenant())
    with check (parent_tenant_id = wa_current_tenant());
  drop policy if exists wa_tenant_isolation on commission_ledger;
  create policy wa_tenant_isolation on commission_ledger for all to wa_app
    using (parent_tenant_id = wa_current_tenant()) with check (parent_tenant_id = wa_current_tenant());

  execute 'grant usage, select on sequence wa_audit_log_id_seq to wa_app';
  execute 'grant usage, select on sequence commission_ledger_id_seq to wa_app';
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant usage, select on sequence wa_audit_log_id_seq to service_role';
    execute 'grant usage, select on sequence commission_ledger_id_seq to service_role';
  end if;
end $$;

-- A reseller may read how busy a customer workspace is (message counts only) and nothing else of it. The function checks the link itself.
create or replace function wa_child_activity(parent uuid, child uuid, since timestamptz, until timestamptz)
returns table (sent bigint, received bigint, contacts bigint)
language sql security definer set search_path = public as $fn$
  select
    (select count(*) from messages m where m.tenant_id = child and m.direction = 'out' and m.created_at >= since and m.created_at < until),
    (select count(*) from messages m where m.tenant_id = child and m.direction = 'in' and m.created_at >= since and m.created_at < until),
    (select count(*) from contacts c where c.tenant_id = child)
  where exists (select 1 from tenant_parent p where p.child_tenant_id = child and p.parent_tenant_id = parent);
$fn$;
revoke all on function wa_child_activity(uuid, uuid, timestamptz, timestamptz) from public;
grant execute on function wa_child_activity(uuid, uuid, timestamptz, timestamptz) to wa_app;

-- Redeeming a customer's code links it to the reseller that holds the code. The customer's own row cannot be read by the reseller; this function is the only door.
create or replace function wa_redeem_partner_code(code_in text, parent uuid, percent numeric, note_in text)
returns uuid language plpgsql security definer set search_path = public as $fn$
declare child uuid;
begin
  select tenant_id into child from partner_invites where code = code_in and used_at is null and expires_at > now() for update;
  if child is null then
    return null;
  end if;
  if child = parent then
    raise exception 'a workspace cannot be its own reseller' using errcode = '22023';
  end if;
  if exists (select 1 from tenant_parent where child_tenant_id = child) then
    raise exception 'this workspace already has a reseller' using errcode = '23505';
  end if;
  insert into tenant_parent (child_tenant_id, parent_tenant_id, commission_percent, note) values (child, parent, percent, note_in);
  update partner_invites set used_at = now() where code = code_in;
  return child;
end $fn$;
revoke all on function wa_redeem_partner_code(text, uuid, numeric, text) from public;
grant execute on function wa_redeem_partner_code(text, uuid, numeric, text) to wa_app;

-- A reseller sees the name of a customer workspace it is linked to (nothing else of the tenants table).
create or replace function wa_child_name(parent uuid, child uuid) returns text language sql security definer set search_path = public as $fn$
  select t.name from tenants t where t.id = child
    and exists (select 1 from tenant_parent p where p.child_tenant_id = child and p.parent_tenant_id = parent);
$fn$;
revoke all on function wa_child_name(uuid, uuid) from public;
grant execute on function wa_child_name(uuid, uuid) to wa_app;
