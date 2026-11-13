// WP-0 foundations (PRD-037): the pure state derivation, id shortening, event classes and the toast policy.
// These import the production modules directly, the same way screenshots.spec.ts imports current-work.
import { expect, test } from "@playwright/test";
import { currentWorkSummary } from "../src/current-work";
import { allowedEventData, eventClass } from "../src/event-humanizer";
import { compareStates, deriveState, labelFor, shortId, STATE_IDS } from "../src/state";

test("shortId keeps the first 6 and last 4 characters of a long id", () => {
  expect(shortId("0b0f6d3e-1c2a-4f5b-8d7e-123456789abc")).toBe("0b0f6d…9abc");
  expect(shortId("abcdef123456")).toBe("abcdef123456");
  expect(shortId("short")).toBe("short");
});

test("deriveState: alert states outrank everything and the two rank 1 states tie by order", () => {
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "approval", tasks: [2] } })).toMatchObject({ id: "needs-approval", tier: "alert", rank: 1, detail: "Task 2" });
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "proposal" } }).id).toBe("needs-approval");
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "provider", message: "Should I continue?" } })).toMatchObject({ id: "needs-input", detail: "Should I continue?" });
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "operator" } }).id).toBe("needs-input");
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "blocked", tasks: [4] } })).toMatchObject({ id: "needs-input", detail: "Task 4 blocked" });
  expect(deriveState({ status: "RUNNING", taskCounts: { VERIFYING: 1, BLOCKED: 1 } }).id).toBe("needs-approval");
  expect(deriveState({ status: "RUNNING", taskCounts: { BLOCKED: 1 } }).id).toBe("needs-input");
  expect(deriveState({ status: "FAILED" })).toMatchObject({ id: "failed", tier: "alert", rank: 2 });
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "failed", message: "task 3 failed" } }).id).toBe("failed");
  expect(deriveState({ status: "COMPLETED", taskCounts: { FAILED: 1 } }).id).toBe("failed");
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "approval" }, taskCounts: { FAILED: 1 } }).id, "approval outranks failed").toBe("needs-approval");
});

test("deriveState: the quiet states, labels and icons", () => {
  const cases: Array<[string, string, string]> = [
    ["RUNNING", "running", "Running"],
    ["STOPPING", "running", "Stopping"],
    ["PAUSED", "paused", "Paused"],
    ["CREATED", "queued", "Queued"],
    ["READY", "queued", "Queued"],
    ["COMPLETED", "done", "Done"],
    ["STOPPED", "stopped", "Stopped"],
    ["CANCELLED", "stopped", "Cancelled"],
  ];
  for (const [status, id, label] of cases) expect(deriveState({ status }), status).toMatchObject({ id, label, tier: "quiet" });
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "capacity" } }).id).toBe("waiting");
  for (const id of STATE_IDS) expect(labelFor(id).length).toBeGreaterThan(0);
});

test("deriveState: offline needs a live stream, ignores the replication flag and does not hide a finished mission", () => {
  const worker = { state: "offline", lastSeen: 1000 };
  const now = 1000 * 1000 + 180_000;
  expect(deriveState({ status: "RUNNING", worker, conn: "live", now })).toMatchObject({ id: "offline", detail: "last seen 3m, was running" });
  expect(deriveState({ status: "RUNNING", worker, conn: "reconnecting" }).id, "stream down keeps the last-known state").toBe("running");
  expect(deriveState({ status: "RUNNING", worker, conn: "offline" }).id).toBe("running");
  expect(deriveState({ status: "COMPLETED", worker, conn: "live" }).id, "a finished mission stays Done").toBe("done");
  expect(deriveState({ status: "RUNNING", worker: { state: "online" }, conn: "live" }).id).toBe("running");
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "approval" }, worker, conn: "live" }).id, "needs approval outranks offline").toBe("needs-approval");
  expect(deriveState({ status: "RUNNING", needsYou: { kind: "capacity" }, worker, conn: "live" }).id, "offline outranks waiting").toBe("offline");
  // `stale` is not an input: an object that carries it derives the same state
  expect(deriveState({ status: "RUNNING", ...({ stale: true } as object) }).id).toBe("running");
});

test("deriveState: tasks, workers and providers", () => {
  expect(deriveState({ entity: "task", status: "VERIFYING", taskOrdinal: 2, needsYou: { kind: "approval", tasks: [2] } }).id).toBe("needs-approval");
  expect(deriveState({ entity: "task", status: "VERIFYING", taskOrdinal: 3, needsYou: { kind: "approval", tasks: [2] } })).toMatchObject({ id: "running", label: "Verifying" });
  expect(deriveState({ entity: "task", status: "FAILED" }).id).toBe("failed");
  expect(deriveState({ entity: "task", status: "BLOCKED" }).id).toBe("needs-input");
  expect(deriveState({ entity: "worker", status: "online" }).id).toBe("online");
  expect(deriveState({ entity: "worker", status: "offline", worker: { state: "offline", lastSeen: 100 }, now: 100_000 + 7_200_000 }).detail).toBe("last seen 2h");
  expect(deriveState({ entity: "provider", status: "ready" }).id).toBe("ready");
  expect(deriveState({ entity: "provider", status: "no", reason: "not signed in" })).toMatchObject({ id: "unavailable", detail: "not signed in" });
  const sorted = [deriveState({ status: "COMPLETED" }), deriveState({ status: "FAILED" }), deriveState({ status: "RUNNING" }), deriveState({ status: "RUNNING", needsYou: { kind: "approval" } })].sort(compareStates);
  expect(sorted.map((s) => s.id)).toEqual(["needs-approval", "failed", "running", "done"]);
});

test("event classes: plumbing is never a milestone and unknown kinds are details", () => {
  for (const kind of ["controller-heartbeat", "capacity-refresh", "operation-process", "task-status-transition"]) expect(eventClass(kind), kind).toBe("plumbing");
  for (const kind of ["mission-created", "task-verified", "verification-failed", "provider-wait", "provider-handoff", "task-awaiting-answer"]) expect(eventClass(kind), kind).toBe("milestone");
  for (const kind of ["command-applied", "operation-running", "provider-selection", "terminal-opened", "worker-connected", "something-new"]) expect(eventClass(kind), kind).toBe("detail");
});

test("raw event data keeps only the allowlisted scalar fields", () => {
  expect(allowedEventData({ from: "RUNNING", to: "VERIFYING", "do-not-render-this": "secret", nested: { reason: "x" }, exit_code: 1, command: "pause\nnow" })).toEqual({ from: "RUNNING", to: "VERIFYING", exit_code: 1, command: "pause now" });
});

test("current-work summary omits unknown parts instead of printing Provider unknown", () => {
  expect(currentWorkSummary({ current_task: { ordinal: 7, title: "Investigate the timeout" }, station_observation: { station: "unknown", model: "unknown" }, station_context: { last_launched_station: "" } })).toBe("Task 7 · Investigate the timeout");
  expect(currentWorkSummary({ current_task: { ordinal: 3, title: "Run checks" }, station_context: { last_launched_station: "codex" }, station_observation: { station: "unknown", model: "unknown" }, station_config: { models: { codex: "gpt-5" } } })).toBe("Task 3 · Run checks · codex · gpt-5");
  expect(currentWorkSummary({})).toBe("Waiting for the next task");
});
