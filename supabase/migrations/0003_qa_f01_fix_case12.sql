-- Plan_Travel migration 0003_qa_f01_fix_case12 · version 3.0.0-alpha.1 · F01 QA
-- CHANGE 2026-10-04 F01: scenario 12 also accepts the trip_kv_trip_locked trigger error (the move is blocked before RLS WITH CHECK runs).
do $do$
declare d text; n text;
begin
  d := pg_get_functiondef('qa.f01_run()'::regprocedure);
  n := replace(d,
    $s$'C3/§4 eA moves an A kv row to trip B (update trip_id)', 'error 42501 or 0 rows'$s$,
    $s$'C3/§4 eA moves an A kv row to trip B (update trip_id)', 'error 42501 or 0 rows or error contains trip_kv_trip_locked'$s$);
  if n = d then raise exception 'patch target not found'; end if;
  execute n;
end
$do$;
revoke all on all functions in schema qa from public, anon, authenticated;
