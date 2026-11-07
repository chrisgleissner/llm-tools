// T47 (UXR-040): the defects of the fresh-eyes critique (watch/prd037-critique.md, findings F2 to F9, F37) that were real product
// defects, each with the assertion that fails when its fix is reverted. The finding number is in the title.
import { DENSE_CALM_URL, MISSIONS, MISSION_START_PATH } from "../mock";
import { expect, uxr } from "./gate";
import { isPhone, MISSION_URL, openApp, showTab } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-3", "WP-4"] as const;
const DENSE = { auth: "in", dataset: "dense" } as const;
const LIVE = { auth: "in", terminalScreen: true } as const;

// ---- F2: a failed row says which task failed, without commands for a shell, and offers Retry -------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F2: a failed Inbox row names the failing task, shows no shell command and offers Retry", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  const row = page.locator('[data-section="needs-you"] [data-state="failed"]').first();
  await expect(row.locator('[data-action="retry"]'), "Retry once the failing task is known").toBeVisible();
  await expect(row).toContainText(/Task 2 failed/);
  const text = (await row.innerText()).toLowerCase();
  expect(text, "commands for a shell are not shown in the console").not.toMatch(/llm-bridge|inspect with/);
  await expect(page.locator("[data-q=next]")).toHaveText("Retry or open");
});

// ---- F3: one count of what needs the operator on every page ---------------------------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F3: the Inbox badge, the tab title and the Missions answer line show the same count", async ({ page }) => {
  await openApp(page, DENSE, "/missions");
  const need = page.locator("[data-q=need-me]");
  await expect(need).not.toHaveText("Checking...");
  const badge = page.locator(".main-nav .nav-count");
  // The status of the missions that need a read arrives after the first paint: the badge follows it (it used to keep the earlier, smaller number).
  const figure = async (el: typeof badge): Promise<string> => ((await el.textContent()) ?? "").match(/^\d+\+?/)?.[0] ?? "";
  await expect.poll(async () => { const [b, n] = [await figure(badge), await figure(need)]; return b !== "" && b === n ? "same" : `badge ${b}, line ${n}`; }, { message: "the badge equals the Missions answer line" }).toBe("same");
  const count = await figure(badge);
  expect(await page.title(), "the tab title carries the same number").toContain(`(${count.replace("+", "")})`);
  // More missions are unread than the fan-out reads, so the number says it is a lower bound.
  expect(count, "a count that may be too small says so").toMatch(/\+$/);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Inbox" }).click();
  await expect(page.locator("[data-q=need-me]")).not.toHaveText("Checking...");
  await expect.poll(async () => { const [b, n] = [await figure(badge), await figure(page.locator("[data-q=need-me]"))]; return b !== "" && b === n ? "same" : `badge ${b}, line ${n}`; }, { message: "the badge equals the Inbox answer line" }).toBe("same");
});

// ---- F4: the outcome of an action is said in words -------------------------------------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F4: a successful Pause says 'Mission paused.', with no 'Applied.' prefix", async ({ page }) => {
  await openApp(page, DENSE, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  await expect(page.locator(".toast")).toHaveText(/^Mission paused\.$/);
});

uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F4: a refused Pause says what could not be done, not 'Rejected.' and not the fixture text", async ({ page }) => {
  await openApp(page, { ...DENSE, failMutations: true }, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  const toast = page.locator(".toast");
  await expect(toast).toHaveText("Could not pause the mission. The worker refused the change.");
  await expect(toast).not.toContainText(/Rejected|\(test\)/);
});

// ---- F5: a toast never covers the header (phones: it sits above the lowest bar) -------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F5: a toast covers neither the page header nor the mission header and stays inside the screen", async ({ page }) => {
  await openApp(page, DENSE, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  const toast = page.locator(".toast");
  await expect(toast).toBeVisible();
  const box = (await toast.boundingBox())!;
  const view = page.viewportSize()!;
  expect(box.y + box.height, "inside the screen").toBeLessThanOrEqual(view.height);
  for (const selector of ["header.top", '[data-l0="mission.header"] h1']) {
    const other = (await page.locator(selector).first().boundingBox())!;
    const overlap = box.x < other.x + other.width && box.x + box.width > other.x && box.y < other.y + other.height && box.y + box.height > other.y;
    expect(overlap, `the toast covers ${selector}`).toBe(false);
  }
  if (isPhone(page)) {
    // Above the lowest bar: the action dock (or the composer when it sits higher).
    const bars = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("[data-composer], .mission-head .head-actions, .main-nav")].filter((e) => e.offsetWidth > 0).map((e) => e.getBoundingClientRect().top).filter((t) => t > window.innerHeight / 2));
    expect(box.y + box.height, "above the bars at the bottom").toBeLessThanOrEqual(Math.min(...bars) + 0.5);
  }
});

// ---- F6: giving control back is a visible button --------------------------------------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS, "WP-2"] }, "F6: while controlling, Release is a visible button (not only in the menu) and returns to read-only", async ({ page }) => {
  const app = await openApp(page, LIVE, MISSION_URL);
  await expect(page.locator("#terminal-status")).toContainText("Watching, read only");
  await expect(page.locator('[data-action="release"]')).toBeHidden();
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  const release = page.locator('[data-action="release"]');
  await expect(release, "Release is visible without opening a menu").toBeVisible();
  await release.click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "view");
  expect(app.terminalSockets().length, "a new read-only connection replaced the writer").toBeGreaterThanOrEqual(3);
  await expect(release).toBeHidden();
});

// ---- F7: one primary button on the mission page ---------------------------------------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS, "WP-2"] }, "F7: the mission page has one primary button (the header's); task rows and Take control are secondary", async ({ page }) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  await expect(page.locator('[data-l0="mission.header"] [data-action="approve"]')).toBeVisible();
  const primaries = async () => page.locator("main .btn.primary:visible").evaluateAll((els) => els.map((e) => (e.getAttribute("data-action") ?? e.textContent ?? "").trim()));
  expect(await primaries(), "session view").toEqual(["approve"]);
  await showTab(page, "Tasks");
  await expect(page.locator('[data-task="3"] [data-action="retry"]')).toBeVisible();
  expect(await primaries(), "tasks view").toEqual(["approve"]);
});

// ---- F8: the evidence for the decision is shown, and it says the same everywhere ---------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F8: Evidence opens for the task awaiting approval, the header quotes the verifier, and the Inbox dialog agrees", async ({ page }) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  await showTab(page, "Tasks");
  const verifier = page.locator(".evidence-verifier");
  await expect(verifier, "Evidence is open without choosing the task").toContainText("Verifier: cargo test exited 0: 128 passed, 0 failed");
  await expect(verifier).not.toContainText("No verifier result");
  await expect(page.locator('[data-l0="mission.header"] [data-q="what"]'), "the header sentence carries the verifier result").toContainText("Verifier: cargo test exited 0: 128 passed, 0 failed");
  await page.goto("/");
  const row = page.locator('[data-section="needs-you"] [data-entity^="mission:"]', { has: page.locator('[data-action="approve"]') }).first();
  await expect(row, "the approval row says what it rests on").toContainText("verifier: cargo test exited 0");
  await row.locator('[data-action="approve"]').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Verifier: cargo test exited 0: 128 passed, 0 failed");
  await expect(dialog, "the label is not doubled").not.toContainText(/Verifier: Verifier/);
});

// ---- F9: the header and the task list tell one story -------------------------------------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F9: an approval hint that the task list contradicts is not shown: no Needs approval pill and no Approve for a queued task", async ({ page }) => {
  const app = await openApp(page, { auth: "in" }, MISSION_START_PATH);
  const header = page.locator('[data-l0="mission.header"]');
  await expect(header.locator(".pill").first()).toContainText("Queued");
  // The status projection still says "awaiting approval" while the task list (read separately) shows both tasks queued.
  app.setNeeds(MISSIONS[5]!.mission.id, { kind: "approval", tasks: [2], message: "task 2 awaits your review" });
  app.setTasks(MISSIONS[5]!.mission.id, ["QUEUED", "QUEUED"]);
  const id = MISSIONS[5]!.mission.id;
  const reads = (suffix: string) => app.requests().filter((r) => r.method === "GET" && r.path === `/missions/${id}${suffix}`).length;
  const [statusBefore, tasksBefore] = [reads(""), reads("/tasks")];
  await page.reload();
  // Both reads must have been answered before the header is judged: until then it shows only what the list says.
  await expect.poll(() => [reads("") > statusBefore, reads("/tasks") > tasksBefore], { message: "the status and the task list were read again" }).toEqual([true, true]);
  await page.waitForTimeout(500);
  await expect(header.locator(".pill").first()).toContainText("Queued");
  await expect(header.locator('[data-action="approve"]'), "Approve is not offered for a task that is not awaiting it").toHaveCount(0);
});

uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS] }, "F9: after Pause the running task says the mission is paused", async ({ page }) => {
  // A slow answer keeps the moment in which the mission is shown as paused but the task list still has its running task.
  await openApp(page, { ...DENSE, mutationDelayMs: 4000 }, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  await expect(page.locator('[data-l0="mission.header"] .pill').first()).toContainText("Paused");
  await showTab(page, "Tasks");
  await expect(page.locator('[data-task="2"]'), "the running task is not shown as plain Running while the mission is paused").toContainText("mission paused");
});

// ---- F37: disabled controls keep 4.5:1 in both themes (measured, not judged by eye) --------------------------------------
uxr({ t: "T47", uxr: ["UXR-040"], needs: [...NEEDS, "WP-2"] }, "F37: disabled Send, Take control and keys have a text contrast of at least 4.5:1 in light and dark", async ({ page }, info) => {
  await openApp(page, { ...LIVE, control: "deny" }, MISSION_URL);
  await expect(page.locator("[data-composer]")).toContainText("This worker allows viewing only");
  await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; animation: none !important; }" }); // measure at rest, not mid-transition
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.evaluate((s) => document.documentElement.setAttribute("data-theme", s), scheme);
    await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
    const ratios = await page.evaluate(() => {
      const lum = (c: number[]) => { const f = c.map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; }); return 0.2126 * f[0]! + 0.7152 * f[1]! + 0.0722 * f[2]!; };
      const parse = (v: string): number[] | null => { const m = v.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1]!.split(/[ ,/]+/).map(Number); return [p[0]!, p[1]!, p[2]!, p[3] ?? 1]; };
      const backdrop = (el: Element): number[] => {
        let rgb = [255, 255, 255];
        const chain: number[][] = [];
        for (let e: Element | null = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c[3]! > 0) { chain.push(c); if (c[3] === 1) break; } }
        for (const c of chain.reverse()) rgb = rgb.map((v, i) => c[i]! * c[3]! + v * (1 - c[3]!));
        return rgb;
      };
      const out: Record<string, number> = {};
      const targets: Array<[string, string]> = [["send", "#composer-send"], ["take", "#terminal-control"], ["key", ".keybar .key"]];
      for (const [name, selector] of targets) {
        const el = document.querySelector<HTMLElement>(selector);
        if (!el || el.offsetWidth === 0 || !(el as HTMLButtonElement).disabled) continue;
        const fg = parse(getComputedStyle(el).color)!;
        const bg = backdrop(el);
        const a = lum(fg), b = lum(bg);
        out[name] = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      }
      return out;
    });
    info.annotations.push({ type: `contrast ${scheme}`, description: JSON.stringify(ratios) });
    console.log(`CONTRAST ${scheme} ${JSON.stringify(ratios)}`);
    expect(Object.keys(ratios).length, `${scheme}: disabled controls were measured`).toBeGreaterThan(0);
    for (const [name, ratio] of Object.entries(ratios)) expect(ratio, `${scheme}: ${name} contrast ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
  }
});
