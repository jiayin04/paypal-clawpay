# Verification guide

Run checks from the repository root unless a command changes directories. Default automated tests use fake providers and do not spend money or require hosted credentials.

## Backend

Use Python 3.12 and install the locked dependencies first:

```sh
cd backend
uv sync --locked --python 3.12
uv run --offline pytest
```

The suite covers the HTTP/MCP contracts, quote validation, spending reservations, device approvals, passports, payment classification, idempotency, and reconciliation. The PayPal live smoke test is explicit opt-in; passing the default suite does not verify real provider credentials or perform a payment.

Supabase pgTAP suites are under `supabase/tests/database/`. Run them against an operator-controlled test database after applying incremental migrations. Static SQL tests do not prove database permissions or concurrency behavior.

## Frontend

Use Node 24+:

```sh
cd frontend
npm ci
npm test
npm run build:pages
```

`npm test` verifies Hono authorization forwarding, endpoint allowlists, method restrictions, upstream failures, and SPA serving. `build:pages` runs TypeScript, Vite, and the complete Hono Worker packaging for Cloudflare Pages.

With `npm start` running in another terminal and Playwright Chromium installed:

```sh
cd frontend
npm run test:ui
```

Browser checks cover responsive screens, order traces, demo replays, search/filter states, and authentication boundaries using controlled fixtures. Screenshots go to the ignored `frontend/test-results/ui/` directory. These checks do not verify a real owner's credentials or hosted backend availability.

## Watch

For portable audio and delivery checks:

```sh
swiftc mobile/watch/ClawPayWatch/VoiceAudio.swift mobile/watch/ClawPayWatch/DemoDelivery.swift mobile/watch/Tests/VoiceAudioChecks.swift -o /tmp/clawpay-voice-checks
/tmp/clawpay-voice-checks
```

Build and run `mobile/watch/ClawPayWatch.xcodeproj` with an installed watchOS simulator runtime. See the [Watch guide](../../mobile/watch/README.md) for simulator fixtures and microphone/Siri checks. Software simulator keys do not validate Secure Enclave presence enforcement. A physical Watch is needed to validate wrist unlock, microphone behavior, and Siri invocation.

## Hosted behavior and live validation

Check the landing page, `/dashboard`, asset delivery, and `/api/config` at https://clawpay.pages.dev. The current Pages deployment has `liveAvailable: false` and offers the sample demo. A successful frontend build does not connect the pending VPS deployment.

A connected end-to-end test needs a reachable HTTPS API, applied migrations, a paired test device, an owner account, a linked Sandbox wallet, and configured speech/catalog/model providers. Verify the order's provider-confirmed outcome and spending records, including retry/reconciliation behavior; do not treat the dashboard replay or Watch visual fixtures as payment evidence. See [remaining work](remaining-work.md) and [deployment](../../deploy/README.md).

Earlier dated implementation evidence is retained in the [archive](archive/verification-2026-11.md).
