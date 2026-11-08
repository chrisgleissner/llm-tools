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
  /** `admin reset-password` on the host, the way an SSH session would run it; `input` is what is typed or piped. */
  resetPassword: (args: string[], input?: string) => string;
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
    resetPassword: (args, input = "") => execFileSync(BIN, ["admin", "reset-password", ...args, "--data-dir", data], { encoding: "utf8", input }).trim(),
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
  await page.getByLabel("Administrator username").fill(username);
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill(PASSWORD);
  const setupResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname.endsWith("/auth/setup") && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.getByRole("button", { name: "Create administrator" }).click();
  const response = await setupResponse;
  expect(response.ok(), `first-run setup returned HTTP ${response.status()}`).toBe(true);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible({ timeout: 30_000 });
}

/** Set up at the DNS localhost origin and enroll a real CTAP2 virtual passkey for passkey-gated UI tests. */
export async function setupWithVirtualPasskey(page: Page, coord: Coordinator, username = "admin"): Promise<() => Promise<void>> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
  });
  await page.addInitScript(() => {
    const pk = window.PublicKeyCredential as typeof PublicKeyCredential & {
      isConditionalMediationAvailable?: () => Promise<boolean>;
      getClientCapabilities?: () => Promise<{ conditionalGet?: boolean }>;
    };
    Object.defineProperty(pk, "isConditionalMediationAvailable", { configurable: true, value: async () => false });
    Object.defineProperty(pk, "getClientCapabilities", { configurable: true, value: async () => ({ conditionalGet: false }) });
  });
  const origin = coord.url.replace("127.0.0.1", "localhost");
  await page.goto(origin);
  await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
  await page.getByLabel("Setup token").fill(coord.setupToken());
  await page.getByLabel("Administrator username").fill(username);
  await page.getByLabel("New password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Repeat password").fill(PASSWORD);
  const setupResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/auth/setup") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Create administrator" }).click();
  expect((await setupResponse).ok()).toBe(true);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  const registration = await page.evaluate(async (password) => {
    const state = await fetch("/api/v1/auth/state").then((response) => response.json()) as { csrf_token: string };
    const stepped = await fetch("/api/v1/auth/step-up", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf_token }, body: JSON.stringify({ password }),
    });
    if (!stepped.ok) throw new Error(`step-up failed: ${stepped.status} ${await stepped.text()}`);
    const fresh = await stepped.json() as { csrf_token: string };
    const started = await fetch("/api/v1/auth/passkeys/registration/start", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": fresh.csrf_token }, body: JSON.stringify({ name: "UI test passkey" }),
    });
    if (!started.ok) throw new Error(`registration start failed: ${started.status} ${await started.text()}`);
    const ceremony = await started.json() as { challenge_id: string; options: { publicKey: Record<string, unknown> } };
    const publicKey = ceremony.options.publicKey as Record<string, unknown> & { challenge: string; user: { id: string }; excludeCredentials?: Array<{ id: string; type: "public-key"; transports?: AuthenticatorTransport[] }> };
    const decode = (value: string) => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
    const credential = await navigator.credentials.create({ publicKey: {
      ...publicKey,
      challenge: decode(publicKey.challenge),
      user: { ...publicKey.user, id: decode(publicKey.user.id) },
      excludeCredentials: publicKey.excludeCredentials?.map((item) => ({ ...item, id: decode(item.id) })),
    } as unknown as PublicKeyCredentialCreationOptions }) as PublicKeyCredential;
    const response = credential.response as AuthenticatorAttestationResponse;
    const encode = (value: ArrayBuffer | ArrayBufferView) => {
      const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    };
    const finished = await fetch("/api/v1/auth/passkeys/registration/finish", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": fresh.csrf_token },
      body: JSON.stringify({ challenge_id: ceremony.challenge_id, credential: {
        id: credential.id, rawId: encode(credential.rawId), type: credential.type,
        response: { clientDataJSON: encode(response.clientDataJSON), attestationObject: encode(response.attestationObject), transports: response.getTransports() },
        clientExtensionResults: credential.getClientExtensionResults(),
      } }),
    });
    return { status: finished.status, body: await finished.text() };
  }, PASSWORD);
  expect(registration.status, registration.body).toBe(201);
  await page.goto(`${origin}/account`);
  await expect(page.getByRole("list", { name: "Registered passkeys" })).toContainText("UI test passkey");
  return () => cdp.detach();
}

/** Open the account menu and sign out. */
export async function signOutViaUi(page: Page): Promise<void> {
  await page.locator("#account-button").click();
  await page.locator("#account-panel").getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

/** Go to a page through the navigation that the current viewport shows (side bar, tab bar or the More sheet). */
export async function navTo(page: Page, name: "Overview" | "Workers" | "Missions" | "Sessions" | "Usage" | "Diagnostics" | "Account"): Promise<void> {
  if (name === "Account") {
    await page.locator("#account-button").click();
    await page.locator("#account-panel").getByRole("button", { name: "Change password" }).click();
    return;
  }
  // The shell (side bar, tab bar and More button) is built only after the page's auth-state request returns, which
  // can be after the load event of a goto. isVisible() does not wait, so without this the check below can run before
  // any navigation exists and fall through to a More button that is not there yet (or is hidden on desktop).
  await page.locator("#more-button").waitFor({ state: "attached" });
  const link = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true });
  const more = page.locator("#more-button");
  await expect.poll(async () => (await link.isVisible()) || (await more.isVisible()), {
    message: `navigation for ${name} should render before choosing its responsive control`,
  }).toBe(true);
  if (await link.isVisible()) return link.click();
  await more.click();
  await page.locator("#more-sheet").getByRole("link", { name, exact: true }).click();
}

export async function loginViaUi(page: Page, username = "admin", password = PASSWORD): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

/** Complete the password/MFA prompt shown when a sensitive browser mutation needs step-up. */
export async function stepUpViaUi(page: Page, password = PASSWORD): Promise<void> {
  const passkeyDialog = page.getByRole("dialog", { name: "Use a passkey to continue" });
  if (await passkeyDialog.isVisible().catch(() => false)) {
    await passkeyDialog.getByRole("button", { name: "Use a passkey instead" }).click();
    await expect(passkeyDialog).toHaveCount(0);
    return;
  }
  const dialog = page.getByRole("dialog", { name: "Verify it is you" });
  if (!(await dialog.isVisible().catch(() => false))) return;
  await dialog.getByLabel("Current password").fill(password);
  await dialog.getByRole("button", { name: "Verify and continue" }).click();
  await expect(dialog).toHaveCount(0);
}

/** WCAG 2.x A/AA axe scan in both colour schemes; fails on any violation and prints the details. */
export async function expectAccessible(page: Page, label: string): Promise<void> {
  // Buttons and chips fade their colours over 0.12 s. Switching the colour scheme starts that
  // transition, and axe measuring contrast mid-fade reports colours no steady state has (seen in
  // CI on #login-submit and the danger buttons). Settle styles before every scan.
  await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; animation: none !important; }" });
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    const summary = r.violations.map((v) => `${v.impact} ${v.id}: ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).join("\n  ")}`);
    expect(summary, `axe violations on ${label} (${scheme})`).toEqual([]);
  }
}

export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(0);
}
