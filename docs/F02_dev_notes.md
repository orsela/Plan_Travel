# F02 · dev notes — app on the plan-travel backend (v3.0.0-alpha.2)

<!-- CHANGE 2026-10-04 F02-DOCS-01: new file (no previous version). -->

Contract: `docs/F02_spec.md`. Base: `orsela/Vietnam_Travel_Planner` v2.15.2 (md5 of index.html `931019a2effd1c4f273a78616fc68240`), copied into `app/`. The base repo was not modified.

## 1. What changed (file / area → why)

| File / area | CHANGE ID | What changed from v2.15.2 | Why (spec) |
|---|---|---|---|
| `app/index.html` `<title>` | F02-VER-02 | v2.15.2 → v3.0.0-alpha.2 | §5 |
| `app/index.html` main script, `APP_VERSION` line | F02-VER-01 | `'2.15.2'` → `'3.0.0-alpha.2'`. **The only changed line inside the main script.** | §5 |
| `app/index.html` head: GM-08 block (static supabase-js `<script src>` + inline IIFE defining `window.storage` on the old table) | F02-GATE-01 | **Removed and replaced** by one new block (style + script). **Unavoidable modification**: the IIFE held the old project's URL/key, which must not appear in the app. | §0, §3, §4 |
| same new block | F02-NS-01 | localStorage + IndexedDB namespacing layer | §2 |
| same new block | F02-BOOT-01 | boot gate: supabase-js → anon sign-in → trip lookup → start main script | §3 |
| same new block | F02-NC-01 | "not connected" card + its CSS | §3 |
| same new block | F02-KV-01 | `window.storage` on `trip_kv` | §4 |
| `app/index.html` head: new small classic `<script>` before the gate block | F02-FLAG-01 | top-level `const STORAGE_BACKEND`, `const CHAT_FEATURE` (globals, same convention as `HOME_LAYOUT`/`VISITS_FEATURE`); the gate IIFE reads them | §5 |
| same new block | F02-CHAT-01 | chat FAB → toast, no network | §0 D3, §5 |
| same new block | F02-MAPS-01 | placeholder `window.initMap` + replay | consequence of BOOT-02 (see §6 below) |
| `app/index.html` main `<script>` opening tag | F02-BOOT-02 | `<script>` → `<script type="text/plain" id="ptMainScript">`; the gate executes its text after the trip is known. **Tag change only; body byte-identical except F02-VER-01.** | §3 step 5 |
| `app/sw.js` | F02-SW-01 | caches `vtp-icons-v1`/`vtp-shell-v1` → `pt-icons-v1`/`pt-shell-v1`; `ownCache()` guard on the only `caches.delete`; icon lookup `caches.match(req)` (searched every cache on the origin, incl. the live app's) → `caches.open(ICON_CACHE).match(req)`. **Two existing lines modified** (flagged inline). | §2 |
| `app/manifest.json` | F02-MAN-01 | `id`: `vietnam-travel-planner` → `plan-travel`; a `_change` member carries the CHANGE note (JSON has no comments; browsers ignore unknown members). name/short_name/icons unchanged. | §2 |
| `app/icon-192.png`, `icon-512.png`, `apple-touch-icon.png` | — | copied unchanged | §1 |
| `scripts/import_planner_kv.py` | F02-IMPORT-01, F02-IMPORT-02 | new; IMPORT-02: a normal run always ends with the verification queries | §6 |
| `supabase/migrations/0005_f02.sql` | F02-DB-01 (dev), F02-DB-02 (architect) | new (not applied). DB-02 trigger `trip_kv_set_updated_by` written by the architect, not by dev | §7 |
| `scripts/dev/f02_static_check.py`, `f02_smoke.py`, `fake_supabase.js` | F02-DEVCHK-01, F02-DEVSMOKE-01 | dev self-checks (not the QA suite) | DoD self-check |

Everything else in v2.15.2 is untouched: all ~30 `localStorage.*` call sites, `SS`, offline queue, SYNC-MERGE, vault code (`indexedDB.open('vtp-vault',1)` stays literally in the code — it's redirected by the layer), `CHAT_ENDPOINT` line, update check, SEED_DATA/USERS/UTC+7 (D5).

## 2. Namespacing layer (F02-NS-01)

Installed in the head block, before any other app script runs.

- Captures the native `Storage.prototype` methods and the `length` getter, then wraps them. The wrappers act **only when `this === window.localStorage`**; sessionStorage is untouched.
  - `getItem/setItem/removeItem(k)` → native call on `NS + k`, where `NS = 'pt:<trip_id>:'` (`'pt:local:'` in local-only).
  - `length` / `key(i)` → computed over raw keys starting with `NS`, returned with `NS` stripped. So `SS.list`'s fallback loop sees exactly this trip's keys, under their original names.
  - `clear()` → removes only `NS` keys (no code calls it; safety net). No code anywhere calls `localStorage.clear()`.
  - Before `NS` is set: reads return `null`, length is 0, writes are dropped with a console warning. The main script doesn't run before `NS` is set, so this only affects third-party code.
- Gate-internal raw access (`rGet/rSet/rDel`) uses the captured native methods and is restricted by `assertOwn()` to keys starting `pt:` or `sb-zwufpxnioqaweobnvffs-` (supabase-js auth token). Device-level key: `pt:_device:active_trip` = `{"trip_id","role"}`.
- supabase-js gets an explicit `auth.storage` adapter over the raw accessors, so its session lives at the default key `sb-zwufpxnioqaweobnvffs-auth-token` (not inside a trip namespace — it's device-level and must exist before the trip is known).
- IndexedDB: `IDBFactory.prototype.open/deleteDatabase` are wrapped: `vtp-vault` → `pt-vault-<trip_id>`; names already starting `pt-` pass through; any other name → `pt-<trip_id>-<name>` (or `pt-_device-<name>` before the trip is known). `indexedDB.databases()` is not wrapped (read-only; unused by the app).
- Cache Storage: only the service worker uses it; see F02-SW-01. The page never touches caches.
- Consequence (expected, same as v2.15.2 on a new device): on first start in a trip namespace the device has no local `vietnam_planner_user_v1`, so the "who are you" picker opens once per trip.

## 3. Boot gate (F02-BOOT-01 / BOOT-02)

`<html>` gets class `pt-booting` (body content hidden) until the gate decides.

1. `STORAGE_BACKEND !== 'cloud'` → namespace `pt:local:`, start main script. No supabase-js, no network.
2. Read cached `pt:_device:active_trip`.
3. If `navigator.onLine === false` and a cached trip exists → start immediately on it (step 4 of spec); try to create the client from the SW-cached library in the background.
4. Load supabase-js dynamically from the same CDN URL as v2.15.2 (`https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2`, 10 s cap). Create the client (`persistSession: true`, `autoRefreshToken: true`, `detectSessionInUrl: false`). Failure → cached trip if any, else the card.
5. `auth.getSession()`; none → `auth.signInAnonymously()` (8 s timeout each). Failure → cached trip if any, else the card (with code `--------`, copy disabled; "נסו שוב" re-runs the whole sequence).
6. `trip_members.select(trip_id, display_name, role, joined_at).eq(user_id, uid).eq(status,'active').order(joined_at desc)`; the newest row wins (picker = F09). 0 rows → remove `pt:_device:active_trip`, show card. Query error → cached trip if any, else card.
7. Cache `{trip_id, role}`, set namespace, install `window.storage`, then **start**: remove the card, create a `<script>` whose text is `#ptMainScript`'s text and append it (runs synchronously in global scope, same as the original inline script), then apply F02-CHAT-01 and F02-MAPS-01.

An `online` listener creates the client later if the app started offline without the library, so the existing `online → flushQueue` path reaches `trip_kv` without a reload. `window.__PT` (read-only object) exposes `{version, backend, chat, phase, uid, tripId, role, namespace, started}` for QA.

"Not connected" card: `#ptGate`, Hebrew/RTL, `.card` + `.btn.outline` (copy) + `.btn.green` (retry), mono 2rem code = first 8 hex chars of `auth.uid()` (dashes removed), `min-height/min-width 44px`, max-width 380px with 16px padding. While shown, `html.pt-gate` hides every other body child (no tabs/dashboards). Copy uses `navigator.clipboard` with an `execCommand('copy')` fallback. Retry re-runs step 6 (or the full sequence if there is no client/identity yet).

## 4. window.storage on trip_kv (F02-KV-01)

Same interface, 8 s timeout, throw-on-error. `planner:X` ↔ `X`; other keys as-is.
- `get` → `select value … eq(trip_id) eq(key) maybeSingle()`.
- `set` → `upsert({trip_id, key, value}, {onConflict:'trip_id,key'})`; `updated_at`/`updated_by` never sent.
- `delete` → `delete … eq(trip_id) eq(key) .select('key')`. **Interpretation:** PostgREST/RLS makes a viewer's DELETE match 0 rows instead of returning an error. To honour "an RLS rejection is an error", if 0 rows were deleted the row is re-read; if it still exists, `delete` throws so the existing code queues it. (If it didn't exist, success — same as v2.15.2.)
- `list(prefix)` → `like(escape(map(prefix)) + '%')` within the trip, `\`, `%`, `_` escaped with `\`; keys mapped back by re-adding `planner:` when the prefix had it.
- Viewer upsert → RLS error → thrown → `SS.set` enqueues (existing code). Local copy kept (existing code writes localStorage first).

## 5. Flags

| Flag | Values | Default | Effect |
|---|---|---|---|
| `STORAGE_BACKEND` | `'cloud'` / `'local-only'` | `'cloud'` | `local-only`: no supabase-js, no auth, zero Supabase network, namespace `pt:local:`, app shows "מצב מקומי בלבד". There is no mode pointing at the old project. |
| `CHAT_FEATURE` | `'soon'` / `'on'` | `'soon'` | `soon`: FAB visible, click → toast `הצ'אט יחזור בקרוב`, no request; `window.SUPABASE_URL` is never set, so the main script's `CHAT_ENDPOINT` evaluates to `''`. `on` (F14): sets `window.SUPABASE_URL/ANON_KEY` to plan-travel before the main script runs, so `CHAT_ENDPOINT` = plan-travel `/functions/v1/chat-assistant` (not deployed yet). |
| `APP_VERSION` | `'3.0.0-alpha.2'` | | drives the update card; `vxCheckForUpdate` still fetches `location.pathname` (now this app's own URL). |
| existing v2.15.2 flags | unchanged | | `HOME_LAYOUT`, `ROUTE_LAYOUT`, `JOURNAL_LAYOUT`, `BUDGET_LAYOUT`, `SYNC_MERGE`, `VAULT_IDB`, `APP_UPDATE_CHECK`, `DAY_TODAY`, … |

Both new flags are top-level `const`s in their own small classic `<script>` just before the F02 gate block (`app/index.html`, search `F02-FLAG-01`), visible as globals; flip them there. The gate IIFE only reads them.

## 6. Google Maps (F02-MAPS-01)

The Maps `<script async defer … callback=initMap>` tag is unchanged. Because the main script now starts later, Google may call `initMap` before it exists. A placeholder `window.initMap` sets `__ptGmapsReady`; the main script's `function initMap` then replaces it (global function declaration), and the gate calls the real `initMap()` once if Maps is ready and `map` is still null. `startGoogleMapsWatch` (8 s fallback to schematic) is unchanged. On the not-connected card no map is drawn. If the Google key's HTTP referrer restriction doesn't include `orsela.github.io/Plan_Travel/*`, the app falls back to the schematic map — that is a Cloud Console setting for Or (criterion 14), not a code fix.

## 7. GitHub Pages path assumption

The app is served from **`https://orsela.github.io/Plan_Travel/app/`** (Pages source = repo root of `orsela/Plan_Travel`, files under `/app/`). Everything is relative (`sw.js`, `manifest.json`, icons, `start_url: "."`, `scope: "./"`), so the service worker's scope is `/Plan_Travel/app/` only and never overlaps `/Vietnam_Travel_Planner/`. If Pages is instead configured to serve `/app` as the site root, the app works unchanged at `/Plan_Travel/` (scope `/Plan_Travel/`), still disjoint from the live app.

## 8. Import tool (scripts/import_planner_kv.py)

```
python3 scripts/import_planner_kv.py export.csv  --trip <uuid> -o import.sql [--csv-empty-null]
python3 scripts/import_planner_kv.py export.json --trip <uuid>            > import.sql
python3 scripts/import_planner_kv.py --trip <uuid> --verify-sql             # only the count + budget-total queries
python3 scripts/import_planner_kv.py --self-test                            # synthetic export; set PT_SELFTEST_DSN for a real-Postgres round trip
```
- Input: CSV with header containing `key,value` (extra columns such as `updated_at` ignored; quoted CRLF preserved), or JSON list of `{key,value,…}` (also `{rows:[…]}` or an object with one list member).
- Output: one transaction, a guard that the trip exists, then `insert into public.trip_kv (trip_id,key,value) … on conflict (trip_id,key) do update set value = excluded.value` per row. Keys/values dollar-quoted with a tag that doesn't occur in the text → byte-for-byte.
- Never skips silently: empty key, key > 200 chars, two source keys that collide after stripping `planner:`, or a NUL byte → hard error.
- Every normal run ends with the verification queries (F02-IMPORT-02): appended after `commit;` in the emitted SQL, so running the file prints the trip_kv counts right after the import; with `-o` they are also printed to stdout. The count query is `select <family case> , count(*) from public.trip_kv where trip_id = '<uuid>' group by 1`, joined to the fixed family list so empty families show 0 in the same order as the tool's output.
- stderr: per-family counts (trip, expenses, reservations, journal:\*, checkins, timeline, budget, other) + budget totals (expenses item count, sum of `amount`, budget target). `--verify-sql` prints queries returning the same numbers from `trip_kv`.
- **Interpretation:** families are exact key names after stripping (`trip`, `expenses`, …), `journal:*` = every key starting `journal:`; everything else (catbudgets, photos, personalbudget:*, fxrates, tripname, documents…) is `other`.
- **CSV NULL:** the Supabase dashboard writes NULL as an empty field; CSV can't distinguish it from `''`. Default keeps `''`; `--csv-empty-null` turns empty into NULL. JSON exports keep NULL exactly. A non-string JSON value is re-serialized with a loud warning (byte identity can't be guaranteed then — prefer CSV or a JSON export of the text column).
- Self-test result: JSON + CSV parse, counts, tricky value (Hebrew, quotes, CRLF, `$pt$`, backslash) byte-identical, collision refused; DB round trip on local Postgres 16: 11/11 rows byte-identical, and the verification query's per-family counts equal the tool's counts.

## 9. Migration 0005_f02 (not applied)

**`updated_by` is enforced by the architect's F02-DB-02 trigger `trip_kv_set_updated_by`** in this migration: on every insert/update it sets `updated_by = auth.uid()` for JWT writers (so a client can't forge it); service-role/SQL writes (e.g. the import, `auth.uid()` null) keep the given value. The app never sends `updated_by` either way. Dev did not modify that part.

F02-DB-01 adds `public.trip_kv` and `public.trip_members` to `supabase_realtime`, each only if not already a member (checks `pg_publication_tables`); creates the publication only if missing. No tables/policies/grants touched. Verified on local Postgres 16: runs twice without error, both tables listed once.

## 10. Self-check results (2026-10-04)

**Protected functions** (`scripts/dev/f02_static_check.py`, brace-matched extraction, byte compare vs v2.15.2):

| Item | Bytes | Result |
|---|---|---|
| drawMap | 379 | IDENTICAL |
| drawGoogleMap | 4613 | IDENTICAL |
| drawSchematicMap | 3427 | IDENTICAL |
| fallbackToSchematic | 178 | IDENTICAL |
| SEED_DATA | 9277 | IDENTICAL |
| stayBlock | 517 | IDENTICAL |
| bookingUrl | 309 | IDENTICAL |

Main script: 4041 lines in both; exactly 1 differing line (`APP_VERSION`).
`node --check`: all 5 inline script blocks OK (SW-register, F02 flags, F02 gate, gm_authFailure, main) + `sw.js` OK.
Duplicate ids: v2.15.2 has `hminHotelDocs` ×2 (pre-existing); no new duplicates.
Forbidden strings in `app/`: the old project ref, old publishable key, `vietnam-trip-2026`, `planner_kv`, `localStorage.clear(` → none. (Repo-wide, the old project ref appears only in `docs/F02_spec.md`; the dev scripts build their search pattern by string concatenation.)

**Smoke** (`scripts/dev/f02_smoke.py`, Playwright Chromium 390×844, origin `https://orsela.github.io` via routing, fake supabase-js, all other hosts aborted, SW blocked): **51/51 pass** — unlinked card (texts, 8-hex code = uid, no tabs, 44px targets, no h-scroll, RTL, main script not started); link + retry → app runs on the active trip (pending ignored); writes land in `trip_kv` with trip_id, stripped key, updated_by = uid; list/get mapping and `_` escaping; app-visible keys are only its own; vault in `pt-vault-<trip>`; chat toast with zero requests and empty `CHAT_ENDPOINT`; reload keeps the uid; viewer write rejected → queued under `pt:<trip>:planner:__queue__`, local copy kept; seeded live-app localStorage, `vtp-vault` content and `vtp-*` caches unchanged; zero requests to the old project; second trip doesn't see the first trip's local data or vault; removed member → card; offline start on cached trip; local-only: `pt:local:`, no supabase-js, zero Supabase requests, "מצב מקומי בלבד", no page errors.

Not covered by the dev smoke (left to QA / real device): service-worker behaviour on a real Pages origin, real supabase-js against plan-travel, SYNC-MERGE with two devices, Google Maps referrer.

## 11. Open points / interpretations

1. Head GM-08 block replaced and main `<script>` tag changed — both unavoidable (old credentials; spec §3 step 5). Flagged inline.
2. Two lines of `sw.js` modified (cache delete guard, icon lookup scoped to own cache) — needed so the worker can't read/delete the live app's caches.
3. Viewer delete detection by re-read (§4 above).
4. Device code before sign-in succeeded shows `--------` (no uid exists yet); retry re-runs the full sequence.
5. Manifest CHANGE note is a `_change` JSON member (no comments in JSON).
