-- 0002_identity.sql — a contact is a person, not a phone number.
--
-- Since 2026 WhatsApp can hide the phone number (usernames) and identify a
-- user by a business-scoped user id (BSUID) instead. Keying contacts on phone
-- silently forks one person into two contacts (or, when a number is recycled,
-- glues two people into one). This migration adds:
--
--   contact_identities   every key a person is known by: phone, bsuid,
--                        parent_bsuid — unique per tenant
--   wa_resolve_contact() the ONE function that turns "an event carrying these
--                        identifiers" into a contact: find, create, attach,
--                        merge duplicates, or move a recycled phone number
--   wa_merge_contacts()  folds one contact into another without losing a
--                        conversation, message, lead or opt-out
--   contact_identity_audit  what was merged/moved, with a snapshot to undo by hand
--
-- Keys, not attributes: phone / bsuid / parent_bsuid identify. A username does
-- not — it can be changed or released and re-taken by someone else — so it is
-- stored on the contact (wa_username) for display only.
--
-- Rules the resolver applies (see wa_identity_conflict):
--   * same bsuid or parent_bsuid  -> the same person, always
--   * two different bsuids (and no shared parent) -> two different people;
--     if they are fighting over a phone number, the number was recycled and
--     the person seen most recently keeps it
--   * anything else that shares a key -> merge
--
-- Run after 0001. Idempotent. Callable through Supabase RPC by service_role
-- only (anon/authenticated are revoked below).

-- ── tables ───────────────────────────────────────────────────────────────────
create table if not exists contact_identities (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id),
  contact_id uuid not null references contacts(id) on delete cascade,
  kind       text not null check (kind in ('phone', 'bsuid', 'parent_bsuid')),
  value      text not null check (value <> ''),
  first_seen timestamptz not null default now(),
  last_seen  timestamptz not null default now(),
  unique (tenant_id, kind, value)
);
create index if not exists contact_identities_contact_idx on contact_identities (contact_id);

-- No FK on the contact ids: an audit row must outlive the contact it describes.
create table if not exists contact_identity_audit (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id),
  action           text not null check (action in ('merge', 'move_identity')),
  contact_id       uuid,          -- survivor / new owner
  other_contact_id uuid,          -- merged-away / previous owner
  kind             text,
  value            text,
  reason           text,
  detail           jsonb,         -- snapshot of what was folded away
  at               timestamptz not null default now()
);
create index if not exists contact_identity_audit_tenant_idx on contact_identity_audit (tenant_id, at desc);

create or replace function wa_tg_identity_tenant() returns trigger
language plpgsql as $$
declare parent uuid;
begin
  select tenant_id into parent from contacts where id = new.contact_id;
  if parent is distinct from new.tenant_id then
    raise exception 'identity tenant does not match its contact''s tenant' using errcode = '23514';
  end if;
  return new;
end $$;
drop trigger if exists wa_identity_tenant on contact_identities;
create trigger wa_identity_tenant before insert or update of contact_id, tenant_id on contact_identities
  for each row execute function wa_tg_identity_tenant();

-- ── healing: legacy writers set contacts.phone / contacts.bsuid directly ─────
create or replace function wa_heal_contact(p_contact uuid) returns void
language sql as $$
  insert into contact_identities (tenant_id, contact_id, kind, value)
  select c.tenant_id, c.id, 'phone', wa_norm_phone(c.phone)
    from contacts c where c.id = p_contact and wa_norm_phone(c.phone) is not null
  on conflict (tenant_id, kind, value) do nothing;
  insert into contact_identities (tenant_id, contact_id, kind, value)
  select c.tenant_id, c.id, 'bsuid', c.bsuid
    from contacts c where c.id = p_contact and c.bsuid is not null and c.bsuid <> ''
  on conflict (tenant_id, kind, value) do nothing;
$$;

-- Idempotent, re-runnable any time (the n8n flows keep writing contacts.phone
-- until they call the resolver). Never merges: two contacts with the same
-- number are listed by contact_identity_conflicts for a human to look at.
create or replace function wa_backfill_identities() returns table (identities_added bigint, conflicts bigint)
language plpgsql as $$
declare before_n bigint; after_n bigint;
begin
  select count(*) into before_n from contact_identities;
  insert into contact_identities (tenant_id, contact_id, kind, value)
  select c.tenant_id, c.id, 'phone', wa_norm_phone(c.phone)
    from contacts c where wa_norm_phone(c.phone) is not null
  on conflict (tenant_id, kind, value) do nothing;
  insert into contact_identities (tenant_id, contact_id, kind, value)
  select c.tenant_id, c.id, 'bsuid', c.bsuid
    from contacts c where c.bsuid is not null and c.bsuid <> ''
  on conflict (tenant_id, kind, value) do nothing;
  select count(*) into after_n from contact_identities;
  return query select after_n - before_n,
    (select count(*) from contact_identity_conflicts);
end $$;

create or replace view contact_identity_conflicts as
  select c.tenant_id, c.id as contact_id, c.name, c.phone,
         ci.contact_id as owner_contact_id
    from contacts c
    join contact_identities ci
      on ci.tenant_id = c.tenant_id and ci.kind = 'phone'
     and ci.value = wa_norm_phone(c.phone)
   where ci.contact_id <> c.id;

-- ── is this the same person? ─────────────────────────────────────────────────
-- True when `other` provably is NOT the anchor person: they hold a different
-- key of a strong kind and share none of that kind (a shared parent_bsuid
-- always wins — it identifies a person across linked business portfolios).
create or replace function wa_identity_conflict(p_anchor uuid, p_other uuid,
                                                p_bsuid text, p_parent text)
returns boolean language sql stable as $$
  with a as (
    select kind, value from contact_identities
     where contact_id = p_anchor and kind in ('bsuid', 'parent_bsuid')
    union select 'bsuid', p_bsuid where p_bsuid is not null
    union select 'parent_bsuid', p_parent where p_parent is not null
  ), o as (
    select kind, value from contact_identities
     where contact_id = p_other and kind in ('bsuid', 'parent_bsuid')
  )
  select case
    when exists (select 1 from a join o using (kind, value) where kind = 'parent_bsuid')
      then false
    else exists (
      select 1 from (values ('bsuid'), ('parent_bsuid')) as k(kind)
       where exists (select 1 from a where a.kind = k.kind)
         and exists (select 1 from o where o.kind = k.kind)
         and not exists (select 1 from a join o using (kind, value) where kind = k.kind))
  end
$$;

-- ── merge ────────────────────────────────────────────────────────────────────
-- SECURITY DEFINER because folding a contact touches tables (leads,
-- follow_up_queue, activity_log) that a tenant-scoped role cannot see under
-- RLS — without it the final DELETE would cascade over rows the merge never
-- re-pointed. The guard below is what keeps it tenant-safe.
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
      update conversations sv set
        message_count     = sv.message_count + c.message_count,
        substantive_count = sv.substantive_count + c.substantive_count,
        last_active_at    = greatest(sv.last_active_at, c.last_active_at),
        created_at        = least(sv.created_at, c.created_at),
        -- never let the bot talk over a human who had taken the thread
        bot_paused        = sv.bot_paused or c.bot_paused,
        status            = case when 'human_takeover' in (sv.status, c.status)
                                 then 'human_takeover' else sv.status end
       where sv.id = sc;
      delete from conversations where id = c.id;
      n_conv_merged := n_conv_merged + 1;
    end if;
  end loop;

  update leads           set contact_id = p_survivor where contact_id = p_loser;
  update follow_up_queue set contact_id = p_survivor where contact_id = p_loser;
  update activity_log    set contact_id = p_survivor where contact_id = p_loser;
  update contact_identities set contact_id = p_survivor where contact_id = p_loser;

  -- The loser must be gone before the survivor can take over its phone/email
  -- (both are unique per tenant).
  delete from contacts where id = p_loser;

  update contacts set
    tags        = (select coalesce(array_agg(distinct t order by t), '{}')
                     from unnest(coalesce(s.tags, '{}') || coalesce(l.tags, '{}')) as t),
    -- compliance: an opt-out is never lost in a merge
    opt_out     = s.opt_out or l.opt_out,
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

  return jsonb_build_object(
    'survivor', p_survivor, 'merged', p_loser,
    'conversations_moved', n_conv_moved, 'conversations_merged', n_conv_merged,
    'messages_moved', n_msgs);
end $$;

-- ── resolve ──────────────────────────────────────────────────────────────────
create or replace function wa_resolve_contact(
  p_tenant   uuid,
  p_phone    text default null,
  p_bsuid    text default null,
  p_parent   text default null,
  p_username text default null,
  p_name     text default null,
  p_source   text default null
) returns table (contact_id uuid, created boolean, merged_ids uuid[], moved_from uuid[])
language plpgsql as $$
#variable_conflict use_column
declare
  v_phone  text := wa_norm_phone(p_phone);
  v_bsuid  text := nullif(btrim(p_bsuid), '');
  v_parent text := nullif(btrim(p_parent), '');
  v_user   text := nullif(btrim(p_username), '');
  v_name   text := nullif(btrim(p_name), '');
  k        text;
  matched  uuid[];
  anchor   uuid;
  other    uuid;
  v_created boolean := false;
  v_merged  uuid[] := '{}';
  v_moved   uuid[] := '{}';
begin
  if p_tenant is null then
    raise exception 'wa_resolve_contact needs a tenant' using errcode = '22023';
  end if;
  if v_phone is null and v_bsuid is null and v_parent is null then
    raise exception 'wa_resolve_contact needs a phone, a bsuid or a parent bsuid'
      using errcode = '22023';
  end if;

  -- One resolver at a time per identifier, always locked in the same order
  -- (no deadlock), so two events for the same new person cannot both create.
  for k in
    select distinct x from unnest(array[
      case when v_phone  is not null then p_tenant::text || ':phone:'  || v_phone  end,
      case when v_bsuid  is not null then p_tenant::text || ':bsuid:'  || v_bsuid  end,
      case when v_parent is not null then p_tenant::text || ':parent:' || v_parent end
    ]) as x where x is not null order by x
  loop
    perform pg_advisory_xact_lock(hashtextextended(k, 0));
  end loop;

  select array_agg(c.id order by c.created_at, c.id) into matched
    from contacts c
   where c.tenant_id = p_tenant and c.id in (
     select ci.contact_id from contact_identities ci
      where ci.tenant_id = p_tenant and (
            (ci.kind = 'phone'        and ci.value = v_phone)
         or (ci.kind = 'bsuid'        and ci.value = v_bsuid)
         or (ci.kind = 'parent_bsuid' and ci.value = v_parent))
     union
     select id from contacts
      where tenant_id = p_tenant and v_phone is not null and wa_norm_phone(phone) = v_phone
     union
     select id from contacts
      where tenant_id = p_tenant and v_bsuid is not null and bsuid = v_bsuid);

  if matched is not null then
    foreach other in array matched loop
      perform wa_heal_contact(other);
    end loop;

    -- the anchor is whoever holds the strongest key the event carries
    if v_parent is not null then
      select ci.contact_id into anchor from contact_identities ci
       where ci.tenant_id = p_tenant and ci.kind = 'parent_bsuid' and ci.value = v_parent;
    end if;
    if anchor is null and v_bsuid is not null then
      select ci.contact_id into anchor from contact_identities ci
       where ci.tenant_id = p_tenant and ci.kind = 'bsuid' and ci.value = v_bsuid;
    end if;
    -- nobody holds the event's strong keys: the oldest contact that could be
    -- this person (matched by phone only, and not provably somebody else)
    if anchor is null then
      foreach other in array matched loop
        if not wa_identity_conflict(null, other, v_bsuid, v_parent) then
          anchor := other;
          exit;
        end if;
      end loop;
    end if;
  end if;

  if anchor is null then
    -- Brand new — or every phone match belongs to a different person. The phone
    -- is set after any move below (contacts.phone is unique per tenant).
    insert into contacts (tenant_id, name, source_first_seen)
    values (p_tenant, coalesce(v_name, v_user, 'Unknown Lead'), p_source)
    returning id into anchor;
    v_created := true;
  end if;

  if matched is not null then
    foreach other in array matched loop
      continue when other = anchor;
      if wa_identity_conflict(anchor, other, v_bsuid, v_parent) then
        -- a different person who used to hold this number: it moves
        if v_phone is not null then
          delete from contact_identities
           where tenant_id = p_tenant and contact_id = other
             and kind = 'phone' and value = v_phone;
          update contacts set phone = null
           where id = other and wa_norm_phone(phone) = v_phone;
          insert into contact_identity_audit
            (tenant_id, action, contact_id, other_contact_id, kind, value, reason)
          values (p_tenant, 'move_identity', anchor, other, 'phone', v_phone, 'phone_reassigned');
          v_moved := v_moved || other;
        end if;
      else
        perform wa_merge_contacts(anchor, other, 'identity_event');
        v_merged := v_merged || other;
      end if;
    end loop;
  end if;

  if v_phone is not null then
    insert into contact_identities (tenant_id, contact_id, kind, value)
    values (p_tenant, anchor, 'phone', v_phone)
    on conflict (tenant_id, kind, value) do update set last_seen = now();
  end if;
  if v_bsuid is not null then
    insert into contact_identities (tenant_id, contact_id, kind, value)
    values (p_tenant, anchor, 'bsuid', v_bsuid)
    on conflict (tenant_id, kind, value) do update set last_seen = now();
  end if;
  if v_parent is not null then
    insert into contact_identities (tenant_id, contact_id, kind, value)
    values (p_tenant, anchor, 'parent_bsuid', v_parent)
    on conflict (tenant_id, kind, value) do update set last_seen = now();
  end if;

  update contacts set
    phone       = coalesce(v_phone, phone),
    bsuid       = coalesce(v_bsuid, bsuid),
    wa_username = coalesce(v_user, wa_username),
    name        = case when (coalesce(name, '') in ('', 'Unknown Lead') or name = wa_username)
                        and coalesce(v_name, v_user) is not null
                       then coalesce(v_name, v_user) else name end
   where id = anchor;

  return query select anchor, v_created, v_merged, v_moved;
end $$;

-- ── who may call these ───────────────────────────────────────────────────────
-- Supabase's default privileges hand every new function to anon and
-- authenticated, which would put wa_resolve_contact behind the public
-- publishable key. Take that away; service_role (n8n, the licence server) and
-- the tenant-scoped app role (0003) are the only callers.
revoke all on table contact_identities, contact_identity_audit, contact_identity_conflicts
  from public, anon, authenticated;
revoke all on function wa_heal_contact(uuid) from public, anon, authenticated;
revoke all on function wa_backfill_identities() from public, anon, authenticated;
revoke all on function wa_identity_conflict(uuid, uuid, text, text) from public, anon, authenticated;
revoke all on function wa_merge_contacts(uuid, uuid, text) from public, anon, authenticated;
revoke all on function wa_resolve_contact(uuid, text, text, text, text, text, text)
  from public, anon, authenticated;
alter table contact_identities      enable row level security;
alter table contact_identity_audit  enable row level security;
alter view  contact_identity_conflicts set (security_invoker = on);

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on table contact_identities, contact_identity_audit, contact_identity_conflicts to service_role;
    grant execute on function wa_heal_contact(uuid) to service_role;
    grant execute on function wa_backfill_identities() to service_role;
    grant execute on function wa_identity_conflict(uuid, uuid, text, text) to service_role;
    grant execute on function wa_merge_contacts(uuid, uuid, text) to service_role;
    grant execute on function wa_resolve_contact(uuid, text, text, text, text, text, text) to service_role;
  end if;
end $$;

-- ── one-time fill of the identity table from today's contacts ────────────────
select * from wa_backfill_identities();
