// Bundle budget (WEB-010) and initial-load measurement on Pixel 4 emulation with CPU throttling.
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { expect, test } from "./fixtures";

const web = resolve(import.meta.dirname, "..");
const dist = join(web, "dist");
const budget = JSON.parse(readFileSync(join(web, "budget.json"), "utf8")) as {
  initial: Record<string, number>;
  lazy: Record<string, number>;
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
  const j = size(js);
  const c = size(css);
  const initial = { jsRaw: j.raw, jsGzip: j.gzip, cssRaw: c.raw, cssGzip: c.gzip, totalRaw: j.raw + c.raw, totalGzip: j.gzip + c.gzip };
  for (const [k, v] of Object.entries(initial)) expect(v, `initial ${k} (budget ${budget.initial[k]})`).toBeLessThanOrEqual(budget.initial[k] ?? 0);
  // The initial entry must not statically pull in the terminal: chunks are loaded on demand only.
  const chunks = readdirSync(dist).filter((f) => f.startsWith("chunk-") && f.endsWith(".js"));
  expect(chunks.length).toBeGreaterThan(0);
  const entry = readFileSync(join(dist, js), "utf8");
  const staticImports = [...entry.matchAll(/(?:^|[;}])import(?:[^;"']*from)?["']\.\/(chunk-[A-Z0-9]+\.js)["']/g)].map((m) => m[1]);
  const biggest = [...chunks].sort((a, b) => size(b).raw - size(a).raw)[0] ?? "";
  expect(staticImports, "the terminal chunk (xterm.js) is not imported statically by the entry").not.toContain(biggest);
  expect(entry, "the terminal chunk is loaded with a dynamic import").toContain(`import("./${biggest}")`);
  const lazy = chunks.reduce((a, f) => ({ raw: a.raw + size(f).raw, gzip: a.gzip + size(f).gzip }), { raw: 0, gzip: 0 });
  expect(lazy.raw, `lazy raw (budget ${budget.lazy["jsRaw"]})`).toBeLessThanOrEqual(budget.lazy["jsRaw"] ?? 0);
  expect(lazy.gzip, `lazy gzip (budget ${budget.lazy["jsGzip"]})`).toBeLessThanOrEqual(budget.lazy["jsGzip"] ?? 0);
  expect(statSync(join(dist, js)).size).toBe(j.raw);
  console.log(`BUNDLE initial js ${j.raw}/${j.gzip} css ${c.raw}/${c.gzip} lazy ${lazy.raw}/${lazy.gzip} (raw/gzip bytes)`);
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
