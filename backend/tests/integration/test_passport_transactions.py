"""Opt-in disposable PostgreSQL test: CLAWPAY_LOCAL_POSTGRES_TEST=1 uv run pytest this_file.

Uses local PostgreSQL binaries and an isolated Unix socket. Never uses project DB credentials.
"""
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAWPAY_LOCAL_POSTGRES_TEST") != "1"
    or not all(shutil.which(name) for name in ("initdb", "pg_ctl", "psql")),
    reason="Opt-in local PostgreSQL binaries required",
)
ROOT = Path(__file__).resolve().parents[2]
USER = "11111111-1111-1111-1111-111111111111"
DEVICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
GRANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_grant_revocation_and_expiry_serialize_with_order_creation():
    with tempfile.TemporaryDirectory(prefix="clawpay-passport-pg-", dir="/tmp") as directory:
        data = str(Path(directory) / "data")
        command = ["psql", "-X", "-h", directory, "-p", "55484", "-U", "postgres", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", "-qAt"]

        def sql(statement, *, application="passport-test"):
            result = subprocess.run(command, input=statement, text=True, capture_output=True,
                                    env={**os.environ, "PGAPPNAME": application}, timeout=15)
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        def create(expected=GRANT, required=False, cart="a"):
            expected = f"'{expected}'::uuid" if expected else "null::uuid"
            return f"""select reason from public.create_order_with_passport(
                '{USER}','{DEVICE}','grocer','MYR',clock_timestamp()+interval '5 minutes','review',
                repeat('{cart}',64),'[{{"sku":"MILK","name":"Milk","qty":1,"unit_price_minor":690}}]',
                690,{expected},{str(required).lower()});"""

        def blocked_decision(*, expire=False):
            holder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            try:
                holder.stdin.write(f"""begin;
                do $$ begin perform 1 from public.agent_delegations where id='{GRANT}' for update; end $$;
                {'' if expire else f"update public.agent_delegations set revoked_at=clock_timestamp() where id='{GRANT}';"}
                \\echo LOCKED
                """)
                holder.stdin.flush()
                assert select.select([holder.stdout], [], [], 5)[0], "No lock barrier"
                assert holder.stdout.readline().strip() == "LOCKED"
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(sql, create(), application="passport-blocked-order")
                    deadline = time.monotonic() + 5
                    while sql("select count(*) from pg_stat_activity where application_name='passport-blocked-order' and wait_event_type='Lock';") != "1":
                        assert time.monotonic() < deadline, "Order did not wait for the grant lock"
                        time.sleep(0.05)
                    if expire:
                        while sql(f"select expires_at <= clock_timestamp() from public.agent_delegations where id='{GRANT}';") != "t":
                            assert time.monotonic() < deadline, "Grant did not expire"
                            time.sleep(0.05)
                    holder.stdin.write("commit;\n\\q\n")
                    holder.stdin.flush()
                    holder.wait(timeout=5)
                    return result.result(timeout=10)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=5)

        started = False
        try:
            subprocess.run(["initdb", "-D", data, "-A", "trust", "-U", "postgres", "--no-locale", "-E", "UTF8"],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(["pg_ctl", "-D", data, "-l", directory+"/server.log", "-o",
                            f"-k {directory} -p 55484 -c listen_addresses=''", "start", "-w"],
                           check=True, capture_output=True, timeout=30)
            started = True
            sql("""create role anon; create role authenticated; create role service_role bypassrls;
                create schema auth; grant usage on schema auth to public;
                create function auth.uid() returns uuid language sql stable as $$
                  select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
                create table auth.users(id uuid primary key,email text,aud text,role text,
                  raw_user_meta_data jsonb default '{}');""")
            for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
                sql(migration.read_text())
            sql(f"""insert into auth.users(id) values ('{USER}');
                insert into public.devices(id,user_id,device_name,public_key,key_algorithm,status)
                values ('{DEVICE}','{USER}','review','review-key','ecdsa-p256-sha256','active');
                select * from public.create_agent_delegation('{GRANT}','{USER}','{DEVICE}',
                  array['grocer'],5000,'MYR',clock_timestamp()+interval '1 hour',
                  '0123456789abcdef','{{}}',repeat('A',86));""")
            assert blocked_decision() == "delegation_revoked"
            assert sql("select count(*) from public.orders;") == "0"
            sql(f"update public.agent_delegations set revoked_at=null,expires_at=clock_timestamp()+interval '2 seconds' where id='{GRANT}';")
            assert blocked_decision(expire=True) == "delegation_expired"
            assert sql("select count(*) from public.orders;") == "0"
            sql(f"update public.agent_delegations set expires_at=clock_timestamp()+interval '1 hour' where id='{GRANT}';")
            assert sql(create(expected=None)) == "delegation_changed"
            assert sql(create(required=True)) == "created"
            assert sql("select count(*) from public.orders;") == "1"
            assert sql("select has_function_privilege('authenticated','public.create_order_with_passport(uuid,uuid,text,character,timestamptz,text,text,jsonb,bigint,uuid,boolean)','EXECUTE');") == "f"
        finally:
            if started:
                subprocess.run(["pg_ctl", "-D", data, "stop", "-m", "fast", "-w"],
                               check=True, capture_output=True, timeout=15)
