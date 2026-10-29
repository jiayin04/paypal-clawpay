-- GENERATED: python scripts/generate_database_bootstrap.py
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

-- These codes must match the A2A merchant agents (mock catalogs and Channel3).
insert into public.merchants (merchant_code, display_name, status) values
  ('grocer', 'Grocer Demo', 'active'),
  ('electronics', 'Electronics Demo', 'active'),
  ('channel3', 'Channel3 Marketplace', 'active')
on conflict (merchant_code) do nothing;

commit;

select merchant_code, display_name, status from public.merchants order by merchant_code;
select
  (select count(*) from auth.users) as auth_users,
  (select count(*) from public.user_profiles) as user_profiles;
