# ESP-Claw as the ClawPay on-device agent: investigation report

Snapshot inspected: `espressif/esp-claw` HEAD `9b23c15` (shallow clone; latest CHANGELOG entry 2026-06-04). MCP SDK inspected: `espressif/mcp-c-sdk` v2.0.1 from `espressif/esp-iot-solution/components/mcp-c-sdk`. ESP-Claw pins it as `^2.0.0` (`components/claw_capabilities/cap_mcp_client/idf_component.yml`), so the build resolves 2.0.x. Nothing was built or flashed. All findings come from reading source and docs.

## Summary answer

Go, with conditions. ESP-Claw works as the device agent for ClawPay. You need two small C additions and one config decision.

1. **Voice: not built in.** The repo has raw mic/speaker plumbing (`audio_hub`, `audio_playback`, Lua `audio` recorder/player that can play HTTPS URLs). There is no wake word, no STT/ASR and no TTS. The ASR fields in `app_claw_config_t` are placeholders that nothing uses. Lowest-effort path: a C push-to-talk component. Hold a button, record 16 kHz PCM, POST it to ClawPay `/api/v1/voice/stt` (ClawPay calls Whisper / gpt-4o-transcribe), then inject the text into the agent as a message. For replies, ClawPay returns an MP3 URL and the device plays it with `player:play(url)`. Fallback with zero firmware work: Telegram or Web Chat text, using phone keyboard dictation.
2. **MCP client: works with a public HTTPS server.**
   - Transport is MCP Streamable HTTP: JSON-RPC POST with `Accept: application/json, text/event-stream` and protocol `2025-11-25`.
   - TLS uses the ESP-IDF CA bundle (forced in the SDK), so a Let's Encrypt or Cloudflare cert works.
   - There is no server registry. The LLM passes `server_url` and `tool_name` to `mcp_call_tool` on every call. You teach it the URL with a custom SKILL.md.
   - No auth header is sent. The SDK supports `esp_mcp_mgr_set_bearer_token()`, but `cap_mcp_client` never calls it. You need a roughly 10-line patch, or put a capability token in the URL for the demo.
   - Each call opens a fresh session (TLS + `initialize` + call). Timeout is 20 s. Only `content[].text` is returned to the LLM.
3. **MCP server mode: not usable for ClawPay to call into the device.** `edge_agent` never starts `cap_mcp_server`. The separate `mcp_server_point` app has no LLM and only exposes `lua.run_script*`. It is LAN-only HTTP with mDNS and no auth by default. Use device-initiated polling or long-polling to ClawPay instead.
4. **Boards.**
   - Minimum is 8 MB flash and 8 MB PSRAM (octal).
   - A "generic ESP32-S3" works only if it is N8R8 or larger. N4R2, N8R2 and N16R2 are not enough.
   - The bare `esp32_S3_DevKitC_1` profile has only an LED strip. Mic + speaker + screen boards: ESP-BOX-3, M5Stack CoreS3, Waveshare S3 Touch AMOLED 2.16, ESP Vocat, MoveCall Moji/Cuican, SparkBot, DFRobot K10, and the DevKitC breadboard profile (ST7789 SPI LCD + USB-UAC audio).
5. **LLM config.** Set via the Web Console and stored in plaintext NVS namespace `app`. Custom OpenAI-compatible or Anthropic-compatible base URLs are supported, with auth `bearer` / `api-key` / `none`. You can point it at a ClawPay LLM proxy so no OpenAI key lives on the device. A pluggable `claw_endpoint` credential provider also exists, for short-lived tokens.
6. **Signing patch: must be C, not Lua.**
   - Custom C capability groups are supported (`app_capabilities_register_external_group`, `claw_cap_register_group`).
   - mbedTLS ECDSA P-256 is available in software. The ESP32-S3 has no ECDSA peripheral; it has a DS peripheral (RSA only) and HMAC. Ed25519 would need `espressif/libsodium`.
   - **Security finding:** Lua `capability.call()` runs as `CLAW_CAP_CALLER_SYSTEM` and skips the `CALLABLE_BY_LLM` check. The LLM writes and runs Lua ("chat coding"). So any registered capability, including one you mark non-LLM-callable, can be reached by LLM-written Lua.
   - What follows: the signing key and the "button pressed" decision must live entirely inside a C task that reads the GPIO itself. Never trust events from the event router, which Lua can forge with `event_publisher`.
7. **Build on Windows.** Use the web flasher (https://esp-claw.com/en/flash/) for stock boards. Patched firmware needs ESP-IDF v5.5.4 (CI uses `espressif/idf:release-v5.5`) plus `pip install esp-bmgr-assist`. Native Windows builds should work: everything is CMake/Python with no bash. One risk: the build patches files inside your ESP-IDF install with exact multi-line string matching and is fatal by default. CRLF line endings in the IDF checkout could break it, and WSL2 avoids that.
8. **Maturity.** Apache-2.0 and actively developed, with breaking API changes as recently as May 2026. The vendor says it is not intended for production. The Web Console is unauthenticated plain HTTP, and NVS secrets are readable through it (issue #41). IM channels: Telegram, QQ, Feishu, WeChat, local Web Chat. Telegram handles text, photo and document only, not voice notes.

---

## 1. Voice pipeline

| Piece | Present? | Evidence |
|---|---|---|
| Mic capture | Yes (PCM) | `components/common/audio_hub/include/audio_capture.h` (`audio_capture_start`, `audio_capture_open_subscriber`); Lua `audio.open_input()`, `input:read()`, `audio.recorder():record(path,{duration_ms})` → WAV/AAC (`components/lua_modules/lua_module_audio/README.md`) |
| Speaker playback | Yes | `components/common/audio_playback/`; Lua `player:play(path_or_uri)` accepts **HTTP/HTTPS URIs** (README "Player"; test `lua_module_audio/test/audio_play_https.lua`) |
| Level / FFT analysis | Yes | Lua `audio.analyzer():read_level()/read_spectrum()`; could act as a crude voice-activity detector |
| Wake word / AFE (esp-sr) | No | `git grep esp-sr|wakenet|wake word` finds nothing |
| ASR / STT | Placeholder only | `CLAW_ENDPOINT_SERVICE_ASR "asr"` (`components/claw_modules/claw_endpoint/include/claw_endpoint.h:23`) and `asr_provider/asr_api_key/asr_model/asr_endpoint` fields (`components/common/app_claw/include/app_claw.h:57-63`). Nothing else in the tree references them. |
| TTS | No | none |
| LLM audio input | No | no `input_audio` in `claw_core` |

Boards with an audio codec (from `board_devices.yaml` `type: audio_codec`), ESP32-S3 only: `esp_box_3`, `esp_vocat_board_v1_2`, `esp_sparkbot`, `m5stack_cores3`, `m5stack_sticks3`, `waveshare_ESP32_S3_Touch_AMOLED_2_16`, `waveshare_ESP32_S3_RLCD_4_2`, `movecall_moji_esp32s3`, `movecall_cuican_esp32s3`, `dfrobot_k10`, `dfrobot_ai_camera`, `xiao_esp32s3_sense` (mic), `eda_*`, `community/anyboard`, and `esp32_S3_DevKitC_1*_breadboard`. The breadboard profile uses a `fake_audio_dac` placeholder plus real USB-UAC mic/speaker (`espressif/usb_host_uac`) and PDM out on GPIO9.

**Recommended lowest-effort voice path for the hackathon (ranked):**
1. **Demo fallback, zero firmware work.** Telegram bot (`cap_im_tg`) or local Web Chat (`http://esp-claw.local/`). Speak through the phone keyboard's dictation, which sends text. The device still does screen display, the button press and signing. This de-risks the demo.
2. **Push-to-talk C component (`cap_clawpay_voice`), about 1 day.**
   - On a long press, open an `audio_capture` subscriber at 16 kHz mono 16-bit and buffer up to 8 s in PSRAM (~256 KB).
   - On release, POST `audio/wav` to `https://<clawpay>/api/v1/voice/stt` using `esp_http_client` with `crt_bundle_attach` and a device bearer token. ClawPay calls OpenAI `gpt-4o-mini-transcribe` / Whisper and returns `{text}`.
   - Inject the text into the agent as an IM-style inbound message on `cap_im_local`, or publish a message event (the same mechanism `lua_module_event_publisher` uses: `publish_message{channel, chat_id, text}`).
   - For the reply, add a router rule or capability that sends the final text to ClawPay `/api/v1/voice/tts`. ClawPay returns `{url}` to a short-lived MP3, which a Lua `player:play(url)` plays.
   - Why C: Lua has no HTTP client, and `http_request` only takes a string body with no file upload (`cap_http_request.c:689` schema: `url, method, headers, body, save_path, timeout_ms, ...`).
3. Full duplex or realtime (OpenAI Realtime over WebSocket, or the esp-sr wake word): too much for a hackathon on ESP-Claw. If always-on voice is mandatory, Xiaozhi/esp-sr remains the mature stack.

## 2. MCP client

Source: `components/claw_capabilities/cap_mcp_client/src/cap_mcp_client.c`, `cap_mcp_client_core.c`; SDK `esp-iot-solution/components/mcp-c-sdk/src/transports/esp_mcp_http_client.c`.

- **LLM-facing tools:** `mcp_list_tools {server_url, endpoint?, cursor?}`, `mcp_call_tool {server_url, tool_name, endpoint?, arguments?}`, `mcp_discover` (mDNS `_mcp._tcp`, LAN only). The group is not LLM-visible by default. Enable it via a skill with `metadata.cap_groups: ["cap_mcp_client"]` or via the `llm_visible_cap_groups` config. The docs link a `cap_mcp_client/skills/.../SKILL.md` that **does not exist** in the repo, so you must write your own.
- **Transport:** `esp_mcp_transport_http_client` only, which is Streamable HTTP. Each POST sends `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version`, and echoes `MCP-Session-Id` (SDK lines ~1378-1404, 1516). The default protocol is `"2025-11-25"` (SDK line 26). The SDK has a GET SSE stream (`open_stream`), but only `esp_mcp_mgr_open_stream()` opens it, and `cap_mcp_client` never calls that. No WebSocket and no stdio.
- **URL composition:** `server_url` + `/` + `endpoint`, where `endpoint` defaults to `"mcp"` (`MCP_MDNS_DEFAULT_ENDPOINT`; SDK `esp_mcp_http_client_build_url`). For example `server_url=https://clawpay.example.com`, `endpoint=mcp` gives `https://clawpay.example.com/mcp`.
- **TLS:** the SDK forces `out->crt_bundle_attach = esp_crt_bundle_attach` (`esp_mcp_http_client.c:1294`), so any public-CA cert works. Self-signed certs would need `cert_pem`, which `cap_mcp_client` does not expose. Use a real cert (ngrok, Cloudflare Tunnel, Fly/Render).
- **Auth:**
  - The SDK supports a static bearer token (`esp_mcp_mgr_set_bearer_token`, `esp_mcp_mgr.h:633`) and OAuth discovery with a 401 callback (`esp_mcp_mgr_set_auth_callback`).
  - `cap_mcp_mgr_create()` (`cap_mcp_client_core.c`) sets neither, so **no Authorization header is ever sent**. It also only passes `url/timeout_ms/buffer_size/keep_alive_enable`.
  - Options: (a) patch `cap_mcp_mgr_create` to call `esp_mcp_mgr_set_bearer_token(*mgr_out, <token from NVS>)` after `esp_mcp_mgr_init`; (b) for the demo, accept an unguessable path token, e.g. `endpoint = "mcp/<device_token>"`, but that leaks into LLM context and logs.
- **Limits:**
  - `CAP_MCP_HTTP_TIMEOUT_MS 20000`, HTTP buffer 4096, `server_url` ≤ 255 chars, `endpoint` ≤ 63, `tool_name` ≤ 127, `cursor` ≤ 127.
  - Capability output buffer is 32 KB (`CLAW_CAP_CORE_OUTPUT_SIZE`, `claw_cap.c:31`).
  - The result handler concatenates only `content[]` items with `type:"text"` and ignores `structuredContent`. `isError` with empty text becomes "Error: Tool returned application error".
  - Every call does `init → start → register_endpoint → initialize → call → destroy`, so expect a full TLS handshake plus 2 round trips per tool call (~1–3 s on ESP32-S3).
  - ClawPay must accept requests without prior session state, or issue a new `Mcp-Session-Id` on every `initialize`.
- **Configuration:** no config file, Kconfig or web field lists servers. The URL comes from the LLM's tool arguments. Put it in a SKILL.md under `fatfs_image/storage/skills/clawpay/` (or upload it via the Web Console file manager in Admin Mode) or in the memory files (`identity.md`/`user.md`).

## 3. MCP server mode

- `cap_mcp_server` (`components/claw_capabilities/cap_mcp_server/`) is HTTP on port 18791 with endpoint `mcp`, mDNS-advertised, using the `esp_http_server` transport. The SDK's server auth is off by default (`MCP_HTTP_AUTH_ENABLE n`; token default `"mcp-dev-token"`).
- **`edge_agent` never calls `cap_mcp_server_init/start`.** A grep finds it only in `application/mcp_server_point/main/main.c:207`. That trimmed app disables `claw_core` (no LLM) and the MCP client, and exposes only `lua.run_script`, `lua.run_script_async`, `lua.list_async_jobs`, `lua.get_async_job`, `lua.stop_async_job`, `lua.stop_all_async_jobs` (`cap_mcp_lua.c`).
- **Conclusion:** ClawPay cannot reach the device from the internet anyway (NAT, no TLS, no auth). Do not rely on ClawPay → device calls. Have the device pull pending approval requests: the approval C task polls or long-polls `GET /api/v1/devices/{device_id}/pending-approvals`, or the agent calls `create_order` and the order summary comes back in the tool result.

## 4. Boards (ESP32-S3)

Requirements: "at least 8MB Flash and 8MB PSRAM" (`docs/.../tutorial/supported-list.mdx`, `bom.mdx`). Board defaults use `CONFIG_SPIRAM_MODE_OCT=y` (octal PSRAM), and partition tables exist for 8, 16 and 32 MB.

| Board dir | Audio | Display | Touch | Notes |
|---|---|---|---|---|
| espressif/esp32_S3_DevKitC_1 | – | – | – | LED strip only; 8MB/octal PSRAM |
| espressif/esp32_S3_DevKitC_1_breadboard | USB-UAC + PDM out | ST7789 SPI 320x240 | – | the "official" DIY build; web-flasher supports this display |
| espressif/esp32_S3_DevKitC_1_N32R16V_breadboard | same | same | – | 32MB flash |
| espressif/esp_box_3 | codec | LCD | yes | best all-in-one for a demo |
| espressif/esp_vocat_board_v1_2, esp_sparkbot | codec | LCD | vocat: yes | |
| m5stack/m5stack_cores3 / m5stack_sticks3 | codec | LCD | CoreS3: yes | |
| waveshare/ESP32_S3_Touch_AMOLED_2_16 | codec | AMOLED | yes | |
| waveshare/ESP32_S3_RLCD_4_2 | codec | (custom) | – | |
| waveshare/esp32_s3_geek | – | LCD | – | |
| waveshare/esp32_s3_zero_n8r8 | – | – | – | LED strip |
| movecall/moji, cuican; dfrobot/k10 | codec | LCD | – | |
| lilygo/t_display_s3 | – | custom | – | |
| seeedstudio/xiao_esp32s3_sense | mic | – | – | camera |
| dfrobot firebeetle_2_S3 / romeo_S3, lceda eda_* | varies | varies | – | |

All of these have a BOOT button (GPIO0) usable for "hold to approve".

**What to check on your board:**
1. Exact module marking, e.g. `ESP32-S3-WROOM-1-N8R8` / `N16R8`. You need R8 (8 MB octal PSRAM) and N8 or more. Run `esptool.py --chip esp32s3 flash_id` and look for "Embedded PSRAM 8MB" in the boot log.
2. Whether it is one of the listed boards. If not, use the `esp32_S3_DevKitC_1` profile (works on any N8R8 module).
3. Whether it has a mic (I2S MEMS like INMP441, or PDM), a speaker amp (MAX98357A over I2S), and a screen. If not, the DevKitC breadboard profile expects an ST7789 SPI LCD (CS15, DC7, RST6, MOSI5, SCLK4, BL16) and USB-UAC audio. Any other wiring needs a custom board yaml (ESP Board Manager).
4. Avoid "U" variants (external antenna) per `bom.mdx`. Wi-Fi is 2.4 GHz only.

## 5. LLM configuration

- Fields: `llm_api_key, llm_backend_type (openai|anthropic), llm_endpoint_provider, llm_model, llm_base_url, llm_auth_type, llm_timeout_ms (default 120000), llm_max_tokens, llm_supports_tools, ...` (`app_claw.h`; `application/edge_agent/components/app_config/app_config.c:39,78`).
- Storage: menuconfig defaults are overridden by **plaintext** NVS namespace `app` (`docs/.../reference-project/configuration.mdx`). They are edited in the Web Console's LLM Settings, which needs a restart.
- Custom endpoint: "custom LLM providers" take a Base URL without the `/chat/completions` suffix (`docs/.../tutorial/web-config.mdx`). The auth styles are `bearer`, `api-key`, `none` (`claw_endpoint.h:26-28`).
  - So set Base URL `https://<clawpay>/llm/v1`, auth `bearer`, key = per-device ClawPay token.
  - ClawPay then forwards to OpenAI with the real key, enforces model, rate and spend limits, and can strip or deny dangerous tools.
- Advanced: `claw_endpoint_register_provider()` (`claw_endpoint.h`) lets a C component supply URL and headers per request (up to 4 headers) with refresh on 401. You could use it to fetch short-lived LLM tokens from ClawPay with the device key.
- The model needs strong tool use. The README recommends gpt-5.4 / claude-4.6-sonnet class models.

## 6. Extensibility and the signing patch

**Adding C code:**
- Write a component with `claw_cap_descriptor_t[]` + `claw_cap_group_t` + `claw_cap_register_group()` (`docs/.../reference-cap/implement-capability.mdx`). Wire it with `app_capabilities_register_external_group(&(app_capability_external_group_t){group_id, display_name, llm_visible_by_default, prepare, reg})` (`components/common/app_claw/include/app_capabilities.h:29-44`). Ship a `skills/<name>/SKILL.md` with `metadata.cap_groups`.
- Flags: `CLAW_CAP_FLAG_CALLABLE_BY_LLM`, `EMITS_EVENTS`, `SUPPORTS_LIFECYCLE`, `RESTRICTED`, `ROOT_AGENT_ONLY` (`claw_cap.h:33-37`).

**Lua:** dynamic scripts, with LLM-authored scripts run via `cap_lua`. Modules include `button` (`button.new(gpio)`, `on(...,"long_press_start")`), `display` (immediate-mode 2D drawing), `lvgl`, `gpio`, `audio`, `capability`, `event_publisher`.

**Why the approval cannot be Lua or a plain capability (critical):**
- `lua_module_capability.c:254` sets `ctx->caller = CLAW_CAP_CALLER_SYSTEM`. `claw_cap_call()` (`claw_cap.c` ~1870) only enforces `claw_cap_authorize_llm_tool_locked` (the `CALLABLE_BY_LLM` check) when the caller is an agent. **LLM-written Lua can call any registered capability.**
- `event_publisher.publish/publish_trigger` lets Lua forge arbitrary events, including fake "button" events.
- `lua_driver_gpio` lets Lua reconfigure pins. An approval pin switched to output could read back as "pressed".

**Required design (C task `clawpay_approver`):**
1. Do not register the signing function as a capability. The only LLM-callable entry is `clawpay_request_approval {order_id}`. All it does is fetch the authoritative order from ClawPay (`GET /api/v1/orders/{id}/approval-challenge`, which returns the canonical payload and nonce) and queue it to the C task. Never let it accept the amount or merchant from the LLM.
2. The C task owns the display (or draws over it) and shows merchant, amount, currency and expiry. It configures the approval GPIO as input with pull-up at that moment (`gpio_config`, `gpio_set_direction(INPUT)`), then requires a continuous low level for ≥ 1.5–2 s, polled directly with `gpio_get_level` in C.
3. On success it signs `SHA-256(canonical_bytes)` with ECDSA P-256. Use mbedTLS `mbedtls_pk_sign` or PSA `psa_sign_message(PSA_ALG_ECDSA(PSA_ALG_SHA_256))`, both available in IDF 5.5. Then POST `{signature_b64, key_version, device_id, nonce}` to `/api/v1/orders/{id}/gadget-approval` itself. The LLM only learns "approved/declined".
4. Disable `lua_driver_gpio` (or at least avoid the approval pin) and `lua_module_event_publisher` via `enabled_lua_modules`, or Kconfig `CONFIG_APP_CLAW_LUA_MODULE_*`. Don't use the shared Lua `button` module for the approval pin.

**Crypto and key storage on ESP32-S3:**
- ECDSA P-256 is in mbedTLS (software, ~100–200 ms per sign on S3, fine). Ed25519 is not in mbedTLS 3.x; use `espressif/libsodium` if needed. **Use P-256**: Python `cryptography` verifies it trivially.
- Pick one encoding and document it: DER signature, or raw r‖s 64 bytes, base64url.
- Hardware: the S3 has no ECDSA peripheral (that is C6/H2/P4/C5). Its DS peripheral is RSA-only, with keys wrapped by an eFuse HMAC key. That is a stretch goal and needs `esp_secure_cert_mgr` provisioning.
- Hackathon storage: generate the key on first boot in a separate NVS namespace `clawpay` (not `app`, which the Web Console dumps), and print or upload the public key once for registration.
- Optional hardening: NVS encryption with the HMAC scheme (`CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC`) and/or flash encryption. **Both permanently burn eFuses (irreversible). Don't do this on your only board during a hackathon.**
- Also, the Web Console's file manager can download "almost all content in the filesystem", so never put the key on FAT.

**Display and buttons:** the board profiles expose a display through `display_service` / Lua `display` and LVGL. Your C task can use `esp_lcd` through `display_service` (`components/common/display_service/`). Check its arbitration API so Lua jobs can't overdraw the approval screen during the hold window.

## 7. Build and flash on Windows

- **Web flasher:** https://esp-claw.com/en/flash/ (README) runs in Chrome/Edge over WebSerial and configures in the browser. Use it for day-1 bring-up on a stock board profile. Custom C components need a local build.
- **Local build:**
  - ESP-IDF **v5.5.4** (`docs/.../reference-project/build-from-source.mdx`; CI `espressif/idf:release-v5.5` in `.gitlab/ci/build.yml`). Then `pip install esp-bmgr-assist`.
  - Steps: `cd application/edge_agent`, `idf.py bmgr -c ./boards -b <board>`, `idf.py build flash monitor`. Boards are selected by the board manager, so no `set-target` is needed.
- **Windows specifics:**
  - No bash or shell scripts in the build (grep found none). It is CMake + Python, so the native ESP-IDF Windows installer or EIM should work. The repo docs don't mention Windows or WSL at all, and I did not build this.
  - Risk: `application/edge_agent/tools/cmake/esp_idf_patch.cmake` edits your ESP-IDF install in place: `esp_lcd_panel_io_parl.c`, `usb_types_ch9.h`, `esp_http_client.c/.h` (adds `esp_http_client_set_event_handler`). It uses exact multi-line string matching and `EDGE_AGENT_STRICT_IDF_PATCH=ON` makes a mismatch fatal. If git `core.autocrlf` gives the IDF tree and the esp-claw tree different line endings, configure fails.
  - Mitigations: use a dedicated IDF v5.5.4 install with consistent line endings, or build in WSL2 / the `espressif/idf:release-v5.5` Docker image. Flash from Windows with `esptool` or the web flasher. Keep the path short and without spaces. Your workspace path has spaces, so clone esp-claw to e.g. `C:\esp\esp-claw`.

## 8. Maturity and risks

- License Apache-2.0 (`LICENSE`). Actively developed: many dated CHANGELOG entries through 2026-06; breaking rename of persistence APIs on 2026-05-19. Pin a commit.
- The vendor's security warning: `edge_agent` "is not intended for production". The Web Console is unauthenticated HTTP on the LAN, returns nearly everything including NVS secrets, and the SoftAP has full privileges (`web-config.mdx`; GitHub issue #41). For the demo: set a SoftAP password, enable SoftAP auto-disable, and keep the device on a private network.
- Not in the Espressif bug bounty (README).
- Docs gaps: the MCP client SKILL.md is referenced but missing, and the MCP server doc says "SSE transport" while the code uses the Streamable HTTP server.
- Self-programming needs a strong tool-use model. Weak models will mis-call `mcp_call_tool`, and the server_url must be spelled out in the skill.
- IM fallbacks: Telegram (`cap_im_tg`: text, photo, document; **no voice notes**), QQ, Feishu (not Lark international), WeChat, and local Web Chat (`cap_im_local`). Telegram works from Malaysia and is the simplest fallback.

---

## Recommendation

**Go for ESP-Claw** as the device agent, provided that: (a) the board is N8R8 or larger; (b) the demo uses text input via Telegram or Web Chat as the guaranteed path, with push-to-talk voice as a stretch; (c) approval is a C task, not Lua.

### Integration contract ClawPay should implement

1. **MCP server:** `POST https://<clawpay-host>/mcp`, Streamable HTTP, stateless.
   - Accept `initialize`, `notifications/initialized`, `tools/list` and `tools/call`.
   - Return `application/json` (single JSON-RPC response; SSE optional). Return 405 for GET `/mcp`.
   - Tolerate a fresh `initialize` before every call, and either omit `Mcp-Session-Id` or issue a new one each time. Echo `protocolVersion` `2025-11-25` (or the requested one).
   - Valid public TLS certificate.
2. **Auth:** `Authorization: Bearer <device_token>` (requires the cap_mcp_client patch). Temporary fallback: `/mcp/<device_token>` path. Map the token to `device_id`/user. Never accept `device_id` from tool arguments.
3. **Tool results:** always `content: [{type:"text", text:"<compact JSON string>"}]`, under ~4 KB, with `isError: true` plus text for failures. Keep each tool's description short, and tell the LLM in the description what to say to the user. Tools:
   - `search_products {query, max_results?}` → `[{sku, name, merchant_id, price, currency}]`
   - `create_order {merchant_id, items:[{sku, qty}], currency?}` → `{order_id, merchant, total, currency (MYR, fallback USD), expires_at, status:"awaiting_device_approval"}`. This only creates a PayPal order or quote and **never captures**.
   - `get_order_status {order_id}` → `{status, paypal_status, approved_at?}`
   - `get_spending_summary {period}` → totals
   - **No approve or capture tool is exposed over MCP.**
4. **Device endpoints (not MCP; called by firmware C code with the device token):**
   - `GET /api/v1/orders/{id}/approval-challenge` → `{canonical: "<exact bytes to sign>", nonce, expires_at, key_version}`
   - `POST /api/v1/orders/{id}/gadget-approval {device_id, key_version, nonce, signature (base64url DER ECDSA-P256-SHA256)}` → ClawPay verifies against the registered public key, checks nonce single-use and expiry, then captures via PayPal.
   - `POST /api/v1/devices/register {device_id, public_key_pem, key_version}`, admin-gated, one time.
   - Optional: `GET /api/v1/devices/{id}/pending-approvals` (long-poll).
   - Optional voice: `POST /api/v1/voice/stt` (audio/wav → `{text}`), `POST /api/v1/voice/tts {text}` → `{url}` (short-lived MP3 at 16 kHz or similar).
   - Optional LLM proxy: `POST /llm/v1/chat/completions` (OpenAI-compatible passthrough with server-held key).
5. **Canonical payload:** define it server-side and send the exact bytes in the challenge, so firmware signs the bytes it is given and also renders the parsed fields. Use fixed key order with no whitespace:
   `{"amount":"12.50","currency":"MYR","device_id":"...","expiry":1767225600,"key_version":1,"nonce":"...","order_id":"..."}`
   The device must parse and display amount, currency and merchant from these same bytes before signing.

### Firmware tasks (ESP-Claw fork)

1. Pick and flash a board profile via the web flasher. Confirm Wi-Fi, LLM and Web Chat work.
2. Patch `cap_mcp_client_core.c` `cap_mcp_mgr_create()` to set a bearer token from NVS (`esp_mcp_mgr_set_bearer_token`). Add an NVS key and web field, or hard-code it via Kconfig for the demo.
3. Add `skills/clawpay/SKILL.md` with `cap_groups: ["cap_mcp_client", "cap_clawpay"]`, the exact `server_url`/`endpoint`, the tool list, and the rule that the agent must call `clawpay_request_approval` after `create_order` and must never claim payment succeeded without `get_order_status`.
4. New C component `cap_clawpay`:
   - Key generation and storage in NVS namespace `clawpay`, plus a console command to print the public key.
   - LLM-callable `clawpay_request_approval {order_id}`.
   - Private approver task: fetch challenge → render on display → direct-GPIO hold-to-approve with timeout → mbedTLS ECDSA-P256 sign → POST gadget-approval → return the result text.
   - Register via `app_capabilities_register_external_group`.
5. Lock down the Lua attack surface: disable `lua_driver_gpio` and `lua_module_event_publisher` (or exclude the approval pin), set a SoftAP password and auto-disable.
6. Stretch: `cap_clawpay_voice` push-to-talk (audio_capture → STT upload → inject message; TTS URL playback).
7. Local build environment: IDF v5.5.4 (WSL2/Docker if the Windows patch step fails), esp-bmgr-assist, clone to a path without spaces.

### Middleware tasks (ClawPay backend)

1. MCP Streamable-HTTP endpoint `/mcp` (stateless, JSON responses, bearer auth → device lookup) with the four tools above, returning text-JSON content.
2. Device registry table (device_id, public_key, key_version, owner, status) and token issuance.
3. Approval challenge plus signature verification (`cryptography` ECDSA P-256, single-use nonce, expiry, amount/currency matching the stored order), then PayPal capture (MYR sandbox, USD fallback).
4. Order state machine: `created → awaiting_device_approval → approved → captured / failed / expired`. Never let `create_order` capture.
5. Optional: OpenAI-compatible LLM proxy (keeps the OpenAI key off the device; add per-device rate and spend limits).
6. Optional: STT/TTS endpoints for push-to-talk voice.
7. Public HTTPS exposure for the demo (Cloudflare Tunnel / ngrok / Render) with a CA-signed cert.
8. Update `doc/implementation-plan.md` to replace the Xiaozhi assumptions with this contract.
