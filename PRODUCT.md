# ClawPay

ClawPay is a watch-first hackathon prototype for agent-assisted shopping and PayPal Sandbox payments. A user speaks to the watch; the backend assistant coordinates a merchant agent (including Channel3 catalog search), prepares a quote and waits for a signed watch approval. A separate PayPal agent executes authorized Sandbox payments.

The web dashboard serves the owner watching their activity and hackathon judges exploring clearly labeled sample scenarios. It is a read-only companion, not a payment approval surface. Live reads use a Supabase owner JWT and existing RLS-scoped backend endpoints. No admin/service-role/device signing secrets belong in the browser.

Existing limits, USD100 per order and USD500 daily, support the budget protection demo. Payments with uncertain results can be reconciled with the provider. Channel3 provides real catalog data; retailer fulfillment and person-to-person transfers are not implemented. Demo lab replays are simulations and do not call agents.

User commitments: Vite + React SPA, Hono.js server, mobile-friendly layout, modern Apple-inspired restraint, no decorative gradients. Preserve the existing watch and backend behavior. Dashboard remains useful without a live session through explicitly synthetic fixtures.

Confirmed first-release audience: owner dashboard plus judge demo (user confirmed 2026-11-05). A stable reachable backend HTTPS URL and the paired watch owner's Supabase account are needed for hosted live use.

Voice interaction: Talk records foreground audio and automatically sends the successful transcript after a short pause, with Stop & send as a manual alternative. Payment review and explicit confirmation remain mandatory. Fish Audio PCM is streamed through the backend to watch playback; order creation, spending and confirmed-paid single-tool replies avoid an extra model round trip.

Confirmed fulfillment choice (2026-11-05): simulate shipping for the hackathon. The watch requires a fictional demo destination before approval, shows persisted cart/payment receipts and advances local delivery stages only after verified paid status. Simulation stages and SIM tracking references are local to the watch; no retailer/carrier order is submitted. Channel3 public checkout remains coming soon. Live action summaries show executed tool stages and results, rather than private model reasoning tokens.

Delivery address choice: watch users can review a saved address, enter one manually, or explicitly request a one-shot location suggestion and confirm it. Apple reverse geocoding is disclosed at opt-in. Raw coordinates are not stored/logged or sent to agents. Confirmed receipt addresses and optional saved-address entries use the device-only Keychain. Shipping remains simulated; addresses are not sent to a retailer. Saved addresses are local and scoped to the paired device credential.
