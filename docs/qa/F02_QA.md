# F02 · QA report — v3.0.0-alpha.2 (2026-10-05)

QA team worked from docs/F02_spec.md only (black box). Suite: tests/f02/ (Playwright + fake supabase-js) and
supabase/migrations/0007_qa_f02_harness.sql (`select * from qa.f02_run() order by id;`, always rolled back).

| Layer | Result |
|---|---|
| Browser suite vs dev build | 39 passed, 1 skipped (C16 → DB) |
| Browser suite vs v2.15.2 baseline (discrimination check) | 26 failed (expected), 12 passed (static checks the baseline already met) |
| DB `qa.f02_run()` on plan-travel | 37/37 pass, 0 leftovers |
| Supabase advisors | no new findings from F02 (existing F01 INFO/WARN items are by design: anonymous members are `authenticated`; RLS helper functions are callable; token tables have no policies; unused indexes) |
| Real phone (Or) | anonymous identity created (device 4ed9cc42), not-connected card shown, linked via `admin_assign_device`, app opens on the test trip |

## Acceptance criteria
| # | Criterion | Evidence |
|---|---|---|
| 1 | Silent anonymous identity, survives reload | browser test_ac01; phone ✓ |
| 2 | Unlinked / pending / removed → not-connected card | test_ac02 ×3; DB #20–28 |
| 3 | Viewer write rejected, kept locally, pending shown | test_ac03; DB #10–15 |
| 4 | Import counts equal source | test_ac04 (synthetic CSV+JSON, byte check); test trip: 32/32 keys present, 29 identical md5, 3 newer in live app |
| 5 | Same rendering as v2.15.2 on 8 screens | test_ac05 |
| 6 | Writes land with trip_id, updated_by = auth.uid() | test_ac06; DB #1–7 (needed trigger F02-DB-02, found by QA) |
| 7 | Zero requests to the live project | test_ac07 |
| 8 | Offline queue under pt:<trip_id>: and flushed | test_ac08 |
| 9 | Concurrent edits → same SYNC-MERGE result | test_ac09 (two contexts, shared backend) |
| 10 | Live-app local state byte-identical | test_ac10 |
| 11 | Two trips don't mix | test_ac11; DB #30–33 |
| 12 | Chat "בקרוב", no network | test_ac12 |
| 13 | local-only flag: zero Supabase calls | test_ac13 |
| 14 | 390px, RTL, ≥44px | test_ac14 |
| 15 | Protected functions identical, version, CHANGE comments, node --check | test_ac15 ×20 |
| 16 | Advisors clean, anon reads 0 rows | DB #40–44; advisors |

## Defects found and fixed during the cycle
1. `updated_by` kept the previous writer on update (DB) → trigger `trip_kv_set_updated_by` (0006).
2. Flags were inside the gate IIFE, not globals → moved to a top-level script (F02-FLAG-01).
3. Import tool printed the verification query only with `--verify-sql` → now always (F02-IMPORT-02).
4. Harness read localStorage through the app's namespaced view → reads raw storage from a blank same-origin page.

## Still to confirm on the phone (tests/f02/MANUAL.md)
Reopen after closing (same identity), Add-to-Home-Screen, Google map on the new path, chat button, old app intact, two-device sync.
