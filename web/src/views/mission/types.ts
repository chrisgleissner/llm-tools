// Shared types of the mission detail page (PRD-037 WP-3).
import type { NeedsYou } from "../../api";
import { absTime } from "../../dom";
import type { ToastAction } from "../../ui";

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

export type TaskVerb = "approve" | "reject" | "retry" | "resume" | "pause" | "cancel" | "priority" | "depend" | "amend";

/** What the task panel, the evidence block and the header need from the page, so the modules do not import each other. */
export interface PageCtx {
  workerName: string;
  missionId: string;
  /** "" when mutations are possible, otherwise the plain-words reason they are disabled. */
  blocked(): string;
  /** `needs_you` of the status projection (null: nothing is needed). */
  needs(): NeedsYou | null;
  /** The mission's status (RUNNING, PAUSED, ...), optimistic while a change is in flight. */
  status(): string;
  /** Take control of the session to answer the provider (the Answer button of a task that needs input). */
  answer(): void;
  /** The task the header's primary action is about (Approve or Retry), or undefined: its row has no inline copy of that action. */
  subject(): number | undefined;
  /** The verifier result of a task whose evidence was read ("" when none is recorded): the header quotes it. */
  verified(ordinal: number, text: string): void;
  /** Send a task change, toast the outcome once ("Approved task 2" or "Could not approve task 2. ..."), then reload. */
  mutateTask(verb: TaskVerb, operation: string, task: Task, params?: Record<string, unknown>, undo?: ToastAction): Promise<void>;
}

/** The text of a plain-words age: "14:02 (3m ago)". */
export function asOfText(ms: number): string {
  if (!ms) return "never";
  const age = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return `${absTime(new Date(ms)).slice(11)} (${age < 1 ? "just now" : `${age}m ago`})`;
}
