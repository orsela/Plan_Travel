# F03 — Super-admin invites a manager · spec

Version target: **3.0.0-alpha.3** (S2). Mockup approved by Or 2026-10-05: canvas https://claude.ai/artifact/FtgwXNmFKoSSrgnuqghbMG, row 4 (artboards `F03_*`).

User value: as the super-admin, Or invites a group manager by email and sees the state of every group (name, managers, destination, dates, member count, status) — never trip content.

Base: `app/` at 3.0.0-alpha.2 (F02). Supabase project `plan-travel` (`zwufpxnioqaweobnvffs`). The live project `vietnam-trip-2026` is never touched.

## 0. Decisions (Or, 2026-10-05)

| # | Decision |
|---|---|
| D1 | Email: Gmail SMTP with an app password. The invite email is sent by the Edge Function itself (secrets `GMAIL_USER`, `GMAIL_APP_PASSWORD`). The same Gmail is configured as Supabase Auth custom SMTP (needed for email linking now and for magic links in F04). |
| D2 | Super-admin identity: Or links an email to the anonymous identity already on his phone (same `auth.uid()`, membership kept). Then one manual SQL row in `platform_admins`. |
| D3 | Admin screen lives inside the app, reached from "עוד" only by a super-admin; it has no trip chrome (no tab bar, chat FAB, FX pill). |
| D4 | The invite link lands on an interim page that validates the token **without consuming it** and says the setup wizard is coming (F04). An invalid link shows one generic message. |
| D5 | Resend = new token (old link dies), expiry reset to 7 days. A second active invite to the same email is refused. A used invite cannot be revoked. |
| D6 | Status is derived (no new status column). List served by a `security definer` RPC, not a view. |
| D7 | List also shows trip dates and member count (approved addition). Invite tracking adds `opened_at` and `send_count` (approved addition). |

## 1. Screens (match the approved artboards)

All Hebrew, RTL, 390px-safe, touch ≥44px, existing UI-kit styles. Flag: `ADMIN_FEATURE='on'|'off'` (top-level, next to `CHAT_FEATURE`). `off` = F02 behavior exactly: no "החשבון שלי" rows, no admin screen, `#invite=` ignored (falls through to normal boot).

1. **"עוד" → section "החשבון שלי"** (`F03_LinkEmail`, `F03_More`), inserted at the top of the existing `#more` panel; the rest of `#more` unchanged.
   - Row "קישור מייל לחשבון" when the user has no email. Opens a bottom sheet: email input, three-step explanation, button "שליחת מייל אימות" → `supabase.auth.updateUser({email},{emailRedirectTo: <app URL>})`. Success → sheet shows "שלחנו מייל ל-<email>. פתחו את הקישור בטלפון הזה." Errors mapped to Hebrew (invalid email, rate limit, email already used by another account, offline).
   - After confirmation (user has `email`): row "מייל מקושר" + the address + badge "מקושר".
   - Row "ניהול מערכת" + badge "סופר-אדמין" **only if** `platform_admins` has the user's row (RLS: own row). Opens the admin screen.
   - Visible to every member (any role). Not shown in `STORAGE_BACKEND='local-only'`.
2. **Admin screen** `#admin` panel (`F03_Admin`): back button to "עוד"; primary button "הזמנת מנהל לקבוצה חדשה"; segmented filter הכל / ממתינות / פעילות / הסתיימו with counts; cards; link "הזמנות שבוטלו · N" toggles revoked invites; privacy footnote. Card kinds:
   - pending invite (dashed): draft name (or "קבוצה ללא שם"), email (LTR), "נשלחה לפני X · בתוקף עוד Y ימים", ⋯ button → actions sheet.
   - expired invite (dashed): "פג ב-dd/mm", buttons "שליחה מחדש" and "הסרה מהרשימה" (= revoke).
   - trip: flag emoji per country (from `trip_countries`, max 3), name, "מנהלים: …", "<countries in Hebrew> · dd/mm–dd/mm/yyyy · N חברים", badge פעילה / הסתיימה.
   - Filter "ממתינות" = pending + expired. Sort: pending/expired first (newest sent first), then active trips by start_date, then ended (newest end first).
   - Offline: shows "אין חיבור — הרשימה תתעדכן כשהרשת תחזור" and disables actions. Not cached.
3. **Invite sheet** (`F03_InviteSheet`): email (required, validated), draft name (optional, ≤80). Send → `invite-manager` `create`. Error `invite_exists` → amber notice with the existing invite's name and days left + button "לשלוח שוב את ההזמנה הקיימת" (→ that invite's actions sheet); submit disabled while the email matches. Success → toast "ההזמנה נשלחה ל-<email>", list refreshes.
4. **Actions sheet** (`F03_InviteActions`): name + badge, email; rows: נשלחה לראשונה, שליחה אחרונה (+ count "פעם אחת" / "N פעמים"), בתוקף עד, הקישור נפתח (date or "עדיין לא"). Actions "שליחה חוזרת" and "ביטול ההזמנה" (confirm dialog before revoke).
5. **Invite email** (`F03_Email`): subject "הוזמנת לנהל טיול ב-Plan_Travel"; from name "Plan_Travel"; RTL HTML + plain-text part; inviter name, draft name, button "פתיחת ההזמנה" → `<APP_URL>#invite=<token>`, expiry date, ignore line. No tracking pixels.
6. **Landing** (`F03_Landing`, `F03_LandingInvalid`): when the URL hash is `#invite=<token>`, before trip boot, render a full-screen page (no trip chrome) and call `invite-manager` `check`. Valid → "ההזמנה שלך אומתה", inviter + draft name, expiry, 4 future steps, disabled button "התחלת הקמה · בקרוב". Invalid / expired / revoked / used / replaced / malformed → one identical generic message. Network error → "אין חיבור. נסו שוב" with retry. The token must not be logged or stored anywhere (no localStorage, no console); clear it from the address bar with `history.replaceState` after reading.

## 2. Database — migration `0008_f03.sql`

- `manager_invites` add: `send_count int not null default 1`, `last_sent_at timestamptz`, `opened_at timestamptz`, `inviter_name text` (≤40).
- Partial unique index: one active invite per email — `unique (email) where used_at is null and revoked_at is null` (expiry is checked in the function; an expired-but-not-revoked invite is revoked automatically when a new invite to the same email is created).
- `public.admin_trip_summary()` → `setof jsonb` (or a table type), `security definer`, `stable`, `set search_path=''`; raises `not_platform_admin` (errcode 42501) unless `is_platform_admin()`. Returns trips (id, name, start_date, end_date, status `active`|`ended` — `ended` when `status='archived'` or `end_date < current_date`, manager display_names of active managers, country codes by sort, active member count) and unused invites (id, email, draft_name, created_at, last_sent_at, send_count, opened_at, expires_at, status `pending`|`expired`|`revoked`). Never returns `trip_kv`, members other than managers' names, or tokens. `grant execute … to authenticated`, revoke from public/anon.
- No new client-writable grants. Advisors must stay clean.
- QA harness `0009_qa_f03_harness.sql`: `qa.f03_run()` in the locked `qa` schema, rolled back, F01 pattern.

## 3. Edge Function `invite-manager` (Deno, service role)

Single function, body `{action, …}`. All actions except `check` require a valid JWT whose user is in `platform_admins` (else 403 `not_platform_admin`). Fixed error codes, translated by the client dictionary.

| action | input | does |
|---|---|---|
| `create` | email, draft_name? | normalize email (trim, lower), validate; if an active unexpired invite exists → 409 `invite_exists` {invite_id, draft_name, expires_at}; an expired unrevoked one is revoked; generate 32 random bytes → base64url token; store SHA-256 hex as `token_hash`; `expires_at = now()+7d`, `last_sent_at=now()`, `send_count=1`, `inviter_name` = caller's most recent active `trip_members.display_name` (fallback "מנהל המערכת"); send email; if sending fails → delete the row, 502 `email_failed`. |
| `resend` | invite_id | only if not used and not revoked (else 409 `invite_closed`); new token+hash, `expires_at=now()+7d`, `send_count+1`, `last_sent_at=now()`, `opened_at=null`; send email; on send failure keep the old token (transaction) and return `email_failed`. Works for expired invites. |
| `revoke` | invite_id | not used (else 409 `invite_used`); set `revoked_at=now()`. Idempotent. |
| `check` | token | **no JWT required** (`verify_jwt=false`); hash; find invite; valid = not used, not revoked, not expired → `{valid:true, draft_name, inviter_name, expires_at}` and set `opened_at` if null; anything else → `{valid:false}` (same shape, same status 200, no reason). Does not consume the invite. |

- Email via SMTP to `smtp.gmail.com:465` (implicit TLS; Supabase Edge blocks 25/587). From `"Plan_Travel" <GMAIL_USER>`. Library pinned by version.
- `APP_URL` secret (default `https://orsela.github.io/Plan_Travel/app/`).
- CORS: allow the app origin `https://orsela.github.io` and `http://localhost:*` only.
- Never log tokens, never return `token_hash`.

## 4. Client wiring

- `PT_ADMIN_ENDPOINT = PT_SUPABASE_URL + '/functions/v1/invite-manager'`; calls carry the session JWT (`Authorization: Bearer`) and `apikey`.
- `isPlatformAdmin` = `select user_id from platform_admins` (own row by RLS) at boot, cached in memory only.
- List = `supabase.rpc('admin_trip_summary')`.
- Country names/flags: a small static map for countries in use (Hebrew name + flag); unknown code → the code itself. (Full `countries.json` comes in F04/F08.)
- Version `APP_VERSION='3.0.0-alpha.3'`, `<title>` likewise; every change marked `CHANGE 2026-10-05 F03-…` stating what changed from alpha.2. Protected functions byte-identical (`drawMap`, `drawGoogleMap`, `drawSchematicMap`, `fallbackToSchematic`, `SEED_DATA`, `stayBlock`, `bookingUrl`). No change to trip boot, storage, sync or existing panels except the inserted "החשבון שלי" section in `#more`.

## 5. Acceptance criteria

Access
1. A non-admin authenticated user (any trip role) calling `admin_trip_summary()` or `invite-manager` create/resend/revoke gets `not_platform_admin`; anon key alone gets nothing.
2. The "ניהול מערכת" row and `#admin` panel never appear for a non-admin, even when navigating to `#admin` directly.
3. The summary never contains trip_kv data, tokens/hashes, or non-manager member names.

Invites
4. `create` stores only a SHA-256 hash; expiry 7 days; sends one email whose link contains a token that hashes to the stored value.
5. Second `create` for the same email (any case/whitespace) while active → `invite_exists` with the existing invite's id/name; no email sent, no row added.
6. `create` for an email whose previous invite is expired → old one revoked, new one created.
7. `resend` → new token; the previous link now checks invalid; `send_count` +1; `expires_at` reset; `opened_at` cleared. Works on expired invites. Refused on used/revoked.
8. `revoke` → link checks invalid immediately; refused on a used invite; repeating is harmless.
9. Email failure on `create` leaves no row; on `resend` leaves the old link valid.

Landing
10. Valid token → valid page with draft name, inviter, expiry; `opened_at` set once; invite still unused.
11. Expired, revoked, replaced, used, malformed and random tokens → byte-identical invalid page and identical function response shape.
12. The token is removed from the address bar and never written to storage or the console.

Admin list
13. Statuses derived correctly: pending, expired, revoked (hidden until toggled), active, ended (by end_date or archived); counts on the filter match.
14. Sorting and card contents as §1.2; dates dd/mm; email LTR.

Email linking
15. Sheet calls `updateUser` with the entered email and the app URL as redirect; success and each error show the mapped Hebrew text. After linking, the same `auth.uid()` keeps its trip membership.

Definition of Done
16. `ADMIN_FEATURE='off'` → behaves exactly as alpha.2 (regression suite of F02 passes unchanged in both flag states, except criteria explicitly about the new rows).
17. 390px: no horizontal scroll, RTL, touch ≥44px on all new UI; trip chrome absent on admin and landing.
18. Protected functions byte-identical; version + CHANGE comments; `node --check` on every script block; advisors clean; `qa.f03_run()` all pass, nothing left behind.

## 6. Deployment steps (Or, one at a time, after code is ready)

1. Gmail: create an app password (2-Step Verification required) for the sending account.
2. Supabase Auth → SMTP: enable custom SMTP (smtp.gmail.com, 465, the account + app password, sender name Plan_Travel). Auth → URL configuration: Site URL and Redirect URLs = `https://orsela.github.io/Plan_Travel/app/`.
3. SQL Editor: run `0008_f03.sql`, then `0009_qa_f03_harness.sql`. Claude verifies by reading.
4. Edge Functions: create `invite-manager` from the repo file, set `verify_jwt` off (the function checks the JWT itself for admin actions), secrets `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `APP_URL`.
5. Phone: open "עוד" → link email → confirm from the mail on the same phone.
6. SQL Editor: `insert into public.platform_admins(user_id) select id from auth.users where email = '<Or's email>';` Claude verifies.
7. Phone: "ניהול מערכת" appears; send a test invite to a second address of Or's; open the link; resend; revoke.

## 7. Test strategy

- Client: Playwright 390×844 with the F02 fake supabase-js extended with `rpc('admin_trip_summary')`, `auth.updateUser`, `from('platform_admins')`, and a fake `invite-manager` endpoint (route interception). Both flag states.
- Function: Deno unit tests with a mocked SMTP transport and a local Postgres or mocked client covering criteria 4–11.
- DB: `qa.f03_run()` for criteria 1, 3, 5 (unique index), 13.
- Or's phone after deployment (§6.7).
