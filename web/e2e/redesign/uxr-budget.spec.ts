// T39 (UXR-039): bundle caps. The existing budget.spec.ts keeps the initial, CSS, terminal and lazy caps; these two tests
// add what PRD-037 changes: the route modules are not in the initial graph, and a measured `routes` cap exists.
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { expect, uxrStatic } from "./gate";

const web = resolve(import.meta.dirname, "../..");
const dist = join(web, "dist");

function initialFiles(): string[] {
  const html = readFileSync(join(dist, "index.html"), "utf8");
  const entry = /src="\/(app-[A-Z0-9]+\.js)"/.exec(html)?.[1] ?? "";
  const seen = new Set<string>();
  const pending = [entry];
  while (pending.length) {
    const file = pending.pop() ?? "";
    if (!file || seen.has(file)) continue;
    seen.add(file);
    pending.push(...[...readFileSync(join(dist, file), "utf8").matchAll(/from"\.\/(chunk-[A-Z0-9]+\.js)"/g)].map((m) => m[1] ?? ""));
  }
  return [...seen];
}

uxrStatic({ t: "T39", uxr: ["UXR-039"], needs: ["WP-0"] }, "the sign-in, account and workers routes are not in the initial JavaScript graph", async () => {
  const initial = initialFiles().map((f) => readFileSync(join(dist, f), "utf8")).join("\n");
  // One string that only the route's module renders (src/views/login.ts, account.ts, workers.ts).
  for (const [route, marker] of [["login", "Create administrator"], ["account", "Change password"], ["workers", "Add worker"]] as const)
    expect(initial.includes(marker), `${route} module code ("${marker}") is in the initial graph; load it with import()`).toBe(false);
  const lazy = readdirSync(dist).filter((f) => f.startsWith("chunk-") && f.endsWith(".js") && !initialFiles().includes(f)).map((f) => readFileSync(join(dist, f), "utf8")).join("\n");
  for (const marker of ["Create administrator", "Change password", "Add worker"]) expect(lazy.includes(marker), `"${marker}" is in a lazy chunk`).toBe(true);
});

uxrStatic({ t: "T39", uxr: ["UXR-039"], needs: ["WP-5"] }, "budget.json records a routes cap and the lazy route chunks stay within it", async () => {
  const budget = JSON.parse(readFileSync(join(web, "budget.json"), "utf8")) as { routes?: { jsRaw?: number; jsGzip?: number } };
  expect(budget.routes?.jsRaw, "budget.json has routes.jsRaw (measured after WP-5, recorded as D18)").toBeGreaterThan(0);
  expect(budget.routes?.jsGzip, "budget.json has routes.jsGzip").toBeGreaterThan(0);
  const initial = new Set(initialFiles());
  const chunks = readdirSync(dist).filter((f) => f.startsWith("chunk-") && f.endsWith(".js") && !initial.has(f));
  // The terminal chunk is the biggest; the rest are route chunks.
  const sized = chunks.map((f) => ({ f, raw: readFileSync(join(dist, f)).length, gzip: gzipSync(readFileSync(join(dist, f)), { level: 9 }).length })).sort((a, b) => b.raw - a.raw).slice(1);
  expect(sized.reduce((n, c) => n + c.raw, 0), "route chunks, raw bytes").toBeLessThanOrEqual(budget.routes?.jsRaw ?? 0);
  expect(sized.reduce((n, c) => n + c.gzip, 0), "route chunks, gzip bytes").toBeLessThanOrEqual(budget.routes?.jsGzip ?? 0);
});
