// Shared types of the mission detail page (PRD-037 WP-3).
import type { NeedsYou } from "../../api";

export interface Task {
  id: number;
  ordinal: number;
  title: string;
  status: string;
  priority: number;
  revision: number;
  prompt?: string;
}

export type Tab = "session" | "tasks" | "activity";

/** What the task panel, the evidence block and the header need from the page, so the modules do not import each other. */
export interface PageCtx {
  workerName: string;
  missionId: string;
  /** "" when mutations are possible, otherwise the plain-words reason they are disabled. */
  blocked(): string;
  /** `needs_you` of the status projection (null: nothing is needed). */
  needs(): NeedsYou | null;
  /** Send a task change, toast the outcome once, then reload. */
  mutateTask(label: string, operation: string, task: Task, params?: Record<string, unknown>): Promise<void>;
}

/** The text of a plain-words age: "14:02 (3m ago)". */
export function asOfText(ms: number, now = Date.now()): string {
  if (!ms) return "never";
  const d = new Date(ms);
  const age = Math.max(0, Math.round((now - ms) / 60000));
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} (${age < 1 ? "just now" : `${age}m ago`})`;
}
