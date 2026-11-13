// Task actions shared by the task rows, the evidence block and the header: Approve and Reject (with evidence and a reason),
// Retry, Pause, Resume and Cancel. Each sends one request; the page toasts the outcome once and reloads.
import { confirmDialog as confirm, type ConfirmOptions } from "../../ui";
import { evidenceBody } from "./evidence";
import { refused, taskTitle } from "./kit";
import type { PageCtx, Task } from "./types";

export type TaskActions = Record<"approve" | "reject" | "retry" | "resume" | "pause" | "cancel", (t: Task) => Promise<void>>;

export function taskActions(ctx: PageCtx): TaskActions {
  const quote = (t: Task) => `"${taskTitle(t)}"`;
  /** One change: refused while the page is blocked, asks first when it has a dialog, then sends `task.<operation>`. */
  const run =
    (verb: keyof TaskActions, operation: string, ask?: (t: Task) => ConfirmOptions, params?: (reason: string) => Record<string, unknown>) =>
    async (t: Task): Promise<void> => {
      if (refused(ctx.blocked())) return;
      const reason = ask ? await confirm(ask(t)) : "";
      if (reason !== null) await ctx.mutateTask(verb, `task.${operation}`, t, params?.(reason));
    };
  const on = ` on ${ctx.workerName}`;
  return {
    approve: run(
      "approve",
      "verify",
      (t) => ({ title: `Approve task ${t.ordinal}?`, message: `Approve ${quote(t)}${on}.`, confirmLabel: "Approve", details: evidenceBody(ctx.workerName, ctx.missionId, t, false).el }),
      () => ({ approved: true, evidence: "approved in web UI" }),
    ),
    reject: run("reject", "reject", (t) => ({ title: `Reject task ${t.ordinal}?`, message: `Send ${quote(t)} back${on}. The provider is told why.`, confirmLabel: "Reject", danger: true, reasonLabel: "Reason" }), (reason) => ({ reason })),
    retry: run("retry", "retry"),
    resume: run("resume", "resume"),
    pause: run("pause", "pause"),
    cancel: run("cancel", "cancel", (t) => ({ title: `Cancel task ${t.ordinal}?`, message: `Cancel ${quote(t)}${on}. This cannot be undone from the browser.`, confirmLabel: "Cancel task", danger: true })),
  };
}
