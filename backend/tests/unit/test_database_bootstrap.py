"""Offline safety, completeness and regeneration checks for SQL Editor scripts."""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.merchant_agents import channel3
from app.merchant_agents.catalogs import CATALOGS
from scripts import generate_database_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[2]


def test_generated_sql_is_current():
    for name, expected in bootstrap.render_files().items():
        assert (ROOT / name).read_text(encoding="utf-8") == expected


def test_generator_check_works_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_database_bootstrap.py"), "--check"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_schema_includes_every_migration_in_order_and_records_source_hashes():
    schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
    positions = []
    for path, source in bootstrap.read_sources():
        marker = f"-- === BEGIN MIGRATION {path.name} ==="
        end = f"-- === END MIGRATION {path.name} ==="
        assert schema.count(marker) == schema.count(end) == 1
        positions.append(schema.index(marker))
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        assert f"{path.name}  sha256-lf={digest}" in schema
        embedded = schema.split(marker + "\n", 1)[1].split("\n" + end, 1)[0]
        # Data is intentionally separated; all DDL, privileges and function bodies remain canonical.
        expected = bootstrap.MERCHANT_SEED.sub(
            "-- Merchant data is intentionally applied separately by seed.sql.", source
        ).rstrip()
        assert embedded == expected
    assert positions == sorted(positions)


def test_preflight_happens_before_cleanup_and_preserves_system_data():
    schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
    cleanup_start = schema.index("drop trigger if exists on_auth_user_created on auth.users;")
    assert schema.index("$bootstrap_preflight$;") < cleanup_start
    assert schema.index("ClawPay tables already exist:") < cleanup_start
    assert schema.index("Supabase Auth is missing:") < cleanup_start
    assert schema.index("belongs to another integration") < cleanup_start
    # No destructive table/schema reset, and no managed-auth mutations.
    assert not re.search(r"(?im)^\s*(?:drop\s+(?:table|schema)|truncate\s|delete\s+from|insert\s+into\s+auth\.|update\s+auth\.)", schema)
    assert not re.search(r"(?im)^drop (?:function|trigger).*cascade", schema)
    assert not re.search(r"(?im)^(?:insert|update|delete).*supabase_migrations", schema)
    assert schema.index("begin;") < cleanup_start < schema.index("create table public.user_profiles")
    assert schema.index("$bootstrap_verify$;") < schema.rindex("commit;")
    assert "notify pgrst, 'reload schema';" in schema


def test_schema_covers_all_tables_with_rls_and_explicit_backend_access():
    schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
    tables = bootstrap.TABLE.findall(schema)
    assert len(tables) == len(set(tables)) == 18
    for table in tables:
        assert f"alter table public.{table} enable row level security;" in schema
        assert f"'{table}'" in schema[:schema.index("$bootstrap_preflight$;")]
    grant = schema[schema.rindex("grant all on table\n"):schema.rindex("-- Assert")]
    for table in tables:
        assert f"public.{table}" in grant
    assert "to service_role;" in grant
    assert "has_function_privilege('anon'" in schema
    assert "has_function_privilege('authenticated'" in schema


def test_cleanup_covers_all_known_signatures_without_cascade():
    schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
    prefix = schema[:schema.index("-- === BEGIN MIGRATION")]
    for _, source in bootstrap.read_sources():
        for name, types in bootstrap.FUNCTION_ACL.findall(source):
            types = ", ".join(part.strip() for part in types.split(",") if part.strip())
            assert f"drop function if exists public.{name}({types});" in prefix
    # Both historical reserve-spending signatures must be cleared before replay.
    assert "public.reserve_spending(uuid, uuid, bigint, char, bigint, text);" in prefix
    assert "public.reserve_spending(uuid, uuid, bigint, char, bigint, date);" in prefix


def test_seed_is_idempotent_reference_data_only_and_restores_auth_profiles():
    seed = (ROOT / "supabase/seed.sql").read_text(encoding="utf-8")
    inserts = re.findall(r"(?im)^insert into ([a-z_.]+)", seed)
    assert inserts == ["public.user_profiles", "public.merchants"]
    assert "from auth.users u" in seed
    assert "on conflict (id) do nothing;" in seed
    assert "on conflict (merchant_code) do nothing;" in seed
    codes = set(re.findall(r"^  \('([^']+)',", seed, re.MULTILINE))
    assert codes == set(CATALOGS) | {channel3.MERCHANT_CODE}
    assert not re.search(r"(?im)^(?:update|delete|truncate|drop)\s", seed)
    schema = (ROOT / "supabase/schema.sql").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^insert into public.merchants", schema)


def test_generator_fails_closed_for_unexpected_sources(monkeypatch):
    monkeypatch.setattr(bootstrap, "read_sources", lambda: [])
    with pytest.raises(ValueError, match="No source migrations"):
        bootstrap.render_files()
    monkeypatch.setattr(bootstrap, "read_sources", lambda: [(Path("test.sql"), "create table public.same (id int);\ncreate table public.same (id int);")])
    with pytest.raises(ValueError, match="Duplicate table definitions"):
        bootstrap.render_files()



def test_generated_sql_is_portable_across_source_line_endings(tmp_path, monkeypatch):
    # Git normalizes SQL on checkout; generated manifests must remain stable.
    source = "\n".join(text for _, text in bootstrap.read_sources())
    path = tmp_path / "001_combined.sql"
    monkeypatch.setattr(bootstrap, "MIGRATIONS", tmp_path)
    path.write_text(source, encoding="utf-8", newline="\n")
    lf_files = bootstrap.render_files()
    path.write_text(source, encoding="utf-8", newline="\r\n")
    assert bootstrap.render_files() == lf_files
