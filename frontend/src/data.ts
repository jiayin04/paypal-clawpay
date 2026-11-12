export type Summary = Record<string, unknown>;
export type Event = {
    id: string;
    created_at: string;
    event_type?: string;
    source?: string;
    summary?: Summary;
    status?: string;
    decision?: string;
    verification_status?: string;
    actor_type?: string;
    entity_type?: string;
    entity_id?: string;
    method?: string;
    merchant_code?: string;
    direction?: string;
    outcome?: string;
    latency_ms?: number;
};
export type Order = {
    id: string;
    merchant_id?: string;
    merchant?: string;
    currency: string;
    total_minor: number;
    status: string;
    created_at: string;
    updated_at?: string;
    quote_expires_at?: string;
    items?: {
        description: string;
        quantity: number;
        line_total_minor: number;
    }[];
    approvals?: { decision: string; verification_status: string }[];
    payment_attempts?: { status: string; failure_code?: string }[];
};
export type Device = {
    id: string;
    device_name: string;
    status: string;
    key_algorithm: string;
    key_version: number;
    last_seen_at?: string;
    created_at: string;
};
export type Conversation = {
    id: string;
    device_id: string;
    source: string;
    status: string;
    created_at: string;
    updated_at: string;
};
export type Wallet = {
    id: string;
    provider: string;
    status: string;
    payer_email_masked?: string;
};
export type Spending = {
    currency: string;
    limit_minor: number;
    committed_minor: number;
    reserved_minor: number;
    remaining_minor: number;
    day?: string;
    timezone?: string;
};
// Agent passport: a signed, revocable grant for one agent device.
export type Passport = {
    id: string;
    device_id: string;
    status: string;
    merchants: string[];
    max_order_minor: number;
    currency: string;
    expires_at: string;
    revoked_at?: string | null;
    created_at: string;
    key_id: string;
};
export type PassportReceipt = {
    id: string;
    delegation_id?: string | null;
    device_id: string;
    order_id?: string | null;
    decision: "allowed" | "denied";
    reason: string;
    merchant_code: string;
    amount_minor?: number | null;
    currency: string;
    created_at: string;
};
export type Snapshot = {
    orders: Order[];
    events: Event[];
    devices: Device[];
    conversations: Conversation[];
    wallets: Wallet[];
    spending: Spending | null;
    passports: Passport[];
    receipts: PassportReceipt[];
};
const ago = (minutes: number) =>
    new Date(Date.now() - minutes * 60000).toISOString();
export const demoOrders: Order[] = [
    {
        id: "d68a191b-9079-4cc9-9cc2-914d11dbe046",
        merchant: "Channel3",
        currency: "USD",
        total_minor: 3400,
        status: "awaiting_device_approval",
        created_at: ago(2),
        quote_expires_at: ago(-28),
        items: [
            {
                description: "Everyday insulated water bottle",
                quantity: 1,
                line_total_minor: 3400,
            },
        ],
    },
    {
        id: "64042755-75cd-4d94-8d0a-e8f81b6b3c59",
        merchant: "Demo Grocer",
        currency: "USD",
        total_minor: 2600,
        status: "paid",
        created_at: ago(18),
        items: [
            {
                description: "Weekly pantry essentials",
                quantity: 1,
                line_total_minor: 2600,
            },
        ],
        approvals: [{ decision: "approved", verification_status: "verified" }],
        payment_attempts: [{ status: "succeeded" }],
    },
    {
        id: "97f79c8e-5498-4bd7-8244-20dcac69e212",
        merchant: "Demo Electronics",
        currency: "USD",
        total_minor: 7900,
        status: "expired",
        created_at: ago(72),
        items: [
            {
                description: "Compact wireless charger",
                quantity: 1,
                line_total_minor: 7900,
            },
        ],
    },
];
export const demoEvents: Event[] = [
    {
        id: "e1",
        event_type: "approval.requested",
        source: "conversation_events",
        created_at: ago(2),
        summary: {
            message: "Water bottle is ready for your review.",
            total_minor: 3400,
            currency: "USD",
        },
    },
    {
        id: "e2",
        event_type: "merchant.quote_received",
        source: "a2a_messages",
        merchant_code: "channel3",
        method: "quote_cart",
        outcome: "success",
        latency_ms: 482,
        created_at: ago(3),
        summary: { message: "Channel3 returned a $34.00 quote." },
    },
    {
        id: "e3",
        event_type: "tool.called",
        source: "conversation_events",
        created_at: ago(3.5),
        summary: {
            tool: "search_products",
            query: "water bottle under $40",
            result_count: 4,
        },
    },
    {
        id: "e4",
        event_type: "payment.succeeded",
        source: "audit_logs",
        actor_type: "agent",
        created_at: ago(18),
        summary: { message: "PayPal Sandbox confirmed $26.00." },
    },
    {
        id: "e5",
        event_type: "approval.verified",
        source: "approvals",
        created_at: ago(18.5),
        verification_status: "verified",
        decision: "approved",
        summary: { message: "Watch signature verified before payment." },
    },
    {
        id: "e6",
        event_type: "order.expired",
        source: "audit_logs",
        created_at: ago(42),
        summary: { message: "Charger quote expired. No payment was made." },
    },
];
export const demo: Snapshot = {
    orders: demoOrders,
    events: demoEvents,
    devices: [
        {
            id: "watch-demo",
            device_name: "Apple Watch",
            status: "active",
            key_algorithm: "P-256",
            key_version: 1,
            last_seen_at: ago(2),
            created_at: ago(1000),
        },
    ],
    conversations: [
        {
            id: "conversation-demo",
            device_id: "watch-demo",
            source: "watch",
            status: "active",
            created_at: ago(4),
            updated_at: ago(2),
        },
    ],
    wallets: [
        {
            id: "wallet-demo",
            provider: "paypal",
            status: "active",
            payer_email_masked: "c••••@example.com",
        },
    ],
    passports: [
        {
            id: "passport-demo",
            device_id: "watch-demo",
            status: "active",
            merchants: ["channel3", "grocer"],
            max_order_minor: 5000,
            currency: "USD",
            expires_at: new Date(Date.now() + 6 * 86400000).toISOString(),
            revoked_at: null,
            created_at: ago(1440),
            key_id: "3f9c1a7d2e5b8c40",
        },
    ],
    receipts: [
        {
            id: "receipt-demo-1",
            delegation_id: "passport-demo",
            device_id: "watch-demo",
            order_id: "order-demo-1",
            decision: "allowed",
            reason: "within_delegation",
            merchant_code: "channel3",
            amount_minor: 2600,
            currency: "USD",
            created_at: ago(9),
        },
        {
            id: "receipt-demo-2",
            delegation_id: "passport-demo",
            device_id: "watch-demo",
            order_id: null,
            decision: "denied",
            reason: "delegation_limit_exceeded",
            merchant_code: "channel3",
            amount_minor: 8900,
            currency: "USD",
            created_at: ago(31),
        },
    ],
    spending: {
        currency: "USD",
        limit_minor: 50000,
        committed_minor: 2600,
        reserved_minor: 3400,
        remaining_minor: 44000,
        day: new Date().toISOString().slice(0, 10),
        timezone: "Asia/Kuala_Lumpur",
    },
};
export const demoTimeline = (order: Order): Event[] =>
    order.status === "paid"
        ? [
              {
                  id: "t1",
                  source: "conversation_events",
                  event_type: "tool.called",
                  summary: {
                      tool: "create_order",
                      message: "Pantry essentials quoted at $26.00.",
                  },
                  created_at: ago(20),
              },
              {
                  id: "t2",
                  source: "approvals",
                  decision: "approved",
                  verification_status: "verified",
                  summary: {
                      message: "Approved on Apple Watch. Signature verified.",
                  },
                  created_at: ago(19),
              },
              {
                  id: "t3",
                  source: "a2a_messages",
                  merchant_code: "paypal",
                  method: "payment.execute",
                  outcome: "success",
                  latency_ms: 728,
                  summary: {
                      message: "Payment agent submitted the authorized order.",
                  },
                  created_at: ago(18.5),
              },
              {
                  id: "t4",
                  source: "payment_attempts",
                  status: "succeeded",
                  summary: { message: "PayPal Sandbox confirmed the payment." },
                  created_at: ago(18),
              },
          ]
        : [
              {
                  id: "t1",
                  source: "conversation_events",
                  event_type: "tool.called",
                  summary: {
                      tool: "search_products",
                      message: "Product search completed.",
                  },
                  created_at: order.created_at,
              },
              {
                  id: "t2",
                  source: "a2a_messages",
                  method: "quote_cart",
                  merchant_code: order.merchant?.toLowerCase(),
                  outcome: "success",
                  latency_ms: 482,
                  summary: { message: "Merchant quote received." },
                  created_at: order.created_at,
              },
              {
                  id: "t3",
                  source: "conversation_events",
                  event_type:
                      order.status === "expired"
                          ? "order.expired"
                          : "approval.requested",
                  summary: {
                      message:
                          order.status === "expired"
                              ? "Quote expired without payment."
                              : "Waiting for review and confirmation on the watch.",
                  },
                  created_at: order.created_at,
              },
          ];
export const scenarios = [
    {
        id: "shopping",
        title: "Voice to checkout",
        description:
            "Find a water bottle under $40, then review it on your wrist.",
        prompt: "“Find me a water bottle under $40.”",
        steps: [
            ["Watch", "Speech becomes a reviewable transcript."],
            ["ClawPay", "The assistant searches within your price range."],
            [
                "Channel3",
                "The merchant returns products and a validated quote.",
            ],
            [
                "Watch approval",
                "You review the $34 cart and sign the approval.",
            ],
            ["PayPal agent", "Sandbox payment completes after authorization."],
        ],
    },
    {
        id: "budget",
        title: "A budget that holds",
        description: "Show how spending limits stop an oversized cart.",
        prompt: "“Buy this $120 pair of headphones.”",
        steps: [
            ["Watch", "You ask for the headphones."],
            ["ClawPay", "The cart totals $120 before approval."],
            ["Spending policy", "The $100 per-order cap blocks the cart."],
            ["Result", "No payment is submitted. Choose a lower-priced item."],
        ],
    },
    {
        id: "recovery",
        title: "Resolve the unknown",
        description: "Recover a payment result after a slow provider response.",
        prompt: "“Did my payment go through?”",
        steps: [
            [
                "PayPal agent",
                "The original request timed out after submission.",
            ],
            ["ClawPay", "The order is marked for reconciliation."],
            [
                "Provider check",
                "Check the existing payment before attempting again.",
            ],
            ["Result", "A confirmed payment updates the order to Paid."],
        ],
    },
];
export const money = (minor: number, currency = "USD") =>
    new Intl.NumberFormat(undefined, { style: "currency", currency }).format(
        minor / 100,
    );
export const human = (value: string) =>
    value.replaceAll(/[_.-]/g, " ").replace(/\b\w/g, (x) => x.toUpperCase());
export const passportReason = (reason: string) =>
    ({
        within_delegation: "Within passport",
        no_delegation: "No passport",
        delegation_revoked: "Passport revoked",
        delegation_expired: "Passport expired",
        merchant_not_delegated: "Shop not allowed",
        currency_not_delegated: "Currency not allowed",
        delegation_limit_exceeded: "Over per-order limit",
    })[reason] || human(reason);
export const statusLabel = (status: string) =>
    ({
        awaiting_device_approval: "Needs approval",
        requires_reconciliation: "Checking payment",
        payment_pending: "Payment pending",
        payment_failed: "Payment failed",
    })[status.toLowerCase()] || human(status.toLowerCase());
export const eventTitle = (event: Event) =>
    event.method
        ? human(event.method)
        : event.event_type
          ? human(event.event_type)
          : event.decision
            ? `Approval ${event.decision}`
            : event.status
              ? `Payment ${human(event.status)}`
              : human(event.source || "Activity");
export const eventDetail = (event: Event) =>
    String(
        event.summary?.message ||
            event.summary?.query ||
            event.summary?.tool ||
            event.summary?.reason ||
            event.merchant_code ||
            event.actor_type ||
            "Recorded by ClawPay",
    );
