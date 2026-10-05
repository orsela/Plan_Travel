# F03 · DB cases the `qa.f03_run()` harness must contain (criteria 1, 3, 5, 13)

Version 1.0.0 · CHANGE 2026-10-05 F03-QA: first version (no previous version).

Independent checklist written from `docs/F03_spec.md` §2/§5 only, to compare against the dev harness
`0009_qa_f03_harness.sql`. Same pattern as F01/F02 (`qa.f02_as/count/dml/val` in the locked `qa` schema,
impersonation through `request.jwt.claims` + `set local role`, all data in a sub-transaction that is always rolled
back). After the run: `select count(*)` of every table touched must equal the count before (nothing left behind).

**Fixture** (inside the rolled-back block): users A (platform admin, manager of T1), M (manager of T2, not admin),
E (editor of T2), V (viewer of T2), P (pending in T2), R (removed manager of T2), O (outsider, no membership).
Trips: T1 active (end_date ≥ today), T2 active with countries TH,VN (sort 1,0), T3 ended (end_date < today),
T4 `status='archived'` with end_date in the future, T5 with 4 countries. `trip_kv` rows with a marker value
`'QA-SECRET'` in every trip. Invites: I1 pending, I2 expired (expires_at < now, not revoked), I3 revoked,
I4 used, I5 pending with opened_at set and send_count 3.

## Schema (0008)

| # | Case | Expected |
|---|---|---|
| S1 | `manager_invites` has `send_count int not null default 1`, `last_sent_at timestamptz`, `opened_at timestamptz`, `inviter_name text` | columns exist with these types / default / nullability |
| S2 | `inviter_name` of 41 characters | rejected (check ≤ 40) |
| S3 | partial unique index on `manager_invites(email) where used_at is null and revoked_at is null` | exists in `pg_indexes` with that predicate |
| S4 | `admin_trip_summary()` is `security definer`, `stable`, `search_path=''` | `pg_proc.prosecdef`, `provolatile='s'`, `proconfig` contains `search_path=""` |
| S5 | execute grants on `admin_trip_summary()` | authenticated: yes; anon: no; public: no |
| S6 | no new client-writable grants | `manager_invites`: no privileges for anon/authenticated (unchanged from F01); no new INSERT/UPDATE/DELETE grant to anon/authenticated on any table vs. F01/F02 |
| S7 | no `token`/`token_hash` column exposed by any view/function returned to authenticated | `admin_trip_summary` output keys checked in A5 |

## C1 · access

| # | Caller | Call | Expected |
|---|---|---|---|
| A1 | M (manager, not admin) | `select public.admin_trip_summary()` | error `42501`, message contains `not_platform_admin` |
| A2 | E, V, P, R, O (each) | same | `42501 not_platform_admin` |
| A3 | anon role, no `sub` | same | permission denied (no execute) — 0 rows, error |
| A4 | A (admin) | same | rows returned |
| A4b | A after its `platform_admins` row is deleted (same tx) | same | `42501 not_platform_admin` |
| A4c | authenticated M | `select * from platform_admins` | 0 rows (own-row RLS); A sees exactly its own 1 row |
| A4d | authenticated M / anon | `select * from manager_invites` | permission denied (no grant) |

## C3 · content of the summary

| # | Case | Expected |
|---|---|---|
| A5 | union of all keys of all returned jsonb rows | no `token`, `token_hash`, `invited_by`, `trip_kv`, `value`, `key`, `user_id`, `email` on trip rows; invite rows carry only id, email, draft_name, created_at, last_sent_at, send_count, opened_at, expires_at, status (+ a kind/type discriminator) |
| A6 | `summary::text` | does not contain `'QA-SECRET'`, nor any `token_hash` value |
| A7 | manager names of T2 | exactly M's display_name — not E, V, P (pending), R (removed manager) |
| A8 | member count of T2 | active members only (M, E, V = 3) — P and R not counted |
| A9 | country codes of T2 | `["VN","TH"]` (ordered by `sort`) |
| A10 | used invite I4 | not returned |

## C5 · one active invite per email

| # | Case (service role) | Expected |
|---|---|---|
| U1 | insert a second row with I1's email, `used_at`/`revoked_at` null | `23505` unique violation |
| U2 | same, email in different case | `23514` (`manager_invites_email_lower`) — the function normalizes before insert |
| U3 | insert a row with I3's (revoked) email | ok |
| U4 | insert a row with I4's (used) email | ok |
| U5 | revoke I2 (expired), then insert a new row with I2's email | ok (this is the function's "expired → revoke → create" path) |
| U6 | insert with I2's email **without** revoking I2 first | `23505` (expiry is not part of the index; the function must revoke) |

## C13 · derived statuses

| # | Row | Expected `status` |
|---|---|---|
| D1 | I1 (future expiry, not used/revoked) | `pending` |
| D2 | I2 (expires_at < now, not revoked) | `expired` |
| D3 | I3 (revoked_at set, also when expired) | `revoked` |
| D4 | I5 | `pending`; `opened_at` and `send_count=3` passed through |
| D5 | T1 active, end_date ≥ current_date | `active` |
| D6 | T3 end_date < current_date (status 'active') | `ended` |
| D7 | T4 status 'archived', future end_date | `ended` |
| D8 | trip with end_date = current_date | `active` (ended only when `end_date < current_date`) |
| D9 | trip with null end_date | `active` |
| D10 | counts: pending+expired / active / ended / revoked from the summary | equal the fixture (2 / 2+ / 2 / 1) — the client filter counts are computed from these |

## Hygiene

| # | Case | Expected |
|---|---|---|
| H1 | after `qa.f03_run()` | row counts of auth.users, trips, trip_members, trip_kv, trip_countries, platform_admins, manager_invites unchanged |
| H2 | `qa` schema | no privileges for public/anon/authenticated; functions `search_path=''` |
| H3 | Supabase advisors after 0008 | no new security/performance findings vs. alpha.2 |
