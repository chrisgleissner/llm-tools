// How much attention an event deserves. Kept apart from the phrase table (event-humanizer.ts) because the toast policy in ui.ts needs only this part,
// and the phrases are needed only by the mission page.

const PLUMBING = new Set([
  "controller-heartbeat", "capacity-refresh", "controller-heartbeat-error", "capacity-refresh-error",
  "capacity-wake-plan", "operation-process", "repository-checkpoint", "prompt-assembled", "controller-mode",
  "controller-config", "operation-progress", "provider-result-diagnosed",
  "task-status-transition", "events-pruned", "operation-prepared", "suspend-decision", "suspend-provider-started",
  "operation-log-pruned", "managed-process-prepared", "controller-ownership",
  "station-observation", "station-stopped", "native-session-restoration", "task-provider-continuation",
  "steering-handoff-consumed", "steering-pause-applied", "steering-stop-task-applied", "steering-verify-applied",
  "steering-verify-drained", "after-verification-result",
]);

export function isPlumbing(kind: string): boolean {
  return PLUMBING.has(kind) || (kind.startsWith("steering-") && ["applied", "drained", "consumed"].includes(kind.split("-").at(-1) ?? ""));
}

/**
 * How much attention an event deserves. Milestones are what the operator reads by default (task transitions, verification,
 * approvals, mission lifecycle, handoffs, failures, provider waits). Details (commands, operations, terminal and worker
 * connection, provider selection) appear under "All activity". Plumbing (heartbeats, capacity refreshes, bookkeeping) is
 * counted, hidden by default, never toasted and never counted in totals.
 */
export type EventClass = "milestone" | "detail" | "plumbing";

const MILESTONES = new Set([
  "mission-created", "mission-status", "mission-reopened", "mission-waiting-for-operator",
  "task-added", "task-status", "task-verified", "task-rejected", "task-awaiting-answer", "task-proposed",
  "task-proposal-accepted", "task-proposal-rejected", "task-evidence-recorded",
  "provider-wait", "provider-usage-limit", "provider-switched", "provider-handoff", "provider-handoff-exhausted", "provider-handoff-selected",
  "operation-uncertain", "operation-aborted-before-launch", "controller-exited-unexpectedly", "controller-failed",
  "review-hold", "review-hold-released", "suspend-outcome",
]);

export function eventClass(kind: string): EventClass {
  if (isPlumbing(kind)) return "plumbing";
  if (MILESTONES.has(kind) || kind.startsWith("verification-")) return "milestone";
  return "detail";
}

