import { createApp } from "./app.ts";
// Public build assets only. Runtime credentials and user sessions never enter this map.
export function createWorker(
    assets: Record<string, { body: string; type: string }>,
) {
    return {
        fetch(request: Request, env: Record<string, string | undefined>) {
            const app = createApp({
                apiOrigin: env.CLAWPAY_API_ORIGIN || "",
                supabaseUrl: env.SUPABASE_URL || "",
                supabaseKey: env.SUPABASE_PUBLISHABLE_KEY || "",
            });
            app.get("*", (c) => {
                if (c.req.path.startsWith("/api/"))
                    return c.json(
                        { error: { message: "Endpoint not found." } },
                        404,
                    );
                const asset =
                    assets[c.req.path] ||
                    (!c.req.path.includes(".")
                        ? assets["/index.html"]
                        : undefined);
                if (!asset) return c.notFound();
                return new Response(asset.body, {
                    headers: {
                        "Content-Type": asset.type,
                        "Cache-Control": c.req.path.startsWith("/assets/")
                            ? "public, max-age=31536000, immutable"
                            : "no-cache",
                    },
                });
            });
            return app.fetch(request);
        },
    };
}
