# Merge review, 8 November 2026

Scope: `feature/watch-voice-assistant` against `origin/main`, including the pending voice, receipt, delivery-address and dashboard work. This review does not implement the signed task-policy proposal from the team slide deck.

## Findings fixed

- **P1: Dashboard data survived an authentication boundary.** Supabase sign-out broadcasts cleared the session but left live mode and retained owner data visible. Switching owners could also reuse previously loaded resources if the new reads failed. Authentication changes now invalidate pending snapshot reads, clear owner data and open details, and return signed-out sessions to the labeled demo. Browser regression checks exercise owner switching with failed reads and a sign-out broadcast.
- **P2: Watch requests could update another connection's screen.** Pending assistant, receipt and approval responses could arrive after editing the server URL or device token. Connection changes now clear private state and stop voice capture/playback. Every asynchronous result and progress callback checks that its client still matches the current connection. An outdated receipt error also cannot clear a newer order reference.
- **P2: Failed token migration could lose the original credential.** The previous migration removed the UserDefaults token even when the Keychain write failed. It now removes the old value only after a successful write, and Settings reports token-write failures.

## Validation

- `uv run --offline pytest -q`: passed with one opt-in live-provider test skipped and an existing Starlette/httpx deprecation warning.
- Dashboard proxy tests, TypeScript/Vite/Worker build and Playwright browser checks: passed. Browser checks cover five screens at five widths, traces, search/filter, simulation replays, authentication boundaries and live error/retry states.
- `npm --prefix dashboard audit --omit=dev --audit-level=high`: zero vulnerabilities reported.
- Portable Swift voice, PCM, delivery, address and location-fix checks: passed.
- watchOS simulator Debug and unsigned device Release builds: passed without warnings.
- `git diff --check`: passed.

External speech/catalog/payment providers are mocked in automated tests. This review did not perform a charge, test physical-watch microphone/GPS/Secure Enclave behavior, or publish a dashboard deployment. Retailer fulfillment remains simulated. Authentication tests use synthetic accounts.

The unrelated image and generated presentation remain local and outside the code merge.
