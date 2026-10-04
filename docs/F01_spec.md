# F01 — Trip isolation (schema + RLS) · spec

Version target: **3.0.0-alpha.1** (S1). Approved by Or 2026-10-04 (decision 1 = A: join code readable by managers; tests run on the `plan-travel` project inside rolled-back transactions).

User value: members of one trip can never read or change another trip's data, even with the public (publishable/anon) key.

Supabase project: `plan-travel` (Postgres 17). All objects in schema `public` unless stated.

## 1. Tables

All tables: RLS **enabled**. `anon` role gets **no** privileges on any of them (revoke all). `authenticated` gets only the privileges its policies need.

| Table | Columns (type, constraints) |
|---|---|
| `platform_admins` | `user_id uuid PK → auth.users(id) on delete cascade`, `created_at timestamptz default now()` |
| `trips` | `id uuid PK default gen_random_uuid()`, `name text not null` (1–80 chars), `start_date date`, `end_date date` (end ≥ start when both set), `home_currency char(3) not null default 'ILS'` (uppercase A–Z), `default_lang text not null default 'he'` (format `^[a-z]{2}(-[A-Z]{2})?$`), `default_role text not null default 'editor'` (`editor`\|`viewer`), `status text not null default 'active'` (`active`\|`archived`), `created_by uuid → auth.users(id) on delete set null`, `created_at timestamptz default now()`, `updated_at timestamptz default now()` (auto-updated on UPDATE) |
| `trip_join_codes` | `trip_id uuid PK → trips(id) on delete cascade`, `code text not null unique` (8 chars from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`), `version int not null default 1`, `rotated_at timestamptz default now()` |
| `trip_countries` | `trip_id uuid → trips on delete cascade`, `country_code char(2)` (uppercase A–Z), `sort int not null default 0`, PK `(trip_id, country_code)` |
| `trip_currencies` | `trip_id uuid → trips on delete cascade`, `currency_code char(3)` (uppercase A–Z), `kind text not null` (`base`\|`destination`\|`added`), `sort int not null default 0`, PK `(trip_id, currency_code)` |
| `trip_members` | `id uuid PK default gen_random_uuid()`, `trip_id uuid not null → trips on delete cascade`, `user_id uuid not null → auth.users(id) on delete cascade`, `display_name text not null` (1–40 chars), `role text not null` (`manager`\|`editor`\|`viewer`), `status text not null default 'pending'` (`pending`\|`active`\|`removed`), `joined_at timestamptz default now()`, `approved_at timestamptz`, `last_seen_at timestamptz`, UNIQUE `(trip_id, user_id)` |
| `manager_invites` | `id uuid PK`, `email text not null` (stored lowercase), `token_hash text not null unique`, `draft_name text`, `invited_by uuid → auth.users on delete set null`, `expires_at timestamptz not null`, `used_at timestamptz`, `revoked_at timestamptz`, `trip_id uuid → trips on delete set null`, `created_at timestamptz default now()` |
| `relink_tokens` | `id uuid PK`, `member_id uuid not null → trip_members on delete cascade`, `token_hash text not null unique`, `expires_at timestamptz not null`, `used_at timestamptz`, `created_by uuid → auth.users on delete set null`, `created_at timestamptz default now()` |
| `trip_kv` | `trip_id uuid not null → trips on delete cascade`, `key text not null` (1–200 chars), `value text`, `updated_at timestamptz not null default now()` (auto-updated), `updated_by uuid default auth.uid()`, PK `(trip_id, key)` |

Indexes: every FK column that is not already the leading PK column (e.g. `trip_members(user_id)`, `relink_tokens(member_id)`, `manager_invites(trip_id)`), plus `trip_members(trip_id, status)`.

## 2. Database-level rules (triggers)

1. **Max 10 currencies per trip** — INSERT into `trip_currencies` that would make an 11th row for that trip fails with an error whose message contains `currency_limit`.
2. **Base currencies are locked** — DELETE of a `trip_currencies` row with `kind='base'`, or UPDATE that changes its `kind` away from `base`, fails (message contains `base_currency_locked`) — **unless** the parent trip is being deleted (cascade must still work).
3. **At least one active manager** — any UPDATE or DELETE on `trip_members` that would leave a trip that has ≥1 active manager with **zero** rows where `role='manager' and status='active'` fails (message contains `last_manager`) — **unless** the parent trip is being deleted (cascade must work).
4. `updated_at` auto-maintained on `trips` and `trip_kv`.

## 3. Helper functions

All: `language sql` or `plpgsql`, `security definer`, `stable`, `set search_path = ''`, fully-qualified names inside, `revoke execute from public, anon`, `grant execute to authenticated`.

- `public.is_platform_admin() returns boolean` — current user (`auth.uid()`) is in `platform_admins`.
- `public.trip_role(p_trip uuid) returns text` — the current user's `role` in that trip if `status='active'`, else NULL.
- `public.my_trip_ids(p_roles text[] default array['manager','editor','viewer']) returns setof uuid` — trip ids where the current user is an **active** member with one of `p_roles`.

## 4. RLS policies (role `authenticated` only; nothing for `anon`)

Write policies with `(select auth.uid())` and `trip_id in (select public.my_trip_ids(...))` patterns.

| Table | SELECT | INSERT / UPDATE / DELETE |
|---|---|---|
| `trip_kv` | active member of the trip (any role) | active `manager` or `editor` of the trip (INSERT and UPDATE `with check` the same; key moves to another trip are impossible) |
| `trips` | active member | UPDATE: active `manager`. INSERT/DELETE: none (Edge Functions with service role only) |
| `trip_countries`, `trip_currencies` | active member | active `manager` (insert, update, delete) |
| `trip_members` | (a) active member sees all member rows of that trip; (b) any user sees their own rows (incl. pending/removed) | none (Edge Functions only) |
| `trip_join_codes` | active `manager` only | none (Edge Functions only) |
| `manager_invites`, `relink_tokens` | none | none |
| `platform_admins` | own row only | none |

`service_role` bypasses RLS as usual (used by Edge Functions later).

Out of F01 scope: `alert_dismissals` (F11), `usage_log` (F15), `admin_trip_summary` view (F03), Realtime publication (F02), Storage bucket (F12).

## 5. Acceptance criteria (what QA verifies)

Setup: two trips A and B; users: `mA` (manager A), `eA` (editor A), `vA` (viewer A), `pA` (pending in A), `mB` (manager B), `out` (no membership), `adm` (platform admin, not a member of A or B). Each trip has kv rows, countries, currencies (incl. ILS/USD/EUR as base), a join code.

1. `eA` reads `trip_kv` → only trip A rows; 0 rows of B.
2. `eA` inserts / updates / deletes a trip A kv row → succeeds.
3. `eA` inserts a kv row for trip B → rejected.
4. `eA` updates a trip B kv row → 0 rows affected; deleting it → 0 rows affected.
5. `vA` reads trip A kv → allowed; `vA` insert/update kv in A → rejected / 0 rows.
6. `pA` reads anything of trip A (trips, kv, countries, currencies, other members) → 0 rows; `pA` sees own `trip_members` row.
7. `out` sees 0 rows in every table (except nothing of its own).
8. `anon` role: any SELECT on any table → permission denied or 0 rows; any INSERT → denied.
9. `trip_join_codes`: `mA` sees A's code only; `eA` and `vA` see 0 rows; `mB` does not see A's code.
10. `trips`: `mA` can UPDATE A's name; `eA` UPDATE → 0 rows; `mA` UPDATE of B → 0 rows; `mA` INSERT a trip → rejected; `mA` DELETE → rejected/0 rows.
11. `trip_members`: `mA` cannot INSERT/UPDATE/DELETE (no write policy) → rejected/0 rows; `eA` sees all A members, none of B.
12. `trip_currencies`: `mA` adds an `added` currency → ok; `eA` adds → rejected.
13. 11th currency for a trip (as postgres/service) → fails with `currency_limit`.
14. Deleting a `base` currency row (as postgres) → fails with `base_currency_locked`.
15. Removing / demoting the only active manager (as postgres) → fails with `last_manager`; with a second active manager it succeeds.
16. Deleting trip A (as postgres) → cascades cleanly (no trigger error), all its child rows gone.
17. `is_platform_admin()` true for `adm`, false for `mA`; `adm` sees 0 rows of trip A kv (admin ≠ member).
18. `trip_role(A)` returns `manager` for mA, `editor` for eA, NULL for pA / out / mB.
19. `anon` cannot execute the helper functions.
20. `platform_admins`: `adm` sees own row; `mA` sees 0 rows.
