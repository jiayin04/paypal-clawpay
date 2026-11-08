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
