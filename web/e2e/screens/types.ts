// Shared by the per-package screen lists in this folder (e2e/screens/<package>.ts); `e2e/screenshots.spec.ts` runs them all.
import { expect } from "../fixtures";
import type { MockOptions } from "../mock";
import type { Page } from "@playwright/test";

export interface Screen {
  name: string;
  mock: MockOptions;
  path: string;
  /** Drive the page into the state to capture; return a locator text that proves it is ready. */
  run: (page: Page) => Promise<void>;
  narrowOnly?: boolean;
  wideOnly?: boolean;
}

export const IN: MockOptions = { auth: "in" };
export const heading = (page: Page, name: string) => expect(page.getByRole("heading", { name, exact: true }).first()).toBeVisible();
export const live = (page: Page) => expect(page.locator("#conn")).toContainText("Live");
export const fillSetup = async (page: Page) => {
  await page.getByLabel("Setup token").fill("e2e-token");
  await page.getByLabel("Administrator username").fill("admin");
  await page.getByLabel("New password", { exact: true }).fill("correct horse battery");
  await page.getByLabel("Repeat password").fill("correct horse battery");
};
export const signInAttempt = async (page: Page) => {
  await page.getByLabel("Username", { exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("not the password");
  await page.getByRole("button", { name: "Sign in" }).click();
};
export const nav = async (page: Page, name: string) => {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true }).click();
};

