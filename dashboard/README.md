# ClawPay dashboard

Vite + React + TypeScript SPA with a read-only Hono API proxy. Includes a responsive overview, order timelines and agent messages, audit feed, conversation events, device/wallet status, and three explicit simulation replays.

## Run locally

Use Node 24+ and start the existing Python backend on port 8000.

```sh
cd dashboard
npm ci
npm run build
npm start
```

Open http://127.0.0.1:8787. The production Hono server serves the Vite assets and proxies only the allowlisted dashboard GET endpoints.

For frontend development, run `npm run dev` and `npm run dev:web` in separate terminals, then open http://127.0.0.1:5173. Vite forwards `/api` to Hono.

The Hono process reads `../.env` with Node's env loader. Reuse `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (legacy `SUPABASE_ANON_KEY` also accepted). It ignores service-role, PayPal, DeepSeek, watch tokens and signing keys. Optional process env:

```text
CLAWPAY_API_ORIGIN=http://127.0.0.1:8000
DASHBOARD_PORT=8787
```

Browser Supabase sessions use sessionStorage and refresh automatically. Sign in with the Supabase email/password account whose user ID owns the paired watch. Creating an unrelated account gives an empty workspace by design. No admin token or service-role key can be used to sign in. Accounts using another auth method need that account's email/password configured in Supabase first; the dashboard does not reset passwords.

Live mode polls every 15 seconds while the tab is visible, reading the latest 50 orders, conversations and audit events. Order traces fetch up to 100 events/messages. This is a bounded recent snapshot, not an all-time analytics report. Summary spend comes from the backend's spending day/timezone; timestamps display in the browser's timezone. Failures are visible and never switch to sample data.

## Demo lab

All fixture data is labeled synthetic. Voice to checkout, budget protection, and uncertain-payment reconciliation are step-by-step simulations, never agent calls or payments. Live device enrollment, wallet changes and payment approval stay in the existing flows.

## Hosted preview

`npm run build` also produces `dist/server/index.js`, an ESM Hono Worker containing only built public assets. `.openai/hosting.json` stores the private Sites project identity. There are no secrets in the artifact. Runtime settings for hosted live mode are `CLAWPAY_API_ORIGIN` (stable HTTPS backend), `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`. Without these, the hosted site offers demo mode and explains that live mode is not configured. No local loopback backend is reachable from hosting.

## Verify

```sh
npm test
npm run build
# With npm start already running, and Playwright Chromium installed:
npm run test:ui
```

Proxy tests verify owner auth forwarding, endpoint allowlists, method restrictions, no-store responses and safe upstream failures. Browser checks cover five screens at five viewport widths, order traces, search/filter empty states, three scenario replays, mocked owner sign-in, live empty/error/retry states, account switching with failed reads, and sign-out broadcasts. They don't prove a real owner's credential or hosted backend availability. Screenshots are saved under `../.impeccable/review/`.
