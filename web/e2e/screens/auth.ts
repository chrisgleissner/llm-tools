// Screens owned by the auth package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, heading, live, fillSetup, signInAttempt, type Screen } from "./types";

export const SCREENS: Screen[] = [
  { name: "setup-empty", mock: { auth: "setup" }, path: "/", run: (p) => heading(p, "First-run setup") },
  { name: "setup-filled", mock: { auth: "setup" }, path: "/", run: async (p) => { await fillSetup(p); await expect(p.locator("#strength-text")).toContainText("Strong"); } },
  { name: "setup-weak-password", mock: { auth: "setup" }, path: "/", run: async (p) => { await p.getByLabel("New password", { exact: true }).fill("short"); await expect(p.locator("#strength-text")).toContainText("Too short"); } },
  { name: "setup-validation-errors", mock: { auth: "setup" }, path: "/", run: async (p) => { await p.getByLabel("New password", { exact: true }).fill("short"); await p.getByRole("button", { name: "Create administrator" }).click(); await expect(p.locator("#setup-token-error")).toContainText("Enter the setup token"); } },
  { name: "setup-wrong-token", mock: { auth: "setup", setup: "bad-token" }, path: "/", run: async (p) => { await fillSetup(p); await p.getByRole("button", { name: "Create administrator" }).click(); await expect(p.locator("#setup-token-error")).toContainText("not the setup token"); } },
  { name: "login-empty", mock: { auth: "login" }, path: "/login", run: (p) => heading(p, "Sign in") },
  { name: "login-wrong-password", mock: { auth: "login", login: "wrong" }, path: "/login", run: async (p) => { await signInAttempt(p); await expect(p.locator("#form-error")).toHaveText("Username or password is incorrect. Check both and try again."); await expect(p.getByLabel("Password", { exact: true })).toBeFocused(); } },
  { name: "login-rate-limited", mock: { auth: "login", login: "limited" }, path: "/login", run: async (p) => { await signInAttempt(p); await expect(p.locator("#form-error")).toContainText("try again in 5 min 0 s (at 12:05:00)"); } },
  { name: "login-caps-lock", mock: { auth: "login" }, path: "/login", run: async (p) => { await p.getByLabel("Password", { exact: true }).focus(); await p.getByLabel("Password", { exact: true }).evaluate((el) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "A", modifierCapsLock: true, bubbles: true }))); await expect(p.locator("#password-caps")).toBeVisible(); } },
  { name: "login-signed-out", mock: IN, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await p.locator("#logout").click(); await expect(p.locator("#login-notice")).toContainText("You signed out"); } },
  { name: "setup-token-help-open", mock: { auth: "setup" }, path: "/", run: async (p) => { await p.locator("#token-help summary").click(); await expect(p.locator("#token-help")).toContainText("bridge-coordinator setup-token"); } },
  { name: "login-forgot-open", mock: { auth: "login" }, path: "/login", run: async (p) => { await p.locator("#forgot-password-hint summary").click(); await expect(p.locator("#forgot-password-hint")).toContainText("llm-bridge-coordinator reset-password"); } },
  { name: "account-token-create", mock: IN, path: "/account", run: async (p) => { await heading(p, "Account"); await p.getByRole("button", { name: "Create token" }).click(); await p.getByLabel("Token name").fill("automation-key"); await expect(p.getByRole("dialog", { name: "Create an API token" })).toBeVisible(); } },
  { name: "account-token-secret", mock: IN, path: "/account", run: async (p) => { await heading(p, "Account"); await p.getByRole("button", { name: "Create token" }).click(); await p.getByLabel("Token name").fill("automation-key"); await p.getByRole("dialog", { name: "Create an API token" }).getByRole("button", { name: "Create token" }).click(); await expect(p.getByRole("dialog", { name: "Token created" })).toBeVisible(); await expect(p.getByRole("button", { name: "Copy token" })).toBeFocused(); } },
  { name: "account", mock: IN, path: "/account", run: async (p) => { await heading(p, "Account"); await p.getByLabel("New password", { exact: true }).fill("a fairly long passphrase"); } },
];
