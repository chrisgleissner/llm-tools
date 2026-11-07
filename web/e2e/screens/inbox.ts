// Screens owned by the Inbox and Missions package (PRD-037 WP-4). Names are baseline file names, so they stay unique across packages.
// `screens` carries the variants T42 requires for the Inbox (empty, sparse, dense, error); `SCREENS` is the same list for the
// aggregated screenshot spec (e2e/screens/index.ts).
import { expect } from "../fixtures";
import { MISSIONS, MISSION_PATH } from "../mock";
import { IN, type Screen } from "./types";

/** Inbox screens also name the T42 variant they stand for (empty, sparse, dense or error). */
type ScreenDef = Screen & { variant?: "empty" | "sparse" | "dense" | "error" };

const DENSE = { auth: "in", dataset: "dense" } as const;
/** The Inbox has loaded its lists and finished reading the status of the visible rows. */
const inboxReady = async (p: import("@playwright/test").Page): Promise<void> => {
  await expect(p.locator('[data-section="needs-you"] [data-entity^="mission:"]').first()).toBeVisible();
  await expect(p.locator("[data-q=need-me]")).not.toHaveText("Checking...");
  await expect(p.locator('[data-section="needs-you"] [data-action="approve"]').first()).toBeVisible();
  // The rows that need a second read (the failing task of a failed mission, the verifier of an approval) are complete before the page is captured.
  await expect(p.locator('[data-section="needs-you"] .mrow[data-state="failed"]:not(:has([data-action="retry"]))')).toHaveCount(0);
  await expect(p.locator('[data-section="needs-you"] .mrow[data-state="needs-approval"]').first()).toContainText("verifier:");
};
const withProviders = async (p: import("@playwright/test").Page): Promise<void> => {
  await expect(p.locator('[data-section="running"] [data-entity^="mission:"]').first()).toContainText(/Codex/);
};

export const screens: ScreenDef[] = [
  { name: "inbox-sparse", variant: "sparse", mock: IN, path: "/", run: inboxReady },
  { name: "inbox-dense", variant: "dense", mock: DENSE, path: "/", run: async (p) => { await inboxReady(p); await withProviders(p); } },
  { name: "inbox-empty", variant: "empty", mock: { ...IN, missions: "none" }, path: "/", run: async (p) => { await expect(p.getByRole("heading", { name: "No missions yet" })).toBeVisible(); } },
  { name: "inbox-no-workers", variant: "empty", mock: { auth: "in", workers: "none" }, path: "/", run: async (p) => { await expect(p.getByRole("heading", { name: "No workers yet" })).toBeVisible(); } },
  { name: "inbox-error", variant: "error", mock: { ...IN, apiErrors: true }, path: "/", run: async (p) => { await expect(p.locator(".error-block")).toBeVisible({ timeout: 15_000 }); } },
  { name: "inbox-provider-question", mock: { ...IN, providerQuestion: true }, path: "/", run: async (p) => {
    const row = p.locator('[data-section="needs-you"] [data-state="needs-input"]').first();
    await expect(row).toContainText("Should I continue?");
    await expect(row.locator('[data-action="answer"]')).toHaveAttribute("href", `${MISSION_PATH}?focus=composer`);
  } },
  { name: "inbox-approve-dialog", mock: IN, path: "/", run: async (p) => {
    await inboxReady(p);
    await p.locator('[data-section="needs-you"] [data-action="approve"]').first().click();
    await expect(p.getByRole("dialog")).toContainText("2 files");
  } },
  { name: "inbox-reject-reason-missing", mock: IN, path: "/", run: async (p) => {
    await inboxReady(p);
    await p.locator('[data-section="needs-you"] [data-action="reject"]').first().click();
    await p.getByRole("dialog").locator("[data-confirm]").click();
    await expect(p.getByRole("dialog").locator(".field-error")).toContainText("Enter a reason");
  } },
  { name: "missions", mock: IN, path: "/missions", run: async (p) => {
    await expect(p.locator('[data-section="active"] [data-entity^="mission:"]')).toHaveCount(5);
    await expect(p.locator('[data-section="finished"] summary')).toContainText("(3)");
    await expect(p.locator("[data-q=need-me]")).not.toHaveText("Checking...");
  } },
  { name: "missions-dense", mock: DENSE, path: "/missions", run: async (p) => {
    await expect(p.locator('[data-section="finished"] summary')).toContainText("(10)");
    await expect(p.locator("[data-q=need-me]")).not.toHaveText("Checking...");
  } },
  { name: "missions-finished-open", mock: IN, path: "/missions", run: async (p) => {
    await p.locator('[data-section="finished"] summary').click();
    await expect(p.locator('[data-section="finished"] [data-entity^="mission:"]')).toHaveCount(3);
    await p.evaluate(() => window.scrollTo(0, 0)); // opening the group scrolls the page by an amount that depends on timing; the capture starts at the top
  } },
  { name: "missions-provider-question", mock: { ...IN, providerQuestion: true }, path: "/missions", run: async (p) => {
    const row = p.locator(`[data-entity="mission:alpha/${MISSIONS[0]!.mission.id}"]`);
    await expect(row.locator(".pill")).toHaveAttribute("data-state", "needs-input");
  } },
  { name: "missions-filtered", mock: IN, path: "/missions?state=paused", run: async (p) => { await expect(p.locator('[data-section="active"] [data-entity^="mission:"]')).toHaveCount(1); } },
  { name: "missions-no-match", mock: IN, path: "/missions?q=zzz", run: (p) => expect(p.getByRole("heading", { name: "No missions match" })).toBeVisible() },
  { name: "missions-none", mock: { auth: "in", missions: "none" }, path: "/missions", run: (p) => expect(p.getByRole("heading", { name: "No missions yet" })).toBeVisible() },
  { name: "missions-error", mock: { ...IN, apiErrors: true }, path: "/missions", run: (p) => expect(p.locator(".error-block")).toBeVisible() },
];

export const SCREENS = screens;
