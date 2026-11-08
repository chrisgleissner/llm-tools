// Screens owned by the mission package (PRD-037 WP-3). Names are baseline file names, so they stay unique across packages.
// `SCREENS` feeds screenshots.spec.ts (baselines and axe); `screens` is the subset with a variant, which uxr-baselines.spec.ts
// (T42) checks for the four variants every page-level package must show: empty, sparse, dense and error.
import type { Page } from "@playwright/test";
import { expect } from "../fixtures";
import { DENSE_CALM_URL, MISSION_PATH, MISSION_START_PATH, MISSIONS } from "../mock";
import { IN, type Screen } from "./types";

type MissionScreen = Screen & { variant?: "empty" | "sparse" | "dense" | "error" };

/** Open a view of the mission page when the layout shows views as tabs (below 60rem); on the two-pane layout Tasks and Activity are already in the side column. */
const showTab = async (p: Page, name: "Tasks" | "Activity"): Promise<void> => {
  const tab = p.getByRole("tab", { name: new RegExp(`^${name}\\b`) });
  if (await tab.isVisible()) await tab.click();
};
const header = (p: Page) => expect(p.locator('[data-l0="mission.header"] .pill').first()).toBeVisible();
const BETA_RUNNING = `/missions/beta/${MISSIONS[4]!.mission.id}`;

const SCREEN_LIST: MissionScreen[] = [
  // Dense: every task state, eight activity rows, a task awaiting approval.
  { name: "mission-detail-dense", variant: "dense", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await showTab(p, "Tasks"); await expect(p.locator('[data-entity^="task:"]')).toHaveCount(5); await expect(p.locator('[data-l0="mission.header"] [data-action="approve"]')).toBeVisible(); } },
  // Sparse: a calm running mission with three tasks.
  { name: "mission-detail-sparse", variant: "sparse", mock: { auth: "in", dataset: "dense" }, path: DENSE_CALM_URL, run: async (p) => { await header(p); await showTab(p, "Tasks"); await expect(p.locator('[data-entity^="task:"]')).toHaveCount(2); await expect(p.locator('[data-l0="mission.header"] [data-action="pause"]')).toBeVisible(); } },
  // Empty: a created mission has no session and no activity yet.
  { name: "mission-detail-empty", variant: "empty", mock: IN, path: MISSION_START_PATH, run: async (p) => { await header(p); await expect(p.locator('[data-l0="mission.header"] [data-action="start"]')).toBeVisible(); await expect(p.locator(".mission-session .empty")).toBeVisible(); } },
  // Error: the worker refuses Pause; the pill rolls back and an error toast stays.
  { name: "mission-error", variant: "error", mock: { auth: "in", dataset: "dense", failMutations: true }, path: DENSE_CALM_URL, run: async (p) => { await header(p); await p.locator('[data-action="pause"]').click(); await expect(p.locator(".toast.rejected")).toBeVisible(); await expect(p.locator('[data-l0="mission.header"] .pill').first()).toContainText("Running"); } },
  { name: "mission-activity", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await showTab(p, "Activity"); await expect(p.locator("[data-event-class=milestone]").first()).toBeVisible(); await expect(p.locator("[data-plumbing-count]")).toBeVisible(); await expect(p.locator("#events-list")).not.toContainText("do-not-render-this"); await expect(p.locator("#events-list")).toContainText("RUNNING → VERIFYING"); } },
  { name: "mission-task-evidence", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await showTab(p, "Tasks"); await expect(p.locator(".evidence-diff")).toContainText("2 files"); /* open without choosing the task */ await expect(p.locator(".evidence-verifier")).toBeVisible(); } },
  { name: "mission-add-task", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await showTab(p, "Tasks"); await p.getByRole("button", { name: "Add task" }).click(); await expect(p.locator("#new-task")).toBeFocused(); } },
  { name: "mission-offline", mock: IN, path: BETA_RUNNING, run: async (p) => { await header(p); await expect(p.locator('[data-l0="mission.header"] .pill').first()).toContainText("Offline"); await expect(p.locator('[data-l0="mission.header"] [data-action="pause"]')).toBeDisabled(); } },
  { name: "mission-confirm-reject", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await showTab(p, "Tasks"); /* Reject is in the header (D28, R2); below 30rem it is in the header menu */ const reject = p.locator('[data-l0="mission.header"] [data-action="reject"]'); if (await reject.isVisible()) await reject.click(); else { await p.locator('[data-l0="mission.header"] [aria-haspopup="menu"]').click(); await p.getByRole("menuitem", { name: /^Reject task/ }).click(); } await expect(p.getByRole("dialog")).toBeVisible(); } },
  { name: "mission-confirm-stop", mock: IN, path: MISSION_PATH, run: async (p) => { await header(p); await p.locator('[data-l0="mission.header"] [data-action="stop"]').click(); await expect(p.getByRole("dialog")).toBeVisible(); } },
  { name: "mission-toast-applied", mock: { auth: "in", dataset: "dense" }, path: DENSE_CALM_URL, run: async (p) => { await header(p); await p.locator('[data-action="pause"]').click(); await expect(p.locator(".toast.applied")).toBeVisible(); } },
];

export const SCREENS: Screen[] = SCREEN_LIST;
export const screens = SCREEN_LIST.filter((s): s is MissionScreen & { variant: NonNullable<MissionScreen["variant"]> } => s.variant !== undefined);
