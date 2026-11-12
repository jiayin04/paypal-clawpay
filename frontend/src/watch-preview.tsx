import {
    CheckCircle2,
    LoaderCircle,
    MessageSquare,
    Mic,
    ShieldCheck,
    ShoppingBag,
} from "lucide-react";
import "./watch-preview.css";

type Screen = {
    kind: "idle" | "request" | "search" | "product" | "review" | "success" | "limit";
    title: string;
    message?: string;
    amount?: string;
    product?: string;
    action: string;
};

// Presentation-only stories. Controls advance the replay; they never record audio or pay.
const stories: Record<string, Screen[]> = {
    shopping: [
        { kind: "idle", title: "What can I find for you?", message: "Try a water bottle under $40.", action: "Talk" },
        { kind: "request", title: "Request sent", message: "“Find me a water bottle under $40.”", action: "Continue" },
        { kind: "search", title: "Finding your bottle", message: "Checking products and prices on Channel3.", action: "See result" },
        { kind: "product", title: "A match for you", amount: "USD 34.00", product: "Insulated water bottle", message: "Channel3 · Within your budget", action: "Review" },
        { kind: "review", title: "Review payment", amount: "USD 34.00", product: "1 × Insulated water bottle", message: "Channel3 Marketplace", action: "Confirm" },
        { kind: "success", title: "Payment complete", amount: "USD 34.00", message: "Approved on your watch.", action: "Try again" },
    ],
    budget: [
        { kind: "idle", title: "Your budget comes first.", message: "Try the $120 headphones.", action: "Talk" },
        { kind: "request", title: "Request sent", message: "“Buy this $120 pair of headphones.”", action: "Continue" },
        { kind: "product", title: "Review the price", amount: "USD 120.00", product: "Wireless headphones", message: "Your per-order limit is $100.", action: "Check limit" },
        { kind: "limit", title: "Above your limit", message: "This cart is $20 over your $100 limit.", action: "See outcome" },
        { kind: "limit", title: "Payment stopped", message: "Your limit held. No payment was made.", action: "Try again" },
    ],
    recovery: [
        { kind: "idle", title: "Need a payment update?", message: "Ask about your last order.", action: "Talk" },
        { kind: "request", title: "Taking a little longer", message: "“Did my payment go through?”", action: "Check status" },
        { kind: "search", title: "Checking with PayPal", message: "Checking the original payment before any retry.", action: "Continue" },
        { kind: "search", title: "Verifying the result", message: "Matching your original order and payment.", action: "See result" },
        { kind: "success", title: "Payment confirmed", amount: "USD 34.00", message: "Original payment found. No repeat charge.", action: "Try again" },
    ],
};

export function WatchPreview({
    scenario,
    step,
    onAdvance,
    onReset,
}: {
    scenario: string;
    step: number;
    onAdvance: () => void;
    onReset: () => void;
}) {
    const sequence = stories[scenario] || stories.shopping;
    const screen = sequence[Math.max(0, Math.min(step + 1, sequence.length - 1))];
    const complete = step >= sequence.length - 2;
    const Icon = {
        idle: Mic,
        request: MessageSquare,
        search: LoaderCircle,
        product: ShoppingBag,
        review: ShieldCheck,
        success: CheckCircle2,
        limit: ShieldCheck,
    }[screen.kind];
    return (
        <figure className="watch-preview" aria-label="Apple Watch preview">
            <div className="watch-preview-case">
                <button
                    className="watch-preview-crown"
                    aria-label="Reset watch preview"
                    title="Reset watch preview"
                    onClick={onReset}
                />
                <div className="watch-preview-screen">
                    <div className="watch-preview-bar">
                        <span>ClawPay</span>
                        <span>9:41</span>
                    </div>
                    <div className={`watch-preview-content ${screen.kind}`} aria-live="polite" aria-atomic="true">
                        <Icon size={25} className={screen.kind === "search" ? "spin" : ""} aria-hidden="true" />
                        <h3>{screen.title}</h3>
                        {(screen.amount || screen.product) && (
                            <div className="watch-preview-cart">
                                {screen.amount && <strong>{screen.amount}</strong>}
                                {screen.product && <span>{screen.product}</span>}
                            </div>
                        )}
                        {screen.message && <p>{screen.message}</p>}
                        {screen.kind === "success" && <span className="watch-preview-receipt">Demo receipt</span>}
                    </div>
                    <button
                        className={`watch-preview-action ${complete ? "again" : ""}`}
                        onClick={complete ? onReset : onAdvance}
                    >
                        {screen.kind === "idle" && <Mic size={17} aria-hidden="true" />}
                        {screen.action}
                    </button>
                </div>
            </div>
            <figcaption>
                <strong>Apple Watch preview</strong>
                <span>{step < 0 ? "Tap Talk to start the story." : "Tap the watch or use the replay controls."}</span>
            </figcaption>
        </figure>
    );
}
