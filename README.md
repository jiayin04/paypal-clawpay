# ClawPay Middleware

Backend that sits between the ESP-Claw on-device agent (ESP32-S3 gadget), merchant agents, and PayPal. The device LLM will call ClawPay's MCP tools over HTTPS, and a firmware C task will sign order approvals. The backend owns orders, approvals, spending limits, and payment state, with Supabase Postgres as the source of truth. This is the Phase 0 + Phase 1 scaffold: app factory, typed config, redacting logs, health checks, Supabase client, and the initial database schema.

- Plan, decisions and roadmap: [../doc/impl_plan.md](../doc/impl_plan.md)
- MCP / device / approval contracts (proposed): [../doc/contracts.md](../doc/contracts.md)
- Setting up the backend and the ESP-Claw device together: [../doc/setup.md](../doc/setup.md)

## Phase 0 assessment

Greenfield. The workspace contained only `doc/impl_plan.md`: no existing backend, device/relay code, AP2 sample, AgentCore/A2A code, PayPal integration, schema, or tests to reuse. PayPal, AP2, A2A, AgentCore, and MCP modules are intentionally not created yet.

## Prerequisites

- Python 3.12 (pinned via `requires-python = ">=3.12,<3.13"`)
- [uv](https://docs.astral.sh/uv/) 0.10.x (dependency manager; `uv.lock` holds the resolved tree)
- Supabase CLI 2.x for migrations. Docker Desktop only if you want the optional local stack.

## Setup

```powershell
cd backend
uv sync --python 3.12
Copy-Item .env.example .env   # then fill in values; never commit .env
```

Direct dependencies are pinned to exact versions in `pyproject.toml`: fastapi 0.162.0, pydantic 2.14.1, pydantic-settings 2.16.1, supabase 2.33.0, uvicorn 0.54.0, tzdata 2026.5 (IANA zones on Windows); dev: pytest 9.1.1, httpx 0.28.1.

## Environment variables

Money values are integers in minor units (sen for MYR): RM100 = `10000`, RM500 = `50000`.

| Variable | Needed now | Meaning |
| --- | --- | --- |
| `APP_ENV` | no (default `development`) | `development` / `test` / `staging` / `production`. Production disables `/docs` and `/openapi.json`. |
| `APP_HOST`, `APP_PORT` | no | Bind address hints (pass to uvicorn). |
| `LOG_LEVEL` | no (default `INFO`) | Root log level. |
| `SUPABASE_URL` | for `/health/ready` | Supabase project API URL. |
| `SUPABASE_SERVICE_ROLE_KEY` | for `/health/ready` | Server-side only. Bypasses RLS. |
| `SUPABASE_ANON_KEY` | no | Reserved for later user-scoped calls. |
| `PAYPAL_ENV`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` | no (Phase 2) | PayPal Sandbox credentials. Secret is server-side only. |
| `GADGET_APPROVAL_PUBLIC_KEY_REGISTRY` | no | Placeholder; device keys live in the `devices` table. |
| `AGENTCORE_REGION`, `PAYPAL_AGENT_ENDPOINT`, `GROCER_AGENT_ENDPOINT`, `ELECTRONICS_AGENT_ENDPOINT` | no | Agent hosting / A2A endpoints (later phases). |
| `INTERNAL_SERVICE_TOKEN` | no | Admin / server-to-server token (device registration and agent calls into internal endpoints, Phase 2). Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `PUBLIC_BASE_URL` | no (Phase 2) | Public HTTPS origin the ESP-Claw device calls (tunnel or host, CA-signed cert). |
| `MCP_ENABLED` | no (default `false`) | Mounts `POST /mcp` once implemented. |
| `DEVICE_TOKEN_PEPPER` | no (Phase 2) | HMAC pepper for hashing device tokens at rest. Server-side only. |
| `LLM_PROXY_UPSTREAM_BASE_URL`, `LLM_PROXY_UPSTREAM_API_KEY`, `LLM_PROXY_MODEL` | no (Phase 4) | OpenAI-compatible LLM proxy for the device, so the LLM key stays off the device. The key is server-side only. |
| `PHONE_APPROVAL_THRESHOLD_MINOR` | no (default `10000`) | Phone approval required for totals strictly above this. The spec's `PHONE_APPROVAL_THRESHOLD_MYR` / `DAILY_SPENDING_LIMIT_MYR` names are rejected at startup (not silently ignored) so a major-unit value can't be mistaken for minor units. |
| `DAILY_SPENDING_LIMIT_MINOR` | no (default `50000`) | Daily cap. Must be >= the threshold. |
| `DEFAULT_CURRENCY` | no (default `MYR`) | ISO 4217 code, normalised to upper case. |
| `SPENDING_DAY_TIMEZONE` | no (default `Asia/Kuala_Lumpur`) | Calendar day boundary for the daily cap. |

Secrets are held as `SecretStr` and are scrubbed from logs. Keep the service-role key, PayPal secret, internal token, device-token pepper, and LLM upstream key out of the dashboard, public frontend variables, and ESP32 firmware. Use the hosting platform's secret manager when deployed.

## Run

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET /health`: liveness, always 200, no secrets.
- `GET /health/ready`: 200 `{"status":"ready","checks":{"supabase":"ok"}}` when Supabase is reachable and the migration is applied (it queries `merchants`). Otherwise 503 with `supabase` = `not_configured` (env vars missing) or `error` (unreachable / schema missing). Error details are never returned. The probe result is cached for 5 seconds so the unauthenticated endpoint can't drive database load; still keep it off the public ingress or behind rate limiting when deployed.

## Test

```powershell
uv run pytest
```

Tests live in `tests/` and need no network or Supabase (the client is mocked).

Database tests (pgTAP, `supabase/tests/database/`) cover `reserve_spending` validation, cross-owner FKs, approval single use, audit append-only, and the `authenticated`/`anon` privilege and RLS matrix. They run against the optional local stack and roll back all data (see below).

## Database (Supabase)

Primary target is a hosted Supabase project. Run from `backend/`:

```powershell
supabase login
supabase link --project-ref <ref>   # prompts for the database password
supabase db push --dry-run
supabase db push
```

Then set `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` in `.env` from the project's API settings and check `GET /health/ready`.

Local Supabase is optional, for validating migrations before a push (needs Docker Desktop). It was used for Phase 1 validation and then removed:

```powershell
supabase start        # first run pulls Docker images
supabase db reset     # re-applies supabase/migrations/*
supabase test db      # pgTAP suite
supabase stop
```

Schema (`supabase/migrations/*_initial_schema.sql`): `user_profiles`, `devices`, `payment_methods`, `merchants`, `orders`, `order_items`, `mandates`, `approvals`, `spending_records`, `payment_attempts`, `conversations`, `conversation_events`, `audit_logs`, `webhook_events`. Money is `bigint` `*_minor` with a `currency char(3)` column.

Security model:

- RLS is on for every table. `anon` has no access.
- Signed-in users can only `SELECT` their own rows (`user_profiles` also allows editing `display_name`). All writes go through the backend with the service role.
- `payment_methods.vault_token_reference` / `provider_customer_reference` and `merchants.agent_endpoint_reference` are hidden from clients with column-level grants.
- `audit_logs` is append-only (update/delete/truncate raise, even for service role). `audit_logs.user_id` has no FK so history survives user deletion. Users can read their own rows but not `details` or `actor_id`.
- Composite FKs keep related rows on the same user: an approval's order and device, an order's or conversation's device, and a mandate's or spending record's order must all belong to that row's `user_id`.
- Approvals are single use at the DB level: `consumed_at` can only be set on a verified `approved` row, and at most one approval per `(order_id, approval_type)` can be consumed. Phase 2 should still consume with `update ... where consumed_at is null`.
- `webhook_events` is service-role only; `(provider, event_id)` is unique for deduplication.
- Idempotency: `orders (user_id, idempotency_key)` and `payment_attempts (idempotency_key)` are unique. `approvals.nonce` is unique for replay protection.
- `reserve_spending(p_user_id, p_order_id, p_amount_minor, p_currency, p_daily_limit_minor, p_spending_day)` takes a per-user-per-day advisory lock, locks the order row, and validates it in the same transaction: the order must belong to the user, be `AWAITING_APPROVAL` / `APPROVED` / `PAYMENT_PENDING`, have an unexpired `quote_expires_at`, and `p_amount_minor` must equal `orders.total_minor`. `p_currency` is the policy currency (pass `DEFAULT_CURRENCY`): the order must be in it, and usage is summed across all currencies so the cap is one budget. It inserts a `reserved` record for the order total only if committed + reserved + total <= limit. It is idempotent per order (a different amount returns `amount_mismatch`) and executable only by `service_role`. Possible `reason` values: `reserved`, `already_reserved`, `order_not_found`, `currency_mismatch`, `mixed_currency`, `invalid_order_status`, `quote_expired`, `amount_mismatch`, `daily_limit_exceeded`.

Spending lifecycle: `reserved` → `committed` on confirmed payment, or `released` on failure, expiry, or cancellation (backend-managed). Refunds do not release spending yet; that policy is still open.

## Open assumptions to validate before Phase 2

- PayPal Sandbox: vaulting a PayPal Wallet without a purchase, setup-token → payment-token exchange, and merchant-initiated charging on the saved token.
- AP2: actual mandate fields and validation method in the chosen sample (`mandates.mandate_type` is free text until then).
- ESP-Claw integration: MCP server, device tokens, approval challenge and gadget signature (ECDSA P-256) are proposed in [../doc/contracts.md](../doc/contracts.md) and not implemented. `conversations.source` still defaults to `'xiaozhi'` from before the switch to ESP-Claw; change it in the Phase 2 migration.
- AgentCore as the agent host is not yet confirmed.
- Refund vs. spending-cap policy. The daily cap is a single budget in the policy currency; orders in other currencies are rejected by `reserve_spending` until a conversion policy exists.
- Reservation is allowed from `AWAITING_APPROVAL` onwards and requires `quote_expires_at` to be set; revisit if Phase 2 reserves at a different step.
- Supabase Auth JWT verification for user endpoints and `INTERNAL_SERVICE_TOKEN` auth for internal endpoints are not implemented yet (no such endpoints exist).

## Known limitations

- Only health endpoints exist; no `/api/v1` routes yet.
- Starlette emits a deprecation warning that its test client prefers `httpx2`; httpx 0.28.1 is kept because the spec names HTTPX and `supabase` 2.33.0 requires `httpx<0.29`.
- On Windows, `.venv\Scripts\python.exe` is a launcher that spawns a child process; stop a background server with `taskkill /PID <pid> /T /F`, not just the parent PID.
