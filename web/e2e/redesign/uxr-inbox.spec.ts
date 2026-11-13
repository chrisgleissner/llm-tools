// T19, T20 (UXR-012, UXR-013, UXR-018, UXR-019): the Inbox. T18 (fold) lives in uxr-matrix.spec.ts because it runs at every viewport.
import type { Page } from "@playwright/test";
import { DIFF_STATS, OUTPUT_TAIL } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { openApp } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-4"] as const;
const DENSE = { auth: "in", dataset: "dense" } as const;

const approvalRow = (page: Page) => page.locator('[data-section="needs-you"] [data-entity^="mission:"]', { has: page.locator('[data-action="approve"]') }).first();

uxr({ t: "T19", uxr: ["UXR-013"], needs: [...NEEDS] }, "running rows show what the provider is doing and how long ago, without a click and without 'Provider unknown'", async ({ page }, info) => {
  const app = await openApp(page, DENSE, "/");
  const rows = page.locator('[data-section="running"] [data-entity^="mission:"]');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count(), "running rows").toBeGreaterThan(0);
  const texts = await rows.evaluateAll((els) => els.slice(0, 8).map((e) => (e as HTMLElement).innerText));
  for (const text of texts) {
    expect(text, "each running row shows when it was updated").toMatch(/updated\s+(now|just now|\d+\s?(s|m|h|d|min))/i);
    expect(text, "unknown parts are omitted, never printed").not.toMatch(/unknown/i);
    expect(/codex|claude|gpt-5/i.test(text) || text.includes(OUTPUT_TAIL.at(-1)!), `a provider or output line in: ${text}`).toBe(true);
  }
  expect(await app.ix.count(), "interactions needed to see it").toBe(0);
  await evidence(page, info, ["UXR-013"], "running-rows");
});

uxr({ t: "T20", uxr: ["UXR-018"], needs: [...NEEDS] }, "Approve opens an evidence dialog (task, verifier line, changed files) and completes in 2 interactions", async ({ page }, info) => {
  const app = await openApp(page, DENSE, "/");
  const row = approvalRow(page);
  await expect(row).toBeVisible();
  await row.locator('[data-action="approve"]').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog, "task title").toContainText("Implement the new tokenizer");
  await expect(dialog, "verifier line").toContainText(/verif/i);
  await expect(dialog, "changed files").toContainText(new RegExp(`${DIFF_STATS.files} files`));
  await expect(dialog).toContainText(`+${DIFF_STATS.added}`);
  await expect(dialog).toContainText(`-${DIFF_STATS.removed}`);
  await evidence(page, info, ["UXR-018"], "approve-dialog");
  expect(app.operations().includes("task.verify"), "nothing is sent before the confirmation").toBe(false);
  await dialog.locator("[data-confirm]").click();
  await expect.poll(() => app.operations()).toContain("task.verify");
  expect(app.requests().find((r) => r.operation === "task.verify")?.body?.["approved"], "approved flag").not.toBe(false);
  expect(await app.ix.count(), "interactions: Approve, confirm").toBe(2);
});

uxr({ t: "T20", uxr: ["UXR-019"], needs: [...NEEDS] }, "Reject with an empty reason shows an inline error and sends nothing", async ({ page }, info) => {
  const app = await openApp(page, DENSE, "/");
  await approvalRow(page).locator('[data-action="reject"]').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator("[data-confirm]").click();
  await expect(dialog.locator('[role="alert"], [aria-invalid="true"], .field-error').first(), "inline validation message").toBeVisible();
  await expect(dialog, "the dialog stays open").toBeVisible();
  await evidence(page, info, ["UXR-019"], "reject-empty-reason");
  await page.waitForTimeout(300);
  expect(app.operations().includes("task.reject"), "a rejection was sent with no reason").toBe(false);
});

uxr({ t: "T20", uxr: ["UXR-019"], needs: [...NEEDS] }, "Reject with a reason completes in 3 interactions", async ({ page }) => {
  const app = await openApp(page, DENSE, "/");
  await approvalRow(page).locator('[data-action="reject"]').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("textarea, input[type=text], input:not([type])").first(), "the reason field takes focus").toBeFocused();
  await page.keyboard.type("The verifier output is not enough");
  await dialog.locator("[data-confirm]").click();
  await expect.poll(() => app.operations()).toContain("task.reject");
  expect(JSON.stringify(app.requests().find((r) => r.operation === "task.reject")?.body), "the reason is in the request").toContain("The verifier output is not enough");
  expect(await app.ix.count(), "interactions: Reject, type, confirm").toBe(3);
});
