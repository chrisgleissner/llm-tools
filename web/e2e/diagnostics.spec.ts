// Diagnostics view (PRD-011, PRD-013): overall status, one row per check, the backup panel, and the old-coordinator shape.
// The response is a fixture served over the standard mock; the pure helpers are imported from the production module.
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { mockApp } from "./mock";
import { IN } from "./screens/types";
import { ageWords, backupRows, checkRows, valueText } from "../src/views/diagnostics";

const OLD = { version: "0.9.2", protocol_version: 1, uptime_seconds: 93784, rss_kib: 38400, db_bytes: 466944, sqlite_version: "3.46.0", connected_workers: 2, command_queue_depths: { beta: { pending: 1 } } };
const NEW = {
  ...OLD,
  status: "fail",
  checks: {
    uptime: { value: 93784, status: "ok", fix: null },
    command_queue: { value: 120, status: "fail", fix: "Check that the workers named under Command queues are online." },
    last_backup: { value: { age_seconds: 200000 }, status: "warn", fix: "Run bridge-coordinator backup." },
  },
  backup: { enabled: true, interval_seconds: 86400, keep: 7, dir: "/var/lib/bridge/backups", count: 3, last_backup_at: 1_793_000_000, last_backup_age_seconds: 11_520, last_backup_bytes: 2_097_152, last_attempt_at: 1_793_000_000, last_result: "skipped", last_reason: "free space below the budget", next_due_at: 1_793_086_400 },
};

async function open(page: Page, body: unknown): Promise<void> {
  await mockApp(page, IN);
  await page.route("**/api/v1/diagnostics", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }));
  await page.goto("/diagnostics");
}

test("check rows keep order, normalise unknown statuses and drop the fix of an ok check", () => {
  const rows = checkRows(NEW.checks);
  expect(rows.map((r) => [r.name, r.status, r.fix === null])).toEqual([
    ["uptime", "ok", true],
    ["command_queue", "fail", false],
    ["last_backup", "warn", false],
  ]);
  expect(checkRows({ x: { value: 1, status: "weird", fix: "f" }, y: { value: 2, status: "ok", fix: "ignored" } }).map((r) => [r.status, r.fix])).toEqual([["warn", "f"], ["ok", null]]);
  expect(checkRows(undefined)).toEqual([]);
  expect(checkRows([1])).toEqual([]);
  expect(valueText({ a: 1 })).toBe('{"a":1}');
  expect(valueText(null)).toBe("-");
});

test("backup rows show age size kept next due and last result", () => {
  const rows = Object.fromEntries(backupRows(NEW.backup, 1_793_000_000_000)!);
  expect(rows["Last backup"]).toContain("3 h 12 min ago");
  expect(rows["Size"]).toBe("2.0 MB");
  expect(rows["Kept"]).toBe("3 of 7");
  expect(rows["Last attempt"]).toBe("skipped: free space below the budget");
  expect(rows["Automatic backups"]).toBe("On, every 24 h 0 min");
  expect(ageWords(90)).toBe("1 min 30 s ago");
  expect(Object.fromEntries(backupRows({ enabled: true, keep: 7, count: 0 })!)["Last backup"]).toBe("None yet");
  expect(backupRows({ enabled: false })).toEqual([["Automatic backups", "Off"]]);
  expect(backupRows(undefined)).toBeNull();
});

test("new-shape response shows overall status check rows and the backup panel", async ({ page }) => {
  await open(page, NEW);
  await expect(page.locator("#diag-overall")).toContainText("Overall:");
  await expect(page.locator("#diag-overall")).toContainText("Failing");
  const rows = page.locator("#diag-checks li");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("OK");
  await expect(rows.nth(0)).not.toContainText("Fix:");
  await expect(rows.nth(1)).toContainText("Failing");
  await expect(rows.nth(1)).toContainText("Fix: Check that the workers");
  await expect(rows.nth(2)).toContainText("Warning");
  const panel = page.locator("section[aria-labelledby=dg-backup]");
  await expect(panel).toContainText("2.0 MB");
  await expect(panel).toContainText("3 of 7");
  await expect(panel).toContainText("skipped: free space below the budget");
  await expect(panel).toContainText("bridge-coordinator restore");
  await expect(panel).not.toContainText("backup_interval_hours");
});

test("disabled backups say so and name the configuration key", async ({ page }) => {
  await open(page, { ...NEW, backup: { enabled: false, interval_seconds: null, keep: 7, dir: "/x", count: 0 } });
  const panel = page.locator("section[aria-labelledby=dg-backup]");
  await expect(panel).toContainText("Off");
  await expect(panel).toContainText("backup_interval_hours");
});

test("old-shape response renders the previous view without errors", async ({ page }) => {
  const problems: string[] = [];
  page.on("pageerror", (e) => problems.push(e.message));
  await open(page, OLD);
  await expect(page.getByRole("heading", { name: "Coordinator" })).toBeVisible();
  await expect(page.locator("dt", { hasText: "Protocol" })).toBeVisible();
  await expect(page.locator("#diag-overall")).toHaveCount(0);
  await expect(page.locator("#diag-checks")).toHaveCount(0);
  await expect(page.locator("section[aria-labelledby=dg-backup]")).toHaveCount(0);
  expect(problems).toEqual([]);
});
