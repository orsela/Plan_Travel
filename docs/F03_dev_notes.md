# F03 · dev notes — super-admin invites a manager (v3.0.0-alpha.3)

<!-- CHANGE 2026-10-05 F03-DOCS-01: new file (no previous version). -->

Contract: `docs/F03_spec.md` (mockup approved 2026-10-05, artboards `F03_*`). Base: `app/` at 3.0.0-alpha.2 (F02).
Nothing was deployed or run against Supabase/Gmail (the sandbox cannot reach them). All checks below are local.

## 1. What changed (file / area → why)

| File / area | CHANGE ID | What changed from 3.0.0-alpha.2 | Spec |
|---|---|---|---|
| `supabase/migrations/0008_f03.sql` | F03-DB-01 | new: `manager_invites` + `send_count int not null default 1`, `last_sent_at`, `opened_at`, `inviter_name` (check 1–40 chars), `send_count >= 1` check; partial unique index `manager_invites_one_active_per_email (email) where used_at is null and revoked_at is null`; RPC `public.admin_trip_summary()` (security definer, stable, `search_path=''`, raises `not_platform_admin`/42501, execute to authenticated + service_role only). Idempotent (`if not exists`, `create or replace`, guarded constraints). | §2, D5–D7 |
| `supabase/migrations/0009_qa_f03_harness.sql` | F03-QA-01 | new: `qa.f03_run()` (49 scenarios) in the locked `qa` schema, F01/F02 pattern, always rolled back | §2, §7 |
| `supabase/functions/invite-manager/index.ts` | F03-FN-01 | new Edge Function (create / resend / revoke / check) | §3 |
| `supabase/functions/invite-manager/test.ts`, `test_import_map.json`, `test_stubs/*` | F03-FN-02 | Deno unit tests (16) with mocked SMTP + in-memory store; offline import map | §7 |
| `supabase/functions/invite-manager/README.md` | F03-FN-03 | API table, behaviour, deployment, how to test | §6 |
| `app/index.html` `<title>` | F03-VER-02 | v3.0.0-alpha.2 → v3.0.0-alpha.3 | §4 |
| `app/index.html` flags script | F03-FLAG-01 | new top-level `const ADMIN_FEATURE='on'` (next to `CHAT_FEATURE`) | §1 |
| `app/index.html` head, new style + script block between the flags script and the F02 gate block | F03-UI-01 (+ LAND-01, ACCT-01, LINK-01, ADMIN-01, INV-01, ACT-01) | all F03 UI (see §2 below). Returns on its first line when `ADMIN_FEATURE!=='on'` or `STORAGE_BACKEND!=='cloud'`. | §1, §4 |
| `app/index.html` F02 gate, `PT_VERSION` | F03-VER-03 | `'3.0.0-alpha.2'` → `'3.0.0-alpha.3'` (`window.__PT.version`). **Changed line in an existing function body** (see §5). | §4 |
| `app/index.html` F02 gate, just before `boot().catch(…)` | F03-GATE-01 | **new lines only**: `PT_ADMIN_ENDPOINT`, bridge `window.__ptF03.attach({…})`, and `if (takeOverBoot()) return` for `#invite=` | §1.6, §4 |
| `app/index.html` main script, `APP_VERSION` | F03-VER-01 | `'3.0.0-alpha.2'` → `'3.0.0-alpha.3'` (only changed line in the main script; 4042 lines before and after) | §4 |
| `scripts/dev/supabase_local_shim.sql`, `scripts/dev/f03_db_check.py` | F03-DEVDB-01 | dev: apply all migrations to a local Postgres and run qa.f01/f02/f03 | DoD |
| `scripts/dev/f03_smoke.py` | F03-DEVSMOKE-01 | dev: Playwright 390×844 smoke, extends the F02 QA fake | DoD, §7 |
| `app/sw.js` | — | **unchanged** (see §6) | |

Protected functions (`drawMap`, `drawGoogleMap`, `drawSchematicMap`, `fallbackToSchematic`, `SEED_DATA`, `stayBlock`,
`bookingUrl`) are byte-identical (F02 suite's `protected:*` checks, run against the v2.15.2 baseline, pass in both flag states).
Lines removed/changed in `app/index.html` vs alpha.2: exactly 3 — `<title>`, `PT_VERSION`, `APP_VERSION`. Everything else is additive.

## 2. Client (app/index.html)

Architecture: one new classic `<script>` (IIFE) in `<head>`, **before** the F02 gate, so it can read and clear the hash
before anything else. It exposes only a frozen `window.__ptF03 = {version, attach, takeOverBoot}`. The F02 gate gives it
read-only access to its client via `attach({endpoint, key, client:()=>sb, uid:()=>state.uid, withTimeout, timeoutMs})`
(F03-GATE-01) — so F03 uses the same supabase-js client, session and storage adapter as F02; no second client.

- **Start hook**: a `MutationObserver` on `<html class>` notices when the gate's `start()` ran (`__PT.started`), then
  inserts "החשבון שלי" into `#more` and reads the account. No existing function is wrapped or patched.
- **"החשבון שלי"** (F03-ACCT-01): inserted as the first child of `#more`'s content wrapper (`#ptAccount`), followed by a
  "הטיול" section label above the existing rows (as in the artboard); the existing rows are untouched.
  Rows: "קישור מייל לחשבון" (no email; shows "ממתין לאישור: <new_email>" after sending) or "מייל מקושר" + address + "מקושר";
  "ניהול מערכת" + "סופר-אדמין" only if `from('platform_admins').select('user_id').eq('user_id', uid).maybeSingle()` returns a
  row (RLS: own row). `isPlatformAdmin` is in memory only, read at start and on `online`. Email comes from
  `auth.getSession()` (instant) then `auth.getUser()` (server truth, e.g. after confirmation on another tab).
- **Link-email sheet** (F03-LINK-01): `auth.updateUser({email}, {emailRedirectTo: <app URL>})` where app URL =
  `location.origin + location.pathname` without `index.html` (= `https://orsela.github.io/Plan_Travel/app/` in production).
  Errors → Hebrew: invalid email (client-side and `email_address_invalid`/`validation_failed`), rate limit (429 /
  `over_email_send_rate_limit`), email used by another account (`email_exists`/`user_already_exists`/"already registered"),
  offline (`navigator.onLine` false / fetch error), anything else → "לא הצלחנו לשלוח את המייל. נסו שוב."
- **Auth redirect**: the gate has `detectSessionInUrl:false`, so after the user clicks the confirmation link the app opens
  with `#access_token=…` (or `#error=…`). The F03 script removes that fragment from the address bar before boot (never
  stored or logged) and shows a toast ("המייל קושר לחשבון ✓" / link expired). The existing session (same `auth.uid()`,
  same membership) keeps working; `getUser()` then reports the new email. *Interpretation*: we do not swap the session to
  the one in the URL — it is the same user, and keeping the gate's session avoids touching F02 boot.
- **Admin screen** (F03-ADMIN-01): `<section id="admin">` appended to `<body>` only when a platform admin opens it,
  removed on close. It is **not** a `.panel` (so `switchPanel` never shows it and a non-admin can't reach it): a
  full-screen fixed layer plus `html.pt-admin-open`, which hides every other body child (tab bar, chat FAB, FX pill,
  panels) — "no trip chrome". Opening pushes `#admin` to history; back button / browser back close it and return to עוד.
  Typing `#admin` as a non-admin does nothing and the hash is removed (criterion 2). Data: `rpc('admin_trip_summary')`,
  memory only, never cached; a `not_platform_admin` answer closes the screen and hides the row.
  Cards, filter (הכל / ממתינות / פעילות / הסתיימו with counts; ממתינות = pending + expired), sorting (pending/expired
  newest-sent first → active by start_date → ended newest end first), revoked toggle "הזמנות שבוטלו · N", privacy footnote,
  offline notice "אין חיבור — הרשימה תתעדכן כשהרשת תחזור" with all actions disabled, reload on `online`.
  *Interpretation*: the "הכל" count = pending + expired + trips (revoked are hidden until toggled, as in the artboard's 5).
- **Invite sheet** (F03-INV-01) and **actions sheet** (F03-ACT-01): own bottom-sheet layer `#ptSheetLayer` (z-index
  above the admin layer; the app's `.modal` system is not reused because `closeModal` calls `switchPanel`).
  `invite_exists` → amber notice with name + days left, submit disabled while the email still matches, button
  "לשלוח שוב את ההזמנה הקיימת" opens that invite's actions sheet. Revoke asks for confirmation inside the sheet
  ("ביטול ההזמנה" / "חזרה") — *interpretation* of "confirm dialog": an in-sheet confirm instead of `window.confirm`
  (RTL-safe, ≥44px). Expired cards: "שליחה מחדש" = resend, "הסרה מהרשימה" = revoke (no confirm, as in the artboard).
- **Landing** (F03-LAND-01): when the URL hash is `#invite=<token>` the token is copied into the closure and the address
  bar cleaned with `history.replaceState` immediately (before the gate). The gate bridge then calls `takeOverBoot()`,
  which renders `#ptInvite` (full screen, `html.pt-landing` hides everything else) and **returns from the gate before
  `boot()`** — no anonymous sign-in, no trip lookup, main script never started. `check` is sent without a JWT
  (`apikey` only). All invalid outcomes (expired, revoked, replaced, used, malformed, random) render one constant markup
  string; a token that is not 43 base64url chars is rejected without a request (same page). Network error / timeout /
  non-200 → "אין חיבור. נסו שוב" (or "לא הצלחנו לבדוק את ההזמנה. נסו שוב" for a server error) with retry.
  The token is never written to storage or the console.
- Country names/flags: a static Hebrew map (~70 codes) + regional-indicator flag emoji; unknown code → the code itself.
- Error dictionary: `ERR_HE` in the F03 script maps every function error code to Hebrew.

## 3. Flags

| Flag | Values | Default | Effect |
|---|---|---|---|
| `ADMIN_FEATURE` | `'on'` / `'off'` | `'on'` | `off` = alpha.2 exactly: the F03 script returns on its first line, `window.__ptF03` is undefined, the gate bridge is a no-op, no rows in עוד, no admin screen, `#invite=` is ignored (normal boot; the hash stays). Styles exist but match nothing. |
| `STORAGE_BACKEND` | unchanged | `'cloud'` | `local-only` also disables all of F03 (spec §1.1: "not shown in local-only"). |
| `APP_VERSION` / `PT_VERSION` | `'3.0.0-alpha.3'` | | |

## 4. Edge Function

See `supabase/functions/invite-manager/README.md`. Logic is in `makeHandler(deps)` (DB, mailer, clock, random injected);
production wiring (`supabaseStore`, `gmailSender`, `Deno.serve`) runs only under `import.meta.main`.
- **Deviation (resend atomicity)**: spec says "on send failure keep the old token (transaction)". Implemented as
  *send first with the new token, then update the row* (conditional on still open). A failed send changes nothing
  (criterion 9). Residual edge: if the send succeeds but the DB update then fails, the emailed link is invalid while the
  old one still works; the function returns `server_error` and the admin can resend. A true DB transaction around SMTP is
  not possible from the function with supabase-js.
- `create` inserts first and deletes the row if the send fails (spec), so the partial unique index also serializes two
  concurrent creates for the same email (the loser gets `invite_exists`).
- `check` for invalid tokens returns 200 `{valid:false}`; on an internal error it returns 500 `server_error` (the client
  shows the network-error state, not the invalid page) — that keeps "invalid" meaning invalid.
- `days_left` is added to the `invite_exists` payload (convenience; the client recomputes from `expires_at`).
- Pinned: `npm:@supabase/supabase-js@2.45.4`, `https://deno.land/x/denomailer@1.6.0/mod.ts`.
  **Not verified here**: the sandbox's egress policy blocks deno.land, esm.sh and registry.npmjs.org (403), so the real
  libraries were never downloaded; `deno check` ran against typed stubs with the same call shapes. If the dashboard
  bundler rejects `npm:` specifiers, switch the import to `https://esm.sh/@supabase/supabase-js@2.45.4`.

## 5. Existing code modified (unavoidable)

1. F02 gate IIFE: `const PT_VERSION='3.0.0-alpha.2'` → `'3.0.0-alpha.3'` (version string only).
2. F02 gate IIFE: new statements inserted before `boot().catch(…)` (F03-GATE-01). No existing statement changed; with the
   flag off they do nothing. This is the only way to (a) give F03 the gate's private client and (b) stop trip boot for
   `#invite=` "before trip boot" without wrapping F02 functions.
3. Main script: `APP_VERSION` value (same pattern as F02-VER-01).
No other existing function body, panel or style was changed. `#more` is changed at runtime only by inserting `#ptAccount`.

## 6. Service worker

`app/sw.js` is **not changed**. Navigations are network-first (the new index.html is fetched on every online start and
replaces the cached shell); the invite-manager POSTs are not intercepted (`req.method !== "GET"` → network). No cache
version bump is needed, and leaving the file byte-identical keeps the F02 SW checks untouched.

## 7. Self-check results (2026-10-05, sandbox)

- `node --check`: all 6 inline script blocks of `app/index.html` (SW-register, flags, **F03**, F02 gate, gm_authFailure, main) OK; `sw.js` OK.
- DB (`scripts/dev/f03_db_check.py` on local PostgreSQL 16 + `supabase_local_shim.sql`): migrations 0001–0004, 0006–0009
  applied, 0008 applied twice (idempotent) → `qa.f01_run()` **109/109**, `qa.f02_run()` **37/37**, `qa.f03_run()` **49/49**,
  leftovers row (#58) 0/0. Real Supabase (Postgres 17, real `auth` schema, advisors) not run.
- Edge Function: `deno test` (offline import map) **16/16**; `deno check` (index.ts + test.ts against typed stubs) clean;
  `deno lint` clean.
- F02 QA suite (`tests/f02`, Playwright, v2.15.2 baseline from the project library, md5 `931019a2…`), with its
  `VERSION` constant set to `3.0.0-alpha.3` (only change, in a scratch copy): **ADMIN_FEATURE='on': 39 passed, 1 skipped;
  ADMIN_FEATURE='off': 39 passed, 1 skipped** (the skip is C16, DB-only, same as F02). Unpatched it would fail only
  `app_version`/`title_version` (they pin alpha.2).
- F03 smoke (`scripts/dev/f03_smoke.py`, 390×844): **91/91** — non-admin rows and `#admin` refusal; link-email sheet
  (invalid / rate-limit / already-used / offline errors, `updateUser` args, success text); admin list (counts, sort,
  card texts, LTR email, revoked toggle, filters, privacy note, offline notice + disabled actions, reload on online,
  no storage caching, back + browser back); invite sheet (JWT + apikey + normalized body, toast, refresh,
  `invite_exists` notice + disabled submit + jump to actions, `email_failed`); actions (rows, resend, confirm/cancel/revoke,
  "הסרה מהרשימה"); landing (valid page, token gone from URL/storage/console, no trip chrome, no sign-in, `opened_at`,
  6 invalid kinds byte-identical, network error + retry); flag off (no rows, `#admin` inert, `#invite=` → normal boot,
  no function request); local-only (no rows); no horizontal scroll, RTL and ≥44px targets on every new screen; no page errors.
  Screenshots were checked visually against the artboards.

Not verified (needs the real stack / Or's phone, spec §6.7): real Gmail SMTP send and rendering in Gmail; real
`updateUser` email-change flow and redirect; Supabase advisors; Edge Function deploy with `verify_jwt` off; real
supabase-js PostgREST shapes inside `supabaseStore()`.

## 8. Deploy (Or, in order — spec §6)

1. Gmail app password. 2. Auth → SMTP (smtp.gmail.com, 465, sender name Plan_Travel); Auth → URL configuration: Site URL
   and Redirect URLs = `https://orsela.github.io/Plan_Travel/app/`.
3. SQL Editor: `0008_f03.sql`, then `0009_qa_f03_harness.sql`; then `select * from qa.f03_run() order by id;` (expect 49 × true).
4. Edge Function `invite-manager` from `supabase/functions/invite-manager/index.ts`, **Verify JWT off**, secrets
   `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `APP_URL`. 5. Push `app/` (Pages). 6. Phone: עוד → קישור מייל → confirm on the same phone.
7. SQL: `insert into public.platform_admins(user_id) select id from auth.users where email = '<Or>';` 8. Phone test (§6.7).

## 9. Open points

1. Resend ordering (send-then-update) instead of a DB transaction — see §4.
2. "Confirm dialog" for revoke is in-sheet, not `window.confirm`.
3. `#admin` is a body-level layer, not a `.panel` inside the panel system (keeps it out of `switchPanel` and away from non-admins).
4. Remote Deno imports could not be fetched in the sandbox; first real deploy is the first time they are resolved.
5. The F02 QA suite pins `VERSION = "3.0.0-alpha.2"`; QA will need to bump it to `3.0.0-alpha.3` (not changed in the repo, since it is QA's file).
