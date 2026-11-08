# Hackathon stories

The strongest pitch is a watch-first shopping assistant with accountable agent handoffs and explicit human payment approval. The web dashboard makes that otherwise invisible work observable to judges.

1. **Voice to checkout.** Ask for a water bottle under $40 on the watch. Pause to send the request automatically, inspect the merchant quote, then approve the exact cart on the watch. Show the order timeline and sanitized A2A messages on the dashboard. PayPal Sandbox confirms the outcome. Channel3 catalog is real; retail fulfillment is not connected.
2. **A budget that holds.** Ask for an item above the configured per-order cap. Show that the backend enforces the limit before payment. The dashboard's daily committed/reserved/available allocation explains why spending room changes. The demo lab's $120 headphones against a $100 cap is synthetic; live values come from configuration.
3. **Resolve an uncertain payment.** Explain what happens if a provider times out after submission: the order enters reconciliation and the existing payment is checked before another attempt. Show the eventual provider-confirmed status and trace. The dashboard demo lab is a replay, not a live fault injector.

Potential next extensions, not implemented by this dashboard: scheduled replenishment reminders that still require watch approval; multi-merchant price comparisons with compatible quote schemas; proactive low-budget suggestions using current spending room. Avoid promising real retailer delivery, person-to-person payouts or autonomous charging until those integrations exist.

Demo lab is available immediately with sample data. For a real live demonstration, start the backend and PayPal agent, sign into the dashboard with the Supabase account owning the paired watch, and use the watch flow. Do not substitute service-role/admin/device tokens for a user session. A hosted dashboard also needs a stable reachable HTTPS backend origin; a loopback address or expired quick tunnel will not work.

## Product positioning and AP2 scope

Current strengths are device-signed approval of the exact cart/amount, spending policy, replay rejection, credential separation and payment reconciliation. Voice, A2A calls, progress views and fictional shipping make the demo understandable; they are not by themselves a novelty claim.

The signed `clawpay.order_approval.v1` payload is a project-specific authorization format. Calling PayPal Orders v2 and storing a cart mandate does not demonstrate full AP2 interoperability. A signed user Intent Mandate, merchant-signed Cart Mandate and independently verifiable standard credentials are not implemented. The spoken instruction “under $40” is not currently stored as a signed, hard budget policy; the existing backend enforces configured order/day caps.

Recommended next differentiator: turn a user's concrete purchase rule into enforceable authorization, with evidence of every check. A focused future demo would show an above-budget or changed cart being rejected, valid watch authorization accepted and an uncertain provider result reconciled before any retry. Keep these future-policy examples distinct from the limits and replay checks that already exist. Primary AP2 context: https://github.com/google-agentic-commerce/AP2/blob/main/docs/overview.md .
