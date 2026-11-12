# Remaining validation and deployment work

The repository contains the Watch voice flow, dashboard, agent passports, signed device approvals, and PayPal Sandbox orchestration. Automated tests use fakes. Keep implementation status separate from validation against external services and physical hardware.

## Before claiming a hosted end-to-end payment

- **Reachable backend.** Restore VPS SSH access and deploy the API and PayPal agent with a stable HTTPS origin. The current public Pages site is configured for demo mode. See the [deployment guide](../../deploy/README.md).
- **Incremental database migrations.** Inspect hosted migration history and apply missing migrations, including `20261112090000_agent_passports.sql` and `20261112090100_atomic_passport_orders.sql`. Do not run the standalone rebuild schema over a populated database. Run pgTAP/RLS and concurrent reservation checks on a test database.
- **Owner and device access.** Pair a dedicated test Watch, confirm its signing key, link its owner's Sandbox wallet, and verify two-user isolation. Keep the existing token pepper and signing key stable.
- **Provider integration.** Validate the configured model/tool loop, Fish Audio STT and streamed TTS, and Channel3 catalog/price refresh using dedicated test requests.
- **Sandbox payment outcomes.** Check completion, decline, uncertain timeout/reconciliation, cancellation, and refunds. Confirm provider currency support, request-id retention, and duplicate-invoice behavior before increasing replay windows. The opt-in smoke test alone does not perform a full payment.
- **Frontend live mode.** Only after the API is reachable, configure the Pages runtime API origin and public Supabase settings, redeploy, verify `/api/config`, and sign in as the paired device's owner.
- **Webhook delivery, if enabled.** Verify authentic, duplicate, and out-of-order notifications against actual Sandbox payloads. Keep polling reconciliation enabled when webhooks are unavailable.

## Hardware validation

The simulator is the submission's initial Watch route. Physical-watch microphone accuracy, Siri invocation, location permissions, and Secure Enclave presence checks require a real device. ESP-Claw signing firmware is a separate project; the stock firmware does not implement the required payment-signing component.

## Deliberate product boundaries

- PayPal is Sandbox-only; production configuration is rejected.
- Shipping/address progress is simulated locally on the Watch; no Channel3 retailer checkout or carrier booking is connected.
- Signed user-intent budgets and full AP2 interoperability are future work. Spoken budget text is not itself a signed spending policy.
- Person-to-person transfers, scheduled autonomous purchasing, and phone approval above the configured threshold are not implemented.

## Before wider use

Complete a payment/security review and live SQL validation. Move process-local rate limits, LLM budgets, and reconciliation throttles into shared storage before using multiple API workers. Define operational ownership for uncertain payments, exhausted attempts, provider incidents, and manual recovery.

Use the [verification guide](verification.md) for repeatable local checks and the [architecture guide](architecture.md) for contribution rules. Historical design decisions remain in the [archived implementation plan](archive/implementation-plan.md#19-decisions-log).
