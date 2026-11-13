// Task actions shared by the task rows, the evidence block and the header: Approve and Reject (with evidence and a reason),
// Retry, Pause, Resume and Cancel. Each sends one request; the page toasts the outcome once and reloads.
import * as api from "../../api";
import { h } from "../../dom";
import { announce, confirmDialog as confirm, type ConfirmOptions } from "../../ui";
import { evidenceBody } from "./evidence";
import { record, refused, taskTitle } from "./kit";
import type { PageCtx, Task } from "./types";

export type TaskActions = Record<"approve" | "reject" | "retry" | "resume" | "pause" | "cancel" | "priority" | "depend" | "amend", (t: Task) => Promise<void>>;

const field = (id: string, label: string, control: HTMLElement, hint = ""): HTMLElement => h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("p", { class: "muted small" }, hint) : null);

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
  const whole = (v: string): number | null => (/^-?\d+$/.test(v.trim()) ? Number(v) : null);
  return {
    // Priority, prerequisite and prompt changes of PRD-016. The dialog holds the fields; the values are read when it is confirmed.
    priority: async (t) => {
      if (refused(ctx.blocked())) return;
      const input = h("input", { id: "task-priority", type: "number", step: "1", value: String(t.priority), inputmode: "numeric" });
      const reason = await confirm({ title: `Priority of task ${t.ordinal}`, message: `Tasks with a higher priority run first${on}.`, confirmLabel: "Set priority", details: field("task-priority", "Priority", input) });
      if (reason === null) return;
      const next = whole(input.value);
      if (next === null) return void announce("The priority must be a whole number. Nothing was changed.", "failed");
      if (next === t.priority) return;
      const old = t.priority;
      await ctx.mutateTask("priority", "task.priority", t, { priority: next }, { label: "Undo", run: () => void ctx.mutateTask("priority", "task.priority", t, { priority: old, expected_revision: undefined }) });
    },
    depend: async (t) => {
      if (refused(ctx.blocked())) return;
      const input = h("input", { id: "task-prerequisite", type: "number", step: "1", min: "1", inputmode: "numeric" });
      const reason = await confirm({ title: `Prerequisite for task ${t.ordinal}`, message: `Task ${t.ordinal} waits until the task you name is completed${on}. A prerequisite cannot be removed afterwards.`, confirmLabel: "Add prerequisite", details: field("task-prerequisite", "Task number", input) });
      if (reason === null) return;
      const n = whole(input.value);
      if (n === null || n < 1 || n === t.ordinal) return void announce(`Enter the number of another task. Nothing was changed.`, "failed");
      await ctx.mutateTask("depend", "task.dependency", t, { depends_on: n });
    },
    amend: async (t) => {
      if (refused(ctx.blocked())) return;
      const shown = await api.taskShow(ctx.workerName, ctx.missionId, t.ordinal).then((r) => record(r["task"])["prompt"], () => undefined);
      const prompt = t.prompt ?? (typeof shown === "string" ? shown : "");
      const box = h("textarea", { id: "task-prompt", rows: 6 });
      box.value = prompt;
      const requeue = t.status === "VERIFYING";
      const again = h("input", { id: "task-requeue", type: "checkbox", checked: requeue });
      const body = h("div", { class: "stack" }, field("task-prompt", "Prompt", box), requeue ? h("label", { class: "check" }, again, " Run the task again with the new prompt") : null);
      const reason = await confirm({ title: `Amend task ${t.ordinal}`, message: `The change creates a new revision of the prompt${on}; the earlier text stays in the task history.`, confirmLabel: "Amend task", details: body });
      if (reason === null) return;
      const next = box.value.trim();
      if (!next) return void announce("The prompt cannot be empty. Nothing was changed.", "failed");
      if (next === prompt.trim()) return;
      await ctx.mutateTask("amend", "task.amend", t, { prompt: next, requeue: again.checked });
    },
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
