// The Tasks panel (spec 8.1): TaskRow with at most two visible buttons and an overflow menu, completed tasks grouped under a
// disclosure, the inline "Add task" field, and the Evidence block of the selected task.
import * as api from "../../api";
import { button, copyText, disclosure, emptyState, h, hooks, keepFocus, setBusy, statePill } from "../../dom";
import { menu, type MenuItem } from "../../menu";
import { deriveState } from "../../state";
import { type TaskActions } from "./actions";
import { evidenceBody } from "./evidence";
import type { PageCtx, Task } from "./types";

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const OPEN_PAGE = 50;

export interface TaskPanelData {
  tasks: Task[];
  total: number;
  loaded: boolean;
  error: string;
}

export interface TasksPanel {
  el: HTMLElement;
  update(data: TaskPanelData): void;
  /** Focus the first row action that needs the operator (Approve, Retry, Resume), else the first row link. */
  focusFirstAction(): void;
}

export function tasksPanel(ctx: PageCtx, actions: TaskActions, onAdd: (prompt: string) => Promise<boolean>): TasksPanel {
  let data: TaskPanelData = { tasks: [], total: 0, loaded: false, error: "" };
  let selected: number | null = null;
  let evidenceKey = "";
  let openShown = OPEN_PAGE;
  let adding = false;
  let touched = false;
  let completedOpen = false;
  let signature = "";

  const headBox = h("div", { class: "panel-head" });
  const notices = h("div", { class: "panel-notices" });
  const listBox = h("div", { class: "task-list-box" });
  const evidenceBox = h("section", { class: "evidence", "aria-label": "Evidence", hidden: true });

  // ---- add task: one inline field, Enter sends, Shift+Enter adds a line
  const prompt = h("textarea", { id: "new-task", rows: 3, "aria-label": "New task prompt", placeholder: "Describe the task for the provider", "aria-describedby": "new-task-error" });
  const promptError = h("p", { class: "field-error", id: "new-task-error", role: "alert" });
  const submit = button("Add", () => void send(), { kind: "primary small", id: "new-task-add" });
  const form = h(
    "form",
    { class: "add-task", hidden: true, novalidate: true, "aria-label": "Add task" },
    prompt,
    promptError,
    h("div", { class: "actions" }, submit, button("Cancel", () => closeForm(true), { kind: "small" })),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    void send();
  });
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      void send();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeForm(true);
    }
  });
  prompt.addEventListener("input", () => {
    if (prompt.value.trim()) {
      promptError.textContent = "";
      prompt.removeAttribute("aria-invalid");
    }
  });

  function openForm(): void {
    adding = true;
    form.hidden = false;
    prompt.focus();
  }
  function closeForm(restoreFocus: boolean): void {
    adding = false;
    form.hidden = true;
    prompt.value = "";
    promptError.textContent = "";
    prompt.removeAttribute("aria-invalid");
    if (restoreFocus) document.getElementById("add-task")?.focus();
  }
  async function send(): Promise<void> {
    const text = prompt.value.trim();
    if (!text) {
      promptError.textContent = "Enter the task prompt.";
      prompt.setAttribute("aria-invalid", "true");
      prompt.focus();
      promptError.scrollIntoView?.({ block: "nearest" });
      return;
    }
    if (submit.getAttribute("aria-busy") === "true") return;
    setBusy(submit, true);
    const ok = await onAdd(text);
    setBusy(submit, false);
    if (ok) closeForm(true);
  }

  // ---- rows
  function rowState(t: Task) {
    return deriveState({ entity: "task", status: t.status, taskOrdinal: t.ordinal, needsYou: ctx.needs() });
  }

  function menuItems(t: Task): MenuItem[] {
    const why = ctx.blocked();
    const items: MenuItem[] = [];
    if (["QUEUED", "READY", "RUNNING"].includes(t.status)) items.push({ label: "Pause task", onSelect: () => void actions.pause(t), disabled: Boolean(why), reason: why });
    if (!TERMINAL.has(t.status)) items.push({ label: "Cancel task", danger: true, onSelect: () => void actions.cancel(t), disabled: Boolean(why), reason: why });
    items.push({ label: "Copy title", onSelect: () => copy(t.title || `Task ${t.ordinal}`, "title") });
    items.push({
      label: "Copy prompt",
      onSelect: () =>
        void (async () => {
          let text = t.prompt ?? "";
          if (!text) {
            try {
              const show = await api.taskShow(ctx.workerName, ctx.missionId, t.ordinal);
              const task = show["task"] as Record<string, unknown> | undefined;
              text = typeof task?.["prompt"] === "string" ? task["prompt"] : "";
            } catch {
              text = "";
            }
          }
          if (text) copy(text, "prompt");
          else hooks.say("The prompt is not available from the worker.");
        })(),
    });
    return items;
  }

  function copy(text: string, what: string): void {
    void copyText(text).then((ok) => hooks.say(ok ? `Copied ${what}.` : `Could not copy the ${what}; select it and copy manually.`));
  }

  function inlineButtons(t: Task, state: ReturnType<typeof rowState>): HTMLElement[] {
    if (t.ordinal === ctx.subject()) return []; // the header offers this action (spec 6.1: one place per viewport)
    const why = ctx.blocked();
    // The header holds the one primary button of the page (the same Approve or Retry); the buttons of a row are secondary.
    const b = (label: string, fn: () => void, kind: string, action: string): HTMLElement =>
      button(label, fn, { kind: `${kind} small`, reason: why, label: `${label} task ${t.ordinal}`, action });
    if (state.id === "needs-approval") return [b("Approve", () => void actions.approve(t), "", "approve"), b("Reject", () => void actions.reject(t), "danger", "reject")];
    if (state.id === "needs-input") return [b("Answer", () => ctx.answer(), "", "answer")];
    if (t.status === "FAILED") return [b("Retry", () => void actions.retry(t), "", "retry")];
    if (t.status === "PAUSED") return [b("Resume", () => void actions.resume(t), "", "resume")];
    return [];
  }

  function row(t: Task): HTMLElement {
    const state = rowState(t);
    const title = t.title || `Task ${t.ordinal}`;
    const link = h("a", { class: "task-title clamp-2", href: `#task-${t.ordinal}`, title, "aria-current": selected === t.ordinal ? "true" : null, "data-task-link": t.ordinal }, title);
    link.addEventListener("click", (e) => {
      e.preventDefault();
      touched = true;
      selected = selected === t.ordinal ? null : t.ordinal;
      renderList();
      renderEvidence();
    });
    return h(
      "li",
      { class: `task-row${selected === t.ordinal ? " selected" : ""}`, "data-entity": `task:${ctx.missionId}/${t.ordinal}`, "data-task": t.ordinal },
      h("span", { class: "task-ord num" }, String(t.ordinal)),
      h(
        "div",
        { class: "task-main" },
        link,
        h("div", { class: "task-meta" }, statePill(state, { dense: true }), t.priority > 0 ? h("span", { class: "pill-meta" }, `Priority ${t.priority}`) : null, t.status === "RUNNING" && ["PAUSED", "STOPPING"].includes(ctx.status()) ? h("span", { class: "pill-meta" }, "mission paused") : null),
      ),
      h("div", { class: "task-actions" }, ...inlineButtons(t, state), menu({ label: `More actions for task ${t.ordinal}`, kind: "ghost small" }, menuItems(t))),
    );
  }

  function renderHead(): void {
    const why = ctx.blocked();
    keepFocus(headBox, () =>
      headBox.replaceChildren(
        h("h2", { class: "panel-title" }, "Tasks"),
        button("Add task", () => (adding ? closeForm(true) : openForm()), { icon: "plus", kind: "small", id: "add-task", reason: why, action: "add-task" }),
      ),
    );
  }

  function renderList(): void {
    keepFocus(listBox, () => {
      const open = data.tasks.filter((t) => t.status !== "COMPLETED");
      const done = data.tasks.filter((t) => t.status === "COMPLETED");
      const nodes: Node[] = [];
      if (data.error) nodes.push(h("p", { class: "notice", role: "status" }, data.error));
      if (!data.loaded && !data.error) nodes.push(h("p", { class: "muted", role: "status" }, "Loading tasks..."));
      else if (data.loaded && data.tasks.length === 0)
        nodes.push(emptyState({ icon: "inbox", title: "No tasks yet", text: "Tasks appear here as soon as the worker reports them. Add the first one with the button above or with the command below.", command: "llm-bridge task add" }));
      // The rows of completed tasks are built only while the disclosure is open, so a closed group holds no hidden task entities.
      if (done.length)
        nodes.push(
          disclosure("Completed", completedOpen ? h("ul", { class: "task-list", "aria-label": "Completed tasks" }, ...done.map(row)) : h("span", {}), {
            count: done.length,
            id: "tasks-completed",
            open: completedOpen,
            onToggle: (o) => {
              if (o === completedOpen) return;
              completedOpen = o;
              renderList();
            },
          }),
        );
      if (open.length) {
        nodes.push(h("ul", { class: "task-list", "aria-label": "Open tasks" }, ...open.slice(0, openShown).map(row)));
        if (open.length > openShown) nodes.push(button(`Show ${Math.min(OPEN_PAGE, open.length - openShown)} more`, () => { openShown += OPEN_PAGE; renderList(); }, { kind: "small" }));
      }
      if (data.total > data.tasks.length) nodes.push(h("p", { class: "notice info" }, `Showing the first ${data.tasks.length} of ${data.total} tasks.`));
      listBox.replaceChildren(...nodes);
    });
  }

  function renderEvidence(): void {
    const t = data.tasks.find((x) => x.ordinal === selected);
    if (!t) {
      evidenceBox.hidden = true;
      evidenceBox.replaceChildren();
      evidenceKey = "";
      return;
    }
    const key = `${t.ordinal}:${t.status}:${t.revision}`;
    if (key === evidenceKey && !evidenceBox.hidden) return;
    evidenceKey = key;
    evidenceBox.hidden = false;
    const view = evidenceBody(ctx.workerName, ctx.missionId, t, true);
    void view.done.then((e) => ctx.verified(t.ordinal, e.verifier));
    evidenceBox.replaceChildren(h("h3", {}, `Evidence, task ${t.ordinal}`), view.el);
  }

  const el = h("div", { class: "tasks-panel stack" }, headBox, form, notices, listBox, evidenceBox);
  renderHead();
  renderList();

  return {
    el,
    update(next) {
      const sig = JSON.stringify([next, ctx.needs(), ctx.blocked(), selected, ctx.status()]);
      data = next;
      if (sig === signature) return;
      signature = sig;
      if (selected !== null && !data.tasks.some((t) => t.ordinal === selected)) selected = null;
      // The evidence of the task that awaits approval opens by itself: it is what the decision rests on. Choosing a task (or closing it) is respected.
      if (!touched && selected === null) selected = data.tasks.find((t) => rowState(t).id === "needs-approval")?.ordinal ?? null;
      renderHead();
      renderList();
      renderEvidence();
    },
    focusFirstAction() {
      const target = listBox.querySelector<HTMLElement>(".task-actions [data-action]:not(:disabled)") ?? listBox.querySelector<HTMLElement>(".task-title");
      target?.focus();
    },
  };
}
