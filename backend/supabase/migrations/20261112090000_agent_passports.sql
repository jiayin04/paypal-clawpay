-- ClawPay agent passports (lite): a signed, revocable grant from a user to one
-- of their agent devices, plus a signed receipt for every order decision made
-- under a grant.
--
-- The grant document and signature are produced by the backend with its
-- Ed25519 issuer key (public key at /.well-known/agent-passport); the database
-- stores them verbatim so anyone can re-verify. Enforcement happens before an
-- order is created; spending caps and the signed device approval are unchanged.
--
-- Lock order (design §1.1): devices row -> agent_delegations row.

-- ---------------------------------------------------------------------------
-- agent_delegations
-- ---------------------------------------------------------------------------

create table public.agent_delegations (
  id uuid primary key,
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  device_id uuid not null,
  merchants text[] not null check (cardinality(merchants) between 1 and 10),
  max_order_minor bigint not null check (max_order_minor > 0),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  key_id text not null check (key_id ~ '^[0-9a-f]{16}$'),
  document text not null check (char_length(document) <= 4096 and document ~ '^[ -~]+$'),
  signature text not null check (signature ~ '^[A-Za-z0-9_-]{86}$'),
  created_at timestamptz not null default now(),
  constraint agent_delegations_device_owner_fkey foreign key (device_id, user_id)
    references public.devices (id, user_id) on delete cascade
);

-- At most one live (unrevoked) grant per device.
create unique index agent_delegations_one_active_per_device
  on public.agent_delegations (device_id) where revoked_at is null;
create index agent_delegations_user_created_idx on public.agent_delegations (user_id, created_at desc);

alter table public.agent_delegations enable row level security;
revoke all on public.agent_delegations from anon, authenticated;
-- The signed document is meant to be shared and verified; nothing here is secret.
grant select on public.agent_delegations to authenticated;
grant all on public.agent_delegations to service_role;
create policy agent_delegations_select_own on public.agent_delegations
  for select to authenticated using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- agent_receipts: one signed record per allowed or denied order decision.
-- order_id is null for denials (no order was created).
-- ---------------------------------------------------------------------------

create table public.agent_receipts (
  id uuid primary key,
  user_id uuid not null references public.user_profiles (id) on delete cascade,
  delegation_id uuid references public.agent_delegations (id) on delete cascade,
  device_id uuid not null,
  order_id uuid,
  decision text not null check (decision in ('allowed', 'denied')),
  reason text not null check (char_length(reason) <= 40),
  merchant_code text not null check (char_length(merchant_code) <= 40),
  amount_minor bigint check (amount_minor > 0),
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  key_id text check (key_id ~ '^[0-9a-f]{16}$'),
  document text not null check (char_length(document) <= 4096 and document ~ '^[ -~]+$'),
  signature text check (signature ~ '^[A-Za-z0-9_-]{86}$'),
  created_at timestamptz not null default now(),
  constraint agent_receipts_order_owner_fkey foreign key (order_id, user_id)
    references public.orders (id, user_id) on delete cascade
);

create index agent_receipts_user_created_idx on public.agent_receipts (user_id, created_at desc);
create index agent_receipts_delegation_idx on public.agent_receipts (delegation_id);
create index agent_receipts_order_idx on public.agent_receipts (order_id);

alter table public.agent_receipts enable row level security;
revoke all on public.agent_receipts from anon, authenticated;
grant select on public.agent_receipts to authenticated;
grant all on public.agent_receipts to service_role;
create policy agent_receipts_select_own on public.agent_receipts
  for select to authenticated using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- create_agent_delegation (device FOR SHARE -> that device's grants FOR UPDATE)
-- Expired live grants are revoked first, so an expired grant never blocks a new one.
-- reasons: created, device_not_found, device_revoked, delegation_exists.
-- A concurrent duplicate fails on agent_delegations_one_active_per_device (23505).
-- ---------------------------------------------------------------------------

create or replace function public.create_agent_delegation(
  p_id uuid,
  p_user_id uuid,
  p_device_id uuid,
  p_merchants text[],
  p_max_order_minor bigint,
  p_currency char(3),
  p_expires_at timestamptz,
  p_key_id text,
  p_document text,
  p_signature text
)
returns table (ok boolean, reason text, delegation_id uuid)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_status text;
begin
  if p_id is null or p_user_id is null or p_device_id is null or p_merchants is null
     or p_max_order_minor is null or p_currency is null or p_expires_at is null
     or p_key_id is null or p_document is null or p_signature is null then
    raise exception 'create_agent_delegation: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select d.status into v_status
  from public.devices d
  where d.id = p_device_id and d.user_id = p_user_id
  for share;

  if not found then
    return query select false, 'device_not_found'::text, null::uuid;
    return;
  end if;
  if v_status <> 'active' then
    return query select false, 'device_revoked'::text, null::uuid;
    return;
  end if;

  perform 1 from public.agent_delegations g
  where g.device_id = p_device_id and g.revoked_at is null
  order by g.id
  for update;

  update public.agent_delegations g
    set revoked_at = now()
    where g.device_id = p_device_id and g.revoked_at is null and g.expires_at <= now();

  if exists (
    select 1 from public.agent_delegations g
    where g.device_id = p_device_id and g.revoked_at is null
  ) then
    return query select false, 'delegation_exists'::text, null::uuid;
    return;
  end if;

  insert into public.agent_delegations (id, user_id, device_id, merchants, max_order_minor, currency,
                                        expires_at, key_id, document, signature)
  values (p_id, p_user_id, p_device_id, p_merchants, p_max_order_minor, p_currency,
          p_expires_at, p_key_id, p_document, p_signature);

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (p_user_id, 'user', p_user_id::text, 'delegation.created', 'device', p_device_id::text,
          jsonb_build_object('delegation_id', p_id, 'max_order_minor', p_max_order_minor,
                             'merchants', to_jsonb(p_merchants)));

  return query select true, 'created'::text, p_id;
end;
$$;

revoke execute on function public.create_agent_delegation(uuid, uuid, uuid, text[], bigint, char, timestamptz, text, text, text)
  from public, anon, authenticated;
grant execute on function public.create_agent_delegation(uuid, uuid, uuid, text[], bigint, char, timestamptz, text, text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- revoke_agent_delegation (delegation FOR UPDATE)
-- reasons: revoked, already_revoked, not_found (missing or another user's).
-- ---------------------------------------------------------------------------

create or replace function public.revoke_agent_delegation(p_id uuid, p_user_id uuid)
returns table (ok boolean, reason text)
language plpgsql
security invoker
set search_path = ''
as $$
#variable_conflict use_column
declare
  v_user_id uuid;
  v_device_id uuid;
  v_revoked_at timestamptz;
begin
  if p_id is null or p_user_id is null then
    raise exception 'revoke_agent_delegation: arguments must not be null'
      using errcode = 'null_value_not_allowed';
  end if;

  select g.user_id, g.device_id, g.revoked_at into v_user_id, v_device_id, v_revoked_at
  from public.agent_delegations g
  where g.id = p_id
  for update;

  if not found or v_user_id <> p_user_id then
    return query select false, 'not_found'::text;
    return;
  end if;
  if v_revoked_at is not null then
    return query select true, 'already_revoked'::text;
    return;
  end if;

  update public.agent_delegations g set revoked_at = now() where g.id = p_id;

  insert into public.audit_logs (user_id, actor_type, actor_id, event_type, entity_type, entity_id, details)
  values (p_user_id, 'user', p_user_id::text, 'delegation.revoked', 'device', v_device_id::text,
          jsonb_build_object('delegation_id', p_id));

  return query select true, 'revoked'::text;
end;
$$;

revoke execute on function public.revoke_agent_delegation(uuid, uuid) from public, anon, authenticated;
grant execute on function public.revoke_agent_delegation(uuid, uuid) to service_role;
