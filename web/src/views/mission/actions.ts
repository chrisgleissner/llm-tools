// Task actions shared by the task rows, the evidence block and the header: Approve and Reject (with evidence and a reason),
// Retry, Pause, Resume and Cancel. Each sends one request; the page toasts the outcome once and reloads.
import { announce, confirmDialog as confirm } from "../../ui";
import { evidenceBody } from "./evidence";
import type { PageCtx, Task } from "./types";

export interface TaskActions {
  approve(t: Task): Promise<void>;
  reject(t: Task): Promise<void>;
  retry(t: Task): Promise<void>;
  resume(t: Task): Promise<void>;
  pause(t: Task): Promise<void>;
  cancel(t: Task): Promise<void>;
}

export function taskActions(ctx: PageCtx): TaskActions {
  const refuse = (): boolean => {
    const why = ctx.blocked();
    if (why) announce(why, "failed");
    return Boolean(why);
  };
  const quote = (t: Task) => `"${t.title || `Task ${t.ordinal}`}"`;
  return {
    async approve(t) {
      if (refuse()) return;
      const evidence = evidenceBody(ctx.workerName, ctx.missionId, t, false);
      const reason = await confirm({
        title: `Approve task ${t.ordinal}?`,
        message: `Approve ${quote(t)} on ${ctx.workerName}.`,
        confirmLabel: "Approve",
        details: evidence.el,
      });
      if (reason === null) return;
      await ctx.mutateTask(`Approve task ${t.ordinal}`, "task.verify", t, { approved: true, evidence: "approved in web UI" });
    },
    async reject(t) {
      if (refuse()) return;
      const reason = await confirm({
        title: `Reject task ${t.ordinal}?`,
        message: `Send ${quote(t)} back on ${ctx.workerName}. The provider is told why.`,
        confirmLabel: "Reject",
        danger: true,
        reasonLabel: "Reason",
      });
      if (reason === null) return;
      await ctx.mutateTask(`Reject task ${t.ordinal}`, "task.reject", t, { reason });
    },
    retry: (t) => (refuse() ? Promise.resolve() : ctx.mutateTask(`Retry task ${t.ordinal}`, "task.retry", t)),
    resume: (t) => (refuse() ? Promise.resolve() : ctx.mutateTask(`Resume task ${t.ordinal}`, "task.resume", t)),
    pause: (t) => (refuse() ? Promise.resolve() : ctx.mutateTask(`Pause task ${t.ordinal}`, "task.pause", t)),
    async cancel(t) {
      if (refuse()) return;
      const reason = await confirm({ title: `Cancel task ${t.ordinal}?`, message: `Cancel ${quote(t)} on ${ctx.workerName}. This cannot be undone from the browser.`, confirmLabel: "Cancel task", danger: true });
      if (reason === null) return;
      await ctx.mutateTask(`Cancel task ${t.ordinal}`, "task.cancel", t);
    },
  };
}
