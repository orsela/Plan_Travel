# F02 — The app on the new infrastructure · spec

Version target: **3.0.0-alpha.2** (S1). Approved by Or 2026-10-04 (technical diagram + acceptance criteria).

User value: the family gets the same app they know (v2.15.2), now on the secured `plan-travel` backend where only trip members can read or write trip data.

Base code: `orsela/Vietnam_Travel_Planner` @ v2.15.2 (`index.html`, `sw.js`, `manifest.json`, icons). The live repo and the live Supabase project `vietnam-trip-2026` (ref `kkitwcnkoxuhbcsabcdl`) are **never written to and never called** by anything in this repo.

Supabase: project `plan-travel`, URL `https://zwufpxnioqaweobnvffs.supabase.co`, publishable key `sb_publishable_Atuc7ybYTF2o7ujCJBjKjA_So_TbaV8` (client-visible by design; RLS from F01 is the protection).

## 0. Decisions (Or, 2026-10-04)

| # | Decision |
|---|---|
| D1 | Identity in F02: **anonymous Supabase sign-in**, created silently on first open. The device is linked to a trip by Claude via SQL (`trip_members` row, status `active`). No login screen until S2. |
| D2 | Test-trip data: an export of the live `planner_kv` table that **Or** produces (dashboard CSV/JSON). Claude converts it to `trip_kv` rows. Claude never queries the live project. |
| D3 | Chat button stays visible; tapping it shows "בקרוב" and makes **no network call**. Real chat comes in F14. |
| D4 | Base v2.15.2; revert flag value `local-only` (never a "legacy" mode pointing at the live project); version `3.0.0-alpha.2`. |
| D5 | Vietnam-specific content (UTC+7, "טיסות" detection, `SEED_DATA`, `USERS`) is **unchanged** in F02 — it belongs to F08/F16. |

## 1. Files in the repo

`app/index.html`, `app/sw.js`, `app/manifest.json`, `app/icon-192.png`, `app/icon-512.png`, `app/apple-touch-icon.png` (served by GitHub Pages from `/app/`, or repo root if Pages is configured on root — implementer documents which). Plus `scripts/` (import tool) and `tests/` (QA).

## 2. Same-origin isolation (critical)

`orsela.github.io/Plan_Travel/…` and `orsela.github.io/Vietnam_Travel_Planner/` share one origin: localStorage, IndexedDB and Cache Storage are shared. The new app must not read, write, or delete **anything** the live app owns.

| Store | Live app uses | New app must use |
|---|---|---|
| localStorage | keys starting `planner:` and `vietnam_planner_` | only keys starting `pt:` — trip data under `pt:<trip_id>:<original key>`, device-level under `pt:_device:<name>` |
| IndexedDB | database `vtp-vault` | database `pt-vault-<trip_id>` |
| Cache Storage | `vtp-icons-v1`, `vtp-shell-v1` | `pt-icons-v1`, `pt-shell-v1`; the service worker must never delete a cache whose name doesn't start with `pt-` |
| Service worker | scope `/Vietnam_Travel_Planner/` | scope of its own directory only |
| Manifest | `id: vietnam-travel-planner` | `id: plan-travel` (name/short_name/icons unchanged → no visible change) |

Rule: any code path that enumerates localStorage (`localStorage.length`/`key(i)`, e.g. `SS.list` fallback) only sees `pt:<trip_id>:` keys, with the prefix stripped so the app logic is unchanged. No code ever calls `localStorage.clear()`.

Recommended approach (implementer may choose another that meets the rule): one namespacing layer installed before the main script, so the ~30 existing `localStorage.*` call sites stay byte-identical.

## 3. Boot sequence

1. Load supabase-js (same CDN as today). Create client with the `plan-travel` URL + publishable key, `persistSession: true`. The auth session is stored by supabase-js under its default key `sb-zwufpxnioqaweobnvffs-auth-token` (does not collide with the live app's `sb-kkitwcnkox…` key).
2. If no session → `auth.signInAnonymously()`. If that fails (offline, disabled) → go to step 5 with the cached trip, or to the "not connected" state.
3. Resolve the active trip: `select trip_id, display_name, role from trip_members where user_id = auth.uid() and status = 'active'` (allowed by F01 RLS: own rows). 0 rows → "not connected" state. ≥2 rows → the most recent `joined_at` (picker comes in F09). Cache `{trip_id, role}` in `pt:_device:active_trip`.
4. Offline start: if the network is unavailable and `pt:_device:active_trip` exists, start immediately on the cached trip from local data; reconnect later as today.
5. Only then does the main app initialize (`loadState` etc.) — no main-script code reads storage before the trip is known.

**"Not connected" state** (the only new UI; shown only to an unlinked device): a single centered card in the existing UI-kit style, Hebrew, RTL, 390px-safe: title `המכשיר עדיין לא מחובר לטיול`, body `שלחו למנהל הטיול את קוד המכשיר:`, the first 8 hex chars of `auth.uid()` in a large mono font with a copy button, and a `נסו שוב` button that re-runs step 3. No app tabs behind it, no empty dashboards. Device linking is then done by Claude via SQL.

## 4. `window.storage` on `trip_kv`

Same interface as v2.15.2 (`get`, `set`, `delete`, `list`), same 8 s timeout, same throw-on-error contract (so the existing offline queue and SYNC-MERGE keep working unchanged).

- Key mapping: app key `planner:X` ↔ db key `X` (the spec'd "no `planner:` prefix"). Any other app key is stored as-is.
- `get(k)` → `select value from trip_kv where trip_id=$T and key=map(k)`.
- `set(k,v)` → upsert `(trip_id=$T, key, value)` on conflict `(trip_id,key)`. Do not send `updated_at`/`updated_by` (DB sets them).
- `delete(k)` → delete by `(trip_id, key)`.
- `list(prefix)` → `like map(prefix)%` within `trip_id=$T`, returned keys mapped back to app keys. `%`/`_` in the prefix must be escaped.
- An RLS rejection (viewer write) is an error → the existing code enqueues it; the sync card shows the pending count (existing behavior). The local copy is never discarded.
- The offline queue (`planner:__queue__`) and SYNC-MERGE bases (`planner:mergebase:*`) live under the trip namespace automatically via §2.

## 5. Flags and versions

- `STORAGE_BACKEND='cloud'` (default) | `'local-only'`. `local-only`: no supabase-js client, no auth, no network to Supabase at all; trip namespace = `pt:local:`; the app behaves as v2.15.2 did with an unfilled Supabase block ("מצב מקומי בלבד").
- `CHAT_FEATURE='soon'` (F02) | `'on'` (F14). In `soon`: the chat button opens nothing networked; it shows a toast `הצ'אט יחזור בקרוב`. `CHAT_ENDPOINT` must be `''`.
- `APP_VERSION='3.0.0-alpha.2'`; `<title>` version updated likewise. The update-card check keeps fetching `location.pathname` (now the new app's own URL).
- Every change carries `CHANGE 2026-10-04 F02-…` comments stating what changed from v2.15.2.
- Protected functions byte-identical to v2.15.2: `drawMap`, `drawGoogleMap`, `drawSchematicMap`, `fallbackToSchematic`, `SEED_DATA`, `stayBlock`, `bookingUrl`.

## 6. Data import tool

`scripts/import_planner_kv.py <export.csv|json> --trip <uuid>` → emits `INSERT … ON CONFLICT DO UPDATE` SQL for `trip_kv`, stripping `planner:` from keys, preserving `value` byte-for-byte, skipping nothing silently (prints a count per key family: trip, expenses, reservations, journal:*, checkins, timeline, budget, other). Plus a verification query that prints the same counts from `trip_kv`. Vault documents (device-local IndexedDB) are not part of the import (F12/F16).

## 7. Database (migration `0005_f02`)

- Add `trip_kv` and `trip_members` to the `supabase_realtime` publication (planned in F01 for F02; the app does not require Realtime yet, but it costs nothing and F05 needs it).
- No new tables, no policy changes. Advisors must stay clean.

## 8. Acceptance criteria (QA verifies; approved by Or)

Identity and access
1. New device: anonymous identity created on first open with no new screen (for a linked device); survives reload (same `auth.uid()`).
2. Unlinked device, or member `pending`/`removed`: 0 trip rows; the "not connected" card from §3 is shown — not an empty or broken app.
3. Viewer write: rejected by RLS, local change kept, sync card shows the pending change.

Parity with v2.15.2
4. Import of Or's export: counts per key family and budget totals equal source.
5. Same rendering as v2.15.2 on the same data: home, route, budget, reservations, journal, check-in, vault, converter.
6. Every write lands in `trip_kv` with the correct `trip_id`, `updated_by = auth.uid()`.
7. Zero network requests to `kkitwcnkoxuhbcsabcdl` (or any `vietnam` path) during a full session.

Offline and sync
8. Offline changes are queued under `pt:<trip_id>:` and flushed on reconnect.
9. Two devices editing concurrently → same SYNC-MERGE outcome as v2.15.2.

Same-domain separation
10. Seeded live-app state (`planner:*`, `vietnam_planner_*` localStorage keys, `vtp-vault` IndexedDB, `vtp-*` caches) is byte-identical after a full session in the new app.
11. Two different `trip_id`s on one device do not mix (localStorage and IndexedDB).

Chat and flag
12. Chat button shows "בקרוב", zero network calls.
13. `STORAGE_BACKEND='local-only'` works fully from the device, zero calls to Supabase.

Definition of Done
14. 390px: no horizontal scroll, RTL intact, touch ≥44px for the new card; map loads on the new path (if the Google key's referrer blocks it, report to Or — it's a Cloud Console setting, not a code fix).
15. Protected functions byte-identical; version + CHANGE comments present; `node --check` passes on every script block.
16. Supabase advisors clean; the publishable key without a session reads 0 rows.

## 9. Test strategy

The CI/sandbox network cannot reach Supabase. Therefore:
- **Client tests** (Playwright, headless Chromium, 390×844): the page is loaded with a **fake supabase-js** injected in place of the CDN script, implementing `auth` (anonymous, session persistence), and `from('trip_kv'|'trip_members')` with an in-memory RLS model equivalent to F01 (member/viewer/pending/none). The fake also records every request so criteria 7/12/13 can be asserted. Network to any other host is intercepted and logged.
- **DB tests** (criteria 3, 6 at the DB level, 16): a `qa.f02_run()` function in the locked `qa` schema (same pattern as F01), always rolled back, run via the Connector.
- **Real-device check** by Or after deployment (criteria 1, 14).
