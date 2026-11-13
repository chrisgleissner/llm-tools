// The Inbox and Missions read what needs the operator from the `attention` and `task_counts` fields of the mission list
// (PRD-037 WP-7). The mock of e2e/mock.ts predates those fields, so this spec adds them to the list answer in the page and
// checks that no per-mission status read is made, and that an older worker (no fields) still gets its status read.
import { MISSIONS } from "./mock";
import { expect, test } from "./fixtures";
import { openApp } from "./redesign/harness";

const FIRST = MISSIONS[0]!.mission.id;

test.beforeEach(async ({ page }) => {
  await page.addInitScript((first) => {
    const original = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const res = await original(input, init);
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
      if (url.pathname !== "/api/v1/missions" || !res.ok) return res;
      const body = (await res.json()) as { missions: Array<{ mission: Record<string, unknown> }> };
      for (const m of body.missions) {
        m.mission["task_counts"] = { COMPLETED: 2 };
        m.mission["attention"] = m.mission["id"] === first ? { kind: "approval", message: "task 2 awaits approval", tasks: [2] } : null;
      }
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    };
  }, FIRST);
});

test("the Inbox takes attention and task counts from the mission list without reading each mission", async ({ page }) => {
  const app = await openApp(page, { auth: "in" }, "/");
  const approval = page.locator('[data-section="needs-you"] .mrow[data-state="needs-approval"]');
  await expect(approval).toHaveCount(1);
  await expect(approval.locator('[data-action="approve"]')).toBeVisible();
  await expect(page.locator('[data-q="need-me"]')).toContainText(/\d+ needs? you/);
  await page.waitForTimeout(800);
  const statusReads = app.requests().filter((r) => (r.body?.["request"] as { operation?: string } | undefined)?.operation === "mission.status");
  expect(statusReads, "mission.status reads made for the Inbox").toEqual([]);
});

test("the Missions list shows progress from the task counts of the list", async ({ page }) => {
  const app = await openApp(page, { auth: "in" }, "/missions");
  const row = page.locator(`[data-entity="mission:alpha/${FIRST}"]`);
  await expect(row.locator('[data-cell="progress"]')).toHaveText("2/2");
  await expect(row.locator(".pill")).toHaveAttribute("data-state", "needs-approval");
  expect(app.requests().some((r) => (r.body?.["request"] as { operation?: string } | undefined)?.operation === "mission.status")).toBe(false);
});
