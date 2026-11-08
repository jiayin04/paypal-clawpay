-- pgTAP suite for migrations 20261025090000 / 20261025090100 (Phase 2).
-- Phase 3 and 4 append their sections and recompute plan().
-- Run with: supabase test db   (local stack) — everything is rolled back.
-- Not executed by the implementer (no local stack; design §7.3).

begin;
create extension if not exists pgtap with schema extensions;
set local search_path = public, extensions;

select plan(85);

-- ---------------------------------------------------------------------------
-- Fixtures (as postgres)
-- ---------------------------------------------------------------------------

insert into auth.users (id, email, aud, role) values
  ('11111111-1111-1111-1111-111111111111', 'p2-user-a@example.com', 'authenticated', 'authenticated'),
  ('22222222-2222-2222-2222-222222222222', 'p2-user-b@example.com', 'authenticated', 'authenticated');

insert into public.devices (id, user_id, device_name, public_key, key_algorithm, status, token_hash, llm_token_hash, revoked_at) values
  ('d2000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111', 'p2 A', 'p2-pk-a', 'ecdsa-p256-sha256', 'active', 'p2-th-a', 'p2-lh-a', null),
  ('d2000000-0000-0000-0000-0000000000a2', '11111111-1111-1111-1111-111111111111', 'p2 A2', 'p2-pk-a2', 'ecdsa-p256-sha256', 'active', 'p2-th-a2', 'p2-lh-a2', null),
  ('d2000000-0000-0000-0000-0000000000a3', '11111111-1111-1111-1111-111111111111', 'p2 A3', 'p2-pk-a3', 'ecdsa-p256-sha256', 'revoked', null, null, now()),
  ('d2000000-0000-0000-0000-0000000000b1', '22222222-2222-2222-2222-222222222222', 'p2 B', 'p2-pk-b', 'ecdsa-p256-sha256', 'active', 'p2-th-b', 'p2-lh-b', null);

insert into public.orders
  (id, user_id, device_id, merchant_id, currency, subtotal_minor, total_minor, status, idempotency_key, quote_expires_at)
select v.id::uuid, v.user_id::uuid, v.device_id::uuid,
       (select m.id from public.merchants m where m.merchant_code = 'grocer'),
       'MYR', v.total, v.total, v.status, v.k, now() + v.ttl::interval
from (values
  ('00000000-0000-0000-0000-0000000002a1', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 1000, 'AWAITING_APPROVAL', 'p2-k1', '1 hour'),
  ('00000000-0000-0000-0000-0000000002a2', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a2', 1000, 'AWAITING_APPROVAL', 'p2-k2', '1 hour'),
  ('00000000-0000-0000-0000-0000000002a3', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 1000, 'PAID', 'p2-k3', '1 hour'),
  ('00000000-0000-0000-0000-0000000002a4', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 1000, 'AWAITING_APPROVAL', 'p2-k4', '-1 minute'),
  ('00000000-0000-0000-0000-0000000002a5', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 2000, 'AWAITING_APPROVAL', 'p2-k5', '1 hour'),
  ('00000000-0000-0000-0000-0000000002a6', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 3000, 'AWAITING_APPROVAL', 'p2-k6', '1 hour'),
  ('00000000-0000-0000-0000-0000000002b1', '22222222-2222-2222-2222-222222222222', 'd2000000-0000-0000-0000-0000000000b1', 1000, 'AWAITING_APPROVAL', 'p2-k7', '1 hour')
) as v(id, user_id, device_id, total, status, k, ttl);

insert into public.order_items (id, order_id, product_reference, description, quantity, unit_price_minor, line_total_minor) values
  ('a1000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000002a1', 'GRC-MILK-1L', 'Milk', 1, 1000, 1000),
  ('a1000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000002b1', 'GRC-MILK-1L', 'Milk', 1, 1000, 1000);

insert into public.approvals (id, order_id, user_id, device_id, approval_type, decision, nonce, signed_payload_hash) values
  ('a2000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000002a1', '11111111-1111-1111-1111-111111111111',
   'd2000000-0000-0000-0000-0000000000a1', 'gadget', 'approved', 'p2-nonce-a', 'p2-h-a'),
  ('a2000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000002b1', '22222222-2222-2222-2222-222222222222',
   'd2000000-0000-0000-0000-0000000000b1', 'gadget', 'approved', 'p2-nonce-b', 'p2-h-b');

insert into public.mandates (id, order_id, user_id, mandate_type) values
  ('a3000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000002a1', '11111111-1111-1111-1111-111111111111', 'intent'),
  ('a3000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000002b1', '22222222-2222-2222-2222-222222222222', 'intent');

insert into public.payment_attempts (id, order_id, idempotency_key, amount_minor, currency) values
  ('a4000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000002a1', 'p2-pa-a', 1000, 'MYR'),
  ('a4000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-0000000002b1', 'p2-pa-b', 1000, 'MYR');

insert into public.conversations (id, user_id, device_id, external_session_id) values
  ('a5000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 'p2-session-a'),
  ('a5000000-0000-0000-0000-0000000000b1', '22222222-2222-2222-2222-222222222222', 'd2000000-0000-0000-0000-0000000000b1', 'p2-session-b');

insert into public.conversation_events (id, conversation_id, event_type, payload, summary) values
  ('a6000000-0000-0000-0000-0000000000a1', 'a5000000-0000-0000-0000-0000000000a1', 'agent_call', '{"internal": 1}', '{"tool": "search_products"}'),
  ('a6000000-0000-0000-0000-0000000000b1', 'a5000000-0000-0000-0000-0000000000b1', 'agent_call', '{"internal": 1}', '{"tool": "search_products"}');

insert into public.a2a_messages (id, user_id, merchant_code, correlation_id, direction, method, outcome, payload) values
  ('a7000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111', 'grocer', 'c-a', 'request', 'search', 'ok', '{"internal": 1}'),
  ('a7000000-0000-0000-0000-0000000000b1', '22222222-2222-2222-2222-222222222222', 'grocer', 'c-b', 'request', 'search', 'ok', '{"internal": 1}');

-- ---------------------------------------------------------------------------
-- Catalog-wide checks (as postgres, before any role switch)
-- ---------------------------------------------------------------------------

select is(
  (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity),
  0::bigint, 'every public table has RLS enabled');

select is(
  (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm')
      and has_table_privilege('anon', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')),
  0::bigint, 'anon has no privilege on any public table');

select is(
  (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname <> '_tmp_default_priv_probe'
      and (has_function_privilege('anon', p.oid, 'EXECUTE')
           or has_function_privilege('authenticated', p.oid, 'EXECUTE'))),
  0::bigint, 'no public function is executable by anon or authenticated');

create table public._tmp_default_priv_table (id integer);
select ok(
  not has_table_privilege('anon', 'public._tmp_default_priv_table', 'SELECT, INSERT, UPDATE, DELETE')
  and not has_table_privilege('authenticated', 'public._tmp_default_priv_table', 'SELECT, INSERT, UPDATE, DELETE'),
  'new public tables get no anon/authenticated privileges (default privileges)');
drop table public._tmp_default_priv_table;

create function public._tmp_default_priv_probe() returns int language sql as $$ select 1 $$;
select is(
  (select count(*)::int from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
    where p.oid = 'public._tmp_default_priv_probe()'::regprocedure
      and a.grantee in ('anon'::regrole, 'authenticated'::regrole)),
  0, 'new public functions get no explicit anon/authenticated grant');
drop function public._tmp_default_priv_probe();

-- ---------------------------------------------------------------------------
-- Order status transition trigger
-- ---------------------------------------------------------------------------

select lives_ok(
  $$ update public.orders set status = 'APPROVED' where id = '00000000-0000-0000-0000-0000000002a6' $$,
  'legal transition AWAITING_APPROVAL -> APPROVED');
select throws_ok(
  $$ update public.orders set status = 'PAID' where id = '00000000-0000-0000-0000-0000000002a6' $$,
  '23514', null, 'illegal transition APPROVED -> PAID');
select throws_ok(
  $$ update public.orders set status = 'CANCELLED' where id = '00000000-0000-0000-0000-0000000002a3' $$,
  '23514', null, 'illegal transition PAID -> CANCELLED');
select throws_ok(
  $$ update public.orders set status = 'PAID' where id = '00000000-0000-0000-0000-0000000002a1' $$,
  '23514', null, 'illegal transition AWAITING_APPROVAL -> PAID');
select lives_ok(
  $$ update public.orders set status = status where id = '00000000-0000-0000-0000-0000000002a3' $$,
  'unchanged status is not a transition');

-- ---------------------------------------------------------------------------
-- reserve_spending: timezone validation and SQL-derived day (review #2)
-- ---------------------------------------------------------------------------

select throws_ok(
  $$ select * from public.reserve_spending('11111111-1111-1111-1111-111111111111',
       '00000000-0000-0000-0000-0000000002a1', 1000, 'MYR', 50000, 'Mars/Olympus_Mons') $$,
  '22023', null, 'unknown timezone raises invalid_parameter_value');

select results_eq(
  $$ select success, reason from public.reserve_spending('11111111-1111-1111-1111-111111111111',
       '00000000-0000-0000-0000-0000000002a1', 1000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (true, 'reserved'::text) $$,
  'reserve_spending with timezone reserves');

select is(
  (select spending_day from public.spending_records where order_id = '00000000-0000-0000-0000-0000000002a1'),
  (now() at time zone 'Asia/Kuala_Lumpur')::date,
  'spending_day is derived in SQL from the timezone');

select results_eq(
  $$ select committed_minor, reserved_minor from public.spending_summary(
       '11111111-1111-1111-1111-111111111111', 'Asia/Kuala_Lumpur') $$,
  $$ values (0::bigint, 1000::bigint) $$,
  'spending_summary reports committed and reserved for today');

-- ---------------------------------------------------------------------------
-- register_device
-- ---------------------------------------------------------------------------

select results_eq(
  $$ select ok, reason from public.register_device('11111111-1111-1111-1111-111111111111',
       'p2 new', 'p2-pk-new', 1, 'p2-th-new', 'p2-lh-new', 'admin', null) $$,
  $$ values (true, 'registered'::text) $$,
  'register_device registers an active device');

select is(
  (select llm_token_hash from public.devices where public_key = 'p2-pk-new'),
  'p2-lh-new', 'register_device stores llm_token_hash');

select results_eq(
  $$ select ok, reason from public.register_device('22222222-2222-2222-2222-222222222222',
       'p2 dup', 'p2-pk-new', 1, 'p2-th-dup', 'p2-lh-dup', 'admin', null) $$,
  $$ values (false, 'public_key_already_registered'::text) $$,
  'same public key cannot be registered twice while active');

select throws_ok(
  $$ insert into public.devices (user_id, device_name, public_key, key_algorithm, status)
     values ('22222222-2222-2222-2222-222222222222', 'x', 'p2-pk-new', 'ecdsa-p256-sha256', 'active') $$,
  '23505', null, 'devices_active_public_key_key backs the concurrent-registration race');

select results_eq(
  $$ select ok, reason from public.register_device('33333333-3333-3333-3333-333333333333',
       'p2 none', 'p2-pk-none', 1, 'p2-th-none', 'p2-lh-none', 'admin', null) $$,
  $$ values (false, 'user_not_found'::text) $$,
  'register_device for an unknown user');

-- ---------------------------------------------------------------------------
-- create_order_from_quote
-- ---------------------------------------------------------------------------

create temp table _p2_first on commit drop as
  select * from public.create_order_from_quote(
    '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 'grocer', 'MYR',
    now() + interval '5 minutes', 'q-1', repeat('a', 64),
    '[{"sku":"GRC-MILK-1L","name":"Fresh milk 1L","qty":2,"unit_price_minor":690}]'::jsonb, 1380);

select results_eq(
  $$ select ok, reason, deduplicated from _p2_first $$,
  $$ values (true, 'created'::text, false) $$,
  'create_order_from_quote creates an order');

select results_eq(
  $$ select o.status, o.total_minor, o.subtotal_minor from public.orders o
     where o.id = (select order_id from _p2_first) $$,
  $$ values ('AWAITING_APPROVAL'::text, 1380::bigint, 1380::bigint) $$,
  'created order is AWAITING_APPROVAL with the recomputed total');

select is(
  (select count(*) from public.order_items i where i.order_id = (select order_id from _p2_first)),
  1::bigint, 'order items inserted');

select is(
  (select order_id from public.create_order_from_quote(
    '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 'grocer', 'MYR',
    now() + interval '5 minutes', 'q-2', repeat('a', 64),
    '[{"sku":"GRC-MILK-1L","name":"Fresh milk 1L","qty":2,"unit_price_minor":690}]'::jsonb, 1380)),
  (select order_id from _p2_first),
  'identical cart from the same device within 60 s returns the same order');

select throws_ok(
  $$ select * from public.create_order_from_quote(
       '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a1', 'grocer', 'MYR',
       now() + interval '5 minutes', 'q-3', repeat('b', 64),
       '[{"sku":"GRC-MILK-1L","name":"Fresh milk 1L","qty":2,"unit_price_minor":690}]'::jsonb, 9999) $$,
  '23514', null, 'item sum mismatch raises');

select results_eq(
  $$ select ok, reason from public.create_order_from_quote(
       '11111111-1111-1111-1111-111111111111', 'd2000000-0000-0000-0000-0000000000a3', 'grocer', 'MYR',
       now() + interval '5 minutes', 'q-4', repeat('c', 64),
       '[{"sku":"GRC-MILK-1L","name":"Fresh milk 1L","qty":1,"unit_price_minor":690}]'::jsonb, 690) $$,
  $$ values (false, 'device_revoked'::text) $$,
  'revoked device cannot create orders');

-- ---------------------------------------------------------------------------
-- revoke_device
-- ---------------------------------------------------------------------------

select results_eq(
  $$ select ok, reason from public.revoke_device('d2000000-0000-0000-0000-0000000000a2',
       '22222222-2222-2222-2222-222222222222', 'user', '22222222-2222-2222-2222-222222222222') $$,
  $$ values (false, 'not_found'::text) $$,
  'another user cannot revoke the device');

select results_eq(
  $$ select ok, reason, cancelled_orders from public.revoke_device('d2000000-0000-0000-0000-0000000000a2',
       null, 'admin', null) $$,
  $$ values (true, 'revoked'::text, 1) $$,
  'admin revokes the device and cancels its awaiting order');

select is(
  (select status from public.orders where id = '00000000-0000-0000-0000-0000000002a2'),
  'CANCELLED', 'awaiting order of a revoked device is cancelled');

select results_eq(
  $$ select status, revoked_at is not null from public.devices where id = 'd2000000-0000-0000-0000-0000000000a2' $$,
  $$ values ('revoked'::text, true) $$,
  'device is revoked with revoked_at');

-- ---------------------------------------------------------------------------
-- cancel_order / expire_order
-- ---------------------------------------------------------------------------

select results_eq(
  $$ select ok, reason from public.cancel_order('00000000-0000-0000-0000-0000000002a5',
       '11111111-1111-1111-1111-111111111111', 'user', '11111111-1111-1111-1111-111111111111') $$,
  $$ values (true, 'cancelled'::text) $$,
  'owner cancels an awaiting order');

select results_eq(
  $$ select ok, reason from public.cancel_order('00000000-0000-0000-0000-0000000002a3',
       '11111111-1111-1111-1111-111111111111', 'user', '11111111-1111-1111-1111-111111111111') $$,
  $$ values (false, 'invalid_order_status'::text) $$,
  'paid order cannot be cancelled');

select results_eq(
  $$ select ok, reason from public.cancel_order('00000000-0000-0000-0000-0000000002b1',
       '11111111-1111-1111-1111-111111111111', 'user', '11111111-1111-1111-1111-111111111111') $$,
  $$ values (false, 'not_found'::text) $$,
  'another user''s order is not found');

select results_eq(
  $$ select ok, reason from public.expire_order('00000000-0000-0000-0000-0000000002a4') $$,
  $$ values (true, 'expired'::text) $$,
  'order with an expired quote is expired');

select is(
  (select status from public.orders where id = '00000000-0000-0000-0000-0000000002a4'),
  'EXPIRED', 'expired order status');

select results_eq(
  $$ select ok, reason from public.expire_order('00000000-0000-0000-0000-0000000002a1') $$,
  $$ values (false, 'not_expirable'::text) $$,
  'order with a live quote is not expired');

-- ---------------------------------------------------------------------------
-- Row scoping and hidden columns as user A
-- ---------------------------------------------------------------------------

set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub": "11111111-1111-1111-1111-111111111111", "role": "authenticated"}', true);

select ok(exists (select 1 from public.devices where id = 'd2000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.devices where id = 'd2000000-0000-0000-0000-0000000000b1'),
  'A sees own devices only');
select ok(exists (select 1 from public.approvals where id = 'a2000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.approvals where id = 'a2000000-0000-0000-0000-0000000000b1'),
  'A sees own approvals only');
select ok(exists (select 1 from public.mandates where id = 'a3000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.mandates where id = 'a3000000-0000-0000-0000-0000000000b1'),
  'A sees own mandates only');
select ok(exists (select 1 from public.payment_attempts where id = 'a4000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.payment_attempts where id = 'a4000000-0000-0000-0000-0000000000b1'),
  'A sees own payment_attempts only');
select ok(exists (select 1 from public.order_items where id = 'a1000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.order_items where id = 'a1000000-0000-0000-0000-0000000000b1'),
  'A sees own order_items only');
select ok(exists (select 1 from public.conversation_events where id = 'a6000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.conversation_events where id = 'a6000000-0000-0000-0000-0000000000b1'),
  'A sees own conversation_events only');
select ok(exists (select 1 from public.a2a_messages where id = 'a7000000-0000-0000-0000-0000000000a1')
  and not exists (select 1 from public.a2a_messages where id = 'a7000000-0000-0000-0000-0000000000b1'),
  'A sees own a2a_messages only');

select throws_ok($$ select token_hash from public.devices $$, '42501', null, 'devices.token_hash hidden');
select throws_ok($$ select llm_token_hash from public.devices $$, '42501', null, 'devices.llm_token_hash hidden');
select throws_ok($$ select payload from public.conversation_events $$, '42501', null, 'conversation_events.payload hidden');
select throws_ok($$ select payload from public.a2a_messages $$, '42501', null, 'a2a_messages.payload hidden');
select lives_ok(
  $$ select e.summary, m.summary, d.last_seen_at
     from public.conversation_events e, public.a2a_messages m, public.devices d $$,
  'allowlisted summary and device columns are readable');

-- ---------------------------------------------------------------------------
-- Row scoping as user B
-- ---------------------------------------------------------------------------

select set_config('request.jwt.claims',
  '{"sub": "22222222-2222-2222-2222-222222222222", "role": "authenticated"}', true);

select ok(exists (select 1 from public.devices where id = 'd2000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.devices where id = 'd2000000-0000-0000-0000-0000000000a1'),
  'B sees own devices only');
select ok(exists (select 1 from public.approvals where id = 'a2000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.approvals where id = 'a2000000-0000-0000-0000-0000000000a1'),
  'B sees own approvals only');
select ok(exists (select 1 from public.mandates where id = 'a3000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.mandates where id = 'a3000000-0000-0000-0000-0000000000a1'),
  'B sees own mandates only');
select ok(exists (select 1 from public.payment_attempts where id = 'a4000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.payment_attempts where id = 'a4000000-0000-0000-0000-0000000000a1'),
  'B sees own payment_attempts only');
select ok(exists (select 1 from public.order_items where id = 'a1000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.order_items where id = 'a1000000-0000-0000-0000-0000000000a1'),
  'B sees own order_items only');
select ok(exists (select 1 from public.conversation_events where id = 'a6000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.conversation_events where id = 'a6000000-0000-0000-0000-0000000000a1'),
  'B sees own conversation_events only');
select ok(exists (select 1 from public.a2a_messages where id = 'a7000000-0000-0000-0000-0000000000b1')
  and not exists (select 1 from public.a2a_messages where id = 'a7000000-0000-0000-0000-0000000000a1'),
  'B sees own a2a_messages only');

-- ---------------------------------------------------------------------------
-- Phase 3: Hidden columns and privileges under authenticated (user A)
-- ---------------------------------------------------------------------------

set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub": "11111111-1111-1111-1111-111111111111", "role": "authenticated"}', true);

select throws_ok($$ select provider_order_id from public.payment_attempts $$, '42501', null, 'payment_attempts.provider_order_id hidden');
select throws_ok($$ select provider_capture_id from public.payment_attempts $$, '42501', null, 'payment_attempts.provider_capture_id hidden');
select throws_ok($$ select idempotency_key from public.payment_attempts $$, '42501', null, 'payment_attempts.idempotency_key hidden');
select throws_ok($$ select nonce from public.approvals $$, '42501', null, 'approvals.nonce hidden');
select throws_ok($$ select signed_payload_hash from public.approvals $$, '42501', null, 'approvals.signed_payload_hash hidden');
select throws_ok($$ select provider_mandate_reference from public.mandates $$, '42501', null, 'mandates.provider_mandate_reference hidden');
select throws_ok($$ select setup_token_reference from public.payment_methods $$, '42501', null, 'payment_methods.setup_token_reference hidden');
select throws_ok($$ select setup_state_hash from public.payment_methods $$, '42501', null, 'payment_methods.setup_state_hash hidden');
select throws_ok($$ select * from public.approval_challenges $$, '42501', null, 'approval_challenges not readable');
select lives_ok($$ select payer_email_masked from public.payment_methods $$, 'payer_email_masked readable');

-- ---------------------------------------------------------------------------
-- Phase 3: RPCs (as postgres)
-- ---------------------------------------------------------------------------

reset role;

-- issue_approval_challenge
select results_eq(
  $$ select ok, reason, key_version from public.issue_approval_challenge(
       '00000000-0000-0000-0000-0000000002a6', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 1, 'nonce-test-12345678901234567890123456789012',
       '{"canonical":true}', repeat('1', 64), 3000, 'MYR', now() + interval '2 minutes') $$,
  $$ values (true, 'issued'::text, 1) $$,
  'issue_approval_challenge issues challenge');

select results_eq(
  $$ select ok, reason, key_version from public.issue_approval_challenge(
       '00000000-0000-0000-0000-0000000002a6', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 1, 'nonce-test-fresh1234567890123456789012345678',
       '{"canonical":true}', repeat('2', 64), 3000, 'MYR', now() + interval '2 minutes') $$,
  $$ values (true, 'existing'::text, 1) $$,
  'issue_approval_challenge idempotent returns existing');

select results_eq(
  $$ select ok, reason from public.issue_approval_challenge(
       '00000000-0000-0000-0000-0000000002a4', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 1, 'nonce-test-exp123456789012345678901234567890',
       '{"canonical":true}', repeat('3', 64), 1000, 'MYR', now() + interval '2 minutes') $$,
  $$ values (false, 'quote_expired'::text) $$,
  'issue_approval_challenge on expired quote transitions order');

-- record_gadget_denial
insert into public.approval_challenges (
  id, order_id, device_id, user_id, nonce, canonical_payload,
  payload_sha256, amount_minor, currency, key_version, expires_at
) values (
  'c1000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000002a1',
  'd2000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
  'nonce-denial-test123456789012345678901234567', '{"test":1}', repeat('d', 64),
  1000, 'MYR', 1, now() + interval '5 minutes'
);

select results_eq(
  $$ select ok, reason from public.record_gadget_denial(
       'c1000000-0000-0000-0000-000000000001', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 'nonce-denial-test123456789012345678901234567',
       repeat('d', 64), 1, 'device', 'd2000000-0000-0000-0000-0000000000a1') $$,
  $$ values (true, 'denied'::text) $$,
  'record_gadget_denial succeeds');

select is(
  (select status from public.orders where id = '00000000-0000-0000-0000-0000000002a1'),
  'DENIED', 'denied order has status DENIED');

select is(
  (select consumed_at is not null from public.approval_challenges where id = 'c1000000-0000-0000-0000-000000000001'),
  true, 'denial consumed the challenge');

select results_eq(
  $$ select decision, consumed_at is null, approved_at is null from public.approvals
     where order_id = '00000000-0000-0000-0000-0000000002a1' and approval_type = 'gadget' and decision = 'denied' $$,
  $$ values ('denied'::text, true, true) $$,
  'denial approvals row has null consumed_at and approved_at');

-- accept_gadget_approval
insert into public.payment_methods (id, user_id, provider, status, vault_token_reference, payer_email_masked)
values ('pm000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
        'paypal', 'active', 'vault-token-123', 'a***@example.com')
on conflict do nothing;

select ok(
  (select a.ok and a.reason = 'approved' and a.attempt_id is not null from public.accept_gadget_approval(
     (select id from public.approval_challenges where order_id = '00000000-0000-0000-0000-0000000002a6' limit 1),
     'd2000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
     'nonce-test-12345678901234567890123456789012', repeat('1', 64),
     'MYR', 50000, 'Asia/Kuala_Lumpur', 10000) a),
  'accept_gadget_approval succeeds');

select is(
  (select count(*) from public.mandates m where m.order_id = '00000000-0000-0000-0000-0000000002a6'
     and m.status = 'verified' and m.provider_mandate_reference = repeat('1', 64)),
  1::bigint, 'verified mandate exists with matching reference');

select is(
  (select status from public.orders where id = '00000000-0000-0000-0000-0000000002a6'),
  'PAYMENT_PENDING', 'order is PAYMENT_PENDING after approval');

select results_eq(
  $$ select ok, reason from public.accept_gadget_approval(
       (select id from public.approval_challenges where order_id = '00000000-0000-0000-0000-0000000002a6' limit 1),
       'd2000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
       'nonce-test-12345678901234567890123456789012', repeat('1', 64),
       'MYR', 50000, 'Asia/Kuala_Lumpur', 10000) $$,
  $$ values (false, 'challenge_consumed'::text) $$,
  'consumed challenge replay fails');

-- Phone threshold & daily limit checks
insert into public.orders (
  id, user_id, device_id, merchant_id, currency, subtotal_minor, total_minor,
  status, idempotency_key, quote_expires_at
) values (
  '00000000-0000-0000-0000-000000000299', '11111111-1111-1111-1111-111111111111',
  'd2000000-0000-0000-0000-0000000000a1',
  (select id from public.merchants where merchant_code = 'grocer'),
  'MYR', 10001, 10001, 'AWAITING_APPROVAL', 'p2-k99', now() + interval '1 hour'
);

insert into public.approval_challenges (
  id, order_id, device_id, user_id, nonce, canonical_payload,
  payload_sha256, amount_minor, currency, key_version, expires_at
) values (
  'c2000000-0000-0000-0000-000000000099', '00000000-0000-0000-0000-000000000299',
  'd2000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
  'nonce-phone-99123456789012345678901234567890', '{"phone":1}', repeat('9', 64),
  10001, 'MYR', 1, now() + interval '5 minutes'
);

select results_eq(
  $$ select ok, reason from public.accept_gadget_approval(
       'c2000000-0000-0000-0000-000000000099', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 'nonce-phone-99123456789012345678901234567890',
       repeat('9', 64), 'MYR', 50000, 'Asia/Kuala_Lumpur', 10000) $$,
  $$ values (false, 'phone_approval_required'::text) $$,
  '10001 rejected with phone_approval_required');

select results_eq(
  $$ select ok, reason from public.accept_gadget_approval(
       'c2000000-0000-0000-0000-000000000099', 'd2000000-0000-0000-0000-0000000000a1',
       '11111111-1111-1111-1111-111111111111', 'nonce-phone-99123456789012345678901234567890',
       repeat('9', 64), 'MYR', 2000, 'Asia/Kuala_Lumpur', 20000) $$,
  $$ values (false, 'daily_limit_exceeded'::text) $$,
  'daily limit exceeded leaves challenge open');

select is(
  (select consumed_at is null and invalidated_at is null from public.approval_challenges where id = 'c2000000-0000-0000-0000-000000000099'),
  true, 'challenge stays unconsumed on cap rejection');

-- mark_capture_sent
insert into public.payment_attempts (id, order_id, provider, status, idempotency_key, amount_minor, currency)
values ('pa000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000299', 'paypal', 'initiated', 'test-pa-cap', 10001, 'MYR');

select results_eq(
  $$ select previous is null, send_count from public.mark_capture_sent('pa000000-0000-0000-0000-000000000001') $$,
  $$ values (true, 1) $$,
  'first mark_capture_sent returns prev null and count 1');

select results_eq(
  $$ select previous is not null, send_count from public.mark_capture_sent('pa000000-0000-0000-0000-000000000001') $$,
  $$ values (true, 2) $$,
  'second mark_capture_sent returns prev set and count 2');

-- mark_attempt_exhausted
update public.orders set status = 'PAYMENT_PENDING' where id = '00000000-0000-0000-0000-000000000299';

select results_eq(
  $$ select ok, order_status from public.mark_attempt_exhausted('pa000000-0000-0000-0000-000000000001') $$,
  $$ values (true, 'REQUIRES_RECONCILIATION'::text) $$,
  'mark_attempt_exhausted moves order to REQUIRES_RECONCILIATION');

select is(
  (select count(*) from public.claim_reconcilable_attempt('00000000-0000-0000-0000-000000000299')),
  0::bigint, 'exhausted attempt cannot be claimed');

-- revoke_payment_method
select results_eq(
  $$ select ok, reason from public.revoke_payment_method('pm000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111') $$,
  $$ values (true, 'revoked'::text) $$,
  'revoke_payment_method revokes when no in-flight attempts');

-- apply_payment_result
insert into public.payment_attempts (id, order_id, provider, status, idempotency_key, amount_minor, currency)
values ('pa000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-0000000002a6', 'paypal', 'initiated', 'test-pa-apply', 3000, 'MYR');

select results_eq(
  $$ select ok, reason, new_status from public.apply_payment_result(
       'pa000000-0000-0000-0000-000000000002', 'uncertain', 'po-1', 'pc-1', 'PENDING', null, 'replay') $$,
  $$ values (true, 'applied'::text, 'unknown'::text) $$,
  'apply_payment_result replay uncertain -> unknown');

select is(
  (select status from public.orders where id = '00000000-0000-0000-0000-0000000002a6'),
  'REQUIRES_RECONCILIATION', 'uncertain replay sets order to REQUIRES_RECONCILIATION');

reset role;
select * from finish();
rollback;

