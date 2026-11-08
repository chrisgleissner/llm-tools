import { h, hooks } from "./dom";
import { icon, type IconName } from "./icons";

let liveRegion: HTMLElement | null = null;
let toastBox: HTMLElement | null = null;

/** What happened to a requested change. These words match the worker's outcome vocabulary. */
export type Outcome = "accepted" | "applied" | "rejected" | "failed" | "uncertain";
export type ToastKind = "info" | "error" | Outcome;

const TOAST: Record<ToastKind, { icon: IconName; label: string }> = {
  info: { icon: "info", label: "Note" },
  error: { icon: "alert", label: "Error" },
  accepted: { icon: "clock", label: "Accepted" },
  applied: { icon: "checkCircle", label: "Applied" },
  rejected: { icon: "ban", label: "Rejected" },
  failed: { icon: "alert", label: "Failed" },
  uncertain: { icon: "help", label: "Uncertain" },
};

export const OUTCOME_HELP: Record<Outcome, string> = {
  accepted: "The coordinator queued the request. The worker has not confirmed it yet.",
  applied: "The worker carried the change out.",
  rejected: "The worker refused the change. Nothing was modified.",
  failed: "The request could not be carried out. Nothing was modified.",
  uncertain: "The request may or may not have been applied. Refresh and check before trying again.",
};

const nextRoute: Array<[string, ToastKind, string]> = [];

/** Toast for the page that is about to replace this one: every route change mounts a fresh toast box, so it is shown once that page is up. */
export function announceNextPage(message: string, kind: ToastKind = "info", detail = ""): void {
  nextRoute.push([message, kind, detail]);
}

export function mountFeedback(root: HTMLElement): void {
  liveRegion = h("div", { id: "live-status", class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
  toastBox = h("div", { id: "toasts", class: "toasts" });
  root.append(liveRegion, toastBox);
  for (const [message, kind, detail] of nextRoute.splice(0)) announce(message, kind, detail);
}

hooks.toast = (message) => announce(message);

/** Announce a message to screen readers and show it briefly. Failures and uncertain results use role=alert and stay longer. */
export function announce(message: string, kind: ToastKind = "info", detail = ""): void {
  if (liveRegion) {
    liveRegion.textContent = "";
    window.setTimeout(() => liveRegion && (liveRegion.textContent = `${kind === "info" ? "" : TOAST[kind].label + ": "}${message}`), 30);
  }
  if (!toastBox) return;
  const t = TOAST[kind];
  const loud = kind === "error" || kind === "failed" || kind === "uncertain" || kind === "rejected";
  const el = h("div", { class: `toast ${kind}`, role: loud ? "alert" : null, "data-kind": kind });
  const close = h("button", { type: "button", class: "btn icon ghost", "aria-label": "Dismiss notification" }, icon("x", 18));
  close.addEventListener("click", () => el.remove());
  el.append(
    h("span", { class: "toast-icon" }, icon(t.icon, 20)),
    h("div", { class: "toast-text" }, kind === "info" ? null : h("strong", {}, `${t.label}. `), message, detail ? h("div", { class: "muted small" }, detail) : null),
    close,
  );
  toastBox.append(el);
  while (toastBox.children.length > 4) toastBox.firstElementChild?.remove();
  window.setTimeout(() => el.remove(), loud ? 10000 : 4500);
}

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  reasonLabel?: string;
  /** Extra content shown under the message, for example the details of what will change. */
  details?: Node;
}

/** Modal confirmation built on <dialog>: focus is trapped, Escape cancels. Resolves with the reason text ("" when none) or null. */
export function confirmDialog(opts: ConfirmOptions): Promise<string | null> {
  return new Promise((resolve) => {
    const reason = opts.reasonLabel ? h("textarea", { id: "dialog-reason", rows: 3, required: true }) : null;
    const dlg = h("dialog", { class: "dialog", "aria-labelledby": "dialog-title", "aria-describedby": "dialog-msg" });
    const form = h(
      "form",
      { method: "dialog" },
      h("h2", { id: "dialog-title" }, opts.title),
      h("p", { id: "dialog-msg" }, opts.message),
      opts.details ?? null,
      reason && h("div", { class: "field" }, h("label", { for: "dialog-reason" }, opts.reasonLabel ?? ""), reason),
      h(
        "div",
        { class: "actions end" },
        h("button", { type: "button", class: "btn", id: "dialog-cancel", onclick: () => dlg.close("cancel") }, "Go back"),
        h("button", { type: "submit", class: `btn ${opts.danger ? "danger solid" : "primary"}`, id: "dialog-confirm", value: "ok" }, opts.confirmLabel),
      ),
    );
    form.addEventListener("submit", (e) => {
      if (reason && !reason.value.trim()) {
        e.preventDefault();
        reason.focus();
      }
    });
    dlg.append(form);
    dlg.addEventListener("close", () => {
      const ok = dlg.returnValue === "ok";
      const value = reason ? reason.value.trim() : "";
      dlg.remove();
      resolve(ok ? value : null);
    });
    document.body.append(dlg);
    dlg.showModal();
    (reason ?? dlg.querySelector<HTMLElement>("#dialog-cancel"))?.focus();
  });
}

/** A modal panel with arbitrary content (the shortcut list, the mobile "More" sheet, the worker enrolment result). */
export function openPanel(opts: { title: string; id: string; body: Node; sheet?: boolean; closeLabel?: string }): HTMLDialogElement {
  document.getElementById(opts.id)?.remove();
  const dlg = h("dialog", { class: `dialog${opts.sheet ? " sheet" : ""}`, id: opts.id, "aria-labelledby": `${opts.id}-title` });
  const close = h("button", { type: "button", class: "btn icon ghost", "aria-label": "Close" }, icon("x", 20));
  close.addEventListener("click", () => dlg.close());
  dlg.append(h("div", { class: "dialog-head" }, h("h2", { id: `${opts.id}-title` }, opts.title), close), opts.body);
  // A click on the backdrop (outside the panel's box) closes it.
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) dlg.close();
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  close.focus();
  return dlg;
}

export const SHORTCUTS: Array<[string, string]> = [
  ["?", "Show this list"],
  ["/", "Focus the search box"],
  ["g then o", "Go to Overview"],
  ["g then w", "Go to Workers"],
  ["g then m", "Go to Missions"],
  ["g then s", "Go to Sessions"],
  ["g then u", "Go to Usage"],
  ["g then d", "Go to Diagnostics"],
  ["g then a", "Go to Account"],
  ["t", "Change the theme"],
  ["Esc", "Close a dialog or menu"],
];

export function shortcutHelp(): void {
  const body = h(
    "div",
    {},
    h("p", { class: "muted" }, "Shortcuts work when no text field has focus."),
    h("dl", { class: "shortcuts" }, ...SHORTCUTS.flatMap(([keys, what]) => [h("dt", {}, ...keys.split(" ").map((k) => (k === "then" ? ` ${k} ` : h("kbd", {}, k)))), h("dd", {}, what)])),
    h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => (document.getElementById("shortcut-help") as HTMLDialogElement | null)?.close() }, "Close")),
  );
  openPanel({ title: "Keyboard shortcuts", id: "shortcut-help", body });
}
