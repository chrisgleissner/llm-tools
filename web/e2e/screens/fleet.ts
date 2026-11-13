// Screens owned by the fleet package (PRD-037). Names are baseline file names, so they stay unique across packages.
import { expect } from "../fixtures";
import { IN, type Screen } from "./types";

const ENROL_TOKEN = "k3Qx9ZpL2mNvB8sTr5WyHc1DfGa7JeUo4IiXb6AnOqE";

export const SCREENS: Screen[] = [
  { name: "workers", mock: IN, path: "/workers", run: async (p) => { await expect(p.locator('[data-entity^="worker:"]')).toHaveCount(3); const alpha = p.locator('[data-entity="worker:alpha"]'); await expect(alpha).toContainText("build-node"); await expect(alpha).toContainText("203.0.113.10"); await expect(p.locator('[data-entity^="worker:"]').first()).toContainText("beta"); } },
  { name: "workers-none", mock: { auth: "in", workers: "none" }, path: "/workers", run: (p) => expect(p.getByRole("heading", { name: "No workers enrolled" })).toBeVisible() },
  { name: "workers-add-dialog", mock: IN, path: "/workers", run: async (p) => { await p.locator("#add-worker-button").click(); await p.getByLabel("Worker name").fill("Bad Name"); await p.locator("#add-worker").getByRole("button", { name: "Add worker" }).click(); await expect(p.locator("#worker-name-error")).toContainText("lowercase"); } },
  { name: "workers-enrol-token", mock: IN, path: "/workers", run: async (p) => { await p.route("**/api/v1/workers", (route) => (route.request().method() === "POST" ? route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ name: "build-1", token: ENROL_TOKEN }) }) : route.fallback())); await p.locator("#add-worker-button").click(); await p.getByLabel("Worker name").fill("build-1"); await p.locator("#add-worker").getByRole("button", { name: "Add worker" }).click(); await expect(p.locator("#worker-token")).toHaveText(ENROL_TOKEN); await p.locator("#enrol-result pre.cmd").evaluate((el) => { el.textContent = (el.textContent ?? "").replace(/wss:\/\/\S+/, "wss://coordinator.example:8443"); }); } },
  { name: "workers-offline-why", mock: IN, path: "/workers", run: async (p) => { await p.locator('[data-entity="worker:beta"]').getByRole("button", { name: "Why offline?" }).click(); await expect(p.locator('[data-entity="worker:beta"]')).toContainText("last heard from beta"); } },
  { name: "sessions", mock: IN, path: "/sessions", run: async (p) => { await expect(p.locator('[data-entity^="session:"]:visible')).toHaveCount(1); await expect(p.locator('[data-section="history"] summary')).toContainText("History"); } },
  { name: "sessions-history-open", mock: IN, path: "/sessions", run: async (p) => { await p.locator('[data-section="history"] summary').click(); await expect(p.locator('[data-entity^="session:"]:visible')).toHaveCount(2); } },
  { name: "sessions-none", mock: { auth: "in", sessions: "none" }, path: "/sessions", run: (p) => expect(p.getByRole("heading", { name: "No provider sessions yet" })).toBeVisible() },
  { name: "usage", mock: IN, path: "/usage", run: async (p) => { await expect(p.locator('[role="meter"]').first()).toBeVisible(); await expect(p.locator('[data-entity^="provider:"]').first()).toContainText("Claude"); } },
  { name: "usage-none", mock: { auth: "in", usage: "none" }, path: "/usage", run: (p) => expect(p.getByRole("heading", { name: "No usage data yet" })).toBeVisible() },
  { name: "diagnostics", mock: IN, path: "/diagnostics", run: async (p) => { await expect(p.getByRole("heading", { name: "Coordinator" })).toBeVisible(); await expect(p.getByText("26 h 3 min").first()).toBeVisible(); } },
];
