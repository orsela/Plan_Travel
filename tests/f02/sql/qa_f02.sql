-- Plan_Travel QA · F02 DB-level test harness · version 3.0.0-alpha.2 · F02 QA
-- CHANGE 2026-10-04 F02-QA: first version (no previous version). Installs qa.f02_run() in the locked `qa` schema,
--   same pattern as F01 (migrations 0002–0004): helpers impersonate a user via request.jwt.claims + SET LOCAL ROLE,
--   all test data is created inside a sub-transaction that is always rolled back (raise 'f02_rollback'),
--   search_path pinned to '', nothing granted to public/anon/authenticated.
--
-- Written from docs/F02_spec.md only (black box; migration 0005 not read).
-- NOT applied by QA — the architect applies it (as a migration, e.g. 0006_qa_f02_harness) and runs:
--     select * from qa.f02_run() order by id;
-- Output: one row per scenario (id, name, pass, detail) — detail = 'expected: … | actual: …'.
--
-- Criteria covered (spec §8): 2 (pending/removed/outsider read 0 trip_kv rows and resolve no active trip),
-- 3 (viewer write rejected by RLS), 6 (editor writes land with the right trip_id and updated_by = auth.uid(),
-- with the exact write shape §4 prescribes: upsert of (trip_id,key,value) on conflict (trip_id,key), no
-- updated_at/updated_by sent), 11 at DB level (one user in two trips: rows do not mix),
-- 16 (anon key without a session reads 0 rows), §7 (trip_kv + trip_members in supabase_realtime; F01 policies unchanged).
--
-- Note on "security definer": the F01 harness functions are SECURITY INVOKER (run as postgres by the Connector),
-- because PostgreSQL forbids SET ROLE inside a SECURITY DEFINER function ("cannot set parameter role within
-- security-definer function"). This file follows the F01 pattern exactly, so it is invoker too.

create schema if not exists qa;
revoke all on schema qa from public, anon, authenticated;

-- Impersonate: p_role in ('authenticated','anon','postgres'); p_uid null = no "sub" claim (anon key without a session).
create or replace function qa.f02_as(p_uid uuid, p_role text)
returns void language plpgsql set search_path = '' as $f$
begin
  execute 'reset role';
  if p_role = 'postgres' then
    perform pg_catalog.set_config('request.jwt.claims', '', true);
    perform pg_catalog.set_config('request.jwt.claim.sub', '', true);
    perform pg_catalog.set_config('request.jwt.claim.role', '', true);
    return;
  end if;
  perform pg_catalog.set_config('request.jwt.claims',
    case when p_uid is null then pg_catalog.json_build_object('role', p_role)::text
         else pg_catalog.json_build_object('sub', p_uid, 'role', p_role)::text end, true);
  perform pg_catalog.set_config('request.jwt.claim.sub', coalesce(p_uid::text, ''), true);
  perform pg_catalog.set_config('request.jwt.claim.role', p_role, true);
  execute pg_catalog.format('set local role %I', p_role);
end $f$;

-- Count rows returned by p_sql while impersonating.
create or replace function qa.f02_count(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare n bigint;
begin
  perform qa.f02_as(p_uid, p_role);
  execute 'select count(*) from (' || p_sql || ') f02_s' into n;
  execute 'reset role';
  return n || ' rows';
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Run one DML statement while impersonating; report affected rows.
create or replace function qa.f02_dml(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare n bigint;
begin
  perform qa.f02_as(p_uid, p_role);
  execute p_sql;
  get diagnostics n = row_count;
  execute 'reset role';
  return n || ' rows';
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Evaluate a scalar expression while impersonating; report it as text.
create or replace function qa.f02_val(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare v text;
begin
  perform qa.f02_as(p_uid, p_role);
  execute p_sql into v;
  execute 'reset role';
  return coalesce(v, 'NULL');
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Expected-value grammar (same as F01): 'N rows' exact · 'error 42501' prefix · 'error contains xyz' · 'A or B'.
create or replace function qa.f02_match(p_exp text, p_act text)
returns boolean language plpgsql immutable set search_path = '' as $f$
declare alt text;
begin
  if p_act is null then return false; end if;
  if position(' or ' in p_exp) > 0 then
    foreach alt in array pg_catalog.string_to_array(p_exp, ' or ') loop
      if qa.f02_match(alt, p_act) then return true; end if;
    end loop;
    return false;
  end if;
  if p_exp like 'error contains %' then
    return p_act like 'error %' and position(substr(p_exp, 16) in p_act) > 0;
  end if;
  if p_exp like 'error %' then
    return p_act like p_exp || '%';
  end if;
  return p_act = p_exp;
end $f$;

create or replace function qa.f02_rec(r jsonb, p_id int, p_name text, p_exp text, p_act text)
returns jsonb language plpgsql set search_path = '' as $f$
begin
  return r || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
    'id', p_id, 'name', p_name,
    'detail', 'expected: ' || p_exp || ' | actual: ' || coalesce(p_act, '(null)'),
    'pass', qa.f02_match(p_exp, p_act)));
end $f$;

create or replace function qa.f02_run()
returns table(id int, name text, pass boolean, detail text)
language plpgsql set search_path = '' as $$
#variable_conflict use_column
declare
  r   jsonb := '[]'::jsonb;
  a   text;
  au  constant text := 'authenticated';
  -- fixed test identities (namespace f02…)
  u_m   constant uuid := 'f0200000-0000-4000-8000-0000000000a1';  -- manager, trip A
  u_e   constant uuid := 'f0200000-0000-4000-8000-0000000000a2';  -- editor, trip A (and editor in trip B)
  u_v   constant uuid := 'f0200000-0000-4000-8000-0000000000a3';  -- viewer, trip A
  u_p   constant uuid := 'f0200000-0000-4000-8000-0000000000a4';  -- pending, trip A
  u_r   constant uuid := 'f0200000-0000-4000-8000-0000000000a5';  -- removed, trip A
  u_o   constant uuid := 'f0200000-0000-4000-8000-0000000000c1';  -- outsider (anonymous device, never linked)
  u_mb  constant uuid := 'f0200000-0000-4000-8000-0000000000b1';  -- manager, trip B
  t_a   constant uuid := 'f02a0000-0000-4000-8000-00000000000a';
  t_b   constant uuid := 'f02a0000-0000-4000-8000-00000000000b';
  -- the boot query of spec §3 step 3, as the client sends it (own rows, status active)
  q_boot constant text := 'select trip_id, display_name, role from public.trip_members where user_id = auth.uid() and status = ''active''';
begin
  begin
    -- ================= SETUP (as postgres, bypassing RLS) =================
    execute 'reset role';

    insert into auth.users (id, aud, role, email, instance_id, created_at, updated_at) values
      (u_m,  'authenticated', 'authenticated', 'f02-m@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_e,  'authenticated', 'authenticated', 'f02-e@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_v,  'authenticated', 'authenticated', 'f02-v@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_p,  'authenticated', 'authenticated', 'f02-p@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_r,  'authenticated', 'authenticated', 'f02-r@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_o,  'authenticated', 'authenticated', null,                '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_mb, 'authenticated', 'authenticated', 'f02-mb@qa.invalid', '00000000-0000-0000-0000-000000000000', now(), now());

    insert into public.trips (id, name, start_date, end_date, home_currency, created_by) values
      (t_a, 'F02 QA trip A', '2026-12-01', '2026-12-22', 'ILS', u_m),
      (t_b, 'F02 QA trip B', '2027-01-01', '2027-01-10', 'ILS', u_mb);

    insert into public.trip_members (trip_id, user_id, display_name, role, status, approved_at, joined_at) values
      (t_a, u_m,  'm',  'manager', 'active',  now(), now() - interval '3 days'),
      (t_a, u_e,  'e',  'editor',  'active',  now(), now() - interval '2 days'),
      (t_a, u_v,  'v',  'viewer',  'active',  now(), now() - interval '2 days'),
      (t_a, u_p,  'p',  'editor',  'pending', null,  now() - interval '1 day'),
      (t_a, u_r,  'r',  'editor',  'removed', now(), now() - interval '1 day'),
      (t_b, u_mb, 'mb', 'manager', 'active',  now(), now() - interval '5 days'),
      (t_b, u_e,  'e',  'editor',  'active',  now(), now() - interval '1 hour');

    -- seeded as if imported by Claude (§6): written by the manager
    insert into public.trip_kv (trip_id, key, value, updated_by) values
      (t_a, 'trip',         '[{"day":1}]',         u_m),
      (t_a, 'expenses',     '[{"amount":100}]',    u_m),
      (t_a, 'journal:1:entry:e1', '{"id":"e1"}',   u_m),
      (t_b, 'trip',         '[{"day":"B"}]',       u_mb),
      (t_b, 'expenses',     '[{"amount":7}]',      u_mb);

    -- ================= C6 · editor writes land with trip_id + updated_by = auth.uid() =================
    -- exact §4 write shape: upsert (trip_id,key,value) on conflict (trip_id,key); updated_at/updated_by NOT sent
    a := qa.f02_dml(u_e, au, pg_catalog.format(
      'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value',
      t_a, 'reservations', '[{"id":"r1"}]'));
    r := qa.f02_rec(r, 1, 'C6 editor upsert of a NEW key succeeds', '1 rows', a);
    r := qa.f02_rec(r, 2, 'C6 new key: row has trip_id = A and updated_by = editor', 'ok',
      qa.f02_val(null, 'postgres', pg_catalog.format(
        'select case when trip_id = %L and updated_by = %L then ''ok'' else ''trip_id='' || trip_id || '' updated_by='' || coalesce(updated_by::text,''NULL'') end from public.trip_kv where trip_id = %L and key = %L',
        t_a, u_e, t_a, 'reservations')));

    a := qa.f02_dml(u_e, au, pg_catalog.format(
      'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value',
      t_a, 'expenses', '[{"amount":100},{"amount":5}]'));
    r := qa.f02_rec(r, 3, 'C6 editor upsert of an EXISTING key (written by the manager) succeeds', '1 rows', a);
    r := qa.f02_rec(r, 4, 'C6 existing key: updated_by becomes the editor (DB sets it, client does not send it)', 'ok',
      qa.f02_val(null, 'postgres', pg_catalog.format(
        'select case when trip_id = %L and updated_by = %L and value = %L then ''ok'' else ''trip_id='' || trip_id || '' updated_by='' || coalesce(updated_by::text,''NULL'') || '' value='' || value end from public.trip_kv where trip_id = %L and key = %L',
        t_a, u_e, '[{"amount":100},{"amount":5}]', t_a, 'expenses')));

    a := qa.f02_dml(u_m, au, pg_catalog.format(
      'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value',
      t_a, 'budget', '23000'));
    r := qa.f02_rec(r, 5, 'C6 manager upsert succeeds, updated_by = manager', '1 rows / ok',
      a || ' / ' || qa.f02_val(null, 'postgres', pg_catalog.format(
        'select case when updated_by = %L then ''ok'' else coalesce(updated_by::text,''NULL'') end from public.trip_kv where trip_id = %L and key = %L', u_m, t_a, 'budget')));

    r := qa.f02_rec(r, 6, 'C6 editor delete by (trip_id,key) removes the row', '1 rows',
      qa.f02_dml(u_e, au, pg_catalog.format('delete from public.trip_kv where trip_id = %L and key = %L', t_a, 'journal:1:entry:e1')));

    r := qa.f02_rec(r, 7, 'C6 editor list(prefix) = LIKE within trip A returns only A rows', '2 rows',
      qa.f02_count(u_e, au, pg_catalog.format('select key from public.trip_kv where trip_id = %L and key like %L', t_a, 'e%')
        || ' union all ' || pg_catalog.format('select key from public.trip_kv where trip_id = %L and key like %L', t_a, 'r%')));

    -- ================= C3 · viewer write rejected by RLS =================
    r := qa.f02_rec(r, 10, 'C3 viewer upsert of a new key is rejected (RLS)', 'error 42501',
      qa.f02_dml(u_v, au, pg_catalog.format(
        'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value',
        t_a, 'checkins', '[{"id":"ci"}]')));
    r := qa.f02_rec(r, 11, 'C3 viewer upsert of an existing key is rejected (RLS)', 'error 42501',
      qa.f02_dml(u_v, au, pg_catalog.format(
        'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value',
        t_a, 'trip', '[]')));
    r := qa.f02_rec(r, 12, 'C3 viewer delete affects 0 rows', '0 rows or error 42501',
      qa.f02_dml(u_v, au, pg_catalog.format('delete from public.trip_kv where trip_id = %L and key = %L', t_a, 'trip')));
    r := qa.f02_rec(r, 13, 'C3 trip A data unchanged after viewer attempts', 'ok',
      qa.f02_val(null, 'postgres', pg_catalog.format(
        'select case when count(*) = 0 then ''ok'' else count(*)::text || '' bad rows'' end from public.trip_kv where trip_id = %L and ((key = ''trip'' and value <> ''[{"day":1}]'') or key = ''checkins'')', t_a)));
    r := qa.f02_rec(r, 14, 'C3 viewer can still read trip A (4 rows)', '4 rows',
      qa.f02_count(u_v, au, pg_catalog.format('select 1 from public.trip_kv where trip_id = %L', t_a)));
    r := qa.f02_rec(r, 15, 'C3 viewer boot query resolves trip A with role viewer', 'viewer',
      qa.f02_val(u_v, au, 'select string_agg(role, '','') from (' || q_boot || ') b'));

    -- ================= C2 · pending / removed / outsider: 0 rows, no active trip =================
    r := qa.f02_rec(r, 20, 'C2 pending member reads 0 trip_kv rows', '0 rows',
      qa.f02_count(u_p, au, 'select 1 from public.trip_kv'));
    r := qa.f02_rec(r, 21, 'C2 removed member reads 0 trip_kv rows', '0 rows',
      qa.f02_count(u_r, au, 'select 1 from public.trip_kv'));
    r := qa.f02_rec(r, 22, 'C2 outsider (unlinked anonymous device) reads 0 trip_kv rows', '0 rows',
      qa.f02_count(u_o, au, 'select 1 from public.trip_kv'));
    r := qa.f02_rec(r, 23, 'C2 pending member: boot query returns 0 rows (→ not-connected card)', '0 rows',
      qa.f02_count(u_p, au, q_boot));
    r := qa.f02_rec(r, 24, 'C2 removed member: boot query returns 0 rows', '0 rows',
      qa.f02_count(u_r, au, q_boot));
    r := qa.f02_rec(r, 25, 'C2 outsider: boot query returns 0 rows', '0 rows',
      qa.f02_count(u_o, au, q_boot));
    r := qa.f02_rec(r, 26, 'C2 pending member cannot write trip_kv', 'error 42501',
      qa.f02_dml(u_p, au, pg_catalog.format(
        'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value', t_a, 'x', 'y')));
    r := qa.f02_rec(r, 27, 'C2 outsider cannot write trip_kv', 'error 42501',
      qa.f02_dml(u_o, au, pg_catalog.format(
        'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L) on conflict (trip_id, key) do update set value = excluded.value', t_a, 'x', 'y')));
    r := qa.f02_rec(r, 28, 'C2 outsider reads 0 trip_members rows', '0 rows',
      qa.f02_count(u_o, au, 'select 1 from public.trip_members'));

    -- ================= C11 at DB level · one user in two trips =================
    r := qa.f02_rec(r, 30, 'C11 editor in A and B: boot query returns 2 rows (client picks most recent joined_at)', '2 rows',
      qa.f02_count(u_e, au, q_boot));
    r := qa.f02_rec(r, 31, 'C11 most recent joined_at among the editor''s active trips is trip B', 'B',
      qa.f02_val(u_e, au, pg_catalog.format(
        'select case when trip_id = %L then ''B'' else ''A'' end from public.trip_members where user_id = auth.uid() and status = ''active'' order by joined_at desc limit 1', t_b)));
    r := qa.f02_rec(r, 32, 'C11 get(trip) filtered by trip B returns B''s value only', '[{"day":"B"}]',
      qa.f02_val(u_e, au, pg_catalog.format('select string_agg(value, ''|'') from public.trip_kv where trip_id = %L and key = ''trip''', t_b)));
    r := qa.f02_rec(r, 33, 'C11 manager of A sees no trip B rows', '0 rows',
      qa.f02_count(u_m, au, pg_catalog.format('select 1 from public.trip_kv where trip_id = %L', t_b)));

    -- ================= C16 · anon key without a session reads 0 rows =================
    r := qa.f02_rec(r, 40, 'C16 anon (no session) reads trip_kv: 0 rows', '0 rows or error 42501',
      qa.f02_count(null, 'anon', 'select 1 from public.trip_kv'));
    r := qa.f02_rec(r, 41, 'C16 anon (no session) reads trip_members: 0 rows', '0 rows or error 42501',
      qa.f02_count(null, 'anon', 'select 1 from public.trip_members'));
    r := qa.f02_rec(r, 42, 'C16 anon (no session) reads trips: 0 rows', '0 rows or error 42501',
      qa.f02_count(null, 'anon', 'select 1 from public.trips'));
    r := qa.f02_rec(r, 43, 'C16 anon (no session) cannot write trip_kv', 'error 42501',
      qa.f02_dml(null, 'anon', pg_catalog.format(
        'insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L)', t_a, 'anon', 'x')));
    r := qa.f02_rec(r, 44, 'C16 authenticated role without a sub claim reads 0 trip_kv rows', '0 rows or error 42501',
      qa.f02_count(null, au, 'select 1 from public.trip_kv'));

    execute 'reset role';
    raise exception 'f02_rollback';
  exception when others then
    if sqlerrm <> 'f02_rollback' then
      r := r || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
        'id', 0, 'name', 'unexpected error (setup or harness) — later scenarios did not run',
        'detail', 'expected: no error | actual: error ' || sqlstate || ' ' || sqlerrm, 'pass', false));
    end if;
  end;

  execute 'reset role';
  perform pg_catalog.set_config('request.jwt.claims', '', true);
  perform pg_catalog.set_config('request.jwt.claim.sub', '', true);
  perform pg_catalog.set_config('request.jwt.claim.role', '', true);

  -- ================= §7 · catalog checks (read-only, outside the rolled-back block) =================
  r := qa.f02_rec(r, 50, '§7 trip_kv is in publication supabase_realtime', '1',
    (select count(*)::text from pg_catalog.pg_publication_tables
      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'trip_kv'));
  r := qa.f02_rec(r, 51, '§7 trip_members is in publication supabase_realtime', '1',
    (select count(*)::text from pg_catalog.pg_publication_tables
      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'trip_members'));
  r := qa.f02_rec(r, 52, '§7 no policy changes: trip_kv policies are exactly the F01 four', 'trip_kv_delete_editor,trip_kv_insert_editor,trip_kv_select_member,trip_kv_update_editor',
    (select coalesce(string_agg(policyname::text, ',' order by policyname), '') from pg_catalog.pg_policies
      where schemaname = 'public' and tablename = 'trip_kv'));
  r := qa.f02_rec(r, 53, '§7 no policy changes: trip_members policy is exactly the F01 one', 'trip_members_select',
    (select coalesce(string_agg(policyname::text, ',' order by policyname), '') from pg_catalog.pg_policies
      where schemaname = 'public' and tablename = 'trip_members'));
  r := qa.f02_rec(r, 54, '§7 no new tables in public (still the 9 F01 tables)', '9',
    (select count(*)::text from pg_catalog.pg_tables where schemaname = 'public'));
  r := qa.f02_rec(r, 55, 'Leftovers after the run: 0 f02 users, 0 f02 trips', '0/0',
    (select (select count(*) from auth.users where id::text like 'f0200000-%')::text || '/' ||
            (select count(*) from public.trips where id::text like 'f02a0000-%')::text));

  return query
    select (e->>'id')::int, e->>'name', (e->>'pass')::boolean, e->>'detail'
      from pg_catalog.jsonb_array_elements(r) e;
end $$;

revoke all on all functions in schema qa from public, anon, authenticated;
