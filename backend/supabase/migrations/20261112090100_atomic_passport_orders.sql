-- The early Python check saves quote calls but cannot authorize a later write.
-- Serialize grant issuance against creation with the device lock. Hold the
-- selected grant FOR SHARE so revocation/replacement cannot cross the decision.
-- No historical RPC is rewritten; the backend now calls this guarded entrypoint.
create or replace function public.create_order_with_passport(
  p_user_id uuid,
  p_device_id uuid,
  p_merchant_code text,
  p_currency char(3),
  p_quote_expires_at timestamptz,
  p_quote_reference text,
  p_cart_sha256 text,
  p_items jsonb,
  p_total_minor bigint,
  p_delegation_id uuid,
  p_passport_required boolean
)
returns table (ok boolean, reason text, order_id uuid, deduplicated boolean)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_status text;
  v_grant public.agent_delegations%rowtype;
  v_reason text;
begin
  if p_user_id is null or p_device_id is null or p_merchant_code is null
     or p_currency is null or p_total_minor is null or p_passport_required is null then
    raise exception 'create_order_with_passport: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  -- ponytail: serialize mutations per device; unrelated devices remain concurrent.
  select d.status into v_status from public.devices d
    where d.id = p_device_id and d.user_id = p_user_id for update;
  if not found or v_status <> 'active' then
    return query select false, 'device_revoked'::text, null::uuid, false;
    return;
  end if;

  select g.* into v_grant from public.agent_delegations g
    where g.device_id = p_device_id and g.user_id = p_user_id
    order by (g.revoked_at is null) desc, g.created_at desc, g.id desc
    limit 1 for share;

  if not found then
    if p_passport_required or p_delegation_id is not null then
      v_reason := 'no_delegation';
    end if;
  elsif v_grant.revoked_at is not null then
    v_reason := 'delegation_revoked';
  -- now() is the transaction start time and can be stale after a lock wait.
  elsif v_grant.expires_at <= clock_timestamp() then
    v_reason := 'delegation_expired';
  elsif v_grant.id is distinct from p_delegation_id then
    v_reason := 'delegation_changed';
  elsif not (p_merchant_code = any(v_grant.merchants)) then
    v_reason := 'merchant_not_delegated';
  elsif p_currency <> v_grant.currency then
    v_reason := 'currency_not_delegated';
  elsif p_total_minor > v_grant.max_order_minor then
    v_reason := 'delegation_limit_exceeded';
  end if;

  if v_reason is not null then
    return query select false, v_reason, null::uuid, false;
    return;
  end if;

  -- The grant lock remains held until this transaction commits the order.
  return query select * from public.create_order_from_quote(
    p_user_id, p_device_id, p_merchant_code, p_currency, p_quote_expires_at,
    p_quote_reference, p_cart_sha256, p_items, p_total_minor
  );
end;
$$;

revoke execute on function public.create_order_with_passport(uuid, uuid, text, char, timestamptz, text, text, jsonb, bigint, uuid, boolean)
  from public, anon, authenticated;
grant execute on function public.create_order_with_passport(uuid, uuid, text, char, timestamptz, text, text, jsonb, bigint, uuid, boolean)
  to service_role;
