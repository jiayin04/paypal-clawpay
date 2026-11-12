import { copyFile, mkdir, rm, writeFile } from "node:fs/promises";

// Reuse the complete Hono Worker, including its embedded public assets and API.
// Keep Pages output separate from the existing Workers/Sites deployment artifacts.
await rm("dist-pages", { recursive: true, force: true });
await mkdir("dist-pages", { recursive: true });
await copyFile("dist/server/index.js", "dist-pages/_worker.js");
await copyFile("dist/index.html", "dist-pages/index.html");
await writeFile(
    "dist-pages/_routes.json",
    JSON.stringify({ version: 1, include: ["/*"], exclude: [] }) + "\n",
);
console.log("Pages advanced-mode bundle ready in dist-pages.");
