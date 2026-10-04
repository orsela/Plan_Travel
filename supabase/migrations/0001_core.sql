-- Plan_Travel migration 0001_core · version 3.0.0-alpha.1 · F01
-- CHANGE 2026-10-04 F01: initial schema — trips, members, join codes, invites, relink tokens, trip_kv, helper functions, RLS. (No previous version.)
--
-- Contract: docs/F01_spec.md §1–§4. Runs in a single transaction on a fresh Supabase project (Postgres 17).
-- Writes to trips / trip_members / trip_join_codes / manager_invites / relink_tokens / platform_admins
-- are done by Edge Functions with the service role (bypasses RLS).

-- ===========================================================================
-- 1. Tables
-- ===========================================================================

create table public.platform_admins (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  created_at timestamptz default now()
);

create table public.trips (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  start_date    date,
  end_date      date,
  home_currency char(3) not null default 'ILS',
  default_lang  text not null default 'he',
  default_role  text not null default 'editor',
  status        text not null default 'active',
  created_by    uuid references auth.users (id) on delete set null,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now(),
  constraint trips_name_len        check (char_length(name) between 1 and 80),
  constraint trips_dates_order     check (start_date is null or end_date is null or end_date >= start_date),
  constraint trips_home_currency   check (home_currency ~ '^[A-Z]{3}$'),
  constraint trips_default_lang    check (default_lang ~ '^[a-z]{2}(-[A-Z]{2})?$'),
  constraint trips_default_role    check (default_role in ('editor', 'viewer')),
  constraint trips_status          check (status in ('active', 'archived'))
);

create table public.trip_join_codes (
  trip_id    uuid primary key references public.trips (id) on delete cascade,
  code       text not null unique,
  version    int not null default 1,
  rotated_at timestamptz default now(),
  constraint trip_join_codes_code_format check (code ~ '^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$'),
  constraint trip_join_codes_version     check (version >= 1)
);

create table public.trip_countries (
  trip_id      uuid not null references public.trips (id) on delete cascade,
  country_code char(2) not null,
  sort         int not null default 0,
  primary key (trip_id, country_code),
  constraint trip_countries_code_format check (country_code ~ '^[A-Z]{2}$')
);

create table public.trip_currencies (
  trip_id       uuid not null references public.trips (id) on delete cascade,
  currency_code char(3) not null,
  kind          text not null,
  sort          int not null default 0,
  primary key (trip_id, currency_code),
  constraint trip_currencies_code_format check (currency_code ~ '^[A-Z]{3}$'),
  constraint trip_currencies_kind        check (kind in ('base', 'destination', 'added'))
);

create table public.trip_members (
  id           uuid primary key default gen_random_uuid(),
  trip_id      uuid not null references public.trips (id) on delete cascade,
  user_id      uuid not null references auth.users (id) on delete cascade,
  display_name text not null,
  role         text not null,
  status       text not null default 'pending',
  joined_at    timestamptz default now(),
  approved_at  timestamptz,
  last_seen_at timestamptz,
  constraint trip_members_trip_user_key   unique (trip_id, user_id),
  constraint trip_members_display_name    check (char_length(display_name) between 1 and 40),
  constraint trip_members_role            check (role in ('manager', 'editor', 'viewer')),
  constraint trip_members_status          check (status in ('pending', 'active', 'removed'))
);

create table public.manager_invites (
  id          uuid primary key default gen_random_uuid(),
  email       text not null,
  token_hash  text not null unique,
  draft_name  text,
  invited_by  uuid references auth.users (id) on delete set null,
  expires_at  timestamptz not null,
  used_at     timestamptz,
  revoked_at  timestamptz,
  trip_id     uuid references public.trips (id) on delete set null,
  created_at  timestamptz default now(),
  constraint manager_invites_email_lower check (email = lower(email))
);

create table public.relink_tokens (
  id          uuid primary key default gen_random_uuid(),
  member_id   uuid not null references public.trip_members (id) on delete cascade,
  token_hash  text not null unique,
  expires_at  timestamptz not null,
  used_at     timestamptz,
  created_by  uuid references auth.users (id) on delete set null,
  created_at  timestamptz default now()
);

create table public.trip_kv (
  trip_id    uuid not null references public.trips (id) on delete cascade,
  key        text not null,
  value      text,
  updated_at timestamptz not null default now(),
  updated_by uuid default auth.uid(),
  primary key (trip_id, key),
  constraint trip_kv_key_len check (char_length(key) between 1 and 200)
);

-- ===========================================================================
-- Indexes (FK columns that are not the leading PK/unique column, + members by status)
-- ===========================================================================

create index trips_created_by_idx           on public.trips (created_by);
create index trip_members_user_id_idx       on public.trip_members (user_id);
create index trip_members_trip_status_idx   on public.trip_members (trip_id, status);
create index manager_invites_trip_id_idx    on public.manager_invites (trip_id);
create index manager_invites_invited_by_idx on public.manager_invites (invited_by);
create index relink_tokens_member_id_idx    on public.relink_tokens (member_id);
create index relink_tokens_created_by_idx   on public.relink_tokens (created_by);

-- ===========================================================================
-- 2. Database-level rules (triggers)
-- Trigger functions are VOLATILE on purpose: each query gets a fresh snapshot,
-- so during an ON DELETE CASCADE from trips (RI action runs after the parent row
-- was deleted, in a later command) the parent row is already invisible and the
-- "parent still exists?" check correctly lets the cascade through.
-- ===========================================================================

-- 2.4 updated_at auto-maintenance (trips, trip_kv)
create function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := pg_catalog.now();
  return new;
end;
$$;

create trigger trips_set_updated_at
  before update on public.trips
  for each row execute function public.set_updated_at();

-- trip_kv: keep updated_at fresh and forbid moving a key to another trip
create function public.trip_kv_before_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.trip_id is distinct from old.trip_id then
    raise exception 'trip_kv_trip_locked: a key cannot be moved to another trip'
      using errcode = 'check_violation';
  end if;
  new.updated_at := pg_catalog.now();
  return new;
end;
$$;

create trigger trip_kv_before_update
  before update on public.trip_kv
  for each row execute function public.trip_kv_before_update();

-- 2.1 Max 10 currencies per trip (AFTER trigger so multi-row inserts are counted in full)
create function public.enforce_currency_limit()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count int;
begin
  -- serialize concurrent currency inserts for the same trip (compatible with FK KEY SHARE locks)
  perform 1 from public.trips t where t.id = new.trip_id for no key update;

  select count(*) into v_count
    from public.trip_currencies c
   where c.trip_id = new.trip_id;

  if v_count > 10 then
    raise exception 'currency_limit: a trip can have at most 10 currencies (trip %)', new.trip_id
      using errcode = 'check_violation';
  end if;
  return null;
end;
$$;

create trigger trip_currencies_limit
  after insert or update of trip_id on public.trip_currencies
  for each row execute function public.enforce_currency_limit();

-- 2.2 Base currencies are locked (except when the parent trip is being deleted)
create function public.enforce_base_currency_lock()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if old.kind <> 'base' then
    return case when tg_op = 'DELETE' then old else new end;
  end if;

  if tg_op = 'UPDATE'
     and new.kind = 'base'
     and new.trip_id = old.trip_id
     and new.currency_code = old.currency_code then
    return new;  -- still the same base currency (e.g. sort change)
  end if;

  -- parent trip being deleted (ON DELETE CASCADE): parent row is already gone
  if not exists (select 1 from public.trips t where t.id = old.trip_id) then
    return case when tg_op = 'DELETE' then old else new end;
  end if;

  raise exception 'base_currency_locked: base currency % of trip % cannot be removed or changed',
    old.currency_code, old.trip_id
    using errcode = 'check_violation';
end;
$$;

create trigger trip_currencies_base_lock
  before update or delete on public.trip_currencies
  for each row execute function public.enforce_base_currency_lock();

-- 2.3 At least one active manager per trip (AFTER trigger so multi-row role swaps in one statement work)
create function public.enforce_last_manager()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- only rows that WERE an active manager can reduce the manager count (see trigger WHEN clause)
  if tg_op = 'UPDATE'
     and new.trip_id = old.trip_id
     and new.role = 'manager'
     and new.status = 'active' then
    return null;
  end if;

  -- parent trip being deleted (ON DELETE CASCADE): parent row is already gone.
  -- The row lock also serializes concurrent demotions in the same trip.
  perform 1 from public.trips t where t.id = old.trip_id for no key update;
  if not found then
    return null;
  end if;

  if not exists (
    select 1 from public.trip_members m
     where m.trip_id = old.trip_id
       and m.role = 'manager'
       and m.status = 'active'
  ) then
    raise exception 'last_manager: trip % must keep at least one active manager', old.trip_id
      using errcode = 'check_violation';
  end if;
  return null;
end;
$$;

create trigger trip_members_last_manager
  after update or delete on public.trip_members
  for each row
  when (old.role = 'manager' and old.status = 'active')
  execute function public.enforce_last_manager();

-- Trigger functions are never called directly
revoke all on function public.set_updated_at()             from public, anon, authenticated;
revoke all on function public.trip_kv_before_update()      from public, anon, authenticated;
revoke all on function public.enforce_currency_limit()     from public, anon, authenticated;
revoke all on function public.enforce_base_currency_lock() from public, anon, authenticated;
revoke all on function public.enforce_last_manager()       from public, anon, authenticated;

-- ===========================================================================
-- 3. Helper functions
-- ===========================================================================

create function public.is_platform_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.platform_admins pa
     where pa.user_id = auth.uid()
  );
$$;

create function public.trip_role(p_trip uuid)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select m.role
    from public.trip_members m
   where m.trip_id = p_trip
     and m.user_id = auth.uid()
     and m.status = 'active';
$$;

create function public.my_trip_ids(p_roles text[] default array['manager', 'editor', 'viewer'])
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select m.trip_id
    from public.trip_members m
   where m.user_id = auth.uid()
     and m.status = 'active'
     and m.role = any (p_roles);
$$;

revoke execute on function public.is_platform_admin()  from public, anon;
revoke execute on function public.trip_role(uuid)      from public, anon;
revoke execute on function public.my_trip_ids(text[])  from public, anon;
grant  execute on function public.is_platform_admin()  to authenticated, service_role;
grant  execute on function public.trip_role(uuid)      to authenticated, service_role;
grant  execute on function public.my_trip_ids(text[])  to authenticated, service_role;

-- ===========================================================================
-- Privileges: nothing for anon/public; authenticated only what policies need;
-- service_role keeps full access (Edge Functions).
-- ===========================================================================

revoke all on table
  public.platform_admins, public.trips, public.trip_join_codes, public.trip_countries,
  public.trip_currencies, public.trip_members, public.manager_invites, public.relink_tokens,
  public.trip_kv
  from public, anon, authenticated;

grant all on table
  public.platform_admins, public.trips, public.trip_join_codes, public.trip_countries,
  public.trip_currencies, public.trip_members, public.manager_invites, public.relink_tokens,
  public.trip_kv
  to service_role;

grant select on public.platform_admins to authenticated;
grant select on public.trips           to authenticated;
-- managers may edit trip settings, never id / created_by / created_at (updated_at is set by trigger)
grant update (name, start_date, end_date, home_currency, default_lang, default_role, status)
  on public.trips to authenticated;
grant select on public.trip_join_codes to authenticated;
grant select, insert, update, delete on public.trip_countries  to authenticated;
grant select, insert, update, delete on public.trip_currencies to authenticated;
grant select on public.trip_members    to authenticated;
grant select, insert, update, delete on public.trip_kv         to authenticated;
-- manager_invites, relink_tokens: no grants (service role only)

-- ===========================================================================
-- 4. Row Level Security
-- ===========================================================================

alter table public.platform_admins enable row level security;
alter table public.trips           enable row level security;
alter table public.trip_join_codes enable row level security;
alter table public.trip_countries  enable row level security;
alter table public.trip_currencies enable row level security;
alter table public.trip_members    enable row level security;
alter table public.manager_invites enable row level security;
alter table public.relink_tokens   enable row level security;
alter table public.trip_kv         enable row level security;

-- platform_admins: own row only
create policy platform_admins_select_own on public.platform_admins
  for select to authenticated
  using (user_id = (select auth.uid()));

-- trips: active members read; active managers update; no insert/delete
create policy trips_select_member on public.trips
  for select to authenticated
  using (id in (select public.my_trip_ids()));

create policy trips_update_manager on public.trips
  for update to authenticated
  using      (id in (select public.my_trip_ids(array['manager'])))
  with check (id in (select public.my_trip_ids(array['manager'])));

-- trip_join_codes: active managers read; no writes
create policy trip_join_codes_select_manager on public.trip_join_codes
  for select to authenticated
  using (trip_id in (select public.my_trip_ids(array['manager'])));

-- trip_countries: members read; managers write
create policy trip_countries_select_member on public.trip_countries
  for select to authenticated
  using (trip_id in (select public.my_trip_ids()));

create policy trip_countries_insert_manager on public.trip_countries
  for insert to authenticated
  with check (trip_id in (select public.my_trip_ids(array['manager'])));

create policy trip_countries_update_manager on public.trip_countries
  for update to authenticated
  using      (trip_id in (select public.my_trip_ids(array['manager'])))
  with check (trip_id in (select public.my_trip_ids(array['manager'])));

create policy trip_countries_delete_manager on public.trip_countries
  for delete to authenticated
  using (trip_id in (select public.my_trip_ids(array['manager'])));

-- trip_currencies: members read; managers write
create policy trip_currencies_select_member on public.trip_currencies
  for select to authenticated
  using (trip_id in (select public.my_trip_ids()));

create policy trip_currencies_insert_manager on public.trip_currencies
  for insert to authenticated
  with check (trip_id in (select public.my_trip_ids(array['manager'])));

create policy trip_currencies_update_manager on public.trip_currencies
  for update to authenticated
  using      (trip_id in (select public.my_trip_ids(array['manager'])))
  with check (trip_id in (select public.my_trip_ids(array['manager'])));

create policy trip_currencies_delete_manager on public.trip_currencies
  for delete to authenticated
  using (trip_id in (select public.my_trip_ids(array['manager'])));

-- trip_members: active members see the whole trip roster; anyone sees own rows; no writes
create policy trip_members_select on public.trip_members
  for select to authenticated
  using (
    trip_id in (select public.my_trip_ids())
    or user_id = (select auth.uid())
  );

-- trip_kv: members read; managers/editors write (trip move blocked by trigger)
create policy trip_kv_select_member on public.trip_kv
  for select to authenticated
  using (trip_id in (select public.my_trip_ids()));

create policy trip_kv_insert_editor on public.trip_kv
  for insert to authenticated
  with check (trip_id in (select public.my_trip_ids(array['manager', 'editor'])));

create policy trip_kv_update_editor on public.trip_kv
  for update to authenticated
  using      (trip_id in (select public.my_trip_ids(array['manager', 'editor'])))
  with check (trip_id in (select public.my_trip_ids(array['manager', 'editor'])));

create policy trip_kv_delete_editor on public.trip_kv
  for delete to authenticated
  using (trip_id in (select public.my_trip_ids(array['manager', 'editor'])));

-- manager_invites, relink_tokens: RLS enabled, no policies (service role only)
