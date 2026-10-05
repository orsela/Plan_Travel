-- CHANGE 2026-10-05 F03-DEVDB-01: new file (no previous version). Dev-only, NEVER run on Supabase.
-- Minimal stand-in for the parts of a Supabase database that migrations 0001–0009 rely on, so they can be
-- applied and the qa.*_run() harnesses executed on a plain local PostgreSQL 16/17:
--   roles anon / authenticated / service_role, schema auth with auth.users and auth.uid() (same definition shape
--   as Supabase: request.jwt.claim.sub, falling back to request.jwt.claims->>'sub'), usage on public.
-- Used by scripts/dev/f03_db_check.py.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin noinherit; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin noinherit; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin noinherit bypassrls; end if;
end $$;

create schema if not exists auth;
create table if not exists auth.users (
  id           uuid primary key,
  aud          text,
  role         text,
  email        text,
  instance_id  uuid,
  is_anonymous boolean default false,
  created_at   timestamptz default now(),
  updated_at   timestamptz default now()
);

create or replace function auth.uid() returns uuid language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

grant usage on schema auth   to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;
