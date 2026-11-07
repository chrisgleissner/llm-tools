// Screens owned by the inbox package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, heading, live, type Screen } from "./types";
import { MISSIONS, MISSION_PATH } from "../mock";

export const SCREENS: Screen[] = [
  { name: "overview", mock: IN, path: "/", run: async (p) => { await live(p); await expect(p.locator('[data-attention="approval"]').first()).toBeVisible(); await expect(p.locator("#activity li").first()).toBeVisible(); await expect(p.locator(".running-list")).toContainText("Implement the new tokenizer"); await expect(p.locator(".transition-summary").first()).toContainText("RUNNING → VERIFYING"); } },
  { name: "overview-no-missions", mock: { ...IN, missions: "none" }, path: "/", run: async (p) => { await expect(p.getByText("Next: create a mission on a worker.")).toBeVisible(); await expect(p.locator("#ov-health")).toBeVisible(); } },
  { name: "provider-question", mock: { ...IN, providerQuestion: true }, path: "/", run: async (p) => { const row = p.locator('[data-attention="provider-wait"]'); await expect(row).toHaveCount(1); await expect(row).toContainText("Should I continue?"); await expect(row).toContainText("1. Continue; 2. Stop"); await expect(row).toContainText("take control of the terminal"); } },
  { name: "overview-no-workers", mock: { auth: "in", workers: "none" }, path: "/", run: async (p) => { await expect(p.getByRole("heading", { name: "No workers yet" })).toBeVisible(); } },
  { name: "missions", mock: IN, path: "/missions", run: async (p) => { await expect(p.locator("#missions-table tbody tr")).toHaveCount(8); await expect(p.locator(".now-doing")).toContainText("Implement the new tokenizer"); } },
  { name: "missions-provider-question", mock: { ...IN, providerQuestion: true }, path: "/missions", run: async (p) => { const row = p.locator(`#missions-table tbody tr[data-mission="${MISSIONS[0]!.mission.id}"]`); await expect(row.locator("[data-needs-you=provider]")).toContainText("Codex asks: Should I continue?"); await expect(row.getByRole("link", { name: "Open mission to answer" })).toHaveAttribute("href", MISSION_PATH); } },
  { name: "missions-filtered", mock: IN, path: "/missions?status=RUNNING", run: async (p) => { await expect(p.locator("#missions-table tbody tr")).toHaveCount(2); } },
  { name: "missions-no-match", mock: IN, path: "/missions?q=zzz", run: (p) => expect(p.getByRole("heading", { name: "No missions match" })).toBeVisible() },
  { name: "missions-none", mock: { auth: "in", missions: "none" }, path: "/missions", run: (p) => expect(p.getByRole("heading", { name: "No missions yet" })).toBeVisible() },
];
