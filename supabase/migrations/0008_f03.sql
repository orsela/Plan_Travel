-- Plan_Travel migration 0008_f03 · version 3.0.0-alpha.3 · F03 (super-admin invites a manager)
-- CHANGE 2026-10-05 F03-DB-01: invite tracking columns on public.manager_invites (send_count, last_sent_at, opened_at,
--   inviter_name), one active invite per email (partial unique index), and the admin list RPC
--   public.admin_trip_summary(). What changed from 0001–0007: 4 new columns + 1 check constraint + 1 index on
--   manager_invites, 1 new function. No new tables, no policy changes, no new client-writable grants
--   (manager_invites stays service-role only; the RPC is execute-only for authenticated and refuses non-admins).
--
-- Contract: docs/F03_spec.md §2 (decisions D5–D7). Idempotent: safe to run more than once.
-- Writes to manager_invites are done only by the Edge Function invite-manager with the service role.

-- ===========================================================================
-- 1. manager_invites: tracking columns
-- ===========================================================================
alter table public.manager_invites add column if not exists send_count   int not null default 1;
alter table public.manager_invites add column if not exists last_sent_at timestamptz;
alter table public.manager_invites add column if not exists opened_at    timestamptz;
alter table public.manager_invites add column if not exists inviter_name text;

do $f03$
begin
  if not exists (select 1 from pg_catalog.pg_constraint
                  where conrelid = 'public.manager_invites'::regclass and conname = 'manager_invites_inviter_name_len') then
    alter table public.manager_invites
      add constraint manager_invites_inviter_name_len
      check (inviter_name is null or char_length(inviter_name) between 1 and 40);
  end if;
  if not exists (select 1 from pg_catalog.pg_constraint
                  where conrelid = 'public.manager_invites'::regclass and conname = 'manager_invites_send_count_pos') then
    alter table public.manager_invites
      add constraint manager_invites_send_count_pos check (send_count >= 1);
  end if;
end
$f03$;

-- rows created before F03 (none expected): last send = creation
update public.manager_invites set last_sent_at = created_at where last_sent_at is null;

-- ===========================================================================
-- 2. One active invite per email (D5). Expiry is checked by the Edge Function: an expired-but-not-revoked invite
--    still holds the slot until the function revokes it (it does so automatically on the next create).
-- ===========================================================================
create unique index if not exists manager_invites_one_active_per_email
  on public.manager_invites (email)
  where used_at is null and revoked_at is null;

-- ===========================================================================
-- 3. Admin list (D6/D7): security definer RPC, not a view. Status is derived, never stored.
--    Returns one jsonb per row:
--      trip   {kind:'trip', id, name, start_date, end_date, status:'active'|'ended', managers:[display_name…],
--              countries:[code… by sort], member_count}
--      invite {kind:'invite', id, email, draft_name, created_at, last_sent_at, send_count, opened_at, expires_at,
--              status:'pending'|'expired'|'revoked'}            (unused invites only)
--    Never returns trip_kv, members other than active managers' display names, tokens or hashes.
-- ===========================================================================
create or replace function public.admin_trip_summary()
returns setof jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
begin
  if not public.is_platform_admin() then
    raise exception 'not_platform_admin' using errcode = '42501';
  end if;

  return query
  select pg_catalog.jsonb_build_object(
           'kind',         'trip',
           'id',           t.id,
           'name',         t.name,
           'start_date',   t.start_date,
           'end_date',     t.end_date,
           'status',       case when t.status = 'archived'
                                  or (t.end_date is not null and t.end_date < current_date)
                                then 'ended' else 'active' end,
           'managers',     coalesce((select pg_catalog.jsonb_agg(m.display_name order by m.joined_at, m.display_name)
                                       from public.trip_members m
                                      where m.trip_id = t.id and m.role = 'manager' and m.status = 'active'),
                                    '[]'::jsonb),
           'countries',    coalesce((select pg_catalog.jsonb_agg(c.country_code order by c.sort, c.country_code)
                                       from public.trip_countries c
                                      where c.trip_id = t.id),
                                    '[]'::jsonb),
           'member_count', (select count(*) from public.trip_members m
                             where m.trip_id = t.id and m.status = 'active'))
    from public.trips t
  union all
  select pg_catalog.jsonb_build_object(
           'kind',         'invite',
           'id',           i.id,
           'email',        i.email,
           'draft_name',   i.draft_name,
           'created_at',   i.created_at,
           'last_sent_at', coalesce(i.last_sent_at, i.created_at),
           'send_count',   i.send_count,
           'opened_at',    i.opened_at,
           'expires_at',   i.expires_at,
           'status',       case when i.revoked_at is not null then 'revoked'
                                when i.expires_at <= pg_catalog.now() then 'expired'
                                else 'pending' end)
    from public.manager_invites i
   where i.used_at is null;
end;
$$;

revoke all     on function public.admin_trip_summary() from public, anon;
grant  execute on function public.admin_trip_summary() to authenticated, service_role;
