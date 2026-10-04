# Plan_Travel – CHANGELOG

## 3.0.0-alpha.1 (in progress) — S1

### F01 · Trip isolation (schema + RLS) — 2026-10-04
- Supabase project `plan-travel` (eu-central-1, free plan).
- `0001_core`: 9 tables (platform_admins, trips, trip_join_codes, trip_countries, trip_currencies, trip_members,
  manager_invites, relink_tokens, trip_kv), 7 indexes, helper functions `is_platform_admin()`, `trip_role()`,
  `my_trip_ids()`, triggers (`currency_limit` ≤10, `base_currency_locked`, `last_manager`, `trip_kv_trip_locked`,
  `updated_at`), RLS on all tables (authenticated only; anon has no privileges).
- Decision (Or, 2026-10-04): join code stored readable by trip managers only (not hashed) so the link can always be copied.
- `0002`–`0004`: QA harness `qa.f01_run()` in a locked, non-exposed `qa` schema (109 scenarios, always rolled back).
- QA: 109/109 pass, no test data left behind. See docs/qa/F01_QA.md.

## 0.0.1 — 2026-10-04
- Repo bootstrap (README).
