-- Plan_Travel migration 0006_f02 · version 3.0.0-alpha.2 · F02
-- Numbering note (2026-10-05): 0005 is taken by 0005_f02_test_trip (test trip + admin_assign_device, applied 2026-10-04 02:48 UTC); this file is 0006.
-- CHANGE 2026-10-04 F02-DB-01: add public.trip_kv and public.trip_members to the supabase_realtime publication
-- (planned in F01 for F02; the app does not subscribe yet — F05 will). What changed from 0001–0004: publication
-- membership only. No new tables, no new columns, no policy or grant changes (docs/F02_spec.md §7).
--
-- Idempotent: each table is added only if it is not already a member; if the publication does not exist
-- (it always does on Supabase) it is created empty first. Safe to run more than once.
-- Realtime still enforces RLS on postgres_changes for the subscribing user, so membership exposes nothing
-- beyond what F01's select policies already allow.

do $f02$
declare
  t text;
begin
  if not exists (select 1 from pg_catalog.pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;

  foreach t in array array['trip_kv', 'trip_members'] loop
    if not exists (
      select 1 from pg_catalog.pg_publication_tables
      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t
    ) then
      execute pg_catalog.format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end
$f02$;

-- CHANGE 2026-10-04 F02-DB-02: trip_kv.updated_by is now set on EVERY insert and update to the writer's auth.uid().
-- What changed from 0001: before this, updated_by was only a column DEFAULT, so it applied to inserts that omitted it
-- and an upsert of an existing key kept the previous writer (found by F02 QA, SQL case #4); a client could also send
-- any updated_by value. Now the trigger overrides it. Service-role / SQL writes (no JWT, auth.uid() is null, e.g. the
-- data import) keep the value given (normally null). Additive: a separate trigger; trip_kv_before_update is unchanged.
create or replace function public.trip_kv_set_updated_by()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is not null then
    new.updated_by := v_uid;
  end if;
  return new;
end;
$$;

revoke execute on function public.trip_kv_set_updated_by() from public, anon, authenticated;

drop trigger if exists trip_kv_set_updated_by on public.trip_kv;
create trigger trip_kv_set_updated_by
  before insert or update on public.trip_kv
  for each row execute function public.trip_kv_set_updated_by();
