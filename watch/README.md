# ClawPay watch client

A watch-only SwiftUI app (watchOS 10+) that talks to the agent by voice. The user taps the mic (system dictation, scribble or keyboard) or a suggestion chip; the app sends the text to `POST /api/v1/assistant/messages` with the claw's `cpd_` token and shows the reply as a chat. It speaks the reply with the Fish Audio voice from `POST /api/v1/assistant/speech`, falling back to the built-in voice when the backend has no Fish Audio key or the call fails (Settings → Voice turns Fish Audio off). An order awaiting approval shows an amber card with the total and merchant, plus a haptic.

## Approving payments on the watch

By default the watch uses the claw's token and the claw's button approves. To approve on the watch instead, register the watch as its own device:

1. On first launch the app creates a P-256 approver key: in the Secure Enclave on a real watch (each signature needs the watch unlocked on the wrist, or the passcode, enforced by the hardware), or a software Keychain key in the simulator (no presence check; demo only).
2. `scripts/register_watch.py` registers that public key with the existing admin endpoint and gives the watch its own `cpd_` token (Settings shows the key type and ID; on a real watch use Settings → Share public key and `--public-key-file`).
3. Orders the watch creates now belong to the watch. The card shows **Approve**: the watch fetches the server's challenge and checks it (this order, `clawpay.order_approval.v1`, unexpired, positive amount, MYR or USD only). The card then shows the amount and merchant **from the signed payload** with **Confirm & pay**. Only that tap triggers the presence check (wrist + unlocked, or passcode; the system prompt repeats "Pay MYR 13.80 to Grocer Demo"), the Secure Enclave signature, and the post to `gadget-approval`. If the response is lost and a retry finds the challenge already used, the card says "processing" rather than "not paid". The server verifies it against the registered key exactly as for the claw, so spending limits, idempotency and PayPal handling are unchanged.

The device token is kept in the Keychain. The app refuses plain HTTP except to this machine (`127.0.0.1`/`localhost`), so use the HTTPS tunnel for a real watch. Demo hooks (`CLAWPAY_*` launch variables, auto-approve) are compiled into simulator builds only. Apple Pay's double-click side button is not available to third-party apps. The claw keeps its own token and button; orders made with the claw's token still need the claw.

Files: `ClawPayWatch/ContentView.swift` (screens), `ClawPayWatch/SpeechPlayer.swift` (Fish Audio playback + fallback), `ClawPayWatch/AssistantClient.swift` (HTTP and approval), `ClawPayWatch/ApproverKey.swift` (Secure Enclave key, presence check, signing), `ClawPayWatch/Theme.swift` (colors), `ClawPayWatch/Info.plist` (allows plain HTTP to localhost/LAN only).

## Run in the simulator against the offline demo backend

Needs Xcode with the watchOS simulator runtime (`xcodebuild -downloadPlatform watchOS`).

1. Start the demo backend from the repository root. It uses the in-memory store, mock merchants and a keyword stand-in for the LLM, and prints a device token. With `FISH_AUDIO_API_KEY` in `.env` it calls Fish Audio for speech (paid); without it the watch uses its own voice:

   ```bash
   uv run python -m scripts.assistant_demo_server
   ```

2. Open `watch/ClawPayWatch.xcodeproj` in Xcode, choose an Apple Watch simulator and run. In the app, tap the gear and paste the `cpd_` token (the server URL defaults to `http://127.0.0.1:8000`). Tap a suggestion chip, or tap the mic and type with the Mac keyboard, for example `buy two milk`.

   Or from the command line (no Xcode UI), replacing `<udid>` and `<token>`:

   ```bash
   xcodebuild -project watch/ClawPayWatch.xcodeproj -scheme ClawPayWatch -destination "id=<udid>" -derivedDataPath build/watch CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- build
   xcrun simctl install <udid> build/watch/Build/Products/Debug-watchsimulator/ClawPayWatch.app
   SIMCTL_CHILD_CLAWPAY_TOKEN=<token> SIMCTL_CHILD_CLAWPAY_DEMO_PROMPT="buy two milk" xcrun simctl launch <udid> com.clawpay.watch
   ```

   Keep the ad-hoc signing (`CODE_SIGN_IDENTITY=-`): with `CODE_SIGNING_ALLOWED=NO` the app has no Keychain access and cannot create its approver key.

   To approve on the watch, after that first launch: `uv run python -m scripts.register_watch --user-id <uuid> --admin-token <token> --simulator <udid>` (the demo server prints this command with the right values). It relaunches the app with the watch's own token.

   `CLAWPAY_BASE_URL`, `CLAWPAY_TOKEN`, `CLAWPAY_APPROVE_ON_WATCH=1`, `CLAWPAY_DEMO_PROMPT` (sends one message on launch) and `CLAWPAY_DEMO_APPROVE=1` (taps Approve once the order arrives) are read only when passed through `SIMCTL_CHILD_`.

Simulator dictation is limited; type instead. Dictation works on a real watch.

## Real backend / real watch

Point the server URL at the HTTPS tunnel (`PUBLIC_BASE_URL`) and use the claw's real `cpd_` token. The backend needs `MCP_ENABLED=true` and the `LLM_PROXY_*` settings. A real watch needs a signing team set in Xcode. For the claw to show watch-created orders, its approval task must long-poll `GET /api/v1/devices/{id}/pending-approvals`.
