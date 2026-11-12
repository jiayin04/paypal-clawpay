import React, { useEffect, useRef, useState } from "react";
import {
    ArrowRight,
    BadgeCheck,
    Check,
    CreditCard,
    Eye,
    FlaskConical,
    Gauge,
    KeyRound,
    Mic,
    Search,
    ShieldCheck,
    ShoppingBag,
    Sparkles,
    Watch,
} from "lucide-react";
import "./landing.css";

type Navigate = (path: string) => void;

const steps = [
    {
        icon: Mic,
        title: "Ask your watch",
        body: "Tap Talk and say what you need. ClawPay sends your request after a short pause.",
    },
    {
        icon: Sparkles,
        title: "ClawPay plans it",
        body: "The assistant works out what you asked for and coordinates the agents that can help.",
    },
    {
        icon: ShoppingBag,
        title: "A merchant agent quotes",
        body: "Product search runs on Channel3 catalog data and comes back as a priced cart.",
    },
    {
        icon: CreditCard,
        title: "You approve, PayPal pays",
        body: "You review the cart on your wrist. A separate PayPal agent pays only after your signed approval.",
    },
];

const features = [
    {
        icon: Mic,
        title: "Voice to checkout",
        body: "Go from a spoken request to a reviewed cart without opening a phone or a browser.",
    },
    {
        icon: Watch,
        title: "Approval on your wrist",
        body: "Every payment needs an explicit confirmation on your paired device, signed by that device.",
    },
    {
        icon: Gauge,
        title: "Built-in spending limits",
        body: "Orders are capped at USD 100 each and USD 500 per day. ClawPay enforces them, not the agent.",
    },
    {
        icon: Search,
        title: "Real catalog search",
        body: "The merchant agent searches real Channel3 product data to build your quote.",
    },
    {
        icon: Eye,
        title: "A dashboard that only watches",
        body: "Follow orders, agent messages and audit events on the web. The dashboard can't approve payments.",
    },
    {
        icon: FlaskConical,
        title: "Explore before you connect",
        body: "Follow a shopping journey from your request to approval. No purchases or payments are made.",
    },
];

const safeguards = [
    "Payment confirmation happens on the watch, never in the browser.",
    "Uncertain payment results are reconciled with PayPal before an order is marked paid.",
    "The web dashboard is read-only and only shows your own account's records.",
    "Your payment details stay protected and are never exposed in the dashboard.",
];

const prefersReducedMotion = () =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Stagger index for reveal transitions, read by CSS as --i.
const stagger = (i: number) => ({ "--i": i }) as React.CSSProperties;

/**
 * Scroll motion for the landing page:
 * - reveals [data-reveal] elements once as they enter the viewport
 * - exposes scroll position (--lp-scroll) and page progress (--lp-progress) to CSS
 * - marks the nav as scrolled once the page leaves the top
 */
function useScrollMotion(root: React.RefObject<HTMLDivElement | null>) {
    useEffect(() => {
        const el = root.current;
        if (!el) return;
        const targets = el.querySelectorAll<HTMLElement>("[data-reveal]");
        let observer: IntersectionObserver | undefined;
        if (prefersReducedMotion() || !("IntersectionObserver" in window)) {
            targets.forEach((t) => t.classList.add("is-visible"));
        } else {
            observer = new IntersectionObserver(
                (entries) => {
                    for (const entry of entries) {
                        if (!entry.isIntersecting) continue;
                        entry.target.classList.add("is-visible");
                        observer?.unobserve(entry.target);
                    }
                },
                { threshold: 0.15, rootMargin: "0px 0px -8% 0px" },
            );
            targets.forEach((t) => observer!.observe(t));
        }

        let frame = 0;
        const update = () => {
            frame = 0;
            const y = window.scrollY;
            const max =
                document.documentElement.scrollHeight - window.innerHeight;
            el.style.setProperty(
                "--lp-progress",
                String(max > 0 ? Math.min(1, y / max) : 0),
            );
            el.style.setProperty("--lp-scroll", String(Math.min(y, 900)));
            el.classList.toggle("lp-scrolled", y > 8);
        };
        const onScroll = () => {
            if (!frame) frame = requestAnimationFrame(update);
        };
        update();
        window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll);
        return () => {
            observer?.disconnect();
            cancelAnimationFrame(frame);
            window.removeEventListener("scroll", onScroll);
            window.removeEventListener("resize", onScroll);
        };
    }, [root]);
}

/** Counts up to `to` once the number scrolls into view. */
function CountUp({ to, duration = 1200 }: { to: number; duration?: number }) {
    const ref = useRef<HTMLSpanElement>(null);
    const [value, setValue] = useState(() =>
        prefersReducedMotion() ? to : 0,
    );
    useEffect(() => {
        const el = ref.current;
        if (!el || prefersReducedMotion() || !("IntersectionObserver" in window)) {
            setValue(to);
            return;
        }
        let frame = 0;
        const observer = new IntersectionObserver(
            ([entry]) => {
                if (!entry.isIntersecting) return;
                observer.disconnect();
                const start = performance.now();
                const tick = (now: number) => {
                    const t = Math.min(1, (now - start) / duration);
                    setValue(Math.round(to * (1 - Math.pow(1 - t, 3))));
                    if (t < 1) frame = requestAnimationFrame(tick);
                };
                frame = requestAnimationFrame(tick);
            },
            { threshold: 0.6 },
        );
        observer.observe(el);
        return () => {
            observer.disconnect();
            cancelAnimationFrame(frame);
        };
    }, [to, duration]);
    return (
        <span ref={ref}>{value}</span>
    );
}

function DemoLink({
    navigate,
    className,
    children,
}: {
    navigate: Navigate;
    className: string;
    children: React.ReactNode;
}) {
    return (
        <a
            className={className}
            href="/dashboard"
            onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0)
                    return;
                e.preventDefault();
                navigate("/dashboard");
            }}
        >
            {children}
        </a>
    );
}

function HeroVisual() {
    const trace = [
        "Request understood",
        "Quote from merchant agent",
        "Within daily limit",
    ];
    return (
        <div className="lp-visual" aria-hidden="true">
            <div className="lp-watch-wrap">
                <div className="lp-rings">
                    <span />
                    <span />
                    <span />
                    <span className="lp-ripple" />
                    <span className="lp-ripple" />
                </div>
                <div className="lp-watch">
                    <div className="lp-watch-crown" />
                    <div className="lp-watch-screen">
                        <span className="lp-watch-label">Review payment</span>
                        <strong className="lp-watch-item">
                            Everyday insulated water bottle
                        </strong>
                        <span className="lp-watch-merchant">Channel3</span>
                        <span className="lp-watch-total">$34.00</span>
                        <span className="lp-watch-confirm">
                            <Check size={14} />
                            Confirm
                        </span>
                    </div>
                </div>
            </div>
            <div className="lp-trace-wrap">
                <div className="lp-trace">
                    <span className="lp-trace-title">Agent activity</span>
                    <ol>
                        {trace.map((item, i) => (
                            <li key={item} style={stagger(i)}>
                                <span className="lp-trace-dot done" />
                                {item}
                            </li>
                        ))}
                        <li style={stagger(trace.length)}>
                            <span className="lp-trace-dot wait" />
                            Waiting for your watch
                        </li>
                    </ol>
                </div>
            </div>
        </div>
    );
}

export function LandingPage({ navigate }: { navigate: Navigate }) {
    const root = useRef<HTMLDivElement>(null);
    useScrollMotion(root);
    return (
        <div className="lp" ref={root}>
            <a className="skip-link" href="#lp-main">
                Skip to content
            </a>
            <header className="lp-nav">
                <div className="lp-container lp-nav-inner">
                    <a className="lp-brand" href="/">
                        <img src="/favicon.svg" alt="" />
                        <span>ClawPay</span>
                    </a>
                    <nav aria-label="Page sections" className="lp-nav-links">
                        <a href="#how">How it works</a>
                        <a href="#features">Features</a>
                        <a href="#security">Security</a>
                    </nav>
                    <DemoLink navigate={navigate} className="lp-button small">
                        Open the demo
                    </DemoLink>
                </div>
                <span className="lp-progress" aria-hidden="true" />
            </header>

            <main id="lp-main" tabIndex={-1}>
                <section className="lp-hero">
                    <div className="lp-container lp-hero-inner">
                        <div className="lp-hero-copy">
                            <span className="lp-eyebrow">
                                <Watch size={14} />
                                Agent-led shopping · Approved by you
                            </span>
                            <h1>
                                <span className="lp-line">Shop by voice.</span>
                                <span className="lp-line">
                                    Approve on your wrist.
                                </span>
                            </h1>
                            <p className="lp-lede">
                                ClawPay turns a spoken request on your watch
                                into a ready-to-pay cart. A team of agents finds
                                the product and prepares the PayPal checkout.
                                Nothing is paid until you confirm on your watch.
                            </p>
                            <div className="lp-actions">
                                <DemoLink
                                    navigate={navigate}
                                    className="lp-button"
                                >
                                    Open the demo
                                    <ArrowRight size={16} />
                                </DemoLink>
                                <a className="lp-button secondary" href="#how">
                                    How it works
                                </a>
                            </div>
                            <p className="lp-note">
                                Explore the experience. No purchases or payments
                                are made.
                            </p>
                        </div>
                        <HeroVisual />
                    </div>
                </section>

                <section className="lp-facts" aria-label="Key facts">
                    <div className="lp-container lp-facts-inner">
                        <div data-reveal style={stagger(0)}>
                            <strong>
                                USD <CountUp to={100} />
                            </strong>
                            <span>limit per order</span>
                        </div>
                        <div data-reveal style={stagger(1)}>
                            <strong>
                                USD <CountUp to={500} />
                            </strong>
                            <span>limit per day</span>
                        </div>
                        <div data-reveal style={stagger(2)}>
                            <strong>1 signed approval</strong>
                            <span>required for every payment</span>
                        </div>
                        <div data-reveal style={stagger(3)}>
                            <strong>Read-only</strong>
                            <span>web dashboard</span>
                        </div>
                    </div>
                </section>

                <section className="lp-section lp-how" id="how">
                    <div className="lp-container">
                        <div className="lp-section-head" data-reveal>
                            <span className="lp-kicker">How it works</span>
                            <h2>One request. A team of agents.</h2>
                            <p>
                                Each agent does one job. You make the final
                                call.
                            </p>
                        </div>
                        {/* Track showing the request passing from agent to agent */}
                        <div className="lp-rail" data-reveal aria-hidden="true">
                            <span className="lp-rail-line">
                                <span className="lp-rail-fill" />
                                <span className="lp-rail-runner">
                                    <span />
                                </span>
                            </span>
                            {steps.map((step, i) => (
                                <span
                                    key={step.title}
                                    className="lp-rail-node"
                                    style={stagger(i)}
                                >
                                    {i + 1}
                                </span>
                            ))}
                        </div>
                        <ol className="lp-steps" data-reveal>
                            {steps.map((step, i) => (
                                <li
                                    key={step.title}
                                    className="lp-step"
                                    style={stagger(i)}
                                >
                                    <div className="lp-step-top">
                                        <span className="lp-icon">
                                            <step.icon size={20} />
                                        </span>
                                        <span className="lp-step-number">
                                            Step {i + 1}
                                        </span>
                                    </div>
                                    <h3>{step.title}</h3>
                                    <p>{step.body}</p>
                                </li>
                            ))}
                        </ol>
                    </div>
                </section>

                <section className="lp-section alt lp-features" id="features">
                    <div className="lp-container">
                        <div className="lp-section-head" data-reveal>
                            <span className="lp-kicker">Features</span>
                            <h2>Your agents do the legwork.</h2>
                            <p>
                                ClawPay handles search, quoting and payment.
                                You keep control of the money.
                            </p>
                        </div>
                        <div className="lp-grid">
                            {features.map((feature, i) => (
                                <article
                                    key={feature.title}
                                    className="lp-card"
                                    data-reveal
                                    style={stagger(i % 3)}
                                >
                                    <span className="lp-icon">
                                        <feature.icon size={20} />
                                    </span>
                                    <h3>{feature.title}</h3>
                                    <p>{feature.body}</p>
                                    <feature.icon
                                        className="lp-card-mark"
                                        size={104}
                                        strokeWidth={1}
                                        aria-hidden="true"
                                    />
                                </article>
                            ))}
                        </div>
                    </div>
                </section>

                <section className="lp-section" id="security">
                    <div className="lp-container lp-security">
                        <div
                            className="lp-section-head left"
                            data-reveal
                            data-reveal-from="left"
                        >
                            <span className="lp-kicker">Security</span>
                            <h2>The money only moves when you say so.</h2>
                            <p>
                                Agents can search, quote and prepare a payment.
                                Only your paired watch can approve it.
                            </p>
                        </div>
                        <div className="lp-safeguards" data-reveal>
                            <div className="lp-safeguards-head">
                                <ShieldCheck size={20} />
                                <strong>Safeguards</strong>
                            </div>
                            <ul>
                                {safeguards.map((item, i) => (
                                    <li key={item} style={stagger(i + 1)}>
                                        <BadgeCheck size={18} />
                                        <span>{item}</span>
                                    </li>
                                ))}
                            </ul>
                            <p className="lp-footnote">
                                <KeyRound size={14} />
                                This interactive preview makes no purchases or
                                payments. No items are shipped.
                            </p>
                        </div>
                    </div>
                </section>

                <section className="lp-cta">
                    <div className="lp-container">
                        <div className="lp-cta-box" data-reveal data-reveal-from="scale">
                            <div>
                                <h2>See ClawPay in action.</h2>
                                <p>
                                    Explore sample orders, agent activity and
                                    three step-by-step scenarios in the
                                    dashboard demo.
                                </p>
                            </div>
                            <DemoLink navigate={navigate} className="lp-button">
                                Open the demo
                                <ArrowRight size={16} />
                            </DemoLink>
                        </div>
                    </div>
                </section>
            </main>

            <footer className="lp-footer">
                <div className="lp-container lp-footer-inner">
                    <a className="lp-brand" href="/">
                        <img src="/favicon.svg" alt="" />
                        <span>ClawPay</span>
                    </a>
                    <nav aria-label="Footer" className="lp-footer-links">
                        <a href="#how">How it works</a>
                        <a href="#features">Features</a>
                        <a href="#security">Security</a>
                        <DemoLink navigate={navigate} className="">
                            Dashboard demo
                        </DemoLink>
                    </nav>
                    <span className="lp-footer-caption">
                        ClawPay · You stay in control
                    </span>
                </div>
            </footer>
        </div>
    );
}
