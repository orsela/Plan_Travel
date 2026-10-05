# F03 · invite-manager — black-box cases (criteria 1, 4–11)

Version 1.0.0 · CHANGE 2026-10-05 F03-QA: first version (no previous version).

Written from `docs/F03_spec.md` §3/§5 only (the function source was not read). Automated in `tests/f03/function_test.ts`
(ids FN-xx below). The client-side counterpart over a fake of the same semantics is `tests/f03/test_f03.py`.

**Conventions.** `POST /functions/v1/invite-manager`, JSON body `{action, …}`. "admin" = JWT of a user with a
`platform_admins` row. "member" = JWT of a non-admin (any trip role or none). Error body is assumed to be
`{"error":"<code>", …}` (spec: "fixed error codes"); if the dev notes name another key, adjust `errCode()` in
function_test.ts. `T0` = time of the call. `H(x)` = SHA-256 hex of x. Mail is captured through a test seam
(`setMailer`, see function_test.ts header).

## Access (C1)

| id | caller | body | expected status | expected body | side effects |
|---|---|---|---|---|---|
| FN-01a | member (manager of a trip) | create {email} | 403 | `error=not_platform_admin` | no row, no mail |
| FN-01b | member (editor / viewer / no trip) | create / resend / revoke an existing id | 403 | `error=not_platform_admin` | target row unchanged |
| FN-02a | no Authorization | create | 401 or 403 | no `invite_id`, no token | none |
| FN-02b | `Bearer <anon key>` | create | 401 or 403 | same | none |
| FN-02c | `Bearer not.a.jwt` | create | 401 or 403 | same | none |
| FN-02d | none | check {token} | 200 | `check` works without a JWT (`verify_jwt=false`) | — |

## create (C4, C5, C6, C9)

| id | precondition | body | expected status | expected body | side effects |
|---|---|---|---|---|---|
| FN-10 | — | `{action:create, email:"x@d", draft_name:"יפן 2027"}` | 200 | no token, no `token_hash` | 1 row: `token_hash` = 64 hex = H(token in mail); raw token not stored; token = 43-char base64url (32 bytes); `expires_at` ≈ T0+7d; `send_count=1`; `last_sent_at` set; `opened_at/used_at/revoked_at` null; `inviter_name` = caller's most recent active `trip_members.display_name` (fallback "מנהל המערכת"); exactly 1 mail |
| FN-11 | — | create | 200 | — | mail: subject "הוזמנת לנהל טיול ב-Plan_Travel"; from name "Plan_Travel"; HTML with `dir=rtl` + plain-text part; link `<APP_URL>#invite=<token>`; button "פתיחת ההזמנה"; inviter + draft name; expiry dd/mm/yyyy; no `<img>` (no tracking pixel) |
| FN-12a | — | email `"  X@D.CO  "` | 200 | — | stored `x@d.co` |
| FN-12b | — | email `""`, `no-at-sign`, `a@`, `@b.c`, `a b@c.d` | 4xx | — | no row, no mail |
| FN-13 | — | draft_name of 81 chars | 4xx (or stored ≤ 80) | — | never stored > 80 |
| FN-20 | active unexpired invite for x@d | create email `x@d` / `X@D` / `" x@d "` | 409 | `error=invite_exists`, `invite_id`=existing id, `draft_name`=existing name, `expires_at` | no row added, no mail; existing link still valid |
| FN-21 | invite for x@d with `expires_at < now`, not revoked | create x@d | 200 | — | old row `revoked_at` set; new row; old link → `{valid:false}`; new link valid |
| FN-22 | previous invite for x@d revoked / used | create x@d | 200 | — | new row |
| FN-50 | SMTP throws | create y@d | 502 | `error=email_failed` | **no row left**; a later create for y@d succeeds |

## resend (C7, C9)

| id | precondition | body | expected status | expected body | side effects |
|---|---|---|---|---|---|
| FN-30 | pending invite, opened, 2 days left | `{action:resend, invite_id}` | 200 | no token/hash | 1 mail with a **new** token; `token_hash`=H(new); `send_count` +1; `expires_at` ≈ T0+7d; `last_sent_at` updated; `opened_at` = null; old link `{valid:false}`; new link valid |
| FN-31 | invite expired (not revoked) | resend | 200 | — | new link valid |
| FN-32a | invite used | resend | 409 | `error=invite_closed` | no mail, `send_count` unchanged |
| FN-32b | invite revoked | resend | 409 | `error=invite_closed` | same |
| FN-51 | SMTP throws | resend | 502 (any non-2xx) | `error=email_failed` | `token_hash`, `send_count`, `expires_at` unchanged; **old link still valid** |

## revoke (C8)

| id | precondition | body | expected status | expected body | side effects |
|---|---|---|---|---|---|
| FN-40a | pending | `{action:revoke, invite_id}` | 200 | — | `revoked_at` set; link `{valid:false}` immediately |
| FN-40b | already revoked | revoke again | 200 | — | `revoked_at` unchanged (idempotent) |
| FN-41 | used | revoke | 409 | `error=invite_used` | `revoked_at` stays null |

## check (C10, C11)

| id | token | expected status | expected body (exact) | side effects |
|---|---|---|---|---|
| FN-60a | valid | 200 | keys exactly `valid, draft_name, inviter_name, expires_at`; `valid:true` | `opened_at` set (first time only); `used_at` stays null |
| FN-60b | same valid token again | 200 | same | `opened_at` unchanged |
| FN-61a | expired | 200 | `{"valid":false}` | `opened_at` not set |
| FN-61b | revoked | 200 | `{"valid":false}` | — |
| FN-61c | replaced (resend issued a new one) | 200 | `{"valid":false}` | — |
| FN-61d | used | 200 | `{"valid":false}` | — |
| FN-61e | malformed `%%bad<>`, `""`, missing, number, 10 000 chars | 200 | `{"valid":false}` | — |
| FN-61f | random 43-char base64url | 200 | `{"valid":false}` | — |
| FN-61g | all of the above | — | **byte-identical** response bodies (no reason leaked) | — |

## Other (§3)

| id | case | expected |
|---|---|---|
| FN-70 | admin: unknown action, `{}`, resend without id, revoke with a non-uuid, resend of a random uuid | 4xx, never 5xx |
| FN-71a | preflight from `https://orsela.github.io`, `http://localhost:5173`, `http://localhost:8080` | 2xx, `Access-Control-Allow-Origin` = that origin |
| FN-71b | preflight from `https://evil.example`, `https://orsela.github.io.evil.example`, `http://localhost.evil.example` | origin not echoed, not `*` |
| FN-72 (review) | function source / logs | no `console.log` of token or `token_hash`; SMTP library pinned by version; `smtp.gmail.com:465`; From `"Plan_Travel" <GMAIL_USER>` — checked by reading the merged code + function logs after §6.7 |
