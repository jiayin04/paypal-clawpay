import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { createApp } from "./app.ts";
// Node 24 loads the existing backend env without exposing it to Vite.
try {
    process.loadEnvFile("../backend/.env");
} catch {
    /* optional in deployed environments */
}
try {
    process.loadEnvFile(".env");
} catch {
    /* optional dashboard overrides */
}
const app = createApp({
    apiOrigin: process.env.CLAWPAY_API_ORIGIN || "http://127.0.0.1:8000",
    supabaseUrl: process.env.SUPABASE_URL || "",
    supabaseKey:
        process.env.SUPABASE_PUBLISHABLE_KEY ||
        process.env.SUPABASE_ANON_KEY ||
        "",
});
app.use("*", serveStatic({ root: "./dist" }));
app.get("*", serveStatic({ path: "./dist/index.html" }));
serve(
    {
        fetch: app.fetch,
        hostname: "127.0.0.1",
        port: Number(process.env.DASHBOARD_PORT || 8787),
    },
    (info) => console.log(`ClawPay Hono: http://127.0.0.1:${info.port}`),
);
