// T39 (UXR-039): bundle caps. e2e/budget.spec.ts keeps the initial, terminal and routes caps. These tests read the same
// numbers the same way (the routes figure is the sum of the lazily loaded chunks other than the terminal chunk, which is the
// biggest chunk) and add the PRD-037 checks: the route modules named in budget.json are not in the initial graph, and lazy
// route stylesheets stay within `routes.cssRaw`/`routes.cssGzip` when budget.json records them.
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { expect, uxrStatic } from "./gate";

const web = resolve(import.meta.dirname, "../..");
const dist = join(web, "dist");
interface Budget { routes: Record<string, number>; lazyRoutes: Record<string, string> }
const budget = (): Budget => JSON.parse(readFileSync(join(web, "budget.json"), "utf8")) as Budget;

const read = (file: string) => readFileSync(join(dist, file));
const size = (file: string) => ({ raw: read(file).length, gzip: gzipSync(read(file), { level: 9 }).length });
const staticImports = (source: string) => [...source.matchAll(/from"\.\/(chunk-[A-Z0-9]+\.js)"/g)].map((m) => m[1] ?? "");

/** The graph budget.spec.ts uses: the entry named in index.html and every transitive static chunk import. */
function graph() {
  const html = readFileSync(join(dist, "index.html"), "utf8");
  const js = /src="\/(app-[A-Z0-9]+\.js)"/.exec(html)?.[1] ?? "";
  const css = /href="\/(app-[A-Z0-9]+\.css)"/.exec(html)?.[1] ?? "";
  const initial = new Set<string>();
  const pending = [js];
  while (pending.length) {
    const file = pending.pop() ?? "";
    if (!file || initial.has(file)) continue;
    initial.add(file);
    pending.push(...staticImports(read(file).toString("utf8")));
  }
  const chunks = readdirSync(dist).filter((f) => f.startsWith("chunk-") && f.endsWith(".js"));
  const biggest = [...chunks].sort((a, b) => size(b).raw - size(a).raw)[0] ?? "";
  return { initial, css, chunks, biggest };
}

uxrStatic({ t: "T39", uxr: ["UXR-039"], needs: ["WP-0"] }, "the lazy route modules named in budget.json are not in the initial graph and are in a lazy chunk", async () => {
  const { initial, chunks } = graph();
  const initialSource = [...initial].map((f) => read(f).toString("utf8")).join("\n");
  const lazySource = chunks.filter((f) => !initial.has(f)).map((f) => read(f).toString("utf8")).join("\n");
  const markers = Object.entries(budget().lazyRoutes);
  expect(markers.length, "budget.json lists lazyRoutes markers").toBeGreaterThan(0);
  for (const [route, marker] of markers) {
    expect(initialSource.includes(marker), `route "${route}" ("${marker}") is in the initial graph; load it with import()`).toBe(false);
    expect(lazySource.includes(marker), `route "${route}" ("${marker}") is in no lazy chunk`).toBe(true);
  }
});

uxrStatic({ t: "T39", uxr: ["UXR-039"], needs: ["WP-0"] }, "lazy route chunks (all lazy chunks but the terminal) stay within budget.json routes, the figure budget.spec.ts reports", async () => {
  const { initial, chunks, biggest } = graph();
  const b = budget().routes;
  expect(b["jsRaw"], "budget.json routes.jsRaw").toBeGreaterThan(0);
  expect(b["jsGzip"], "budget.json routes.jsGzip").toBeGreaterThan(0);
  const routes = chunks.filter((f) => !initial.has(f) && f !== biggest).reduce((a, f) => ({ raw: a.raw + size(f).raw, gzip: a.gzip + size(f).gzip }), { raw: 0, gzip: 0 });
  console.log(`T39 routes ${routes.raw}/${routes.gzip} (raw/gzip bytes)`);
  expect(routes.raw, `routes raw (budget ${b["jsRaw"]})`).toBeLessThanOrEqual(b["jsRaw"] ?? 0);
  expect(routes.gzip, `routes gzip (budget ${b["jsGzip"]})`).toBeLessThanOrEqual(b["jsGzip"] ?? 0);
  // Lazy route stylesheets: every .css in dist except the app stylesheet and the one the terminal chunk loads.
  if (b["cssRaw"] !== undefined) {
    const terminalSource = read(biggest).toString("utf8");
    const lazyCss = readdirSync(dist).filter((f) => f.endsWith(".css") && f !== graph().css && !terminalSource.includes(f));
    const css = lazyCss.reduce((a, f) => ({ raw: a.raw + size(f).raw, gzip: a.gzip + size(f).gzip }), { raw: 0, gzip: 0 });
    expect(css.raw, `lazy route css raw (budget ${b["cssRaw"]})`).toBeLessThanOrEqual(b["cssRaw"] ?? 0);
    expect(css.gzip, `lazy route css gzip (budget ${b["cssGzip"] ?? 0})`).toBeLessThanOrEqual(b["cssGzip"] ?? Infinity);
  }
});
