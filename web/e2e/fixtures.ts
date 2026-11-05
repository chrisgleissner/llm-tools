import { test as base, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const repo = resolve(import.meta.dirname, "../..");
const targetDir = process.env.CARGO_TARGET_DIR ?? join(repo, "coordinator", "target");
export const BIN = process.env.BRIDGE_COORDINATOR_BIN ?? join(targetDir, "release", "bridge-coordinator");
export const PASSWORD = "correct horse battery";

export interface Coordinator {
  url: string;
  dir: string;
  setupToken: () => string;
  /** Run a bridge-coordinator subcommand against this data directory; returns stdout. */
  cli: (...args: string[]) => string;
}

async function startCoordinator(): Promise<{ coord: Coordinator; stop: () => Promise<void> }> {
  const dir = mkdtempSync(join(tmpdir(), "bridge-e2e-"));
  const data = join(dir, "data");
  execFileSync(BIN, ["init", "--data-dir", data], { stdio: "pipe" });
  const cfg = join(data, "config.toml");
  writeFileSync(cfg, readFileSync(cfg, "utf8").replace("127.0.0.1:8443", "127.0.0.1:0"));
  const child: ChildProcess = spawn(BIN, ["serve", "--data-dir", data, "--web-dir", resolve(import.meta.dirname, "../dist")], { stdio: ["ignore", "ignore", "pipe"] });
  const url = await new Promise<string>((ok, fail) => {
    let buf = "";
    const timer = setTimeout(() => fail(new Error(`coordinator did not start: ${buf}`)), 15_000);
    child.stderr?.on("data", (d: Buffer) => {
      buf += d.toString();
      const m = /listening on (https:\/\/\S+)\n/.exec(buf);
      if (m?.[1]) {
        clearTimeout(timer);
        ok(m[1]);
      }
    });
    child.on("exit", (code) => fail(new Error(`coordinator exited with ${code}: ${buf}`)));
  });
  const coord: Coordinator = {
    url,
    dir: data,
    setupToken: () => readFileSync(join(data, "setup-token"), "utf8").trim(),
    cli: (...args) => execFileSync(BIN, [...args, "--data-dir", data], { encoding: "utf8" }).trim(),
  };
  return {
    coord,
    stop: async () => {
      const exited = new Promise<void>((r) => child.once("exit", () => r()));
      child.kill("SIGTERM");
      await Promise.race([exited, new Promise((r) => setTimeout(r, 12_000))]);
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

export const test = base.extend<{ coord: Coordinator }>({
  // eslint-disable-next-line no-empty-pattern
  coord: async ({}, use) => {
    const { coord, stop } = await startCoordinator();
    await use(coord);
    await stop();
  },
  baseURL: async ({ coord }, use) => {
    await use(coord.url);
  },
});
export { expect };

/** Complete first-run setup through the UI (the page then signs in automatically). */
export async function setupViaUi(page: Page, coord: Coordinator, username = "admin"): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
  await page.getByLabel("Setup token").fill(coord.setupToken());
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

export async function loginViaUi(page: Page, username = "admin", password = PASSWORD): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

/** WCAG 2.x A/AA axe scan in both colour schemes; fails on any violation and prints the details. */
export async function expectAccessible(page: Page, label: string): Promise<void> {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    const summary = r.violations.map((v) => `${v.impact} ${v.id}: ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).join("\n  ")}`);
    expect(summary, `axe violations on ${label} (${scheme})`).toEqual([]);
  }
}

export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(0);
}
