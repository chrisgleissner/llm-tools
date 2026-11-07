// Screens owned by the mission package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, type Screen } from "./types";
import { MISSION_PATH } from "../mock";

export const SCREENS: Screen[] = [
  { name: "mission-detail", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await expect(p.locator("#events-list li").first()).toBeVisible(); await expect(p.locator("#events-list .transition-summary")).toContainText("RUNNING → VERIFYING"); await expect(p.locator("#events-list")).not.toContainText("do-not-render-this"); await expect(p.locator("#mission-status")).toContainText("healthy"); await expect(p.locator("#mission-current-work")).toContainText("Implement the new tokenizer"); await expect(p.locator("#terminal-status")).toHaveAttribute("data-mode", "view"); } },
  { name: "mission-confirm-reject", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await p.getByRole("button", { name: "Reject task 2" }).click(); await expect(p.getByRole("dialog")).toBeVisible(); } },
  { name: "mission-toast-applied", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await p.getByRole("button", { name: "Pause task 5" }).click(); await expect(p.locator(".toast.applied")).toBeVisible(); } },
];
