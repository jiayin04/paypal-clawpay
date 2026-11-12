> Historical implementation plan. For the current system, use the [backend guide](../../README.md) and [architecture](../architecture.md).

# ClawPay Middleware — Detailed Engineering Implementation Specification

Companion documents:

- [contracts.md](../contracts.md): concrete MCP, device REST, approval-payload and LLM-proxy contracts.
- [setup.md](../setup.md): Windows setup for the backend and the ESP-Claw device together.
- [../README.md](../../README.md): backend commands, environment variables, database security model.

Status, decisions and open items are in sections 19–21.

## 1. Project Overview

Build the middleware backend for ClawPay, an agentic payment system driven from an ESP32-S3 gadget.

The gadget runs Espressif [ESP-Claw](https://github.com/espressif/esp-claw) (`edge_agent`), an on-device LLM agent. The user talks to it by text (Telegram or the local Web Chat; push-to-talk voice is a stretch goal). The device LLM calls ClawPay's MCP tools over public HTTPS. ClawPay coordinates merchant agents, constructs and validates the order, and returns an order summary. A private C task on the device fetches the authoritative approval challenge, shows it on screen, waits for a physical button hold, signs it, and posts the signature. ClawPay verifies the signature and policies, charges the user's linked PayPal payment method, and exposes the result to the device and the dashboard.

Example interaction:

1. User links PayPal once through a setup page.
2. User registers their gadget (admin-gated, one time).
3. User sends "buy milk and bread" via Telegram or Web Chat to the device.
4. The device LLM calls `search_products` and `create_order` on ClawPay's MCP server.
5. ClawPay contacts the Grocer merchant agent over A2A.
6. The merchant agent returns product details and a quote.
7. ClawPay validates the quote, creates the order (no payment yet) and returns `awaiting_device_approval`.
8. The device LLM calls `clawpay_request_approval {order_id}` (C capability). The C approver task fetches the challenge, displays merchant and total, and signs after a ≥1.5 s button hold.
9. ClawPay verifies the signature, nonce, expiry and spending limits.
10. If the order exceeds RM100, the phone must also approve (deferred; see section 20).
11. ClawPay executes the eligible PayPal Sandbox payment.
12. The payment result is persisted. The device LLM reads it with `get_order_status` and tells the user.
13. The dashboard displays the conversation, merchant-agent interactions, mandate, order, approval and payment status.

The daily spending cap is RM500. The phone-approval threshold is RM100, with phone approval required for orders strictly above RM100. These policies are configurable and enforced server-side.

## 2. Scope Boundaries

### Implement in this repository

- FastAPI middleware and API contracts.
- Supabase schema, migrations, RLS policies and data access.
- PayPal Sandbox linking and payment integration.
- Order management and payment state transitions.
- Spending limits and approval verification.
- AP2 mandate integration.
- MCP Streamable-HTTP server for the ESP-Claw device LLM, and device REST endpoints.
- Device registry and device-token issuance.
- A2A integration with Grocer and Electronics merchant agents (mock catalogs first).
- Optional OpenAI-compatible LLM proxy so the LLM key stays off the device.
- Optional STT/TTS endpoints for push-to-talk voice.
- Dashboard-facing APIs and real-time event publishing.
- Automated tests, documentation and deployment configuration.

### Firmware work (separate ESP-Claw fork, not this backend)

Listed so the contracts match. Tracked in [setup.md](../setup.md) section E.

- `skills/clawpay/SKILL.md` telling the device LLM the server URL, tools and rules.
- `cap_mcp_client` patch: send `Authorization: Bearer <device_token>`.
- `cap_clawpay` C component: key generation in NVS namespace `clawpay`, `clawpay_request_approval`, private approver task (display, direct-GPIO hold, ECDSA P-256 sign, POST).
- Lua lockdown: disable `lua_driver_gpio` and `lua_module_event_publisher`.
- Stretch: `cap_clawpay_voice` push-to-talk.

### Out of scope

- ESP-Claw internals beyond the patches above, wake word, face recognition.
- Hardware wiring, soldering and pin mapping.

Do not assume ESP-Claw behavior that has not been verified. The verified facts used here come from [esp-claw-investigation.md](../esp-claw-investigation.md) (source reading of esp-claw `9b23c15`; nothing built or flashed).

## 3. Architecture and Technology Choices

Use:

- Python 3.12, FastAPI, Pydantic.
- Supabase PostgreSQL, Supabase Auth for user identity, Supabase Realtime for dashboard events where useful.
- SQL migrations managed through the Supabase CLI. Target: a hosted Supabase project. Local Supabase is optional, for validation only.
- PayPal REST APIs in Sandbox.
- MCP (Streamable HTTP, stateless) as the device → ClawPay tool transport.
- A2A for merchant-agent communication.
- Amazon Bedrock AgentCore: KIV (parked). Not used until a concrete need appears.
- Pytest and HTTPX for testing.

Dependency manager: `uv` with `pyproject.toml` and `uv.lock` (established).

Do not introduce a separate PostgreSQL server, Redis, Kafka, Celery or another orchestration framework without a demonstrated requirement.

Supabase is the source of truth for orders, approvals, spending accounting and payment state. External agents and LLMs, including the device LLM, are not the source of truth for payment authorization.

```text
[User] --text (Telegram / Web Chat)--> [ESP-Claw device LLM]
   |                                        | MCP tools (HTTPS, bearer device token)
   | button hold                            v
[cap_clawpay C task] --challenge/sign--> [ClawPay FastAPI] --A2A--> [Grocer / Electronics agents]
                                            |  \--> PayPal Sandbox (Orders/Vault)
   device LLM --(optional)--> /llm/v1 proxy |  \--> Supabase (source of truth) --> Dashboard
```

## 4. Repository Assessment

Done in Phase 0: greenfield. No existing Xiaozhi/relay, AP2, AgentCore, A2A or PayPal code to reuse. See [../README.md](../../README.md).

## 5. Backend Structure

The implemented layout is maintained in [architecture.md](../architecture.md). Repository interfaces live in `app/repositories/ports/<feature>.py`, concrete Supabase adapters in `app/repositories/supabase/<feature>.py`, and shared fake adapters in `tests/fakes/repositories/`. The container wires them together. Tracked `docs/` and tests are self-contained within this repository; `.agents/` is local workflow scratch.

Business services stay independent of provider and transport formats. `integrations/mcp/` only translates JSON-RPC to service calls; `payment_service.py` enforces business rules; `integrations/paypal/` handles PayPal requests.

## 6. Environment Configuration

`backend/.env.example` is the authoritative template. Integration-related variables:

```dotenv
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_WEBHOOK_ID=

AGENTCORE_REGION=            # KIV
PAYPAL_AGENT_ENDPOINT=
GROCER_AGENT_ENDPOINT=
ELECTRONICS_AGENT_ENDPOINT=
INTERNAL_SERVICE_TOKEN=      # admin / server-to-server (device registration in Phase 2)

PUBLIC_BASE_URL=             # public HTTPS origin the device calls (tunnel or host)
MCP_ENABLED=false            # mounts POST /mcp once implemented
DEVICE_TOKEN_PEPPER=         # HMAC pepper for device tokens at rest

LLM_PROXY_UPSTREAM_BASE_URL= # e.g. https://api.openai.com/v1
LLM_PROXY_UPSTREAM_API_KEY=
LLM_PROXY_MODEL=

PHONE_APPROVAL_THRESHOLD_MINOR=10000
DAILY_SPENDING_LIMIT_MINOR=50000
DEFAULT_CURRENCY=MYR
SPENDING_DAY_TIMEZONE=Asia/Kuala_Lumpur
```

Money configuration is in minor units: RM100 = 10000 sen, RM500 = 50000 sen. The spec's original `*_MYR` names are rejected at startup.

Never commit `.env` or real credentials. Do not place service-role keys, PayPal secrets, the device-token pepper or the LLM upstream key in the dashboard, public frontend variables or ESP firmware. The device holds only its own device token and its own private signing key. Use the hosting platform's secret manager when deployed.

## 7. Supabase Database Design

Implemented in `backend/supabase/migrations/*_initial_schema.sql`: `user_profiles`, `devices`, `payment_methods`, `merchants`, `orders`, `order_items`, `mandates`, `approvals`, `spending_records`, `payment_attempts`, `conversations`, `conversation_events`, `audit_logs`, `webhook_events`. Column-level detail and the security model are in [../README.md](../../README.md).

Requirements that still apply to every new migration:

- Do not store private signing keys, PayPal passwords, raw card data or unnecessary payment credentials.
- Do not treat a client-submitted `verified=true` flag as valid verification.
- Never assume a payment attempt succeeded because a request was sent.
- Exclude secrets and unnecessary sensitive data from transcripts and logs.
- Audit records are not editable by ordinary users. Audit: order creation, approval/denial, signature failure, spending-cap rejection, payment initiation/completion, webhook processing, device registration/revocation, token issuance, administrative changes.
- RLS on every user-facing table, explicit grants, queries scoped to the user, writes only through the trusted backend.

Phase 2 schema additions:

- `devices`: add `token_hash` (HMAC-SHA256 of the device token with `DEVICE_TOKEN_PEPPER`), `token_issued_at`, `last_seen_at`. Never store the raw token. `key_algorithm` = `ecdsa-p256-sha256`.
- `approval_challenges`: `id`, `order_id`, `device_id`, `user_id`, `nonce` (unique), `canonical_payload` (exact bytes), `payload_sha256`, `key_version`, `expires_at`, `consumed_at`. A challenge is consumed atomically when its approval is accepted.
- Phase 2 hardening from review (section 21).

## 8. Order and Payment State Machine

Database states (uppercase, existing CHECK constraint):

`CREATED → QUOTED → AWAITING_APPROVAL → APPROVED → PAYMENT_PENDING → PAID`

Terminal or recovery: `DENIED`, `EXPIRED`, `CANCELLED`, `PAYMENT_FAILED`, `REQUIRES_RECONCILIATION`.

API and MCP expose lowercase names. `AWAITING_APPROVAL` is rendered as `awaiting_device_approval` while the gadget approval is outstanding. The full mapping is in [contracts.md](../contracts.md#8-order-state-machine).

Rules:

- `create_order` ends at `AWAITING_APPROVAL`. It never charges and never creates a capturable PayPal payment.
- Payment stays blocked until every required condition holds: verified gadget approval, phone approval when total > threshold, verified mandate, spending reservation, active payment method.
- A central transition validator rejects illegal transitions and records actor and reason for every security-sensitive transition.

## 9. API Contracts

Versioned REST endpoints under `/api/v1`; MCP at `/mcp`; LLM proxy at `/llm/v1`. Request/response bodies, auth and error codes are in [contracts.md](../contracts.md). Summary:

| Endpoint | Caller | Auth | Phase |
| --- | --- | --- | --- |
| `GET /health`, `GET /health/ready` | ops | none | done |
| `POST /mcp` | device LLM (`cap_mcp_client`) | bearer device token | 2 |
| `POST /api/v1/devices/register` | admin | `INTERNAL_SERVICE_TOKEN` | 2 |
| `GET /api/v1/devices`, `DELETE /api/v1/devices/{device_id}` | user (dashboard) | Supabase JWT | 2 |
| `GET /api/v1/orders/{order_id}/approval-challenge` | device C task | bearer device token | 3 |
| `POST /api/v1/orders/{order_id}/gadget-approval` | device C task | bearer device token | 3 |
| `GET /api/v1/devices/{device_id}/pending-approvals` (optional) | device C task | bearer device token | 3 |
| `POST /api/v1/payment-methods/paypal/setup`, `GET`, `DELETE` | user | Supabase JWT | 3 |
| `GET /api/v1/orders/{order_id}`, `POST .../cancel` | user | Supabase JWT | 2 |
| `POST /api/v1/orders/{order_id}/phone-approval` | user | Supabase JWT | deferred |
| `GET /api/v1/spending/summary` | user | Supabase JWT | 2 |
| `POST /api/v1/webhooks/paypal` | PayPal | webhook signature | 4 |
| `POST /llm/v1/chat/completions` (optional) | device LLM | bearer device token | 4 |
| `POST /api/v1/voice/stt`, `/tts` (optional) | device C task | bearer device token | stretch |

Rules carried from the original spec:

- Device revocation immediately blocks MCP calls, challenges and approvals from that device.
- The identity of the caller always comes from the token. Never accept `device_id` or `user_id` from MCP tool arguments.
- Gadget approval signatures cover a canonical payload that binds device, order, merchant, amount, currency, cart, expiry and a single-use nonce. The server provides the exact bytes; the device does not build them.
- Verify the key is registered, active and at the expected `key_version`.
- Phone approval requires an authenticated user and an explicit decision. The server decides whether step-up is required.
- There is no generic charge endpoint. Payment runs only inside the trusted pipeline after verified approval, and validates state, quote validity, ownership, mandate, approvals, spending capacity and payment-method reference. Use database-backed idempotency; if a previous attempt is uncertain, reconcile with PayPal rather than charging again.
- PayPal webhooks are verified with PayPal's documented process, persisted and deduplicated before business effects, and safe under reordering and repeats.

## 10. Spending-Limit Rules

- Phone approval is required when the order total is greater than RM100.
- The daily cap is RM500.
- Both are enforced on the backend.
- Concurrent orders must not collectively exceed the limit (`reserve_spending` advisory lock; implemented).
- Reservations are released or reconciled when a payment fails, expires or is cancelled.
- Successful payments are counted exactly once.
- Refunds follow a documented policy (open, section 20) rather than silently resetting usage.
- Calendar days in `Asia/Kuala_Lumpur`.
- Reservation and state validation happen in one database transaction.

## 11. PayPal Integration

Provider adapter isolating PayPal from business services. Suggested interface: `create_setup_flow`, `retrieve_setup_result`, `get_payment_method`, `create_payment`, `get_payment_status`, `verify_webhook`. Exact methods follow the chosen APIs.

Sandbox accounts: Malaysian merchant and buyer accounts with MYR. If MYR or vault-without-purchase fails in Sandbox, fall back to USD; currency stays configurable (`DEFAULT_CURRENCY`).

Validate in Phase 3 before building on it:

- Linking a PayPal Wallet without a purchase (Vault v3 setup token → payment token) for a Malaysian account.
- Required buyer consent and the returned reusable token.
- Merchant-initiated charging rules on the saved token.
- Whether creating an order with a vaulted `payment_source` completes the payment immediately. If it does, the PayPal order must only be created after verified approval, never in `create_order`.
- Orders/capture APIs, webhook verification and reconciliation.

If the saved-method flow is not supported, report the limitation and propose a tested alternative. Do not hardcode unsupported assumptions.

## 12. Agent Integration

### Orchestration

No separate orchestrator LLM for now. The ESP-Claw device LLM (via the ClawPay LLM proxy once available) is the orchestrator. It only calls ClawPay's deterministic MCP tools: `search_products`, `create_order`, `get_order_status`, `get_spending_summary`. No MCP tool approves, captures or charges. Add a server-side orchestrator agent only if a demonstrated need appears.

### Merchant agents

Grocer and Electronics start as deterministic A2A services with mock catalogs. Add an LLM to a merchant agent only if needed.

Shared request/response contract: merchant identity, products (sku, name, unit price minor, currency), quantity, totals, quote expiry, request correlation ID. Treat agent responses as untrusted: validate schema, merchant identity, quote expiry and recompute totals before creating an order.

### Skills

- Shop: product discovery and order building (MCP tools above).
- Check payments: order status and spending summary (read-only).
- Transfer: undefined (section 20). Not implemented until its meaning and authorization are agreed.

Any financial action goes through the same trusted services and policy checks as every other caller.

## 13. AP2 Mandate Integration

Inspect the actual AP2 sample and its supported contracts. Implement an adapter that creates or receives the mandate artifact, persists references and status, verifies it with the sample's actual validation method, binds it to user and order, checks amount, currency, expiry and authorization, and rejects mismatches before payment.

Working direction (open, section 20): the gadget signature over the canonical payload serves as the user's authorization of the cart mandate, so there is one user signature per order. The payload includes `cart_sha256` for this reason.

Do not invent AP2 fields or assume a sample mandate authorizes every PayPal transaction.

## 14. ESP-Claw Integration

Verified constraints (investigation report):

- `cap_mcp_client` speaks MCP Streamable HTTP: JSON-RPC POST, `Accept: application/json, text/event-stream`, protocol `2025-11-25`, ESP-IDF CA bundle (public CA cert required), 20 s timeout, fresh session per call (`initialize` then the call), only `content[].text` is passed to the LLM.
- No server registry: the LLM passes `server_url` and `tool_name` each call, so the URL lives in `skills/clawpay/SKILL.md`.
- No auth header is sent today. Bearer token needs a ~10-line firmware patch; temporary fallback is the path token `/mcp/<device_token>` (leaks into LLM context and logs; demo only).
- ClawPay cannot call into the device (NAT, no TLS, no auth). The device pulls: the approval task fetches the challenge, optionally long-polling pending approvals.
- LLM-written Lua can call any registered capability and forge events. Approval and signing must be a C task that reads the GPIO directly and is not an LLM-callable signing capability.
- Voice is not built in. Guaranteed demo input is Telegram or Web Chat text; push-to-talk is a stretch.

Middleware requirements:

- Stateless MCP server at `POST /mcp`: accept `initialize`, `notifications/initialized`, `tools/list`, `tools/call`; JSON responses; 405 on GET; no session state required.
- Bearer device token → device → user lookup on every request; revoked or unknown → 401.
- Tool results: compact JSON in `content[0].text`, under ~4 KB, `isError: true` with text on failure, and short descriptions that tell the LLM what to say.
- Respond well within 20 s. Merchant A2A calls need their own shorter timeouts (e.g. 8 s) so `create_order` never exceeds the device timeout.
- Rate-limit per device token. Validate every argument with Pydantic.
- Persist each tool call as a `conversation_events` row (user-safe payload only; see section 21 finding 3).

## 15. Dashboard Integration

Expose APIs and events for: conversation (device chat turns and MCP tool calls), A2A merchant requests/responses, AP2 mandate status, order items and total, gadget approval and phone step-up, payment attempts and authoritative status, daily spending summary, audit events.

Use Supabase Realtime selectively. Scope every event to the authenticated user or authorized operator. Never expose provider secrets, payment tokens, device tokens, private keys or unrestricted internal tool output.

## 16. Testing Requirements

### Unit

Order totals; RM100 boundary; RM500 cap; reservation concurrency; expired challenge/approval; invalid signature; wrong key version; revoked device; replayed nonce; payload mismatch; mandate mismatch; illegal state transition; duplicate idempotency key; MCP argument validation; `device_id` in tool arguments ignored.

### Integration

Supabase persistence; mock merchant A2A flow; mock PayPal linking and payment; webhook deduplication; payment timeout and reconciliation; phone approval and denial (when built).

### Contract

- MCP: `initialize` → `tools/list` → `tools/call` replayed exactly as `cap_mcp_client` sends them (fresh `initialize` before each call, no session ID), GET → 405, missing/invalid bearer → 401, result shape `content[].text` only.
- Device REST: challenge and gadget-approval with a test P-256 key; signature produced over the exact challenge bytes.

### End-to-end

1. User links a Sandbox payment method.
2. Text request on the device (Telegram or Web Chat).
3. Merchant agent returns a quote.
4. Backend creates and validates the order.
5. Device fetches challenge, signs after button hold; backend verifies signature and mandate.
6. Orders above RM100 are blocked until phone approval exists.
7. Backend checks the daily cap.
8. Eligible payment executes in Sandbox.
9. PayPal confirms the result (polling reconciliation; webhook when a public URL is configured).
10. Device LLM reports status from `get_order_status`; dashboard shows the final state.

Include failure cases, not only the happy path. Steps 2 and 5 can be simulated with an MCP test client and a software P-256 key before hardware is ready.

## 17. Security Requirements

- Do not store PayPal passwords or raw card data.
- Keep all server secrets out of the frontend and firmware. The device holds only its own device token and signing key.
- Never trust a client-supplied payment status, spending balance or approval verification flag.
- Never trust amounts, merchant or `device_id` supplied by the LLM; derive them from server state and the token.
- Validate every order and payment amount on the server.
- Authenticate and authorize every sensitive endpoint.
- Verify gadget signatures and enforce single-use nonces and expiry.
- Approval and signing on the device live in C, outside reach of LLM-written Lua.
- Verify PayPal webhooks.
- Use idempotency and reconciliation for external payment calls.
- Restrict RLS policies and server-side database permissions.
- Redact secrets and sensitive values (including device tokens) from logs.
- TLS with a public-CA certificate for every device-facing endpoint.
- Sandbox credentials only for development and testing.
- ESP-Claw demo hygiene: Web Console is unauthenticated plain HTTP and exposes NVS; set a SoftAP password, enable SoftAP auto-disable, keep the device on a private network, use an LLM key with a spending cap (or the ClawPay proxy), and never burn eFuses on the only board.

## 18. Execution Instructions

Backend phases 0–4 are implemented; see section 21 and `docs/verification.md`. Hosted database execution and real-device/live-PayPal validation remain operator work.

Do not attempt the entire integration in one pass. For every phase, report: files created or modified, design decisions, commands to run, tests executed with actual results, known limitations, remaining tasks and dependencies. Only move to the next phase after the current phase works and its acceptance criteria are met. Update [contracts.md](../contracts.md) from "Proposed" to "Implemented" as each contract lands.

## 19. Decisions Log

| # | Decision | Notes |
| --- | --- | --- |
| D1 | On-device agent is Espressif ESP-Claw, replacing Xiaozhi. | Xiaozhi was the original assumption in this spec. Meta Muse Agent was considered and rejected: US/Canada only, user is in Malaysia. |
| D2 | Device → ClawPay transport is a stateless MCP Streamable-HTTP server at `POST /mcp` on public HTTPS with a CA-signed cert. | Matches `cap_mcp_client` behavior. |
| D3 | Device auth is a bearer device token (needs `cap_mcp_client` patch). Temporary fallback: `/mcp/<device_token>`. | Path token is demo-only. |
| D4 | MCP tools: `search_products`, `create_order`, `get_order_status`, `get_spending_summary`, returning text-JSON content. No approve or capture tool over MCP. | |
| D5 | Device endpoints: approval challenge, gadget approval (ECDSA P-256 SHA-256, base64url DER, single-use nonce, server-provided canonical bytes), admin-gated registration; optional pending-approvals, STT/TTS, LLM proxy. | See [contracts.md](../contracts.md). |
| D6 | Approval on the device is a C task, not Lua and not an LLM-callable signing capability. | LLM-written Lua can call any capability and forge events (investigation section 6). |
| D7 | Guaranteed demo input is Telegram or Web Chat text; push-to-talk voice is a stretch goal. | Voice is not built into ESP-Claw. |
| D8 | PayPal Sandbox uses Malaysian accounts with MYR; fall back to USD if MYR or vault-without-purchase fails. Currency stays configurable. | |
| D9 | AgentCore is KIV. Merchant agents start as deterministic A2A services with mock catalogs; LLM only if needed. | |
| D10 | No separate orchestrator LLM: the device LLM (via the ClawPay LLM proxy) orchestrates by calling deterministic MCP tools. | |
| D11 | Phone approval (> RM100) is deferred but still enforced: such orders stay blocked until phone approval is implemented or the policy is changed. | |
| D12 | Supabase: local stack was used only to validate migrations and has been removed. The user creates a hosted project and runs `supabase login` / `supabase link --project-ref <ref>` / `supabase db push`. | |
| D13 | Approval payload money is `amount_minor` (integer) + `currency`. | Rationale in [contracts.md](../contracts.md#5-canonical-approval-payload). |
| D14 | MCP server is hand-rolled JSON-RPC 2.0 (`app/integrations/mcp/`), not the `mcp` SDK. | Exact control of 202/405/401, stateless behaviour, 64 KB request and 3900-byte response caps; the surface is 5 methods. Implemented in Phase 2. |
| D15 | Merchant protocol is a hand-rolled A2A JSON-RPC subset (`message/send` with one `data` part, agent card), not `a2a-sdk`. Mock agents run in-process via `httpx.ASGITransport` unless `*_AGENT_ENDPOINT` is set. | Deterministic mocks, no new dependency, no public route. Implemented in Phase 2. |
| D16 | Every multi-row or money/approval state change is one Postgres function called with the service role (global lock order device → order → challenge → payment method → spending lock), retried once on deadlock/serialization failure. | One transaction per PostgREST call. Phase 2 RPCs: `register_device`, `revoke_device`, `create_order_from_quote`, `cancel_order`, `expire_order`, `expire_stale_orders`, `spending_summary`. |
| D17 | `create_order` creates the DB order only after a validated quote, in `AWAITING_APPROVAL`; no PayPal call. Identical cart from the same device within 60 s returns the first order. | The device sends no idempotency key and JSON-RPC ids restart every session. PayPal order creation stays after approval (Phase 3). |
| D18 | Registration issues two tokens: `cpd_` device token (MCP, device REST) and `cpl_` LLM token (LLM proxy only). | The LLM key sits in Web-Console-exposed NVS; a leaked `cpl_` cannot create or deny orders. |
| D19 | Owner self-registration exists behind `DEVICE_OWNER_REGISTRATION_ENABLED` and stays off; admin token only. Path-token MCP (`MCP_PATH_TOKEN_ENABLED`) is off by default. | User decisions Q4/Q8. |

Final decisions: MCP uses the hand-written stateless JSON-RPC handler; merchants use the deterministic A2A subset. Polling is primary and webhooks are optional. Pending payments keep reservations; refunds do not restore capacity. Provider orders are created only after approval. Rate limits and LLM budgets require one uvicorn worker. Dashboard frontend, AG Grid, Bryntum, Render, Channel3 and voice are out of scope.

## 20. Open Items

| Item | Blocking | Notes |
| --- | --- | --- |
| Board confirmation | Phase 3 hardware test | User has an "ESP32-S3"; exact module unconfirmed. Needs ≥ 8 MB flash and 8 MB octal PSRAM (N8R8 / N16R8); mic + speaker + screen for the full demo. N4R2/N8R2/N16R2 are not enough. Check with `esptool flash_id` and the module marking. |
| MYR / vault Sandbox validation | Phase 3 | Malaysian Sandbox accounts: MYR orders, vault-without-purchase, MIT charging. Fall back to USD if any fails. |
| PayPal order creation timing | Resolved | Create the provider order only after verified gadget approval; create may capture immediately, APPROVED triggers fixed-id capture. |
| AP2 mandate vs gadget signature | Phase 3 | Proposed: the gadget signature is the user authorization of the cart mandate (`cart_sha256` in payload). Confirm against the AP2 sample's validation method. |
| Transfer skill | none | Meaning and required authorization undefined. Not built. |
| Refund policy | Resolved | Refunds/reversals only audit `payment.refund_recorded`; daily capacity is not restored. |
| Pending-capture accounting | Resolved | Pending/uncertain payments retain reserved capacity until completion or definitive failure. |
| Phone approval UX | deferred | Channel (dashboard page, push), timeout, ordering vs gadget approval. Orders > RM100 blocked meanwhile. |
| Webhook public URL | Resolved | Polling reconciliation is primary; verified webhooks are secondary and optional on quick tunnels. |
| Supabase key type | first hosted run | Confirm the hosted project's service-role key works with `supabase` 2.33.0 (legacy JWT `service_role` key is the tested path). Check with `GET /health/ready`. |
| Phase 2 migrations on the hosted project | first hosted run | Two new migrations (`20261025090000`, `20261025090100`) are written and statically checked, but have not been applied to any database and their pgTAP suite has not been run. Follow setup.md A3 (link, migration list, repair if needed, dry-run, push). |
| JWT signing on the hosted project | dashboard | Whether the project signs user JWTs with asymmetric keys (JWKS) or the legacy HS256 secret is unverified; both paths are implemented. |

## 21. Phase Status and Roadmap

### Phase 0 + 1: done

From [../README.md](../../README.md) and `docs/verification.md` (iteration 2, review verdict APPROVED):

- FastAPI app factory, typed `Settings` (`SecretStr` secrets, minor-unit policy, rejection of `*_MYR` names), redacting logging via LogRecord factory, generic error handler, docs disabled in production.
- `GET /health` (liveness) and `GET /health/ready` (Supabase probe, cached 5 s, no error details).
- Cached service-role Supabase client.
- Initial migration: all section-7 tables plus `webhook_events`, RLS on every table, column grants hiding token references, composite owner FKs, approval single-use and nonce uniqueness, append-only `audit_logs`, concurrency-safe `reserve_spending`.
- Tests: pytest suite (no network; Supabase mocked) and a pgTAP suite of 42 assertions. Validated against local Supabase (`db reset`, `test db`, two-session concurrency) before the local stack was removed. Not yet pushed to a hosted project.

### Phase 2: MCP server, devices, merchants, order creation — implemented (backend)

Evidence: `docs/verification.md`. Per-phase review was skipped at the user's request; one review runs after Phase 4.

Done:

- Implemented review hardening:
  1. Logging factory wraps `filter` in try/except; malformed log calls no longer raise. `cpd_`/`cpl_` tokens are redacted, including `/mcp/<token>` in access logs.
  2. `reserve_spending(..., p_timezone)` derives the day in SQL and validates the timezone. The limit stays a parameter (single policy source in env; function is service-role only).
  3. `conversation_events.summary` (allowlisted, user-readable) + column grant hiding `payload`, which is always `{}`.
  4. Default-privilege revokes for tables/sequences/functions in `public`; pgTAP catalog-wide checks (RLS everywhere, no anon table privileges, no anon/authenticated EXECUTE) and row scoping for approvals, mandates, devices, payment_attempts, order_items, conversation_events, a2a_messages.
- Migrations `20261025090000_phase2_hardening.sql` (default privileges, `reserve_spending`, summary column, status-transition trigger) and `20261025090100_phase2_devices_orders_a2a.sql` (device token hashes, global active-key uniqueness, order `cart_sha256`/`quote_reference`, merchant seed `grocer`/`electronics`, `a2a_messages`, Phase 2 RPCs). The initial migration is unchanged (hash checked by a test). `approval_challenges` moved to Phase 3. `conversations.source` already defaulted to `esp_claw`; only the README note was stale.
- Device registry: admin-gated registration (owner JWT behind a flag), `cpd_` + `cpl_` tokens hashed with the pepper, revocation that cancels awaiting orders, no-cache bearer auth with an IP failure limiter that never blocks a valid device.
- Stateless MCP server at `/mcp` with the four tools, protocol/size/origin checks, per-device limits, conversation events in background tasks.
- Mock Grocer and Electronics A2A agents (in-process by default, runnable standalone) and untrusted-response validation of every quote and search result.
- `create_order`: server-side totals, 60 s same-cart dedup, quote expiry cap, advisory cap check, `no_payment_method`, RM100 block; `get_order_status` with lazy expiry; `get_spending_summary`.
- Dashboard order REST (list, detail, cancel) and device list, RLS-scoped via the user's JWT.

Not done / not verified:

- Migrations not applied anywhere and pgTAP not run (no local stack by design; hosted push is the user's step).
- No real device or tunnel test yet; the exit criterion "ESP-Claw calls the tools through a tunnel" is still open (board still being confirmed).
- Phase 3 now fills payment timestamps/status and on-demand reconciliation; live-provider behavior remains unverified.

### Phase 3: approval and payment — implemented (backend)

- Canonical challenge, stored-byte P-256 verification, single-use nonce and expiry; atomic approval/mandate/reservation/attempt transaction.
- PayPal Sandbox adapter with OAuth preflight before sent markers, first/replay classification, stable create/capture ids, amount/currency validation, inline budget and leased reconciliation.
- JWT PayPal linking uses stored setup references and hashed single-use state; masked wallet display, no raw provider payloads.
- Pending-approval polling, expiry sweeps, late-completion accounting and conservative exhaustion.
- Device contract test with software signing, offline failure/race tests, opt-in isolated-credential OAuth/setup-only smoke (skipped by default).
- Firmware `cap_clawpay`, actual board/button/tunnel and Sandbox wallet validation remain separate work. The backend stores `clawpay.gadget_cart.v1` as the verified cart mandate; external AP2 SDK compatibility is not claimed.

### Phase 4: LLM proxy, webhook integration, dashboard — implemented (backend)

- Verified raw-byte PayPal webhooks; privacy-safe persistence, dedup/retry/ordering, refund-only audits and convergence with polling.
- OpenAI-compatible proxy with `cpl_` auth, pinned model, key allowlist, token clamping, usage accounting and both streaming relay modes; no provider headers or credentials exposed.
- JWT/RLS dashboard timeline, A2A summaries, conversations/events, audit events, spending summary, device and payment-method lists. Safe column lists and keyset pagination. Orders-only guarded Realtime migration.
- Mocked end-to-end happy/failure flows and updated README, contracts, setup and verification record.
- New migrations/pgTAP have not been executed against any database in this task; operator application and real hardware/provider validation remain required.

### Stretch

- Push-to-talk voice (`/api/v1/voice/stt`, `/api/v1/voice/tts`, firmware `cap_clawpay_voice`).
- Phone approval for > RM100 (or earlier if the demo needs it).
