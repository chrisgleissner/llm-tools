// Screens owned by the session package (PRD-037). Names are baseline file names, so they stay unique across packages.
import type { Page } from "@playwright/test";
import { expect } from "../fixtures";
import { IN, type Screen } from "./types";
import { MISSION_PATH } from "../mock";

const live = { ...IN, terminalScreen: true };
const output = async (p: Page) => {
  await expect(p.locator("#terminal-status")).toContainText("Watching, read only");
  await expect(p.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs");
};
const control = async (p: Page) => {
  await output(p);
  await p.locator('[data-action="take-control"]').click();
  await expect(p.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
};

export const SCREENS: Screen[] = [
  { name: "mission-live-output", mock: live, path: MISSION_PATH, run: output },
  { name: "session-control", mock: live, path: MISSION_PATH, run: async (p) => { await control(p); await p.keyboard.type("run the tests again"); } },
  { name: "session-view-only", mock: { ...live, control: "deny" }, path: MISSION_PATH, run: async (p) => { await output(p); await expect(p.locator("[data-composer]")).toContainText("This worker allows viewing only"); } },
  { name: "session-steer-menu", mock: live, path: MISSION_PATH, run: async (p) => { await output(p); await p.locator('[data-composer] button[aria-haspopup="menu"]').click(); await expect(p.locator('[data-steer="handoff"]')).toBeVisible(); } },
  { name: "session-steer-dialog", mock: live, path: MISSION_PATH, run: async (p) => { await output(p); await p.locator('[data-composer] button[aria-haspopup="menu"]').click(); await p.locator('[data-steer="review"]').click(); await expect(p.getByRole("dialog")).toContainText("Instructions (optional)"); await expect(p.getByRole("dialog").locator("select option").first()).toHaveText(/Plan the change/); } },
  { name: "session-queued-steer", mock: live, path: MISSION_PATH, run: async (p) => { await output(p); await p.locator('[data-composer] button[aria-haspopup="menu"]').click(); await p.locator('[data-steer="pause-after-iteration"]').click(); await expect(p.locator("[data-queued-steer]")).toContainText("Queued: pause after this iteration"); await expect(p.locator(".toast")).toBeVisible(); } },
  { name: "session-transcript", mock: live, path: MISSION_PATH, run: async (p) => { await output(p); await p.getByRole("button", { name: "Transcript" }).click(); await expect(p.locator('[data-pane="session"] pre[tabindex="0"]')).toContainText("Reading src/tokenizer.rs"); } },
  { name: "session-more-keys", mock: live, path: MISSION_PATH, narrowOnly: true, run: async (p) => { await control(p); await p.locator("#key-more").click(); await expect(p.locator("#key-pgdn")).toBeVisible(); } },
];
