# invite-manager · Edge Function (F03)

<!-- CHANGE 2026-10-05 F03-FN-03: new file (no previous version). -->

Super-admin manager invites for Plan_Travel, version **3.0.0-alpha.3**. Contract: `docs/F03_spec.md` §3.
One function, `POST` body `{action, …}`:

| action | auth | input | success (200) | errors |
|---|---|---|---|---|
| `create` | admin JWT | `email`, `draft_name?` (≤80) | `{ok, invite_id, email, draft_name, expires_at}` | 400 `invalid_email` / `invalid_name`, 409 `invite_exists` `{invite_id, draft_name, expires_at, days_left}`, 502 `email_failed` |
| `resend` | admin JWT | `invite_id` | `{ok, invite_id, expires_at, send_count}` | 404 `invite_not_found`, 409 `invite_closed`, 502 `email_failed` |
| `revoke` | admin JWT | `invite_id` | `{ok, invite_id}` (idempotent) | 404 `invite_not_found`, 409 `invite_used` |
| `check` | none | `token` | `{valid:true, draft_name, inviter_name, expires_at}` or `{valid:false}` | — (anything invalid is `{valid:false}`, status 200) |

Any admin action without a valid JWT of a user in `platform_admins` → 403 `not_platform_admin`. Malformed body /
unknown action → 400 `invalid_request`. Unexpected failure → 500 `server_error`.

Behaviour notes
- Tokens: 32 random bytes → base64url (43 chars). Only `token_hash = sha256 hex` is stored. Link = `APP_URL#invite=<token>`.
- `create`: email trimmed + lower-cased. An open (unused, unrevoked) **unexpired** invite for the same email → `invite_exists`,
  nothing sent. An open **expired** one is revoked first. Insert → send email → on send failure the row is deleted.
  A unique-index race (two creates at once) is reported as `invite_exists`.
- `resend`: the email with the NEW token is sent first; the row (new hash, `expires_at = now+7d`, `send_count+1`,
  `last_sent_at = now`, `opened_at = null`) is updated only after the send succeeded, and only if the invite is still open.
  So a failed send leaves the old token/link valid (spec: "keep the old token"). Works for expired invites.
- `check`: does not consume the invite; sets `opened_at` only if it was null. Malformed / unknown / expired / revoked /
  used / replaced tokens all return the identical body `{"valid":false}`.
- `inviter_name` = the caller's most recent active `trip_members.display_name` (fallback `מנהל המערכת`), stored on the row.
- Logs: one JSON line per event with the event name, invite id and error code only. Never tokens, hashes, emails, bodies.
- CORS: `Access-Control-Allow-Origin` is echoed only for `https://orsela.github.io` and `http://localhost[:port]`.
- Email: `"Plan_Travel" <GMAIL_USER>`, subject `הוזמנת לנהל טיול ב-Plan_Travel`, RTL HTML + plain-text part, no images,
  no tracking pixels. SMTP `smtp.gmail.com:465` implicit TLS via `denomailer@1.6.0` (Supabase Edge blocks 25/587).

## Deployment (Or, dashboard)

1. Secrets (Project Settings → Edge Functions → Secrets): `GMAIL_USER` (the sending Gmail address),
   `GMAIL_APP_PASSWORD` (16-char app password, needs 2-Step Verification), `APP_URL` = `https://orsela.github.io/Plan_Travel/app/`
   (optional; that is the default). `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are provided by Supabase.
   If the project has the legacy JWT keys disabled, also set `PT_SERVICE_KEY` to an `sb_secret_…` key (it is preferred when present).
2. Edge Functions → Create function `invite-manager` → paste `index.ts` (single file, no other files needed).
3. **Turn "Verify JWT" OFF** for this function (`check` is called without a session; the function verifies the JWT
   itself for create/resend/revoke). With the CLI: `supabase functions deploy invite-manager --no-verify-jwt`, or in
   `supabase/config.toml`: `[functions.invite-manager]` / `verify_jwt = false`.
4. Migrations `0008_f03.sql` (columns, unique index, `admin_trip_summary`) must be applied first.
5. Quick check from a browser console on the app origin (no secrets involved):
   `fetch('https://zwufpxnioqaweobnvffs.supabase.co/functions/v1/invite-manager',{method:'POST',headers:{'content-type':'application/json'},body:'{"action":"check","token":"x"}'}).then(r=>r.json())`
   → `{valid:false}`.

## Tests

`test.ts` covers spec criteria 1 (function part), 4–11 with a mocked SMTP transport and an in-memory store that enforces
the DB rules (lower-case email, one open invite per email → 23505, unique hash):

```
deno test supabase/functions/invite-manager/test.ts                          # with network
deno test --no-check --import-map=supabase/functions/invite-manager/test_import_map.json \
          supabase/functions/invite-manager/test.ts                          # offline (stubs for the 2 remote imports)
deno check --import-map=supabase/functions/invite-manager/test_import_map.json supabase/functions/invite-manager/index.ts
```

The two remote imports (`npm:@supabase/supabase-js@2.45.4`, `denomailer@1.6.0`) are only used by the production wiring
at the bottom of `index.ts` (`import.meta.main`), so the stubs in `test_stubs/` never change what the tests exercise.
Not covered offline: the real supabase-js query shapes in `supabaseStore()` and the real SMTP send — verified on the
deployed function (spec §6.7).
