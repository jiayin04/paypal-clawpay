# Product behavior

ClawPay is a transaction authorization layer for agent-assisted shopping. Its initial experience combines a native Watch client, merchant agents, PayPal Sandbox, and a read-only owner dashboard.

## Authorization and payment

The assistant searches and prepares a cart. The backend validates the quote and configured spending limits, checks applicable signed agent passports, and waits for a device-signed approval before executing payment. Passports limit merchants, order value, and expiry; owners can revoke them and inspect decision receipts.

A separate PayPal agent can hold payment-provider credentials. Idempotency and reconciliation protect the handling of uncertain provider outcomes. The device's reviewed transaction and provider-confirmed result remain distinct from model-generated text.

The authorization format is project-specific. Full AP2 interoperability and signed interpretation of a user's spoken budget are not implemented.

## Watch interaction

Talk records foreground audio and sends a successful transcript automatically after a short pause. Stop & send is available as a manual alternative. Fish Audio handles transcription and is the only provider for spoken replies. If speech is unavailable, the reply remains visible as text. DeepSeek handles text reasoning and tool calls.

A Siri App Shortcut hands a request into the same assistant path. Payment review and explicit confirmation remain mandatory. The Watch shows short replies, structured products, and completed action summaries rather than private model reasoning tokens.

## Dashboard

The Vite + React SPA and Hono server provide an owner workspace plus labeled demo scenarios for judges. Live reads use the owner's Supabase JWT and scoped backend endpoints. The dashboard does not approve payments, enroll devices, or change wallets.

The public Pages deployment currently offers demo mode. Hosted live use requires a reachable HTTPS backend, public Supabase configuration, and the owner account associated with the paired Watch.

## Delivery and location

Delivery is simulated for the hackathon. Users confirm a saved address, manually entered address, fictional demo destination, or one-shot current-location suggestion before approval. Reverse geocoding through Apple is disclosed at opt-in. Raw coordinates are not logged, stored, or sent to agents.

Confirmed receipt addresses and optional saved entries use the device-only Keychain and are scoped to the paired credential. Local delivery stages and `SIM-` tracking references are not synchronized to the dashboard or a carrier. No address is submitted to a retailer.

## Integration boundaries

Channel3 supplies real catalog search and price refresh when configured. Retailer checkout, actual fulfillment, person-to-person transfers, and production PayPal charging are not implemented. Demo lab replays are simulations and never call agents or submit payments.

See the [project overview](../README.md), [Watch guide](../mobile/watch/README.md), and [remaining validation checklist](../backend/docs/remaining-work.md).
