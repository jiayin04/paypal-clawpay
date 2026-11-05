-- ClawPay: merchant fulfillment handoff (reserve before payment, confirm or
-- release after) and the Shopee / Foodpanda / Grab demo merchant agents.
--
-- Additive only. The order status machine (orders_enforce_status_transition)
-- is unchanged: fulfillment progress lives in its own columns, written only
-- through set_order_fulfillment by the backend (service role).
--
-- fulfillment_status:
--   none                no merchant reservation yet
--   reserved            the merchant agent holds the cart (before payment)
--   reservation_failed  the merchant refused or could not answer; no payment is sent
--   confirmed           paid and accepted by the merchant (merchant_fulfillment_id set)
--   fulfillment_pending paid, merchant confirmation still outstanding (retried)
--   released            payment failed or the order ended; reservation cancelled

alter table public.orders
  add column fulfillment_status text not null default 'none'
    check (fulfillment_status in (
      'none', 'reserved', 'reservation_failed', 'confirmed', 'fulfillment_pending', 'released'
    )),
  add column merchant_reservation_id text check (char_length(merchant_reservation_id) <= 64),
  add column merchant_fulfillment_id text check (char_length(merchant_fulfillment_id) <= 64),
  add column fulfillment_updated_at timestamptz;

-- a2a_messages now also records the fulfillment calls.
alter table public.a2a_messages drop constraint a2a_messages_method_check;
alter table public.a2a_messages add constraint a2a_messages_method_check
  check (method in ('search', 'quote', 'reserve', 'confirm', 'cancel'));

-- ---------------------------------------------------------------------------
-- set_order_fulfillment: compare-and-set on fulfillment_status.
-- reasons: updated, order_not_found, status_changed (current value returned).
-- Ids are only ever filled in, never cleared.
-- ---------------------------------------------------------------------------

create or replace function public.set_order_fulfillment(
  p_order_id uuid,
  p_expected text[],
  p_status text,
  p_reservation_id text default null,
  p_fulfillment_id text default null
)
returns table (ok boolean, reason text, fulfillment_status text)
language plpgsql
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_current text;
begin
  if p_order_id is null or p_expected is null or p_status is null then
    raise exception 'set_order_fulfillment: missing argument' using errcode = 'invalid_parameter_value';
  end if;

  select o.user_id, o.fulfillment_status into v_user_id, v_current
  from public.orders o
  where o.id = p_order_id
  for update;

  if not found then
    return query select false, 'order_not_found'::text, null::text;
    return;
  end if;

  if not (v_current = any (p_expected)) then
    return query select false, 'status_changed'::text, v_current;
    return;
  end if;

  update public.orders o
    set fulfillment_status = p_status,
        merchant_reservation_id = coalesce(p_reservation_id, o.merchant_reservation_id),
        merchant_fulfillment_id = coalesce(p_fulfillment_id, o.merchant_fulfillment_id),
        fulfillment_updated_at = now()
    where o.id = p_order_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (v_user_id, 'system', null, 'order.fulfillment_changed', 'order', p_order_id::text,
          jsonb_build_object('from', v_current, 'to', p_status));

  return query select true, 'updated'::text, p_status;
end;
$$;

revoke execute on function public.set_order_fulfillment(uuid, text[], text, text, text) from public, anon, authenticated;
grant execute on function public.set_order_fulfillment(uuid, text[], text, text, text) to service_role;

-- ---------------------------------------------------------------------------
-- Demo merchants (mock agents in app/merchant_agents/catalogs.py). Whether
-- ClawPay calls them is decided by ENABLED_MERCHANTS.
-- ---------------------------------------------------------------------------

insert into public.merchants (merchant_code, display_name, status) values
  ('shopee', 'Shopee Demo', 'active'),
  ('foodpanda', 'Foodpanda Demo', 'active'),
  ('grab', 'Grab Demo', 'active')
on conflict (merchant_code) do nothing;
