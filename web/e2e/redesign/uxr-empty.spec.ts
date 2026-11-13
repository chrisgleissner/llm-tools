// T41 (UXR-035): teaching empty states. A page with nothing to show has a heading, one sentence and one action or command.
import { MISSION_START_PATH } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { openApp } from "./harness";
import type { MockOptions } from "../mock";

const EMPTY = "[data-empty], .empty-state, .empty";
const CASES: Array<{ name: string; path: string; mock: MockOptions; needs: Array<"WP-0" | "WP-1" | "WP-3" | "WP-4" | "WP-5"> }> = [
  { name: "inbox without workers", path: "/", mock: { auth: "in", workers: "none" }, needs: ["WP-0", "WP-1", "WP-4"] },
  { name: "inbox without missions", path: "/", mock: { auth: "in", missions: "none" }, needs: ["WP-0", "WP-1", "WP-4"] },
  { name: "missions", path: "/missions", mock: { auth: "in", missions: "none" }, needs: ["WP-0", "WP-1", "WP-4"] },
  { name: "sessions", path: "/sessions", mock: { auth: "in", sessions: "none" }, needs: ["WP-0", "WP-1", "WP-5"] },
  { name: "workers", path: "/workers", mock: { auth: "in", workers: "none" }, needs: ["WP-0", "WP-1", "WP-5"] },
  { name: "usage", path: "/usage", mock: { auth: "in", usage: "none" }, needs: ["WP-0", "WP-1", "WP-5"] },
  { name: "mission with no session running", path: MISSION_START_PATH, mock: { auth: "in", terminalClose: true }, needs: ["WP-0", "WP-1", "WP-3"] },
];

for (const c of CASES) {
  uxr({ t: "T41", uxr: ["UXR-035"], needs: c.needs }, `${c.name}: the empty state has a heading, a sentence and one action or command`, async ({ page }, info) => {
    await openApp(page, c.mock, c.path);
    const empty = page.locator(EMPTY).first();
    await expect(empty).toBeVisible();
    const parts = await empty.evaluate((el) => ({
      heading: (el.querySelector("h2, h3, h4, [role=heading]")?.textContent ?? "").trim(),
      sentence: (el.querySelector("p, .empty-text")?.textContent ?? "").trim(),
      actions: el.querySelectorAll("button:not([disabled]), a[href], code").length,
    }));
    expect(parts.heading.length, "heading").toBeGreaterThan(0);
    expect(parts.sentence.length, "one sentence of explanation").toBeGreaterThan(10);
    expect(parts.actions, "an action or a copyable command").toBeGreaterThan(0);
    await evidence(page, info, ["UXR-035"], `empty-${c.name.replace(/ /g, "-")}`);
  });
}
