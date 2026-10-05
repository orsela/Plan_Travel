# Plan_Travel – CHANGELOG

## 3.0.0-alpha.2 — S1

### F02 · The app on the new infrastructure — 2026-10-05
- `app/`: v2.15.2 running on `plan-travel` (index.html, sw.js, manifest.json, icons). Live at https://orsela.github.io/Plan_Travel/app/.
- Identity: silent anonymous Supabase sign-in; active trip resolved from `trip_members`; unlinked devices see the
  "המכשיר עדיין לא מחובר לטיול" card with an 8-char device code (the only new UI).
- `window.storage` reads/writes `trip_kv` by `trip_id` (keys without `planner:`), same interface/timeout as v2.15.2;
  offline queue and SYNC-MERGE unchanged.
- Same-origin isolation from the live app: localStorage under `pt:<trip_id>:`, IndexedDB `pt-vault-<trip_id>`,
  caches `pt-*`; the live app's `planner:*` / `vietnam_planner_*` / `vtp-*` state is never read or written.
- Flags (top-level): `STORAGE_BACKEND='cloud'|'local-only'`, `CHAT_FEATURE='soon'` (chat shows "בקרוב", no network).
- Protected functions byte-identical to v2.15.2. Live project `vietnam-trip-2026` untouched (read-only comparison only).
- `scripts/import_planner_kv.py`: planner_kv export → trip_kv SQL + verification counts.
- DB: `0005_f02_test_trip` (test trip `f0200000-…-0001` + `admin_assign_device`, applied 2026-10-04 by an earlier session;
  data = 32 keys copied read-only from the live app, 29 byte-identical, 3 updated in the live app afterwards);
  `0006_f02` (Realtime on trip_kv/trip_members; trigger `trip_kv_set_updated_by`);
  `0007_qa_f02_harness` (`qa.f02_run()`). 0006/0007 were run by Or in the dashboard SQL Editor on 2026-10-05
  (Connector write approval unavailable), so they are not in `supabase_migrations.schema_migrations`.
- QA: browser 39/39 (1 skipped → covered by DB), DB 37/37, advisors: no new findings. Or's phone: linked and opens the trip.
  See docs/qa/F02_QA.md.

## 3.0.0-alpha.1 — S1

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
