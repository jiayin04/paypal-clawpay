# Setup: ClawPay backend + ESP-Claw device (Windows)

Backend phases 0–4 are implemented and locally mocked tests cover the payment flow. Hosted migrations, live PayPal behavior and hardware integration are separate validation steps. Firmware work in section E is required in a separate ESP-Claw fork; the backend does not build or flash it.

Contracts referenced below are in [contracts.md](contracts.md). Backend details are in [../README.md](../README.md).

Commands are PowerShell unless marked WSL.

## Prerequisites

| Tool | Needed for | Install | Label |
| --- | --- | --- | --- |
| Python 3.12 | backend | python.org installer or `py install 3.12` | Works now |
| uv 0.10.x | backend deps | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | Works now |
| Supabase CLI 2.x | migrations | `scoop bucket add supabase https://github.com/supabase/scoop-bucket.git` then `scoop install supabase` | Works now |
| Chrome or Edge | web flasher (WebSerial) | — | Works now |
| cloudflared or ngrok | public HTTPS for the device | `winget install --id Cloudflare.cloudflared` or `winget install --id Ngrok.Ngrok` | Works now |
| esptool 4.8.1 | board check, flash backup | run through `uvx --from esptool==4.8.1 esptool.py ...` (no global install) | Works now |
| Git (includes `openssl`) | cloning, test keys | git-scm.com | Works now |
| ESP-IDF v5.5.4 + `esp-bmgr-assist` | custom firmware only (section E) | WSL2 recommended (see E) | Installable now; only needed for firmware patches (Phase 2+) |

Docker is not required: the local Supabase stack was only for migration validation and has been removed.

Clone ESP-Claw (and ESP-IDF, if used) to a short path **without spaces**, e.g. `C:\esp\esp-claw`. A workspace path with spaces can break the firmware build.

## A. Backend

### A1. Install dependencies — Works now

```powershell
cd "C:\path\to\clawpay\backend"  # replace with your checkout path
uv sync --python 3.12
uv run pytest
```

Tests need no network and no Supabase.

### A2. Create `.env` — Works now

```powershell
Copy-Item .env.example .env
```

Fill in (never commit `.env`):

- `SUPABASE_URL` = `https://<project-ref>.supabase.co` (hosted project → Project Settings → API).
- `SUPABASE_SERVICE_ROLE_KEY` = the project's `service_role` key. Server-side only. The legacy JWT `service_role` key is the tested path with `supabase` 2.33.0; the newer `sb_secret_...` key has not been tested here.
- Fill in Sandbox `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`, `PUBLIC_BASE_URL`, a random ≥32-character `DEVICE_TOKEN_PEPPER`, a separate ≥32-character `INTERNAL_SERVICE_TOKEN`, and `SUPABASE_PUBLISHABLE_KEY`. Set `MCP_ENABLED=true`. LLM settings are optional until you configure the proxy.
- Keep the policy defaults: `PHONE_APPROVAL_THRESHOLD_MINOR=10000`, `DAILY_SPENDING_LIMIT_MINOR=50000`, `DEFAULT_CURRENCY=MYR`. Do not use `*_MYR` names; startup rejects them.

### A3. Apply migrations to the hosted Supabase project — Works now

**Tables already dropped?** Use the [direct rebuild guide](database-rebuild.md): run [schema.sql](../supabase/schema.sql), then [seed.sql](../supabase/seed.sql) in SQL Editor as postgres. This recreates the complete current backend schema, repairs profiles for surviving Auth users, and seeds the two merchants. Do not follow the incremental push below after that full rebuild until migration history has been reconciled. Device registration and PayPal linking must be repeated.

**Existing intact database:** follow the incremental steps below.

Run from `backend\` (where `supabase\migrations\` lives):

```powershell
supabase login
supabase link --project-ref <project-ref>
supabase migration list
# If the initial schema exists but its remote history entry is absent:
supabase migration repair --status applied 20261024133807
# Only if the hosted schema differs from the original: pull and review that diff first.
# supabase db pull
supabase db push --dry-run
# Proceed only when the dry run lists exactly the four NEW migrations below.
supabase db push
```

The initial migration is already hosted; never edit or reapply it. New migrations, in order: `20261025090000_phase2_hardening.sql`, `20261025090100_phase2_devices_orders_a2a.sql`, `20261025090200_phase3_approvals_payments.sql`, `20261025090300_phase4_realtime.sql`. If repair is unnecessary, skip it. Review any hosted schema changes before pushing. These commands are operator steps, not commands executed by the coding agent.

Optional local validation (needs Docker Desktop; not executed in this implementation):

```powershell
supabase start
supabase db reset
supabase test db
supabase stop
```

### A4. Run the API — Works now

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

In a second terminal:

```powershell
curl.exe -s -i http://127.0.0.1:8000/health
curl.exe -s -i http://127.0.0.1:8000/health/ready
```

Expected:

- `/health` → `{"status":"ok","service":"clawpay-middleware",...}`
- `/health/ready` → `200 {"status":"ready","checks":{"supabase":"ok"}}` after A2 and A3. `503` with `not_configured` means the env vars are missing; `error` means unreachable or the migration is not applied.

Logs and metrics: every response has an `X-Request-ID` header, and each log line shows the same id in brackets, so you can find all lines for one request. Set `LOG_FORMAT=json` to ship logs to a collector. To scrape metrics, set `METRICS_TOKEN` (32+ chars) and run:

```powershell
curl.exe -s -H "Authorization: Bearer <METRICS_TOKEN>" http://127.0.0.1:8000/metrics
```

Prometheus scrape config: `bearer_token` = that value, `metrics_path: /metrics`. Keep `/metrics` off the public tunnel if you can (it reveals traffic shape, though no ids or secrets).

Stop the server with `Ctrl+C`. If it runs in the background, use `taskkill /PID <pid> /T /F` (the venv launcher spawns a child process).

### A5. Enable device integration settings — Implemented

Set these server settings:

```dotenv
PUBLIC_BASE_URL=https://<your-tunnel-host>
MCP_ENABLED=true
DEVICE_TOKEN_PEPPER=<python -c "import secrets; print(secrets.token_urlsafe(32))">
INTERNAL_SERVICE_TOKEN=<same command, different value>
```

Restart uvicorn after editing `.env`.

## B. Public HTTPS tunnel — Works now

The device only accepts CA-signed certificates, so use a tunnel rather than a self-signed cert or plain LAN HTTP.

Cloudflare quick tunnel (no account; URL changes on every start):

```powershell
cloudflared tunnel --url http://localhost:8000
```

ngrok (free account, `ngrok config add-authtoken <token>` once):

```powershell
ngrok http 8000
```

Copy the `https://...` URL into `PUBLIC_BASE_URL` and the SKILL.md (D1). A quick-tunnel URL change means updating both.

Notes:

- The tunnel exposes every route, including `/health/ready` and `/docs` (non-production). Stop the tunnel when you are not testing.
- PayPal webhooks need a stable URL; polling reconciliation is the primary path, so quick tunnels are fine for the demo.

## C. ESP-Claw device

### C1. Confirm the board module — Works now

Open item: the exact module is not confirmed yet. ESP-Claw needs **≥ 8 MB flash and 8 MB octal PSRAM** (`N8R8`, `N16R8`, `N32R16`). `N4R2`, `N8R2`, `N16R2` are not enough. Mic + speaker + screen are needed for the full demo (screen and button are enough for the text-only demo).

1. Read the marking on the metal shield, e.g. `ESP32-S3-WROOM-1-N16R8`. Avoid `U` (external antenna) variants.
2. Find the COM port in Device Manager → Ports.
3. Query the chip:

```powershell
uvx --from esptool==4.8.1 esptool.py --chip esp32s3 -p COM5 flash_id
```

Look for `Detected flash size` and, in the chip description, embedded PSRAM. Octal PSRAM is confirmed later by `Embedded PSRAM 8MB` / PSRAM init lines in the ESP-Claw boot log.

4. Note whether the board matches an ESP-Claw profile (ESP-BOX-3, M5Stack CoreS3, Waveshare S3 Touch AMOLED 2.16, DevKitC-1, ...). A plain N8R8 board uses the `esp32_S3_DevKitC_1` profile.

### C2. Back up the current flash — Works now

Do this before the first ESP-Claw flash. Use the size from `flash_id` (8 MB = `0x800000`, 16 MB = `0x1000000`):

```powershell
uvx --from esptool==4.8.1 esptool.py --chip esp32s3 -p COM5 -b 460800 read_flash 0 0x1000000 C:\esp\backup_flash_16mb.bin
```

Restore later with `write_flash 0 C:\esp\backup_flash_16mb.bin`.

### C3. Flash ESP-Claw with the web flasher — Works now

1. Open https://esp-claw.com/en/flash/ in Chrome or Edge.
2. Pick the matching board profile, connect over WebSerial, and flash.
3. Close the serial monitor before flashing; only one program can hold the COM port.

### C4. Wi-Fi, LLM and lock-down — Works now

In the flasher's configuration step or the device Web Console (`http://esp-claw.local/` once on Wi-Fi):

1. Wi-Fi: 2.4 GHz network only. Use a private network or phone hotspot, not shared Wi-Fi.
2. LLM (now): an OpenAI-compatible or Anthropic key from a project with a **hard spending cap**. ESP-Claw stores it in plaintext NVS, and the Web Console can read it back. Use a strong tool-use model.
3. LLM via ClawPay proxy — **Implemented**: custom OpenAI-compatible provider, Base URL `https://<PUBLIC_BASE_URL host>/llm/v1`, auth `bearer`, key = `llm_token` (`cpl_`) returned at registration. Then delete the direct key.
4. Set a SoftAP password and enable SoftAP auto-disable.
5. Keep the Web Console off untrusted networks. It is unauthenticated plain HTTP.

### C5. Check the agent works — Works now

- Web Chat: open `http://esp-claw.local/` and send "hello".
- Telegram (optional): create a bot with @BotFather, put the bot token in the Web Console IM settings, message the bot. Telegram handles text only (no voice notes); phone keyboard dictation works as voice input.

## D. Connecting the device to ClawPay

### D1. Upload the ClawPay skill — Backend implemented; firmware bearer patch required

ESP-Claw has no MCP server registry: the LLM passes `server_url` and `tool_name` on every call, so the skill must spell them out. Upload this as `skills/clawpay/SKILL.md` via the Web Console file manager (Admin Mode), next to the built-in skills, then restart the device. The file format (JSON front matter between `---` lines with `metadata.cap_groups`) matches ESP-Claw's built-in skills; how quickly an uploaded skill is picked up has not been verified.

Replace `<server_url>` with the tunnel URL (no trailing slash). `<endpoint>` is `mcp` once the bearer-token firmware patch (E3) is flashed, or `mcp/<device_token>` before that (demo only; ≤ 63 characters).

```markdown
---
{
  "name": "clawpay",
  "description": "Shop from ClawPay merchants and pay with PayPal after on-device button approval.",
  "metadata": {
    "cap_groups": [
      "cap_mcp_client"
    ]
  }
}
---
# ClawPay shopping

Use this skill when the user wants to find products, buy something, check an order, or check today's spending.

Always call ClawPay with `mcp_call_tool` using exactly:
- `server_url`: `<server_url>`
- `endpoint`: `<endpoint>`

Tools (`tool_name` and `arguments`):
- `search_products` `{"query": "milk", "max_results": 5}` returns sku, name, merchant_id and price.
- `create_order` `{"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}` creates an unpaid order and returns order_id and total.
- `get_order_status` `{"order_id": "<order_id>"}` returns the authoritative status.
- `get_spending_summary` `{"period": "today"}` returns spending against the daily limit.

## Rules

- Never tell the user no money was taken without calling `get_order_status` again.
- Use only sku and merchant_id values returned by `search_products`. Never invent prices or totals; repeat the total exactly as returned.
- One merchant per order. Confirm items and quantities with the user before `create_order`.
- After `create_order`, request approval: call `clawpay_request_approval` with the order_id if that tool exists. If it does not, tell the user the order is waiting for approval on the device.
- Tell the user to check the device screen and hold the button to approve.
- Never say a payment succeeded unless `get_order_status` returns status `paid`. If the status is `payment_pending`, say it is still processing and check again when asked.
- If a tool returns an error, tell the user the message from the result. Do not retry with different items or amounts unless the user asks.
- Never put device IDs, tokens or keys in tool arguments or chat replies.
```

When the `cap_clawpay` firmware component exists (Phase 3), add `"cap_clawpay"` to `cap_groups`.

### D2. Register the device — Implemented

Until `cap_clawpay` generates the real key on the device (Phase 3), register a test P-256 key generated on the laptop. Keep the private key out of the repo:

```powershell
openssl ecparam -name prime256v1 -genkey -noout -out C:\esp\test_device_key.pem
openssl ec -in C:\esp\test_device_key.pem -pubout -out C:\esp\test_device_pub.pem
```

Register (admin call, `INTERNAL_SERVICE_TOKEN` from `.env`):

```powershell
$body = @{
  user_id        = "<supabase-user-uuid>"
  device_name    = "desk-claw"
  public_key_pem = (Get-Content C:\esp\test_device_pub.pem -Raw)
  key_version    = 1
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/devices/register" `
  -Headers @{ Authorization = "Bearer $env:INTERNAL_SERVICE_TOKEN" } `
  -ContentType "application/json" -Body $body
```

The response contains `device_id`, `device_token` (`cpd_`) and `llm_token` (`cpl_`) once. Put `llm_token` into Web Console LLM Settings. Store the token in the firmware (E3) or the path-token `endpoint` (D1). In Phase 3, re-register with the public key printed by `cap_clawpay`.

### D3. End-to-end test — Backend implemented; hardware integration still required

1. Backend running (A4), tunnel up (B), skill uploaded (D1), device registered (D2).
2. In Web Chat or Telegram: "find milk and bread". Expect product names and prices.
3. "Buy two milk." Expect an order total and a prompt to approve on the device.
4. With the separate `cap_clawpay` firmware installed, the device screen shows merchant and amount; hold the button ≥ 1.5 s.
5. "Did it go through?" The agent should call `get_order_status` and report `paid` only when it is.

## E. Custom firmware — Required separate ESP-Claw work

### E1. Build environment (WSL2 recommended)

The build patches files inside ESP-IDF using exact multi-line matching; CRLF line endings on a native Windows checkout can make configure fail. WSL2 avoids that.

```bash
# WSL (Ubuntu)
sudo apt-get install -y git wget flex bison gperf python3 python3-venv cmake ninja-build ccache libffi-dev libssl-dev dfu-util libusb-1.0-0
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.4 --recursive https://github.com/espressif/esp-idf.git
./esp-idf/install.sh esp32s3
. ./esp-idf/export.sh
pip install esp-bmgr-assist
git clone https://github.com/espressif/esp-claw.git
cd esp-claw && git checkout 9b23c15   # the inspected commit; ESP-Claw has breaking changes, so pin one
```

Native Windows (ESP-IDF installer / EIM) may work but is untested; keep paths short and without spaces (`C:\esp\...`) and avoid `core.autocrlf=true` for both trees.

### E2. Build and flash

```bash
cd ~/esp/esp-claw/application/edge_agent
idf.py bmgr -c ./boards -b <board>    # e.g. esp32_S3_DevKitC_1
idf.py build
```

Flash and monitor:

- From WSL with the USB device attached through `usbipd`: `idf.py -p /dev/ttyACM0 flash monitor`.
- Or flash from Windows using the generated args file:

```powershell
cd \\wsl$\Ubuntu\home\<user>\esp\esp-claw\application\edge_agent\build
uvx --from esptool==4.8.1 esptool.py --chip esp32s3 -p COM5 -b 460800 write_flash "@flash_args"
```

### E3. Bearer-token patch (`cap_mcp_client`) — Separate firmware work

In `components/claw_capabilities/cap_mcp_client/src/cap_mcp_client_core.c`, `cap_mcp_mgr_create()`, after `esp_mcp_mgr_init` succeeds, read the token from NVS namespace `clawpay` and set it. Sketch; check the exact `esp_mcp_mgr_set_bearer_token` signature in `esp_mcp_mgr.h` (mcp-c-sdk 2.0.x):

```c
/* After esp_mcp_mgr_init(...) succeeds */
nvs_handle_t h;
if (nvs_open("clawpay", NVS_READONLY, &h) == ESP_OK) {
    char token[96];
    size_t len = sizeof(token);
    if (nvs_get_str(h, "device_token", token, &len) == ESP_OK) {
        esp_mcp_mgr_set_bearer_token(*mgr_out, token);
    }
    nvs_close(h);
}
```

Then switch the SKILL.md `endpoint` to `mcp`, and revoke and re-register the device so the old path token stops working.

### E4. `cap_clawpay` C component — Separate firmware work

- Key: generate a P-256 key on first boot, store it in NVS namespace `clawpay` (never `app`, never FAT), print the public key PEM from a console command for D2.
- LLM-callable `clawpay_request_approval {order_id}` only queues the order ID. It never accepts amounts or merchants from the LLM.
- Private approver task: `GET /api/v1/orders/{id}/approval-challenge` → decode `canonical_b64url` → render merchant, amount and currency from those bytes → configure the approval GPIO as input with pull-up → require a continuous low for ≥ 1.5 s via `gpio_get_level` → SHA-256 + ECDSA P-256 sign (mbedTLS) → `POST /api/v1/orders/{id}/gadget-approval` → return only "approved" / "declined" / error text.
- Register with `app_capabilities_register_external_group`. Signing is never a capability.

### E5. Lock down Lua

LLM-written Lua can call any capability and forge events. Disable `lua_driver_gpio` and `lua_module_event_publisher` (via `enabled_lua_modules` or `idf.py menuconfig` → search `APP_CLAW_LUA_MODULE`), and do not use the Lua `button` module for the approval pin.

### E6. Do not burn eFuses

NVS encryption with HMAC and flash encryption permanently burn eFuses. Do not enable them on your only board during the hackathon.

## F. Troubleshooting

| Symptom | Check |
| --- | --- |
| `uv sync` picks the wrong Python | `uv sync --python 3.12`; `py -0` lists installed versions. |
| Startup error about `*_MYR` | Rename to `PHONE_APPROVAL_THRESHOLD_MINOR` / `DAILY_SPENDING_LIMIT_MINOR` in minor units. |
| `/health/ready` 503 `not_configured` | `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` empty in `.env`. |
| `/health/ready` 503 `error` | Wrong key/URL, project paused, or `supabase db push` not run. Result is cached 5 s. |
| `supabase link` / `db push` fails | Re-run `supabase login`; check the project ref and database password. |
| Port 8000 busy after stopping | `taskkill /PID <pid> /T /F`. |
| Device tool calls fail with TLS errors | Must be public-CA HTTPS (tunnel URL), not `http://` or a self-signed cert. |
| Device tool calls time out | 20 s limit per call including TLS + `initialize`; check backend logs and tunnel latency. |
| LLM never calls ClawPay | Skill not loaded or `cap_mcp_client` not visible; confirm `skills/clawpay/SKILL.md` and restart. Use a strong tool-use model. |
| LLM calls the wrong URL | `server_url` / `endpoint` in SKILL.md must match the current tunnel URL exactly. |
| `401` from `/mcp` | Bearer patch not flashed and endpoint is `mcp` without the token, or the device was revoked. |
| Web flasher cannot open the port | Close other serial monitors; try another USB cable/port; hold BOOT while connecting. |
| Boot log shows no PSRAM or crashes on start | Board is not N8R8+, or the wrong board profile was flashed. |
| Firmware configure fails in the IDF patch step | Line-ending mismatch; build in WSL2 or the `espressif/idf:release-v5.5` Docker image. |


## G. Run the backend and ESP-Claw together

1. Complete A1–A3. Fill Supabase keys, Sandbox credentials, pepper/admin token; keep server keys on the laptop. Default MYR policy is 10000 per-order threshold and 50000 daily cap. Phone approval is deferred, so larger orders are blocked. Empty merchant endpoints run the deterministic Grocer/Electronics agents in-process. To run a standalone Grocer: `uv run uvicorn --factory app.merchant_agents.app:grocer_app --host 127.0.0.1 --port 8101`, then set `GROCER_AGENT_ENDPOINT=http://127.0.0.1:8101/a2a`.
2. Start one API worker with the A4 command. Open the HTTPS tunnel from B, set its origin in `PUBLIC_BASE_URL`, set `APP_ENV=production`, restart. Use `CLIENT_IP_HEADER=cf-connecting-ip` for cloudflared or `x-forwarded-for-last` behind trusted ngrok; otherwise `none`. Quick tunnels change their URL on restart, so update device/return URLs each time. Keep readiness off public ingress.
3. Confirm the board and flash/configure ESP-Claw using C and the separate firmware patches in E. The bearer patch must send the private `cpd_` token to `/mcp`; `cap_clawpay` must own the private P-256 key and sign stored canonical bytes after a button hold. A stock web-flashed image alone does not supply this payment-approval integration. Register its public key using D2. Save the two returned tokens once: `cpd_` in private NVS `clawpay`; `cpl_` in LLM Settings.
4. Configure the backend LLM proxy: `LLM_PROXY_UPSTREAM_BASE_URL` (OpenAI-compatible base without `/chat/completions`), `LLM_PROXY_UPSTREAM_API_KEY`, `LLM_PROXY_MODEL`. Web Console: custom provider, base `<PUBLIC_BASE_URL>/llm/v1`, bearer auth, key `cpl_...`; any model is replaced by the pinned model. Delete a direct upstream key from the device. `LLM_PROXY_MAX_TOKENS=1024`, `LLM_PROXY_REQUESTS_PER_MINUTE=20`, `LLM_PROXY_DAILY_TOKEN_LIMIT=200000`, `LLM_PROXY_MAX_BODY_BYTES=262144`, `LLM_PROXY_TIMEOUT_SECONDS=110`. Usage and limits are per process/KL day and reset on restart; use one worker. Streams work with or without client-requested usage.
5. Sign in to Supabase and obtain a user access JWT (never a service-role key). Link that user's Sandbox PayPal wallet with `POST /api/v1/payment-methods/paypal/setup`, header `Authorization: Bearer <user JWT>`. Open the returned `approve_url`. **Only open approve links you generated yourself**, and check the masked account displayed after linking. The browser returns to `<PUBLIC_BASE_URL>/api/v1/payment-methods/paypal/return?state=...`; the backend uses its stored setup id, not query token ids. `GET /api/v1/payment-methods` with the same JWT shows the masked wallet. Setup state expires after 30 minutes.
6. Upload the ClawPay skill from D1, enable the needed capabilities, and use text chat for the demo. Search, create an order ≤RM100, fetch the canonical challenge through the private C task, show its merchant/amount, hold the button to sign, then post approval. No payment is created until the signature and policy pass. Poll `get_order_status` until confirmed. Do not say no money was taken without another status call. Daily summary includes reserved plus committed spending. Dashboard GET APIs require that user's JWT; frontend UI is separate work.
7. Polling runs every `RECONCILE_INTERVAL_SECONDS=30`; inline PayPal work has `PAYPAL_INLINE_BUDGET_SECONDS=10`, per-call timeout `PAYPAL_HTTP_TIMEOUT_SECONDS=8`. Unknown provider ids replay the same request id only within `RECONCILE_REPLAY_WINDOW_SECONDS=900`; older sent attempts are exhausted and keep reservations. Never raise the window until Request-Id retention and duplicate-invoice blocking are confirmed. If the PayPal order id is known, reconciliation GETs it, with fixed-id capture only when APPROVED.
8. Webhooks are optional for quick tunnels. For a stable endpoint configure the Sandbox webhook at `<PUBLIC_BASE_URL>/api/v1/webhooks/paypal`, subscribe to PAYMENT.CAPTURE.COMPLETED/PENDING/DENIED/DECLINED/REFUNDED/REVERSED and CHECKOUT.ORDER.COMPLETED, save `PAYPAL_WEBHOOK_ID`, then restart. Polling works without it. Refund/reversal events are audit-only and do not restore daily capacity. Late completion can change payment_failed to paid.

### Optional live smoke (operator only)

Default `uv run --offline pytest` skips this. To opt in, set `CLAWPAY_LIVE_SMOKE=1`, `CLAWPAY_SMOKE_PAYPAL_CLIENT_ID` and `CLAWPAY_SMOKE_PAYPAL_CLIENT_SECRET` in the shell to isolated Sandbox credentials, then run `uv run pytest tests/integration/test_paypal_live_smoke.py`. It builds Settings with `_env_file=None` and calls only OAuth plus setup-token creation, without creating a charge/capture or opening the approve link. Remove those shell variables afterward. This was not run by the coding agent.

### Remaining validation

Apply/review migrations and run pgTAP in an operator-controlled database; test actual device/tunnel behavior. Malaysian Sandbox MYR/vault/merchant-initiated charging, payer email availability, Request-Id retention and invoice blocking remain unverified live. Fall back to `DEFAULT_CURRENCY=USD` if MYR is unsupported; minor-unit policy values then represent USD cents. Firmware is in a separate repository. No external AP2 SDK or AP2 sample compatibility is claimed; `clawpay.gadget_cart.v1` records the verified gadget signature's cart mandate.
