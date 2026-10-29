"""Static checks on the SQL migrations (no database is available to the tests)."""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "supabase" / "migrations"
PGTAP = ROOT / "supabase" / "tests" / "database"
INITIAL = "20261024133807_initial_schema.sql"
INITIAL_SHA256 = "803b86eada20a362aa8e442776896f0ad624f5ff1ced84013ebd36148640157c"
PHASE2 = ["20261025090000_phase2_hardening.sql", "20261025090100_phase2_devices_orders_a2a.sql"]

_FUNC_RE = re.compile(r"create (?:or replace )?function public\.(\w+)\s*\(", re.I)
_TABLE_RE = re.compile(r"create table public\.(\w+)\s*\(", re.I)


def _new_migrations() -> list[Path]:
    return sorted(p for p in MIGRATIONS.glob("*.sql") if p.name != INITIAL)


def test_initial_migration_unchanged() -> None:
    digest = hashlib.sha256((MIGRATIONS / INITIAL).read_bytes()).hexdigest()
    assert digest == INITIAL_SHA256


def test_phase2_migrations_exist_and_sort_after_initial() -> None:
    names = [p.name for p in _new_migrations()]
    for name in PHASE2:
        assert name in names
    assert all(name > INITIAL for name in names)


@pytest.mark.parametrize("path", _new_migrations(), ids=lambda p: p.name)
def test_every_new_function_is_locked_down(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    functions = set(_FUNC_RE.findall(sql))
    if path.name.endswith("phase4_realtime.sql"):
        assert not functions and not _TABLE_RE.findall(sql)
        assert "pg_publication_tables" in sql and "and not exists" in sql
        return
    if path.name.endswith("channel3_merchant.sql"):
        # Data-only: one idempotent merchant row, no DDL or privilege changes.
        assert not functions and not _TABLE_RE.findall(sql)
        assert not re.search(r"(?im)^\s*(?:alter|grant|revoke|drop|create|update|delete)\s", sql)
        assert "('channel3', 'Channel3 Marketplace', 'active')" in sql
        assert "on conflict (merchant_code) do nothing;" in sql
        return
    assert functions, "expected at least one function"
    for name in functions:
        body = re.search(
            rf"create (?:or replace )?function public\.{name}\s*\(.*?\$\$;", sql, re.I | re.S
        )
        assert body, name
        assert "set search_path = ''" in body.group(0), f"{name}: search_path"
        assert "security definer" not in body.group(0).lower(), f"{name}: must be invoker"
        assert re.search(
            rf"revoke execute on function public\.{name}\([^;]*\)\s+from public, anon, authenticated;",
            sql,
        ), f"{name}: missing revoke"
        assert re.search(
            rf"grant execute on function public\.{name}\([^;]*\)\s+to service_role;", sql
        ), f"{name}: missing grant"


@pytest.mark.parametrize("path", _new_migrations(), ids=lambda p: p.name)
def test_every_new_table_has_rls_and_revoke(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    for name in _TABLE_RE.findall(sql):
        assert f"alter table public.{name} enable row level security;" in sql, name
        assert f"revoke all on public.{name} from anon, authenticated;" in sql, name


def test_hardening_migration_content() -> None:
    sql = (MIGRATIONS / PHASE2[0]).read_text(encoding="utf-8")
    for kind in ("tables", "sequences", "functions"):
        assert re.search(
            rf"alter default privileges for role postgres in schema public revoke all on {kind}\s+from anon, authenticated;",
            sql,
        )
    # No schema-less (global) default-privilege revoke.
    assert "revoke execute on functions from public" not in sql
    assert "drop function public.reserve_spending(uuid, uuid, bigint, char, bigint, date);" in sql
    assert "(now() at time zone p_timezone)::date" in sql
    assert "pg_timezone_names" in sql
    assert "grant select (id, conversation_id, order_id, correlation_id, event_type, summary, created_at)" in sql
    assert "create trigger orders_enforce_status_transition" in sql


def test_transition_table_matches_python_mirror() -> None:
    from app.domain.order_state import ALLOWED_TRANSITIONS

    sql = (MIGRATIONS / PHASE2[0]).read_text(encoding="utf-8")
    sql_table = {
        src: set(re.findall(r"'(\w+)'", targets))
        for src, targets in re.findall(r"when '(\w+)' then p_to in \(([^)]*)\)", sql)
    }
    expected = {k: set(v) for k, v in ALLOWED_TRANSITIONS.items() if v}
    assert sql_table == expected


def test_devices_migration_content() -> None:
    sql = (MIGRATIONS / PHASE2[1]).read_text(encoding="utf-8")
    assert "drop constraint devices_user_public_key_key" in sql
    assert "devices_active_public_key_key" in sql and "where status <> 'revoked'" in sql
    assert "revoke select on public.devices from authenticated;" in sql
    granted = re.search(r"grant select \(([^)]*)\)\s+on public\.devices to authenticated", sql)
    assert granted and "token_hash" not in granted.group(1)
    a2a = re.search(r"grant select \(([^)]*)\)\s+on public\.a2a_messages to authenticated", sql)
    assert a2a and "payload" not in a2a.group(1)
    assert "('grocer', 'Grocer Demo', 'active')" in sql
    assert "hashtextextended(p_device_id::text || ':' || p_cart_sha256, 0)" in sql
    assert "interval '60 seconds'" in sql


@pytest.mark.parametrize("name", ["schema_security.test.sql", "phase2_4.test.sql"])
def test_pgtap_plan_matches_assertion_count(name: str) -> None:
    sql = (PGTAP / name).read_text(encoding="utf-8")
    planned = int(re.search(r"select plan\((\d+)\);", sql).group(1))
    count = len(re.findall(r"(?m)^select (?:is|ok|results_eq|throws_ok|lives_ok|isnt)\(", sql))
    assert planned == count
    assert "begin;" in sql and sql.rstrip().endswith("rollback;")


def test_pgtap_uses_new_reserve_spending_signature() -> None:
    sql = (PGTAP / "schema_security.test.sql").read_text(encoding="utf-8")
    assert not re.search(r"50000, '\d{4}-\d{2}-\d{2}'\)", sql)
    assert "50000, 'Asia/Kuala_Lumpur')" in sql
    assert "'test-grocer'" in sql
    assert "'ed25519'" not in sql


def test_phase3_migration_content() -> None:
    p3_file = "20261025090200_phase3_approvals_payments.sql"
    names = [p.name for p in _new_migrations()]
    assert p3_file in names
    sql = (MIGRATIONS / p3_file).read_text(encoding="utf-8")
    assert "create table public.approval_challenges" in sql
    assert "create unique index payment_attempts_one_per_order" in sql
    assert "create unique index payment_methods_one_active_per_user" in sql
    assert "grant select (payer_email_masked) on public.payment_methods to authenticated;" in sql
    for fn in [
        "issue_approval_challenge",
        "record_gadget_denial",
        "accept_gadget_approval",
        "mark_attempt_sent",
        "mark_capture_sent",
        "apply_payment_result",
        "claim_reconcilable_attempts",
        "claim_reconcilable_attempt",
        "mark_attempt_exhausted",
        "activate_payment_method",
        "revoke_payment_method",
        "find_payment_method_by_state",
    ]:
        assert f"create or replace function public.{fn}" in sql



def test_phase4_realtime_matches_design():
    sql = (MIGRATIONS / "20261025090300_phase4_realtime.sql").read_text()
    # Golden SQL is self-contained: a fresh clone has no ignored agent notes.
    expected = """do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (select 1 from pg_publication_tables
                     where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'orders') then
    alter publication supabase_realtime add table public.orders;
  end if;
end $$;"""
    assert sql.strip() == expected


def test_phase3_expiry_sweep_preserves_lock_order():
    sql = (MIGRATIONS / '20261025090200_phase3_approvals_payments.sql').read_text(encoding='utf-8')
    sweep = sql[sql.index('create or replace function public.expire_stale_orders'):]
    assert sweep.index('public.devices') < sweep.index('for update skip locked')
    assert 'for share skip locked' in sweep and 'order by o.id' in sweep
