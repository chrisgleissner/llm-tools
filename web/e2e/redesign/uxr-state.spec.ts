// T05, T06, T07, T40 (UXR-002, UXR-007, UXR-034, UXR-030): one state model, one pill per entity.
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { DENSE_CALM_URL } from "../mock";
import { expect, uxr, uxrStatic } from "./gate";
import { activeElement, openApp, pillsIn } from "./harness";
import { needsFor, PAGES } from "./pages";

// ---- T05: deriveState (web/src/state.ts, WP-0) ----------------------------------------------------------------------
// The spec fixes the rows of section 5.1 but not the argument shape of deriveState. This adapter is the one place that
// knows the shape; WP-0 edits `toInput` (and nothing else here) if its signature differs.
interface Row {
  title: string;
  /** "mission", "task", "worker" or "provider". */
  entity: "mission" | "task" | "worker" | "provider";
  status?: string;
  needs?: { kind: string; tasks?: number[] };
  tasks?: Array<{ ordinal: number; status: string }>;
  worker?: { state: "online" | "offline"; lastSeenSecondsAgo?: number };
  provider?: { ready: boolean; available: boolean; reason?: string };
  conn?: "live" | "reconnecting" | "offline";
  replicationStale?: boolean;
  expect: { state: string; label: string; tier?: "alert" | "quiet" };
}
// WP-0 shape (web/src/state.ts): deriveState({entity, status, needsYou, taskCounts, taskOrdinal, worker, conn, now, reason}) returns
// {id, label, tier, icon, rank, detail}. The adapter converts the row to that input and the result to {state, label, tier}.
const toInput = (row: Row): Record<string, unknown> => {
  const taskCounts: Record<string, number> = {};
  for (const t of row.tasks ?? []) taskCounts[t.status] = (taskCounts[t.status] ?? 0) + 1;
  const own = row.entity === "task" ? (row.tasks ?? []).find((t) => t.status === row.status) : undefined;
  const now = Date.now();
  return {
    entity: row.entity,
    status: row.entity === "provider" ? (row.provider?.ready ? "ready" : "unavailable") : row.entity === "worker" ? row.worker?.state : row.status,
    needsYou: row.needs ? { kind: row.needs.kind, tasks: row.needs.tasks ?? [] } : null,
    taskCounts: row.entity === "mission" ? taskCounts : null,
    taskOrdinal: own?.ordinal ?? null,
    worker: row.worker ? { state: row.worker.state, lastSeen: row.worker.lastSeenSecondsAgo === undefined ? null : now / 1000 - row.worker.lastSeenSecondsAgo } : null,
    reason: row.provider?.reason ?? null,
    conn: row.conn ?? "live",
    now,
    stale: row.replicationStale ?? false,
  };
};

const ROWS: Row[] = [
  { title: "approval", entity: "mission", status: "RUNNING", needs: { kind: "approval", tasks: [2] }, expect: { state: "needs-approval", label: "Needs approval", tier: "alert" } },
  { title: "proposal", entity: "mission", status: "RUNNING", needs: { kind: "proposal" }, expect: { state: "needs-approval", label: "Needs approval", tier: "alert" } },
  { title: "a task VERIFYING that the operator must approve", entity: "task", status: "VERIFYING", needs: { kind: "approval", tasks: [2] }, tasks: [{ ordinal: 2, status: "VERIFYING" }], expect: { state: "needs-approval", label: "Needs approval" } },
  { title: "provider question", entity: "mission", status: "RUNNING", needs: { kind: "provider" }, expect: { state: "needs-input", label: "Needs input", tier: "alert" } },
  { title: "operator and blocked", entity: "mission", status: "RUNNING", needs: { kind: "blocked" }, expect: { state: "needs-input", label: "Needs input" } },
  { title: "a task BLOCKED", entity: "task", status: "BLOCKED", expect: { state: "needs-input", label: "Needs input" } },
  { title: "mission FAILED", entity: "mission", status: "FAILED", expect: { state: "failed", label: "Failed", tier: "alert" } },
  { title: "needs_you failed", entity: "mission", status: "RUNNING", needs: { kind: "failed", tasks: [3] }, expect: { state: "failed", label: "Failed" } },
  { title: "a task FAILED", entity: "task", status: "FAILED", expect: { state: "failed", label: "Failed" } },
  { title: "worker offline while the stream is live", entity: "mission", status: "RUNNING", worker: { state: "offline", lastSeenSecondsAgo: 180 }, expect: { state: "offline", label: "Offline", tier: "quiet" } },
  { title: "capacity wait", entity: "mission", status: "RUNNING", needs: { kind: "capacity" }, expect: { state: "waiting", label: "Waiting for capacity", tier: "quiet" } },
  { title: "running", entity: "mission", status: "RUNNING", expect: { state: "running", label: "Running", tier: "quiet" } },
  { title: "a task RUNNING", entity: "task", status: "RUNNING", expect: { state: "running", label: "Running" } },
  { title: "a task VERIFYING without an approval request", entity: "task", status: "VERIFYING", expect: { state: "running", label: "Verifying" } },
  { title: "paused", entity: "mission", status: "PAUSED", expect: { state: "paused", label: "Paused" } },
  ...["CREATED", "QUEUED", "READY"].map((status): Row => ({ title: `queued (${status})`, entity: "mission", status, expect: { state: "queued", label: "Queued" } })),
  { title: "completed", entity: "mission", status: "COMPLETED", expect: { state: "done", label: "Done" } },
  { title: "stopped", entity: "mission", status: "STOPPED", expect: { state: "stopped", label: "Stopped" } },
  { title: "cancelled", entity: "mission", status: "CANCELLED", expect: { state: "stopped", label: "Cancelled" } },
  { title: "worker online", entity: "worker", worker: { state: "online", lastSeenSecondsAgo: 5 }, expect: { state: "online", label: "Online" } },
  { title: "provider ready", entity: "provider", provider: { ready: true, available: true }, expect: { state: "ready", label: "Ready" } },
  { title: "provider unavailable", entity: "provider", provider: { ready: false, available: false, reason: "not-authenticated" }, expect: { state: "unavailable", label: "Unavailable" } },
  // Rank ties: a mission shows the highest-ranked state among its own status and its tasks.
  { title: "rank: a failed task outranks a running mission", entity: "mission", status: "RUNNING", tasks: [{ ordinal: 1, status: "RUNNING" }, { ordinal: 2, status: "FAILED" }], expect: { state: "failed", label: "Failed" } },
  { title: "rank: approval (1) outranks failed (2)", entity: "mission", status: "RUNNING", needs: { kind: "approval", tasks: [2] }, tasks: [{ ordinal: 2, status: "VERIFYING" }, { ordinal: 3, status: "FAILED" }], expect: { state: "needs-approval", label: "Needs approval" } },
  { title: "rank: needs input and needs approval tie at rank 1, approval first", entity: "mission", status: "RUNNING", needs: { kind: "approval", tasks: [2] }, tasks: [{ ordinal: 4, status: "BLOCKED" }, { ordinal: 2, status: "VERIFYING" }], expect: { state: "needs-approval", label: "Needs approval" } },
  { title: "rank: offline (3) outranks running (5)", entity: "mission", status: "RUNNING", worker: { state: "offline", lastSeenSecondsAgo: 60 }, expect: { state: "offline", label: "Offline" } },
  { title: "rank: failed (2) outranks offline (3)", entity: "mission", status: "FAILED", worker: { state: "offline", lastSeenSecondsAgo: 60 }, expect: { state: "failed", label: "Failed" } },
  // Replication lag and a dropped stream never change the derived state.
  { title: "the coordinator's replication flag stale is ignored", entity: "mission", status: "RUNNING", replicationStale: true, expect: { state: "running", label: "Running" } },
  { title: "stream down keeps the last-known state (no Offline is derived)", entity: "mission", status: "RUNNING", conn: "reconnecting", worker: { state: "offline", lastSeenSecondsAgo: 600 }, expect: { state: "running", label: "Running" } },
  { title: "stream offline keeps the last-known alert", entity: "mission", status: "RUNNING", needs: { kind: "approval", tasks: [2] }, conn: "offline", expect: { state: "needs-approval", label: "Needs approval" } },
];

uxrStatic({ t: "T05", uxr: ["UXR-002"], needs: ["WP-0"] }, "deriveState covers every row of section 5.1, rank ties, ignored stale flag and stream down", async () => {
  const mod = (await import(pathToFileURL(resolve(import.meta.dirname, "../../src/state.ts")).href)) as {
    deriveState: (input: Record<string, unknown>) => { id: string; label?: string; tier?: string };
    labelFor?: (state: string) => string;
  };
  const failures: string[] = [];
  for (const row of ROWS) {
    const result = mod.deriveState(toInput(row));
    const got = { state: result.id, label: result.label, tier: result.tier };
    const label = got.label ?? "";
    if (got.state !== row.expect.state) failures.push(`${row.title}: state ${got.state}, expected ${row.expect.state}`);
    else if (label !== row.expect.label) failures.push(`${row.title}: label "${label}", expected "${row.expect.label}"`);
    else if (row.expect.tier && got.tier && got.tier !== row.expect.tier) failures.push(`${row.title}: tier ${got.tier}, expected ${row.expect.tier}`);
  }
  expect(failures).toEqual([]);
});

// ---- T06 and T07: DOM ---------------------------------------------------------------------------------------------
for (const def of PAGES) {
  uxr({ t: "T06", uxr: ["UXR-002"], needs: needsFor(def) }, `${def.id}: every entity has exactly one .pill, no .badge, no "stale"`, async ({ page }) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const report = await page.evaluate(() => {
      const entities = [...document.querySelectorAll<HTMLElement>("[data-entity]")];
      return {
        entities: entities.length,
        wrongCount: entities.filter((e) => e.querySelectorAll(".pill").length !== 1).map((e) => `${e.dataset["entity"]}: ${e.querySelectorAll(".pill").length} pills`),
        nested: document.querySelectorAll("[data-entity] [data-entity]").length,
        pillOutsideEntity: [...document.querySelectorAll(".pill")].filter((p) => !p.closest("[data-entity]")).map((p) => p.textContent?.trim() ?? ""),
        badges: document.querySelectorAll(".badge").length,
        stale: (document.body.innerText.match(/\bstale\b/gi) ?? []).length,
      };
    });
    expect(report.entities, "the page marks its entities with data-entity").toBeGreaterThan(0);
    expect(report.wrongCount, "entities without exactly one pill").toEqual([]);
    expect(report.nested, "nested entities").toBe(0);
    expect(report.pillOutsideEntity, "pills that belong to no entity").toEqual([]);
    expect(report.badges, ".badge elements (the second badge)").toBe(0);
    expect(report.stale, 'the word "stale" is shown').toBe(0);
  });

  uxr({ t: "T07", uxr: ["UXR-002", "UXR-030"], needs: needsFor(def) }, `${def.id}: a pill is never colour only (icon and text; border in forced colours)`, async ({ page }) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const bad = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".pill")].filter((p) => !p.querySelector("svg[data-icon]") || !(p.textContent ?? "").trim()).map((p) => p.outerHTML.slice(0, 120)));
    expect(bad, "pills without an icon or a label").toEqual([]);
    await page.emulateMedia({ forcedColors: "active" });
    const borders = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".pill")].filter((p) => parseFloat(getComputedStyle(p).borderTopWidth) < 1).map((p) => p.textContent?.trim() ?? ""));
    expect(borders, "pills without a border in forced-colors mode").toEqual([]);
  });
}

// ---- T40: refresh keeps focus and layout; skeleton timing; optimistic update with rollback --------------------------
uxr({ t: "T40", uxr: ["UXR-034"], needs: ["WP-0", "WP-1", "WP-4"] }, "a stream refresh causes no layout shift and keeps focus", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  const row = page.locator('[data-entity^="mission:"] a').first();
  await row.waitFor();
  await row.focus();
  const before = await row.evaluate((el) => el.closest("[data-entity]")?.getAttribute("data-entity") ?? "");
  await page.evaluate(() => {
    const w = window as unknown as { __cls: number };
    w.__cls = 0;
    new PerformanceObserver((list) => { for (const e of list.getEntries() as unknown as Array<{ value: number; hadRecentInput: boolean }>) if (!e.hadRecentInput) w.__cls += e.value; }).observe({ type: "layout-shift", buffered: false });
  });
  for (let i = 0; i < 3; i += 1) {
    app.pushEvent({ kind: "task-status", data: { from: "RUNNING", to: "VERIFYING" } });
    await page.waitForTimeout(700);
  }
  const focused = await page.evaluate(() => document.activeElement?.closest("[data-entity]")?.getAttribute("data-entity") ?? "");
  expect(focused, `focus stays on the same row (now on ${await activeElement(page)})`).toBe(before);
  expect(await page.evaluate(() => (window as unknown as { __cls: number }).__cls), "layout shift caused by the refresh").toBe(0);
});

uxr({ t: "T40", uxr: ["UXR-034"], needs: ["WP-0", "WP-1", "WP-4"] }, "a skeleton appears only after 150 ms of loading", async ({ page }) => {
  for (const [delay, expectSkeleton] of [[60, false], [900, true]] as const) {
    const fresh = await page.context().newPage();
    await fresh.addInitScript(() => {
      const w = window as unknown as { __skeletonAt: number | null; __t0: number };
      w.__skeletonAt = null;
      w.__t0 = performance.now();
      new MutationObserver(() => { if (w.__skeletonAt === null && document.querySelector(".skeleton")) w.__skeletonAt = performance.now() - w.__t0; }).observe(document, { childList: true, subtree: true });
    });
    await openApp(fresh, { auth: "in", dataset: "dense", getDelayMs: delay }, "/missions");
    await fresh.locator('[data-entity^="mission:"]').first().waitFor();
    const at = await fresh.evaluate(() => (window as unknown as { __skeletonAt: number | null }).__skeletonAt);
    if (expectSkeleton) expect(at, "skeleton shown while the list loads").not.toBeNull();
    else expect(at, "no skeleton for a fast answer").toBeNull();
    await fresh.close();
  }
});

uxr({ t: "T40", uxr: ["UXR-034", "UXR-007"], needs: ["WP-0", "WP-3"] }, "Pause updates optimistically and rolls back when the worker refuses", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense", failMutations: true, mutationDelayMs: 1200 }, DENSE_CALM_URL);
  const pill = pillsIn(page.locator('[data-entity^="mission:"]')).first();
  await expect(pill).toContainText("Running");
  await page.locator('[data-action="pause"]').click();
  await expect(pill).toContainText("Paused", { timeout: 500 });
  await expect(pill).toContainText("Running", { timeout: 5000 });
  await expect(page.locator(".toast")).toContainText(/refused|could not|failed/i);
});
