// Screens owned by the fleet package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, heading, type Screen } from "./types";

export const SCREENS: Screen[] = [
  { name: "workers", mock: IN, path: "/workers", run: async (p) => { await expect(p.locator("#workers-table tbody tr")).toHaveCount(3); const alpha = p.locator('tr[data-worker="alpha"]'); await expect(alpha.locator('[data-label="Host"]')).toHaveText("build-node"); await expect(alpha.locator('[data-label="Address"]')).toHaveText("203.0.113.10"); } },
  { name: "workers-none", mock: { auth: "in", workers: "none" }, path: "/workers", run: (p) => expect(p.getByRole("heading", { name: "No workers enrolled" })).toBeVisible() },
  { name: "workers-add-dialog", mock: IN, path: "/workers", run: async (p) => { await p.locator("#add-worker-button").click(); await p.getByLabel("Worker name").fill("Bad Name"); await p.locator("#add-worker").getByRole("button", { name: "Add worker" }).click(); await expect(p.locator("#worker-name-error")).toContainText("lowercase"); } },
  { name: "sessions", mock: IN, path: "/sessions", run: (p) => expect(p.locator("#sessions-table tbody tr")).toHaveCount(2) },
  { name: "usage", mock: IN, path: "/usage", run: (p) => expect(p.locator("#usage-table tbody tr")).toHaveCount(3) },
  { name: "diagnostics", mock: IN, path: "/diagnostics", run: (p) => expect(p.getByRole("heading", { name: "Coordinator" })).toBeVisible() },
];
