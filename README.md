# ClawPay

[Website](https://clawpay.pages.dev/)

ClawPay is a watch-first hackathon prototype for agent-assisted shopping and PayPal Sandbox payments. A user speaks to the watch or ESP-Claw device, the backend agent builds an order from merchant agents, and payment happens only after a signed approval on the device.

| Folder | What it is | Stack | Guide |
| --- | --- | --- | --- |
| [`backend/`](backend/README.md) | API, MCP server, merchant and PayPal agents, database migrations | Python 3.12, FastAPI, Supabase | [backend/README.md](backend/README.md) |
| [`frontend/`](frontend/README.md) | Read-only owner and judge dashboard | Vite, React, Hono | [frontend/README.md](frontend/README.md) |
| [`mobile/watch/`](mobile/watch/README.md) | Apple Watch client with voice and payment approval | SwiftUI, watchOS | [mobile/watch/README.md](mobile/watch/README.md) |

The ESP-Claw firmware is not in this repository; its setup is in [backend/docs/setup.md](backend/docs/setup.md).

```text
watch / ESP-Claw device --> backend --> merchant agents (mock catalogs, Channel3)
                               |   \--> PayPal agent or PayPal Sandbox
dashboard ---- read-only ------/   \--> Supabase
```

## Agent passports

Each agent device can carry a signed, revocable passport from its owner: which shops it may order from, the largest single order, and an expiry. The backend refuses any order outside it and keeps a signed receipt of every allowed or refused decision, which the dashboard's Devices page shows. Anyone can verify passports and receipts with the public key at `/.well-known/agent-passport`. Setup, API and limits: [backend/README.md](backend/README.md#agent-passports-lite).

## Run locally

Start the backend first. The dashboard reads its settings from `backend/.env`.

```bash
cd backend
uv sync --python 3.12
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

In a second terminal (Node 24+), then open http://127.0.0.1:8787. Demo mode works straight away; owner sign-in shows live activity.

```bash
cd frontend
npm ci
npm run build
npm start
```

For the watch, open `mobile/watch/ClawPayWatch.xcodeproj` in Xcode and follow [its guide](mobile/watch/README.md). It covers the offline demo backend, simulator setup and product receipts.

Secrets stay in `backend/.env` (never committed). Copy `backend/.env.example` to start.

## More

- [Hackathon deployment](deploy/README.md)
- [Setup, including the device and tunnel](backend/docs/setup.md)
- [API contracts](backend/docs/contracts.md)
- [Code layout](backend/docs/architecture.md)
- [Hackathon demo](backend/docs/hackathon-demo.md)
- [Product notes](PRODUCT.md) and [dashboard design system](DESIGN.md)

## License

[MIT](LICENSE) — Copyright (c) 2026 ClawPay contributors.
