// Bundle budget (WEB-010) and initial-load measurement on Pixel 4 emulation with CPU throttling.
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { expect, test } from "./fixtures";

const web = resolve(import.meta.dirname, "..");
const dist = join(web, "dist");
const budget = JSON.parse(readFileSync(join(web, "budget.json"), "utf8")) as {
  initial: Record<string, number>;
  terminal: Record<string, number>;
  routes: Record<string, number>;
  lazyRoutes: Record<string, string>;
  load: { cpuThrottle: number; fcpMsMax: number; setupFormMsMax: number };
};

const size = (file: string) => {
  const raw = readFileSync(join(dist, file));
  return { raw: raw.length, gzip: gzipSync(raw, { level: 9 }).length };
};

test("JavaScript and CSS stay within the recorded budget; the terminal loads lazily", async () => {
  const html = readFileSync(join(dist, "index.html"), "utf8");
  const js = /src="\/(app-[A-Z0-9]+\.js)"/.exec(html)?.[1] ?? "";
  const css = /href="\/(app-[A-Z0-9]+\.css)"/.exec(html)?.[1] ?? "";
  expect(js && css, "index.html references the bundles").toBeTruthy();
  const staticImports = (source: string) => [...source.matchAll(/from"\.\/(chunk-[A-Z0-9]+\.js)"/g)].map((match) => match[1] ?? "");
  const initialFiles = new Set<string>();
  const pending = [js];
  while (pending.length > 0) {
    const file = pending.pop() ?? "";
    if (initialFiles.has(file)) continue;
    initialFiles.add(file);
    pending.push(...staticImports(readFileSync(join(dist, file), "utf8")));
  }
  const initialJs = [...initialFiles].reduce((total, file) => {
    const fileSize = size(file);
    return { raw: total.raw + fileSize.raw, gzip: total.gzip + fileSize.gzip };
  }, { raw: 0, gzip: 0 });
  const c = size(css);
  const initial = { jsRaw: initialJs.raw, jsGzip: initialJs.gzip, cssRaw: c.raw, cssGzip: c.gzip, totalRaw: initialJs.raw + c.raw, totalGzip: initialJs.gzip + c.gzip };
  for (const [k, v] of Object.entries(initial)) expect(v, `initial ${k} (budget ${budget.initial[k]})`).toBeLessThanOrEqual(budget.initial[k] ?? 0);
  // The initial entry must not statically pull in the terminal: chunks are loaded on demand only.
  const chunks = readdirSync(dist).filter((f) => f.startsWith("chunk-") && f.endsWith(".js"));
  expect(chunks.length).toBeGreaterThan(0);
  const biggest = [...chunks].sort((a, b) => size(b).raw - size(a).raw)[0] ?? "";
  const appBundles = [...initialFiles, ...chunks.filter((chunk) => !initialFiles.has(chunk))];
  const bundleSources = appBundles.map((file) => readFileSync(join(dist, file), "utf8"));
  const staticChunkImports = bundleSources.flatMap(staticImports);
  expect(staticChunkImports, "the terminal chunk (xterm.js) is not imported statically by any app chunk").not.toContain(biggest);
  expect(bundleSources.some((source) => source.includes(`import("./${biggest}")`)), "the terminal chunk is loaded with a dynamic import").toBe(true);
  // Terminal chunk (xterm.js) and route chunks have separate caps: the terminal is one large chunk, routes are many small ones.
  const terminal = size(biggest);
  expect(terminal.raw, `terminal raw (budget ${budget.terminal["jsRaw"]})`).toBeLessThanOrEqual(budget.terminal["jsRaw"] ?? 0);
  expect(terminal.gzip, `terminal gzip (budget ${budget.terminal["jsGzip"]})`).toBeLessThanOrEqual(budget.terminal["jsGzip"] ?? 0);
  const routes = chunks.filter((file) => !initialFiles.has(file) && file !== biggest).reduce((a, f) => ({ raw: a.raw + size(f).raw, gzip: a.gzip + size(f).gzip }), { raw: 0, gzip: 0 });
  expect(routes.raw, `routes raw (budget ${budget.routes["jsRaw"]})`).toBeLessThanOrEqual(budget.routes["jsRaw"] ?? 0);
  expect(routes.gzip, `routes gzip (budget ${budget.routes["jsGzip"]})`).toBeLessThanOrEqual(budget.routes["jsGzip"] ?? 0);
  // The lazy routes are not in the initial graph: a marker string of each route module is absent from every initial file and present in a lazy chunk.
  const initialSource = [...initialFiles].map((file) => readFileSync(join(dist, file), "utf8")).join("\n");
  const lazySource = chunks.filter((file) => !initialFiles.has(file)).map((file) => readFileSync(join(dist, file), "utf8")).join("\n");
  for (const [route, marker] of Object.entries(budget.lazyRoutes)) {
    expect(initialSource.includes(marker), `route "${route}" (marker "${marker}") must not be in the initial graph`).toBe(false);
    expect(lazySource.includes(marker), `route "${route}" (marker "${marker}") must be in a lazy chunk`).toBe(true);
  }
  expect(statSync(join(dist, js)).size).toBe(size(js).raw);
  console.log(`BUNDLE initial js ${initialJs.raw}/${initialJs.gzip} css ${c.raw}/${c.gzip} terminal ${terminal.raw}/${terminal.gzip} routes ${routes.raw}/${routes.gzip} (raw/gzip bytes)`);
});

interface Sample {
  fcp: number;
  dcl: number;
  load: number;
  formReady: number;
  transferred: number;
  requests: number;
}

test("initial load of the first-run page on Pixel 4 emulation, with and without 4x CPU throttling", async ({ browser, coord }, info) => {
  test.skip(info.project.name !== "chromium-mobile", "CDP emulation needs Chromium; measured on the Pixel 4 project");
  test.setTimeout(180_000);
  const results: Record<string, Sample[]> = { unthrottled: [], throttled: [] };
  for (const mode of ["unthrottled", "throttled"] as const) {
    for (let i = 0; i < 5; i++) {
      const context = await browser.newContext({ ...info.project.use, ignoreHTTPSErrors: true, serviceWorkers: "block" });
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send("Network.enable");
      await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
      if (mode === "throttled") await cdp.send("Emulation.setCPUThrottlingRate", { rate: budget.load.cpuThrottle });
      const t0 = Date.now();
      await page.goto(coord.url + "/", { waitUntil: "load" });
      await page.getByRole("heading", { name: "First-run setup" }).waitFor();
      const formReady = Date.now() - t0;
      const m = await page.evaluate(() => {
        const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
        const fcp = performance.getEntriesByName("first-contentful-paint")[0]?.startTime ?? 0;
        const res = performance.getEntriesByType("resource") as PerformanceResourceTiming[];
        return { fcp, dcl: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, transferred: nav.encodedBodySize + res.reduce((a, r) => a + r.encodedBodySize, 0), requests: 1 + res.length };
      });
      results[mode]!.push({ ...m, formReady });
      await context.close();
    }
  }
  const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] ?? 0;
  const summary = Object.fromEntries(
    Object.entries(results).map(([mode, s]) => [mode, { runs: s.length, fcpMs: Math.round(median(s.map((x) => x.fcp))), domContentLoadedMs: Math.round(median(s.map((x) => x.dcl))), loadMs: Math.round(median(s.map((x) => x.load))), formReadyMs: Math.round(median(s.map((x) => x.formReady))), transferredBytes: median(s.map((x) => x.transferred)), requests: median(s.map((x) => x.requests)) }]),
  );
  mkdirSync(join(web, "test-results"), { recursive: true });
  writeFileSync(join(web, "test-results", "load-metrics.json"), JSON.stringify({ device: "Pixel 4 (Playwright profile, Chromium, emulated)", cpuThrottleRate: budget.load.cpuThrottle, network: "loopback, no throttling, cache disabled", summary }, null, 2));
  console.log("LOAD " + JSON.stringify(summary));
  expect(summary["throttled"]!.fcpMs, "FCP under 4x CPU throttling").toBeLessThanOrEqual(budget.load.fcpMsMax);
  expect(summary["throttled"]!.formReadyMs, "form ready under 4x CPU throttling").toBeLessThanOrEqual(budget.load.setupFormMsMax);
});
