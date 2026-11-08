import { Hono } from "hono";
import { secureHeaders } from "hono/secure-headers";

export type Config = {
    apiOrigin: string;
    supabaseUrl: string;
    supabaseKey: string;
};
// Deliberately read-only: no device tokens, admin credentials or payment operations.
const allowed =
    /^\/(orders(?:\/[0-9a-f-]{36}(?:\/(?:timeline|a2a-messages))?)?|conversations(?:\/[0-9a-f-]{36}\/events)?|spending\/summary|audit-events|devices|payment-methods)$/i;
export function createApp(config: Config, fetcher: typeof fetch = fetch) {
    const app = new Hono();
    app.use(
        "*",
        secureHeaders({
            contentSecurityPolicy: {
                defaultSrc: ["'self'"],
                scriptSrc: ["'self'"],
                styleSrc: ["'self'", "'unsafe-inline'"],
                connectSrc: [
                    "'self'",
                    ...(config.supabaseUrl
                        ? [new URL(config.supabaseUrl).origin]
                        : []),
                ],
                imgSrc: ["'self'", "data:"],
                frameAncestors: ["'none'"],
                baseUri: ["'self'"],
                formAction: ["'self'"],
            },
            referrerPolicy: "no-referrer",
        }),
    );
    app.use("/api/*", async (c, next) => {
        c.header("Cache-Control", "no-store");
        await next();
    });
    app.get("/api/config", (c) =>
        c.json({
            supabaseUrl: config.supabaseUrl,
            supabaseKey: config.supabaseKey,
            liveAvailable: Boolean(
                config.apiOrigin && config.supabaseUrl && config.supabaseKey,
            ),
        }),
    );
    app.all("/api/v1/*", async (c) => {
        if (c.req.method !== "GET")
            return c.json(
                {
                    error: {
                        message:
                            "The dashboard is read-only. Approve payments on your watch.",
                    },
                },
                405,
            );
        const path = c.req.path.slice("/api/v1".length);
        if (!allowed.test(path))
            return c.json(
                { error: { message: "Dashboard endpoint not found." } },
                404,
            );
        if (!config.apiOrigin)
            return c.json(
                {
                    error: {
                        message:
                            "Live backend is not connected to this dashboard.",
                    },
                },
                503,
            );
        const authorization = c.req.header("Authorization");
        if (
            !authorization?.match(/^Bearer [^\s]+$/i) ||
            /^Bearer (?:cpd_|cpl_)/i.test(authorization)
        )
            return c.json(
                {
                    error: {
                        message: "Sign in with your ClawPay owner account.",
                    },
                },
                401,
            );
        const url = new URL("/api/v1" + path, config.apiOrigin);
        const incoming = new URL(c.req.url);
        for (const key of ["limit", "before"])
            if (incoming.searchParams.has(key))
                url.searchParams.set(key, incoming.searchParams.get(key)!);
        try {
            const response = await fetcher(url, {
                headers: {
                    Authorization: authorization,
                    Accept: "application/json",
                },
                redirect: "error",
                signal: AbortSignal.timeout(15000),
            });
            if (
                !response.headers
                    .get("content-type")
                    ?.includes("application/json")
            )
                return c.json(
                    {
                        error: {
                            message:
                                "Backend returned an unexpected response. Try refreshing.",
                        },
                    },
                    502,
                );
            return new Response(response.body, {
                status: response.status,
                headers: {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                },
            });
        } catch {
            return c.json(
                {
                    error: {
                        message:
                            "Cannot reach ClawPay. Check the backend connection and retry.",
                    },
                },
                502,
            );
        }
    });
    return app;
}
