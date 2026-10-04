-- Plan_Travel migration 0002_qa_f01_harness · version 3.0.0-alpha.1 · F01 QA
-- CHANGE 2026-10-04 F01: installs the F01 RLS test suite (supabase/tests/f01_rls_test.sql) as functions in a
-- locked `qa` schema, so a run is one short call: select * from qa.f01_run() order by id;
-- Each run creates its test data inside a sub-transaction that is always rolled back (no data left behind).
-- The qa schema is not exposed by the API and nobody except postgres may use it.

create schema if not exists qa;
revoke all on schema qa from public, anon, authenticated;

create or replace function qa.f01_as(p_uid uuid, p_role text)
returns void language plpgsql as $f$
begin
  execute 'reset role';
  if p_role = 'postgres' then
    perform set_config('request.jwt.claims', '', true);
    perform set_config('request.jwt.claim.sub', '', true);
    perform set_config('request.jwt.claim.role', '', true);
    return;
  end if;
  perform set_config('request.jwt.claims',
    case when p_uid is null then json_build_object('role', p_role)::text
         else json_build_object('sub', p_uid, 'role', p_role)::text end, true);
  perform set_config('request.jwt.claim.sub', coalesce(p_uid::text, ''), true);
  perform set_config('request.jwt.claim.role', p_role, true);
  execute format('set local role %I', p_role);
end $f$;

-- Count rows returned by p_sql while impersonating.
create or replace function qa.f01_count(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql as $f$
declare n bigint;
begin
  perform qa.f01_as(p_uid, p_role);
  execute 'select count(*) from (' || p_sql || ') f01_s' into n;
  execute 'reset role';
  return n || ' rows';
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Run one DML statement while impersonating; report affected rows.
create or replace function qa.f01_dml(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql as $f$
declare n bigint;
begin
  perform qa.f01_as(p_uid, p_role);
  execute p_sql;
  get diagnostics n = row_count;
  execute 'reset role';
  return n || ' rows';
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Same as f01_dml, but the statement is always rolled back afterwards, so an
-- implementation bug in one scenario cannot corrupt the data of later ones.
create or replace function qa.f01_dml_rb(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql as $f$
declare res text;
begin
  begin
    res := qa.f01_dml(p_uid, p_role, p_sql);
    raise exception 'f01_sub';
  exception when others then
    if sqlerrm <> 'f01_sub' then res := coalesce(res, 'error ' || sqlstate || ' ' || sqlerrm); end if;
  end;
  execute 'reset role';
  return res;
end $f$;

-- Evaluate a scalar expression while impersonating; report it as text.
create or replace function qa.f01_val(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql as $f$
declare v text;
begin
  perform qa.f01_as(p_uid, p_role);
  execute p_sql into v;
  execute 'reset role';
  return coalesce(v, 'NULL');
exception when others then
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

create or replace function qa.f01_match(p_exp text, p_act text)
returns boolean language plpgsql immutable as $f$
declare alt text;
begin
  if p_act is null then return false; end if;
  if position(' or ' in p_exp) > 0 then
    foreach alt in array string_to_array(p_exp, ' or ') loop
      if qa.f01_match(alt, p_act) then return true; end if;
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

create or replace function qa.f01_rec(r jsonb, p_id int, p_scn text, p_exp text, p_act text)
returns jsonb language plpgsql as $f$
begin
  return r || jsonb_build_array(jsonb_build_object(
    'id', p_id, 'scenario', p_scn, 'expected', p_exp,
    'actual', coalesce(p_act, '(null)'),
    'pass', qa.f01_match(p_exp, p_act)));
end $f$;

create or replace function qa.f01_run()
returns table(id int, scenario text, expected text, actual text, pass boolean)
language plpgsql as $$
#variable_conflict use_column
declare
  r   jsonb := '[]'::jsonb;
  a   text; a2 text; a3 text; a4 text; a5 text; a6 text;
  t   text;
  k   int;
  au  constant text := 'authenticated';
  -- fixed test identities (namespace f01…)
  u_mA  constant uuid := 'f0100000-0000-4000-8000-0000000000a1';
  u_eA  constant uuid := 'f0100000-0000-4000-8000-0000000000a2';
  u_vA  constant uuid := 'f0100000-0000-4000-8000-0000000000a3';
  u_pA  constant uuid := 'f0100000-0000-4000-8000-0000000000a4';
  u_mB  constant uuid := 'f0100000-0000-4000-8000-0000000000b1';
  u_out constant uuid := 'f0100000-0000-4000-8000-0000000000c1';
  u_adm constant uuid := 'f0100000-0000-4000-8000-0000000000d1';
  t_A   constant uuid := 'f01a0000-0000-4000-8000-00000000000a';
  t_B   constant uuid := 'f01a0000-0000-4000-8000-00000000000b';
  m_mA  constant uuid := 'f01b0000-0000-4000-8000-0000000000a1';
  m_eA  constant uuid := 'f01b0000-0000-4000-8000-0000000000a2';
  m_vA  constant uuid := 'f01b0000-0000-4000-8000-0000000000a3';
  m_pA  constant uuid := 'f01b0000-0000-4000-8000-0000000000a4';
  m_mB  constant uuid := 'f01b0000-0000-4000-8000-0000000000b1';
  inv_A constant uuid := 'f01c0000-0000-4000-8000-0000000000a1';
  rl_vA constant uuid := 'f01d0000-0000-4000-8000-0000000000a3';
  tables constant text[] := array['platform_admins','trips','trip_join_codes','trip_countries',
                                  'trip_currencies','trip_members','manager_invites','relink_tokens','trip_kv'];
begin
  begin
    -- ================= SETUP (as postgres, bypassing RLS) =================
    execute 'reset role';

    insert into auth.users (id, aud, role, email, instance_id, created_at, updated_at) values
      (u_mA,  'authenticated', 'authenticated', 'f01-ma@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_eA,  'authenticated', 'authenticated', 'f01-ea@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_vA,  'authenticated', 'authenticated', 'f01-va@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_pA,  'authenticated', 'authenticated', 'f01-pa@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_mB,  'authenticated', 'authenticated', 'f01-mb@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_out, 'authenticated', 'authenticated', 'f01-out@qa.invalid', '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_adm, 'authenticated', 'authenticated', 'f01-adm@qa.invalid', '00000000-0000-0000-0000-000000000000', now(), now());

    insert into public.platform_admins (user_id) values (u_adm);

    insert into public.trips (id, name, start_date, end_date, home_currency, created_by) values
      (t_A, 'F01 QA trip A', '2026-12-01', '2026-12-22', 'ILS', u_mA),
      (t_B, 'F01 QA trip B', '2027-01-01', '2027-01-10', 'ILS', u_mB);

    insert into public.trip_join_codes (trip_id, code) values
      (t_A, 'ZZQA2345'), (t_B, 'ZZQB6789');

    insert into public.trip_countries (trip_id, country_code, sort) values
      (t_A, 'VN', 0), (t_A, 'KH', 1), (t_B, 'TH', 0);

    insert into public.trip_currencies (trip_id, currency_code, kind, sort) values
      (t_A, 'ILS', 'base', 0), (t_A, 'USD', 'base', 1), (t_A, 'EUR', 'base', 2), (t_A, 'VND', 'destination', 3),
      (t_B, 'ILS', 'base', 0), (t_B, 'USD', 'base', 1), (t_B, 'EUR', 'base', 2), (t_B, 'THB', 'destination', 3);

    insert into public.trip_members (id, trip_id, user_id, display_name, role, status, approved_at) values
      (m_mA, t_A, u_mA, 'mA', 'manager', 'active',  now()),
      (m_eA, t_A, u_eA, 'eA', 'editor',  'active',  now()),
      (m_vA, t_A, u_vA, 'vA', 'viewer',  'active',  now()),
      (m_pA, t_A, u_pA, 'pA', 'editor',  'pending', null),
      (m_mB, t_B, u_mB, 'mB', 'manager', 'active',  now());

    insert into public.trip_kv (trip_id, key, value) values
      (t_A, 'itinerary', 'A-it'), (t_A, 'budget', 'A-bu'), (t_A, 'notes', 'A-no'),
      (t_B, 'itinerary', 'B-it'), (t_B, 'budget', 'B-bu');

    insert into public.manager_invites (id, email, token_hash, draft_name, invited_by, expires_at, trip_id) values
      (inv_A, 'f01-invite@qa.invalid', 'f01-qa-invite-hash', 'draft', u_adm, now() + interval '1 day', t_A);

    insert into public.relink_tokens (id, member_id, token_hash, expires_at, created_by) values
      (rl_vA, m_vA, 'f01-qa-relink-hash', now() + interval '1 day', u_mA);

    -- Backdate updated_at with triggers disabled so the auto-update can be observed.
    -- (If the role may not set session_replication_role, this is skipped and
    --  ids 9/56 can only detect a missing trigger when it uses clock time.)
    begin
      perform set_config('session_replication_role', 'replica', true);
      update public.trips   set updated_at = '2000-01-01' where id in (t_A, t_B);
      update public.trip_kv set updated_at = '2000-01-01' where trip_id in (t_A, t_B);
      perform set_config('session_replication_role', 'origin', true);
    exception when others then
      null;
    end;

    -- ================= §1 structure =================
    r := qa.f01_rec(r, 1, '§1 RLS enabled on all 9 F01 tables', '9 tables',
      (select count(*) || ' tables' from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relname = any(tables) and c.relrowsecurity));
    r := qa.f01_rec(r, 2, '§1/C8 anon holds no table privilege on any F01 table', '0 tables',
      (select count(*) || ' tables' from unnest(tables) tn
        where has_table_privilege('anon', 'public.' || tn,
              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')));

    -- ================= C1 eA reads trip_kv =================
    r := qa.f01_rec(r, 3, 'C1 eA reads trip_kv of A', '3 rows',
      qa.f01_count(u_eA, au, format('select 1 from public.trip_kv where trip_id = %L', t_A)));
    r := qa.f01_rec(r, 4, 'C1 eA reads trip_kv of B', '0 rows',
      qa.f01_count(u_eA, au, format('select 1 from public.trip_kv where trip_id = %L', t_B)));
    r := qa.f01_rec(r, 5, 'C1 eA reads all trip_kv (A only)', '3 rows',
      qa.f01_count(u_eA, au, 'select 1 from public.trip_kv'));

    -- ================= C2 eA writes trip A kv =================
    r := qa.f01_rec(r, 6, 'C2 eA inserts kv in A', '1 rows',
      qa.f01_dml(u_eA, au, format('insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L)', t_A, 'f01_new', 'x')));
    r := qa.f01_rec(r, 7, 'C2/§1 inserted kv.updated_by defaults to auth.uid() (eA)', 'true',
      qa.f01_val(null, 'postgres', format('select (updated_by = %L::uuid)::text from public.trip_kv where trip_id = %L and key = %L', u_eA, t_A, 'f01_new')));
    r := qa.f01_rec(r, 8, 'C2 eA updates kv in A', '1 rows',
      qa.f01_dml(u_eA, au, format('update public.trip_kv set value = %L where trip_id = %L and key = %L', 'A-it2', t_A, 'itinerary')));
    r := qa.f01_rec(r, 9, 'C2/§2.4 trip_kv.updated_at auto-updated on UPDATE', 'true',
      qa.f01_val(null, 'postgres', format('select (updated_at > %L::timestamptz)::text from public.trip_kv where trip_id = %L and key = %L', '2000-01-02', t_A, 'itinerary')));
    r := qa.f01_rec(r, 10, 'C2 eA deletes kv in A', '1 rows',
      qa.f01_dml(u_eA, au, format('delete from public.trip_kv where trip_id = %L and key = %L', t_A, 'f01_new')));

    -- ================= C3 eA inserts into B =================
    r := qa.f01_rec(r, 11, 'C3 eA inserts kv for trip B', 'error 42501',
      qa.f01_dml_rb(u_eA, au, format('insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L)', t_B, 'f01_evil', 'x')));
    r := qa.f01_rec(r, 12, 'C3/§4 eA moves an A kv row to trip B (update trip_id)', 'error 42501 or 0 rows',
      qa.f01_dml_rb(u_eA, au, format('update public.trip_kv set trip_id = %L where trip_id = %L and key = %L', t_B, t_A, 'notes')));

    -- ================= C4 eA updates/deletes B =================
    r := qa.f01_rec(r, 13, 'C4 eA updates kv of B', '0 rows',
      qa.f01_dml(u_eA, au, format('update public.trip_kv set value = %L where trip_id = %L', 'hacked', t_B)));
    r := qa.f01_rec(r, 14, 'C4 eA deletes kv of B', '0 rows',
      qa.f01_dml(u_eA, au, format('delete from public.trip_kv where trip_id = %L', t_B)));
    r := qa.f01_rec(r, 15, 'C3/C4 trip B kv intact and unchanged (as postgres)', '2 rows',
      qa.f01_count(null, 'postgres', format('select 1 from public.trip_kv where trip_id = %L and value like %L', t_B, 'B-%')));

    -- ================= C5 viewer =================
    r := qa.f01_rec(r, 16, 'C5 vA reads kv of A', '3 rows',
      qa.f01_count(u_vA, au, format('select 1 from public.trip_kv where trip_id = %L', t_A)));
    r := qa.f01_rec(r, 17, 'C5 vA inserts kv in A', 'error 42501',
      qa.f01_dml_rb(u_vA, au, format('insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L)', t_A, 'f01_v', 'x')));
    r := qa.f01_rec(r, 18, 'C5 vA updates kv in A', '0 rows or error 42501',
      qa.f01_dml_rb(u_vA, au, format('update public.trip_kv set value = %L where trip_id = %L', 'v', t_A)));
    r := qa.f01_rec(r, 19, 'C5/§4 vA deletes kv in A', '0 rows or error 42501',
      qa.f01_dml_rb(u_vA, au, format('delete from public.trip_kv where trip_id = %L', t_A)));

    -- ================= C6 pending member =================
    r := qa.f01_rec(r, 20, 'C6 pA reads trips', '0 rows',
      qa.f01_count(u_pA, au, 'select 1 from public.trips'));
    r := qa.f01_rec(r, 21, 'C6 pA reads trip_kv', '0 rows',
      qa.f01_count(u_pA, au, 'select 1 from public.trip_kv'));
    r := qa.f01_rec(r, 22, 'C6 pA reads trip_countries', '0 rows',
      qa.f01_count(u_pA, au, 'select 1 from public.trip_countries'));
    r := qa.f01_rec(r, 23, 'C6 pA reads trip_currencies', '0 rows',
      qa.f01_count(u_pA, au, 'select 1 from public.trip_currencies'));
    r := qa.f01_rec(r, 24, 'C6 pA reads other members of A', '0 rows',
      qa.f01_count(u_pA, au, format('select 1 from public.trip_members where user_id <> %L', u_pA)));
    r := qa.f01_rec(r, 25, 'C6 pA sees own (pending) trip_members row', '1 rows',
      qa.f01_count(u_pA, au, format('select 1 from public.trip_members where user_id = %L and status = %L', u_pA, 'pending')));
    r := qa.f01_rec(r, 26, 'C6 pA reads trip_join_codes', '0 rows or error 42501',
      qa.f01_count(u_pA, au, 'select 1 from public.trip_join_codes'));

    -- ================= C7 outsider: every table =================
    k := 27;
    foreach t in array tables loop
      r := qa.f01_rec(r, k, 'C7 out reads ' || t, '0 rows or error 42501',
        qa.f01_count(u_out, au, format('select 1 from public.%I', t)));
      k := k + 1;
    end loop;  -- ids 27..35

    -- ================= C8 anon: every table =================
    k := 36;
    foreach t in array tables loop
      r := qa.f01_rec(r, k, 'C8 anon reads ' || t, 'error 42501 or 0 rows',
        qa.f01_count(null, 'anon', format('select 1 from public.%I', t)));
      k := k + 1;
    end loop;  -- ids 36..44
    r := qa.f01_rec(r, 45, 'C8 anon inserts trip_kv', 'error 42501',
      qa.f01_dml_rb(null, 'anon', format('insert into public.trip_kv (trip_id, key, value) values (%L, %L, %L)', t_A, 'f01_anon', 'x')));
    r := qa.f01_rec(r, 46, 'C8 anon inserts trips', 'error 42501',
      qa.f01_dml_rb(null, 'anon', format('insert into public.trips (name) values (%L)', 'f01 anon trip')));
    r := qa.f01_rec(r, 47, 'C8 anon inserts trip_members', 'error 42501',
      qa.f01_dml_rb(null, 'anon', format('insert into public.trip_members (trip_id, user_id, display_name, role, status) values (%L, %L, %L, %L, %L)', t_A, u_out, 'x', 'manager', 'active')));
    r := qa.f01_rec(r, 48, 'C8 anon inserts trip_currencies', 'error 42501',
      qa.f01_dml_rb(null, 'anon', format('insert into public.trip_currencies (trip_id, currency_code, kind) values (%L, %L, %L)', t_A, 'JPY', 'added')));

    -- ================= C9 join codes =================
    r := qa.f01_rec(r, 49, 'C9 mA reads all trip_join_codes', '1 rows',
      qa.f01_count(u_mA, au, 'select 1 from public.trip_join_codes'));
    r := qa.f01_rec(r, 50, 'C9 mA sees A''s code', '1 rows',
      qa.f01_count(u_mA, au, format('select 1 from public.trip_join_codes where trip_id = %L and code = %L', t_A, 'ZZQA2345')));
    r := qa.f01_rec(r, 51, 'C9 eA reads trip_join_codes', '0 rows or error 42501',
      qa.f01_count(u_eA, au, 'select 1 from public.trip_join_codes'));
    r := qa.f01_rec(r, 52, 'C9 vA reads trip_join_codes', '0 rows or error 42501',
      qa.f01_count(u_vA, au, 'select 1 from public.trip_join_codes'));
    r := qa.f01_rec(r, 53, 'C9 mB reads A''s code', '0 rows',
      qa.f01_count(u_mB, au, format('select 1 from public.trip_join_codes where trip_id = %L', t_A)));
    r := qa.f01_rec(r, 54, 'C9 mB reads own B code (sanity)', '1 rows',
      qa.f01_count(u_mB, au, format('select 1 from public.trip_join_codes where trip_id = %L', t_B)));

    -- ================= C10 trips =================
    r := qa.f01_rec(r, 55, 'C10 mA updates A name', '1 rows',
      qa.f01_dml(u_mA, au, format('update public.trips set name = %L where id = %L', 'F01 QA trip A renamed', t_A)));
    r := qa.f01_rec(r, 56, 'C10/§2.4 trips.updated_at auto-updated on UPDATE', 'true',
      qa.f01_val(null, 'postgres', format('select (updated_at > %L::timestamptz)::text from public.trips where id = %L', '2000-01-02', t_A)));
    r := qa.f01_rec(r, 57, 'C10 eA updates A', '0 rows or error 42501',
      qa.f01_dml(u_eA, au, format('update public.trips set name = %L where id = %L', 'eA was here', t_A)));
    r := qa.f01_rec(r, 58, 'C10 mA updates B', '0 rows',
      qa.f01_dml(u_mA, au, format('update public.trips set name = %L where id = %L', 'mA was here', t_B)));
    r := qa.f01_rec(r, 59, 'C10 mA inserts a trip', 'error 42501',
      qa.f01_dml_rb(u_mA, au, format('insert into public.trips (name, created_by) values (%L, %L)', 'F01 QA trip C', u_mA)));
    r := qa.f01_rec(r, 60, 'C10 mA deletes A', '0 rows or error 42501',
      qa.f01_dml(u_mA, au, format('delete from public.trips where id = %L', t_A)));
    r := qa.f01_rec(r, 61, 'C10 trip names after attempts (as postgres)', 'F01 QA trip A renamed|F01 QA trip B',
      qa.f01_val(null, 'postgres', format('select string_agg(name, %L order by name) from public.trips where id in (%L, %L)', '|', t_A, t_B)));

    -- ================= C11 trip_members =================
    r := qa.f01_rec(r, 62, 'C11 mA inserts trip_members', 'error 42501',
      qa.f01_dml_rb(u_mA, au, format('insert into public.trip_members (trip_id, user_id, display_name, role, status) values (%L, %L, %L, %L, %L)', t_A, u_out, 'out', 'editor', 'active')));
    r := qa.f01_rec(r, 63, 'C11 mA updates trip_members (approve pA)', '0 rows or error 42501',
      qa.f01_dml_rb(u_mA, au, format('update public.trip_members set status = %L where id = %L', 'active', m_pA)));
    r := qa.f01_rec(r, 64, 'C11 mA deletes trip_members (vA)', '0 rows or error 42501',
      qa.f01_dml_rb(u_mA, au, format('delete from public.trip_members where id = %L', m_vA)));
    r := qa.f01_rec(r, 65, 'C11 eA sees all A members (incl. pending)', '4 rows',
      qa.f01_count(u_eA, au, format('select 1 from public.trip_members where trip_id = %L', t_A)));
    r := qa.f01_rec(r, 66, 'C11 eA sees B members', '0 rows',
      qa.f01_count(u_eA, au, format('select 1 from public.trip_members where trip_id = %L', t_B)));

    -- ================= C12 currencies / countries =================
    r := qa.f01_rec(r, 67, 'C12 mA adds ''added'' currency THB to A', '1 rows',
      qa.f01_dml(u_mA, au, format('insert into public.trip_currencies (trip_id, currency_code, kind, sort) values (%L, %L, %L, 4)', t_A, 'THB', 'added')));
    r := qa.f01_rec(r, 68, 'C12 eA adds currency JPY to A', 'error 42501',
      qa.f01_dml_rb(u_eA, au, format('insert into public.trip_currencies (trip_id, currency_code, kind, sort) values (%L, %L, %L, 5)', t_A, 'JPY', 'added')));
    r := qa.f01_rec(r, 69, 'C12/§4 mA adds currency to trip B', 'error 42501',
      qa.f01_dml_rb(u_mA, au, format('insert into public.trip_currencies (trip_id, currency_code, kind) values (%L, %L, %L)', t_B, 'JPY', 'added')));
    r := qa.f01_rec(r, 70, 'C12/§4 mA adds country LA to A', '1 rows',
      qa.f01_dml(u_mA, au, format('insert into public.trip_countries (trip_id, country_code, sort) values (%L, %L, 2)', t_A, 'LA')));
    r := qa.f01_rec(r, 71, 'C12/§4 eA adds country TH to A', 'error 42501',
      qa.f01_dml_rb(u_eA, au, format('insert into public.trip_countries (trip_id, country_code) values (%L, %L)', t_A, 'TH')));

    -- ================= C13 currency limit (rolled back) =================
    a := null; a2 := null;
    begin
      a  := qa.f01_dml(null, 'postgres', format(
              'insert into public.trip_currencies (trip_id, currency_code, kind, sort) values '
              '(%1$L,''JPY'',''added'',4),(%1$L,''GBP'',''added'',5),(%1$L,''CHF'',''added'',6),'
              '(%1$L,''AUD'',''added'',7),(%1$L,''CAD'',''added'',8),(%1$L,''SGD'',''added'',9)', t_B));
      a2 := qa.f01_dml(null, 'postgres', format(
              'insert into public.trip_currencies (trip_id, currency_code, kind, sort) values (%L, %L, %L, 10)', t_B, 'NZD', 'added'));
      raise exception 'f01_sub';
    exception when others then
      if sqlerrm <> 'f01_sub' then a2 := coalesce(a2, 'error ' || sqlstate || ' ' || sqlerrm); end if;
    end;
    r := qa.f01_rec(r, 72, 'C13 fill trip B to exactly 10 currencies (postgres)', '6 rows', a);
    r := qa.f01_rec(r, 73, 'C13 11th currency for trip B (postgres)', 'error contains currency_limit', a2);

    -- ================= C14 base currency lock =================
    r := qa.f01_rec(r, 74, 'C14 delete base ILS of A (postgres)', 'error contains base_currency_locked',
      qa.f01_dml_rb(null, 'postgres', format('delete from public.trip_currencies where trip_id = %L and currency_code = %L', t_A, 'ILS')));
    r := qa.f01_rec(r, 75, 'C14/§2.2 change base USD of A to ''added'' (postgres)', 'error contains base_currency_locked',
      qa.f01_dml_rb(null, 'postgres', format('update public.trip_currencies set kind = %L where trip_id = %L and currency_code = %L', 'added', t_A, 'USD')));
    a := null;
    begin
      a := qa.f01_dml(null, 'postgres', format('delete from public.trip_currencies where trip_id = %L and currency_code = %L', t_A, 'VND'));
      raise exception 'f01_sub';
    exception when others then
      if sqlerrm <> 'f01_sub' then a := coalesce(a, 'error ' || sqlstate || ' ' || sqlerrm); end if;
    end;
    r := qa.f01_rec(r, 76, 'C14 delete non-base VND of A is allowed (postgres, rolled back)', '1 rows', a);

    -- ================= C15 last manager (rolled back) =================
    r := qa.f01_rec(r, 77, 'C15 demote only manager mA to editor (postgres)', 'error contains last_manager',
      qa.f01_dml_rb(null, 'postgres', format('update public.trip_members set role = %L where id = %L', 'editor', m_mA)));
    r := qa.f01_rec(r, 78, 'C15 set only manager mA to removed (postgres)', 'error contains last_manager',
      qa.f01_dml_rb(null, 'postgres', format('update public.trip_members set status = %L where id = %L', 'removed', m_mA)));
    r := qa.f01_rec(r, 79, 'C15 delete only manager mA (postgres)', 'error contains last_manager',
      qa.f01_dml_rb(null, 'postgres', format('delete from public.trip_members where id = %L', m_mA)));
    a := null; a2 := null; a3 := null;
    begin
      a  := qa.f01_dml(null, 'postgres', format('update public.trip_members set role = %L where id = %L', 'manager', m_eA));
      a2 := qa.f01_dml(null, 'postgres', format('update public.trip_members set role = %L where id = %L', 'editor', m_mA));
      a3 := qa.f01_dml(null, 'postgres', format('delete from public.trip_members where id = %L', m_mA));
      raise exception 'f01_sub';
    exception when others then
      if sqlerrm <> 'f01_sub' then a3 := coalesce(a3, 'error ' || sqlstate || ' ' || sqlerrm); end if;
    end;
    r := qa.f01_rec(r, 80, 'C15 promote eA to second active manager (postgres)', '1 rows', a);
    r := qa.f01_rec(r, 81, 'C15 then demote mA succeeds (postgres)', '1 rows', a2);
    r := qa.f01_rec(r, 82, 'C15 then delete mA succeeds (postgres)', '1 rows', a3);

    -- ================= C16 cascade delete of trip A (rolled back) =================
    a := null; a2 := null; a3 := null; a4 := null;
    begin
      a  := qa.f01_dml(null, 'postgres', format('delete from public.trips where id = %L', t_A));
      a2 := qa.f01_count(null, 'postgres', format(
              'select 1 from public.trip_kv where trip_id = %1$L union all '
              'select 1 from public.trip_countries where trip_id = %1$L union all '
              'select 1 from public.trip_currencies where trip_id = %1$L union all '
              'select 1 from public.trip_members where trip_id = %1$L union all '
              'select 1 from public.trip_join_codes where trip_id = %1$L', t_A));
      a3 := qa.f01_count(null, 'postgres', format('select 1 from public.relink_tokens where id = %L', rl_vA));
      a4 := qa.f01_count(null, 'postgres', format('select 1 from public.manager_invites where id = %L and trip_id is null', inv_A));
      raise exception 'f01_sub';
    exception when others then
      if sqlerrm <> 'f01_sub' then a := coalesce(a, 'error ' || sqlstate || ' ' || sqlerrm); end if;
    end;
    r := qa.f01_rec(r, 83, 'C16 delete trip A (postgres) — no trigger error', '1 rows', a);
    r := qa.f01_rec(r, 84, 'C16 A child rows left (kv/countries/currencies/members/join code)', '0 rows', a2);
    r := qa.f01_rec(r, 85, 'C16 relink token of A member cascaded', '0 rows', a3);
    r := qa.f01_rec(r, 86, 'C16/§1 manager_invite of A kept with trip_id NULL', '1 rows', a4);

    -- ================= C17 platform admin =================
    r := qa.f01_rec(r, 87, 'C17 is_platform_admin() as adm', 'true',
      qa.f01_val(u_adm, au, 'select public.is_platform_admin()::text'));
    r := qa.f01_rec(r, 88, 'C17 is_platform_admin() as mA', 'false',
      qa.f01_val(u_mA, au, 'select public.is_platform_admin()::text'));
    r := qa.f01_rec(r, 89, 'C17 adm reads trip_kv of A', '0 rows',
      qa.f01_count(u_adm, au, format('select 1 from public.trip_kv where trip_id = %L', t_A)));
    r := qa.f01_rec(r, 90, 'C17 adm reads trips', '0 rows',
      qa.f01_count(u_adm, au, 'select 1 from public.trips'));

    -- ================= C18 trip_role / my_trip_ids =================
    r := qa.f01_rec(r, 91, 'C18 trip_role(A) as mA', 'manager',
      qa.f01_val(u_mA, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 92, 'C18 trip_role(A) as eA', 'editor',
      qa.f01_val(u_eA, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 93, 'C18 trip_role(A) as vA', 'viewer',
      qa.f01_val(u_vA, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 94, 'C18 trip_role(A) as pA', 'NULL',
      qa.f01_val(u_pA, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 95, 'C18 trip_role(A) as out', 'NULL',
      qa.f01_val(u_out, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 96, 'C18 trip_role(A) as mB', 'NULL',
      qa.f01_val(u_mB, au, format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 97, 'C18 trip_role(B) as mB (sanity)', 'manager',
      qa.f01_val(u_mB, au, format('select public.trip_role(%L::uuid)', t_B)));
    r := qa.f01_rec(r, 98, 'C18/§3 my_trip_ids() as eA = {A}', t_A::text,
      qa.f01_val(u_eA, au, 'select string_agg(x::text, '','') from public.my_trip_ids() x'));
    r := qa.f01_rec(r, 99, 'C18/§3 my_trip_ids(''{manager}'') as eA is empty', 'NULL',
      qa.f01_val(u_eA, au, 'select string_agg(x::text, '','') from public.my_trip_ids(array[''manager'']) x'));
    r := qa.f01_rec(r, 100, 'C18/§3 my_trip_ids() as pA is empty', 'NULL',
      qa.f01_val(u_pA, au, 'select string_agg(x::text, '','') from public.my_trip_ids() x'));

    -- ================= C19 anon cannot execute helpers =================
    r := qa.f01_rec(r, 101, 'C19 anon executes is_platform_admin()', 'error 42501',
      qa.f01_val(null, 'anon', 'select public.is_platform_admin()::text'));
    r := qa.f01_rec(r, 102, 'C19 anon executes trip_role()', 'error 42501',
      qa.f01_val(null, 'anon', format('select public.trip_role(%L::uuid)', t_A)));
    r := qa.f01_rec(r, 103, 'C19 anon executes my_trip_ids()', 'error 42501',
      qa.f01_val(null, 'anon', 'select count(*)::text from public.my_trip_ids()'));
    r := qa.f01_rec(r, 104, '§3 helpers: security definer + stable + search_path='''' + authenticated can execute', '3 functions',
      (select count(*) || ' functions' from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname in ('is_platform_admin','trip_role','my_trip_ids')
          and p.prosecdef and p.provolatile = 's'
          and ('search_path=""' = any(p.proconfig) or 'search_path=' = any(p.proconfig))
          and has_function_privilege('authenticated', p.oid, 'EXECUTE')
          and not has_function_privilege('anon', p.oid, 'EXECUTE')));

    -- ================= C20 platform_admins =================
    r := qa.f01_rec(r, 105, 'C20 adm reads platform_admins (own row)', '1 rows',
      qa.f01_count(u_adm, au, format('select 1 from public.platform_admins where user_id = %L', u_adm)));
    r := qa.f01_rec(r, 106, 'C20 mA reads platform_admins', '0 rows',
      qa.f01_count(u_mA, au, 'select 1 from public.platform_admins'));

    -- ================= §4 no-access tables, read by an insider =================
    r := qa.f01_rec(r, 107, '§4 mA reads manager_invites', '0 rows or error 42501',
      qa.f01_count(u_mA, au, 'select 1 from public.manager_invites'));
    r := qa.f01_rec(r, 108, '§4 mA reads relink_tokens', '0 rows or error 42501',
      qa.f01_count(u_mA, au, 'select 1 from public.relink_tokens'));
    r := qa.f01_rec(r, 109, '§4 adm reads manager_invites (admin has no policy)', '0 rows or error 42501',
      qa.f01_count(u_adm, au, 'select 1 from public.manager_invites'));

    execute 'reset role';
    raise exception 'f01_rollback';
  exception when others then
    if sqlerrm <> 'f01_rollback' then
      r := r || jsonb_build_array(jsonb_build_object(
        'id', 0, 'scenario', 'unexpected error (setup or harness) — later scenarios did not run',
        'expected', 'no error', 'actual', 'error ' || sqlstate || ' ' || sqlerrm, 'pass', false));
    end if;
  end;

  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
  perform set_config('request.jwt.claim.sub', '', true);
  perform set_config('request.jwt.claim.role', '', true);

  return query
    select (e->>'id')::int, e->>'scenario', e->>'expected', e->>'actual', (e->>'pass')::boolean
      from jsonb_array_elements(r) e;

end $$;


revoke all on all functions in schema qa from public, anon, authenticated;
