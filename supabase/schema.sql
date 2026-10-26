-- GENERATED: python scripts/generate_database_bootstrap.py
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
--   20261024133807_initial_schema.sql  sha256-lf=a11249c9596dae355abda987a589f77b4fb6ade39e976fd7a3145e3bce425cc0
--   20261025090000_phase2_hardening.sql  sha256-lf=09aa35cdc5eef57a4fd527592a3bde07b35ee0c3fc04dfe37e1df3e0e87a2844
--   20261025090100_phase2_devices_orders_a2a.sql  sha256-lf=5382def57feac02eff21ce8f6f347ef045dc3453b4bd431ed02726700ff11333
--   20261025090200_phase3_approvals_payments.sql  sha256-lf=2505f5bcc58e093c10951ae91fd50e95e47775252ece215ab1056af743dd92ac
--   20261025090300_phase4_realtime.sql  sha256-lf=5886d077e86f2dc4208fa14c1241bfd539c7cc79e518a697ff5ea2a50f8d01de

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
  foreach v_name in array array['user_profiles', 'devices', 'payment_methods', 'merchants', 'conversations', 'orders', 'order_items', 'mandates', 'approvals', 'spending_records', 'payment_attempts', 'conversation_events', 'audit_logs', 'webhook_events', 'a2a_messages', 'approval_challenges'] loop
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
drop function if exists public.find_payment_method_by_state(text);
drop function if exists public.revoke_payment_method(uuid, uuid);
drop function if exists public.activate_payment_method(uuid, text, text, text);
drop function if exists public.mark_attempt_exhausted(uuid);
drop function if exists public.claim_reconcilable_attempt(uuid, integer);
drop function if exists public.claim_reconcilable_attempts(integer, integer);
drop function if exists public.apply_payment_result(uuid, text, text, text, text, text, text);
drop function if exists public.mark_capture_sent(uuid);
drop function if exists public.mark_attempt_sent(uuid);
drop function if exists public.accept_gadget_approval(uuid, uuid, uuid, text, text, char, bigint, text, bigint);
drop function if exists public.record_gadget_denial(uuid, uuid, uuid, text, text, integer, text, text);
drop function if exists public.issue_approval_challenge(uuid, uuid, uuid, integer, text, text, text, bigint, char, timestamptz);
drop function if exists public.spending_summary(uuid, text);
drop function if exists public.expire_stale_orders(integer);
drop function if exists public.expire_order(uuid);
drop function if exists public.cancel_order(uuid, uuid, text, text);
drop function if exists public.create_order_from_quote(uuid, uuid, text, char, timestamptz, text, text, jsonb, bigint);
drop function if exists public.revoke_device(uuid, uuid, text, text);
drop function if exists public.register_device(uuid, text, text, integer, text, text, text, text);
drop function if exists public.enforce_order_status_transition();
drop function if exists public.order_transition_allowed(text, text);
drop function if exists public.reserve_spending(uuid, uuid, bigint, char, bigint, text);
drop function if exists public.reserve_spending(uuid, uuid, bigint, char, bigint, date);
drop function if exists public.handle_new_user();
drop function if exists public.prevent_modification();
drop function if exists public.set_updated_at();

-- === BEGIN MIGRATION 20261024133807_initial_schema.sql ===
-- ClawPay middleware: initial schema (Phase 1).
--
-- Security model
--   * RLS is enabled on every table.
--   * anon has no access to anything.
--   * authenticated users may only SELECT their own rows (user_profiles also
--     allows updating display_name). All writes to orders, approvals,
--     spending_records, payment_attempts, mandates, audit_logs, etc. go through
--     the trusted backend using the service_role key.
--   * payment_methods.vault_token_reference / provider_customer_reference and
--     merchants.agent_endpoint_reference are hidden from clients via
--     column-level privileges.
--   * audit_logs is append-only for everyone (including service_role);
--     audit_logs.details / actor_id are hidden from clients.
--   * Composite FKs pin approvals / orders / mandates / spending_records /
--     conversations to the same user as the referenced order and device.
--
-- Money is stored as bigint minor units (*_minor) with an explicit ISO 4217
-- currency char(3) column. No currency is hardcoded.

-- ---------------------------------------------------------------------------
-- Helper functions
-- ---------------------------------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create or replace function public.prevent_modification()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception '% on %.% is not allowed (append-only table)',
    tg_op, tg_table_schema, tg_table_name
    using errcode = 'insufficient_privilege';
end;
$$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table public.user_profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Registered gadget approval verification keys. NEVER store private keys here.
create table public.devices (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  device_name text not null,
  public_key text not null,
  key_algorithm text not null,
  key_version integer not null default 1 check (key_version > 0),
  status text not null default 'pending'
    check (status in ('pending', 'active', 'revoked')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  revoked_at timestamptz,
  constraint devices_user_public_key_key unique (user_id, public_key),
  -- Target for composite FKs that pin a device reference to the same user.
  constraint devices_id_user_key unique (id, user_id),
  constraint devices_revoked_at_check check (status <> 'revoked' or revoked_at is not null)
);
comment on column public.devices.public_key is
  'Public verification key only. Private signing keys must never be stored.';

-- No passwords, raw card data, or unnecessary credentials. Token references are
-- not readable by clients (see column grants below).
create table public.payment_methods (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  provider text not null default 'paypal' check (provider in ('paypal')),
  provider_customer_reference text,
  vault_token_reference text,
  status text not null default 'pending'
    check (status in ('pending', 'active', 'revoked', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  revoked_at timestamptz
);

-- agent_endpoint_reference is a server-side lookup key, not exposed to clients.
create table public.merchants (
  id uuid primary key default gen_random_uuid(),
  merchant_code text not null unique,
  display_name text not null,
  agent_endpoint_reference text,
  status text not null default 'active' check (status in ('active', 'inactive')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  device_id uuid,
  source text not null default 'esp_claw',
  external_session_id text not null,
  status text not null default 'active' check (status in ('active', 'closed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint conversations_source_session_key unique (source, external_session_id),
  -- The device must belong to the same user.
  constraint conversations_device_owner_fkey foreign key (device_id, user_id)
    references public.devices (id, user_id) on delete set null (device_id)
);

create table public.orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  device_id uuid,
  merchant_id uuid not null references public.merchants (id) on delete restrict,
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  subtotal_minor bigint not null check (subtotal_minor >= 0),
  total_minor bigint not null check (total_minor >= 0),
  status text not null default 'CREATED' check (status in (
    'CREATED', 'QUOTED', 'AWAITING_APPROVAL', 'APPROVED', 'PAYMENT_PENDING', 'PAID',
    'DENIED', 'EXPIRED', 'CANCELLED', 'PAYMENT_FAILED', 'REQUIRES_RECONCILIATION'
  )),
  idempotency_key text not null,
  quote_expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint orders_user_idempotency_key unique (user_id, idempotency_key),
  -- Target for composite FKs that pin an order reference to the same user.
  constraint orders_id_user_key unique (id, user_id),
  -- The device must belong to the order's user.
  constraint orders_device_owner_fkey foreign key (device_id, user_id)
    references public.devices (id, user_id) on delete set null (device_id)
);

create table public.order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.orders (id) on delete cascade,
  product_reference text not null,
  description text,
  quantity integer not null check (quantity > 0),
  unit_price_minor bigint not null check (unit_price_minor >= 0),
  line_total_minor bigint not null,
  created_at timestamptz not null default now(),
  constraint order_items_line_total_check check (line_total_minor = quantity * unit_price_minor)
);

-- mandate_type is free text until the AP2 sample's actual contract is inspected.
create table public.mandates (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null,
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  provider_mandate_reference text,
  mandate_type text not null,
  status text not null default 'pending'
    check (status in ('pending', 'verified', 'rejected', 'expired', 'revoked')),
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- The mandate's user must be the order's user.
  constraint mandates_order_owner_fkey foreign key (order_id, user_id)
    references public.orders (id, user_id) on delete cascade
);

-- verification_status is set by the backend only, never from a client flag.
-- nonce is globally unique to block replayed gadget approvals.
-- Single use: consumed_at may only be set on a verified 'approved' row, and at
-- most one approval per (order, approval_type) can ever be consumed. Phase 2
-- must still consume with an atomic UPDATE ... WHERE consumed_at IS NULL.
create table public.approvals (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null,
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  -- Composite FK (see below) pins the device to the approval's user. NO ACTION
  -- (end-of-statement check) so a user-deletion cascade can remove devices and
  -- approvals together, while a device with approvals cannot be deleted on its
  -- own (revoke it instead).
  device_id uuid,
  approval_type text not null check (approval_type in ('gadget', 'phone')),
  decision text not null check (decision in ('approved', 'denied')),
  signed_payload_hash text,
  key_version integer,
  nonce text,
  approved_at timestamptz,
  expires_at timestamptz,
  verification_status text not null default 'pending'
    check (verification_status in ('pending', 'verified', 'failed')),
  consumed_at timestamptz,
  created_at timestamptz not null default now(),
  constraint approvals_nonce_key unique (nonce),
  constraint approvals_gadget_fields_check check (
    approval_type <> 'gadget'
    or (device_id is not null and nonce is not null and signed_payload_hash is not null)
  ),
  constraint approvals_consumed_check check (
    consumed_at is null or (decision = 'approved' and verification_status = 'verified')
  ),
  -- The approval's user must be the order's user, and the device must be theirs.
  constraint approvals_order_owner_fkey foreign key (order_id, user_id)
    references public.orders (id, user_id) on delete cascade,
  constraint approvals_device_owner_fkey foreign key (device_id, user_id)
    references public.devices (id, user_id) on delete no action
);

create unique index approvals_single_consumption_key
  on public.approvals (order_id, approval_type)
  where consumed_at is not null;

-- Spending lifecycle (managed by the backend via service_role):
--   reserved  -> created by public.reserve_spending() before payment.
--   committed -> payment confirmed by PayPal (exactly once per order).
--   released  -> payment failed, quote expired, or order cancelled.
-- Refunds do NOT release spending; the refund policy is still to be decided.
create table public.spending_records (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  order_id uuid not null,
  amount_minor bigint not null check (amount_minor > 0),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  status text not null default 'reserved'
    check (status in ('reserved', 'committed', 'released')),
  spending_day date not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint spending_records_order_owner_fkey foreign key (order_id, user_id)
    references public.orders (id, user_id) on delete cascade
);

-- At most one active (reserved/committed) spending record per order.
create unique index spending_records_active_order_key
  on public.spending_records (order_id)
  where status in ('reserved', 'committed');

-- A sent request is never treated as success; status comes from PayPal.
create table public.payment_attempts (
  id uuid primary key default gen_random_uuid(),
  -- NO ACTION: orders with payment history cannot be deleted.
  order_id uuid not null references public.orders (id) on delete no action,
  provider text not null default 'paypal' check (provider in ('paypal')),
  provider_order_id text,
  provider_capture_id text,
  status text not null default 'initiated'
    check (status in ('initiated', 'pending', 'completed', 'failed', 'unknown')),
  idempotency_key text not null,
  amount_minor bigint not null check (amount_minor > 0),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  failure_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint payment_attempts_idempotency_key_key unique (idempotency_key)
);

-- Exclude secrets and unnecessary sensitive data from payloads.
create table public.conversation_events (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations (id) on delete cascade,
  order_id uuid references public.orders (id) on delete set null,
  correlation_id text,
  event_type text not null check (event_type in (
    'transcript', 'intent', 'agent_call', 'agent_response', 'order_summary',
    'approval_required', 'payment_result', 'spoken_receipt', 'system'
  )),
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Append-only. user_id intentionally has no FK: an FK action (set null /
-- cascade) would modify audit rows, and audit history must survive user deletion.
create table public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  actor_type text not null
    check (actor_type in ('user', 'device', 'agent', 'system', 'admin', 'webhook')),
  actor_id text,
  event_type text not null,
  entity_type text,
  entity_id text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Webhook deduplication: persist before applying business effects.
create table public.webhook_events (
  id uuid primary key default gen_random_uuid(),
  provider text not null check (provider in ('paypal')),
  event_id text not null,
  event_type text,
  payload jsonb not null,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  constraint webhook_events_provider_event_id_key unique (provider, event_id)
);

-- ---------------------------------------------------------------------------
-- Indexes (FK columns not already covered by a leading unique index)
-- ---------------------------------------------------------------------------

create index payment_methods_user_id_idx on public.payment_methods (user_id);
create index conversations_user_id_idx on public.conversations (user_id);
create index conversations_device_id_idx on public.conversations (device_id);
create index orders_merchant_id_idx on public.orders (merchant_id);
create index orders_device_id_idx on public.orders (device_id);
create index orders_user_created_idx on public.orders (user_id, created_at desc);
create index order_items_order_id_idx on public.order_items (order_id);
create index mandates_order_id_idx on public.mandates (order_id);
create index mandates_user_id_idx on public.mandates (user_id);
create index approvals_order_id_idx on public.approvals (order_id);
create index approvals_user_id_idx on public.approvals (user_id);
create index approvals_device_id_idx on public.approvals (device_id);
create index spending_records_user_day_idx
  on public.spending_records (user_id, spending_day, status);
create index spending_records_order_user_idx on public.spending_records (order_id, user_id);
create index payment_attempts_order_id_idx on public.payment_attempts (order_id);
create index conversation_events_conversation_created_idx
  on public.conversation_events (conversation_id, created_at);
create index conversation_events_order_id_idx on public.conversation_events (order_id);
create index audit_logs_user_created_idx on public.audit_logs (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

create trigger user_profiles_set_updated_at before update on public.user_profiles
  for each row execute function public.set_updated_at();
create trigger devices_set_updated_at before update on public.devices
  for each row execute function public.set_updated_at();
create trigger payment_methods_set_updated_at before update on public.payment_methods
  for each row execute function public.set_updated_at();
create trigger merchants_set_updated_at before update on public.merchants
  for each row execute function public.set_updated_at();
create trigger conversations_set_updated_at before update on public.conversations
  for each row execute function public.set_updated_at();
create trigger orders_set_updated_at before update on public.orders
  for each row execute function public.set_updated_at();
create trigger mandates_set_updated_at before update on public.mandates
  for each row execute function public.set_updated_at();
create trigger spending_records_set_updated_at before update on public.spending_records
  for each row execute function public.set_updated_at();
create trigger payment_attempts_set_updated_at before update on public.payment_attempts
  for each row execute function public.set_updated_at();

create trigger audit_logs_append_only before update or delete on public.audit_logs
  for each row execute function public.prevent_modification();
create trigger audit_logs_no_truncate before truncate on public.audit_logs
  for each statement execute function public.prevent_modification();

-- Auto-create a profile for every new Supabase Auth user.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.user_profiles (id, display_name)
  values (new.id, new.raw_user_meta_data ->> 'display_name')
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- Spending reservation (concurrency-safe)
-- ---------------------------------------------------------------------------

-- Reserves daily spending capacity for an order. A per-user-per-day advisory
-- transaction lock serialises concurrent reservations so that
-- committed + reserved + amount can never exceed the daily limit.
--
-- Order state is validated inside the same transaction (PostgREST runs each
-- RPC in its own transaction, so the caller cannot do this atomically):
--   * the order is locked FOR UPDATE and must belong to p_user_id;
--   * p_currency is the POLICY currency: the order must be in it, and the day
--     must not already hold active records in another currency (the daily cap
--     is a single budget across all currencies);
--   * status must be AWAITING_APPROVAL, APPROVED or PAYMENT_PENDING;
--   * quote_expires_at must be set and in the future;
--   * p_amount_minor must equal orders.total_minor (the reserved amount is
--     always the server-side order total).
-- Idempotent: re-reserving an order with an active record of the same amount
-- returns it; a different amount returns amount_mismatch.
--
-- reason values: reserved, already_reserved, order_not_found,
-- currency_mismatch, mixed_currency, invalid_order_status, quote_expired,
-- amount_mismatch, daily_limit_exceeded.
create or replace function public.reserve_spending(
  p_user_id uuid,
  p_order_id uuid,
  p_amount_minor bigint,
  p_currency char(3),
  p_daily_limit_minor bigint,
  p_spending_day date
)
returns table (
  success boolean,
  reservation_id uuid,
  used_minor bigint,
  remaining_minor bigint,
  reason text
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_order_total bigint;
  v_order_currency char(3);
  v_order_status text;
  v_quote_expires_at timestamptz;
  v_existing_id uuid;
  v_existing_amount bigint;
  v_used bigint;
  v_other_currency boolean;
  v_remaining bigint;
  v_new_id uuid;
begin
  if p_user_id is null or p_order_id is null or p_amount_minor is null
     or p_currency is null or p_daily_limit_minor is null or p_spending_day is null then
    raise exception 'reserve_spending: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;
  if p_amount_minor <= 0 then
    raise exception 'reserve_spending: amount must be positive'
      using errcode = 'check_violation';
  end if;
  if p_daily_limit_minor < 0 then
    raise exception 'reserve_spending: daily limit must not be negative'
      using errcode = 'check_violation';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_user_id::text || ':' || p_spending_day::text, 0)
  );

  select o.total_minor, o.currency, o.status, o.quote_expires_at
    into v_order_total, v_order_currency, v_order_status, v_quote_expires_at
  from public.orders o
  where o.id = p_order_id and o.user_id = p_user_id
  for update;

  if not found then
    return query select false, null::uuid, 0::bigint, 0::bigint, 'order_not_found'::text;
    return;
  end if;

  -- Usage across ALL currencies: one daily budget, not one per currency.
  select coalesce(sum(s.amount_minor), 0)::bigint,
         coalesce(bool_or(s.currency <> p_currency), false)
    into v_used, v_other_currency
  from public.spending_records s
  where s.user_id = p_user_id
    and s.spending_day = p_spending_day
    and s.status in ('reserved', 'committed');

  v_remaining := greatest(p_daily_limit_minor - v_used, 0)::bigint;

  if v_order_currency <> p_currency then
    return query select false, null::uuid, v_used, v_remaining, 'currency_mismatch'::text;
    return;
  end if;

  if v_other_currency then
    return query select false, null::uuid, v_used, v_remaining, 'mixed_currency'::text;
    return;
  end if;

  if v_order_status not in ('AWAITING_APPROVAL', 'APPROVED', 'PAYMENT_PENDING') then
    return query select false, null::uuid, v_used, v_remaining, 'invalid_order_status'::text;
    return;
  end if;

  if v_quote_expires_at is null or v_quote_expires_at <= now() then
    return query select false, null::uuid, v_used, v_remaining, 'quote_expired'::text;
    return;
  end if;

  if p_amount_minor <> v_order_total then
    return query select false, null::uuid, v_used, v_remaining, 'amount_mismatch'::text;
    return;
  end if;

  select s.id, s.amount_minor into v_existing_id, v_existing_amount
  from public.spending_records s
  where s.order_id = p_order_id and s.status in ('reserved', 'committed');

  if found then
    if v_existing_amount <> v_order_total then
      return query select false, v_existing_id, v_used, v_remaining, 'amount_mismatch'::text;
    else
      return query select true, v_existing_id, v_used, v_remaining, 'already_reserved'::text;
    end if;
    return;
  end if;

  if v_used + v_order_total > p_daily_limit_minor then
    return query select false, null::uuid, v_used, v_remaining, 'daily_limit_exceeded'::text;
    return;
  end if;

  insert into public.spending_records (user_id, order_id, amount_minor, currency, status, spending_day)
  values (p_user_id, p_order_id, v_order_total, v_order_currency, 'reserved', p_spending_day)
  returning id into v_new_id;

  return query select true, v_new_id, (v_used + v_order_total)::bigint,
    (p_daily_limit_minor - v_used - v_order_total)::bigint, 'reserved'::text;
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges: start from zero for anon/authenticated, then grant minimally.
-- ---------------------------------------------------------------------------

revoke all on table
  public.user_profiles, public.devices, public.payment_methods, public.merchants,
  public.conversations, public.orders, public.order_items, public.mandates,
  public.approvals, public.spending_records, public.payment_attempts,
  public.conversation_events, public.audit_logs, public.webhook_events
from anon, authenticated;

revoke execute on function public.set_updated_at() from public, anon, authenticated;
revoke execute on function public.prevent_modification() from public, anon, authenticated;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.reserve_spending(uuid, uuid, bigint, char, bigint, date)
  from public, anon, authenticated;
grant execute on function public.reserve_spending(uuid, uuid, bigint, char, bigint, date)
  to service_role;

alter table public.user_profiles enable row level security;
alter table public.devices enable row level security;
alter table public.payment_methods enable row level security;
alter table public.merchants enable row level security;
alter table public.conversations enable row level security;
alter table public.orders enable row level security;
alter table public.order_items enable row level security;
alter table public.mandates enable row level security;
alter table public.approvals enable row level security;
alter table public.spending_records enable row level security;
alter table public.payment_attempts enable row level security;
alter table public.conversation_events enable row level security;
alter table public.audit_logs enable row level security;
alter table public.webhook_events enable row level security;

-- user_profiles: read own row; edit own display_name only.
grant select on public.user_profiles to authenticated;
grant update (display_name) on public.user_profiles to authenticated;
create policy user_profiles_select_own on public.user_profiles
  for select to authenticated using (id = (select auth.uid()));
create policy user_profiles_update_own on public.user_profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- Owner-scoped, read-only tables.
grant select on public.devices to authenticated;
create policy devices_select_own on public.devices
  for select to authenticated using (user_id = (select auth.uid()));

grant select on public.orders to authenticated;
create policy orders_select_own on public.orders
  for select to authenticated using (user_id = (select auth.uid()));

grant select on public.mandates to authenticated;
create policy mandates_select_own on public.mandates
  for select to authenticated using (user_id = (select auth.uid()));

grant select on public.approvals to authenticated;
create policy approvals_select_own on public.approvals
  for select to authenticated using (user_id = (select auth.uid()));

grant select on public.spending_records to authenticated;
create policy spending_records_select_own on public.spending_records
  for select to authenticated using (user_id = (select auth.uid()));

grant select on public.conversations to authenticated;
create policy conversations_select_own on public.conversations
  for select to authenticated using (user_id = (select auth.uid()));

-- audit_logs: details (verification reasons, key versions) and actor_id (admin /
-- device identifiers) stay server-side; users see only what happened and when.
grant select (id, user_id, actor_type, event_type, entity_type, entity_id, created_at)
  on public.audit_logs to authenticated;
create policy audit_logs_select_own on public.audit_logs
  for select to authenticated using (user_id = (select auth.uid()));

-- Tables owned through their parent order / conversation.
grant select on public.order_items to authenticated;
create policy order_items_select_own on public.order_items
  for select to authenticated using (
    exists (
      select 1 from public.orders o
      where o.id = order_items.order_id and o.user_id = (select auth.uid())
    )
  );

grant select on public.payment_attempts to authenticated;
create policy payment_attempts_select_own on public.payment_attempts
  for select to authenticated using (
    exists (
      select 1 from public.orders o
      where o.id = payment_attempts.order_id and o.user_id = (select auth.uid())
    )
  );

grant select on public.conversation_events to authenticated;
create policy conversation_events_select_own on public.conversation_events
  for select to authenticated using (
    exists (
      select 1 from public.conversations c
      where c.id = conversation_events.conversation_id and c.user_id = (select auth.uid())
    )
  );

-- payment_methods: non-sensitive columns only (no token/customer references).
grant select (id, user_id, provider, status, created_at, updated_at, revoked_at)
  on public.payment_methods to authenticated;
create policy payment_methods_select_own on public.payment_methods
  for select to authenticated using (user_id = (select auth.uid()));

-- merchants: active merchants visible to any signed-in user, minus endpoint refs.
grant select (id, merchant_code, display_name, status, created_at, updated_at)
  on public.merchants to authenticated;
create policy merchants_select_active on public.merchants
  for select to authenticated using (status = 'active');

-- webhook_events: RLS on, no grants, no policies (service_role only).

-- Trusted backend path.
grant all on all tables in schema public to service_role;
-- === END MIGRATION 20261024133807_initial_schema.sql ===


-- === BEGIN MIGRATION 20261025090000_phase2_hardening.sql ===
-- ClawPay Phase 2 hardening (security review findings #2, #3, #4) and the
-- order status transition trigger (design §2.4, §2.9, §6.1).
--
-- The initial migration 20261024133807 is applied to the hosted project and is
-- never edited; every change lands here as a new migration.

-- ---------------------------------------------------------------------------
-- Review #4: default privileges.
-- Removes Supabase's explicit per-schema grants to anon/authenticated for
-- objects created later by postgres. PostgreSQL's global default EXECUTE to
-- PUBLIC on new functions survives this, so every function below also carries
-- its own "revoke execute ... from public, anon, authenticated".
-- ---------------------------------------------------------------------------

alter default privileges for role postgres in schema public revoke all on tables    from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public revoke all on functions from anon, authenticated;

-- ---------------------------------------------------------------------------
-- Review #2: reserve_spending derives the spending day in SQL.
-- Same body and reason codes as the initial version, except the day is
-- (now() at time zone p_timezone)::date and p_timezone must be a known zone.
-- Nothing calls the old signature yet, so dropping it is safe.
-- ---------------------------------------------------------------------------

drop function public.reserve_spending(uuid, uuid, bigint, char, bigint, date);

create function public.reserve_spending(
  p_user_id uuid,
  p_order_id uuid,
  p_amount_minor bigint,
  p_currency char(3),
  p_daily_limit_minor bigint,
  p_timezone text
)
returns table (
  success boolean,
  reservation_id uuid,
  used_minor bigint,
  remaining_minor bigint,
  reason text
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_day date;
  v_order_total bigint;
  v_order_currency char(3);
  v_order_status text;
  v_quote_expires_at timestamptz;
  v_existing_id uuid;
  v_existing_amount bigint;
  v_used bigint;
  v_other_currency boolean;
  v_remaining bigint;
  v_new_id uuid;
begin
  if p_user_id is null or p_order_id is null or p_amount_minor is null
     or p_currency is null or p_daily_limit_minor is null or p_timezone is null then
    raise exception 'reserve_spending: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;
  if not exists (select 1 from pg_catalog.pg_timezone_names tz where tz.name = p_timezone) then
    raise exception 'reserve_spending: unknown timezone'
      using errcode = 'invalid_parameter_value';
  end if;
  if p_amount_minor <= 0 then
    raise exception 'reserve_spending: amount must be positive'
      using errcode = 'check_violation';
  end if;
  if p_daily_limit_minor < 0 then
    raise exception 'reserve_spending: daily limit must not be negative'
      using errcode = 'check_violation';
  end if;

  v_day := (now() at time zone p_timezone)::date;

  perform pg_advisory_xact_lock(
    hashtextextended(p_user_id::text || ':' || v_day::text, 0)
  );

  select o.total_minor, o.currency, o.status, o.quote_expires_at
    into v_order_total, v_order_currency, v_order_status, v_quote_expires_at
  from public.orders o
  where o.id = p_order_id and o.user_id = p_user_id
  for update;

  if not found then
    return query select false, null::uuid, 0::bigint, 0::bigint, 'order_not_found'::text;
    return;
  end if;

  -- Usage across ALL currencies: one daily budget, not one per currency.
  select coalesce(sum(s.amount_minor), 0)::bigint,
         coalesce(bool_or(s.currency <> p_currency), false)
    into v_used, v_other_currency
  from public.spending_records s
  where s.user_id = p_user_id
    and s.spending_day = v_day
    and s.status in ('reserved', 'committed');

  v_remaining := greatest(p_daily_limit_minor - v_used, 0)::bigint;

  if v_order_currency <> p_currency then
    return query select false, null::uuid, v_used, v_remaining, 'currency_mismatch'::text;
    return;
  end if;

  if v_other_currency then
    return query select false, null::uuid, v_used, v_remaining, 'mixed_currency'::text;
    return;
  end if;

  if v_order_status not in ('AWAITING_APPROVAL', 'APPROVED', 'PAYMENT_PENDING') then
    return query select false, null::uuid, v_used, v_remaining, 'invalid_order_status'::text;
    return;
  end if;

  if v_quote_expires_at is null or v_quote_expires_at <= now() then
    return query select false, null::uuid, v_used, v_remaining, 'quote_expired'::text;
    return;
  end if;

  if p_amount_minor <> v_order_total then
    return query select false, null::uuid, v_used, v_remaining, 'amount_mismatch'::text;
    return;
  end if;

  select s.id, s.amount_minor into v_existing_id, v_existing_amount
  from public.spending_records s
  where s.order_id = p_order_id and s.status in ('reserved', 'committed');

  if found then
    if v_existing_amount <> v_order_total then
      return query select false, v_existing_id, v_used, v_remaining, 'amount_mismatch'::text;
    else
      return query select true, v_existing_id, v_used, v_remaining, 'already_reserved'::text;
    end if;
    return;
  end if;

  if v_used + v_order_total > p_daily_limit_minor then
    return query select false, null::uuid, v_used, v_remaining, 'daily_limit_exceeded'::text;
    return;
  end if;

  insert into public.spending_records (user_id, order_id, amount_minor, currency, status, spending_day)
  values (p_user_id, p_order_id, v_order_total, v_order_currency, 'reserved', v_day)
  returning id into v_new_id;

  return query select true, v_new_id, (v_used + v_order_total)::bigint,
    (p_daily_limit_minor - v_used - v_order_total)::bigint, 'reserved'::text;
end;
$$;

revoke execute on function public.reserve_spending(uuid, uuid, bigint, char, bigint, text)
  from public, anon, authenticated;
grant execute on function public.reserve_spending(uuid, uuid, bigint, char, bigint, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- Review #3: conversation_events.payload is server-only.
-- Users read the allowlisted summary; payload is always written as {} in this
-- scope and is hidden by the column grant.
-- ---------------------------------------------------------------------------

alter table public.conversation_events
  add column summary jsonb not null default '{}'::jsonb;

revoke select on public.conversation_events from authenticated;
grant select (id, conversation_id, order_id, correlation_id, event_type, summary, created_at)
  on public.conversation_events to authenticated;

-- ---------------------------------------------------------------------------
-- Order status transitions (design §2.4). Authoritative; app/domain/order_state.py
-- mirrors this table. INSERT is unrestricted (the create RPC inserts CREATED).
-- ---------------------------------------------------------------------------

create or replace function public.order_transition_allowed(p_from text, p_to text)
returns boolean
language sql
immutable
security invoker
set search_path = ''
as $$
  select case p_from
    when 'CREATED' then p_to in ('QUOTED', 'CANCELLED', 'EXPIRED')
    when 'QUOTED' then p_to in ('AWAITING_APPROVAL', 'CANCELLED', 'EXPIRED')
    when 'AWAITING_APPROVAL' then p_to in ('APPROVED', 'DENIED', 'EXPIRED', 'CANCELLED')
    when 'APPROVED' then p_to in ('PAYMENT_PENDING', 'CANCELLED')
    when 'PAYMENT_PENDING' then p_to in ('PAID', 'PAYMENT_FAILED', 'REQUIRES_RECONCILIATION')
    when 'REQUIRES_RECONCILIATION' then p_to in ('PAID', 'PAYMENT_FAILED', 'PAYMENT_PENDING')
    when 'PAYMENT_FAILED' then p_to in ('PAID')
    else false
  end;
$$;

revoke execute on function public.order_transition_allowed(text, text)
  from public, anon, authenticated;
grant execute on function public.order_transition_allowed(text, text) to service_role;

create or replace function public.enforce_order_status_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if not coalesce(public.order_transition_allowed(old.status, new.status), false) then
    raise exception 'illegal order status transition % -> %', old.status, new.status
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

revoke execute on function public.enforce_order_status_transition()
  from public, anon, authenticated;
grant execute on function public.enforce_order_status_transition() to service_role;

create trigger orders_enforce_status_transition
  before update of status on public.orders
  for each row
  when (old.status is distinct from new.status)
  execute function public.enforce_order_status_transition();
-- === END MIGRATION 20261025090000_phase2_hardening.sql ===


-- === BEGIN MIGRATION 20261025090100_phase2_devices_orders_a2a.sql ===
-- ClawPay Phase 2: device tokens, order creation columns, merchant seed,
-- a2a_messages and the Phase 2 RPCs (design §2.3, §2.6, §2.7, §6.2).
--
-- Global lock order for every RPC (design §1.1):
--   devices row -> orders row -> approval_challenges row -> payment_methods row
--   (FOR SHARE) -> spending advisory lock -> spending_records / payment_attempts.
-- Device locks are FOR SHARE except in revoke_device (FOR UPDATE). Multi-row
-- locks are taken ordered by id. An RPC that starts from a lower-ranked row reads
-- it without a lock first, then locks in this order and re-validates.
--
-- Business outcomes are returned as (ok, reason, ...) rows; exceptions are raised
-- only for programming errors (null arguments, inconsistent totals).

-- ---------------------------------------------------------------------------
-- devices: token hashes, last_seen, global active-key uniqueness
-- ---------------------------------------------------------------------------

alter table public.devices
  add column token_hash text,
  add column llm_token_hash text,
  add column token_issued_at timestamptz,
  add column last_seen_at timestamptz;

create unique index devices_token_hash_key
  on public.devices (token_hash) where token_hash is not null;
create unique index devices_llm_token_hash_key
  on public.devices (llm_token_hash) where llm_token_hash is not null;

-- The pushed unique (user_id, public_key) would block re-registration after
-- revocation; replace it with a global uniqueness over non-revoked devices.
alter table public.devices drop constraint devices_user_public_key_key;
create unique index devices_active_public_key_key
  on public.devices (public_key) where status <> 'revoked';

alter table public.devices
  add constraint devices_key_algorithm_check
  check (key_algorithm = 'ecdsa-p256-sha256') not valid;

-- Hide token_hash / llm_token_hash from clients.
revoke select on public.devices from authenticated;
grant select (id, user_id, device_name, public_key, key_algorithm, key_version, status,
              created_at, updated_at, revoked_at, last_seen_at)
  on public.devices to authenticated;

-- ---------------------------------------------------------------------------
-- orders: cart hash and merchant quote reference (dedup window, AP2 binding)
-- ---------------------------------------------------------------------------

alter table public.orders
  add column cart_sha256 text check (cart_sha256 ~ '^[0-9a-f]{64}$'),
  add column quote_reference text check (char_length(quote_reference) <= 64);

create index orders_device_cart_idx
  on public.orders (device_id, cart_sha256, created_at desc);

-- ---------------------------------------------------------------------------
-- merchants seed (endpoints come from env, not the DB)
-- ---------------------------------------------------------------------------

-- Merchant data is intentionally applied separately by seed.sql.

-- ---------------------------------------------------------------------------
-- a2a_messages: merchant agent traffic. summary is user-readable; payload is
-- server-only (validated data objects, never raw bytes or headers).
-- ---------------------------------------------------------------------------

create table public.a2a_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  order_id uuid,
  conversation_id uuid references public.conversations (id) on delete set null,
  merchant_code text not null,
  correlation_id text not null,
  direction text not null check (direction in ('request', 'response', 'error')),
  method text not null check (method in ('search', 'quote')),
  outcome text not null check (outcome in ('ok', 'rejected', 'timeout', 'unavailable')),
  latency_ms integer check (latency_ms >= 0),
  summary jsonb not null default '{}'::jsonb,
  payload jsonb not null default '{}'::jsonb check (pg_column_size(payload) <= 32768),
  created_at timestamptz not null default now(),
  constraint a2a_messages_order_owner_fkey foreign key (order_id, user_id)
    references public.orders (id, user_id) on delete cascade
);

create index a2a_messages_user_created_idx on public.a2a_messages (user_id, created_at desc);
create index a2a_messages_order_id_idx on public.a2a_messages (order_id);
create index a2a_messages_conversation_id_idx on public.a2a_messages (conversation_id);

alter table public.a2a_messages enable row level security;
revoke all on public.a2a_messages from anon, authenticated;
grant select (id, user_id, order_id, conversation_id, merchant_code, correlation_id, direction,
              method, outcome, latency_ms, summary, created_at)
  on public.a2a_messages to authenticated;
grant all on public.a2a_messages to service_role;
create policy a2a_messages_select_own on public.a2a_messages
  for select to authenticated using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- register_device
-- reasons: registered, user_not_found, public_key_already_registered.
-- A concurrent duplicate passes the pre-check and fails on
-- devices_active_public_key_key (23505); the backend maps that to 409.
-- ---------------------------------------------------------------------------

create or replace function public.register_device(
  p_user_id uuid,
  p_device_name text,
  p_public_key text,
  p_key_version integer,
  p_token_hash text,
  p_llm_token_hash text,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text, device_id uuid)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_id uuid;
begin
  if p_user_id is null or p_device_name is null or p_public_key is null
     or p_key_version is null or p_token_hash is null or p_llm_token_hash is null
     or p_actor_type is null then
    raise exception 'register_device: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  if not exists (select 1 from public.user_profiles u where u.id = p_user_id) then
    return query select false, 'user_not_found'::text, null::uuid;
    return;
  end if;

  if exists (
    select 1 from public.devices d
    where d.public_key = p_public_key and d.status <> 'revoked'
  ) then
    return query select false, 'public_key_already_registered'::text, null::uuid;
    return;
  end if;

  insert into public.devices (user_id, device_name, public_key, key_algorithm, key_version,
                              status, token_hash, llm_token_hash, token_issued_at)
  values (p_user_id, p_device_name, p_public_key, 'ecdsa-p256-sha256', p_key_version,
          'active', p_token_hash, p_llm_token_hash, now())
  returning id into v_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, p_actor_type, p_actor_id, 'device.registered', 'device', v_id::text,
     jsonb_build_object('key_version', p_key_version)),
    (p_user_id, p_actor_type, p_actor_id, 'device.token_issued', 'device', v_id::text,
     jsonb_build_object('kinds', jsonb_build_array('device', 'llm')));

  return query select true, 'registered'::text, v_id;
end;
$$;

revoke execute on function public.register_device(uuid, text, text, integer, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.register_device(uuid, text, text, integer, text, text, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- revoke_device (device FOR UPDATE -> its open orders FOR UPDATE ordered by id)
-- p_user_id null = admin caller; otherwise the caller must own the device.
-- reasons: revoked, already_revoked, not_found.
-- Phase 3 replaces this function to also invalidate open approval challenges.
-- ---------------------------------------------------------------------------

create or replace function public.revoke_device(
  p_device_id uuid,
  p_user_id uuid,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text, device_id uuid, cancelled_orders integer)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_status text;
  v_reason text;
  v_order record;
  v_cancelled integer := 0;
begin
  if p_device_id is null or p_actor_type is null then
    raise exception 'revoke_device: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select d.user_id, d.status into v_user_id, v_status
  from public.devices d
  where d.id = p_device_id
  for update;

  if not found or (p_user_id is not null and v_user_id <> p_user_id) then
    return query select false, 'not_found'::text, null::uuid, 0;
    return;
  end if;

  if v_status = 'revoked' then
    v_reason := 'already_revoked';
  else
    update public.devices d
      set status = 'revoked', revoked_at = now()
      where d.id = p_device_id;
    v_reason := 'revoked';
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, p_actor_type, p_actor_id, 'device.revoked', 'device', p_device_id::text, '{}'::jsonb);
  end if;

  -- No spending reservation exists before approval, so nothing to release.
  for v_order in
    select o.id, o.status from public.orders o
    where o.device_id = p_device_id
      and o.status in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
    order by o.id
    for update
  loop
    update public.orders o set status = 'CANCELLED' where o.id = v_order.id;
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, p_actor_type, p_actor_id, 'order.status_changed', 'order', v_order.id::text,
            jsonb_build_object('from', v_order.status, 'to', 'CANCELLED', 'reason', 'device_revoked'));
    v_cancelled := v_cancelled + 1;
  end loop;

  return query select true, v_reason, p_device_id, v_cancelled;
end;
$$;

revoke execute on function public.revoke_device(uuid, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.revoke_device(uuid, uuid, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- create_order_from_quote
-- Device FOR SHARE -> per-(device, cart) advisory lock -> dedup or insert.
-- p_items: [{"sku","name","qty","unit_price_minor"}]; line totals recomputed here.
-- reasons: created, deduplicated, device_revoked, merchant_unavailable.
-- Raises on empty items or when the recomputed sum differs from p_total_minor.
-- ---------------------------------------------------------------------------

create or replace function public.create_order_from_quote(
  p_user_id uuid,
  p_device_id uuid,
  p_merchant_code text,
  p_currency char(3),
  p_quote_expires_at timestamptz,
  p_quote_reference text,
  p_cart_sha256 text,
  p_items jsonb,
  p_total_minor bigint
)
returns table (ok boolean, reason text, order_id uuid, deduplicated boolean)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_device_status text;
  v_merchant_id uuid;
  v_existing uuid;
  v_order_id uuid;
  v_item jsonb;
  v_qty integer;
  v_unit bigint;
  v_sum bigint := 0;
  v_items_sum bigint;
begin
  if p_user_id is null or p_device_id is null or p_merchant_code is null or p_currency is null
     or p_quote_expires_at is null or p_cart_sha256 is null or p_items is null
     or p_total_minor is null then
    raise exception 'create_order_from_quote: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;
  if jsonb_typeof(p_items) <> 'array' or jsonb_array_length(p_items) = 0 then
    raise exception 'create_order_from_quote: items must be a non-empty array'
      using errcode = 'check_violation';
  end if;

  select d.status into v_device_status
  from public.devices d
  where d.id = p_device_id and d.user_id = p_user_id
  for share;

  if not found or v_device_status <> 'active' then
    return query select false, 'device_revoked'::text, null::uuid, false;
    return;
  end if;

  select m.id into v_merchant_id
  from public.merchants m
  where m.merchant_code = p_merchant_code and m.status = 'active';

  if not found then
    return query select false, 'merchant_unavailable'::text, null::uuid, false;
    return;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_device_id::text || ':' || p_cart_sha256, 0));

  select o.id into v_existing
  from public.orders o
  where o.device_id = p_device_id
    and o.cart_sha256 = p_cart_sha256
    and o.merchant_id = v_merchant_id
    and o.currency = p_currency
    and o.status = 'AWAITING_APPROVAL'
    and o.created_at > now() - interval '60 seconds'
    and o.quote_expires_at > now()
  order by o.created_at desc
  limit 1;

  if found then
    return query select true, 'deduplicated'::text, v_existing, true;
    return;
  end if;

  for v_item in select value from jsonb_array_elements(p_items)
  loop
    v_qty := (v_item ->> 'qty')::integer;
    v_unit := (v_item ->> 'unit_price_minor')::bigint;
    if v_qty is null or v_qty <= 0 or v_unit is null or v_unit <= 0
       or coalesce(v_item ->> 'sku', '') = '' then
      raise exception 'create_order_from_quote: invalid item'
        using errcode = 'check_violation';
    end if;
    v_sum := v_sum + v_qty * v_unit;
  end loop;

  if v_sum <> p_total_minor then
    raise exception 'create_order_from_quote: item sum does not match total'
      using errcode = 'check_violation';
  end if;

  insert into public.orders (user_id, device_id, merchant_id, currency, subtotal_minor, total_minor,
                             status, idempotency_key, quote_expires_at, cart_sha256, quote_reference)
  values (p_user_id, p_device_id, v_merchant_id, p_currency, v_sum, v_sum,
          'CREATED', 'mcp:' || gen_random_uuid()::text, p_quote_expires_at, p_cart_sha256,
          p_quote_reference)
  returning id into v_order_id;

  insert into public.order_items (order_id, product_reference, description, quantity,
                                  unit_price_minor, line_total_minor)
  select v_order_id, e ->> 'sku', e ->> 'name', (e ->> 'qty')::integer,
         (e ->> 'unit_price_minor')::bigint,
         (e ->> 'qty')::integer * (e ->> 'unit_price_minor')::bigint
  from jsonb_array_elements(p_items) e;

  select coalesce(sum(i.line_total_minor), 0) into v_items_sum
  from public.order_items i where i.order_id = v_order_id;
  if v_items_sum <> v_sum then
    raise exception 'create_order_from_quote: stored lines do not match total'
      using errcode = 'check_violation';
  end if;

  update public.orders o set status = 'QUOTED' where o.id = v_order_id;
  update public.orders o set status = 'AWAITING_APPROVAL' where o.id = v_order_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, 'device', p_device_id::text, 'order.status_changed', 'order', v_order_id::text,
     jsonb_build_object('from', 'CREATED', 'to', 'QUOTED', 'reason', 'quote_validated')),
    (p_user_id, 'device', p_device_id::text, 'order.status_changed', 'order', v_order_id::text,
     jsonb_build_object('from', 'QUOTED', 'to', 'AWAITING_APPROVAL', 'reason', 'order_created')),
    (p_user_id, 'device', p_device_id::text, 'order.created', 'order', v_order_id::text,
     jsonb_build_object('merchant', p_merchant_code, 'total_minor', v_sum, 'currency', p_currency));

  return query select true, 'created'::text, v_order_id, false;
end;
$$;

revoke execute on function public.create_order_from_quote(uuid, uuid, text, char, timestamptz, text, text, jsonb, bigint)
  from public, anon, authenticated;
grant execute on function public.create_order_from_quote(uuid, uuid, text, char, timestamptz, text, text, jsonb, bigint)
  to service_role;

-- ---------------------------------------------------------------------------
-- cancel_order (device FOR SHARE -> order FOR UPDATE)
-- Allowed from CREATED, QUOTED, AWAITING_APPROVAL, APPROVED; releases any
-- reservation. reasons: cancelled, not_found, invalid_order_status.
-- Phase 3 replaces this function to also invalidate open approval challenges.
-- ---------------------------------------------------------------------------

create or replace function public.cancel_order(
  p_order_id uuid,
  p_user_id uuid,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text, order_id uuid, status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_device_id uuid;
  v_user_id uuid;
  v_status text;
  v_released integer;
begin
  if p_order_id is null or p_user_id is null or p_actor_type is null then
    raise exception 'cancel_order: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  -- Unlocked read to learn the device id, then lock in the global order.
  select o.device_id, o.user_id into v_device_id, v_user_id
  from public.orders o where o.id = p_order_id;

  if not found or v_user_id <> p_user_id then
    return query select false, 'not_found'::text, null::uuid, null::text;
    return;
  end if;

  if v_device_id is not null then
    perform 1 from public.devices d where d.id = v_device_id for share;
  end if;

  select o.status, o.user_id into v_status, v_user_id
  from public.orders o where o.id = p_order_id
  for update;

  if not found or v_user_id <> p_user_id then
    return query select false, 'not_found'::text, null::uuid, null::text;
    return;
  end if;

  if v_status not in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL', 'APPROVED') then
    return query select false, 'invalid_order_status'::text, p_order_id, v_status;
    return;
  end if;

  update public.orders o set status = 'CANCELLED' where o.id = p_order_id;

  update public.spending_records s set status = 'released'
    where s.order_id = p_order_id and s.status = 'reserved';
  get diagnostics v_released = row_count;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, p_actor_type, p_actor_id, 'order.status_changed', 'order', p_order_id::text,
     jsonb_build_object('from', v_status, 'to', 'CANCELLED', 'reason', 'cancelled')),
    (p_user_id, p_actor_type, p_actor_id, 'order.cancelled', 'order', p_order_id::text, '{}'::jsonb);
  if v_released > 0 then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (p_user_id, p_actor_type, p_actor_id, 'spending.released', 'order', p_order_id::text,
            jsonb_build_object('reason', 'cancelled'));
  end if;

  return query select true, 'cancelled'::text, p_order_id, 'CANCELLED'::text;
end;
$$;

revoke execute on function public.cancel_order(uuid, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.cancel_order(uuid, uuid, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- expire_order (device FOR SHARE -> order FOR UPDATE)
-- Moves CREATED/QUOTED/AWAITING_APPROVAL orders whose quote expired to EXPIRED.
-- reasons: expired, not_expirable, order_not_found.
-- ---------------------------------------------------------------------------

create or replace function public.expire_order(p_order_id uuid)
returns table (ok boolean, reason text, status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_device_id uuid;
  v_user_id uuid;
  v_status text;
  v_expires timestamptz;
  v_released integer;
begin
  if p_order_id is null then
    raise exception 'expire_order: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select o.device_id into v_device_id from public.orders o where o.id = p_order_id;
  if not found then
    return query select false, 'order_not_found'::text, null::text;
    return;
  end if;

  if v_device_id is not null then
    perform 1 from public.devices d where d.id = v_device_id for share;
  end if;

  select o.status, o.quote_expires_at, o.user_id into v_status, v_expires, v_user_id
  from public.orders o where o.id = p_order_id
  for update;

  if v_status not in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
     or v_expires is null or v_expires > now() then
    return query select false, 'not_expirable'::text, v_status;
    return;
  end if;

  update public.orders o set status = 'EXPIRED' where o.id = p_order_id;

  update public.spending_records s set status = 'released'
    where s.order_id = p_order_id and s.status = 'reserved';
  get diagnostics v_released = row_count;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (v_user_id, 'system', null, 'order.status_changed', 'order', p_order_id::text,
     jsonb_build_object('from', v_status, 'to', 'EXPIRED', 'reason', 'quote_expired')),
    (v_user_id, 'system', null, 'order.expired', 'order', p_order_id::text, '{}'::jsonb);
  if v_released > 0 then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, 'system', null, 'spending.released', 'order', p_order_id::text,
            jsonb_build_object('reason', 'expired'));
  end if;

  return query select true, 'expired'::text, 'EXPIRED'::text;
end;
$$;

revoke execute on function public.expire_order(uuid) from public, anon, authenticated;
grant execute on function public.expire_order(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- expire_stale_orders: sweep for the background loop. Each order goes through
-- expire_order, which takes its own locks in the global order.
-- ---------------------------------------------------------------------------

create or replace function public.expire_stale_orders(p_limit integer)
returns table (order_id uuid)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_id uuid;
  v_ok boolean;
begin
  if p_limit is null or p_limit <= 0 then
    raise exception 'expire_stale_orders: limit must be positive'
      using errcode = 'check_violation';
  end if;

  for v_id in
    select o.id from public.orders o
    where o.status in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
      and o.quote_expires_at is not null
      and o.quote_expires_at <= now()
    order by o.id
    limit p_limit
  loop
    select e.ok into v_ok from public.expire_order(v_id) e;
    if v_ok then
      order_id := v_id;
      return next;
    end if;
  end loop;
end;
$$;

revoke execute on function public.expire_stale_orders(integer) from public, anon, authenticated;
grant execute on function public.expire_stale_orders(integer) to service_role;

-- ---------------------------------------------------------------------------
-- spending_summary: today's committed / reserved usage in p_timezone.
-- ---------------------------------------------------------------------------

create or replace function public.spending_summary(p_user_id uuid, p_timezone text)
returns table (day date, committed_minor bigint, reserved_minor bigint)
language plpgsql
stable
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_day date;
begin
  if p_user_id is null or p_timezone is null then
    raise exception 'spending_summary: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;
  if not exists (select 1 from pg_catalog.pg_timezone_names tz where tz.name = p_timezone) then
    raise exception 'spending_summary: unknown timezone'
      using errcode = 'invalid_parameter_value';
  end if;

  v_day := (now() at time zone p_timezone)::date;

  return query
  select v_day,
         coalesce(sum(s.amount_minor) filter (where s.status = 'committed'), 0)::bigint,
         coalesce(sum(s.amount_minor) filter (where s.status = 'reserved'), 0)::bigint
  from public.spending_records s
  where s.user_id = p_user_id and s.spending_day = v_day;
end;
$$;

revoke execute on function public.spending_summary(uuid, text) from public, anon, authenticated;
grant execute on function public.spending_summary(uuid, text) to service_role;
-- === END MIGRATION 20261025090100_phase2_devices_orders_a2a.sql ===


-- === BEGIN MIGRATION 20261025090200_phase3_approvals_payments.sql ===
-- ClawPay Phase 3: approval challenges, payment attempts, mandates,
-- payment methods linking and Phase 3 RPCs (design §3.1–§3.10, §6.3).
--
-- Global lock order for every RPC (design §1.1):
--   devices row -> orders row -> approval_challenges row -> payment_methods row
--   (FOR SHARE) -> spending advisory lock -> spending_records / payment_attempts.
-- Multi-row locks are taken ordered by id. An RPC that starts from a lower-ranked
-- row reads it without a lock first, then locks in this order and re-validates.

-- ---------------------------------------------------------------------------
-- approval_challenges (design §6.3)
-- ---------------------------------------------------------------------------

create table public.approval_challenges (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null,
  device_id uuid not null,
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  nonce text not null unique check (nonce ~ '^[A-Za-z0-9_-]{43}$'),
  canonical_payload text not null check (canonical_payload ~ '^[ -~]+$' and char_length(canonical_payload) <= 2048),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  amount_minor bigint not null check (amount_minor > 0),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  key_version integer not null check (key_version > 0),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  invalidated_at timestamptz,
  created_at timestamptz not null default now(),
  constraint approval_challenges_order_user_fk
    foreign key (order_id, user_id) references public.orders (id, user_id) on delete cascade,
  constraint approval_challenges_device_user_fk
    foreign key (device_id, user_id) references public.devices (id, user_id) on delete cascade,
  constraint approval_challenges_consumed_or_invalidated_check
    check (consumed_at is null or invalidated_at is null)
);

create unique index approval_challenges_open_key
  on public.approval_challenges (order_id, device_id)
  where consumed_at is null and invalidated_at is null;

create index approval_challenges_device_idx
  on public.approval_challenges (device_id);

alter table public.approval_challenges enable row level security;
revoke all on public.approval_challenges from anon, authenticated;

-- ---------------------------------------------------------------------------
-- payment_attempts additions and column grants (design §6.3)
-- ---------------------------------------------------------------------------

create unique index payment_attempts_one_per_order
  on public.payment_attempts (order_id);

alter table public.payment_attempts
  add column payment_method_id uuid references public.payment_methods (id) on delete no action,
  add column provider_status text check (char_length(provider_status) <= 40),
  add column claimed_until timestamptz,
  add column first_sent_at timestamptz,
  add column capture_sent_at timestamptz,
  add column capture_send_count integer not null default 0 check (capture_send_count >= 0),
  add column completed_at timestamptz,
  add column exhausted_at timestamptz;

create index payment_attempts_reconcile_idx
  on public.payment_attempts (created_at)
  where status in ('initiated', 'pending', 'unknown') and exhausted_at is null;

revoke select on public.payment_attempts from authenticated;
grant select (id, order_id, provider, status, provider_status, amount_minor,
              currency, failure_code, created_at, updated_at, completed_at)
  on public.payment_attempts to authenticated;

-- ---------------------------------------------------------------------------
-- approvals & mandates column grants (design §6.3)
-- ---------------------------------------------------------------------------

revoke select on public.approvals from authenticated;
grant select (id, order_id, user_id, device_id, approval_type, decision,
              key_version, approved_at, expires_at, verification_status,
              consumed_at, created_at)
  on public.approvals to authenticated;

revoke select on public.mandates from authenticated;
grant select (id, order_id, user_id, mandate_type, status, expires_at,
              created_at, updated_at)
  on public.mandates to authenticated;

-- ---------------------------------------------------------------------------
-- payment_methods additions and grants (design §3.10, §6.3)
-- ---------------------------------------------------------------------------

alter table public.payment_methods
  add column setup_token_reference text,
  add column setup_state_hash text,
  add column setup_expires_at timestamptz,
  add column payer_email_masked text check (char_length(payer_email_masked) <= 120);

grant select (payer_email_masked) on public.payment_methods to authenticated;

create unique index payment_methods_one_active_per_user
  on public.payment_methods (user_id) where status = 'active';

create unique index payment_methods_setup_state_key
  on public.payment_methods (setup_state_hash) where setup_state_hash is not null;

-- ---------------------------------------------------------------------------
-- RPC: revoke_device replacement (design §2.3, §6.2, §6.3)
-- Also invalidates open approval challenges of the device.
-- ---------------------------------------------------------------------------

create or replace function public.revoke_device(
  p_device_id uuid,
  p_user_id uuid,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text, device_id uuid, cancelled_orders integer)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_status text;
  v_reason text;
  v_order record;
  v_challenge record;
  v_cancelled integer := 0;
begin
  if p_device_id is null or p_actor_type is null then
    raise exception 'revoke_device: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select d.user_id, d.status into v_user_id, v_status
  from public.devices d
  where d.id = p_device_id
  for update;

  if not found or (p_user_id is not null and v_user_id <> p_user_id) then
    return query select false, 'not_found'::text, null::uuid, 0;
    return;
  end if;

  if v_status = 'revoked' then
    v_reason := 'already_revoked';
  else
    update public.devices d
      set status = 'revoked', revoked_at = now()
      where d.id = p_device_id;
    v_reason := 'revoked';
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, p_actor_type, p_actor_id, 'device.revoked', 'device', p_device_id::text, '{}'::jsonb);
  end if;

  -- Cancel open orders ordered by id
  for v_order in
    select o.id, o.status from public.orders o
    where o.device_id = p_device_id
      and o.status in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
    order by o.id
    for update
  loop
    update public.orders o set status = 'CANCELLED' where o.id = v_order.id;
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, p_actor_type, p_actor_id, 'order.status_changed', 'order', v_order.id::text,
            jsonb_build_object('from', v_order.status, 'to', 'CANCELLED', 'reason', 'device_revoked'));
    v_cancelled := v_cancelled + 1;
  end loop;

  -- Invalidate open approval challenges of that device ordered by id
  for v_challenge in
    select c.id from public.approval_challenges c
    where c.device_id = p_device_id
      and c.consumed_at is null
      and c.invalidated_at is null
    order by c.id
    for update
  loop
    update public.approval_challenges c set invalidated_at = now() where c.id = v_challenge.id;
  end loop;

  return query select true, v_reason, p_device_id, v_cancelled;
end;
$$;

revoke execute on function public.revoke_device(uuid, uuid, text, text) from public, anon, authenticated;
grant execute on function public.revoke_device(uuid, uuid, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: cancel_order replacement (design §2.7, §6.3)
-- Also invalidates open approval challenges for the order.
-- ---------------------------------------------------------------------------

create or replace function public.cancel_order(
  p_order_id uuid,
  p_user_id uuid,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text, order_id uuid, status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_device_id uuid;
  v_user_id uuid;
  v_status text;
  v_released integer;
begin
  if p_order_id is null or p_user_id is null or p_actor_type is null then
    raise exception 'cancel_order: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select o.device_id, o.user_id into v_device_id, v_user_id
  from public.orders o where o.id = p_order_id;

  if not found or v_user_id <> p_user_id then
    return query select false, 'not_found'::text, null::uuid, null::text;
    return;
  end if;

  if v_device_id is not null then
    perform 1 from public.devices d where d.id = v_device_id for share;
  end if;

  select o.status, o.user_id into v_status, v_user_id
  from public.orders o where o.id = p_order_id
  for update;

  if not found or v_user_id <> p_user_id then
    return query select false, 'not_found'::text, null::uuid, null::text;
    return;
  end if;

  if v_status not in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL', 'APPROVED') then
    return query select false, 'invalid_order_status'::text, p_order_id, v_status;
    return;
  end if;

  perform 1 from public.approval_challenges c
    where c.order_id = p_order_id and c.consumed_at is null and c.invalidated_at is null
    order by c.id for update;

  update public.orders o set status = 'CANCELLED' where o.id = p_order_id;

  update public.spending_records s set status = 'released'
    where s.order_id = p_order_id and s.status = 'reserved';
  get diagnostics v_released = row_count;

  -- Invalidate open challenges
  update public.approval_challenges c
    set invalidated_at = now()
    where c.order_id = p_order_id and c.consumed_at is null and c.invalidated_at is null;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, p_actor_type, p_actor_id, 'order.status_changed', 'order', p_order_id::text,
     jsonb_build_object('from', v_status, 'to', 'CANCELLED', 'reason', 'cancelled')),
    (p_user_id, p_actor_type, p_actor_id, 'order.cancelled', 'order', p_order_id::text, '{}'::jsonb);
  if v_released > 0 then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (p_user_id, p_actor_type, p_actor_id, 'spending.released', 'order', p_order_id::text,
            jsonb_build_object('reason', 'cancelled'));
  end if;

  return query select true, 'cancelled'::text, p_order_id, 'CANCELLED'::text;
end;
$$;

revoke execute on function public.cancel_order(uuid, uuid, text, text) from public, anon, authenticated;
grant execute on function public.cancel_order(uuid, uuid, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: expire_order replacement (design §2.6, §3.9, §6.3)
-- Also invalidates open approval challenges for the expired order.
-- ---------------------------------------------------------------------------

create or replace function public.expire_order(p_order_id uuid)
returns table (ok boolean, reason text, status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_device_id uuid;
  v_user_id uuid;
  v_status text;
  v_expires timestamptz;
  v_released integer;
begin
  if p_order_id is null then
    raise exception 'expire_order: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select o.device_id into v_device_id from public.orders o where o.id = p_order_id;
  if not found then
    return query select false, 'order_not_found'::text, null::text;
    return;
  end if;

  if v_device_id is not null then
    perform 1 from public.devices d where d.id = v_device_id for share;
  end if;

  select o.status, o.quote_expires_at, o.user_id into v_status, v_expires, v_user_id
  from public.orders o where o.id = p_order_id
  for update;

  if v_status not in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
     or v_expires is null or v_expires > now() then
    return query select false, 'not_expirable'::text, v_status;
    return;
  end if;

  -- Lock challenges in deterministic order before any spending row.
  perform 1 from public.approval_challenges c
    where c.order_id = p_order_id and c.consumed_at is null and c.invalidated_at is null
    order by c.id for update;

  update public.orders o set status = 'EXPIRED' where o.id = p_order_id;

  update public.spending_records s set status = 'released'
    where s.order_id = p_order_id and s.status = 'reserved';
  get diagnostics v_released = row_count;

  -- Invalidate open challenges
  update public.approval_challenges c
    set invalidated_at = now()
    where c.order_id = p_order_id and c.consumed_at is null and c.invalidated_at is null;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (v_user_id, 'system', null, 'order.status_changed', 'order', p_order_id::text,
     jsonb_build_object('from', v_status, 'to', 'EXPIRED', 'reason', 'quote_expired')),
    (v_user_id, 'system', null, 'order.expired', 'order', p_order_id::text, '{}'::jsonb);
  if v_released > 0 then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, 'system', null, 'spending.released', 'order', p_order_id::text,
            jsonb_build_object('reason', 'expired'));
  end if;

  return query select true, 'expired'::text, 'EXPIRED'::text;
end;
$$;

revoke execute on function public.expire_order(uuid) from public, anon, authenticated;
grant execute on function public.expire_order(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: issue_approval_challenge (design §3.2, §6.3)
-- Global lock order: device FOR SHARE -> order FOR UPDATE -> open challenge FOR UPDATE
-- ---------------------------------------------------------------------------

create or replace function public.issue_approval_challenge(
  p_order_id uuid,
  p_device_id uuid,
  p_user_id uuid,
  p_key_version integer,
  p_nonce text,
  p_canonical text,
  p_payload_sha256 text,
  p_amount_minor bigint,
  p_currency char(3),
  p_expires_at timestamptz
)
returns table (
  ok boolean,
  reason text,
  challenge_id uuid,
  nonce text,
  canonical_payload text,
  expires_at timestamptz,
  key_version integer
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_dev_user_id uuid;
  v_dev_status text;
  v_dev_kv integer;
  v_order_user_id uuid;
  v_order_device_id uuid;
  v_order_status text;
  v_quote_expires timestamptz;
  v_total_minor bigint;
  v_currency char(3);
  v_existing_id uuid;
  v_existing_nonce text;
  v_existing_canonical text;
  v_existing_expires timestamptz;
  v_existing_kv integer;
  v_new_id uuid;
begin
  if p_order_id is null or p_device_id is null or p_user_id is null
     or p_key_version is null or p_nonce is null or p_canonical is null
     or p_payload_sha256 is null or p_amount_minor is null or p_currency is null
     or p_expires_at is null then
    raise exception 'issue_approval_challenge: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  -- Unlocked check of order ownership and device assignment
  select o.user_id, o.device_id into v_order_user_id, v_order_device_id
  from public.orders o where o.id = p_order_id;

  if not found or v_order_user_id <> p_user_id or v_order_device_id <> p_device_id then
    return query select false, 'order_not_found'::text, null::uuid, null::text, null::text, null::timestamptz, null::integer;
    return;
  end if;

  -- 1) device FOR SHARE
  select d.user_id, d.status, d.key_version into v_dev_user_id, v_dev_status, v_dev_kv
  from public.devices d where d.id = p_device_id for share;

  if not found or v_dev_user_id <> p_user_id or v_dev_status <> 'active' then
    return query select false, 'device_revoked'::text, null::uuid, null::text, null::text, null::timestamptz, null::integer;
    return;
  end if;

  -- 2) order FOR UPDATE
  select o.status, o.quote_expires_at, o.total_minor, o.currency
    into v_order_status, v_quote_expires, v_total_minor, v_currency
  from public.orders o where o.id = p_order_id for update;

  if v_order_status <> 'AWAITING_APPROVAL' then
    return query select false, 'invalid_order_status'::text, null::uuid, null::text, null::text, null::timestamptz, null::integer;
    return;
  end if;

  if v_quote_expires is null or v_quote_expires <= now() then
    update public.orders o set status = 'EXPIRED' where o.id = p_order_id;
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values
      (p_user_id, 'system', null, 'order.status_changed', 'order', p_order_id::text,
       jsonb_build_object('from', 'AWAITING_APPROVAL', 'to', 'EXPIRED', 'reason', 'quote_expired')),
      (p_user_id, 'system', null, 'order.expired', 'order', p_order_id::text, '{}'::jsonb);
    return query select false, 'quote_expired'::text, null::uuid, null::text, null::text, null::timestamptz, null::integer;
    return;
  end if;

  if p_amount_minor <> v_total_minor or p_currency <> v_currency or p_expires_at > v_quote_expires then
    raise exception 'issue_approval_challenge: values do not match order or quote expiry'
      using errcode = 'check_violation';
  end if;

  -- 3) open challenge rows for (order_id, device_id) FOR UPDATE
  select c.id, c.nonce, c.canonical_payload, c.expires_at, c.key_version
    into v_existing_id, v_existing_nonce, v_existing_canonical, v_existing_expires, v_existing_kv
  from public.approval_challenges c
  where c.order_id = p_order_id
    and c.device_id = p_device_id
    and c.consumed_at is null
    and c.invalidated_at is null
    and c.expires_at > now()
  for update;

  if found then
    return query select true, 'existing'::text, v_existing_id, v_existing_nonce, v_existing_canonical, v_existing_expires, v_existing_kv;
    return;
  end if;

  -- Invalidate any expired open challenges
  update public.approval_challenges c
    set invalidated_at = now()
    where c.order_id = p_order_id
      and c.device_id = p_device_id
      and c.consumed_at is null
      and c.invalidated_at is null
      and c.expires_at <= now();

  -- Insert fresh challenge
  insert into public.approval_challenges (
    order_id, device_id, user_id, nonce, canonical_payload, payload_sha256,
    amount_minor, currency, key_version, expires_at
  )
  values (
    p_order_id, p_device_id, p_user_id, p_nonce, p_canonical, p_payload_sha256,
    p_amount_minor, p_currency, p_key_version, p_expires_at
  )
  returning id into v_new_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (p_user_id, 'device', p_device_id::text, 'approval.challenge_issued', 'challenge', v_new_id::text,
          jsonb_build_object('order_id', p_order_id, 'expires_at', p_expires_at));

  return query select true, 'issued'::text, v_new_id, p_nonce, p_canonical, p_expires_at, p_key_version;
end;
$$;

revoke execute on function public.issue_approval_challenge(uuid, uuid, uuid, integer, text, text, text, bigint, char, timestamptz)
  from public, anon, authenticated;
grant execute on function public.issue_approval_challenge(uuid, uuid, uuid, integer, text, text, text, bigint, char, timestamptz)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: record_gadget_denial (design §3.3, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.record_gadget_denial(
  p_challenge_id uuid,
  p_device_id uuid,
  p_user_id uuid,
  p_nonce text,
  p_payload_sha256 text,
  p_key_version integer,
  p_actor_type text,
  p_actor_id text
)
returns table (ok boolean, reason text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_order_id uuid;
  v_device_id uuid;
  v_user_id uuid;
  v_dev_status text;
  v_order_status text;
  v_consumed timestamptz;
  v_invalidated timestamptz;
  v_expires timestamptz;
  v_ch_kv integer;
  v_ch_nonce text;
  v_ch_hash text;
begin
  if p_challenge_id is null or p_device_id is null or p_user_id is null
     or p_nonce is null or p_payload_sha256 is null or p_key_version is null
     or p_actor_type is null then
    raise exception 'record_gadget_denial: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select c.order_id, c.device_id, c.user_id
    into v_order_id, v_device_id, v_user_id
  from public.approval_challenges c where c.id = p_challenge_id;

  if not found or v_device_id <> p_device_id or v_user_id <> p_user_id then
    return query select false, 'challenge_not_found'::text;
    return;
  end if;

  -- 1) device FOR SHARE
  select d.status into v_dev_status from public.devices d where d.id = p_device_id for share;
  if not found or v_dev_status <> 'active' then
    return query select false, 'device_revoked'::text;
    return;
  end if;

  -- 2) order FOR UPDATE
  select o.status into v_order_status from public.orders o where o.id = v_order_id for update;
  if v_order_status <> 'AWAITING_APPROVAL' then
    return query select false, 'invalid_order_status'::text;
    return;
  end if;

  -- 3) challenge FOR UPDATE
  select c.consumed_at, c.invalidated_at, c.expires_at, c.key_version, c.nonce, c.payload_sha256
    into v_consumed, v_invalidated, v_expires, v_ch_kv, v_ch_nonce, v_ch_hash
  from public.approval_challenges c where c.id = p_challenge_id for update;

  if v_consumed is not null or v_invalidated is not null then
    return query select false, 'challenge_consumed'::text;
    return;
  end if;

  if v_expires <= now() then
    return query select false, 'challenge_expired'::text;
    return;
  end if;

  if v_ch_kv <> p_key_version then
    return query select false, 'key_version_mismatch'::text;
    return;
  end if;

  if v_ch_nonce <> p_nonce then
    return query select false, 'nonce_mismatch'::text;
    return;
  end if;

  -- Record denial: consumed_at is null, approved_at is null
  insert into public.approvals (
    order_id, user_id, device_id, approval_type, decision,
    verification_status, nonce, signed_payload_hash, key_version,
    consumed_at, approved_at, expires_at
  )
  values (
    v_order_id, p_user_id, p_device_id, 'gadget', 'denied',
    'pending', p_nonce, p_payload_sha256, p_key_version,
    null, null, v_expires
  );

  update public.approval_challenges c set consumed_at = now() where c.id = p_challenge_id;
  update public.orders o set status = 'DENIED' where o.id = v_order_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, p_actor_type, p_actor_id, 'order.status_changed', 'order', v_order_id::text,
     jsonb_build_object('from', 'AWAITING_APPROVAL', 'to', 'DENIED', 'reason', 'denied')),
    (p_user_id, p_actor_type, p_actor_id, 'approval.denied', 'order', v_order_id::text,
     jsonb_build_object('challenge_id', p_challenge_id));

  return query select true, 'denied'::text;
end;
$$;

revoke execute on function public.record_gadget_denial(uuid, uuid, uuid, text, text, integer, text, text)
  from public, anon, authenticated;
grant execute on function public.record_gadget_denial(uuid, uuid, uuid, text, text, integer, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: accept_gadget_approval (design §3.3, §6.3)
-- Global lock order: device FOR SHARE -> order FOR UPDATE -> challenge FOR UPDATE
-- -> payment_method FOR SHARE -> reserve_spending advisory lock -> insert attempt.
-- ---------------------------------------------------------------------------

create or replace function public.accept_gadget_approval(
  p_challenge_id uuid,
  p_device_id uuid,
  p_user_id uuid,
  p_nonce text,
  p_payload_sha256 text,
  p_policy_currency char(3),
  p_daily_limit_minor bigint,
  p_timezone text,
  p_phone_threshold_minor bigint
)
returns table (
  ok boolean,
  reason text,
  attempt_id uuid,
  idempotency_key text,
  vault_token_reference text
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_order_id uuid;
  v_device_id uuid;
  v_user_id uuid;
  v_ch_amount bigint;
  v_ch_currency char(3);
  v_dev_status text;
  v_dev_kv integer;
  v_order_status text;
  v_quote_expires timestamptz;
  v_total_minor bigint;
  v_currency char(3);
  v_consumed timestamptz;
  v_invalidated timestamptz;
  v_expires timestamptz;
  v_ch_kv integer;
  v_ch_nonce text;
  v_ch_hash text;
  v_pm_id uuid;
  v_vault_token text;
  v_sp_ok boolean;
  v_sp_reason text;
  v_attempt_id uuid;
  v_idem_key text;
begin
  if p_challenge_id is null or p_device_id is null or p_user_id is null
     or p_nonce is null or p_payload_sha256 is null or p_policy_currency is null
     or p_daily_limit_minor is null or p_timezone is null or p_phone_threshold_minor is null then
    raise exception 'accept_gadget_approval: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  -- Read challenge unlocked to discover ids
  select c.order_id, c.device_id, c.user_id, c.amount_minor, c.currency
    into v_order_id, v_device_id, v_user_id, v_ch_amount, v_ch_currency
  from public.approval_challenges c where c.id = p_challenge_id;

  if not found or v_device_id <> p_device_id or v_user_id <> p_user_id then
    return query select false, 'challenge_not_found'::text, null::uuid, null::text, null::text;
    return;
  end if;

  -- 1) device FOR SHARE
  select d.status, d.key_version into v_dev_status, v_dev_kv
  from public.devices d where d.id = p_device_id for share;

  if not found or v_dev_status <> 'active' then
    return query select false, 'device_revoked'::text, null::uuid, null::text, null::text;
    return;
  end if;

  -- 2) order FOR UPDATE
  select o.status, o.quote_expires_at, o.total_minor, o.currency
    into v_order_status, v_quote_expires, v_total_minor, v_currency
  from public.orders o where o.id = v_order_id for update;

  -- 3) challenge FOR UPDATE
  select c.consumed_at, c.invalidated_at, c.expires_at, c.key_version, c.nonce, c.payload_sha256
    into v_consumed, v_invalidated, v_expires, v_ch_kv, v_ch_nonce, v_ch_hash
  from public.approval_challenges c where c.id = p_challenge_id for update;

  if v_consumed is not null or v_invalidated is not null then
    return query select false, 'challenge_consumed'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_expires <= now() then
    return query select false, 'challenge_expired'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_dev_kv <> v_ch_kv then
    return query select false, 'key_version_mismatch'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_ch_nonce <> p_nonce then
    return query select false, 'nonce_mismatch'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_ch_hash <> p_payload_sha256 then
    return query select false, 'hash_mismatch'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_order_status <> 'AWAITING_APPROVAL' then
    return query select false, 'invalid_order_status'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_quote_expires is null or v_quote_expires <= now() then
    update public.orders o set status = 'EXPIRED' where o.id = v_order_id;
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values
      (p_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
       jsonb_build_object('from', 'AWAITING_APPROVAL', 'to', 'EXPIRED', 'reason', 'quote_expired')),
      (p_user_id, 'system', null, 'order.expired', 'order', v_order_id::text, '{}'::jsonb);
    return query select false, 'quote_expired'::text, null::uuid, null::text, null::text;
    return;
  end if;

  if v_total_minor <> v_ch_amount or v_currency <> v_ch_currency then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (p_user_id, 'system', null, 'approval.data_mismatch', 'order', v_order_id::text,
            jsonb_build_object('order_amount', v_total_minor, 'challenge_amount', v_ch_amount));
    raise exception 'accept_gadget_approval: amount/currency mismatch between order and challenge'
      using errcode = 'check_violation';
  end if;

  if v_total_minor > p_phone_threshold_minor then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (p_user_id, 'device', p_device_id::text, 'approval.phone_required_rejected', 'order', v_order_id::text,
            jsonb_build_object('total_minor', v_total_minor, 'threshold', p_phone_threshold_minor));
    return query select false, 'phone_approval_required'::text, null::uuid, null::text, null::text;
    return;
  end if;


  -- 4) payment_methods FOR SHARE
  select pm.id, pm.vault_token_reference into v_pm_id, v_vault_token
  from public.payment_methods pm
  where pm.user_id = p_user_id and pm.status = 'active'
  for share;

  if not found or v_vault_token is null or v_vault_token = '' then
    return query select false, 'no_payment_method'::text, null::uuid, null::text, null::text;
    return;
  end if;

  -- 5) reserve_spending
  select r.success, r.reason into v_sp_ok, v_sp_reason
  from public.reserve_spending(p_user_id, v_order_id, v_total_minor, p_policy_currency, p_daily_limit_minor, p_timezone) r;

  if not v_sp_ok then
    if v_sp_reason = 'daily_limit_exceeded' then
      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values (p_user_id, 'device', p_device_id::text, 'spending.cap_rejected', 'order', v_order_id::text,
              jsonb_build_object('total_minor', v_total_minor, 'limit_minor', p_daily_limit_minor));
      return query select false, 'daily_limit_exceeded'::text, null::uuid, null::text, null::text;
      return;
    elsif v_sp_reason in ('mixed_currency', 'currency_mismatch') then
      return query select false, 'currency_not_supported'::text, null::uuid, null::text, null::text;
      return;
    else
      return query select false, v_sp_reason, null::uuid, null::text, null::text;
      return;
    end if;
  end if;

  -- 6) Commit approval, mandate, transition, payment attempt
  insert into public.approvals (
    order_id, user_id, device_id, approval_type, decision,
    key_version, approved_at, expires_at, verification_status,
    consumed_at, nonce, signed_payload_hash
  )
  values (
    v_order_id, p_user_id, p_device_id, 'gadget', 'approved',
    v_ch_kv, now(), v_expires, 'verified',
    now(), v_ch_nonce, v_ch_hash
  );

  update public.approval_challenges c set consumed_at = now() where c.id = p_challenge_id;

  insert into public.mandates (
    order_id, user_id, mandate_type, provider_mandate_reference, status, expires_at
  )
  values (
    v_order_id, p_user_id, 'clawpay.gadget_cart.v1', v_ch_hash, 'verified', v_quote_expires
  );

  update public.orders o set status = 'APPROVED' where o.id = v_order_id;
  update public.orders o set status = 'PAYMENT_PENDING' where o.id = v_order_id;

  v_idem_key := 'clawpay-' || v_order_id::text || '-1';

  insert into public.payment_attempts (
    order_id, payment_method_id, provider, status, idempotency_key,
    amount_minor, currency, claimed_until
  )
  values (
    v_order_id, v_pm_id, 'paypal', 'initiated', v_idem_key,
    v_total_minor, v_currency, now() + interval '30 seconds'
  )
  returning id into v_attempt_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values
    (p_user_id, 'device', p_device_id::text, 'order.status_changed', 'order', v_order_id::text,
     jsonb_build_object('from', 'AWAITING_APPROVAL', 'to', 'APPROVED', 'reason', 'gadget_approved')),
    (p_user_id, 'device', p_device_id::text, 'order.status_changed', 'order', v_order_id::text,
     jsonb_build_object('from', 'APPROVED', 'to', 'PAYMENT_PENDING', 'reason', 'payment_initiated')),
    (p_user_id, 'device', p_device_id::text, 'approval.verified', 'order', v_order_id::text,
     jsonb_build_object('challenge_id', p_challenge_id, 'mandate_reference', v_ch_hash)),
    (p_user_id, 'device', p_device_id::text, 'payment.initiated', 'payment_attempt', v_attempt_id::text,
     jsonb_build_object('idempotency_key', v_idem_key));

  return query select true, 'approved'::text, v_attempt_id, v_idem_key, v_vault_token;
end;
$$;

revoke execute on function public.accept_gadget_approval(uuid, uuid, uuid, text, text, char, bigint, text, bigint)
  from public, anon, authenticated;
grant execute on function public.accept_gadget_approval(uuid, uuid, uuid, text, text, char, bigint, text, bigint)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: mark_attempt_sent (design §3.8, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.mark_attempt_sent(p_attempt_id uuid)
returns table (previous timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_prev timestamptz;
begin
  if p_attempt_id is null then
    raise exception 'mark_attempt_sent: attempt_id must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select a.first_sent_at into v_prev
  from public.payment_attempts a
  where a.id = p_attempt_id
  for update;

  if not found then
    raise exception 'mark_attempt_sent: attempt not found'
      using errcode = 'no_data_found';
  end if;

  if v_prev is null then
    update public.payment_attempts a set first_sent_at = now() where a.id = p_attempt_id;
  end if;

  return query select v_prev;
end;
$$;

revoke execute on function public.mark_attempt_sent(uuid) from public, anon, authenticated;
grant execute on function public.mark_attempt_sent(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: mark_capture_sent (design §3.7, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.mark_capture_sent(p_attempt_id uuid)
returns table (previous timestamptz, send_count integer)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_prev timestamptz;
  v_count integer;
begin
  if p_attempt_id is null then
    raise exception 'mark_capture_sent: attempt_id must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select a.capture_sent_at, a.capture_send_count
    into v_prev, v_count
  from public.payment_attempts a
  where a.id = p_attempt_id
  for update;

  if not found then
    raise exception 'mark_capture_sent: attempt not found'
      using errcode = 'no_data_found';
  end if;

  v_count := v_count + 1;

  update public.payment_attempts a
    set capture_sent_at = coalesce(a.capture_sent_at, now()),
        capture_send_count = v_count
    where a.id = p_attempt_id;

  return query select v_prev, v_count;
end;
$$;

revoke execute on function public.mark_capture_sent(uuid) from public, anon, authenticated;
grant execute on function public.mark_capture_sent(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: apply_payment_result (design §3.8, §6.3)
-- Lock order: order FOR UPDATE -> spending_record FOR UPDATE -> attempt FOR UPDATE.
-- ---------------------------------------------------------------------------

create or replace function public.apply_payment_result(
  p_attempt_id uuid,
  p_classification text,
  p_provider_order_id text,
  p_provider_capture_id text,
  p_provider_status text,
  p_failure_code text,
  p_source text
)
returns table (ok boolean, reason text, new_status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_order_id uuid;
  v_user_id uuid;
  v_order_status text;
  v_sp_id uuid;
  v_sp_status text;
  v_sp_day date;
  v_att_status text;
  v_exist_order_id text;
  v_exist_capture_id text;
  v_completed_at timestamptz;
  v_now timestamptz := now();
begin
  if p_attempt_id is null or p_classification is null or p_source is null then
    raise exception 'apply_payment_result: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  if p_classification not in ('completed', 'pending', 'failed', 'uncertain') then
    raise exception 'apply_payment_result: invalid classification %', p_classification
      using errcode = 'check_violation';
  end if;

  if p_source not in ('first_create', 'replay', 'poll', 'webhook') then
    raise exception 'apply_payment_result: invalid source %', p_source
      using errcode = 'check_violation';
  end if;

  -- Unlocked read to get order_id and user_id
  select a.order_id, o.user_id
    into v_order_id, v_user_id
  from public.payment_attempts a
  join public.orders o on o.id = a.order_id
  where a.id = p_attempt_id;

  if not found then
    return query select false, 'attempt_not_found'::text, null::text;
    return;
  end if;

  -- 1) order FOR UPDATE
  select o.status into v_order_status
  from public.orders o where o.id = v_order_id for update;

  -- 2) spending_record FOR UPDATE
  select s.id, s.status, s.spending_day
    into v_sp_id, v_sp_status, v_sp_day
  from public.spending_records s
  where s.order_id = v_order_id
  for update;

  -- 3) attempt FOR UPDATE
  select a.status, a.provider_order_id, a.provider_capture_id, a.completed_at
    into v_att_status, v_exist_order_id, v_exist_capture_id, v_completed_at
  from public.payment_attempts a
  where a.id = p_attempt_id
  for update;

  -- Conflicting provider ids check
  if p_provider_order_id is not null and v_exist_order_id is not null and p_provider_order_id <> v_exist_order_id then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, 'system', null, 'payment.conflicting_result', 'payment_attempt', p_attempt_id::text,
            jsonb_build_object('kind', 'order_id_mismatch', 'existing', v_exist_order_id, 'incoming', p_provider_order_id));
    return query select false, 'conflicting_provider_order_id'::text, v_att_status;
    return;
  end if;

  if p_provider_capture_id is not null and v_exist_capture_id is not null and p_provider_capture_id <> v_exist_capture_id then
    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, 'system', null, 'payment.conflicting_result', 'payment_attempt', p_attempt_id::text,
            jsonb_build_object('kind', 'capture_id_mismatch', 'existing', v_exist_capture_id, 'incoming', p_provider_capture_id));
    return query select false, 'conflicting_provider_capture_id'::text, v_att_status;
    return;
  end if;

  -- Store provider ids and status if provided, and always clear claimed_until
  update public.payment_attempts a
    set provider_order_id = coalesce(a.provider_order_id, p_provider_order_id),
        provider_capture_id = coalesce(a.provider_capture_id, p_provider_capture_id),
        provider_status = coalesce(p_provider_status, a.provider_status),
        claimed_until = null
    where a.id = p_attempt_id;

  -- Monotonic state transitions (design §3.8)
  if v_att_status in ('initiated', 'unknown', 'pending') then
    if p_classification = 'completed' then
      update public.payment_attempts a
        set status = 'completed', completed_at = coalesce(a.completed_at, v_now)
        where a.id = p_attempt_id;
      update public.orders o set status = 'PAID' where o.id = v_order_id;
      update public.spending_records s set status = 'committed'
        where s.order_id = v_order_id and s.status = 'reserved';

      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values
        (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
         jsonb_build_object('from', v_order_status, 'to', 'PAID', 'reason', 'payment_completed')),
        (v_user_id, 'system', null, 'spending.committed', 'order', v_order_id::text,
         jsonb_build_object('source', p_source)),
        (v_user_id, 'system', null, 'payment.completed', 'payment_attempt', p_attempt_id::text,
         jsonb_build_object('source', p_source, 'capture_id', coalesce(p_provider_capture_id, v_exist_capture_id)));

      return query select true, 'applied'::text, 'completed'::text;
      return;

    elsif p_classification = 'pending' then
      update public.payment_attempts a set status = 'pending' where a.id = p_attempt_id;
      if v_order_status <> 'PAYMENT_PENDING' then
        update public.orders o set status = 'PAYMENT_PENDING' where o.id = v_order_id;
        insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
        values (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
                jsonb_build_object('from', v_order_status, 'to', 'PAYMENT_PENDING', 'reason', 'payment_pending'));
      end if;

      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values (v_user_id, 'system', null, 'payment.pending', 'payment_attempt', p_attempt_id::text,
              jsonb_build_object('source', p_source));

      return query select true, 'applied'::text, 'pending'::text;
      return;

    elsif p_classification = 'failed' then
      update public.payment_attempts a
        set status = 'failed', failure_code = p_failure_code
        where a.id = p_attempt_id;
      update public.orders o set status = 'PAYMENT_FAILED' where o.id = v_order_id;
      update public.spending_records s set status = 'released'
        where s.order_id = v_order_id and s.status = 'reserved';

      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values
        (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
         jsonb_build_object('from', v_order_status, 'to', 'PAYMENT_FAILED', 'reason', 'payment_failed')),
        (v_user_id, 'system', null, 'spending.released', 'order', v_order_id::text,
         jsonb_build_object('reason', 'payment_failed')),
        (v_user_id, 'system', null, 'payment.failed', 'payment_attempt', p_attempt_id::text,
         jsonb_build_object('source', p_source, 'failure_code', p_failure_code));

      return query select true, 'applied'::text, 'failed'::text;
      return;

    elsif p_classification = 'uncertain' then
      if v_att_status = 'pending' then
        -- Refinement: uncertain on pending attempt is ignored
        return query select true, 'ignored_pending'::text, 'pending'::text;
        return;
      else
        update public.payment_attempts a set status = 'unknown' where a.id = p_attempt_id;
        if v_order_status <> 'REQUIRES_RECONCILIATION' then
          update public.orders o set status = 'REQUIRES_RECONCILIATION' where o.id = v_order_id;
          insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
          values (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
                  jsonb_build_object('from', v_order_status, 'to', 'REQUIRES_RECONCILIATION', 'reason', 'payment_uncertain'));
        end if;

        insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
        values (v_user_id, 'system', null, 'payment.uncertain', 'payment_attempt', p_attempt_id::text,
                jsonb_build_object('source', p_source));

        return query select true, 'applied'::text, 'unknown'::text;
        return;
      end if;
    end if;

  elsif v_att_status = 'completed' then
    if p_classification = 'failed' then
      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values (v_user_id, 'system', null, 'payment.conflicting_result', 'payment_attempt', p_attempt_id::text,
              jsonb_build_object('kind', 'failed_after_completed', 'source', p_source));
    end if;
    return query select true, 'already_completed'::text, 'completed'::text;
    return;

  elsif v_att_status = 'failed' then
    if p_classification = 'completed' then
      -- Late completion (§3.6, Q6)
      update public.payment_attempts a
        set status = 'completed', completed_at = coalesce(a.completed_at, v_now)
        where a.id = p_attempt_id;
      update public.orders o set status = 'PAID' where o.id = v_order_id;

      -- Insert committed spending row on original spending_day
      insert into public.spending_records (user_id, order_id, amount_minor, currency, spending_day, status)
      select o.user_id, o.id, o.total_minor, o.currency, coalesce(v_sp_day, v_now::date), 'committed'
      from public.orders o where o.id = v_order_id;

      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values
        (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
         jsonb_build_object('from', 'PAYMENT_FAILED', 'to', 'PAID', 'reason', 'late_completion')),
        (v_user_id, 'system', null, 'spending.committed', 'order', v_order_id::text,
         jsonb_build_object('late', true, 'source', p_source)),
        (v_user_id, 'system', null, 'payment.late_completion', 'payment_attempt', p_attempt_id::text,
         jsonb_build_object('source', p_source));

      return query select true, 'late_completed'::text, 'completed'::text;
      return;
    else
      return query select true, 'already_failed'::text, 'failed'::text;
      return;
    end if;
  end if;

  return query select true, 'no_change'::text, v_att_status;
end;
$$;

revoke execute on function public.apply_payment_result(uuid, text, text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.apply_payment_result(uuid, text, text, text, text, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: claim_reconcilable_attempts (design §3.9, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.claim_reconcilable_attempts(
  p_limit integer,
  p_lease_seconds integer default 60
)
returns table (
  attempt_id uuid,
  order_id uuid,
  user_id uuid,
  provider_order_id text,
  provider_capture_id text,
  idempotency_key text,
  amount_minor bigint,
  currency char(3),
  first_sent_at timestamptz,
  capture_sent_at timestamptz,
  capture_send_count integer,
  vault_token_reference text,
  created_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_lease_interval interval;
begin
  if p_limit is null or p_limit <= 0 then
    raise exception 'claim_reconcilable_attempts: limit must be positive'
      using errcode = 'check_violation';
  end if;

  v_lease_interval := (coalesce(p_lease_seconds, 60) || ' seconds')::interval;

  return query
  with locked_attempts as (
    select a.id
    from public.payment_attempts a
    where a.status in ('initiated', 'pending', 'unknown')
      and a.exhausted_at is null
      and (a.claimed_until is null or a.claimed_until < now())
      and (a.status <> 'initiated' or a.created_at < now() - interval '60 seconds')
    order by a.created_at
    limit p_limit
    for update skip locked
  ),
  updated as (
    update public.payment_attempts a
    set claimed_until = now() + v_lease_interval
    from locked_attempts la
    where a.id = la.id
    returning a.id, a.order_id, a.provider_order_id, a.provider_capture_id,
              a.idempotency_key, a.amount_minor, a.currency, a.first_sent_at,
              a.capture_sent_at, a.capture_send_count, a.payment_method_id, a.created_at
  )
  select
    u.id as attempt_id,
    u.order_id,
    o.user_id,
    u.provider_order_id,
    u.provider_capture_id,
    u.idempotency_key,
    u.amount_minor,
    u.currency,
    u.first_sent_at,
    u.capture_sent_at,
    u.capture_send_count,
    pm.vault_token_reference,
    u.created_at
  from updated u
  join public.orders o on o.id = u.order_id
  left join public.payment_methods pm on pm.id = coalesce(
    u.payment_method_id,
    (select m.id from public.payment_methods m where m.user_id = o.user_id and m.status = 'active' limit 1)
  );
end;
$$;

revoke execute on function public.claim_reconcilable_attempts(integer, integer)
  from public, anon, authenticated;
grant execute on function public.claim_reconcilable_attempts(integer, integer)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: claim_reconcilable_attempt (design §3.9, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.claim_reconcilable_attempt(
  p_order_id uuid,
  p_lease_seconds integer default 60
)
returns table (
  attempt_id uuid,
  order_id uuid,
  user_id uuid,
  provider_order_id text,
  provider_capture_id text,
  idempotency_key text,
  amount_minor bigint,
  currency char(3),
  first_sent_at timestamptz,
  capture_sent_at timestamptz,
  capture_send_count integer,
  vault_token_reference text,
  created_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_lease_interval interval;
begin
  if p_order_id is null then
    raise exception 'claim_reconcilable_attempt: order_id must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  v_lease_interval := (coalesce(p_lease_seconds, 60) || ' seconds')::interval;

  return query
  with locked_attempt as (
    select a.id
    from public.payment_attempts a
    where a.order_id = p_order_id
      and a.status in ('initiated', 'pending', 'unknown')
      and a.exhausted_at is null
      and (a.claimed_until is null or a.claimed_until < now())
      and (a.status <> 'initiated' or a.created_at < now() - interval '60 seconds')
    for update skip locked
  ),
  updated as (
    update public.payment_attempts a
    set claimed_until = now() + v_lease_interval
    from locked_attempt la
    where a.id = la.id
    returning a.id, a.order_id, a.provider_order_id, a.provider_capture_id,
              a.idempotency_key, a.amount_minor, a.currency, a.first_sent_at,
              a.capture_sent_at, a.capture_send_count, a.payment_method_id, a.created_at
  )
  select
    u.id as attempt_id,
    u.order_id,
    o.user_id,
    u.provider_order_id,
    u.provider_capture_id,
    u.idempotency_key,
    u.amount_minor,
    u.currency,
    u.first_sent_at,
    u.capture_sent_at,
    u.capture_send_count,
    pm.vault_token_reference,
    u.created_at
  from updated u
  join public.orders o on o.id = u.order_id
  left join public.payment_methods pm on pm.id = coalesce(
    u.payment_method_id,
    (select m.id from public.payment_methods m where m.user_id = o.user_id and m.status = 'active' limit 1)
  );
end;
$$;

revoke execute on function public.claim_reconcilable_attempt(uuid, integer)
  from public, anon, authenticated;
grant execute on function public.claim_reconcilable_attempt(uuid, integer)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: mark_attempt_exhausted (design §3.9, §6.3)
-- Lock order: order FOR UPDATE -> attempt FOR UPDATE.
-- ---------------------------------------------------------------------------

create or replace function public.mark_attempt_exhausted(p_attempt_id uuid)
returns table (ok boolean, order_status text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_order_id uuid;
  v_user_id uuid;
  v_order_status text;
  v_was_exhausted timestamptz;
begin
  if p_attempt_id is null then
    raise exception 'mark_attempt_exhausted: attempt_id must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select a.order_id, o.user_id into v_order_id, v_user_id
  from public.payment_attempts a
  join public.orders o on o.id = a.order_id
  where a.id = p_attempt_id;

  if not found then
    return query select false, null::text;
    return;
  end if;

  -- 1) order FOR UPDATE
  select o.status into v_order_status
  from public.orders o where o.id = v_order_id for update;

  -- 2) attempt FOR UPDATE
  select a.exhausted_at into v_was_exhausted
  from public.payment_attempts a where a.id = p_attempt_id for update;

  if v_was_exhausted is null then
    update public.payment_attempts a
      set exhausted_at = now(), claimed_until = null
      where a.id = p_attempt_id;

    if v_order_status = 'PAYMENT_PENDING' then
      update public.orders o set status = 'REQUIRES_RECONCILIATION' where o.id = v_order_id;
      v_order_status := 'REQUIRES_RECONCILIATION';
      insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
      values (v_user_id, 'system', null, 'order.status_changed', 'order', v_order_id::text,
              jsonb_build_object('from', 'PAYMENT_PENDING', 'to', 'REQUIRES_RECONCILIATION', 'reason', 'reconcile_exhausted'));
    end if;

    insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
    values (v_user_id, 'system', null, 'payment.reconcile_exhausted', 'payment_attempt', p_attempt_id::text, '{}'::jsonb);
  end if;

  return query select true, v_order_status;
end;
$$;

revoke execute on function public.mark_attempt_exhausted(uuid) from public, anon, authenticated;
grant execute on function public.mark_attempt_exhausted(uuid) to service_role;

-- ---------------------------------------------------------------------------
-- RPC: activate_payment_method (design §3.10, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.activate_payment_method(
  p_payment_method_id uuid,
  p_vault_token_reference text,
  p_provider_customer_reference text,
  p_payer_email_masked text
)
returns table (ok boolean, reason text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_status text;
begin
  if p_payment_method_id is null or p_vault_token_reference is null then
    raise exception 'activate_payment_method: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select pm.user_id, pm.status into v_user_id, v_status
  from public.payment_methods pm
  where pm.id = p_payment_method_id
  for update;

  if not found then
    return query select false, 'not_found'::text;
    return;
  end if;

  -- Revoke any existing active method for this user
  update public.payment_methods pm
    set status = 'revoked', revoked_at = now()
    where pm.user_id = v_user_id
      and pm.status = 'active'
      and pm.id <> p_payment_method_id;

  -- Activate this method
  update public.payment_methods pm
    set status = 'active',
        vault_token_reference = p_vault_token_reference,
        provider_customer_reference = p_provider_customer_reference,
        payer_email_masked = p_payer_email_masked,
        setup_state_hash = null
    where pm.id = p_payment_method_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (v_user_id, 'user', v_user_id::text, 'payment_method.linked', 'payment_method', p_payment_method_id::text,
          jsonb_build_object('payer_email_masked', p_payer_email_masked));

  return query select true, 'activated'::text;
end;
$$;

revoke execute on function public.activate_payment_method(uuid, text, text, text)
  from public, anon, authenticated;
grant execute on function public.activate_payment_method(uuid, text, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: revoke_payment_method (design §3.10, §6.3)
-- Refuses revocation if an attempt is in flight (initiated, pending, unknown).
-- ---------------------------------------------------------------------------

create or replace function public.revoke_payment_method(
  p_id uuid,
  p_user_id uuid
)
returns table (ok boolean, reason text, vault_token_reference text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_status text;
  v_vault_token text;
begin
  if p_id is null then
    raise exception 'revoke_payment_method: id must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select pm.user_id, pm.status, pm.vault_token_reference
    into v_user_id, v_status, v_vault_token
  from public.payment_methods pm
  where pm.id = p_id
  for update;

  if not found or (p_user_id is not null and v_user_id <> p_user_id) then
    return query select false, 'not_found'::text, null::text;
    return;
  end if;

  if v_status = 'revoked' then
    return query select true, 'already_revoked'::text, null::text;
    return;
  end if;

  if exists (
    select 1 from public.payment_attempts a
    where a.payment_method_id = p_id
      and a.status in ('initiated', 'pending', 'unknown')
  ) then
    return query select false, 'payment_in_progress'::text, null::text;
    return;
  end if;

  update public.payment_methods pm
    set status = 'revoked', revoked_at = now()
    where pm.id = p_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (v_user_id, 'user', v_user_id::text, 'payment_method.revoked', 'payment_method', p_id::text,
          jsonb_build_object('payment_method_id', p_id));

  return query select true, 'revoked'::text, v_vault_token;
end;
$$;

revoke execute on function public.revoke_payment_method(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.revoke_payment_method(uuid, uuid)
  to service_role;

-- ---------------------------------------------------------------------------
-- RPC: find_payment_method_by_state (design §3.10, §6.3)
-- ---------------------------------------------------------------------------

create or replace function public.find_payment_method_by_state(p_state_hash text)
returns table (
  found boolean,
  id uuid,
  user_id uuid,
  setup_token_reference text,
  expired boolean
)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_id uuid;
  v_user_id uuid;
  v_setup_ref text;
  v_expires timestamptz;
  v_status text;
begin
  if p_state_hash is null then
    raise exception 'find_payment_method_by_state: state_hash must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select pm.id, pm.user_id, pm.setup_token_reference, pm.setup_expires_at, pm.status
    into v_id, v_user_id, v_setup_ref, v_expires, v_status
  from public.payment_methods pm
  where pm.setup_state_hash = p_state_hash
    and pm.status = 'pending';

  if not found then
    return query select false, null::uuid, null::uuid, null::text, false;
    return;
  end if;

  if v_expires is not null and v_expires <= now() then
    return query select true, v_id, v_user_id, v_setup_ref, true;
  else
    return query select true, v_id, v_user_id, v_setup_ref, false;
  end if;
end;
$$;

revoke execute on function public.find_payment_method_by_state(text)
  from public, anon, authenticated;
grant execute on function public.find_payment_method_by_state(text)
  to service_role;


-- Phase 3 expiry sweep: skip busy devices/orders instead of blocking approvals.
-- Discover ids unlocked, then honor device -> order -> challenges -> spending.
create or replace function public.expire_stale_orders(p_limit integer)
returns table (order_id uuid)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_candidate record;
  v_locked uuid;
  v_ok boolean;
begin
  if p_limit is null or p_limit <= 0 then
    raise exception 'expire_stale_orders: limit must be positive' using errcode = 'check_violation';
  end if;
  for v_candidate in
    select o.id, o.device_id from public.orders o
    where o.status in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
      and o.quote_expires_at <= now()
    order by o.id limit p_limit
  loop
    if v_candidate.device_id is not null then
      perform 1 from public.devices d where d.id = v_candidate.device_id for share skip locked;
      if not found then continue; end if;
    end if;
    select o.id into v_locked from public.orders o
      where o.id = v_candidate.id and o.status in ('CREATED', 'QUOTED', 'AWAITING_APPROVAL')
        and o.quote_expires_at <= now()
      order by o.id for update skip locked;
    if not found then continue; end if;
    select e.ok into v_ok from public.expire_order(v_locked) e;
    if v_ok then order_id := v_locked; return next; end if;
  end loop;
end;
$$;
revoke execute on function public.expire_stale_orders(integer) from public, anon, authenticated;
grant execute on function public.expire_stale_orders(integer) to service_role;
-- === END MIGRATION 20261025090200_phase3_approvals_payments.sql ===


-- === BEGIN MIGRATION 20261025090300_phase4_realtime.sql ===
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (select 1 from pg_publication_tables
                     where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'orders') then
    alter publication supabase_realtime add table public.orders;
  end if;
end $$;
-- === END MIGRATION 20261025090300_phase4_realtime.sql ===

-- Make backend access explicit even if the public schema/default ACLs were
-- recreated. BYPASSRLS alone does not grant table privileges.
grant all on table
  public.user_profiles,
  public.devices,
  public.payment_methods,
  public.merchants,
  public.conversations,
  public.orders,
  public.order_items,
  public.mandates,
  public.approvals,
  public.spending_records,
  public.payment_attempts,
  public.conversation_events,
  public.audit_logs,
  public.webhook_events,
  public.a2a_messages,
  public.approval_challenges
to service_role;

-- Assert the important bootstrap postconditions before committing.
do $bootstrap_verify$
declare
  v_name text;
begin
  foreach v_name in array array['user_profiles', 'devices', 'payment_methods', 'merchants', 'conversations', 'orders', 'order_items', 'mandates', 'approvals', 'spending_records', 'payment_attempts', 'conversation_events', 'audit_logs', 'webhook_events', 'a2a_messages', 'approval_challenges'] loop
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
    where n.nspname = 'public' and p.proname in ('accept_gadget_approval', 'activate_payment_method', 'apply_payment_result', 'cancel_order', 'claim_reconcilable_attempt', 'claim_reconcilable_attempts', 'create_order_from_quote', 'enforce_order_status_transition', 'expire_order', 'expire_stale_orders', 'find_payment_method_by_state', 'handle_new_user', 'issue_approval_challenge', 'mark_attempt_exhausted', 'mark_attempt_sent', 'mark_capture_sent', 'order_transition_allowed', 'prevent_modification', 'record_gadget_denial', 'register_device', 'reserve_spending', 'revoke_device', 'revoke_payment_method', 'set_updated_at', 'spending_summary')
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
