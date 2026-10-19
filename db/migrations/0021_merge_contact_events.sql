-- 0021_merge_contact_events.sql — merging two contacts must not lose the person's timeline.
--
-- 0018 added contact_events (what happened to a contact: imported, tagged, noted...), which hangs off a contact with
-- ON DELETE CASCADE, so wa_merge_contacts deleting the duplicate would have deleted its history with it. The history is
-- moved to the survivor, and the merge itself is written to the timeline.
--
-- Same function, same signature, same grants (CREATE OR REPLACE keeps them); only the body is extended. Safe to run more than
-- once. Run after 0018.

do $$
begin
  if to_regclass('public.contact_events') is null then
    raise exception '0021_merge_contact_events: contact_events is missing. Run 0018_contact_book.sql first.';
  end if;
end $$;

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
