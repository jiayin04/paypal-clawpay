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

insert into public.merchants (merchant_code, display_name, status) values
  ('grocer', 'Grocer Demo', 'active'),
  ('electronics', 'Electronics Demo', 'active')
on conflict (merchant_code) do nothing;

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
