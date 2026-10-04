# F01 QA report — 3.0.0-alpha.1 · 2026-10-04

Target: Supabase project `plan-travel` (ref zwufpxnioqaweobnvffs), Postgres 17.
Method: black-box suite written by the QA team from docs/F01_spec.md only (did not read the migration);
installed as `qa.f01_run()`; every run creates 7 fake users / 2 trips inside a sub-transaction that is always rolled back.

## Result
- Run 1: 108 pass / 1 fail — #12 (editor moves a trip A kv row to trip B). The move WAS blocked, by the
  `trip_kv_trip_locked` BEFORE UPDATE trigger (error 23514) instead of RLS. Behaviour correct; expectation widened.
- Run 2 (after fix) and run 3 (after search_path hardening): **109 pass / 0 fail**. Leftovers after run: 0 users, 0 trips.

## Coverage (criteria → scenario ids)
C1 3–5 · C2 6–10 · C3 11,12,15 · C4 13–15 · C5 16–19 · C6 20–26 · C7 27–35 · C8 2,36–48 · C9 49–54 · C10 55–61 ·
C11 62–66 · C12 67–71 · C13 72–73 · C14 74–76 · C15 77–82 · C16 83–86 · C17 87–90 · C18 91–100 · C19 101–104 · C20 105–106 · §4 107–109

## Supabase advisors
- Security INFO `rls_enabled_no_policy` on manager_invites, relink_tokens — intentional (service role only).
- Security WARN `authenticated_security_definer_function_executable` on is_platform_admin / trip_role / my_trip_ids —
  accepted: they return only the caller's own flags/roles/trip ids and are required by the policies.
- Security WARN `function_search_path_mutable` on qa.* — fixed in 0004.
- Performance INFO `unused_index` ×3 — expected on an empty database.

## Known limitations (for later features)
- Deleting an auth user who is a trip's only active manager fails with `last_manager` (only trip deletion is exempt). Revisit in F06/F07.
- `trip_kv.updated_by` defaults to auth.uid() but is client-settable.

## Not covered (needs real devices)
Real sign-in flows (F03–F05), Realtime, client app (F02).
