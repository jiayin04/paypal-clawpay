-- pgTAP regression tests for the initial schema: reserve_spending validation,
-- cross-owner composite FKs, approval single use, audit append-only, and the
-- authenticated / anon privilege + RLS matrix.
-- Run with: supabase test db   (everything is rolled back)
--
-- Not covered here: concurrent reservations (needs two sessions; see
-- verification.md for the two-session advisory-lock check).

begin;
create extension if not exists pgtap with schema extensions;
set local search_path = public, extensions;

select plan(42);

-- ---------------------------------------------------------------------------
-- Fixtures (as postgres)
-- ---------------------------------------------------------------------------

insert into auth.users (id, email, aud, role) values
  ('11111111-1111-1111-1111-111111111111', 'user-a@example.com', 'authenticated', 'authenticated'),
  ('22222222-2222-2222-2222-222222222222', 'user-b@example.com', 'authenticated', 'authenticated');

insert into public.merchants (id, merchant_code, display_name, agent_endpoint_reference) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'test-grocer', 'Grocer', 'internal-endpoint-ref');

insert into public.devices (id, user_id, device_name, public_key, key_algorithm, status) values
  ('d0000000-0000-0000-0000-00000000000a', '11111111-1111-1111-1111-111111111111', 'gadget A', 'pk-a', 'ecdsa-p256-sha256', 'active'),
  ('d0000000-0000-0000-0000-00000000000b', '22222222-2222-2222-2222-222222222222', 'gadget B', 'pk-b', 'ecdsa-p256-sha256', 'active');

insert into public.payment_methods (user_id, provider_customer_reference, vault_token_reference, status) values
  ('11111111-1111-1111-1111-111111111111', 'cust-ref', 'vault-token-ref', 'active');

-- o1..o6, o8 belong to A; o7, o9 to B.
insert into public.orders
  (id, user_id, device_id, merchant_id, currency, subtotal_minor, total_minor, status, idempotency_key, quote_expires_at)
values
  ('00000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111', 'd0000000-0000-0000-0000-00000000000a',
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 30000, 30000, 'AWAITING_APPROVAL', 'k1', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000a2', '11111111-1111-1111-1111-111111111111', 'd0000000-0000-0000-0000-00000000000a',
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 30000, 30000, 'APPROVED', 'k2', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000a3', '11111111-1111-1111-1111-111111111111', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 5000, 5000, 'PAID', 'k3', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000a4', '11111111-1111-1111-1111-111111111111', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 5000, 5000, 'AWAITING_APPROVAL', 'k4', now() - interval '1 minute'),
  ('00000000-0000-0000-0000-0000000000a5', '11111111-1111-1111-1111-111111111111', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'USD', 1000, 1000, 'AWAITING_APPROVAL', 'k5', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000a6', '11111111-1111-1111-1111-111111111111', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 10000, 10000, 'AWAITING_APPROVAL', 'k6', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000a8', '11111111-1111-1111-1111-111111111111', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 5000, 5000, 'AWAITING_APPROVAL', 'k8', null),
  ('00000000-0000-0000-0000-0000000000b7', '22222222-2222-2222-2222-222222222222', 'd0000000-0000-0000-0000-00000000000b',
   'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 1000, 1000, 'AWAITING_APPROVAL', 'k7', now() + interval '1 hour'),
  ('00000000-0000-0000-0000-0000000000b9', '22222222-2222-2222-2222-222222222222', null,
   'aaaaaaaa-0000-0000-0000-000000000001', 'USD', 1000, 1000, 'AWAITING_APPROVAL', 'k9', now() + interval '1 hour');

insert into public.audit_logs (user_id, actor_type, actor_id, event_type, details) values
  ('11111111-1111-1111-1111-111111111111', 'admin', 'admin-1', 'signature_failed', '{"reason": "bad key version"}');

-- ---------------------------------------------------------------------------
-- reserve_spending (limit 50000, policy currency MYR). Since migration
-- 20261025090000 the day is derived in SQL from 'Asia/Kuala_Lumpur'; users A
-- and B have separate budgets, so B's USD/MYR case still yields mixed_currency.
-- Fixture audit (Phase 2): merchant code 'test-grocer' avoids the grocer seed,
-- devices use 'ecdsa-p256-sha256' and distinct keys, no fixture updates
-- orders.status, and no authenticated select * on devices/conversation_events.
-- ---------------------------------------------------------------------------

select results_eq(
  $$ select success, reason, used_minor, remaining_minor from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a1', 30000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (true, 'reserved'::text, 30000::bigint, 20000::bigint) $$,
  'reserves the order total when within the cap'
);

select is(
  (select amount_minor from public.spending_records where order_id = '00000000-0000-0000-0000-0000000000a1'),
  30000::bigint, 'reserved amount equals orders.total_minor'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a1', 30000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (true, 'already_reserved'::text) $$,
  'idempotent re-reserve with the same amount'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a6', 100, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'amount_mismatch'::text) $$,
  'caller amount different from orders.total_minor is rejected'
);

select is(
  (select count(*) from public.spending_records where order_id = '00000000-0000-0000-0000-0000000000a6'),
  0::bigint, 'no record created on amount_mismatch'
);

select results_eq(
  $$ select success, reason, used_minor, remaining_minor from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a2', 30000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'daily_limit_exceeded'::text, 30000::bigint, 20000::bigint) $$,
  'second order over the cap is rejected'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a3', 5000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'invalid_order_status'::text) $$,
  'PAID order cannot be reserved'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a4', 5000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'quote_expired'::text) $$,
  'expired quote cannot be reserved'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a8', 5000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'quote_expired'::text) $$,
  'order without quote expiry cannot be reserved'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a5', 1000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'currency_mismatch'::text) $$,
  'order not in the policy currency is rejected'
);

select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000b7', 1000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'order_not_found'::text) $$,
  'another user''s order is not found'
);

update public.orders set subtotal_minor = 31000, total_minor = 31000
  where id = '00000000-0000-0000-0000-0000000000a1';
select results_eq(
  $$ select success, reason from public.reserve_spending(
       '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a1', 31000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'amount_mismatch'::text) $$,
  'existing reservation with a different amount is not reported as success'
);
update public.orders set subtotal_minor = 30000, total_minor = 30000
  where id = '00000000-0000-0000-0000-0000000000a1';

-- User B: a USD reservation exists, then an MYR order the same day.
select results_eq(
  $$ select success, reason from public.reserve_spending(
       '22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-0000000000b9', 1000, 'USD', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (true, 'reserved'::text) $$,
  'fixture: USD reservation for user B'
);
select results_eq(
  $$ select success, reason, used_minor from public.reserve_spending(
       '22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-0000000000b7', 1000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  $$ values (false, 'mixed_currency'::text, 1000::bigint) $$,
  'daily cap is one budget: other-currency usage blocks and is counted'
);

-- ---------------------------------------------------------------------------
-- Cross-owner composite FKs
-- ---------------------------------------------------------------------------

select throws_ok(
  $$ insert into public.approvals (order_id, user_id, device_id, approval_type, decision, nonce, signed_payload_hash)
     values ('00000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
             'd0000000-0000-0000-0000-00000000000b', 'gadget', 'approved', 'n-x1', 'h') $$,
  '23503', null, 'approval cannot use another user''s device'
);

select throws_ok(
  $$ insert into public.approvals (order_id, user_id, approval_type, decision)
     values ('00000000-0000-0000-0000-0000000000a1', '22222222-2222-2222-2222-222222222222', 'phone', 'approved') $$,
  '23503', null, 'approval user must match the order user'
);

select throws_ok(
  $$ insert into public.orders (user_id, device_id, merchant_id, currency, subtotal_minor, total_minor, idempotency_key)
     values ('11111111-1111-1111-1111-111111111111', 'd0000000-0000-0000-0000-00000000000b',
             'aaaaaaaa-0000-0000-0000-000000000001', 'MYR', 1, 1, 'k-x') $$,
  '23503', null, 'order cannot reference another user''s device'
);

select throws_ok(
  $$ insert into public.spending_records (user_id, order_id, amount_minor, currency, spending_day)
     values ('22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-0000000000a6', 1, 'MYR', '2026-01-01') $$,
  '23503', null, 'spending record user must match the order user'
);

select throws_ok(
  $$ insert into public.mandates (order_id, user_id, mandate_type)
     values ('00000000-0000-0000-0000-0000000000a1', '22222222-2222-2222-2222-222222222222', 'intent') $$,
  '23503', null, 'mandate user must match the order user'
);

select lives_ok(
  $$ insert into public.approvals (order_id, user_id, device_id, approval_type, decision, nonce, signed_payload_hash)
     values ('00000000-0000-0000-0000-0000000000a1', '11111111-1111-1111-1111-111111111111',
             'd0000000-0000-0000-0000-00000000000a', 'gadget', 'approved', 'n-ok-1', 'h') $$,
  'approval with own order and own device is accepted'
);

-- ---------------------------------------------------------------------------
-- Approvals: replay / single use
-- ---------------------------------------------------------------------------

select throws_ok(
  $$ insert into public.approvals (order_id, user_id, device_id, approval_type, decision, nonce, signed_payload_hash)
     values ('00000000-0000-0000-0000-0000000000a2', '11111111-1111-1111-1111-111111111111',
             'd0000000-0000-0000-0000-00000000000a', 'gadget', 'approved', 'n-ok-1', 'h') $$,
  '23505', null, 'gadget nonce cannot be reused'
);

insert into public.approvals (id, order_id, user_id, approval_type, decision, verification_status) values
  ('ac000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a2',
   '11111111-1111-1111-1111-111111111111', 'phone', 'approved', 'verified'),
  ('ac000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-0000000000a2',
   '11111111-1111-1111-1111-111111111111', 'phone', 'approved', 'verified');

select lives_ok(
  $$ update public.approvals set consumed_at = now() where id = 'ac000000-0000-0000-0000-000000000001' $$,
  'verified phone approval can be consumed'
);

select throws_ok(
  $$ update public.approvals set consumed_at = now() where id = 'ac000000-0000-0000-0000-000000000002' $$,
  '23505', null, 'a second phone approval for the same order cannot be consumed'
);

select throws_ok(
  $$ insert into public.approvals (order_id, user_id, approval_type, decision, verification_status, consumed_at)
     values ('00000000-0000-0000-0000-0000000000a6', '11111111-1111-1111-1111-111111111111',
             'phone', 'denied', 'verified', now()) $$,
  '23514', null, 'denied approval cannot be consumed'
);

select throws_ok(
  $$ insert into public.approvals (order_id, user_id, approval_type, decision, verification_status, consumed_at)
     values ('00000000-0000-0000-0000-0000000000a6', '11111111-1111-1111-1111-111111111111',
             'phone', 'approved', 'pending', now()) $$,
  '23514', null, 'unverified approval cannot be consumed'
);

-- ---------------------------------------------------------------------------
-- audit_logs append-only (even for the owner role)
-- ---------------------------------------------------------------------------

select throws_ok(
  $$ update public.audit_logs set event_type = 'x' $$,
  '42501', null, 'audit_logs update is blocked'
);
select throws_ok(
  $$ delete from public.audit_logs $$,
  '42501', null, 'audit_logs delete is blocked'
);

-- ---------------------------------------------------------------------------
-- authenticated as user A
-- ---------------------------------------------------------------------------

set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub": "11111111-1111-1111-1111-111111111111", "role": "authenticated"}', true);

select is((select count(*) from public.orders), 7::bigint, 'A sees only own orders');
select is((select count(*) from public.spending_records), 1::bigint, 'A sees only own spending records');
select lives_ok($$ select id, status from public.payment_methods $$, 'A can read non-sensitive payment method columns');
select throws_ok($$ select vault_token_reference from public.payment_methods $$,
  '42501', null, 'vault_token_reference hidden');
select throws_ok($$ select agent_endpoint_reference from public.merchants $$,
  '42501', null, 'merchant endpoint reference hidden');
select is((select count(id) from public.audit_logs), 1::bigint, 'A sees own audit rows');
select throws_ok($$ select details from public.audit_logs $$, '42501', null, 'audit_logs.details hidden');
select throws_ok($$ select actor_id from public.audit_logs $$, '42501', null, 'audit_logs.actor_id hidden');
select throws_ok($$ update public.orders set status = 'PAID' $$, '42501', null, 'A cannot update orders');
select throws_ok(
  $$ insert into public.spending_records (user_id, order_id, amount_minor, currency, spending_day)
     values ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-0000000000a6', 1, 'MYR', '2026-01-01') $$,
  '42501', null, 'A cannot insert spending records');
select throws_ok($$ update public.approvals set verification_status = 'verified' $$,
  '42501', null, 'A cannot change approval verification');
select throws_ok($$ select id from public.webhook_events $$, '42501', null, 'webhook_events not readable');
select throws_ok(
  $$ select * from public.reserve_spending('11111111-1111-1111-1111-111111111111',
       '00000000-0000-0000-0000-0000000000a6', 10000, 'MYR', 50000, 'Asia/Kuala_Lumpur') $$,
  '42501', null, 'A cannot execute reserve_spending');

-- authenticated as user B
select set_config('request.jwt.claims',
  '{"sub": "22222222-2222-2222-2222-222222222222", "role": "authenticated"}', true);
select is((select count(*) from public.orders), 2::bigint, 'B sees only own orders');

-- anon
reset role;
set local role anon;
select throws_ok($$ select id from public.orders $$, '42501', null, 'anon cannot read orders');

reset role;
select * from finish();
rollback;
