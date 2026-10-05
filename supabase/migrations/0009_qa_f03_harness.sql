-- Plan_Travel QA · F03 DB-level test harness · version 3.0.0-alpha.3 · F03
-- CHANGE 2026-10-05 F03-QA-01: first version (no previous version). Installs qa.f03_run() in the locked `qa` schema,
--   same pattern as F01/F02 (0002–0004, 0007): helpers impersonate a user via request.jwt.claims + SET LOCAL ROLE,
--   all test data is created inside a sub-transaction that is always rolled back (raise 'f03_rollback'),
--   search_path pinned to '', nothing granted to public/anon/authenticated. SECURITY INVOKER (run as postgres),
--   because PostgreSQL forbids SET ROLE inside a SECURITY DEFINER function.
--
-- Run (after 0008_f03.sql):   select * from qa.f03_run() order by id;
-- Output: one row per scenario (id, name, pass, detail) — detail = 'expected: … | actual: …'.
--
-- Criteria covered (docs/F03_spec.md §5): 1 (admin_trip_summary refused for non-admins of every trip role and for
-- anon; manager_invites not readable/writable by clients), 3 (summary never contains trip_kv data, tokens/hashes or
-- non-manager member names), 5 at DB level (one active invite per email: second insert refused; revoked/used
-- invites free the slot), 13 (derived statuses pending / expired / revoked / active / ended-by-date /
-- ended-by-archived, counts), plus §2 catalog checks (security definer, search_path, grants, no new tables).

create schema if not exists qa;
revoke all on schema qa from public, anon, authenticated;

-- Impersonate: p_role in ('authenticated','anon','postgres'); p_uid null = no "sub" claim (anon key without a session).
create or replace function qa.f03_as(p_uid uuid, p_role text)
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
create or replace function qa.f03_count(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare n bigint;
begin
  perform qa.f03_as(p_uid, p_role);
  execute 'select count(*) from (' || p_sql || ') f03_s' into n;
  execute 'reset role';
  return n || ' rows';
exception when others then
  execute 'reset role';
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Run one DML statement while impersonating; report affected rows.
create or replace function qa.f03_dml(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare n bigint;
begin
  perform qa.f03_as(p_uid, p_role);
  execute p_sql;
  get diagnostics n = row_count;
  execute 'reset role';
  return n || ' rows';
exception when others then
  execute 'reset role';
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Evaluate a scalar expression while impersonating; report it as text.
create or replace function qa.f03_val(p_uid uuid, p_role text, p_sql text)
returns text language plpgsql set search_path = '' as $f$
declare v text;
begin
  perform qa.f03_as(p_uid, p_role);
  execute p_sql into v;
  execute 'reset role';
  return coalesce(v, 'NULL');
exception when others then
  execute 'reset role';
  return 'error ' || sqlstate || ' ' || left(sqlerrm, 160);
end $f$;

-- Expected-value grammar (same as F01/F02): 'N rows' exact · 'error 42501' prefix · 'error contains xyz' · 'A or B'.
create or replace function qa.f03_match(p_exp text, p_act text)
returns boolean language plpgsql immutable set search_path = '' as $f$
declare alt text;
begin
  if p_act is null then return false; end if;
  if position(' or ' in p_exp) > 0 then
    foreach alt in array pg_catalog.string_to_array(p_exp, ' or ') loop
      if qa.f03_match(alt, p_act) then return true; end if;
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

create or replace function qa.f03_rec(r jsonb, p_id int, p_name text, p_exp text, p_act text)
returns jsonb language plpgsql set search_path = '' as $f$
begin
  return r || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
    'id', p_id, 'name', p_name,
    'detail', 'expected: ' || p_exp || ' | actual: ' || coalesce(p_act, '(null)'),
    'pass', qa.f03_match(p_exp, p_act)));
end $f$;

create or replace function qa.f03_run()
returns table(id int, name text, pass boolean, detail text)
language plpgsql set search_path = '' as $$
#variable_conflict use_column
declare
  r   jsonb := '[]'::jsonb;
  au  constant text := 'authenticated';
  -- fixed test identities (namespace f03…)
  u_adm constant uuid := 'f0300000-0000-4000-8000-0000000000ad';  -- platform admin (also a viewer in trip A)
  u_m   constant uuid := 'f0300000-0000-4000-8000-0000000000a1';  -- manager, trip A
  u_m2  constant uuid := 'f0300000-0000-4000-8000-0000000000a2';  -- second manager, trip A
  u_e   constant uuid := 'f0300000-0000-4000-8000-0000000000a3';  -- editor, trip A (secret name)
  u_v   constant uuid := 'f0300000-0000-4000-8000-0000000000a4';  -- viewer, trip A (secret name)
  u_p   constant uuid := 'f0300000-0000-4000-8000-0000000000a5';  -- pending, trip A
  u_rm  constant uuid := 'f0300000-0000-4000-8000-0000000000a6';  -- removed manager, trip A
  u_mb  constant uuid := 'f0300000-0000-4000-8000-0000000000b1';  -- manager, trip B (ended by date)
  u_mc  constant uuid := 'f0300000-0000-4000-8000-0000000000c1';  -- manager, trip C (archived)
  u_o   constant uuid := 'f0300000-0000-4000-8000-0000000000d1';  -- outsider (anonymous device, no trip)
  t_a   constant uuid := 'f03a0000-0000-4000-8000-00000000000a';
  t_b   constant uuid := 'f03a0000-0000-4000-8000-00000000000b';
  t_c   constant uuid := 'f03a0000-0000-4000-8000-00000000000c';
  i_pend constant uuid := 'f03b0000-0000-4000-8000-000000000001';
  i_exp  constant uuid := 'f03b0000-0000-4000-8000-000000000002';
  i_rev  constant uuid := 'f03b0000-0000-4000-8000-000000000003';
  i_used constant uuid := 'f03b0000-0000-4000-8000-000000000004';
  secret_kv   constant text := 'F03-SECRET-KV-VALUE';
  secret_hash constant text := 'f03hash';
  q_sum constant text := 'select x from public.admin_trip_summary() x';
  q_all constant text := 'select string_agg(x::text, '' '') from public.admin_trip_summary() x';
  n_inv_before bigint;
  n_trips_before bigint;
begin
  select count(*) into n_inv_before from public.manager_invites where used_at is null;
  select count(*) into n_trips_before from public.trips;

  begin
    -- ================= SETUP (as postgres, bypassing RLS) =================
    execute 'reset role';

    insert into auth.users (id, aud, role, email, instance_id, created_at, updated_at) values
      (u_adm, 'authenticated', 'authenticated', 'f03-adm@qa.invalid', '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_m,   'authenticated', 'authenticated', 'f03-m@qa.invalid',   '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_m2,  'authenticated', 'authenticated', 'f03-m2@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_e,   'authenticated', 'authenticated', 'f03-e@qa.invalid',   '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_v,   'authenticated', 'authenticated', 'f03-v@qa.invalid',   '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_p,   'authenticated', 'authenticated', 'f03-p@qa.invalid',   '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_rm,  'authenticated', 'authenticated', 'f03-rm@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_mb,  'authenticated', 'authenticated', 'f03-mb@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_mc,  'authenticated', 'authenticated', 'f03-mc@qa.invalid',  '00000000-0000-0000-0000-000000000000', now(), now()),
      (u_o,   'authenticated', 'authenticated', null,                 '00000000-0000-0000-0000-000000000000', now(), now());

    insert into public.platform_admins (user_id) values (u_adm);

    insert into public.trips (id, name, start_date, end_date, home_currency, status, created_by) values
      (t_a, 'F03 QA trip A', current_date - 2,  current_date + 20, 'ILS', 'active',   u_m),
      (t_b, 'F03 QA trip B', current_date - 30, current_date - 1,  'ILS', 'active',   u_mb),
      (t_c, 'F03 QA trip C', current_date + 30, current_date + 40, 'ILS', 'archived', u_mc);

    insert into public.trip_countries (trip_id, country_code, sort) values
      (t_a, 'VN', 0), (t_a, 'KH', 1), (t_b, 'TH', 0);

    insert into public.trip_members (trip_id, user_id, display_name, role, status, approved_at, joined_at) values
      (t_a, u_m,   'F03Mgr1',          'manager', 'active',  now(), now() - interval '5 days'),
      (t_a, u_m2,  'F03Mgr2',          'manager', 'active',  now(), now() - interval '4 days'),
      (t_a, u_e,   'F03SecretEditor',  'editor',  'active',  now(), now() - interval '3 days'),
      (t_a, u_v,   'F03SecretViewer',  'viewer',  'active',  now(), now() - interval '3 days'),
      (t_a, u_adm, 'F03SecretAdminV',  'viewer',  'active',  now(), now() - interval '3 days'),
      (t_a, u_p,   'F03SecretPending', 'editor',  'pending', null,  now() - interval '1 day'),
      (t_a, u_rm,  'F03SecretRemoved', 'manager', 'removed', now(), now() - interval '6 days'),
      (t_b, u_mb,  'F03MgrB',          'manager', 'active',  now(), now() - interval '40 days'),
      (t_c, u_mc,  'F03MgrC',          'manager', 'active',  now(), now() - interval '2 days');

    insert into public.trip_kv (trip_id, key, value, updated_by) values
      (t_a, 'trip', secret_kv, u_m), (t_b, 'expenses', secret_kv, u_mb);

    insert into public.manager_invites (id, email, token_hash, draft_name, invited_by, expires_at, used_at, revoked_at,
                                        created_at, last_sent_at, send_count, opened_at, inviter_name) values
      (i_pend, 'f03-pend@qa.invalid', secret_hash || '1', 'F03 pending', u_adm, now() + interval '5 days', null, null,
       now() - interval '2 days', now() - interval '2 days', 1, null, 'אור'),
      (i_exp,  'f03-exp@qa.invalid',  secret_hash || '2', 'F03 expired', u_adm, now() - interval '1 day',  null, null,
       now() - interval '8 days', now() - interval '8 days', 2, now() - interval '7 days', 'אור'),
      (i_rev,  'f03-rev@qa.invalid',  secret_hash || '3', 'F03 revoked', u_adm, now() + interval '3 days', null, now(),
       now() - interval '4 days', now() - interval '4 days', 1, null, 'אור'),
      (i_used, 'f03-used@qa.invalid', secret_hash || '4', 'F03 used',    u_adm, now() + interval '3 days', now(), null,
       now() - interval '4 days', now() - interval '4 days', 1, now(), 'אור');

    -- ================= C1 · access =================
    r := qa.f03_rec(r, 1, 'C1 manager (not platform admin) calling admin_trip_summary → not_platform_admin', 'error 42501',
      qa.f03_count(u_m, au, q_sum));
    r := qa.f03_rec(r, 2, 'C1 error message is exactly not_platform_admin', 'error contains not_platform_admin',
      qa.f03_count(u_m, au, q_sum));
    r := qa.f03_rec(r, 3, 'C1 editor → not_platform_admin', 'error 42501', qa.f03_count(u_e, au, q_sum));
    r := qa.f03_rec(r, 4, 'C1 viewer → not_platform_admin', 'error 42501', qa.f03_count(u_v, au, q_sum));
    r := qa.f03_rec(r, 5, 'C1 pending member → not_platform_admin', 'error 42501', qa.f03_count(u_p, au, q_sum));
    r := qa.f03_rec(r, 6, 'C1 outsider (unlinked anonymous) → not_platform_admin', 'error 42501', qa.f03_count(u_o, au, q_sum));
    r := qa.f03_rec(r, 7, 'C1 anon key without a session → no execute permission (nothing)', 'error 42501',
      qa.f03_count(null, 'anon', q_sum));
    r := qa.f03_rec(r, 8, 'C1 authenticated role without a sub claim → not_platform_admin', 'error 42501',
      qa.f03_count(null, au, q_sum));
    r := qa.f03_rec(r, 9, 'C1 platform admin can call it (3 QA trips + 3 unused QA invites)', '6 rows', qa.f03_count(u_adm, au, q_sum || ' where x->>''id'' like ''f03%'''));
    r := qa.f03_rec(r, 10, 'C1 platform_admins: a non-admin sees 0 rows (own row only)', '0 rows',
      qa.f03_count(u_m, au, 'select 1 from public.platform_admins'));
    r := qa.f03_rec(r, 11, 'C1 platform_admins: the admin sees exactly the own row (client isPlatformAdmin check)', '1 rows',
      qa.f03_count(u_adm, au, 'select 1 from public.platform_admins'));
    r := qa.f03_rec(r, 12, 'C1 manager_invites not readable by the admin through the API (service role only)', 'error 42501',
      qa.f03_count(u_adm, au, 'select 1 from public.manager_invites'));
    r := qa.f03_rec(r, 13, 'C1 manager_invites not writable by authenticated clients', 'error 42501',
      qa.f03_dml(u_adm, au, 'insert into public.manager_invites (email, token_hash, expires_at) values (''x@qa.invalid'', ''h'', now())'));
    r := qa.f03_rec(r, 14, 'C1 manager_invites not readable by anon', 'error 42501',
      qa.f03_count(null, 'anon', 'select 1 from public.manager_invites'));

    -- ================= C3 · summary content =================
    r := qa.f03_rec(r, 20, 'C3 summary contains no trip_kv value', 'false',
      qa.f03_val(u_adm, au, pg_catalog.format('select (coalesce((%s), '''') like %L)::text', q_all, '%' || secret_kv || '%')));
    r := qa.f03_rec(r, 21, 'C3 summary contains no token_hash value', 'false',
      qa.f03_val(u_adm, au, pg_catalog.format('select (coalesce((%s), '''') like %L)::text', q_all, '%' || secret_hash || '%')));
    r := qa.f03_rec(r, 22, 'C3 summary has no token/hash keys', 'false',
      qa.f03_val(u_adm, au, 'select (bool_or(x ? ''token_hash'' or x ? ''token'' or x ? ''hash''))::text from public.admin_trip_summary() x'));
    r := qa.f03_rec(r, 23, 'C3 summary contains no non-manager member names (editor/viewer/pending/removed)', 'false',
      qa.f03_val(u_adm, au, pg_catalog.format('select (coalesce((%s), '''') like %L)::text', q_all, '%F03Secret%')));
    r := qa.f03_rec(r, 24, 'C3 trip A managers = the two active managers, in join order', '["F03Mgr1", "F03Mgr2"]',
      qa.f03_val(u_adm, au, pg_catalog.format('select (x->''managers'')::text from public.admin_trip_summary() x where x->>''id'' = %L', t_a)));
    r := qa.f03_rec(r, 25, 'C3 trip rows carry only the allowed keys', 'countries,end_date,id,kind,managers,member_count,name,start_date,status',
      qa.f03_val(u_adm, au, pg_catalog.format(
        'select string_agg(k, '','' order by k) from (select jsonb_object_keys(x) k from public.admin_trip_summary() x where x->>''id'' = %L) s', t_a)));
    r := qa.f03_rec(r, 26, 'C3 invite rows carry only the allowed keys', 'created_at,draft_name,email,expires_at,id,kind,last_sent_at,opened_at,send_count,status',
      qa.f03_val(u_adm, au, pg_catalog.format(
        'select string_agg(k, '','' order by k) from (select jsonb_object_keys(x) k from public.admin_trip_summary() x where x->>''id'' = %L) s', i_pend)));
    r := qa.f03_rec(r, 27, 'C3 used invites are not listed', '0 rows',
      qa.f03_count(u_adm, au, pg_catalog.format('%s where x->>''id'' = %L', q_sum, i_used)));

    -- ================= C13 · derived statuses + counts =================
    r := qa.f03_rec(r, 30, 'C13 trip A (end_date in the future) → active', 'active',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', t_a)));
    r := qa.f03_rec(r, 31, 'C13 trip B (end_date < today) → ended', 'ended',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', t_b)));
    r := qa.f03_rec(r, 32, 'C13 trip C (status archived, future dates) → ended', 'ended',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', t_c)));
    r := qa.f03_rec(r, 33, 'C13 invite with future expiry → pending', 'pending',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', i_pend)));
    r := qa.f03_rec(r, 34, 'C13 invite past expiry, not revoked → expired', 'expired',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', i_exp)));
    r := qa.f03_rec(r, 35, 'C13 revoked invite → revoked', 'revoked',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''status'' from public.admin_trip_summary() x where x->>''id'' = %L', i_rev)));
    r := qa.f03_rec(r, 36, 'C13 trip A member_count = 5 active (pending/removed not counted)', '5',
      qa.f03_val(u_adm, au, pg_catalog.format('select x->>''member_count'' from public.admin_trip_summary() x where x->>''id'' = %L', t_a)));
    r := qa.f03_rec(r, 37, 'C13 trip A countries by sort', '["VN", "KH"]',
      qa.f03_val(u_adm, au, pg_catalog.format('select (x->''countries'')::text from public.admin_trip_summary() x where x->>''id'' = %L', t_a)));
    r := qa.f03_rec(r, 38, 'C13 counts of the QA rows by status (active/ended/pending/expired/revoked)', '1/2/1/1/1',
      qa.f03_val(u_adm, au,
        'select count(*) filter (where x->>''status'' = ''active'') || ''/'' || count(*) filter (where x->>''status'' = ''ended'') || ''/'' ||'
        || ' count(*) filter (where x->>''status'' = ''pending'') || ''/'' || count(*) filter (where x->>''status'' = ''expired'') || ''/'' ||'
        || ' count(*) filter (where x->>''status'' = ''revoked'') from public.admin_trip_summary() x where x->>''id'' like ''f03%'''));
    r := qa.f03_rec(r, 39, 'C13 invite row fields: send_count and opened_at as stored', '2/true',
      qa.f03_val(u_adm, au, pg_catalog.format(
        'select (x->>''send_count'') || ''/'' || ((x->>''opened_at'') is not null)::text from public.admin_trip_summary() x where x->>''id'' = %L', i_exp)));

    -- ================= C5 · one active invite per email (unique partial index) =================
    r := qa.f03_rec(r, 40, 'C5 second active invite for the same email is refused (unique)', 'error 23505',
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''f03-pend@qa.invalid'', ''f03hash-dup'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 41, 'C5 an expired-but-not-revoked invite still holds the slot at DB level (function revokes it first)', 'error 23505',
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''f03-exp@qa.invalid'', ''f03hash-dup2'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 42, 'C5 upper-case email is refused by the lower-case check (function normalizes first)', 'error 23514',
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''F03-New@qa.invalid'', ''f03hash-up'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 43, 'C5 revoking the expired one frees the slot: new invite accepted', '1 rows / 1 rows',
      qa.f03_dml(null, 'postgres', pg_catalog.format('update public.manager_invites set revoked_at = now() where id = %L', i_exp))
      || ' / ' ||
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''f03-exp@qa.invalid'', ''f03hash-new'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 44, 'C5 a revoked invite does not block a new one', '1 rows',
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''f03-rev@qa.invalid'', ''f03hash-new2'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 45, 'C5 a used invite does not block a new one', '1 rows',
      qa.f03_dml(null, 'postgres', 'insert into public.manager_invites (email, token_hash, expires_at) values (''f03-used@qa.invalid'', ''f03hash-new3'', now() + interval ''7 days'')'));
    r := qa.f03_rec(r, 46, 'C5 new columns default: send_count = 1', '1',
      qa.f03_val(null, 'postgres', 'select send_count::text from public.manager_invites where token_hash = ''f03hash-new3'''));
    r := qa.f03_rec(r, 47, 'C5 inviter_name longer than 40 chars refused', 'error 23514',
      qa.f03_dml(null, 'postgres', pg_catalog.format('update public.manager_invites set inviter_name = %L where id = %L', pg_catalog.repeat('x', 41), i_pend)));

    execute 'reset role';
    raise exception 'f03_rollback';
  exception when others then
    if sqlerrm <> 'f03_rollback' then
      r := r || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
        'id', 0, 'name', 'unexpected error (setup or harness) — later scenarios did not run',
        'detail', 'expected: no error | actual: error ' || sqlstate || ' ' || sqlerrm, 'pass', false));
    end if;
  end;

  execute 'reset role';
  perform pg_catalog.set_config('request.jwt.claims', '', true);
  perform pg_catalog.set_config('request.jwt.claim.sub', '', true);
  perform pg_catalog.set_config('request.jwt.claim.role', '', true);

  -- ================= §2 · catalog checks (read-only, outside the rolled-back block) =================
  r := qa.f03_rec(r, 50, '§2 admin_trip_summary is security definer, stable, search_path=''''', 'true/s/search_path=""',
    (select p.prosecdef::text || '/' || p.provolatile::text || '/' || coalesce(array_to_string(p.proconfig, ','), '')
       from pg_catalog.pg_proc p where p.oid = 'public.admin_trip_summary()'::regprocedure));
  r := qa.f03_rec(r, 51, '§2 anon cannot execute admin_trip_summary', 'false',
    pg_catalog.has_function_privilege('anon', 'public.admin_trip_summary()', 'execute')::text);
  r := qa.f03_rec(r, 52, '§2 authenticated can execute admin_trip_summary', 'true',
    pg_catalog.has_function_privilege('authenticated', 'public.admin_trip_summary()', 'execute')::text);
  r := qa.f03_rec(r, 53, '§2 no client grants on manager_invites (authenticated/anon: no select/insert/update/delete)', 'false',
    (pg_catalog.has_table_privilege('authenticated', 'public.manager_invites', 'select,insert,update,delete')
     or pg_catalog.has_table_privilege('anon', 'public.manager_invites', 'select,insert,update,delete'))::text);
  r := qa.f03_rec(r, 54, '§2 no new tables in public (still the 9 F01 tables)', '9',
    (select count(*)::text from pg_catalog.pg_tables where schemaname = 'public'));
  r := qa.f03_rec(r, 55, '§2 partial unique index on manager_invites(email) where unused and not revoked', '1',
    (select count(*)::text from pg_catalog.pg_indexes
      where schemaname = 'public' and tablename = 'manager_invites' and indexname = 'manager_invites_one_active_per_email'
        and indexdef like '%UNIQUE%' and indexdef like '%used_at IS NULL%' and indexdef like '%revoked_at IS NULL%'));
  r := qa.f03_rec(r, 56, '§2 manager_invites has the 4 new columns', 'inviter_name,last_sent_at,opened_at,send_count',
    (select string_agg(column_name::text, ',' order by column_name) from information_schema.columns
      where table_schema = 'public' and table_name = 'manager_invites'
        and column_name in ('send_count', 'last_sent_at', 'opened_at', 'inviter_name')));
  r := qa.f03_rec(r, 57, '§2 manager_invites RLS enabled, still no policies', 'true/0',
    (select c.relrowsecurity::text || '/' ||
            (select count(*) from pg_catalog.pg_policies p where p.schemaname = 'public' and p.tablename = 'manager_invites')
       from pg_catalog.pg_class c where c.oid = 'public.manager_invites'::regclass));
  r := qa.f03_rec(r, 58, 'Leftovers after the run: 0 f03 users, 0 f03 trips, invites/trips counts unchanged', '0/0/true',
    (select (select count(*) from auth.users where id::text like 'f0300000-%')::text || '/' ||
            (select count(*) from public.trips where id::text like 'f03a0000-%')::text || '/' ||
            ((select count(*) from public.manager_invites where used_at is null) = n_inv_before
             and (select count(*) from public.trips) = n_trips_before)::text));

  return query
    select (e->>'id')::int, e->>'name', (e->>'pass')::boolean, e->>'detail'
      from pg_catalog.jsonb_array_elements(r) e;
end $$;

revoke all on all functions in schema qa from public, anon, authenticated;
