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
