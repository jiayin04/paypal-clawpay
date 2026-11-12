import { chromium } from "@playwright/test";
import assert from "node:assert/strict";
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    reducedMotion: "reduce",
});
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
// Enter through the landing page and return through both the UI and browser history.
await page.goto("http://127.0.0.1:8787/");
await page.getByRole("banner").getByRole("link", { name: "Open the demo", exact: true }).click();
await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
await page.getByRole("link", { name: "Back to website", exact: true }).click();
await page.locator(".lp h1").waitFor();
assert.equal(new URL(page.url()).pathname, "/");
await page.goBack();
await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
await page.goForward();
await page.locator(".lp h1").waitFor();
await page.goto("http://127.0.0.1:8787/dashboard");
await page.getByRole("link", { name: "ClawPay", exact: true }).click();
await page.locator(".lp h1").waitFor();
await page.goto("http://127.0.0.1:8787/dashboard");
await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
await page.screenshot({
    path: "test-results/ui/desktop.png",
    fullPage: true,
});
const main = page.getByRole("main");
await main.getByRole("button", { name: "Review order" }).click();
await page.getByRole("dialog", { name: "Order details" }).waitFor();
await page.getByRole("button", { name: /Agent messages/ }).click();
assert.equal(
    await page
        .getByRole("dialog")
        .getByText("Cart quote", { exact: true })
        .count(),
    1,
);
await page.keyboard.press("Escape");
await page
    .getByRole("navigation", { name: "Main navigation", exact: true })
    .getByRole("button", { name: "Orders" })
    .click();
await page.getByRole("textbox", { name: "Search orders" }).fill("zzzzzz");
await page.getByRole("heading", { name: "No matching orders" }).waitFor();
await page.getByRole("textbox", { name: "Search orders" }).fill("");
await page
    .getByRole("combobox", { name: "Filter order status" })
    .selectOption("paid");
assert.equal(await main.locator(".order-row").count(), 1);
await page
    .getByRole("navigation", { name: "Main navigation", exact: true })
    .getByRole("button", { name: "Activity" })
    .click();
await page.getByRole("button", { name: /Watch conversation/ }).click();
await page.getByRole("dialog", { name: "Conversation activity" }).waitFor();
await page.keyboard.press("Escape");
await page
    .getByRole("navigation", { name: "Main navigation", exact: true })
    .getByRole("button", { name: "Devices" })
    .click();
assert.equal(await main.locator(".device-row").count(), 1);
await page
    .getByRole("navigation", { name: "Main navigation", exact: true })
    .getByRole("button", { name: "Demo lab" })
    .click();
const watchPreview = page.getByRole("figure", { name: "Apple Watch preview" });
const replayWrites = [];
const recordReplayWrite = (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
        replayWrites.push(request.url());
};
page.on("request", recordReplayWrite);
for (const name of [
    "Voice to checkout",
    "A budget that holds",
    "Resolve the unknown",
]) {
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("button", { name: "Start replay" }).click();
    while (
        await page
            .getByRole("button", { name: "Next step", exact: true })
            .count()
    )
        await page
            .getByRole("button", { name: "Next step", exact: true })
            .click();
    assert.ok(
        await page
            .getByRole("button", { name: "Replay complete" })
            .isDisabled(),
    );
    await watchPreview.getByRole("heading", {
        name: {
            "Voice to checkout": "Payment complete",
            "A budget that holds": "Payment stopped",
            "Resolve the unknown": "Payment confirmed",
        }[name],
        exact: true,
    }).waitFor();
    await page.getByRole("button", { name: "Reset", exact: true }).click();
    assert.ok(await watchPreview.getByRole("button", { name: "Talk", exact: true }).isVisible());
}
// Watch actions and replay controls must use the same state without submitting payments.
await page.getByRole("button", { name: /Voice to checkout/ }).click();
for (const action of ["Talk", "Continue", "See result", "Review"])
    await watchPreview.getByRole("button", { name: action, exact: true }).click();
await watchPreview.getByRole("heading", { name: "Review payment" }).waitFor();
assert.equal(await page.getByText("4 of 5 steps", { exact: true }).count(), 1);
assert.equal(await watchPreview.getByRole("heading", { name: "Payment complete" }).count(), 0);
await watchPreview.getByRole("button", { name: "Confirm", exact: true }).click();
assert.ok(await page.getByRole("button", { name: "Replay complete" }).isDisabled());
await watchPreview.getByRole("button", { name: "Reset watch preview" }).click();
assert.ok(await page.getByRole("button", { name: "Start replay" }).isVisible());
await page.getByRole("button", { name: /A budget that holds/ }).click();
for (const action of ["Talk", "Continue", "Check limit"])
    await watchPreview.getByRole("button", { name: action, exact: true }).click();
await watchPreview.getByRole("heading", { name: "Above your limit" }).waitFor();
assert.equal(await watchPreview.getByRole("button", { name: "Confirm", exact: true }).count(), 0);
await watchPreview.getByRole("button", { name: "See outcome" }).click();
await watchPreview.getByRole("heading", { name: "Payment stopped" }).waitFor();
await watchPreview.getByRole("button", { name: "Try again" }).click();
await page.getByRole("button", { name: /Resolve the unknown/ }).click();
for (const action of ["Talk", "Check status", "Continue", "See result"])
    await watchPreview.getByRole("button", { name: action, exact: true }).click();
await watchPreview.getByRole("heading", { name: "Payment confirmed" }).waitFor();
await page.getByRole("button", { name: /Voice to checkout/ }).click();
assert.ok(await watchPreview.getByRole("button", { name: "Talk", exact: true }).isVisible());
assert.equal(await watchPreview.getByText("Demo receipt").count(), 0);
assert.deepEqual(replayWrites, []);
page.off("request", recordReplayWrite);
await page.screenshot({
    path: "test-results/ui/lab.png",
    fullPage: true,
});
for (const width of [390, 768, 1024, 1280, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    if (width === 390) {
        await page.getByRole("link", { name: "Back to website", exact: true }).click();
        await page.locator(".lp h1").waitFor();
        await page.getByRole("banner").getByRole("link", { name: "Open the demo", exact: true }).click();
        await page.getByRole("heading", { name: "Overview", exact: true }).waitFor();
    }
    const nav = page.getByRole("navigation", {
        name: width <= 760 ? "Mobile navigation" : "Main navigation",
        exact: true,
    });
    for (const name of [
        "Overview",
        "Orders",
        "Activity",
        "Devices",
        "Demo lab",
    ]) {
        await nav.getByRole("button", { name }).click();
        assert.ok(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
            `Overflow ${name} at ${width}`,
        );
        if (width === 390 && name === "Overview")
            await page.screenshot({
                path: "test-results/ui/mobile.png",
                fullPage: true,
            });
        if (width === 390 && name === "Orders") {
            await main.locator(".order-row").first().click();
            assert.ok(
                await page.evaluate(() => {
                    const r = document
                        .querySelector("dialog[open]")
                        .getBoundingClientRect();
                    return r.left >= 0 && r.right <= window.innerWidth;
                }),
                "Mobile dialog fits",
            );
            await page.screenshot({
                path: "test-results/ui/mobile-order.png",
                fullPage: true,
            });
            await page.keyboard.press("Escape");
        }
    }
}
// A completely separate account session and deterministic mock backend exercise live auth/error/empty states.
const publicConfig = await (
    await fetch("http://127.0.0.1:8787/api/config")
).json();
const supabaseUrl = publicConfig.supabaseUrl || "https://demo.supabase.co";
const live = await browser.newContext({
    viewport: { width: 1280, height: 900 },
});
const livePage = await live.newPage();
livePage.on("pageerror", (error) => errors.push(error.message));
await livePage.route("**/api/config", (route) =>
    route.fulfill({
        json: {
            supabaseUrl,
            supabaseKey: "public-test-key",
            liveAvailable: true,
        },
    }),
);
const payload = Buffer.from(
    JSON.stringify({
        sub: "user-test",
        exp: Math.floor(Date.now() / 1000) + 3600,
        aud: "authenticated",
        role: "authenticated",
    }),
).toString("base64url");
await livePage.route(supabaseUrl + "/auth/v1/token**", (route) =>
    route.fulfill({
        json: {
            access_token: `eyJhbGciOiJIUzI1NiJ9.${payload}.signature`,
            refresh_token: "test-refresh",
            token_type: "bearer",
            expires_in: 3600,
            user: {
                id: "user-test",
                email: "owner@example.com",
                app_metadata: {},
                user_metadata: {},
                aud: "authenticated",
                created_at: new Date().toISOString(),
            },
        },
    }),
);
let fail = true;
await livePage.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (fail)
        return route.fulfill({
            status: 502,
            json: {
                error: { message: "Test backend offline. Retry to reconnect." },
            },
        });
    const key = path.endsWith("/summary") ? "summary" : path.split("/").at(-1);
    const keys = {
        "audit-events": "audit_events",
        "payment-methods": "payment_methods",
    };
    return route.fulfill({
        json:
            key === "summary"
                ? {
                      currency: "USD",
                      limit_minor: 50000,
                      committed_minor: 0,
                      reserved_minor: 0,
                      remaining_minor: 50000,
                  }
                : { [keys[key] || key]: [] },
    });
});
await livePage.goto("http://127.0.0.1:8787/dashboard");
await livePage.getByRole("button", { name: "Connect live" }).click();
await livePage.getByLabel("Email", { exact: true }).fill("owner@example.com");
await livePage.getByLabel("Password", { exact: true }).fill("example-password");
await livePage.getByRole("button", { name: "Sign in", exact: true }).click();
await livePage
    .getByRole("alert")
    .filter({ hasText: "Test backend offline" })
    .waitFor();
await livePage.getByRole("heading", { name: "Orders unavailable" }).waitFor();
await livePage.getByRole("heading", { name: "Activity unavailable" }).waitFor();
assert.equal(
    await livePage.getByText("You’re all caught up", { exact: true }).count(),
    0,
);
assert.equal(
    await livePage.getByRole("heading", { name: "No orders yet" }).count(),
    0,
);
assert.equal(
    await livePage.getByRole("heading", { name: "No activity yet" }).count(),
    0,
);
await livePage.screenshot({
    path: "test-results/ui/live-initial-error.png",
    fullPage: true,
});
fail = false;
await livePage.getByRole("button", { name: "Retry", exact: true }).click();
await livePage.getByText("Your activity, securely connected.").waitFor();
await livePage.getByRole("heading", { name: "No orders yet" }).waitFor();
assert.equal(await livePage.getByText("$26.00", { exact: true }).count(), 0);
fail = true;
await livePage.getByRole("button", { name: "Refresh activity" }).click();
await livePage
    .getByRole("alert")
    .filter({ hasText: "Test backend offline" })
    .waitFor();
await livePage.screenshot({
    path: "test-results/ui/live-error.png",
    fullPage: true,
});
fail = false;
await livePage.getByRole("button", { name: "Retry", exact: true }).click();
await livePage.getByRole("alert").waitFor({ state: "hidden" });
// Supabase broadcasts authentication changes from other tabs, including expiry/sign-out.
// Owner changes must clear retained data even when the next owner's reads all fail.
const broadcastAuth = async (event, session) => {
    await livePage.evaluate(({ event, session }) => {
        const key = Object.keys(sessionStorage).find((key) => key.startsWith("sb-") && key.endsWith("-auth-token"));
        if (!key) throw new Error("Expected a Supabase session");
        const channel = new BroadcastChannel(key);
        channel.postMessage({ event, session });
        channel.close();
    }, { event, session });
};
fail = true;
await broadcastAuth("SIGNED_IN", {
    access_token: "other-owner-token",
    refresh_token: "other-owner-refresh",
    token_type: "bearer",
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: { id: "other-owner", email: "other@example.com" },
});
await livePage.getByRole("heading", { name: "Orders unavailable" }).waitFor();
assert.equal(await livePage.getByRole("heading", { name: "No orders yet" }).count(), 0);
await broadcastAuth("SIGNED_OUT", null);
await livePage.getByText("You’re exploring the demo.").waitFor();
fail = false;
await livePage.getByRole("button", { name: "Connect live" }).click();
await livePage.getByLabel("Email", { exact: true }).fill("owner@example.com");
await livePage.getByLabel("Password", { exact: true }).fill("example-password");
await livePage.getByRole("button", { name: "Sign in", exact: true }).click();
await livePage.getByText("Your activity, securely connected.").waitFor();
await livePage.getByRole("button", { name: "Account details" }).click();
await livePage.getByRole("button", { name: "Sign out", exact: true }).click();
await livePage.getByText("You’re exploring the demo.").waitFor();
assert.deepEqual(errors, []);
console.log(
    "UI passed: 5 screens at 5 widths, traces, filters, 3 replays, authenticated initial-error/empty/stale states, error recovery, owner switching and sign-out broadcasts; no page errors.",
);
await browser.close();
