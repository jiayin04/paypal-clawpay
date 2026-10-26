"""Generate a standalone post-drop schema and safe seed from canonical migrations.

This module never connects to a database. Run from any working directory:
    python scripts/generate_database_bootstrap.py
    python scripts/generate_database_bootstrap.py --check

Do not hand-edit the generated files or rewrite historical migrations.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
FUNCTION_ACL = re.compile(
    r"revoke\s+execute\s+on\s+function\s+public\.(\w+)\(([^)]*)\)", re.IGNORECASE
)
TABLE = re.compile(r"(?m)^create table public\.(\w+)\s*\(", re.IGNORECASE)
MERCHANT_SEED = re.compile(
    r"(?m)^insert into public\.merchants \(merchant_code, display_name, status\) values\n"
    r".*?^on conflict \(merchant_code\) do nothing;", re.DOTALL | re.MULTILINE
)


def read_sources() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(MIGRATIONS.glob("*.sql"))]


def render_files() -> dict[str, str]:
    sources = read_sources()
    if not sources:
        raise ValueError("No source migrations found")
    tables = [name for _, source in sources for name in TABLE.findall(source)]
    if len(tables) != len(set(tables)):
        raise ValueError("Duplicate table definitions need a reviewed bootstrap update")
    signatures: dict[tuple[str, str], None] = {}
    merchant_seeds = []
    for _, source in sources:
        for name, types in FUNCTION_ACL.findall(source):
            signatures[name, ", ".join(part.strip() for part in types.split(",") if part.strip())] = None
        merchant_seeds.extend(MERCHANT_SEED.findall(source))
    if len(merchant_seeds) != 1:
        raise ValueError("Expected exactly one canonical merchant seed")

    table_names = ", ".join(f"'{name}'" for name in tables)
    table_targets = ",\n  ".join(f"public.{name}" for name in tables)
    function_names = ", ".join(f"'{name}'" for name in sorted({name for name, _ in signatures}))
    manifest = "\n".join(
        f"--   {path.name}  sha256-lf={hashlib.sha256(source.encode("utf-8")).hexdigest()}"
        for path, source in sources
    )
    header = f"""-- GENERATED: python scripts/generate_database_bootstrap.py
-- ClawPay standalone schema: all current backend phases, in one transaction.
-- APPLY THIS FILE FIRST in the Supabase SQL Editor as postgres, then seed.sql.
-- Intended for a project whose ClawPay public tables have ALREADY BEEN DROPPED.
-- Supabase Auth (auth.users) and platform roles must remain intact.
-- Refuses to overwrite existing application tables. Does not DROP any table,
-- use CASCADE, reset Auth/Storage, or modify Supabase migration history.
-- Leftover known ClawPay functions and the ClawPay signup trigger are replaced.
-- Keep the backend stopped until BOTH schema.sql and seed.sql succeed.
-- Run this INSTEAD OF replaying the bundled migrations separately.
-- Before future CLI pushes, reconcile migration history with the versions below.
-- Canonical migration sources (original files are not modified):
{manifest}

begin;
set local lock_timeout = '10s';
set local statement_timeout = '120s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('clawpay.bootstrap', 0));

-- Validate platform prerequisites and refuse a partial/nonempty application schema.
do $bootstrap_preflight$
declare
  v_name text;
  v_existing text[] := array[]::text[];
begin
  if pg_catalog.to_regclass('auth.users') is null then
    raise exception 'Supabase Auth is missing: auth.users must exist. This script rebuilds only ClawPay, not Supabase system schemas.'
      using errcode = '55000';
  end if;
  foreach v_name in array array['postgres', 'anon', 'authenticated', 'service_role'] loop
    if not exists (select 1 from pg_catalog.pg_roles where rolname = v_name) then
      raise exception 'Missing Supabase role %. Use a normal Supabase project and run as postgres.', v_name
        using errcode = '55000';
    end if;
  end loop;
  foreach v_name in array array[{table_names}] loop
    if pg_catalog.to_regclass('public.' || v_name) is not null then
      v_existing := pg_catalog.array_append(v_existing, v_name);
    end if;
  end loop;
  if pg_catalog.cardinality(v_existing) > 0 then
    raise exception 'ClawPay tables already exist: %. No tables were changed. Use migrations for an existing database.',
      pg_catalog.array_to_string(v_existing, ', ')
      using errcode = '55000';
  end if;
  -- Do not remove another integration's identically named signup trigger.
  if exists (
    select 1 from pg_catalog.pg_trigger t
    join pg_catalog.pg_proc p on p.oid = t.tgfoid
    join pg_catalog.pg_namespace n on n.oid = p.pronamespace
    where t.tgrelid = pg_catalog.to_regclass('auth.users')
      and t.tgname = 'on_auth_user_created'
      and not (n.nspname = 'public' and p.proname = 'handle_new_user')
  ) then
    raise exception 'on_auth_user_created belongs to another integration; review that trigger before rebuilding ClawPay.'
      using errcode = '55000';
  end if;
end;
$bootstrap_preflight$;

create schema if not exists public;
grant usage on schema public to anon, authenticated, service_role;

-- Dropping application tables can leave this trigger on the surviving Auth table.
drop trigger if exists on_auth_user_created on auth.users;

-- Restrictive drops of known app signatures only (never CASCADE). A foreign
-- dependency causes the transaction to fail rather than deleting that dependency.
"""
    cleanup = "\n".join(
        f"drop function if exists public.{name}({types});" for name, types in reversed(signatures)
    )
    sections = []
    for path, source in sources:
        source = MERCHANT_SEED.sub("-- Merchant data is intentionally applied separately by seed.sql.", source)
        sections.append(f"\n-- === BEGIN MIGRATION {path.name} ===\n{source.rstrip()}\n-- === END MIGRATION {path.name} ===\n")
    footer = f"""
-- Make backend access explicit even if the public schema/default ACLs were
-- recreated. BYPASSRLS alone does not grant table privileges.
grant all on table
  {table_targets}
to service_role;

-- Assert the important bootstrap postconditions before committing.
do $bootstrap_verify$
declare
  v_name text;
begin
  foreach v_name in array array[{table_names}] loop
    if not exists (
      select 1 from pg_catalog.pg_class c
      join pg_catalog.pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relname = v_name and c.relkind = 'r' and c.relrowsecurity
    ) then
      raise exception 'Bootstrap verification failed: table % missing or RLS disabled.', v_name;
    end if;
  end loop;
  if exists (
    select 1 from pg_catalog.pg_proc p
    join pg_catalog.pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ({function_names})
      and (pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE')
           or pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE'))
  ) then
    raise exception 'Bootstrap verification failed: application functions must not be directly executable by client roles.';
  end if;
end;
$bootstrap_verify$;

notify pgrst, 'reload schema';
commit;

select 'ClawPay schema created. Run supabase/seed.sql next.' as result;
"""
    schema = header + cleanup + "\n" + "\n".join(sections) + footer
    seed = f"""-- GENERATED: python scripts/generate_database_bootstrap.py
-- APPLY SECOND, after schema.sql succeeds (or after the normal migrations).
-- Safe to rerun: existing profiles and merchant rows are preserved.
-- No fake Auth users, devices, tokens, PayPal wallets, orders or approvals.
-- Mock product catalogs live in app/merchant_agents/catalogs.py, not SQL tables.

begin;
set local lock_timeout = '10s';
set local statement_timeout = '120s';
select pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('clawpay.bootstrap', 0));

do $seed_preflight$
begin
  if pg_catalog.to_regclass('auth.users') is null
     or pg_catalog.to_regclass('public.user_profiles') is null
     or pg_catalog.to_regclass('public.merchants') is null then
    raise exception 'Apply schema.sql first. Supabase Auth and ClawPay tables must exist.' using errcode = '55000';
  end if;
end;
$seed_preflight$;

-- Existing Auth accounts survive a drop of public tables. Recreate their
-- profile rows so owner FKs and authenticated device registration can work.
-- New signups are handled by the schema's on_auth_user_created trigger.
insert into public.user_profiles (id, display_name)
select u.id, u.raw_user_meta_data ->> 'display_name'
from auth.users u
on conflict (id) do nothing;

-- These codes must match the deterministic A2A merchant agents.
{merchant_seeds[0]}

commit;

select merchant_code, display_name, status from public.merchants order by merchant_code;
select
  (select count(*) from auth.users) as auth_users,
  (select count(*) from public.user_profiles) as user_profiles;
"""
    return {"supabase/schema.sql": schema, "supabase/seed.sql": seed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if generated SQL is out of date; write nothing")
    args = parser.parse_args()
    stale = []
    for name, content in render_files().items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"Generated {name}")
    if stale:
        print("Regenerate SQL: " + ", ".join(stale))
        return 1
    if args.check:
        print("Generated schema.sql and seed.sql match the migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
