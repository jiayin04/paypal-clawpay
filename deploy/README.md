# Hackathon deployment

Use the reviewed `main` branch, including the atomic passport order fix in PR #3. Apply both passport migrations before starting the new backend:

- `20261112090000_agent_passports.sql`
- `20261112090100_atomic_passport_orders.sql`

Apply these incrementally using your normal Supabase migration workflow; never run the generated rebuild schema on an existing populated database.

| Component | Location | Deployment |
| --- | --- | --- |
| Frontend and read-only Hono proxy | Cloudflare Pages | Existing Vite/Hono build in Pages advanced mode, public demo plus owner sign-in |
| FastAPI backend | Existing Ubuntu VPS | One process, image built off the VPS |
| PayPal A2A agent | Same VPS, loopback only | Separate process and provider credentials |
| Database | Existing hosted Supabase | Keep the current data and device identities |
| Watch app | Apple Watch simulator | Xcode installation and recorded demo; no paid membership required |

The Watch is a native app, not a VPS service. LLM, speech and catalog work use remote providers. A screenshot of aggregate CPU/memory does not identify which existing VPS process is busy. Inspect first; do not stop unrelated applications or resize the VPS based only on that screenshot.

## Current rollout status (12 November 2026)

- Public frontend: https://clawpay.pages.dev (demo mode; live backend bindings remain unset). The original Workers URL remains available.
- Backend: amd64 image built and container liveness checked locally. VPS SSH timed out on port 22; no remote services were changed.
- Database: neither passport migration was pushed to the hosted database in this rollout.
- Watch: Debug simulator build passed without signing. Simulator distribution uses the source/Xcode instructions below.

The public dashboard currently demonstrates synthetic activity. It is not evidence that a live Watch-to-PayPal payment has been verified after these changes. To finish live deployment, restore VPS SSH access, select a stable HTTPS API hostname, apply the two incremental passport migrations, and run the final demo checks below. Keep secrets in private files.

## Submission requirements

The [official rules](https://paypalaihackathon.devpost.com/rules) accept complete build/run instructions or a hosted functional demo. Public hosting is not mandatory. Submit a public source repository with an open-source license (this repo uses MIT), a public YouTube demo under three minutes showing the working app on its target platform, and instructions/access that remain available through judging. PayPal Sandbox must be central to the working experience. Clearly identify simulated fulfillment; a synthetic dashboard alone does not demonstrate a working payment flow.

The deadline is **13 November 2026 at 04:00 MYT** (12 November at noon Pacific). For the selected simulator route, provide Xcode build/run steps below and record the working Watch simulator alongside the dashboard. The organizers may request access to specialized hardware; do not claim physical-device testing if only the simulator was tested.

## Check the VPS first

Run these read-only commands through your existing SSH connection:

```sh
uptime
free -h
df -h /
docker stats --no-stream
ps -eo pid,comm,pcpu,pmem --sort=-pcpu | head -15
ss -ltn
docker compose version
```

The supplied setup requires Linux Docker Engine with Compose **2.30+** for raw env files. It binds application ports **18000/18201 to loopback**, uses one API worker because limits and budgets are process-local, caps container memory/CPU and rotates logs. The starting memory ceilings are 512 MiB for the API, 256 MiB for the agent and 128 MiB for optional Caddy. These are limits to validate under load, not measured usage or a capacity guarantee. The VPS still needs spare memory for Docker and its existing services.

## Prepare private runtime files on the Mac

Keep `backend/.env` as the source of existing credentials. In particular, preserve `DEVICE_TOKEN_PEPPER`: changing it invalidates paired-device tokens.

After choosing the real public API hostname:

```sh
cd backend
uv sync --locked
uv run python ../deploy/prepare_env.py --api-url https://api.YOUR-DOMAIN
```

This creates three ignored files with private permissions under `deploy/.secrets/`. `api.env` excludes PayPal client credentials. `paypal.env` contains only the agent's settings. `frontend.json` contains only the API URL and public Supabase settings. Never upload the complete backend `.env` to the frontend or put it in an image. Do not source the generated raw env files in a shell.

Confirm the source config also has the selected LLM endpoint/model/key, `FISH_AUDIO_API_KEY` for microphone STT, and `CHANNEL3_API_KEY` for real catalog search. TTS can fall back to the built-in Watch voice; microphone STT cannot. Keep `PAYPAL_ENV=sandbox`.

## Build the backend off the VPS

From the repository root on a machine running Docker:

```sh
docker buildx build --platform linux/amd64 --load -t clawpay-backend:local backend
docker save clawpay-backend:local | gzip > /tmp/clawpay-backend.tar.gz
```

Use `linux/arm64` instead if the VPS reports `aarch64`. Upload the image archive, the tracked `deploy/` files, and **only `api.env`/`paypal.env`** to the VPS through SSH/SCP. Keep `frontend.json` on the Mac. A private registry with an immutable image tag is an alternative. This avoids a Python/Node dependency build competing with running services on the VPS.

Suggested server layout:

```text
/opt/clawpay/deploy/
  compose.yaml
  Caddyfile
  .env
  .secrets/api.env
  .secrets/paypal.env
```

Protect `.secrets` with mode 700 and its files with mode 600. Copy `deploy/.env.example` to the server's `deploy/.env` and set the image name and API hostname. Check the selected ports are free. Load the image and start the private services:

```sh
gzip -dc /path/to/clawpay-backend.tar.gz | docker load
cd /opt/clawpay/deploy
docker compose config --quiet
docker compose up -d --wait api
docker compose ps
curl --fail http://127.0.0.1:18000/health
curl --fail http://127.0.0.1:18000/health/ready
```

The first endpoint proves liveness. Readiness checks Supabase access, not the whole shopping/payment flow. `docker compose up api` also starts the private agent. There is no local Postgres/Supabase, LLM model, frontend dev server or frontend builder in this stack. Do not run `supabase/schema.sql` on the existing populated database.

## Give the API stable HTTPS

Set a DNS A record for `api.YOUR-DOMAIN` to the VPS and verify it resolves correctly. If using the included Caddy setup, keep that API record DNS-only initially, allow ports 80/443, and check that no existing proxy owns them. Then:

```sh
docker compose --profile ingress up -d ingress
curl --fail https://api.YOUR-DOMAIN/health
```

Caddy obtains a certificate, streams progress/audio without buffering, and blocks public readiness, metrics and API docs. The application and PayPal-agent ports remain private. If another proxy already owns 80/443, configure that existing proxy instead of starting Caddy. Preserve streaming, block the private endpoints, and replace untrusted forwarded-IP headers at the proxy. Set `CLIENT_IP_HEADER=none` if the trusted-forwarding setup differs.

Update the PayPal Sandbox return/cancel URLs to the stable API origin used by the linking flow. A PayPal webhook, if configured, targets `https://api.YOUR-DOMAIN/api/v1/webhooks/paypal`. Verified polling reconciliation remains enabled even without a webhook. Set the generated `PUBLIC_BASE_URL` before creating new linking sessions.

## Publish the frontend

The primary site is https://clawpay.pages.dev, with the dashboard at `/dashboard`. Deploy to the existing Pages project using the pinned Wrangler version:

```sh
cd frontend
npm ci
npm run deploy:pages
```

This builds the complete Hono app as a Pages advanced-mode `_worker.js`, preserving SPA routes, API routing, and security headers. Configuration is in `frontend/pages/wrangler.jsonc`. The Pages project has separate runtime settings from the original Worker. Configure only `CLAWPAY_API_ORIGIN`, `SUPABASE_URL`, and `SUPABASE_PUBLISHABLE_KEY` once the HTTPS backend is reachable, then redeploy. Until then, retain demo mode; verify `/api/config` before claiming live availability. Never copy backend secrets to Pages.

### Original Workers deployment

The project pins Wrangler 4.160.1 as a development dependency. The repo now includes `frontend/wrangler.jsonc`, which builds the existing Hono ESM Worker. It serves both `/` and `/dashboard`; this is not a static-only upload that would lose `/api/*`.

```sh
cd frontend
npm ci
npx wrangler deploy --dry-run
npx wrangler login
npx wrangler deploy
npx wrangler secret bulk ../deploy/.secrets/frontend.json
```

`npx wrangler login` is unnecessary if the CLI is already signed in. Use the returned `workers.dev` URL, or attach an owned custom domain. Until the three runtime bindings are supplied, the Worker truthfully offers demo mode. Verify `/api/config` reports `liveAvailable: true` after configuration. The existing Worker handles assets itself, so its requests count as Worker requests; do not assume the unlimited static-assets pricing applies to this bundle.

Use a dedicated judge Supabase account for live access, with its own paired test device and Sandbox wallet. Do not share an admin/service-role key or your personal Watch's token with judges. Public demo mode uses synthetic records and needs no privileged credentials.

## Distribute the Watch app

For an on-site demo, run `mobile/watch/ClawPayWatch.xcodeproj` in Xcode, use a paired simulator or your physical Watch, and enter the public HTTPS API URL in Settings. Retain the existing Watch device token if using the same hosted database. A new Watch installation has a different signing key and must be enrolled as its own device.

For judges using a Mac with Xcode and a watchOS runtime installed:

```sh
xcodebuild -list -project mobile/watch/ClawPayWatch.xcodeproj
xcrun simctl list devices available
```

Open the project, select the `ClawPayWatch` scheme and an available Apple Watch simulator, then Run. No Apple Developer membership is required for simulator installation. In the app settings, configure the reachable backend URL and enroll the installation as a test device using the backend pairing instructions. Include that setup in the submission; a simulator binary alone is insufficient. Provide a dedicated judge account and Sandbox credentials through the submission's private testing instructions, never the public repository.

Optional later TestFlight distribution requires Apple Developer membership, App Store Connect and signing. Before upload:

1. Select your signing team and register a bundle identifier owned by that team.
2. Add the required AppIcon asset and set the target's AppIcon name. The current repo has no app icon asset.
3. Set Release `SKIP_INSTALL=NO` for an application archive. The current project sets it to `YES`.
4. Set version/build numbers, configure the stable API URL, and archive for a generic watchOS device.
5. Validate/upload in Xcode Organizer, then add a tester group. A public external-testing link becomes usable only after Apple accepts the build for external testing.

An unsigned Release build or simulator `.app` is not a TestFlight distribution. No signing team or certificates are committed to this repo. Follow [Apple's external tester guide](https://developer.apple.com/help/app-store-connect/test-a-beta-version/invite-external-testers/). A recording supports the submission, but judges must also be able to follow the build/run instructions or access a functional demo.

## Final demo verification

- Public frontend and dashboard open from another network; demo data is clearly labeled.
- Owner sign-in reads only that owner's records through Hono.
- The Watch uses the stable HTTPS API, transcribes speech and receives progress/audio.
- Search returns an actual catalog SKU; cart review shows the exact amount and item.
- Explicit Watch approval produces a PayPal **Sandbox** provider-confirmed receipt.
- Delivery remains clearly simulated, including the local address feature.
- Restart the API and verify reconciliation and saved receipts still work.

Capture the public URL, setup instructions, demo video and native tester access required by the actual hackathon rules. Hosting alone does not establish eligibility.
