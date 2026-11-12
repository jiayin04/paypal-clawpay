import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { createClient } from "@supabase/supabase-js";
import type { SupabaseClient, Session } from "@supabase/supabase-js";
import {
    Activity,
    ArrowDown,
    BadgeCheck,
    ArrowRight,
    ArrowUpRight,
    Check,
    ChevronRight,
    CircleHelp,
    CreditCard,
    FlaskConical,
    LayoutDashboard,
    Link2,
    LoaderCircle,
    LogIn,
    LogOut,
    Menu,
    MessageSquare,
    Mic,
    RefreshCw,
    Search,
    ShieldCheck,
    ShoppingBag,
    SlidersHorizontal,
    Sparkles,
    Watch,
    X,
} from "lucide-react";
import {
    demo,
    demoTimeline,
    scenarios,
    money,
    human,
    statusLabel,
    eventTitle,
    eventDetail,
    passportReason,
} from "./data";
import type {
    Snapshot,
    Order,
    Event as AgentEvent,
    Conversation,
} from "./data";
import "./styles.css";
import { LandingPage } from "./landing";

type Page = "overview" | "orders" | "activity" | "devices" | "lab";
const navigation = [
    { id: "overview" as Page, label: "Overview", icon: LayoutDashboard },
    { id: "orders" as Page, label: "Orders", icon: ShoppingBag },
    { id: "activity" as Page, label: "Activity", icon: Activity },
    { id: "devices" as Page, label: "Devices", icon: Watch },
    { id: "lab" as Page, label: "Demo lab", icon: FlaskConical },
];
const empty: Snapshot = {
    orders: [],
    events: [],
    devices: [],
    conversations: [],
    wallets: [],
    spending: null,
    passports: [],
    receipts: [],
};
const time = (date: string) =>
    new Date(date).toLocaleTimeString(undefined, {
        hour: "numeric",
        minute: "2-digit",
    });
const dateTime = (date: string) =>
    new Date(date).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
function Status({ value }: { value: string }) {
    const good = [
        "paid",
        "active",
        "verified",
        "succeeded",
        "success",
    ].includes(value.toLowerCase());
    const warn = [
        "awaiting_device_approval",
        "requires_reconciliation",
        "payment_pending",
    ].includes(value.toLowerCase());
    return (
        <span className={`status ${good ? "good" : warn ? "warn" : "neutral"}`}>
            <span />
            {statusLabel(value)}
        </span>
    );
}
function Empty({
    title,
    children,
}: {
    title: string;
    children: React.ReactNode;
}) {
    return (
        <div className="empty">
            <div className="empty-icon">
                <Activity size={24} />
            </div>
            <h3>{title}</h3>
            <p>{children}</p>
        </div>
    );
}
function Modal({
    open,
    onClose,
    label,
    children,
    wide = false,
}: {
    open: boolean;
    onClose: () => void;
    label: string;
    children: React.ReactNode;
    wide?: boolean;
}) {
    const ref = useRef<HTMLDialogElement>(null);
    useEffect(() => {
        if (open && !ref.current?.open) ref.current?.showModal();
        else if (!open && ref.current?.open) ref.current.close();
    }, [open]);
    return (
        <dialog
            ref={ref}
            className={wide ? "modal wide" : "modal"}
            aria-label={label}
            onCancel={onClose}
            onClick={(e) => {
                if (e.target === ref.current) onClose();
            }}
        >
            <div className="modal-head">
                <h2>{label}</h2>
                <button
                    className="icon-button"
                    onClick={onClose}
                    aria-label="Close"
                >
                    <X size={20} />
                </button>
            </div>
            {children}
        </dialog>
    );
}
function App() {
    const [page, setPage] = useState<Page>("overview"),
        [mode, setMode] = useState<"demo" | "live">("demo"),
        [snapshot, setSnapshot] = useState<Snapshot>(empty),
        [search, setSearch] = useState(""),
        [filter, setFilter] = useState("all"),
        [loading, setLoading] = useState(false),
        [error, setError] = useState(""),
        [updated, setUpdated] = useState<Date | null>(null);
    const [loaded, setLoaded] = useState<
        Partial<Record<keyof Snapshot, boolean>>
    >({});
    const [auth, setAuth] = useState<SupabaseClient | null>(null),
        [session, setSession] = useState<Session | null>(null),
        [configReady, setConfigReady] = useState(false),
        [liveAvailable, setLiveAvailable] = useState(false),
        [login, setLogin] = useState(false),
        [email, setEmail] = useState(""),
        [password, setPassword] = useState(""),
        [authBusy, setAuthBusy] = useState(false),
        [authError, setAuthError] = useState("");
    const [selected, setSelected] = useState<Order | null>(null),
        [detail, setDetail] = useState<Order | null>(null),
        [timeline, setTimeline] = useState<AgentEvent[]>([]),
        [messages, setMessages] = useState<AgentEvent[]>([]),
        [detailError, setDetailError] = useState(""),
        [detailLoading, setDetailLoading] = useState(false),
        [traceTab, setTraceTab] = useState<"timeline" | "a2a">("timeline");
    const [conversation, setConversation] = useState<Conversation | null>(null),
        [conversationEvents, setConversationEvents] = useState<AgentEvent[]>(
            [],
        ),
        [conversationError, setConversationError] = useState(""),
        [conversationLoading, setConversationLoading] = useState(false);
    const [scenario, setScenario] = useState(0),
        [step, setStep] = useState(-1),
        [menu, setMenu] = useState(false),
        [help, setHelp] = useState(false);
    const refreshSequence = useRef(0);
    const data = mode === "demo" ? demo : snapshot;
    const known = (key: keyof Snapshot) =>
        mode === "demo" || Boolean(loaded[key]);
    const unavailable = (resource: string) => (
        <Empty
            title={
                loading
                    ? `Loading ${resource}…`
                    : `${human(resource)} unavailable`
            }
        >
            {loading
                ? "Fetching your workspace."
                : "Refresh to try loading this information again."}
        </Empty>
    );
    useEffect(() => {
        let active = true;
        let ownerId: string | null = null;
        let unsubscribe: (() => void) | undefined;
        fetch("/api/config")
            .then((r) => (r.ok ? r.json() : Promise.reject()))
            .then((config) => {
                if (!active) return;
                setLiveAvailable(config.liveAvailable);
                if (config.supabaseUrl && config.supabaseKey) {
                    const client = createClient(
                        config.supabaseUrl,
                        config.supabaseKey,
                        {
                            auth: {
                                storage: sessionStorage,
                                persistSession: true,
                                detectSessionInUrl: false,
                            },
                        },
                    );
                    setAuth(client);
                    unsubscribe = client.auth.onAuthStateChange(
                        (_event, next) => {
                            if (active) {
                                const nextOwner = next?.user.id ?? null;
                                if (ownerId !== nextOwner) {
                                    ownerId = nextOwner;
                                    // Invalidate pending reads before another owner can see them.
                                    refreshSequence.current++;
                                    setSnapshot(empty);
                                    setLoaded({});
                                    setUpdated(null);
                                    setSelected(null);
                                    setDetail(null);
                                    setTimeline([]);
                                    setMessages([]);
                                    setConversation(null);
                                    setConversationEvents([]);
                                    setLoading(false);
                                    setError("");
                                }
                                setSession(next);
                                setMode(next ? "live" : "demo");
                            }
                        },
                    ).data.subscription.unsubscribe;
                }
            })
            .catch(() => {
                if (active)
                    setAuthError(
                        "Connection settings could not be loaded. Refresh to retry.",
                    );
            })
            .finally(() => {
                if (active) setConfigReady(true);
            });
        return () => {
            active = false;
            unsubscribe?.();
        };
    }, []);
    const api = useCallback(
        async (path: string, signal?: AbortSignal) => {
            if (!session) throw new Error("Sign in to view live activity.");
            const response = await fetch("/api/v1/" + path, {
                headers: { Authorization: "Bearer " + session.access_token },
                signal,
            });
            const body = await response.json();
            if (!response.ok)
                throw new Error(
                    response.status === 401
                        ? "Your session expired. Sign in again."
                        : body.error?.message ||
                              "Could not load activity. Try refreshing.",
                );
            return body;
        },
        [session],
    );
    const refresh = useCallback(async () => {
        if (mode !== "live" || !session) return;
        const seq = ++refreshSequence.current;
        setLoading(true);
        const paths = [
            "orders?limit=50",
            "audit-events?limit=50",
            "devices",
            "conversations?limit=50",
            "payment-methods",
            "spending/summary",
            "delegations",
            "delegations/receipts?limit=20",
        ];
        const results = await Promise.allSettled(
            paths.map((path) => api(path)),
        );
        if (seq !== refreshSequence.current) return;
        const keys = [
            "orders",
            "events",
            "devices",
            "conversations",
            "wallets",
            "spending",
            "passports",
            "receipts",
        ] as const;
        const bodyKeys = [
            "orders",
            "audit_events",
            "devices",
            "conversations",
            "payment_methods",
            "",
            "delegations",
            "receipts",
        ];
        setSnapshot((previous) => {
            const next = { ...previous };
            results.forEach((result, i) => {
                if (result.status === "fulfilled")
                    Object.assign(next, {
                        [keys[i]]: bodyKeys[i]
                            ? result.value[bodyKeys[i]]
                            : result.value,
                    });
            });
            return next;
        });
        setLoaded((previous) => {
            const next = { ...previous };
            results.forEach((result, i) => {
                if (result.status === "fulfilled") next[keys[i]] = true;
            });
            return next;
        });
        const failed = results.flatMap((result, i) =>
            result.status === "rejected"
                ? [`${human(keys[i])}: ${result.reason.message}`]
                : [],
        );
        const reasons = results.flatMap((result) =>
            result.status === "rejected" ? [result.reason.message] : [],
        );
        setError(new Set(reasons).size === 1 ? reasons[0] : failed.join(" "));
        if (!failed.length) setUpdated(new Date());
        setLoading(false);
    }, [api, mode, session]);
    useEffect(() => {
        void refresh();
        const interval = setInterval(() => {
            if (!document.hidden) void refresh();
        }, 15000);
        return () => {
            clearInterval(interval);
            refreshSequence.current++;
        };
    }, [refresh]);
    useEffect(() => {
        setSelected(null);
        setConversation(null);
        setError("");
        setSearch("");
        setFilter("all");
    }, [mode]);
    useEffect(() => {
        if (!selected) return;
        let active = true;
        setDetail(null);
        setTimeline([]);
        setMessages([]);
        setDetailError("");
        if (mode === "demo") {
            setDetail(selected);
            const events = demoTimeline(selected);
            setTimeline(events);
            setMessages(events.filter((e) => e.source === "a2a_messages"));
            setDetailLoading(false);
            return;
        }
        setDetailLoading(true);
        Promise.allSettled([
            api("orders/" + selected.id),
            api("orders/" + selected.id + "/timeline?limit=100"),
            api("orders/" + selected.id + "/a2a-messages?limit=100"),
        ]).then((results) => {
            if (!active) return;
            if (results[0].status === "fulfilled") setDetail(results[0].value);
            if (results[1].status === "fulfilled")
                setTimeline(results[1].value.events);
            if (results[2].status === "fulfilled")
                setMessages(results[2].value.messages);
            setDetailError(
                results
                    .flatMap((r) =>
                        r.status === "rejected" ? [r.reason.message] : [],
                    )
                    .join(" "),
            );
            setDetailLoading(false);
        });
        return () => {
            active = false;
        };
    }, [selected, mode, api, updated]);
    useEffect(() => {
        if (!conversation) return;
        let active = true;
        setConversationError("");
        setConversationEvents([]);
        if (mode === "demo") {
            setConversationEvents(demo.events);
            return;
        }
        setConversationLoading(true);
        api(`conversations/${conversation.id}/events?limit=100`)
            .then((body) => {
                if (active) setConversationEvents(body.events);
            })
            .catch((e) => {
                if (active) setConversationError(e.message);
            })
            .finally(() => {
                if (active) setConversationLoading(false);
            });
        return () => {
            active = false;
        };
    }, [conversation, mode, api]);
    const navigate = (next: Page) => {
        setPage(next);
        setSearch("");
        setFilter("all");
        setMenu(false);
    };
    const signin = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!auth) return;
        setAuthBusy(true);
        setAuthError("");
        try {
            const { error } = await auth.auth.signInWithPassword({
                email,
                password,
            });
            if (error) throw error;
            setLogin(false);
            setPassword("");
        } catch (e) {
            setAuthError(
                e instanceof Error ? e.message : "Sign-in failed. Try again.",
            );
        } finally {
            setAuthBusy(false);
        }
    };
    const signout = async () => {
        await auth?.auth.signOut({ scope: "local" });
        setSession(null);
        setMode("demo");
        setSnapshot(empty);
        setLoaded({});
        setUpdated(null);
    };
    const query = search.trim().toLowerCase();
    const orders = data.orders.filter(
        (o) =>
            (filter === "all" || o.status === filter) &&
            (!query ||
                [
                    o.id,
                    o.merchant,
                    o.status,
                    statusLabel(o.status),
                    ...(o.items?.map((i) => i.description) || []),
                ]
                    .join(" ")
                    .toLowerCase()
                    .includes(query)),
    );
    const events = data.events.filter(
        (e) =>
            (filter === "all" ||
                e.actor_type === filter ||
                e.source === filter) &&
            (!query ||
                [eventTitle(e), eventDetail(e), e.entity_id]
                    .join(" ")
                    .toLowerCase()
                    .includes(query)),
    );
    const needsApproval = data.orders.filter(
        (o) => o.status === "awaiting_device_approval",
    );
    const spending = data.spending;
    const showOrder = (order: Order) => {
        setTraceTab("timeline");
        setSelected(order);
    };
    const OrderRows = ({ limit }: { limit?: number }) => (
        <div className="order-list">
            {!known("orders") ? (
                unavailable("orders")
            ) : orders.length ? (
                orders.slice(0, limit).map((order) => (
                    <button
                        key={order.id}
                        className="order-row"
                        onClick={() => showOrder(order)}
                    >
                        <span
                            className={`merchant-icon ${order.status === "paid" ? "mint" : ""}`}
                        >
                            <ShoppingBag size={20} />
                        </span>
                        <span className="order-name">
                            <strong>
                                {order.merchant ||
                                    `Order ${order.id.slice(0, 8)}`}
                            </strong>
                            <span>
                                {order.items?.[0]?.description ||
                                    `Order ${order.id.slice(0, 8)}`}
                                <span className="row-date">
                                    {" "}
                                    · {dateTime(order.created_at)}
                                </span>
                            </span>
                        </span>
                        <span className="order-value">
                            <strong>
                                {money(order.total_minor, order.currency)}
                            </strong>
                            <Status value={order.status} />
                        </span>
                        <ChevronRight size={16} className="row-chevron" />
                    </button>
                ))
            ) : (
                <Empty
                    title={
                        query || filter !== "all"
                            ? "No matching orders"
                            : "No orders yet"
                    }
                >
                    {query || filter !== "all"
                        ? "Try another search or status."
                        : "Ask ClawPay on your watch to find a product. Your orders will appear here."}
                </Empty>
            )}
        </div>
    );
    const EventRows = ({
        items = events,
        limit,
    }: {
        items?: AgentEvent[];
        limit?: number;
    }) => (
        <ol className="event-list">
            {items.slice(0, limit).map((event) => (
                <li key={event.id}>
                    <span
                        className={`event-marker ${/paid|succeed|verified/.test(event.event_type || event.status || "") ? "mint" : ""}`}
                    >
                        {event.source === "approvals" ? (
                            <ShieldCheck size={16} />
                        ) : event.method ? (
                            <Link2 size={16} />
                        ) : (
                            <Activity size={16} />
                        )}
                    </span>
                    <div>
                        <strong>{eventTitle(event)}</strong>
                        <p>{eventDetail(event)}</p>
                        <span className="event-meta">
                            {human(
                                event.source || event.actor_type || "ClawPay",
                            )}
                            {event.latency_ms != null
                                ? ` · ${event.latency_ms} ms`
                                : ""}
                            {event.outcome ? ` · ${human(event.outcome)}` : ""}
                        </span>
                    </div>
                    <time
                        dateTime={event.created_at}
                        title={dateTime(event.created_at)}
                    >
                        {time(event.created_at)}
                    </time>
                </li>
            ))}
        </ol>
    );
    return (
        <div className="app-shell">
            <a className="skip-link" href="#main">
                Skip to content
            </a>
            <aside className={`sidebar ${menu ? "open" : ""}`}>
                <a
                    className="brand"
                    href="#overview"
                    onClick={(e) => {
                        e.preventDefault();
                        navigate("overview");
                    }}
                >
                    <img src="/favicon.svg" alt="" />
                    <span>ClawPay</span>
                </a>
                <div className="workspace">
                    <span className="workspace-avatar">C</span>
                    <div>
                        <strong>Personal workspace</strong>
                        <span>PayPal Sandbox</span>
                    </div>
                    <ShieldCheck size={16} />
                </div>
                <nav aria-label="Main navigation">
                    {navigation.map((item) => (
                        <button
                            key={item.id}
                            onClick={() => navigate(item.id)}
                            className={
                                page === item.id
                                    ? "nav-item selected"
                                    : "nav-item"
                            }
                            aria-current={page === item.id ? "page" : undefined}
                        >
                            <item.icon size={19} />
                            <span>{item.label}</span>
                            {item.id === "orders" &&
                                needsApproval.length > 0 && (
                                    <span className="nav-count">
                                        {needsApproval.length}
                                    </span>
                                )}
                        </button>
                    ))}
                </nav>
                <div className="sidebar-bottom">
                    <div className="watch-note">
                        <Watch size={22} />
                        <strong>Made for your wrist.</strong>
                        <p>
                            Your agents do the legwork.
                            <br />
                            You make the final call.
                        </p>
                    </div>
                    <button className="nav-item" onClick={() => setHelp(true)}>
                        <CircleHelp size={19} />
                        How it works
                        <ArrowUpRight size={15} />
                    </button>
                    <span className="sidebar-caption">
                        ClawPay · Hackathon build
                    </span>
                </div>
            </aside>
            {menu && (
                <button
                    className="nav-scrim"
                    aria-label="Close navigation"
                    onClick={() => setMenu(false)}
                />
            )}
            <div className="main-shell">
                <header className="topbar">
                    <div className="breadcrumb">
                        <button
                            className="icon-button mobile-menu"
                            aria-label="Open navigation"
                            onClick={() => setMenu(!menu)}
                        >
                            <Menu size={20} />
                        </button>
                        <span>Workspace</span>
                        <ChevronRight size={14} />
                        <strong>
                            {navigation.find((n) => n.id === page)?.label}
                        </strong>
                    </div>
                    <div className="topbar-right">
                        <span
                            className={`connection ${mode === "live" && !error ? "connected" : ""}`}
                        >
                            <span />
                            {mode === "demo"
                                ? "Demo data"
                                : error
                                  ? "Connection interrupted"
                                  : "Owner workspace"}
                        </span>
                        {session ? (
                            <button
                                className="avatar"
                                onClick={() => setHelp(true)}
                                aria-label="Account details"
                            >
                                {session.user.email?.[0]?.toUpperCase() || "C"}
                            </button>
                        ) : (
                            <button
                                className="button small"
                                onClick={() => setLogin(true)}
                            >
                                <LogIn size={15} />
                                <span>Connect live</span>
                            </button>
                        )}
                    </div>
                </header>
                <main id="main" tabIndex={-1}>
                    <div className="page-heading">
                        <div>
                            <h1>
                                {navigation.find((n) => n.id === page)?.label}
                            </h1>
                            <p>
                                {page === "overview"
                                    ? "A clear view of everything your agents are doing."
                                    : page === "orders"
                                      ? "From the first quote to the final payment."
                                      : page === "activity"
                                        ? "Follow the decisions behind every action."
                                        : page === "devices"
                                          ? "Your trusted watch and connected wallet."
                                          : "Three stories. One watch. You stay in control."}
                            </p>
                        </div>
                        <div className="heading-actions">
                            {mode === "live" && (
                                <span className="updated">
                                    {updated
                                        ? `Updated ${time(updated.toISOString())}`
                                        : "Waiting for activity"}
                                </span>
                            )}
                            <button
                                className="button"
                                disabled={loading}
                                onClick={() =>
                                    mode === "live"
                                        ? void refresh()
                                        : setUpdated(new Date())
                                }
                                aria-label="Refresh activity"
                            >
                                <RefreshCw
                                    size={16}
                                    className={loading ? "spin" : ""}
                                />
                                <span>
                                    {loading ? "Refreshing" : "Refresh"}
                                </span>
                            </button>
                        </div>
                    </div>
                    <div className="mode-banner">
                        <span className="banner-icon">
                            {mode === "demo" ? (
                                <FlaskConical size={18} />
                            ) : (
                                <ShieldCheck size={18} />
                            )}
                        </span>
                        <p>
                            <strong>
                                {mode === "demo"
                                    ? "You’re exploring the demo."
                                    : error
                                      ? "Your workspace needs attention."
                                      : "Your activity, securely connected."}
                            </strong>{" "}
                            <span>
                                {mode === "demo"
                                    ? "All activity below is sample data. No payments are made."
                                    : error
                                      ? "Some information could not be refreshed. Available snapshots remain visible."
                                      : "Only your account’s records are shown. Updates every 15 seconds."}
                            </span>
                        </p>
                        {mode === "demo" ? (
                            <button
                                className="text-button"
                                onClick={() =>
                                    session ? setMode("live") : setLogin(true)
                                }
                            >
                                View live activity
                                <ArrowRight size={15} />
                            </button>
                        ) : (
                            <button
                                className="text-button"
                                onClick={() => setMode("demo")}
                            >
                                Explore demo
                                <ArrowRight size={15} />
                            </button>
                        )}
                    </div>
                    {mode === "demo" && updated && (
                        <p className="refresh-note" role="status">
                            Demo snapshot refreshed at{" "}
                            {time(updated.toISOString())}. Sample data stays the
                            same.
                        </p>
                    )}
                    {error && (
                        <div className="error-banner" role="alert">
                            <p>
                                {error}{" "}
                                {updated
                                    ? "Showing the last available snapshot."
                                    : ""}
                            </p>
                            <button
                                className="text-button"
                                onClick={() => void refresh()}
                            >
                                Retry
                            </button>
                        </div>
                    )}
                    {mode === "live" && loading && !updated && (
                        <div className="loading-line" role="status">
                            <LoaderCircle size={17} className="spin" />
                            Loading your workspace…
                        </div>
                    )}
                    {page === "overview" && (
                        <>
                            <section
                                className="summary-strip"
                                aria-label="Workspace summary"
                            >
                                <div>
                                    <span className="stat-label">
                                        Spent today <ArrowUpRight size={15} />
                                    </span>
                                    <strong>
                                        {spending
                                            ? money(
                                                  spending.committed_minor,
                                                  spending.currency,
                                              )
                                            : "—"}
                                    </strong>
                                    <span>
                                        {spending
                                            ? `${spending.currency} · ${spending.timezone || "Account timezone"}`
                                            : "Spending summary unavailable"}
                                    </span>
                                </div>
                                <div>
                                    <span className="stat-label">
                                        Awaiting your approval{" "}
                                        <Watch size={16} />
                                    </span>
                                    <strong>
                                        {known("orders")
                                            ? needsApproval.length
                                            : "—"}
                                        <small>
                                            {needsApproval.length === 1
                                                ? "order"
                                                : "orders"}
                                        </small>
                                    </strong>
                                    <span>
                                        {!known("orders")
                                            ? "Approval status unavailable"
                                            : needsApproval.length
                                              ? "Review on your Apple Watch"
                                              : "You’re all caught up"}
                                    </span>
                                </div>
                                <div>
                                    <span className="stat-label">
                                        Orders in this snapshot{" "}
                                        <ShoppingBag size={16} />
                                    </span>
                                    <strong>
                                        {known("orders")
                                            ? data.orders.length
                                            : "—"}
                                        <small>orders</small>
                                    </strong>
                                    <span>
                                        {known("orders")
                                            ? `${data.orders.filter((o) => o.status === "paid").length} paid · latest 50 orders`
                                            : "Order count unavailable"}
                                    </span>
                                </div>
                            </section>
                            <div className="overview-grid">
                                <div className="primary-column">
                                    {needsApproval.length > 0 && (
                                        <section className="approval-callout">
                                            <div className="approval-watch">
                                                <Watch size={28} />
                                            </div>
                                            <div>
                                                <h2>Your watch is waiting.</h2>
                                                <p>
                                                    {needsApproval.length === 1
                                                        ? `${money(needsApproval[0].total_minor, needsApproval[0].currency)} cart ready to review.`
                                                        : `${needsApproval.length} carts ready to review.`}{" "}
                                                    Confirm on your wrist when
                                                    you’re ready.
                                                </p>
                                                <button
                                                    className="text-button"
                                                    onClick={() =>
                                                        showOrder(
                                                            needsApproval[0],
                                                        )
                                                    }
                                                >
                                                    Review order
                                                    <ArrowRight size={15} />
                                                </button>
                                            </div>
                                            <span className="approval-dot" />
                                        </section>
                                    )}
                                    <section className="panel">
                                        <div className="section-heading">
                                            <div>
                                                <h2>Recent orders</h2>
                                                <p>Every cart has a story.</p>
                                            </div>
                                            <button
                                                className="text-button"
                                                onClick={() =>
                                                    navigate("orders")
                                                }
                                            >
                                                View all
                                                <ArrowRight size={15} />
                                            </button>
                                        </div>
                                        <OrderRows limit={4} />
                                    </section>
                                    <section className="panel activity-panel">
                                        <div className="section-heading">
                                            <div>
                                                <h2>Agent activity</h2>
                                                <p>
                                                    {mode === "demo"
                                                        ? "A sample of the work behind the scenes."
                                                        : "Audit events from your account."}
                                                </p>
                                            </div>
                                            <button
                                                className="text-button"
                                                onClick={() =>
                                                    navigate("activity")
                                                }
                                            >
                                                View all
                                                <ArrowRight size={15} />
                                            </button>
                                        </div>
                                        {!known("events") ? (
                                            unavailable("activity")
                                        ) : events.length ? (
                                            <EventRows limit={4} />
                                        ) : (
                                            <Empty title="No activity yet">
                                                Agent events appear here as you
                                                use the watch.
                                            </Empty>
                                        )}
                                    </section>
                                </div>
                                <div className="secondary-column">
                                    <section className="panel budget-panel">
                                        <div className="section-heading">
                                            <h2>Daily spending</h2>
                                            <ShieldCheck size={19} />
                                        </div>
                                        {spending ? (
                                            <>
                                                <div className="budget-amount">
                                                    <strong>
                                                        {money(
                                                            spending.remaining_minor,
                                                            spending.currency,
                                                        )}
                                                    </strong>
                                                    <span>available today</span>
                                                </div>
                                                <div
                                                    className="budget-track"
                                                    role="img"
                                                    aria-label={`${money(spending.committed_minor, spending.currency)} spent, ${money(spending.reserved_minor, spending.currency)} reserved, ${money(spending.remaining_minor, spending.currency)} available out of ${money(spending.limit_minor, spending.currency)}`}
                                                >
                                                    <span
                                                        className="spent"
                                                        style={{
                                                            width: `${Math.min(100, Math.max(0, (spending.committed_minor / Math.max(1, spending.limit_minor)) * 100))}%`,
                                                        }}
                                                    />
                                                    <span
                                                        className="reserved"
                                                        style={{
                                                            width: `${Math.min(100, Math.max(0, (spending.reserved_minor / Math.max(1, spending.limit_minor)) * 100))}%`,
                                                        }}
                                                    />
                                                </div>
                                                <dl className="budget-legend">
                                                    <div>
                                                        <dt>
                                                            <i className="spent-dot" />
                                                            Spent
                                                        </dt>
                                                        <dd>
                                                            {money(
                                                                spending.committed_minor,
                                                                spending.currency,
                                                            )}
                                                        </dd>
                                                    </div>
                                                    <div>
                                                        <dt>
                                                            <i className="reserved-dot" />
                                                            Reserved
                                                        </dt>
                                                        <dd>
                                                            {money(
                                                                spending.reserved_minor,
                                                                spending.currency,
                                                            )}
                                                        </dd>
                                                    </div>
                                                    <div className="budget-total">
                                                        <dt>Daily limit</dt>
                                                        <dd>
                                                            {money(
                                                                spending.limit_minor,
                                                                spending.currency,
                                                            )}
                                                        </dd>
                                                    </div>
                                                </dl>
                                                <p className="footnote">
                                                    Reservations hold room for
                                                    pending orders. Limits are
                                                    enforced by ClawPay.
                                                </p>
                                            </>
                                        ) : (
                                            <Empty title="Spending unavailable">
                                                Refresh to try loading your
                                                daily allowance.
                                            </Empty>
                                        )}
                                    </section>
                                    <section className="panel route-panel">
                                        <div className="section-heading">
                                            <h2>
                                                One request. A team of agents.
                                            </h2>
                                        </div>
                                        <div className="agent-route">
                                            {[
                                                {
                                                    icon: Watch,
                                                    title: "Your watch",
                                                    sub: "Speak. Review. Approve.",
                                                },
                                                {
                                                    icon: Sparkles,
                                                    title: "ClawPay",
                                                    sub: "Understands your request",
                                                },
                                                {
                                                    icon: ShoppingBag,
                                                    title: "Merchant agent",
                                                    sub: "Channel3 product search",
                                                },
                                                {
                                                    icon: CreditCard,
                                                    title: "PayPal agent",
                                                    sub: "Executes authorized payment",
                                                },
                                            ].map((agent, i) => (
                                                <React.Fragment
                                                    key={agent.title}
                                                >
                                                    <div className="route-node">
                                                        <span
                                                            className={`route-icon route-${i}`}
                                                        >
                                                            <agent.icon
                                                                size={18}
                                                            />
                                                        </span>
                                                        <div>
                                                            <strong>
                                                                {agent.title}
                                                            </strong>
                                                            <span>
                                                                {agent.sub}
                                                            </span>
                                                        </div>
                                                        {i === 0 && (
                                                            <ShieldCheck
                                                                size={16}
                                                                className="route-shield"
                                                            />
                                                        )}
                                                    </div>
                                                    {i < 3 && (
                                                        <div className="route-connector">
                                                            <ArrowDown
                                                                size={12}
                                                            />
                                                        </div>
                                                    )}
                                                </React.Fragment>
                                            ))}
                                        </div>
                                        <button
                                            className="text-button"
                                            onClick={() => navigate("lab")}
                                        >
                                            Explore a demo
                                            <ArrowRight size={15} />
                                        </button>
                                    </section>
                                </div>
                            </div>
                        </>
                    )}
                    {page === "orders" && (
                        <section className="panel full-panel">
                            <div className="list-toolbar">
                                <label className="search-field">
                                    <Search size={18} />
                                    <input
                                        aria-label="Search orders"
                                        placeholder="Search orders…"
                                        value={search}
                                        onChange={(e) =>
                                            setSearch(e.target.value)
                                        }
                                    />
                                </label>
                                <label className="select-label">
                                    <SlidersHorizontal size={16} />
                                    <select
                                        aria-label="Filter order status"
                                        value={filter}
                                        onChange={(e) =>
                                            setFilter(e.target.value)
                                        }
                                    >
                                        <option value="all">
                                            All statuses
                                        </option>
                                        {[
                                            ...new Set(
                                                data.orders.map(
                                                    (o) => o.status,
                                                ),
                                            ),
                                        ].map((status) => (
                                            <option key={status} value={status}>
                                                {statusLabel(status)}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                            </div>
                            <div className="list-caption">
                                <span>
                                    {known("orders")
                                        ? `${orders.length} orders · latest 50`
                                        : "Orders unavailable"}
                                </span>
                                <span>Amount / status</span>
                            </div>
                            <OrderRows />
                        </section>
                    )}
                    {page === "activity" && (
                        <div className="activity-grid">
                            <section className="panel">
                                <div className="list-toolbar">
                                    <label className="search-field">
                                        <Search size={18} />
                                        <input
                                            aria-label="Search activity"
                                            placeholder="Search activity…"
                                            value={search}
                                            onChange={(e) =>
                                                setSearch(e.target.value)
                                            }
                                        />
                                    </label>
                                </div>
                                <div className="section-heading">
                                    <h2>
                                        {mode === "demo"
                                            ? "Activity stream"
                                            : "Audit stream"}
                                    </h2>
                                    <span className="muted">Latest 50</span>
                                </div>
                                {!known("events") ? (
                                    unavailable("activity")
                                ) : events.length ? (
                                    <EventRows />
                                ) : (
                                    <Empty title="No matching activity">
                                        {query
                                            ? "Try a different search."
                                            : "Your agent activity will appear here."}
                                    </Empty>
                                )}
                            </section>
                            <section className="panel conversations">
                                <div className="section-heading">
                                    <div>
                                        <h2>Conversations</h2>
                                        <p>
                                            Open a session to see its tool
                                            events.
                                        </p>
                                    </div>
                                    <MessageSquare size={20} />
                                </div>
                                {!known("conversations") ? (
                                    unavailable("conversations")
                                ) : data.conversations.length ? (
                                    data.conversations.map((conv) => (
                                        <button
                                            className="conversation-row"
                                            key={conv.id}
                                            onClick={() =>
                                                setConversation(conv)
                                            }
                                        >
                                            <MessageSquare size={18} />
                                            <div>
                                                <strong>
                                                    {human(conv.source)}{" "}
                                                    conversation
                                                </strong>
                                                <span>
                                                    {dateTime(
                                                        conv.updated_at ||
                                                            conv.created_at,
                                                    )}
                                                </span>
                                            </div>
                                            <ChevronRight size={16} />
                                        </button>
                                    ))
                                ) : (
                                    <Empty title="No conversations yet">
                                        Start a request on your watch.
                                    </Empty>
                                )}
                            </section>
                        </div>
                    )}
                    {page === "devices" && (
                        <div className="devices-grid">
                            <section className="panel">
                                <div className="section-heading">
                                    <div>
                                        <h2>Trusted devices</h2>
                                        <p>
                                            Devices linked to your ClawPay
                                            account.
                                        </p>
                                    </div>
                                    <Watch size={20} />
                                </div>
                                {!known("devices") ? (
                                    unavailable("devices")
                                ) : data.devices.length ? (
                                    data.devices.map((device) => (
                                        <div
                                            className="device-row"
                                            key={device.id}
                                        >
                                            <div className="device-art">
                                                <Watch
                                                    size={48}
                                                    strokeWidth={1.25}
                                                />
                                            </div>
                                            <div>
                                                <h3>
                                                    {device.device_name ||
                                                        "Watch"}
                                                </h3>
                                                <Status value={device.status} />
                                                <p>
                                                    {device.last_seen_at
                                                        ? `Last seen ${dateTime(device.last_seen_at)}`
                                                        : "Not seen yet"}
                                                </p>
                                                <span className="muted">
                                                    {device.key_algorithm} · Key
                                                    version {device.key_version}
                                                </span>
                                            </div>
                                        </div>
                                    ))
                                ) : (
                                    <Empty title="No watch connected">
                                        Pair your watch through the ClawPay
                                        setup flow.
                                    </Empty>
                                )}
                                <div className="security-note">
                                    <ShieldCheck size={19} />
                                    <p>
                                        Payment approval is signed on your
                                        watch. The dashboard cannot authorize a
                                        payment.
                                    </p>
                                </div>
                            </section>
                            <section className="panel">
                                <div className="section-heading">
                                    <h2>Payment connection</h2>
                                    <CreditCard size={20} />
                                </div>
                                {!known("wallets") ? (
                                    unavailable("wallets")
                                ) : data.wallets.length ? (
                                    data.wallets.map((wallet) => (
                                        <div
                                            className="wallet-row"
                                            key={wallet.id}
                                        >
                                            <div className="paypal-mark">P</div>
                                            <div>
                                                <h3>
                                                    {human(wallet.provider)}
                                                </h3>
                                                <p>
                                                    {wallet.payer_email_masked ||
                                                        "Account email unavailable"}
                                                </p>
                                            </div>
                                            <Status value={wallet.status} />
                                        </div>
                                    ))
                                ) : (
                                    <Empty title="No wallet connected">
                                        Complete the PayPal Sandbox setup before
                                        checkout.
                                    </Empty>
                                )}
                                <div className="payment-environment">
                                    <span className="status neutral">
                                        Sandbox
                                    </span>
                                    <p>
                                        This hackathon build uses PayPal
                                        Sandbox. Channel3 provides the catalog;
                                        real retailer fulfillment is not
                                        connected.
                                    </p>
                                </div>
                            </section>
                            <section className="panel passport-panel">
                                <div className="section-heading">
                                    <div>
                                        <h2>Agent passports</h2>
                                        <p>
                                            What each device may buy for you,
                                            signed by ClawPay.
                                        </p>
                                    </div>
                                    <BadgeCheck size={20} />
                                </div>
                                {!known("passports") ? (
                                    unavailable("passports")
                                ) : data.passports.length ? (
                                    data.passports.map((passport) => (
                                        <div
                                            className="passport-row"
                                            key={passport.id}
                                        >
                                            <div>
                                                <h3>
                                                    {data.devices.find(
                                                        (d) =>
                                                            d.id ===
                                                            passport.device_id,
                                                    )?.device_name ||
                                                        "Agent device"}
                                                </h3>
                                                <p>
                                                    Up to{" "}
                                                    {money(
                                                        passport.max_order_minor,
                                                        passport.currency,
                                                    )}{" "}
                                                    per order ·{" "}
                                                    {passport.merchants
                                                        .map(human)
                                                        .join(", ")}
                                                </p>
                                                <span className="muted">
                                                    {passport.status ===
                                                    "revoked"
                                                        ? `Revoked ${dateTime(passport.revoked_at || passport.created_at)}`
                                                        : `Expires ${dateTime(passport.expires_at)}`}{" "}
                                                    · Key {passport.key_id}
                                                </span>
                                            </div>
                                            <Status value={passport.status} />
                                        </div>
                                    ))
                                ) : (
                                    <Empty title="No passports issued">
                                        Devices without a passport follow your
                                        account spending limits only.
                                    </Empty>
                                )}
                                <div className="passport-decisions">
                                    <h3>Recent decisions</h3>
                                    {!known("receipts") ? (
                                        unavailable("decisions")
                                    ) : data.receipts.length ? (
                                        <ul>
                                            {data.receipts.map((receipt) => (
                                                <li key={receipt.id}>
                                                    <span
                                                        className={`status ${receipt.decision === "allowed" ? "good" : "warn"}`}
                                                    >
                                                        <span />
                                                        {receipt.decision ===
                                                        "allowed"
                                                            ? "Allowed"
                                                            : "Refused"}
                                                    </span>
                                                    <span>
                                                        {human(
                                                            receipt.merchant_code,
                                                        )}
                                                        {receipt.amount_minor
                                                            ? ` · ${money(receipt.amount_minor, receipt.currency)}`
                                                            : ""}
                                                    </span>
                                                    <span className="muted">
                                                        {passportReason(
                                                            receipt.reason,
                                                        )}{" "}
                                                        ·{" "}
                                                        {time(
                                                            receipt.created_at,
                                                        )}
                                                    </span>
                                                </li>
                                            ))}
                                        </ul>
                                    ) : (
                                        <p className="muted">
                                            No passport decisions yet.
                                        </p>
                                    )}
                                </div>
                                <div className="security-note">
                                    <ShieldCheck size={19} />
                                    <p>
                                        Every decision is a signed receipt you
                                        can verify with ClawPay’s public key.
                                        Passports are issued and revoked through
                                        the ClawPay API, not this read-only
                                        dashboard.
                                    </p>
                                </div>
                            </section>
                        </div>
                    )}
                    {page === "lab" && (
                        <div className="lab-layout">
                            <section
                                className="scenario-picker"
                                aria-label="Demo scenarios"
                            >
                                {scenarios.map((item, i) => (
                                    <button
                                        key={item.id}
                                        className={`scenario-option ${scenario === i ? "selected" : ""}`}
                                        onClick={() => {
                                            setScenario(i);
                                            setStep(-1);
                                        }}
                                    >
                                        <span className="scenario-icon">
                                            {i === 0 ? (
                                                <Mic size={20} />
                                            ) : i === 1 ? (
                                                <ShieldCheck size={20} />
                                            ) : (
                                                <RefreshCw size={20} />
                                            )}
                                        </span>
                                        <div>
                                            <strong>{item.title}</strong>
                                            <p>{item.description}</p>
                                        </div>
                                        <ChevronRight size={18} />
                                    </button>
                                ))}
                                <p className="footnote">
                                    Interactive simulations of supported backend
                                    flows. These replays never call agents or
                                    payment providers.
                                </p>
                            </section>
                            <section className="panel simulation">
                                <div className="section-heading">
                                    <h2>{scenarios[scenario].title}</h2>
                                    <span className="status neutral">
                                        Simulation
                                    </span>
                                </div>
                                <blockquote>
                                    {scenarios[scenario].prompt}
                                </blockquote>
                                <ol className="simulation-steps">
                                    {scenarios[scenario].steps.map(
                                        ([title, description], i) => (
                                            <li
                                                className={
                                                    i <= step ? "complete" : ""
                                                }
                                                key={title}
                                            >
                                                <span>
                                                    {i <= step ? (
                                                        <Check size={17} />
                                                    ) : (
                                                        i + 1
                                                    )}
                                                </span>
                                                <div>
                                                    <strong>{title}</strong>
                                                    <p>
                                                        {i <= step
                                                            ? description
                                                            : "Waiting for the next step"}
                                                    </p>
                                                </div>
                                            </li>
                                        ),
                                    )}
                                </ol>
                                <div className="simulation-actions">
                                    <button
                                        className="button primary"
                                        onClick={() => setStep(step + 1)}
                                        disabled={
                                            step >=
                                            scenarios[scenario].steps.length - 1
                                        }
                                    >
                                        {step < 0
                                            ? "Start replay"
                                            : step >=
                                                scenarios[scenario].steps
                                                    .length -
                                                    1
                                              ? "Replay complete"
                                              : "Next step"}
                                        <ArrowRight size={16} />
                                    </button>
                                    <button
                                        className="button"
                                        onClick={() => setStep(-1)}
                                        disabled={step < 0}
                                    >
                                        Reset
                                    </button>
                                    <span role="status">
                                        {step < 0
                                            ? "Ready when you are"
                                            : `${step + 1} of ${scenarios[scenario].steps.length} steps`}
                                    </span>
                                </div>
                            </section>
                        </div>
                    )}
                    <footer className="page-footer">
                        <span>
                            <ShieldCheck size={14} /> Approval stays on your
                            wrist.
                        </span>
                        <span>
                            {mode === "demo"
                                ? "Sample workspace"
                                : "Owner-scoped data"}
                            <i />
                            PayPal Sandbox
                        </span>
                    </footer>
                </main>
            </div>
            <nav className="bottom-nav" aria-label="Mobile navigation">
                {navigation.map((item) => (
                    <button
                        key={item.id}
                        aria-current={page === item.id ? "page" : undefined}
                        onClick={() => navigate(item.id)}
                        className={page === item.id ? "selected" : ""}
                    >
                        <item.icon size={20} />
                        <span>{item.label}</span>
                    </button>
                ))}
            </nav>
            <Modal
                open={login}
                onClose={() => {
                    setLogin(false);
                    setPassword("");
                }}
                label="Connect your workspace"
            >
                <p className="modal-intro">
                    Sign in with the Supabase account that owns your paired
                    watch. Your activity stays private.
                </p>
                {!configReady ? (
                    <p role="status">Loading connection settings…</p>
                ) : !liveAvailable ? (
                    <div className="error-banner">
                        <p>
                            Live mode isn’t configured on this host. Run the
                            local Hono server with your Supabase URL,
                            publishable key and ClawPay API origin. The demo is
                            ready to explore.
                        </p>
                    </div>
                ) : (
                    <form onSubmit={(e) => void signin(e)}>
                        <label className="field">
                            Email
                            <input
                                type="email"
                                autoComplete="username"
                                required
                                value={email}
                                onChange={(e) => setEmail(e.target.value)}
                            />
                        </label>
                        <label className="field">
                            Password
                            <input
                                type="password"
                                autoComplete="current-password"
                                required
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                            />
                        </label>
                        {authError && (
                            <p className="form-error" role="alert">
                                {authError}
                            </p>
                        )}
                        <button
                            className="button primary full-width"
                            type="submit"
                            disabled={authBusy}
                        >
                            {authBusy ? (
                                <LoaderCircle size={17} className="spin" />
                            ) : (
                                <LogIn size={17} />
                            )}{" "}
                            {authBusy ? "Signing in…" : "Sign in"}
                        </button>
                    </form>
                )}
                <p className="footnote">
                    Use your owner account, rather than a watch token or server
                    key. Ask your project administrator if you need account
                    access.
                </p>
            </Modal>
            <Modal
                open={Boolean(selected)}
                onClose={() => setSelected(null)}
                label="Order details"
                wide
            >
                {selected && (
                    <>
                        <div className="detail-summary">
                            <div>
                                <span className="muted">
                                    {(detail || selected).merchant ||
                                        "ClawPay order"}
                                </span>
                                <h3>
                                    {money(
                                        (detail || selected).total_minor,
                                        (detail || selected).currency,
                                    )}
                                </h3>
                                <Status value={(detail || selected).status} />
                            </div>
                            <ShoppingBag size={32} strokeWidth={1.5} />
                        </div>
                        <div className="detail-id">
                            <span>Order</span>
                            <code>{selected.id}</code>
                        </div>
                        {detailLoading && (
                            <p className="loading-line" role="status">
                                <LoaderCircle size={16} className="spin" />
                                Loading order trace…
                            </p>
                        )}
                        {detailError && (
                            <p className="form-error" role="alert">
                                {detailError}
                            </p>
                        )}
                        {detail?.items && (
                            <div className="detail-items">
                                {detail.items.map((item, i) => (
                                    <div key={i}>
                                        <span>
                                            {item.quantity} × {item.description}
                                        </span>
                                        <strong>
                                            {money(
                                                item.line_total_minor,
                                                detail.currency,
                                            )}
                                        </strong>
                                    </div>
                                ))}
                            </div>
                        )}
                        {(detail || selected).status ===
                            "awaiting_device_approval" && (
                            <div className="security-note">
                                <Watch size={20} />
                                <p>
                                    Open ClawPay on your watch to review and
                                    confirm this payment.
                                </p>
                            </div>
                        )}
                        <div
                            className="segmented"
                            role="group"
                            aria-label="Order trace view"
                        >
                            <button
                                className={
                                    traceTab === "timeline" ? "selected" : ""
                                }
                                onClick={() => setTraceTab("timeline")}
                            >
                                Timeline
                            </button>
                            <button
                                className={traceTab === "a2a" ? "selected" : ""}
                                onClick={() => setTraceTab("a2a")}
                            >
                                Agent messages <span>{messages.length}</span>
                            </button>
                        </div>
                        {(traceTab === "timeline" ? timeline : messages)
                            .length ? (
                            <EventRows
                                items={[
                                    ...(traceTab === "timeline"
                                        ? timeline
                                        : messages),
                                ].sort(
                                    (a, b) =>
                                        new Date(b.created_at).getTime() -
                                        new Date(a.created_at).getTime(),
                                )}
                            />
                        ) : (
                            !detailLoading && (
                                <Empty title="No events recorded">
                                    {detailError
                                        ? "The trace could not be loaded. Close this panel and try again."
                                        : "Events will appear as the order progresses."}
                                </Empty>
                            )
                        )}
                        <p className="footnote">
                            {mode === "demo"
                                ? "This order and its trace are sample data."
                                : "Sensitive tokens and provider payloads are excluded from this view."}
                        </p>
                    </>
                )}
            </Modal>
            <Modal
                open={Boolean(conversation)}
                onClose={() => setConversation(null)}
                label="Conversation events"
                wide
            >
                {conversation && (
                    <>
                        <p className="modal-intro">
                            {human(conversation.source)} ·{" "}
                            {dateTime(conversation.created_at)}
                        </p>
                        {conversationLoading ? (
                            <p role="status">Loading conversation…</p>
                        ) : conversationError ? (
                            <p className="form-error" role="alert">
                                {conversationError}
                            </p>
                        ) : conversationEvents.length ? (
                            <EventRows items={conversationEvents} />
                        ) : (
                            <Empty title="No events recorded">
                                Tool events will appear here after a request.
                            </Empty>
                        )}
                    </>
                )}
            </Modal>
            <Modal
                open={help}
                onClose={() => setHelp(false)}
                label="A little more control"
            >
                <div className="help-content">
                    <ShieldCheck size={32} />
                    <p>
                        Speak to ClawPay on your watch. The assistant
                        coordinates product search and quotes with a merchant
                        agent. You review the cart and approve it on your watch
                        before the PayPal agent can pay.
                    </p>
                    <p>
                        The dashboard is your read-only view of those decisions.
                        Payments run in PayPal Sandbox; product fulfillment is
                        not connected.
                    </p>
                    {session && (
                        <div className="account-info">
                            <strong>Signed in as</strong>
                            <span>{session.user.email || session.user.id}</span>
                            <button
                                className="button"
                                onClick={() => {
                                    void signout();
                                    setHelp(false);
                                }}
                            >
                                <LogOut size={16} />
                                Sign out
                            </button>
                        </div>
                    )}
                    <button
                        className="button"
                        onClick={() => {
                            setHelp(false);
                            navigate("lab");
                        }}
                    >
                        <FlaskConical size={17} />
                        Explore demo lab
                    </button>
                </div>
            </Modal>
        </div>
    );
}
// Minimal history router: the Hono servers already fall back to index.html for any page path.
function usePath() {
    const [path, setPath] = useState(window.location.pathname);
    useEffect(() => {
        const onPop = () => setPath(window.location.pathname);
        window.addEventListener("popstate", onPop);
        return () => window.removeEventListener("popstate", onPop);
    }, []);
    const go = useCallback((next: string) => {
        if (next !== window.location.pathname)
            window.history.pushState(null, "", next);
        window.scrollTo(0, 0);
        setPath(next);
    }, []);
    return [path, go] as const;
}
function Root() {
    const [path, go] = usePath();
    const dashboard = path.replace(/\/+$/, "") === "/dashboard";
    useEffect(() => {
        document.title = dashboard
            ? "ClawPay — Agent activity"
            : "ClawPay — Shop by voice, approve on your wrist";
    }, [dashboard]);
    return dashboard ? <App /> : <LandingPage navigate={go} />;
}
createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
        <Root />
    </React.StrictMode>,
);
