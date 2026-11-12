import { readFile, readdir, mkdir } from "node:fs/promises";
import { build } from "esbuild";
const assets = {};
async function collect(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
        const path = directory + "/" + entry.name;
        if (entry.isDirectory()) {
            if (entry.name !== "server") await collect(path);
            continue;
        }
        const types = {
            html: "text/html; charset=utf-8",
            js: "application/javascript; charset=utf-8",
            css: "text/css; charset=utf-8",
            svg: "image/svg+xml",
        };
        const type = types[entry.name.split(".").at(-1)];
        if (!type)
            throw new Error("Unsupported public asset type: " + entry.name);
        assets[path.slice(4)] = { body: await readFile(path, "utf8"), type };
    }
}
await collect("dist");
await mkdir("dist/server", { recursive: true });
await build({
    stdin: {
        contents: `import { createWorker } from './server/worker.ts'; export default createWorker(${JSON.stringify(assets)});`,
        resolveDir: process.cwd(),
        sourcefile: "worker-entry.ts",
    },
    bundle: true,
    platform: "browser",
    format: "esm",
    target: "es2022",
    minify: true,
    outfile: "dist/server/index.js",
});
console.log(
    "Hono Worker built with " +
        Object.keys(assets).length +
        " public assets; no .env values bundled.",
);
