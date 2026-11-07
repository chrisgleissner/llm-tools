// The click-path inventory (UXR-043): every operator task, where it starts, the shortest path, and how many interactions it takes
// at 1440x900 and at 320x568. This file holds only data and the renderer, with no imports, so that both the Playwright specs
// (e2e/redesign/uxr-paths.spec.ts, UXR-044) and `node scripts/click-paths-doc.mjs` (which writes the table in docs/distributed/ux-audit.md)
// read the same rows. The numbers are what the tests measured: the test fails when a path takes more interactions than the row says
// (a regression) and also when it takes fewer (the row must then be lowered, so the inventory stays true).
//
// An interaction is what `interactions.ts` counts: a trusted pointer press, a run of typing (one interaction however many
// characters), and every other key press that is not a bare modifier (Enter, Tab, Escape, arrows, a Ctrl/Cmd combination).

export interface PathRow {
  id: string;
  task: string;
  /** Where the path starts: the page and the state the operator is in. */
  start: string;
  /** The shortest path, as the controls the operator uses. "(phone: ...)" notes where a phone differs. */
  path: string;
  /** Interactions at 1440x900. null: the web console does not offer the task. */
  desktop: number | null;
  /** Interactions at 320x568 (and every other width below 48rem). */
  phone: number | null;
  /** The same task with the keyboard alone on a desktop: the shortcut or palette path the keyboard test performs. */
  keys: string;
}

export const PATH_ROWS: PathRow[] = [
  { id: "sign-in", task: "Sign in", start: "Sign-in page", path: "type the username (the field has focus), Tab, type the password, Enter", desktop: 4, phone: 4, keys: "type, Tab, type, Enter" },
  { id: "sign-out", task: "Sign out", start: "Any page", path: "account menu, Sign out", desktop: 2, phone: 2, keys: "Ctrl+K, type \"sign out\", Enter" },
  { id: "needs-me", task: "See what needs me", start: "Inbox (the home page)", path: "the Needs you list is the first thing on the page", desktop: 0, phone: 0, keys: "none needed" },
  { id: "open-session", task: "Open a live session from the Inbox", start: "Inbox", path: "mission title in a Running row", desktop: 1, phone: 1, keys: "Ctrl+K, type the mission name, Enter" },
  { id: "open-session-list", task: "Open a session from the Sessions page", start: "Any page", path: "Sessions in the navigation, mission link of the session", desktop: 2, phone: 2, keys: "g s, Tab to the mission link, Enter" },
  { id: "watch", task: "Watch a running session", start: "Deep link to a mission (a notification, a bookmark)", path: "the session connects read-only by itself", desktop: 0, phone: 0, keys: "none needed" },
  { id: "take-control", task: "Take control and type", start: "Mission page", path: "Take control, type, Enter", desktop: 3, phone: 3, keys: "c, Enter (Take control), type, Enter" },
  { id: "steer-quick", task: "Steer: pause after this iteration", start: "Mission page", path: "Steer menu, Pause after this iteration", desktop: 2, phone: 2, keys: "c, Tab to Steer, Enter, ArrowDown to the item, Enter" },
  { id: "steer-note", task: "Steer: replace a task prompt", start: "Mission page", path: "Steer menu, Replace a task prompt, type, Send", desktop: 4, phone: 4, keys: "c, Tab to Steer, Enter, ArrowDown to the item, Enter, type, Enter" },
  { id: "answer-question", task: "Answer a provider question", start: "Inbox, a row that says the provider asks", path: "Answer on the row (the page opens with Take control focused), Take control, type, Enter", desktop: 4, phone: 4, keys: "j (select the row), Tab to Answer, Enter, Enter (Take control), type, Enter" },
  { id: "approve-inbox", task: "Approve a task", start: "Inbox", path: "Approve on the row, Approve in the dialog", desktop: 2, phone: 2, keys: "j, a, Enter (the dialog's Approve has focus)" },
  { id: "approve-mission", task: "Approve a task from the mission page", start: "Mission page, state Needs approval", path: "Approve in the header, Approve in the dialog", desktop: 2, phone: 2, keys: "a, Enter" },
  { id: "reject", task: "Reject a task with a reason", start: "Inbox", path: "Reject on the row, type the reason, Reject in the dialog", desktop: 3, phone: 3, keys: "j, r, type, Tab to Reject, Enter" },
  { id: "pause", task: "Pause a mission", start: "Mission page, running", path: "Pause", desktop: 1, phone: 1, keys: "p" },
  { id: "resume", task: "Resume a mission", start: "Mission page, paused", path: "Resume", desktop: 1, phone: 1, keys: "Tab to Resume, Enter" },
  { id: "stop", task: "Stop a mission", start: "Mission page, running", path: "Stop, Stop in the dialog", desktop: 2, phone: 2, keys: "Tab to Stop, Enter, Enter" },
  { id: "start", task: "Start a mission that is not running", start: "Mission page, created or stopped", path: "Start", desktop: 1, phone: 1, keys: "Tab to Start, Enter" },
  { id: "retry", task: "Retry a failed task", start: "Mission page, failed", path: "Retry task", desktop: 1, phone: 1, keys: "Tab to Retry, Enter" },
  { id: "add-task", task: "Add a task", start: "Mission page", path: "(phone: Tasks tab,) Add task, type, Enter", desktop: 3, phone: 4, keys: "Tab to Add task, Enter, type, Enter" },
  { id: "cancel-task", task: "Cancel a task", start: "Mission page", path: "(phone: Tasks tab,) the task's menu, Cancel task, Cancel task in the dialog", desktop: 3, phone: 4, keys: "Tab to the task menu, Enter, ArrowDown to Cancel task, Enter, Enter" },
  { id: "task-evidence", task: "Read the evidence of a task", start: "Mission page", path: "(phone: Tasks tab,) the task title", desktop: 1, phone: 2, keys: "Tab to the task title, Enter" },
  { id: "activity", task: "Read what happened (Activity)", start: "Mission page", path: "Activity tab", desktop: 1, phone: 1, keys: "Tab to the Activity tab, Enter" },
  { id: "find-mission", task: "Find a mission by name", start: "Any page", path: "search button or Ctrl+K, type the name, Enter", desktop: 3, phone: 3, keys: "Ctrl+K, type, Enter" },
  { id: "capacity", task: "Check provider capacity", start: "Any page", path: "the chips in the header show provider, scope and percent", desktop: 0, phone: 0, keys: "none needed" },
  { id: "usage", task: "Open Usage for every provider", start: "Any page", path: "a capacity chip (phone: Usage in the bottom bar)", desktop: 1, phone: 1, keys: "g u" },
  { id: "worker-status", task: "Check a worker", start: "Any page", path: "Workers in the navigation", desktop: 1, phone: 1, keys: "g w" },
  { id: "add-worker", task: "Add a worker and read its enrolment token", start: "Workers page", path: "Add worker, type the name, Enter", desktop: 3, phone: 3, keys: "Tab to Add worker, Enter, type, Enter" },
  { id: "reset-hint", task: "Find out how to reset a forgotten password", start: "Sign-in page", path: "the hint is on the page; Reset it from the host shows the command", desktop: 1, phone: 1, keys: "Tab to Reset it from the host, Enter" },
  { id: "amend-task", task: "Amend a task prompt", start: "-", path: "not offered in the web console (llm-bridge task amend)", desktop: null, phone: null, keys: "-" },
  { id: "reorder-task", task: "Reorder tasks", start: "-", path: "not offered in the web console (llm-bridge task move)", desktop: null, phone: null, keys: "-" },
  { id: "force-stop", task: "Force stop a mission", start: "-", path: "not offered in the web console (Stop is graceful; llm-bridge stop --force)", desktop: null, phone: null, keys: "-" },
];

export const TABLE_BEGIN = "<!-- click-paths:begin (generated by web/scripts/click-paths-doc.mjs from web/e2e/redesign/click-paths-data.ts; do not edit by hand) -->";
export const TABLE_END = "<!-- click-paths:end -->";

const cell = (s: string): string => s.replace(/\|/g, "\\|");
const count = (n: number | null): string => (n === null ? "n/a" : String(n));

/** The markdown table (with its begin and end markers) for the inventory. */
export function renderTable(rows: PathRow[] = PATH_ROWS): string {
  const lines = [
    TABLE_BEGIN,
    "",
    "| Task | Starts at | Path | 1440x900 | 320x568 | Keyboard alone (desktop) |",
    "|---|---|---|---:|---:|---|",
    ...rows.map((r) => `| ${cell(r.task)} | ${cell(r.start)} | ${cell(r.path || "-")} | ${count(r.desktop)} | ${count(r.phone)} | ${cell(r.keys)} |`),
    "",
    TABLE_END,
  ];
  return lines.join("\n");
}
