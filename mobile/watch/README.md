# ClawPay watch client

A watch-only SwiftUI app (watchOS 10+) that talks to the agent by voice. Tap Talk
to record and transcribe a request, or invoke the Siri shortcut below.
The app sends the transcribed text to `POST /api/v1/assistant/messages` with its
`cpd_` token. The latest answer is shown; earlier messages are under History
(the clock button), with New chat inside History. Replies aim for 8–16 words.
Fish Audio is the only reply voice and plays streamed PCM as it arrives. Payment
review opens a separate sheet with the amount and merchant.

## Microphone speech-to-text

1. Tap **Talk** and allow microphone access.
2. Speak, then pause for about **1.1 seconds**. Recording ends and the request is sent automatically.
3. You can also tap **Stop & send**. There is no transcript confirmation or Record again screen. Payment still requires **Review → Confirm & pay**.

The app records 16 kHz mono 16-bit WAV, posts it to the authenticated backend
`POST /api/v1/assistant/transcriptions`, and uses Fish Audio `transcribe-1-pro`
with the existing server-side `FISH_AUDIO_API_KEY`. No extra key is needed.
The transcription API alone never calls the agent or creates an order; the watch immediately submits a successful transcript through the existing assistant endpoint, once. Cancel/backgrounding
stops capture; temporary audio is deleted after transcription or cancellation.
If recording or recognition fails, **Type instead** opens Apple's input sheet.

In Debug simulator builds, `SIMCTL_CHILD_CLAWPAY_TRANSCRIBE_FIXTURE=<wav-path>`
tests the real upload/transcription/automatic-send path with a synthetic WAV without
activating the microphone. Use only a read-only fixture such as “Show my spending today.”; the transcript now goes to the agent automatically. Live input
requires microphone permission and a working simulator/physical-watch input device.

## Product selection, receipts and delivery demo

The watch uses `POST /api/v1/assistant/messages/stream` for newline-delimited live action summaries plus one final response. Stages reflect the actual model/tool loop (understand request, search Channel3, validate quote/spending, check status); private model reasoning tokens are not returned. **Agent activity** shows the completed action summaries. Search responses also carry structured products with exact SKUs and prices; tapping a product explicitly asks the agent to prepare one item. A spoken “yes” can proceed when one specific item was offered; ambiguous multi-product choices should be clarified.

Carts retain saved item names, quantities and line totals. Before watch approval, choose and confirm a **saved, manually entered, current-location or fictional demo address**; Review and Confirm & pay require that selection. Payment is still PayPal Sandbox. The receipt shows **Sandbox paid**, items, total, order reference and confirmed payment time. It refreshes automatically after approval. Pending payments poll status for up to ~24 seconds, with Check status available afterward. Receipt reads are authenticated and scoped to the original device, and never submit another payment. The latest receipt ID is saved locally and restored after reopening the app.

After Paid, **Next delivery update** advances local delivery through Confirmed → Preparing → Shipped → Delivered, with a `SIM-` tracking reference. Address and stage are saved per order on this watch. These are local simulations, not merchant/carrier confirmations, and are not synchronized to the web dashboard. Addresses are kept locally in the watch Keychain and are not sent to the shopping agent or retailer. Current-location lookup sends coordinates to Apple only after the user opts in.

Channel3's public checkout docs currently say **coming soon**: https://channel3.mintlify.dev/docs/checkout . The integration calls product search and live price endpoints, not a retailer purchase endpoint. The receipt explicitly reports `retailer_order_status=not_submitted` and `fulfillment_mode=simulated`.

Debug simulator hook `SIMCTL_CHILD_CLAWPAY_RECEIPT_ORDER_ID=<existing-order-uuid>` restores a receipt through the real authenticated endpoint. It cannot authorize a payment. Offline visual review fixtures include representative line items and live-progress appearance; they do not contact providers.

## Current location and saved addresses

At checkout, choose **Delivery address**. The watch offers:

- **Saved on this watch**: select a prefilled address and review it before use.
- **Use current location**: first confirm the Apple Maps lookup, then grant When In Use permission. The app obtains one fresh location fix and resolves an address suggestion with MapKit on watchOS 26+, or Core Location geocoding on older supported watchOS versions.
- **Enter address**: enter the full street address/postcode and optional apartment or unit without location access.
- **Fictional demo addresses**: continue the hackathon simulation without personal address data.

Location results are suggestions. Confirm the street, building, postal code and unit, then tap **Use this address**. The app does not automatically select or persist a GPS suggestion. The optional **Remember on this watch** switch adds it to saved addresses for later orders. **Settings → Delivery → Saved addresses** also supports entering or editing a prefilled address. Saved addresses are scoped to the paired device credential; there is no phone, PayPal-address or dashboard synchronization.

Confirmed addresses are stored with the local receipt in the device-only Keychain; Remember additionally saves an account-scoped address-book entry. Raw latitude/longitude are not stored or logged, are never sent to DeepSeek, and are not added to backend messages. Apple receives coordinates for reverse geocoding only after the user's explicit choice. This is a foreground, one-shot lookup with no background tracking. Denied access, stale/approximate fixes, lookup failures and timeouts keep saved/manual alternatives available. Switching to manual entry cancels any ongoing lookup so it cannot replace the user's edit.

The OS purpose string is `NSLocationWhenInUseUsageDescription`. No Google Maps API key or extra server key is needed. A simulator needs a configured test location to exercise its GPS permission path. Debug `CLAWPAY_ADDRESS_SELF_CHECK=1` validates encrypted saved/receipt address storage; `CLAWPAY_REVIEW_SCREEN=address` with `CLAWPAY_ADDRESS_FIXTURE=review|saved|denied` renders fictional UI fixtures. `lookup` geocodes fixed public Austin test coordinates without reading or changing the watch's location or permission. All hooks are excluded from physical-watch and Release builds.

## Faster voice replies

The watch uses authenticated `POST /api/v1/assistant/speech/stream`: Fish Audio HTTP streaming with `format=pcm`, 24 kHz mono signed 16-bit little-endian audio and `latency=low`. The backend forwards bounded chunks; it does not collect the entire clip. The watch prebuffers about 200 ms, then schedules PCM buffers in AVAudioEngine as the rest arrives. Starting another request cancels the old stream/playback. The Fish key remains on the backend. Provider failures leave the reply visible as text with a brief voice-unavailable message; no other TTS engine is used. The MP3 endpoint remains available for older clients.

Fish also supports bidirectional WebSocket TTS, but this path streams audio from a completed short agent reply. Model/tool execution and batch transcription still precede playback; this is not full-duplex speech-to-speech. Successful single-tool order creation, spending summaries and confirmed paid status return short replies directly from trusted tool data, avoiding an extra LLM round trip. Product search and other status explanations still use the model.

The recorder estimates end of speech using microphone power, with at least two 100 ms samples of detected voice and a 1.1 s pause. In noisy rooms, use Stop & send; hardware microphone thresholds need validation on a real watch. The 30-second recording bound still applies.

Run the portable pause/PCM checks with:

```sh
swiftc mobile/watch/ClawPayWatch/VoiceAudio.swift mobile/watch/ClawPayWatch/DemoDelivery.swift mobile/watch/Tests/VoiceAudioChecks.swift -o /tmp/clawpay-voice-checks
/tmp/clawpay-voice-checks
```

## Siri and speech-to-text

After installing and opening ClawPay once on a registered watch, enable Siri and
Dictation in the watch's settings and say **“Hey Siri, ask ClawPay”** or
**“Hey Siri, talk to ClawPay.”** Siri asks **“What would you like to do?”**
Examples: “Find wool socks on Channel3,” “What did I spend today?” or ask about
an order discussed in the current conversation (or provide its order ID).

Apple performs STT for Siri or the fallback input sheet; the Talk recorder uses
Fish Audio STT. DeepSeek receives text for reasoning and tool calling. No DeepSeek audio-transcription API is used and
no additional STT key is required. The App Intent requires local device
authentication, opens ClawPay, and hands the request to the same agent path as Talk.
The transient request is encrypted in the Keychain, consumed once, expires after
two minutes, and waits while another request or payment review is in progress.

Siri can search, create an order awaiting approval, and check spending/status.
It cannot sign a payment: the user still reviews and confirms on the watch.
The watch uses wrist unlock/passcode and its Secure Enclave key, not face recognition.
Person-to-person transfers and actual retailer fulfilment are not implemented.
The LLM/tool loop runs on the backend; the watch keeps its scoped token and signing key.

Real-watch Siri wake, dictation accuracy and hardware presence need a physical
watch test. Simulator dictation/Siri services are limited. For a **Debug simulator**
check of the actual intent handler, launch with
`SIMCTL_CHILD_CLAWPAY_DEMO_SIRI_REQUEST="What did I spend today?"`.
`SIMCTL_CHILD_CLAWPAY_VOICE_SELF_CHECK=1` checks Keychain handoff single-use,
expiration, future timestamps, malformed data and input validation. These hooks
are excluded from physical-watch builds and Release builds.

## Approving payments on the watch

By default the watch uses the claw's token and the claw's button approves. To approve on the watch instead, register the watch as its own device:

1. On first launch the app creates a P-256 approver key: in the Secure Enclave on a real watch (each signature needs the watch unlocked on the wrist, or the passcode, enforced by the hardware), or a software Keychain key in the simulator (no presence check; demo only).
2. `scripts/register_watch.py` registers that public key with the existing admin endpoint and gives the watch its own `cpd_` token (Settings shows the key type and ID; on a real watch use Settings → Share public key and `--public-key-file`).
3. Orders the watch creates now belong to the watch. The payment sheet shows **Review**: the watch fetches the server's challenge and checks it (this order, `clawpay.order_approval.v1`, unexpired, positive amount, MYR or USD only). The card then shows the amount and merchant **from the signed payload** with **Confirm & pay**. Only that tap triggers the presence check (wrist + unlocked, or passcode; the system prompt repeats "Pay MYR 13.80 to Grocer Demo"), the Secure Enclave signature, and the post to `gadget-approval`. If the response is lost and a retry finds the challenge already used, the card says "processing" rather than "not paid". The server verifies it against the registered key exactly as for the claw, so spending limits, idempotency and PayPal handling are unchanged.

The device token is kept in the Keychain. The app refuses plain HTTP except to this machine (`127.0.0.1`/`localhost`), so use the HTTPS tunnel for a real watch. Demo hooks (`CLAWPAY_*` launch variables, auto-approve) are compiled into simulator builds only. Apple Pay's double-click side button is not available to third-party apps. The claw keeps its own token and button; orders made with the claw's token still need the claw.

Files: `ClawPayWatch/ContentView.swift` (screens), `ClawPayWatch/SpeechCapture.swift` (microphone, pause detection and automatic send), `ClawPayWatch/VoiceIntent.swift` (Siri), `ClawPayWatch/SpeechPlayer.swift` (playback), `ClawPayWatch/AssistantClient.swift` (HTTP and approval), `ClawPayWatch/ApproverKey.swift` (key and signing), `ClawPayWatch/Theme.swift` (colors), `ClawPayWatch/Info.plist` (microphone purpose and local networking).

## Interface and visual review

The watch uses an OLED-black background, flat blue primary actions, neutral
surfaces, system text styles and SF Symbols. New replies scroll to their beginning.
Payments open in a dedicated native sheet; fetching the signed challenge starts
the confirmation view at the amount again. The compose button is part of the
chat content and cannot cover payment controls. Reduce Motion disables
the custom scroll and content animations.

Debug simulator builds provide offline visual fixtures with
`SIMCTL_CHILD_CLAWPAY_REVIEW_SCREEN=conversation|approval|confirmation|paid|processing|error|loading`
when launching via `xcrun simctl launch`. Set
`SIMCTL_CHILD_CLAWPAY_REVIEW_LARGE_TEXT=1` for XXXL text. Fixtures disable interaction
and do not call providers or submit payments. Launch without these variables to
return to the real app. Screenshots are captured with `xcrun simctl io <udid> screenshot`;
they can be opened in a Codex file panel, while the running native simulator
remains a separate application.

## Run in the simulator against the offline demo backend

Needs Xcode with the watchOS simulator runtime (`xcodebuild -downloadPlatform watchOS`).

1. Start the demo backend from `backend/`. It uses the in-memory store, mock merchants and a keyword stand-in for the LLM, and prints a device token. With `FISH_AUDIO_API_KEY` in `.env` it calls Fish Audio for speech (paid); without it replies remain text-only:

   ```bash
   uv run python -m scripts.assistant_demo_server
   ```

2. Open `mobile/watch/ClawPayWatch.xcodeproj` in Xcode, choose an Apple Watch simulator and run. In the app, tap the gear and paste the `cpd_` token (the server URL defaults to `http://127.0.0.1:8000`). Tap a suggestion chip, or tap the mic and type with the Mac keyboard, for example `buy two milk`.

   Or from the command line (no Xcode UI), replacing `<udid>` and `<token>`:

   ```bash
   xcodebuild -project mobile/watch/ClawPayWatch.xcodeproj -scheme ClawPayWatch -destination "id=<udid>" -derivedDataPath build/watch CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- build
   xcrun simctl install <udid> build/watch/Build/Products/Debug-watchsimulator/ClawPayWatch.app
   SIMCTL_CHILD_CLAWPAY_TOKEN=<token> SIMCTL_CHILD_CLAWPAY_DEMO_PROMPT="buy two milk" xcrun simctl launch <udid> com.clawpay.watch
   ```

   Keep the ad-hoc signing (`CODE_SIGN_IDENTITY=-`): with `CODE_SIGNING_ALLOWED=NO` the app has no Keychain access and cannot create its approver key.

   To approve on the watch, after that first launch: from `backend/`, `uv run python -m scripts.register_watch --user-id <uuid> --admin-token <token> --simulator <udid>` (the demo server prints this command with the right values). It relaunches the app with the watch's own token.

   `CLAWPAY_BASE_URL`, `CLAWPAY_TOKEN`, `CLAWPAY_APPROVE_ON_WATCH=1`, `CLAWPAY_DEMO_PROMPT` (sends one message on launch) and `CLAWPAY_DEMO_APPROVE=1` (reviews and confirms a simulator order) are read only when passed through `SIMCTL_CHILD_`.

Simulator dictation is limited; type instead. Dictation works on a real watch.

## Real backend / real watch

Point the server URL at the HTTPS tunnel (`PUBLIC_BASE_URL`) and use the claw's real `cpd_` token. The backend needs `MCP_ENABLED=true` and the `LLM_PROXY_*` settings. A real watch needs a signing team set in Xcode. For the claw to show watch-created orders, its approval task must long-poll `GET /api/v1/devices/{id}/pending-approvals`.

### Simulator with Channel3 and the PayPal agent

This uses the real backend and hosted Supabase, rather than `assistant_demo_server`.
Put credentials in the private `backend/.env` file:

```dotenv
CHANNEL3_API_KEY=your-channel3-key
LLM_PROXY_UPSTREAM_BASE_URL=https://api.openai.com/v1
LLM_PROXY_UPSTREAM_API_KEY=your-openai-key
LLM_PROXY_MODEL=gpt-5.4-mini
PAYPAL_AGENT_ENDPOINT=http://127.0.0.1:8201/a2a
PAYPAL_AGENT_TOKEN=the-same-random-token-for-both-processes
DEFAULT_CURRENCY=USD
CHANNEL3_COUNTRY=US
```

Get a [Channel3 developer account and key](https://trychannel3.com/sign-up/developer)
and an [OpenAI API key](https://platform.openai.com/api-keys). The model must support
Chat Completions and function calling; see [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini).
Keep the existing Supabase, PayPal Sandbox, pepper and admin credentials. Before
switching an existing account's currency, check that it has no reserved or committed
spending in another currency on the demo spending day.

DeepSeek can replace OpenAI: use `LLM_PROXY_UPSTREAM_BASE_URL=https://api.deepseek.com`,
put your DeepSeek key in `DEEPSEEK_API_KEY` (or `LLM_PROXY_UPSTREAM_API_KEY`), and set `LLM_PROXY_MODEL` to
the model ID used by your ESP32 or another currently available tool-capable model
(the [DeepSeek quickstart](https://api-docs.deepseek.com/) currently uses `deepseek-flash`).
`deepseek-flash` currently serves DeepSeek V4.1 Flash. The `DEEPSEEK_API_KEY`
fallback applies only to the direct DeepSeek URL and only when the generic key is empty.
The proxy maps the response limit to DeepSeek's `max_tokens` and defaults to
non-thinking mode for short voice replies. Both devices can share the server-side
provider key while retaining their own ClawPay device tokens.

Run these from `backend/` in separate terminals, restarting after `.env` changes:

```bash
uv run uvicorn --factory app.paypal_agent.app:paypal_agent_app --host 127.0.0.1 --port 8201 --no-access-log
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Register the simulator using `scripts.register_watch` above with the Supabase owner
of the linked Sandbox wallet. Its backend URL is `http://127.0.0.1:8000`; no tunnel
is needed. Ask explicitly for products from Channel3. Keep the first order at or
below USD 100, then use Review and Confirm & pay on the watch. Simulator approval
uses a software key. The catalog is real, but retailer fulfilment is not implemented.

Run the combined offline wiring check from `backend/` with:

```bash
uv run --offline pytest tests/integration/test_watch_agent_flow.py
```

That check mocks the LLM, Channel3 API and PayPal API. It verifies the watch HTTP
contract, both A2A agents, and payment only after a valid signature; it does not
establish that external credentials, billing or live catalog responses work.

For a clean recording take on a Debug simulator, launch with `SIMCTL_CHILD_CLAWPAY_RECORDING_FRESH_START=1`. This opens a new conversation and skips automatic receipt restoration for that launch. It preserves device credentials, backend orders, payment records, and saved receipt addresses. The app labels local fulfillment as a delivery preview; Sandbox payment status and the no-shipment notice remain visible.
