# Remaining work

Backend phases 0–4 are implemented and covered by mocked tests. The work below is still required for a real device demo or deployment; the code refactor does not verify external services or hardware.

## Before the real demo, in order

- [ ] **Database:** since the tables were dropped, apply [schema.sql](../supabase/schema.sql), then [seed.sql](../supabase/seed.sql) using the [rebuild guide](database-rebuild.md). Reconcile hosted migration history afterward. For an intact database, use incremental migrations instead. Run the pgTAP suites against an operator-controlled database, including RLS/column grants, concurrency and exactly-once accounting. Static SQL tests are not database execution. Follow [setup](setup.md#a3-apply-migrations-to-the-hosted-supabase-project--works-now).
- [ ] **Credentials and Supabase Auth:** configure server-only secrets, publishable key, pepper and admin token. Check readiness, real JWT/JWKS (or configured HS256) and two-user row isolation on the hosted project. No real secrets belong on the device except its scoped tokens.
- [ ] **PayPal Sandbox:** validate MYR with the Malaysian accounts, setup/vault-without-purchase, masked-wallet display and merchant-initiated charging after approval. Verify completion, decline, pending/timeout recovery and refunds. If MYR is unavailable, use USD consistently. The default live smoke only tests OAuth/setup, not a payment.
- [ ] **Idempotency assumptions:** confirm PayPal Request-Id retention and duplicate-invoice enforcement for this Sandbox merchant before increasing the 900-second replay window. Verify webhook REVERSED resource shapes and payer-email availability against actual deliveries.
- [ ] **ESP-Claw firmware:** confirm the board/flash/PSRAM, implement/build the separate MCP bearer patch and private `cap_clawpay` C task, generate/register the device P-256 key, store `cpd_` privately and configure `cpl_` for the proxy. The stock web-flashed image does not provide the payment-signing component.
- [ ] **Complete device test:** run the backend with one worker, open the HTTPS tunnel, update `PUBLIC_BASE_URL`/device URLs, link your own Sandbox wallet, search/create an order ≤RM100, hold the physical button to sign and confirm payment plus spending/dashboard history. Test a lost connection, replay, revocation, expiry, cap rejection and uncertain-payment recovery.
- [ ] **LLM provider:** configure the upstream base/key/model and exercise the real ESP-Claw request pattern, tool calls, streaming modes, model compatibility, usage reporting and budget rejection. Remove any direct upstream key from the device.
- [ ] **Optional stable webhook URL:** configure the Sandbox webhook id/events and test authentic, duplicate and out-of-order deliveries. Quick tunnels can use polling alone.

## Before a wider or production deployment

- [ ] Perform a payment/security review and live SQL validation. The current scope is **Sandbox only**; production PayPal is rejected by configuration.
- [ ] Define operational ownership for exhausted/uncertain payments, reconciliation backlog, provider incidents and manual resolution. No admin-resolution API exists today.
- [ ] Put rate limits, daily LLM budgets and reconciliation throttles in shared storage before using multiple workers. Current limits reset on restart and concurrent requests can overshoot completed-usage accounting.
- [ ] Add deployment-specific ingress controls, readiness protection, monitoring/alerts, backup/restore validation and secret rotation procedures.
- [ ] Add the team's chosen CI, formatter/linter and static type-check gates with pinned tooling. No new tooling packages or provider-specific CI were installed during this cleanup; offline tests and structural checks run with the existing lockfile.
- [ ] Review, stage and commit the implementation, tracked docs and refactor. No commit or remote publication was made by this task.

## Deferred product scope, not unfinished phase 4 backend work

Dashboard frontend, phone approval for orders above 10000 minor units, voice, external AP2 SDK/sample compatibility and sponsor integrations remain separate scope decisions. The current gadget-cart mandate records the verified signature, and refunds deliberately do not restore daily spending capacity. See [implementation decisions](implementation-plan.md#19-decisions-log).
