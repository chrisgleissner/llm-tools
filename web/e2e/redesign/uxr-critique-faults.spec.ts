// T49 (UXR-053 to UXR-057): the five faults that the fresh-eyes critique (UXR-040, docs/distributed/ux-audit.md, "Fresh-eyes critique") found and D48 registered.
// Each test measures computed style or geometry and fails when the fix of its row is undone. Desktop Chromium only: the widths are set here.
import { test, type Page, type TestInfo } from "@playwright/test";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { MISSION_URL, openApp } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-3", "WP-4"] as const;
const DENSE = { auth: "in", dataset: "dense" } as const;
const only = (info: { project: { name: string } }): boolean => info.project.name === "chromium-desktop";

// ---- UXR-053 ------------------------------------------------------------------------------------------------------------
uxr({ t: "T49", uxr: ["UXR-053"], needs: [...NEEDS] }, "UXR-053: at 360 every provider chip in the top bar shows its percentage left, Codex included", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await page.setViewportSize({ width: 360, height: 640 });
  await openApp(page, DENSE, "/");
  await expect(page.locator("#capacity .cap-chip")).toHaveCount(2); // Claude (waiting) and Codex (ready)
  const chips = await page.locator("#capacity .cap-chip").evaluateAll((els) => {
    const box = document.querySelector("#capacity")!.getBoundingClientRect();
    return els.map((el) => {
      const pct = el.querySelector<HTMLElement>(".cap-pct")!;
      const r = pct.getBoundingClientRect();
      const c = el.getBoundingClientRect();
      return { name: el.querySelector(".cap-name")!.firstChild!.textContent, pct: pct.textContent!.trim(), shown: getComputedStyle(pct).display !== "none" && r.width > 8 && r.height > 8, inside: c.left >= box.left - 0.5 && c.right <= box.right + 0.5 };
    });
  });
  expect(chips.map((c) => c.name)).toEqual(["Claude", "Codex"]);
  expect(chips.map((c) => c.pct)).toEqual(["4%", "75%"]);
  for (const c of chips) {
    expect(c.shown, `${c.name}: the percentage left is visible`).toBe(true);
    expect(c.inside, `${c.name}: the chip is not clipped by the header`).toBe(true);
  }
  await expect(page.locator("#capacity .cap-chip.waiting .cap-reset"), "the waiting chip still says when capacity returns").toContainText(/resets \d{2}:\d{2}/);
  await evidence(page, info, ["UXR-053"], "quota-chips-360");
});

// ---- UXR-054 ------------------------------------------------------------------------------------------------------------
async function filterIsNotClipped(page: Page, path: string, info: TestInfo): Promise<void> {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await page.setViewportSize({ width: 360, height: 640 });
  await openApp(page, DENSE, path);
  const select = page.locator("#worker-select");
  await expect(select).toBeVisible();
  const m = await select.evaluate((el: HTMLSelectElement) => {
    // The width the same select takes when nothing limits it: what its longest option needs.
    const probe = el.cloneNode(true) as HTMLSelectElement;
    probe.removeAttribute("id");
    probe.style.cssText = "position:absolute;visibility:hidden;inline-size:auto;min-inline-size:0;max-inline-size:none";
    el.parentElement!.append(probe);
    const natural = probe.getBoundingClientRect().width;
    probe.remove();
    const r = el.getBoundingClientRect();
    return { width: r.width, natural, right: r.right, vw: innerWidth };
  });
  expect(m.width, `the select (${m.width}px) shows all of its text (${m.natural}px)`).toBeGreaterThanOrEqual(m.natural - 0.5);
  expect(m.right, "and stays on the screen").toBeLessThanOrEqual(m.vw);
  await evidence(page, info, ["UXR-054"], `all-workers-${path.slice(1)}-360`);
}

uxr({ t: "T49", uxr: ["UXR-054"], needs: [...NEEDS] }, "UXR-054: at 360 the 'All workers' filter on Usage is not clipped", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await filterIsNotClipped(page, "/usage", info);
});

uxr({ t: "T49", uxr: ["UXR-054"], needs: [...NEEDS] }, "UXR-054: at 360 the 'All workers' filter on Sessions is not clipped", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await filterIsNotClipped(page, "/sessions", info);
});

// ---- UXR-055 ------------------------------------------------------------------------------------------------------------
uxr({ t: "T49", uxr: ["UXR-055"], needs: [...NEEDS] }, "UXR-055: at 1440 an approval row keeps its pill on one line and does not cut its reason", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await openApp(page, DENSE, "/");
  const row = page.locator('[data-section="needs-you"] [data-state="needs-approval"]').first();
  await expect(row.locator(".tail")).toBeVisible();
  const m = await row.evaluate((el) => {
    const pill = el.querySelector<HTMLElement>(".pill")!;
    const head = el.querySelector<HTMLElement>(".row-reason > span:first-child")!;
    const tail = el.querySelector<HTMLElement>(".tail")!;
    const t = getComputedStyle(tail);
    return {
      pillHeight: pill.getBoundingClientRect().height, pillClipped: pill.scrollWidth > pill.clientWidth + 1, pillWrap: getComputedStyle(pill).whiteSpace,
      headCut: head.scrollWidth > head.clientWidth + 1, head: head.textContent, clamp: t.getPropertyValue("-webkit-line-clamp"), tailWrap: t.whiteSpace,
      tailHeight: tail.getBoundingClientRect().height, lineHeight: parseFloat(t.lineHeight),
    };
  });
  expect(m.pillHeight, "the pill is one line high").toBeLessThan(34);
  expect(m.pillClipped, "the pill text is not clipped").toBe(false);
  expect(m.pillWrap).toBe("nowrap");
  expect(m.headCut, `the reason "${m.head}" is shown whole`).toBe(false);
  expect(m.clamp, "the summary clamps at two lines like every clamp, but is one line high: it is cut after a whole word").toBe("2");
  expect(m.tailHeight, "and it shows one line").toBeLessThan(m.lineHeight * 1.5);
  expect(m.tailWrap, "the summary can break between words").not.toBe("nowrap");
  await evidence(page, info, ["UXR-055"], "approval-row-1440");
});

// ---- UXR-056 ------------------------------------------------------------------------------------------------------------
uxr({ t: "T49", uxr: ["UXR-056"], needs: [...NEEDS] }, "UXR-056: at 1440 a failed row on Missions shows the provider of its last run", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await openApp(page, DENSE, "/missions");
  const failed = page.locator('[data-state="failed"] [data-cell="provider"]');
  await expect(failed.first()).toBeVisible();
  const n = await failed.count();
  expect(n, "the dense data set lists failed missions").toBeGreaterThan(0);
  for (let i = 0; i < n; i++) await expect(failed.nth(i), `failed row ${i}: provider cell`).toHaveText(/^Codex\b/);
  await evidence(page, info, ["UXR-056"], "failed-row-provider-1440");
});

// ---- UXR-057 ------------------------------------------------------------------------------------------------------------
uxr({ t: "T49", uxr: ["UXR-057"], needs: [...NEEDS] }, "UXR-057: at 360 a terminal that scrolls sideways shows a chevron at the edge with more behind it", async ({ page }, info) => {
  test.skip(!only(info), "measured at the widths set here, in desktop Chromium");
  await page.setViewportSize({ width: 360, height: 640 });
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  const host = page.locator("#terminal-host");
  await expect(host).toBeVisible();
  await expect.poll(() => host.evaluate((el) => el.scrollWidth - el.clientWidth), { message: "the terminal is wider than the pane" }).toBeGreaterThan(8);
  const more = page.locator(".pan-cue.more");
  const back = page.locator(".pan-cue.back");
  await expect(more, "more to the right: a cue on the right edge").toBeVisible();
  await expect(back, "nothing to the left yet").toBeHidden();
  const inside = await more.evaluate((el) => { const c = el.getBoundingClientRect(); const h = document.querySelector("#terminal-host")!.getBoundingClientRect(); return { text: el.textContent, right: Math.abs(c.right - h.right) <= 1, wide: c.width >= 16 && c.height >= 16 }; });
  expect(inside).toEqual({ text: "›", right: true, wide: true });
  await host.evaluate((el) => el.scrollTo({ left: el.scrollWidth }));
  await expect(more, "at the right end the right cue goes").toBeHidden();
  await expect(back, "and a cue on the left edge shows the way back").toBeVisible();
  await host.evaluate((el) => el.scrollTo({ left: 0 }));
  await evidence(page, info, ["UXR-057"], "terminal-pan-cue-360");
});
