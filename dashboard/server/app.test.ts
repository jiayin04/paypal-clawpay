import { test } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "./app.ts";
test("read-only proxy preserves owner auth and query, strips untrusted headers, blocks mutations and redirects", async () => {
    const calls: { url: string; options?: RequestInit }[] = [];
    const app = createApp(
        {
            apiOrigin: "http://localhost:8000",
            supabaseUrl: "https://example.supabase.co",
            supabaseKey: "public-key",
        },
        (async (url, options) => {
            calls.push({ url: String(url), options });
            return Response.json({ orders: [] });
        }) as typeof fetch,
    );
    const headers = {
        Authorization: "Bearer user.jwt",
        Cookie: "secret=bad",
        "X-Admin-Token": "bad",
    };
    const res = await app.request(
        "/api/v1/orders?limit=50&before=2026-11-01&url=https://evil.test",
        { headers },
    );
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.equal(
        calls[0].url,
        "http://localhost:8000/api/v1/orders?limit=50&before=2026-11-01",
    );
    assert.deepEqual(calls[0].options?.headers, {
        Authorization: "Bearer user.jwt",
        Accept: "application/json",
    });
    assert.equal(calls[0].options?.redirect, "error");
    for (const path of [
        "/api/v1/orders/id/cancel",
        "/api/v1/payment-methods/paypal/setup",
    ])
        assert.equal(
            (await app.request(path, { method: "POST", headers })).status,
            405,
        );
    assert.equal((await app.request("/api/v1/admin", { headers })).status, 404);
    assert.equal((await app.request("/api/v1/orders")).status, 401);
    assert.equal(
        (
            await app.request("/api/v1/orders", {
                headers: { Authorization: "Bearer cpd_secret" },
            })
        ).status,
        401,
    );
    const config = await (await app.request("/api/config")).json();
    assert.deepEqual(Object.keys(config).sort(), [
        "liveAvailable",
        "supabaseKey",
        "supabaseUrl",
    ]);
    assert.equal(calls.length, 1);
});
test("backend errors remain visible and connection errors are recoverable", async () => {
    const config = {
        apiOrigin: "http://localhost:8000",
        supabaseUrl: "",
        supabaseKey: "",
    };
    const app = createApp(config, (async () =>
        Response.json(
            { error: { message: "Expired session" } },
            { status: 401 },
        )) as typeof fetch);
    assert.equal(
        (
            await app.request("/api/v1/devices", {
                headers: { Authorization: "Bearer jwt" },
            })
        ).status,
        401,
    );
    const offline = createApp(config, (async () => {
        throw new Error("private host details");
    }) as typeof fetch);
    const response = await offline.request("/api/v1/orders", {
        headers: { Authorization: "Bearer jwt" },
    });
    assert.equal(response.status, 502);
    assert.ok(!(await response.text()).includes("private host"));
});

test("hosted Hono Worker serves SPA assets and keeps unavailable live mode explicit", async () => {
    const { createWorker } = await import("./worker.ts");
    const worker = createWorker({
        "/index.html": { body: "<html>ClawPay</html>", type: "text/html" },
        "/assets/site.js": {
            body: 'console.log("public")',
            type: "application/javascript",
        },
    });
    const page = await worker.fetch(
        new Request("https://dashboard.example/overview"),
        {},
    );
    assert.equal(page.status, 200);
    assert.match(await page.text(), /ClawPay/);
    assert.ok(page.headers.get("content-security-policy"));
    assert.equal(
        (
            await worker.fetch(
                new Request("https://dashboard.example/api/unknown"),
                {},
            )
        ).status,
        404,
    );
    const config = await (
        await worker.fetch(
            new Request("https://dashboard.example/api/config"),
            {},
        )
    ).json();
    assert.equal(config.liveAvailable, false);
    assert.equal(
        (
            await worker.fetch(
                new Request("https://dashboard.example/api/v1/orders"),
                {},
            )
        ).status,
        503,
    );
    assert.equal(
        (
            await worker.fetch(
                new Request("https://dashboard.example/api/v1/orders", {
                    method: "POST",
                }),
                {},
            )
        ).status,
        405,
    );
});
