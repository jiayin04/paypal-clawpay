# ClawPay Integration Contracts

Status: **Implemented — backend phases 0–4**, tested with in-memory repositories, in-process merchants and mocked PayPal/LLM HTTP. Hosted migration execution, live PayPal behavior and real firmware/hardware integration remain unverified. Voice endpoints in section 9 remain deferred. Current module boundaries and contribution rules are documented in [architecture.md](architecture.md).

Device-side behavior referenced here (ESP-Claw `cap_mcp_client`, Lua limits, signing in C) comes from [esp-claw-investigation.md](esp-claw-investigation.md). Setup steps are in [setup.md](setup.md).

## 1. Conventions

- Base URL: `PUBLIC_BASE_URL`, public HTTPS with a CA-signed certificate (the device uses the ESP-IDF CA bundle; self-signed certs fail).
- JSON is UTF-8. Timestamps in REST bodies are ISO 8601 UTC (`2026-10-24T13:05:00Z`). The approval payload uses Unix seconds.
- Money is integer minor units internally (`*_minor`, sen for MYR). Tool results add a display string (`"RM13.80"`) so the LLM never does arithmetic.
- Default currency is `DEFAULT_CURRENCY` (MYR; USD fallback). An order in any other currency is rejected.
- REST errors use the existing backend shape: `{"error": {"code": "<snake_case>", "message": "<safe text>"}}`. Messages never include secrets, stack traces or provider payloads.
- Caller identity always comes from the token. `device_id` / `user_id` in MCP tool arguments are never read.

## 2. Authentication

Status: Implemented in Phase 2.

| Caller | Credential | Where |
| --- | --- | --- |
| Device (MCP, device REST, voice) | Device token (`cpd_`) | `Authorization: Bearer <device_token>` |
| Device, before the firmware patch | Device token in path | `POST /mcp/<device_token>`, only when `MCP_PATH_TOKEN_ENABLED=true` (demo fallback) |
| Device LLM client (LLM proxy, Phase 4) | LLM token (`cpl_`) | `Authorization: Bearer <llm_token>` |
| Admin (device registration, revoke) | `INTERNAL_SERVICE_TOKEN` (≥ 32 chars) | `Authorization: Bearer <token>` |
| User (dashboard, phone approval) | Supabase Auth JWT | `Authorization: Bearer <jwt>` |

Device token and LLM token:

- Format `cpd_` / `cpl_` + 32 random bytes base64url (no padding), 47 chars. Both are issued once at registration; only `HMAC-SHA256(DEVICE_TOKEN_PEPPER, token)` is stored (`devices.token_hash`, `devices.llm_token_hash`).
- `cpd_` is accepted only by `/mcp` and device REST; `cpl_` only by `/llm/v1`. The wrong kind → `401`.
- Lookup on every request, no cache → device → user. Unknown, revoked or `pending` device → `401 unauthorized` with `WWW-Authenticate: Bearer`. Revoking the device kills both tokens on the next request.
- Auth-failure limiter per client IP (`AUTH_FAILURES_PER_MINUTE`, default 20): malformed tokens and lookup misses count; over the limit → `429` with `Retry-After`. A well-formed token of an active device is never blocked, even behind a tunnel where every request shares `127.0.0.1`. `CLIENT_IP_HEADER` selects the IP source (`none`, `cf-connecting-ip`, `x-forwarded-for-last`).
- `DEVICE_TOKEN_PEPPER` missing → device routes return `503 device_auth_not_configured`.
- Logs redact both token kinds, including the path-token form in access logs.
- The path-token fallback leaks the token into the device LLM context and its logs. Keep it off; if used, revoke and re-register the device once the bearer patch is flashed.

User JWT: HS256 only when `SUPABASE_JWT_SECRET` is set, otherwise ES256/RS256 via the project JWKS. Requires `aud=authenticated`, `iss=<SUPABASE_URL>/auth/v1`, `role=authenticated`, `exp`, UUID `sub`. Failures → `401 unauthorized`; JWKS unreachable → `503 auth_unavailable`. Dashboard read routes return `503 dashboard_not_configured` when `SUPABASE_PUBLISHABLE_KEY` is unset (checked before the JWT).

## 3. MCP endpoint

Status: Implemented in Phase 2. Mounted only when `MCP_ENABLED=true`.

`POST /mcp` — MCP Streamable HTTP, stateless. `POST /mcp/<device_token>` exists only when `MCP_PATH_TOKEN_ENABLED=true` (otherwise `404`).

Request handling, in this order:

1. `Origin` header present and not in `MCP_ALLOWED_ORIGINS` → `403 forbidden_origin` (DNS-rebinding guard). The device sends no `Origin`.
2. `Content-Length` > 64 KB → `413` (before auth).
3. Device auth (section 2) → `401` / `429` / `503`.
4. Per-device `MCP_REQUESTS_PER_MINUTE` (default 120; each tool call costs 3 HTTP requests) → `429` with `Retry-After`.
5. `MCP-Protocol-Version` header: accept `2025-11-25`, `2025-06-18`, `2025-03-26`; missing header is treated as `2025-11-25`; anything else → `400` with JSON-RPC `-32600` "Unsupported MCP-Protocol-Version", `id: null`.
6. Body read with a 64 KB streamed cap → `413`.
7. Invalid JSON → `400` with JSON-RPC `-32700`, `id: null`.
8. Body must be a single JSON-RPC 2.0 object (batches/arrays, wrong `jsonrpc`, missing `method`, `id` that is null/bool/float → HTTP 200 with `-32600`).

- `cap_mcp_client` sends `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version`. Responses are always `Content-Type: application/json` (one JSON-RPC response). SSE is not used. `Accept` is not enforced.
- Stateless: each tool call arrives as a fresh `initialize` → `notifications/initialized` → `tools/call` sequence with no prior state. `Mcp-Session-Id` is never sent; if a client sends one, it is ignored.
- `GET` and `DELETE` on `/mcp` and `/mcp/<token>` → `405` with `Allow: POST`, empty body (no auth check).
- Device timeout is 20 s per call including TLS and `initialize`. Merchant A2A calls time out at `MERCHANT_AGENT_TIMEOUT_SECONDS` (default 8 s; search uses min(5, value)).

Phase 2 changes: `ping` is supported; any `notifications/*` → `202`; a non-notification request without an `id` → HTTP 200, `-32600` "id required", `id: null`, nothing executed; the tool-call rate limit (`MCP_TOOL_CALLS_PER_MINUTE`, default 30 per device) returns an `isError` result instead of `429`; argument validation failures are `isError` `invalid_arguments` results (see Result rules).

Methods:

`initialize`

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"esp-claw","version":"x"}}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"clawpay","version":"0.1.0"}}}
```

Echo the requested `protocolVersion` if supported, otherwise return `2025-11-25`.

`notifications/initialized` (any `notifications/*`, with or without `id`) → `202 Accepted`, empty body.

`ping` → `{"jsonrpc":"2.0","id":<id>,"result":{}}`.

`tools/list` → `{"tools":[{"name","description","inputSchema"}...]}` for the four tools in section 4. No pagination (`nextCursor` omitted).

`tools/call`

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_order_status","arguments":{"order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11"}}}
```

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"order_id\":\"7f3c...\",\"status\":\"paid\"}"}],"isError":false}}
```

Result rules:

- Only `content[]` items of `type: "text"` reach the LLM; `structuredContent` is ignored by the device, so it is not sent. Exactly one text item per result.
- `text` is compact JSON (no whitespace). The whole serialized JSON-RPC response is at most 3900 bytes (device buffer 4096). `search_products` drops trailing results until it fits and adds `"truncated":true`.
- Tool failures: `isError: true` with text `{"error":"<code>","message":"<what to tell the user>"}`. Unexpected server errors → `internal_error` "Something went wrong. Try again later." (never an HTTP 500).
- Argument validation failures (schema, types, extra keys, prices/totals in arguments) → `isError: true`, `{"error":"invalid_arguments","message":"<field>: <reason>"}` so the model can self-correct. `device_id` / `user_id` keys in arguments are removed before validation and never read.
- Protocol errors use JSON-RPC errors: `-32700` parse error, `-32600` invalid request, `-32601` unknown method, `-32602` unknown tool name or non-object `arguments` (missing `arguments` = `{}`).

## 4. MCP tools

Status: Implemented in Phase 2. `tools/list` publishes exactly these four tools with these descriptions and schemas (a contract test compares them with this file).

Descriptions are short and tell the LLM what to do next. No tool approves, captures, refunds or charges.

Error codes and the message the LLM relays: `invalid_arguments` (field-specific), `rate_limited` "Too many requests. Wait a minute and try again.", `merchant_unavailable` "The shops are not responding. Try again shortly.", `unknown_sku`, `quote_invalid`, `currency_not_supported`, `no_payment_method` "Link a PayPal account in the ClawPay setup page first.", `daily_limit_exceeded` (includes the remaining amount), `phone_approval_unavailable` "Orders above RM100 need phone approval, which is not available yet.", `not_found` "Order not found.", `internal_error` "Something went wrong. Try again later.".

### `search_products`

Description: "Search merchant catalogs. Show the user names and prices. Use sku and merchant_id with create_order."

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 100},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
    "merchant_id": {"type": "string", "enum": ["grocer", "electronics"]}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

```json
{"results":[{"sku":"GRC-MILK-1L","name":"Fresh milk 1L","merchant_id":"grocer","price_minor":690,"price":"RM6.90","currency":"MYR"},{"sku":"GRC-BREAD-WW","name":"Wholemeal bread","merchant_id":"grocer","price_minor":450,"price":"RM4.50","currency":"MYR"}]}
```

Both `merchant_id` enums (search and `create_order`) list the enabled merchants (section 10.1). With the default settings they are exactly as shown; `channel3` is added when `CHANNEL3_API_KEY` or its endpoint is set, and `shopee`, `foodpanda`, `grab` when listed in `ENABLED_MERCHANTS`. A merchant that is not enabled → `invalid_arguments`. All enabled merchants are queried in parallel unless `merchant_id` is given; if one fails, the other's results are returned. Results are sorted by (merchant_id, name). No matches → `{"results":[],"message":"No matching products."}` (not an error). Products in a currency other than `DEFAULT_CURRENCY` are dropped.

Errors: `merchant_unavailable` (all queried merchants failed), `invalid_arguments`.

### `create_order`

Description: "Create an order from one merchant. This does not pay. Then call clawpay_request_approval with the order_id and tell the user to check the device screen and hold the button."

```json
{
  "type": "object",
  "properties": {
    "merchant_id": {"type": "string", "enum": ["grocer", "electronics"]},
    "items": {
      "type": "array", "minItems": 1, "maxItems": 10,
      "items": {
        "type": "object",
        "properties": {
          "sku": {"type": "string", "maxLength": 64},
          "qty": {"type": "integer", "minimum": 1, "maximum": 20}
        },
        "required": ["sku", "qty"],
        "additionalProperties": false
      }
    },
    "currency": {"type": "string", "pattern": "^[A-Z]{3}$"}
  },
  "required": ["merchant_id", "items"],
  "additionalProperties": false
}
```

```json
{"order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","merchant":"Grocer Demo","items":[{"name":"Fresh milk 1L","qty":2,"line_total":"RM13.80"}],"total_minor":1380,"total":"RM13.80","currency":"MYR","expires_at":"2026-10-24T13:10:00Z","status":"awaiting_device_approval","next_step":"call clawpay_request_approval"}
```

Behavior:

- Prices come from the merchant agent quote, revalidated and re-totalled server-side; a quote whose stated line totals, subtotal or total differ from the recomputed values is rejected (`quote_invalid`), never "fixed". LLM-supplied prices do not exist in the schema (`invalid_arguments`).
- Duplicate skus are merged by summing `qty`; a merged `qty` > 20 → `invalid_arguments`.
- Ends in DB state `AWAITING_APPROVAL` via `CREATED → QUOTED → AWAITING_APPROVAL` in one database transaction. No PayPal order is created here (see [implementation-plan.md](implementation-plan.md#11-paypal-integration) on vaulted orders).
- `expires_at` = `min(merchant quote expiry, now + QUOTE_TTL_SECONDS)` (default 300 s).
- The order is bound to the token's device and user.
- Each call creates a new order, **except** an identical cart (same merchant, skus, quantities and prices) from the same device within 60 s while the first order is still awaiting approval and unexpired: that returns the first order (device retries after a timeout). An unapproved order expires with its quote and never charges.
- `no_payment_method` is checked only when `ORDER_REQUIRE_PAYMENT_METHOD=true` (default). Approval (Phase 3) always requires a linked method.

Errors: `invalid_arguments`, `unknown_sku`, `merchant_unavailable`, `quote_invalid`, `currency_not_supported`, `no_payment_method`, `daily_limit_exceeded` (advisory pre-check; the reservation at approval is authoritative), `phone_approval_unavailable` (total > `PHONE_APPROVAL_THRESHOLD_MINOR`, i.e. 10001+ sen, while phone approval is not implemented; exactly 10000 is allowed), `internal_error`.

### `get_order_status`

Description: "Get the authoritative status of an order. Only tell the user a payment succeeded if status is paid."

```json
{
  "type": "object",
  "properties": {"order_id": {"type": "string", "format": "uuid"}},
  "required": ["order_id"],
  "additionalProperties": false
}
```

```json
{"order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","status":"paid","paypal_status":"COMPLETED","total":"RM13.80","currency":"MYR","approved_at":"2026-10-24T13:06:12Z","paid_at":"2026-10-24T13:06:15Z","message":"Payment completed."}
```

The result also has `fulfillment_status` (the merchant handoff, section 10.3: `none`, `reserved`, `reservation_failed`, `confirmed`, `fulfillment_pending`, `released`) and `merchant_reference` (the merchant's `fulfillment_id` once confirmed, else `null`). Reading the status retries an outstanding merchant confirm or release.

`paypal_status` is `null` until a payment attempt exists (always `null` in Phase 2, as are `approved_at` / `paid_at`). Orders of other users and unknown ids → `not_found` with the same text (no existence leak). `order_id` must be a UUID (either case; normalised to lowercase), else `invalid_arguments`. An order still awaiting approval whose quote has expired is moved to `expired` before it is reported.

`status` is one of the API names in section 8, including `requires_reconciliation`. `message` texts are fixed: paid "Payment completed."; payment_pending / requires_reconciliation "Payment is still processing. Check again shortly."; payment_failed "Payment failed. No money was taken. If PayPal later confirms the payment, this status changes to paid."; awaiting_device_approval "Waiting for approval on the device."; denied "The order was declined on the device."; expired "The order expired before it was approved."; cancelled "The order was cancelled.".

### `get_spending_summary`

Description: "Get today's spending against the daily limit."

```json
{
  "type": "object",
  "properties": {"period": {"type": "string", "enum": ["today"], "default": "today"}},
  "additionalProperties": false
}
```

```json
{"period":"today","day":"2026-10-24","timezone":"Asia/Kuala_Lumpur","currency":"MYR","limit_minor":50000,"committed_minor":1380,"reserved_minor":0,"remaining_minor":48620,"remaining":"RM486.20"}
```

The day is computed in the database in `SPENDING_DAY_TIMEZONE`. `remaining_minor = max(limit - committed - reserved, 0)`.

## 5. Canonical approval payload

Status: Implemented — Phase 3.

The server builds the payload, stores the exact bytes with the challenge, and sends them to the device. The device signs those bytes, and parses the same bytes to render the screen. It never rebuilds or reformats them.

```json
{"amount_minor":1380,"cart_sha256":"3b7e...e1","currency":"MYR","device_id":"0b9d6a52-1d0e-4b8e-8f43-2c1f7d0e9a10","expires_at":1792850700,"key_version":1,"merchant_id":"grocer","merchant_name":"Grocer Demo","nonce":"q4Jx...Z8","order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","purpose":"clawpay.order_approval.v1"}
```

Format rules:

- Keys in this fixed (lexicographic) order, no whitespace, ASCII only. Server: `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")`.
- `amount_minor`: integer, no leading zeros, equals `orders.total_minor`.
- `currency`: ISO 4217, equals `orders.currency`.
- `cart_sha256`: lowercase hex SHA-256 of the canonical JSON array of `{"qty","sku","unit_price_minor"}` objects sorted by `sku` (same serialization rules). Binds the cart for the AP2 mandate.
- `device_id`, `order_id`: lowercase UUIDs.
- `expires_at`: Unix seconds, `min(quote_expires_at, now + CHALLENGE_TTL_SECONDS)` (default 120 seconds, range 30–300).
- `merchant_name`: printable ASCII, ≤ 40 chars (server-sanitized so no JSON escapes occur).
- `nonce`: 32 random bytes, base64url without padding; unique (`approvals.nonce` / challenge nonce).
- `purpose`: constant domain separator; bump the version suffix for any format change.

Why `amount_minor` integer instead of a decimal string like `"12.50"`: it is the same representation as `orders.total_minor` and `reserve_spending`, so the server check is integer equality with no parsing; it avoids `"12.5"` vs `"12.50"` vs locale ambiguity in a signed artifact; and C firmware formats it with integer division. The device renders `amount_minor / 10^exponent` using a built-in exponent table (MYR 2, USD 2) and refuses to sign any currency not in that table. Display: `MYR 13.80`.

## 6. Device REST endpoints

### 6.1 `POST /api/v1/devices/register`

Status: Implemented in Phase 2. Auth: `INTERNAL_SERVICE_TOKEN` (admin, one time per device). Only when `DEVICE_OWNER_REGISTRATION_ENABLED=true` (default `false`) may a signed-in owner register a device for themselves with a Supabase JWT.

```json
{"user_id":"5c1e...","device_name":"desk-claw","public_key_pem":"-----BEGIN PUBLIC KEY-----\nMFkw...\n-----END PUBLIC KEY-----\n","key_version":1}
```

Rules: `user_id` UUID (required for admin; optional for an owner JWT and must equal its `sub`); `device_name` 1–64 printable chars; `public_key_pem` ≤ 1024 chars, P-256 SPKI PEM (stored normalised); `key_version` 1–1000, default 1. Body ≤ 16 KB.

`201` (`Cache-Control: no-store`):

```json
{"device_id":"0b9d6a52-1d0e-4b8e-8f43-2c1f7d0e9a10","device_token":"cpd_Vb3...","llm_token":"cpl_Q9x...","key_algorithm":"ecdsa-p256-sha256","key_version":1,"status":"active"}
```

`device_token` and `llm_token` are returned only here and never logged or stored raw. The firmware stores `device_id` and `device_token` in NVS namespace `clawpay` (not `app`, which the Web Console exposes). `llm_token` goes into the Web Console LLM Settings API key (Phase 4 LLM proxy); its exposure there is accepted because it can only spend rate-limited LLM budget.

| Status | Code |
| --- | --- |
| 400 | `invalid_public_key` (not SPKI PEM P-256) |
| 401 | `unauthorized` |
| 403 | `forbidden` (JWT caller while `DEVICE_OWNER_REGISTRATION_ENABLED=false`, checked before the body; or body `user_id` ≠ JWT `sub`) |
| 404 | `user_not_found` |
| 409 | `public_key_already_registered` (also covers two concurrent registrations of the same key) |
| 413 | `payload_too_large` |
| 422 | `validation_error` |
| 429 | `rate_limited` (10 failed auth attempts/min per client IP; a valid caller is never blocked) |
| 503 | `device_auth_not_configured` (no `DEVICE_TOKEN_PEPPER`), `service_unavailable` |

Key or token rotation: revoke (`DELETE /api/v1/devices/{device_id}`) and register again; re-registering the same public key after revocation is allowed.

### 6.2 `GET /api/v1/orders/{order_id}/approval-challenge`

Status: Implemented — Phase 3. Auth: device token. Called by the `cap_clawpay` C task after the LLM calls `clawpay_request_approval {order_id}`.

`200`:

```json
{"challenge_id":"c2a1...","order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","canonical_b64url":"eyJhbW91bnRfbWlub3IiOjEzODAs...","canonical":"{\"amount_minor\":1380,...}","nonce":"q4Jx...Z8","key_version":1,"expires_at":"2026-10-24T13:11:40Z"}
```

- `canonical_b64url` is authoritative (exact bytes). `canonical` is the same bytes as a string for device parsing; never log or expose it to the LLM.
- The order must belong to the token's device and user and be in `AWAITING_APPROVAL` with an unexpired quote.
- Repeated calls return the same unexpired, unconsumed challenge (retries do not mint new nonces).

| Status | Code |
| --- | --- |
| 401 | `unauthorized` |
| 403 | `device_revoked` (revocation race) |
| 404 | `order_not_found` (also non-UUID ids) |
| 409 | `invalid_order_status` |
| 410 | `quote_expired` |

### 6.3 `POST /api/v1/orders/{order_id}/gadget-approval`

Status: Implemented — Phase 3. Auth: device token.

```json
{"challenge_id":"c2a1...","device_id":"0b9d6a52-1d0e-4b8e-8f43-2c1f7d0e9a10","key_version":1,"nonce":"q4Jx...Z8","decision":"approved","signature":"MEUCIQD...base64url-DER"}
```

Signature: ECDSA P-256 with SHA-256 over the exact canonical bytes (firmware: SHA-256 the bytes, `mbedtls_pk_sign(..., MBEDTLS_MD_SHA256, ...)`, which outputs ASN.1 DER), base64url without padding. Server: `public_key.verify(der_sig, canonical_bytes, ec.ECDSA(hashes.SHA256()))`.

`decision: "denied"` needs no signature (it can only cancel the order, and requires the device token). `"approved"` requires a valid signature.

Server checks, in one transaction: device matches token and is active; `key_version` matches; challenge belongs to the order and device, is unexpired and unconsumed; nonce matches; signature verifies over the stored bytes; order still `AWAITING_APPROVAL` and its amount/currency equal the payload. Then it records the approval (`signed_payload_hash` = SHA-256 of the bytes), consumes the challenge, and runs the payment pipeline (spending reservation, phone-approval rule, mandate, PayPal).

`200`:

```json
{"order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","approval":"verified","status":"payment_pending","message":"Approved. Payment is processing."}
```

`status` is `paid`, `payment_pending`, `payment_failed` or `denied`. Before PayPal is called, the merchant agent must reserve the cart (section 10.3); if it refuses, the answer is `payment_failed` with a fixed message saying no money was taken. The pipeline then waits up to ~10 s for PayPal, then returns `payment_pending`; the LLM learns the result via `get_order_status`. `fulfillment_status` (optional) is the merchant handoff once the payment result is known.

| Status | Code |
| --- | --- |
| 400 | `malformed_signature` |
| 401 | `unauthorized` |
| 403 | `device_mismatch`, `device_revoked` |
| 404 | `challenge_not_found` |
| 409 | `key_version_mismatch`, `nonce_mismatch`, `challenge_consumed`, `invalid_order_status`, `phone_approval_required`, `daily_limit_exceeded`, `no_payment_method`, `currency_not_supported` |
| 410 | `challenge_expired`, `quote_expired` |
| 422 | `signature_invalid`, `validation_error` |

Signature failures, replays and limit rejections are written to `audit_logs`.

The phone threshold is unconditional: `total_minor > PHONE_APPROVAL_THRESHOLD_MINOR` returns `409 phone_approval_required`, even if an old phone-approval row exists. Policy failures leave the challenge open until expiry. `payment_pending` also covers `requires_reconciliation`. A failed payment may later become paid if PayPal confirms completion; call `get_order_status` again before telling a user no money moved.

### 6.4 `GET /api/v1/devices/{device_id}/pending-approvals` (optional)

Status: Implemented — Phase 3, optional. Auth: device token; `device_id` must match the token (`403 device_mismatch`).

Long-poll: `?wait=0..25` (seconds, default 0). One poll per device; a new poll replaces the old one, which immediately returns `{"orders":[]}`. Each poll counts once against the device rate limit. Disconnected clients release their slot. Returns as soon as an order awaits approval, otherwise an empty list at timeout.

```json
{"orders":[{"order_id":"7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11","created_at":"2026-10-24T13:05:00Z"}]}
```

### 6.5 `DELETE /api/v1/devices/{device_id}`

Status: Implemented in Phase 2. Auth: Supabase JWT (owner) or `INTERNAL_SERVICE_TOKEN`. Sets `status = revoked`; every later request with the device token or LLM token fails with `401`. The device's orders in `created` / `quoted` / `awaiting_device_approval` are cancelled in the same transaction. Open approval challenges for the device are invalidated once challenges exist (Phase 3).

`200 {"device_id":"...","status":"revoked"}` (idempotent). Unknown device, another user's device or a non-UUID id → `404 not_found`.

`GET /api/v1/devices` (Supabase JWT, RLS-scoped) → `{"devices":[{"id","device_name","key_algorithm","key_version","status","created_at","revoked_at","last_seen_at"}]}`. No token hashes and no public key in the response.

### 6.6 Dashboard order endpoints

Status: Implemented in Phase 2. Auth: Supabase JWT. Reads use the caller's JWT with the publishable key, so RLS and column grants apply; `503 dashboard_not_configured` when `SUPABASE_PUBLISHABLE_KEY` is unset.

- `GET /api/v1/orders?limit=20&before=<iso>` → `{"orders":[{"id","merchant_id","currency","total_minor","status","quote_expires_at","created_at","updated_at"}]}`, newest first, `limit` 1–100.
- `GET /api/v1/orders/{order_id}` → the order plus `items` (`product_reference, description, quantity, unit_price_minor, line_total_minor`), `approvals`, `payment_attempts` (no provider ids), `merchant` (display name) and `mandate_status`. Foreign or unknown order, or a non-UUID id → `404 order_not_found`.
- `POST /api/v1/orders/{order_id}/cancel` → `200 {"order_id","status":"cancelled"}` from `created|quoted|awaiting_device_approval|approved` (releases any spending reservation); `404 order_not_found`; `409 invalid_order_status` once payment has started or the order is final. Uses the service client with the JWT `sub`, so it works without the publishable key.

`status` uses the API names in section 8.

## 7. LLM proxy (optional)

Status: Implemented — Phase 4. `POST /llm/v1/chat/completions`, `cpl_` LLM bearer only. `cpd_` → 401; revocation blocks both tokens immediately. ESP-Claw Web Console LLM Settings: OpenAI-compatible custom provider, Base URL `<PUBLIC_BASE_URL>/llm/v1`, bearer auth, API key = registration's `llm_token`, model overridden by `LLM_PROXY_MODEL`.

Request is a JSON object with a nonempty `messages` list. Allowlisted keys: `messages, tools, tool_choice, parallel_tool_calls, temperature, top_p, stop, response_format, stream, stream_options, seed, reasoning_effort, max_tokens, max_completion_tokens`. Other keys (including `n`) are dropped. Both token limits are clamped to `LLM_PROXY_MAX_TOKENS`; if absent, `max_tokens` defaults to it. `stream_options` accepts only boolean `include_usage`. Model is always pinned server-side. Device tokens are never forwarded; upstream Authorization uses the server key.

Non-streaming JSON is returned as-is. Streaming always requests usage upstream. When the client requests `include_usage:true`, stream bytes are relayed unchanged, including usage-only chunks. Otherwise chunks with `choices:[]` and `usage`, plus their blank separator, are removed; other lines and `[DONE]` are preserved. Only content-type and `Cache-Control:no-store` are sent; provider headers/cookies are never copied. Usage is charged from `usage.total_tokens`; missing usage charges the configured request maximum. Per-device KL-day budget/rate limits are in memory, reset on restart and require one worker.

All errors have `{"error":{"message","type","code"}}`. 401 auth uses this shape too. Invalid body/options → 400 `invalid_request_error`; body cap → 413; per-device/upstream rate limit → 429 `rate_limited`; daily budget → 429 `budget_exceeded`; missing config → 503 `proxy_not_configured`; upstream auth/server/connect/timeout failure → 502 `upstream_error`. Upstream 400 messages pass through only at ≤300 characters. A midstream failure ends the stream; prompts/completions are never logged.

## 8. Order state machine

Status: Implemented in Phase 2 (DB trigger `orders_enforce_status_transition` is authoritative; `app/domain/order_state.py` mirrors it). Phase 2 drives `created → quoted → awaiting_device_approval`, `→ expired`, `→ cancelled`; approval and payment transitions are exercised from Phase 3.

Allowed transitions (anything else is rejected by the database):

```text
CREATED            -> QUOTED, CANCELLED, EXPIRED
QUOTED             -> AWAITING_APPROVAL, CANCELLED, EXPIRED
AWAITING_APPROVAL  -> APPROVED, DENIED, EXPIRED, CANCELLED
APPROVED           -> PAYMENT_PENDING, CANCELLED
PAYMENT_PENDING    -> PAID, PAYMENT_FAILED, REQUIRES_RECONCILIATION
REQUIRES_RECONCILIATION -> PAID, PAYMENT_FAILED, PAYMENT_PENDING
PAYMENT_FAILED     -> PAID   (PayPal-confirmed late completion only)
PAID, DENIED, EXPIRED, CANCELLED -> (none)
```

Phase 2 changes: added `payment_failed → paid` (late completion, audit `payment.late_completion`), `requires_reconciliation → payment_pending` (PayPal reports PENDING) and `payment_pending → requires_reconciliation` (reconcile exhausted).

| API / MCP `status` | DB `status` | Entered when | Actor |
| --- | --- | --- | --- |
| `created` | `CREATED` | order row inserted | backend |
| `quoted` | `QUOTED` | merchant quote validated and totals recomputed | backend |
| `awaiting_device_approval` | `AWAITING_APPROVAL` | `create_order` completes | backend |
| `approved` | `APPROVED` | verified gadget approval (and phone approval if required) | device signature / user |
| `payment_pending` | `PAYMENT_PENDING` | spending reserved, PayPal request sent | backend |
| `paid` | `PAID` | PayPal confirms `COMPLETED` (capture response, poll or webhook) | backend |
| `denied` | `DENIED` | device or phone denial | device / user |
| `expired` | `EXPIRED` | quote or challenge expired before approval | backend |
| `cancelled` | `CANCELLED` | user or system cancel before payment | user / backend |
| `payment_failed` | `PAYMENT_FAILED` | PayPal declined or errored definitively; reservation released | backend |
| `requires_reconciliation` | `REQUIRES_RECONCILIATION` | PayPal result uncertain (timeout, unknown) | backend |

```text
created → quoted → awaiting_device_approval → approved → payment_pending → paid
                          │        │              │              │
                          │        └→ denied      └→ cancelled   ├→ payment_failed
                          ├→ expired                             └→ requires_reconciliation → paid | payment_failed
                          └→ cancelled
```

Rules: only the backend transition validator changes status; every security-sensitive transition records actor and reason; `requires_reconciliation` resolves only from PayPal's authoritative status, never by re-charging. Orders above the phone threshold cannot leave `awaiting_device_approval` for `approved` until phone approval exists (and `create_order` rejects them meanwhile).

### Implemented payment and dashboard additions (phases 3–4)

`paid_at` is `payment_attempts.completed_at`, fixed when first completed. `approved_at` is the attempt creation time in the atomic approval transaction. Late completion may move `payment_failed → paid`; pending confirmation may move `requires_reconciliation → payment_pending`; exhaustion may move `payment_pending → requires_reconciliation`. Committed/released accounting uses the original approval day. Refund/reversal events are audit-only and never restore daily capacity.

| Endpoint | Contract |
| --- | --- |
| `POST /api/v1/payment-methods/paypal/setup` | JWT; 5/min/user; 201 `{payment_method_id,approve_url,expires_at}`; hashed high-entropy state, 30 min TTL; 502 `paypal_unavailable` |
| `GET /api/v1/payment-methods/paypal/return` | Unauthenticated callback; pending/unexpired state lookup; ignore provider query token ids, use stored setup reference; require APPROVED, exchange with `pt-<id>`, activate and clear state; cancellation marks failed; static escaped HTML, no-store/no-referrer; 10 failed states/min/IP |
| `GET /api/v1/payment-methods` | JWT/RLS; safe columns including `payer_email_masked` |
| `DELETE /api/v1/payment-methods/{id}` | JWT; 200 `{id,status:"revoked"}`, idempotent; foreign/missing →404; active attempt using method →409 `payment_in_progress`; keep vault reference hidden, provider delete best effort |
| `GET /api/v1/orders/{id}/timeline` | JWT/RLS; `{events:[...]}` merging conversation summaries, A2A summaries, approvals, attempts and order audit rows, newest first |
| `GET /api/v1/orders/{id}/a2a-messages` | JWT/RLS; `{messages:[...]}` safe summary columns |
| `GET /api/v1/conversations` | JWT/RLS; `{conversations:[...]}` |
| `GET /api/v1/conversations/{id}/events` | JWT/RLS; `{events:[...]}` summary columns |
| `GET /api/v1/spending/summary` | JWT subject sent to service RPC; exactly the MCP summary body |
| `GET /api/v1/audit-events` | JWT/RLS; `{audit_events:[...]}` without details or actor_id |
| `POST /api/v1/webhooks/paypal` | Unauthenticated; verification and dedup below |

Payment-method setup/return/delete require PayPal and public URL config; missing →503 `paypal_not_configured` / `public_url_not_configured`. Only open approve links you generated; check the masked linked wallet. Registration and payment method list responses must never expose raw tokens except registration's one-time tokens.

Dashboard routes require JWT and `SUPABASE_PUBLISHABLE_KEY`; unset →503 `dashboard_not_configured` before JWT verification. Foreign ids →404. Lists with pagination use `limit=1..100`, default 20, `before=<created_at ISO>`; invalid →422. User PostgREST JWT errors →401; other errors →503. Explicit safe columns hide token hashes, vault/customer/setup references, provider order/capture ids, idempotency keys, signature/nonce/canonical data, payloads and audit details/actor ids. Only `public.orders` participates in Realtime; other resources are polled.

### Verified webhooks

Status: Implemented — Phase 4. Required PayPal transmission id/time/signature, cert URL and auth algorithm headers (≤500 chars each), JSON object with string id/event_type, body ≤256 KB. 60 counted validation/signature failures/min/IP; once blocked, every delivery gets 429 before any PayPal call. Missing config →503 `webhooks_not_configured`; malformed →400 `invalid_webhook`; verification FAILURE →400 `invalid_signature` with throttled audit; verify outage →503.

Verification splices the exact raw received event bytes into the PayPal verify body. Nothing persists before SUCCESS. Stored payload has only `event_type,resource_type,resource_id,custom_id,status,summary`. Processed duplicate →200 `{"status":"duplicate"}`; an unprocessed duplicate reuses the current verified body. Success/unknown event or resource →200 `{"status":"processed"}`. Processing failure retries safely through PayPal.

CAPTURE COMPLETED/PENDING/DENIED/DECLINED resolves by capture id, related order id, then UUID custom_id. REFUNDED resolves its parent capture from `links[rel=up]` then custom_id. REVERSED uses that lookup plus capture id fallback; both only audit refunds. CHECKOUT.ORDER.COMPLETED resolves the provider order id and embedded capture. Completed amount/currency mismatch →uncertain + `payment.conflicting_result`; out-of-order PENDING after completion cannot undo PAID. Webhooks can resolve exhausted attempts. Polling remains primary; stable webhook URL is optional with quick tunnels.

## 9. Voice endpoints (optional)

Status: Proposed — stretch. Auth: device token.

`POST /api/v1/voice/stt` — body `audio/wav`, 16 kHz mono 16-bit PCM, ≤ 8 s (≤ 300 KB). `200 {"text":"buy milk and bread"}`. Errors: `413 audio_too_large`, `415 unsupported_media_type`, `502 stt_unavailable`.

`POST /api/v1/voice/tts` — `{"text":"Paid RM13.80 to Grocer Demo."}` (≤ 500 chars). `200 {"url":"https://<host>/api/v1/voice/audio/<signed-id>.mp3","expires_at":"2026-10-24T13:08:00Z"}`. The audio URL is a short-lived signed URL with no header auth, because the Lua `player:play(url)` cannot add headers. Errors: `400 text_too_long`, `502 tts_unavailable`.

## 10. Merchant agent protocol (A2A)

Status: Implemented. ClawPay is the client; every merchant agent (mock catalogs, the Channel3 adapter, or a partner's own agent) is a server for the same messages. Code: `app/integrations/a2a/client.py` (transport), `app/services/merchant_service.py` (validation), `app/merchant_agents/` (agents), `app/services/fulfillment_service.py` (handoff).

Transport: JSON-RPC 2.0 `message/send` to `POST <agent>/a2a`, one `data` part in the request and in the reply; agent card at `GET /.well-known/agent-card.json`. Reply must be HTTP 200 JSON, ≤ 64 KB, with the request's JSON-RPC `id`. A JSON-RPC `error` means the merchant is unavailable. Every answer is untrusted: ids, merchant id, items, totals and expiry are checked against what ClawPay sent, and a mismatch rejects the answer (audit `merchant.response_rejected`), it is never corrected.

### 10.1 Registry and configuration

Merchants are listed in `app/merchant_agents/registry.py`. A spec's kind decides how ClawPay reaches it when no URL is configured:

| Kind | Agent | Enabled when |
| --- | --- | --- |
| `catalog` | in-process mock, catalog in `catalogs.py` (`grocer`, `electronics`, `shopee`, `foodpanda`, `grab`) | listed in `ENABLED_MERCHANTS` (default `grocer,electronics`) |
| `api` | in-process adapter for a real API (`channel3`) | adapter configured (`CHANNEL3_API_KEY`) or a URL is set |
| `remote` | hosted elsewhere | a URL is set |

`MERCHANT_AGENT_ENDPOINTS=code=url,code=url` sends that merchant's calls over HTTP (https, or http to 127.0.0.1/localhost). It wins over the legacy `GROCER_AGENT_ENDPOINT` / `ELECTRONICS_AGENT_ENDPOINT` / `CHANNEL3_AGENT_ENDPOINT`. Codes must be in the registry. The MCP `merchant_id` enums list the enabled merchants in registry order. Each merchant also needs a `public.merchants` row (migration; `seed.sql` is generated from the migrations).

Shopee, Foodpanda and Grab are demo agents modelled on those platforms; they do not talk to the real services. One item in each is sold out (`SHP-PLUSH-LTD`, `FPD-DURIAN-CREPE`, `GRB-RIDE-PREMIUM`) to demo a refused reservation.

### 10.2 Messages

| Request `type` | Answer `type` | Purpose |
| --- | --- | --- |
| `clawpay.search_request.v1` {query, max_results} | `clawpay.search_response.v1` {merchant_id, products[{sku, name, unit_price_minor, currency}]} | search |
| `clawpay.quote_request.v1` {items[{sku, qty}], currency} | `clawpay.quote_response.v1` {merchant_id, merchant_name, quote_id, currency, items[{sku, name, qty, unit_price_minor, line_total_minor}], subtotal_minor, total_minor, expires_at} | price a cart for `create_order` |
| `clawpay.reserve_request.v1` {order_ref, currency, items[{sku, qty, unit_price_minor}], total_minor} | `clawpay.reserve_response.v1` {merchant_id, order_ref, reservation_id, currency, items[{sku, qty}], total_minor, expires_at} | hold the approved cart before payment |
| `clawpay.confirm_request.v1` {order_ref, reservation_id} | `clawpay.confirm_response.v1` {merchant_id, order_ref, reservation_id, fulfillment_id, status: "accepted"} | the order is paid; merchant accepts it |
| `clawpay.cancel_request.v1` {order_ref, reservation_id?} | `clawpay.cancel_response.v1` {merchant_id, order_ref, status: "released"} | release the hold |

Every request and answer carries `correlation_id`, which the answer must echo. `order_ref` is the ClawPay order id. `reservation_id` and `fulfillment_id` match `^[A-Za-z0-9_-]{1,64}$`. A reserve answer must echo the exact skus, quantities, currency and total, and expire 30 s to 24 h from now. Reserve is idempotent per `order_ref`. Cancel without `reservation_id` releases whatever the merchant holds for the order; cancelling nothing is a success.

Refusals are `clawpay.error.v1` {correlation_id, code, sku?} with code `bad_request`, `unknown_sku`, `currency_not_supported`, `out_of_stock`, `price_changed`, `unknown_reservation`, `reservation_expired` or `already_confirmed`.

The mock agents hold stock in memory for 15 minutes (the reconciler's replay window). The Channel3 agent re-checks live prices on reserve but cannot hold retailer stock; its confirm is an acknowledgement, not a retailer order.

### 10.3 Reserve before pay

`MERCHANT_FULFILLMENT_ENABLED=true` (default). Each reserve/confirm/cancel call times out at `min(MERCHANT_FULFILLMENT_TIMEOUT_SECONDS, MERCHANT_AGENT_TIMEOUT_SECONDS)` (default 3 s).

1. After a verified gadget approval creates the payment attempt, ClawPay sends `reserve` with the order's stored items and total.
2. Refused, unanswered or invalid → the never-sent attempt fails (`failure_code` `merchant_out_of_stock`, `merchant_price_changed`, `merchant_unknown_sku`, `merchant_unavailable`, `merchant_fulfillment_invalid`), spending is released, the order becomes `payment_failed`, and the approval answer says no money was taken. ClawPay also sends a best-effort `cancel` when it could not read the merchant's answer.
3. Reserved → PayPal create/capture as before.
4. Paid → `confirm`. A confirm failure never undoes the payment: fulfillment becomes `fulfillment_pending` and is retried on `get_order_status`. If the merchant lost the hold, ClawPay reserves again and confirms.
5. Payment failed → `cancel`; fulfillment becomes `released`.
6. Payment still pending → the hold stays; status reads confirm or release it once the payment resolves.

The reconciler never sends a first PayPal request for an order whose merchant has not reserved (`failure_code` `merchant_not_reserved`).

Fulfillment state is `orders.fulfillment_status` (`none`, `reserved`, `reservation_failed`, `confirmed`, `fulfillment_pending`, `released`), with `merchant_reservation_id` and `merchant_fulfillment_id`, written only by `set_order_fulfillment` (compare-and-set, audit `order.fulfillment_changed`). It is separate from the order status machine in section 8. Every reserve/confirm/cancel exchange is in `a2a_messages` with the order id.
