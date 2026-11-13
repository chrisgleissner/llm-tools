#!/usr/bin/env node
// PRD-037 T37: run Lighthouse (mobile preset) on pages of a running console and check the budgets.
//
//   node scripts/lighthouse.mjs --url https://127.0.0.1:8443 --cookie "bridge_session=..." --paths /,/missions,/missions/w/abc
//
// Budgets: performance >= 90, accessibility = 100, best practices >= 95, LCP <= 2500 ms, TBT <= 200 ms.
// Lighthouse is not a dependency of the console; it is run through `npx lighthouse@<pinned version>` so package-lock.json
// stays unchanged. Chrome comes from CHROME_PATH or from the Chromium that Playwright installed.
// Results: test-results/lighthouse/<page>.json and a one-line summary per page on stdout. Exit status 1 on any miss.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const LIGHTHOUSE = "lighthouse@12.8.2";
const BUDGET = { performance: 0.9, accessibility: 1, "best-practices": 0.95, lcpMs: 2500, tbtMs: 200 };

function arg(name, fallback = "") {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? (process.argv[i + 1] ?? fallback) : fallback;
}
const base = arg("url").replace(/\/$/, "");
const cookie = arg("cookie");
const paths = arg("paths", "/,/missions").split(",").filter(Boolean);
if (!base) {
  console.error("usage: lighthouse.mjs --url <console url> [--cookie 'name=value'] [--paths /,/missions]");
  process.exit(2);
}
const outDir = resolve(import.meta.dirname, "..", "test-results", "lighthouse");
mkdirSync(outDir, { recursive: true });

let chrome = process.env.CHROME_PATH ?? "";
if (!chrome) {
  const { chromium } = await import("@playwright/test");
  chrome = chromium.executablePath();
}

const failures = [];
for (const path of paths) {
  const name = (path === "/" ? "inbox" : path.replace(/^\//, "").replace(/[^a-z0-9]+/gi, "-")).slice(0, 40);
  const out = join(outDir, `${name}.json`);
  const args = [
    "--yes", LIGHTHOUSE, `${base}${path}`,
    "--output=json", `--output-path=${out}`, "--quiet",
    "--only-categories=performance,accessibility,best-practices",
    "--chrome-flags=--headless=new --no-sandbox --ignore-certificate-errors",
  ];
  if (cookie) args.push(`--extra-headers=${JSON.stringify({ Cookie: cookie })}`);
  // Lighthouse itself sometimes cannot record a trace ("NO_NAVSTART": the page load started before tracing did). That is a failed measurement, not a result:
  // it is repeated, up to three times. A measurement that was made and misses a budget is never repeated.
  for (let attempt = 1; ; attempt += 1) {
    try {
      execFileSync("npx", args, { env: { ...process.env, CHROME_PATH: chrome }, stdio: ["ignore", "ignore", "inherit"] });
      break;
    } catch (error) {
      if (attempt >= 3) throw error;
      console.error(`lighthouse could not measure ${path} (attempt ${attempt}); trying again`);
    }
  }
  const lhr = JSON.parse(readFileSync(out, "utf8"));
  const score = (id) => lhr.categories[id]?.score ?? 0;
  const lcp = lhr.audits["largest-contentful-paint"]?.numericValue ?? Infinity;
  const tbt = lhr.audits["total-blocking-time"]?.numericValue ?? Infinity;
  console.log(`${path}: performance ${Math.round(score("performance") * 100)}, accessibility ${Math.round(score("accessibility") * 100)}, best practices ${Math.round(score("best-practices") * 100)}, LCP ${Math.round(lcp)} ms, TBT ${Math.round(tbt)} ms`);
  if (score("performance") < BUDGET.performance) failures.push(`${path}: performance ${score("performance")} < ${BUDGET.performance}`);
  if (score("accessibility") < BUDGET.accessibility) failures.push(`${path}: accessibility ${score("accessibility")} < ${BUDGET.accessibility}`);
  if (score("best-practices") < BUDGET["best-practices"]) failures.push(`${path}: best practices ${score("best-practices")} < ${BUDGET["best-practices"]}`);
  if (lcp > BUDGET.lcpMs) failures.push(`${path}: LCP ${Math.round(lcp)} ms > ${BUDGET.lcpMs}`);
  if (tbt > BUDGET.tbtMs) failures.push(`${path}: TBT ${Math.round(tbt)} ms > ${BUDGET.tbtMs}`);
}
if (failures.length) {
  console.error(`Lighthouse budget missed:\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
