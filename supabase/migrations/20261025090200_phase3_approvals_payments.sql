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
