# ClawPay Middleware

Implemented backend for phases 0–4: ESP-Claw calls four stateless MCP tools, deterministic A2A merchants supply validated quotes, a device P-256 signature authorizes payment, and the backend handles PayPal Sandbox execution, spending reservations and reconciliation. Includes verified webhooks, an OpenAI-compatible LLM proxy, and user-scoped dashboard APIs. Includes a responsive Vite + React dashboard with a Hono read-only proxy; see [dashboard setup](dashboard/README.md).

- [Backend + ESP-Claw setup](docs/setup.md)
- [Integration contracts](docs/contracts.md)
- [Decisions and phase status](docs/implementation-plan.md#19-decisions-log)
- [Verification evidence](docs/verification.md)
- [Code layout and contribution guide](docs/architecture.md)
- [Remaining work, in order](docs/remaining-work.md)

The code is organized into HTTP routes, business services, pure domain rules, provider integrations and persistence adapters. Repository interfaces and Supabase implementations are split by feature, with matching test fakes. See the architecture guide above for contribution rules and the remaining-work checklist for live validation still required.

## Setup and run

Python 3.12 and uv are required. From `backend/`:

```powershell
uv sync --python 3.12
Copy-Item .env.example .env
# Fill in your server credentials; do not commit .env.
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Use **one worker**: rate limits, LLM token budgets and reconciliation throttles are in memory. Set `APP_ENV=production` before exposing a tunnel to disable API documentation. Configure `PUBLIC_BASE_URL` with its HTTPS origin. `CLIENT_IP_HEADER=cf-connecting-ip` is for cloudflared; `x-forwarded-for-last` is for a trusted ngrok proxy; use `none` without a proxy. Keep readiness checks off public ingress.

Populate Supabase URL, service-role key, publishable key, token pepper (at least 32 characters), admin token (at least 32 characters), and PayPal Sandbox credentials. Enable `MCP_ENABLED=true`. Empty merchant endpoints use deterministic in-process mock merchants. LLM proxy settings are optional; an unconfigured proxy returns 503.

Registration returns two tokens once: `cpd_` for MCP/device REST in private NVS `clawpay`; `cpl_` for Web Console LLM Settings at `<PUBLIC_BASE_URL>/llm/v1`. The upstream LLM key stays on the backend.

## Database migration pre-step

**If you already dropped the ClawPay tables:** run [supabase/schema.sql](supabase/schema.sql) first, then [supabase/seed.sql](supabase/seed.sql) in the Supabase SQL Editor as postgres. See the [rebuild guide](docs/database-rebuild.md); this applies the full current schema and preserves Supabase Auth. Re-register devices and relink PayPal afterward.

**For an existing intact schema:** never edit the initial applied migration. Run the migration workflow in [setup A3](docs/setup.md#a3-apply-migrations-to-the-hosted-supabase-project--works-now): link, inspect migration history, repair the initial history entry only if needed, and review a dry run before pushing. Exactly four new files follow `20261024133807_initial_schema.sql`:

1. `20261025090000_phase2_hardening.sql`
2. `20261025090100_phase2_devices_orders_a2a.sql`
3. `20261025090200_phase3_approvals_payments.sql`
4. `20261025090300_phase4_realtime.sql`

RLS scopes reads to the user; explicit column grants hide tokens, provider references, raw payloads and audit details. Multi-row changes run in service-role-only RPCs with ordered row locks and one deadlock retry. `reserve_spending(..., p_timezone)` derives the spending day in SQL. Only `orders` is published to Realtime. The other dashboard data is polled. Python tests mock all external services; pgTAP files are provided but were not executed during this implementation. No hosted migrations were pushed.

## Endpoints

| Route | Authentication / purpose |
| --- | --- |
| `GET /health`, `GET /health/ready` | See unauthenticated table below |
| `GET /metrics` | `METRICS_TOKEN` bearer; Prometheus text format; 404 when the token is unset |
| `POST /mcp` | `cpd_` bearer; fresh initialize per tool call; 4 tools only |
| `POST /mcp/{token}` | Optional demo fallback, `MCP_PATH_TOKEN_ENABLED=true` |
| `POST /api/v1/devices/register` | Admin token; owner JWT only with explicit registration flag |
| `GET /api/v1/devices` | JWT; safe device columns |
| `DELETE /api/v1/devices/{id}` | Owner JWT or admin token; revokes both tokens |
| `GET /api/v1/devices/{id}/pending-approvals` | `cpd_`; `wait=0..25`, default 0; new poll replaces old |
| `GET /api/v1/orders/{id}/approval-challenge` | `cpd_`; canonical challenge, TTL capped by quote |
| `POST /api/v1/orders/{id}/gadget-approval` | `cpd_` + P-256 signature for approval; ≤16 KB |
| `GET /api/v1/orders`, `GET /api/v1/orders/{id}` | JWT; safe detail with approvals and payment attempts |
| `POST /api/v1/orders/{id}/cancel` | Owner JWT; cannot cancel in-flight payment |
| `GET /api/v1/orders/{id}/timeline`, `/a2a-messages` | JWT; safe summaries |
| `GET /api/v1/conversations`, `/api/v1/conversations/{id}/events` | JWT; safe summaries |
| `GET /api/v1/spending/summary`, `GET /api/v1/audit-events` | JWT; summary matches MCP tool |
| `POST /api/v1/payment-methods/paypal/setup` | JWT; 5/min, returns approve URL |
| `GET /api/v1/payment-methods/paypal/return` | See unauthenticated table below |
| `GET /api/v1/payment-methods`, `DELETE /api/v1/payment-methods/{id}` | JWT; masked email, no vault references |
| `POST /api/v1/webhooks/paypal` | See unauthenticated table below |
| `POST /llm/v1/chat/completions` | `cpl_` only; model pinned, bounded tokens and daily usage |
| `GET /api/v1/orders/{order_id}/receipt` | `cpd_`; saved cart and verified payment status, restricted to the original device; fulfillment explicitly simulated |
| `POST /api/v1/assistant/messages/stream` | `cpd_`; live action summaries and one final result as NDJSON; no private reasoning tokens |
| `POST /api/v1/assistant/messages` | `cpd_`; mounted with MCP; text in, spoken-style reply out (server-side tool loop for watch/voice) |
| `POST /api/v1/assistant/speech` | `cpd_`; mounted with MCP; reply text in, Fish Audio MP3 out; 503 when `FISH_AUDIO_API_KEY` is unset |
| `POST /api/v1/assistant/speech/stream` | `cpd_`; reply text in, low-latency streamed 24 kHz mono PCM out; used by the watch without full-clip buffering |
| `POST /api/v1/assistant/transcriptions` | `cpd_`; raw 16 kHz mono 16-bit WAV, ≤30 s / 1 MiB; text out via Fish Audio ASR; never executes agent tools |

JWT dashboard reads require `SUPABASE_PUBLISHABLE_KEY`; unset → `503 dashboard_not_configured`. List pagination uses `limit=1..100` (default 20), `before=<created_at ISO timestamp>`. There is no charge/approve/capture/refund/transfer MCP tool.

### Endpoints without user/device authentication

| Endpoint | Protection |
| --- | --- |
| `GET /health`, `GET /health/ready` | No secrets; readiness cached 5 seconds; keep off public ingress |
| `POST /api/v1/webhooks/paypal` | PayPal signature verification before persistence, 256 KB cap, 60 failed verifications/min/IP |
| `GET /api/v1/payment-methods/paypal/return` | High-entropy single-use hashed state, 30-minute expiry, 10 invalid states/min/IP, stored setup-token reference, no-store/no-referrer page |
| `/docs`, `/redoc`, `/openapi.json` | Disabled in production |

## Multi-agent mode: PayPal agent

ClawPay can delegate every PayPal call to a separate A2A service, `app/paypal_agent`. ClawPay still owns the decision to pay: the device signature, spending policy and reservation, the per-order `PayPal-Request-Id`, and classification of results. The agent only executes the call it is given with that request id and reports PayPal's answer. It holds the PayPal Sandbox credentials, requires `PAYPAL_AGENT_TOKEN`, is never mounted on the main app and is not reachable by the device LLM.

```text
device LLM --MCP--> ClawPay --A2A--> Grocer / Electronics mock merchant agents
                       |     --A2A--> Channel3 merchant agent --REST--> Channel3 product API
                       |
                       +--A2A (bearer)--> PayPal agent --REST--> PayPal Sandbox
```

Run both (two terminals; the agent needs `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_AGENT_TOKEN` in its environment):

```powershell
uv run uvicorn --factory app.paypal_agent.app:paypal_agent_app --host 127.0.0.1 --port 8201 --no-access-log
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

with ClawPay's env containing `PAYPAL_AGENT_ENDPOINT=http://127.0.0.1:8201/a2a` and the same `PAYPAL_AGENT_TOKEN`. In that mode ClawPay needs no PayPal credentials. Only PayPal's own error responses (status, name, issue, debug_id) cross back as API errors; an unreachable, unauthorised or misbehaving agent is treated like a PayPal network failure (attempt stays unsent or `requires_reconciliation`), so an agent fault can never release a reservation or produce a second charge. The agent is trusted with the credentials: bind it to 127.0.0.1 or a private network. Agent calls appear in `/metrics` as `clawpay_paypal_requests_total{operation="agent:<op>"}` and carry the caller's `X-Request-ID`, so ClawPay and agent logs join on `request_id`.

## Watch / voice assistant

`POST /api/v1/assistant/messages` runs the agent loop on the server for clients that only have a microphone (Apple Watch, Siri Shortcut, Wear OS). The client transcribes speech itself and sends `{"text": "buy two milk", "reset": false}` with the claw's `cpd_` token. The server calls the pinned LLM through the proxy service (same per-device rate limit and daily token budget) and executes the same four MCP tools in-process, so validation and conversation events are unchanged. The response is `{"reply", "order", "approval_required"}`; the client speaks `reply`.

The watch's Talk button records audio and transcribes it with Fish Audio. After a short pause, it sends the transcript directly to the agent; Stop & send also ends the recording. It also supports App Shortcuts: **“Hey Siri, ask ClawPay.”**
Siri captures the request, opens the app, and hands it to the same authenticated
assistant path. DeepSeek handles text reasoning/tool calls; Apple supplies Siri's STT.
Replies aim for 8–16 words and only the latest answer is shown by default.
`order` also carries verified `get_order_status` results, so a status query can
refresh the watch's payment receipt without inferring state from model prose.
See the [watch Siri setup](watch/README.md#siri-and-speech-to-text).

- It cannot pay. A created order belongs to the token's device and waits for that device's signature: with the claw's token, the claw's button hold (its approval task must long-poll `GET /api/v1/devices/{id}/pending-approvals` to see orders that did not come from its own LLM); with a watch registered as its own device (`scripts/register_watch.py`), the watch's Secure Enclave key. Send `"approver": "watch"` so the reply says to approve on the watch.
- History is kept per device in memory for 15 minutes (last 24 messages); `reset: true` clears it. One worker, like the other limits.
- Each message makes up to 5 LLM calls; LLM proxy settings must be configured or it returns `503 proxy_not_configured`.
- `POST /api/v1/assistant/speech` turns reply text (≤500 chars) into MP3 with [Fish Audio](https://docs.fish.audio) (`POST https://api.fish.audio/v1/tts`). Set `FISH_AUDIO_API_KEY`, optionally `FISH_AUDIO_VOICE_ID` (a voice's `reference_id`) and `FISH_AUDIO_MODEL` (default `s2.1-pro`; `s2.1-pro-free` on the free tier). The key stays on the server, text is never logged, and requests are limited per device (`FISH_AUDIO_REQUESTS_PER_MINUTE`). Any Fish Audio failure is `502 tts_unavailable`; the watch then speaks with its built-in voice.
- A watchOS client is in [watch/](watch/README.md).

## Channel3 merchant agent

Set `CHANNEL3_API_KEY` to add a third merchant, `channel3`, backed by the real Channel3 catalog (`app/merchant_agents/channel3.py`). It speaks the same A2A search/quote protocol as the mock merchants, so ClawPay applies the same untrusted-quote checks and recomputes totals. Search maps to `POST /v1/search` (`config.country` = `CHANNEL3_COUNTRY`, `config.currency` = `DEFAULT_CURRENCY`); a quote re-reads every line with `GET /v1/products/{id}` at live prices. Each product is priced from its cheapest in-stock offer in `DEFAULT_CURRENCY`, and its name shows the retailer domain. Skus are `C3-` + base32 of the Channel3 product id.

- `merchant_id` `channel3` appears in the MCP tool schemas only when Channel3 is enabled; otherwise the schemas are exactly as in the contract and `channel3` is rejected as `invalid_arguments`.
- By default the agent runs in-process (no public route). A standalone agent (`uv run uvicorn --factory app.merchant_agents.channel3:channel3_app --host 127.0.0.1 --port 8103`) has no authentication and spends Channel3 credits on every call: bind it to 127.0.0.1 and set `CHANNEL3_AGENT_ENDPOINT`.
- The `channel3` merchant row comes from migration `20261030090000_channel3_merchant.sql` (also in `supabase/seed.sql`).
- Most Channel3 offers are in USD: set `DEFAULT_CURRENCY=USD` (and `CHANNEL3_COUNTRY=US`) or the agent finds no priced offers. The phone threshold and daily cap apply unchanged, in minor units of `DEFAULT_CURRENCY`.
- Payment goes to the ClawPay PayPal Sandbox merchant; the retailer order is not placed (demo marketplace).

## Observability

- **Request ids.** Every response carries `X-Request-ID` (except `/llm/v1`, whose contract allows only two headers). A caller-supplied `X-Request-ID` matching `^[A-Za-z0-9._-]{8,64}$` is reused, so a tunnel or gateway id flows through; anything else is replaced. The id is forwarded to merchant agents on every A2A call.
- **Log context.** Every log line written while handling a request (background tasks included) carries `request_id` and, once known, `device_id`, `user_id` and `order_id`. Reconciler ticks get their own `reconcile-<id>`. Text format shows `[request_id]`; `LOG_FORMAT=json` writes one JSON object per line for Loki, Datadog, CloudWatch or similar. Redaction still applies before formatting; bodies, tokens, signatures and PayPal payloads are never logged.
- **Access log.** One `app.access` line per request with method, route template, status and latency. Run uvicorn with `--no-access-log` to avoid the duplicate uvicorn line (whose raw path is redacted anyway).
- **Metrics.** `GET /metrics` (bearer `METRICS_TOKEN`) exposes `clawpay_http_requests_total` / `_duration_seconds` by route template, `clawpay_auth_failures_total`, `clawpay_audit_events_total`, `clawpay_payment_results_total{source,classification}`, `clawpay_paypal_requests_total` / `_duration_seconds` by operation (ids stripped), `clawpay_merchant_requests_total` / `_duration_seconds`, and reconciler runs/claims. Labels never hold ids, tokens or amounts. Counters are per process and reset on restart (single worker assumed).
- **Business audit trail.** `audit_logs`, `conversation_events` and `a2a_messages` remain the per-order record; read them through `GET /api/v1/orders/{id}/timeline` and `GET /api/v1/audit-events`. Grep JSON logs by `order_id` to join the two.

## Payment behavior

`create_order` validates merchant prices and stores an awaiting-approval DB order; it makes no PayPal call. Orders above 10000 minor units are blocked because phone approval is deferred. Approval reserves daily capacity and creates the mandate and payment attempt atomically. PayPal orders are created only after signature verification. Capture completion commits spending once; definitive failure releases it. Pending/uncertain outcomes hold the reservation. Polling is primary; webhooks are optional on quick tunnels. Late completion changes `payment_failed` to `paid` using the original approval day's accounting. Refunds and reversals are audited and **do not restore capacity**.

Only open PayPal approve links you generated and check the masked account after linking. Never tell the user no money was taken without calling `get_order_status` again.

## Test

```powershell
uv run --offline pytest
uv run python -c "from app.core.config import Settings; from app.main import create_app; create_app(Settings(_env_file=None))"
```

Tests use in-memory repositories, real software P-256 signatures, MockTransport and in-process ASGI merchants. All default tests run without network or a database. The opt-in integration smoke is skipped unless `CLAWPAY_LIVE_SMOKE=1` and both `CLAWPAY_SMOKE_PAYPAL_CLIENT_ID` / `CLAWPAY_SMOKE_PAYPAL_CLIENT_SECRET` are set. It performs only OAuth + setup-token creation; see setup.md. Starlette emits one existing httpx deprecation warning; dependency pins remain unchanged.

## Environment variables

See `.env.example` for defaults and explanations. Money uses integer minor units: RM100 = 10000 sen, RM500 = 50000. `DEFAULT_CURRENCY` accepts MYR/USD. `SUPABASE_PUBLISHABLE_KEY` wins over legacy `SUPABASE_ANON_KEY`; HS256 requires `SUPABASE_JWT_SECRET`, otherwise JWKS verifies user JWTs. Try the legacy JWT service-role key if the newer `sb_secret_` key is not accepted by your hosted gateway.

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` |  |
| `APP_HOST` | `0.0.0.0` |  |
| `APP_PORT` | `8000` |  |
| `LOG_LEVEL` | `INFO` |  |
| `LOG_FORMAT` | `text` | `text` or `json` (one object per line with `request_id`, `device_id`, `user_id`, `order_id`). |
| `METRICS_TOKEN` | `` | Bearer token for `GET /metrics`. Empty = disabled (404). At least 32 characters. |
| `SUPABASE_URL` | `` |  |
| `SUPABASE_PUBLISHABLE_KEY` | `` | Publishable key (sb_publishable_...) for RLS-scoped dashboard reads. SUPABASE_ANON_KEY is accepted as a legacy alias; PUBLISHABLE wins if both are set. |
| `SUPABASE_SERVICE_ROLE_KEY` | `` |  |
| `SUPABASE_JWT_SECRET` | `` | Only for legacy HS256 projects. Leave empty to verify user JWTs via the project JWKS. |
| `PAYPAL_ENV` | `sandbox` | Sandbox only: PAYPAL_ENV=live is rejected at startup. |
| `PAYPAL_CLIENT_ID` | `` |  |
| `PAYPAL_CLIENT_SECRET` | `` |  |
| `PAYPAL_WEBHOOK_ID` | `` |  |
| `PAYPAL_HTTP_TIMEOUT_SECONDS` | `8` |  |
| `PAYPAL_BRAND_NAME` | `ClawPay` |  |
| `PAYPAL_INLINE_BUDGET_SECONDS` | `10` |  |
| `RECONCILE_INTERVAL_SECONDS` | `30` |  |
| `RECONCILE_REPLAY_WINDOW_SECONDS` | `900` |  |
| `GADGET_APPROVAL_PUBLIC_KEY_REGISTRY` | `` |  |
| `AGENTCORE_REGION` | `` |  |
| `PAYPAL_AGENT_ENDPOINT` | `` | A2A URL of the PayPal agent (e.g. `http://127.0.0.1:8201/a2a`). Empty = ClawPay calls PayPal directly. |
| `PAYPAL_AGENT_TOKEN` | `` | Shared bearer between ClawPay and the PayPal agent, ≥ 32 chars. Required when the endpoint is set and by the agent itself. |
| `GROCER_AGENT_ENDPOINT` | `` | standalone: uv run uvicorn --factory app.merchant_agents.app:grocer_app --host 127.0.0.1 --port 8101 then GROCER_AGENT_ENDPOINT=http://127.0.0.1:8101/a2a. Only https:// or http://127.0.0.1 / http://localhost are accepted. |
| `ELECTRONICS_AGENT_ENDPOINT` | `` |  |
| `CHANNEL3_API_KEY` | `` | Enables merchant `channel3` (real catalog via Channel3; in-process agent). Each search/quote spends Channel3 credits. |
| `CHANNEL3_AGENT_ENDPOINT` | `` | Optional standalone Channel3 agent URL instead of in-process. |
| `CHANNEL3_BASE_URL` | `https://api.trychannel3.com` | Channel3 API base. |
| `CHANNEL3_COUNTRY` | `US` | ISO country for offers. Prices are taken in `DEFAULT_CURRENCY` (usually set `USD`). |
| `CHANNEL3_TIMEOUT_SECONDS` | `4.5` | Per Channel3 HTTP call (search is capped at 5 s overall). |
| `MERCHANT_AGENT_TIMEOUT_SECONDS` | `8` |  |
| `INTERNAL_SERVICE_TOKEN` | `` | Admin token: device registration and admin revoke. At least 32 characters. Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))" |
| `PUBLIC_BASE_URL` | `` | ESP-Claw device integration. PUBLIC_BASE_URL: public HTTPS origin with a CA-signed cert (tunnel or host), e.g. https://xxxx.trycloudflare.com. |
| `MCP_ENABLED` | `false` |  |
| `MCP_PATH_TOKEN_ENABLED` | `false` | Demo-only fallback POST /mcp/<device_token>; leaks the token into the device LLM context and logs. Keep false once the bearer-token firmware patch is flashed. |
| `MCP_ALLOWED_ORIGINS` | `` | Comma-separated; any request with an Origin not listed gets 403. The device sends no Origin. |
| `MCP_TOOL_CALLS_PER_MINUTE` | `30` |  |
| `MCP_REQUESTS_PER_MINUTE` | `120` |  |
| `DEVICE_REQUESTS_PER_MINUTE` | `30` |  |
| `AUTH_FAILURES_PER_MINUTE` | `20` |  |
| `CLIENT_IP_HEADER` | `none` | How to find the client IP for IP rate limits: none (socket peer), cf-connecting-ip (cloudflared), x-forwarded-for-last (ngrok). |
| `DEVICE_OWNER_REGISTRATION_ENABLED` | `false` | false = only INTERNAL_SERVICE_TOKEN can register devices. |
| `DEVICE_TOKEN_PEPPER` | `` | HMAC pepper for device/LLM tokens at rest. Required (>= 32 chars) when MCP_ENABLED=true. Generate like INTERNAL_SERVICE_TOKEN. Changing it invalidates every issued token. |
| `QUOTE_TTL_SECONDS` | `300` | Orders |
| `CHALLENGE_TTL_SECONDS` | `120` |  |
| `ORDER_REQUIRE_PAYMENT_METHOD` | `true` |  |
| `LLM_PROXY_UPSTREAM_BASE_URL` | `` | OpenAI-compatible LLM proxy for the device (Phase 4). The upstream key stays here, never on the device. Base URL without /chat/completions, e.g. https://api.openai.com/v1 |
| `LLM_PROXY_UPSTREAM_API_KEY` | `` |  |
| `LLM_PROXY_MODEL` | `` |  |
| `LLM_PROXY_MAX_TOKENS` | `1024` |  |
| `LLM_PROXY_REQUESTS_PER_MINUTE` | `20` |  |
| `LLM_PROXY_DAILY_TOKEN_LIMIT` | `200000` |  |
| `LLM_PROXY_MAX_BODY_BYTES` | `262144` |  |
| `LLM_PROXY_TIMEOUT_SECONDS` | `110` |  |
| `PHONE_APPROVAL_THRESHOLD_MINOR` | `10000` | Money is in MINOR units (sen for MYR): RM100 = 10000, RM500 = 50000. |
| `DAILY_SPENDING_LIMIT_MINOR` | `50000` |  |
| `DEFAULT_CURRENCY` | `MYR` | MYR or USD only. |
| `SPENDING_DAY_TIMEZONE` | `Asia/Kuala_Lumpur` |  |

## Known limitations

- Sandbox only. MYR between Malaysian accounts, vault-without-purchase, merchant-initiated charges, payer email presence, Request-Id retention, duplicate-invoice enforcement and REVERSED resource shapes remain unverified live. Do not raise the replay window above 900 seconds until retention and invoice blocking are confirmed.
- Rate limits, LLM budgets and reconciliation throttle reset on process restart and are per worker. Budget checks account for completed requests; concurrent requests can briefly exceed a budget.
- Exhausted payment attempts keep reservations until a verified webhook or manual provider-side resolution; no manual-resolution API exists.
- Firmware bearer patch and private `cap_clawpay` signing task, actual hardware/tunnel tests remain separate work. No external AP2 SDK is claimed: the stored gadget-cart mandate binds the verified cart signature.
- Phone approval, voice, AgentCore, AG Grid, Bryntum and Render are out of scope. Hosting is plain uvicorn.
- Channel3 orders are charged to the ClawPay PayPal Sandbox merchant; ClawPay does not place the order with the offer's retailer (no fulfilment). Live Channel3 behaviour (MYR offers, response shape) is unverified; tests mock the API.
- New SQL and pgTAP were reviewed/static-tested, not run against a database in this task.

## Agent activity dashboard

Run `cd dashboard`, `npm ci`, `npm run build`, then `npm start`; open http://127.0.0.1:8787. Demo mode is immediately available. Owner sign-in connects RLS-scoped live activity from the Python backend. See [dashboard/README.md](dashboard/README.md) for environment settings, development and verification.

The watch now shows structured product choices, an itemized Sandbox receipt and explicitly simulated delivery. Choose a fictional destination before approval, then use Simulate next step after paid status. Channel3 public checkout is coming soon; no retailer order is placed. See [watch receipt/demo setup](watch/README.md#product-selection-receipts-and-delivery-demo).
