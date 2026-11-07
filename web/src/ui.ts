import { ownRequests } from "./api";
import { h, hooks } from "./dom";
import { eventClass } from "./event-humanizer";
import { icon, type IconName } from "./icons";

let liveRegion: HTMLElement | null = null;
let toastBox: HTMLElement | null = null;

/** What happened to a requested change. These words match the worker's outcome vocabulary. */
export type Outcome = "accepted" | "applied" | "rejected" | "failed" | "uncertain";
/** `attention` is a new needs-you item (needs approval, needs input, failed): the only toast a stream event may cause. */
export type ToastKind = "info" | "error" | "attention" | Outcome;

const TOAST: Record<ToastKind, { icon: IconName; label: string }> = {
  info: { icon: "info", label: "Note" },
  error: { icon: "alert", label: "Error" },
  attention: { icon: "diamondCheck", label: "Needs you" },
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

export function mountFeedback(root: HTMLElement): void {
  liveRegion = h("div", { id: "live-status", class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
  toastBox = h("div", { id: "toasts", class: "toasts", role: "status", "aria-label": "Notifications", "aria-atomic": "false" });
  queue.length = 0;
  root.append(liveRegion, toastBox);
}

/** Speak a message to screen readers without showing anything (copy confirmations, routine changes). */
export function say(message: string): void {
  if (!liveRegion) return;
  liveRegion.textContent = "";
  window.setTimeout(() => liveRegion && (liveRegion.textContent = message), 30);
}

// Copy confirmations are inline on the button; they never toast.
hooks.toast = say;
hooks.say = say;

// ---- the interrupt ladder (spec 6.4): level 2 is a toast. At most two are visible (one on a phone), the rest queue as "+n".

interface ToastSpec {
  message: string;
  kind: ToastKind;
  detail: string;
  action: ToastAction | undefined;
}

export interface ToastAction {
  label: string;
  run: () => void;
}

export interface ToastOptions {
  /** Button inside the toast, for example "Open" on a needs-you toast. A toast with an action stays until dismissed. */
  action?: ToastAction;
}

const queue: ToastSpec[] = [];
const SUCCESS_MS = 4000;
const isPhone = () => window.matchMedia("(max-width: 47.99rem)").matches;
const maxVisible = () => (isPhone() ? 1 : 2);
const sticky = (spec: ToastSpec) => spec.kind === "error" || spec.kind === "failed" || spec.kind === "uncertain" || spec.kind === "rejected" || spec.action !== undefined;

function showToast(spec: ToastSpec): void {
  if (!toastBox) return;
  const t = TOAST[spec.kind];
  const loud = spec.kind === "error" || spec.kind === "failed" || spec.kind === "uncertain" || spec.kind === "rejected";
  const el = h("div", { class: `toast ${spec.kind}`, role: loud ? "alert" : null, "data-kind": spec.kind });
  const close = h("button", { type: "button", class: "btn icon ghost", "aria-label": "Dismiss notification" }, icon("x", 18));
  const actions = spec.action
    ? h("div", { class: "toast-actions" }, h("button", { type: "button", class: "btn small", onclick: () => { spec.action?.run(); dismiss(); } }, spec.action.label))
    : null;
  el.append(
    h("span", { class: "toast-icon" }, icon(t.icon, 20)),
    h("div", { class: "toast-text" }, spec.kind === "info" ? null : h("strong", {}, `${t.label}. `), spec.message, spec.detail ? h("div", { class: "muted small" }, spec.detail) : null, actions),
    close,
  );
  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    el.remove();
    pump();
  };
  close.addEventListener("click", dismiss);
  if (!sticky(spec)) {
    // Success and information toasts last 4 s and wait while the pointer or keyboard focus is on them.
    const arm = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(dismiss, SUCCESS_MS);
    };
    const hold = () => window.clearTimeout(timer);
    el.addEventListener("mouseenter", hold);
    el.addEventListener("mouseleave", arm);
    el.addEventListener("focusin", hold);
    el.addEventListener("focusout", arm);
    arm();
  }
  toastBox.insertBefore(el, toastBox.querySelector(".toast-more"));
}

function pump(): void {
  if (!toastBox) return;
  while (queue.length > 0 && toastBox.querySelectorAll(".toast").length < maxVisible()) showToast(queue.shift() as ToastSpec);
  toastBox.querySelector(".toast-more")?.remove();
  if (queue.length > 0) toastBox.append(h("div", { class: "toast-more" }, `+${queue.length} more`));
}

/**
 * Show a toast. Use it for the result of the operator's own action (one toast per action) and for a new needs-you item;
 * `announceStreamEvent` is the guarded entry for anything that comes from the event stream. Failures, uncertain results and
 * toasts with an action use role=alert and stay until dismissed.
 */
export function announce(message: string, kind: ToastKind = "info", detail = "", opts: ToastOptions = {}): void {
  if (!toastBox) return;
  if (kind === "attention") {
    // A needs-you toast is for an operator who can act on it now: not while the tab is hidden, and not while typing in the composer or the terminal.
    const active = document.activeElement as HTMLElement | null;
    if (!document.hasFocus() || active?.closest("[data-composer], .xterm, textarea, input")) return;
  }
  queue.push({ message, kind, detail, action: opts.action });
  pump();
}

/**
 * The only way a stream event can reach the operator's eyes. Plumbing and detail events (heartbeats, capacity refreshes, commands,
 * routine task changes) never toast and are not announced. The echo of the operator's own request (a request id this browser sent)
 * is dropped because the action already toasted. A milestone is spoken through the live region; only a caller that knows the event
 * created a new needs-you item passes `needsYou`, which shows one attention toast. Returns whether a toast was queued.
 */
export function announceStreamEvent(event: { kind: string }, message: string, opts: { requestId?: string; needsYou?: boolean; action?: ToastAction } = {}): boolean {
  if (eventClass(event.kind) !== "milestone") return false;
  if (opts.requestId && ownRequests.has(opts.requestId)) return false;
  if (opts.needsYou) {
    const before = queue.length + (toastBox?.querySelectorAll(".toast").length ?? 0);
    announce(message, "attention", "", opts.action ? { action: opts.action } : {});
    return queue.length + (toastBox?.querySelectorAll(".toast").length ?? 0) > before;
  }
  say(message);
  return false;
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
    const reason = opts.reasonLabel ? h("textarea", { id: "dialog-reason", rows: 3, "aria-describedby": "dialog-reason-error" }) : null;
    const reasonError = h("p", { class: "field-error", id: "dialog-reason-error", role: "alert" });
    const dlg = h("dialog", { class: "dialog", "aria-labelledby": "dialog-title", "aria-describedby": "dialog-msg" });
    const form = h(
      "form",
      { method: "dialog", novalidate: true },
      h("h2", { id: "dialog-title" }, opts.title),
      h("p", { id: "dialog-msg" }, opts.message),
      opts.details ?? null,
      reason && h("div", { class: "field" }, h("label", { for: "dialog-reason" }, opts.reasonLabel ?? ""), reason, reasonError),
      h(
        "div",
        { class: "actions end" },
        h("button", { type: "button", class: "btn", id: "dialog-cancel", onclick: () => dlg.close("cancel") }, "Go back"),
        h("button", { type: "submit", class: `btn ${opts.danger ? "danger solid" : "primary"}`, id: "dialog-confirm", value: "ok" }, opts.confirmLabel),
      ),
    );
    // The reason is validated inline (no browser bubble): an empty reason shows a message next to the field and keeps the dialog open.
    form.addEventListener("submit", (e) => {
      if (reason && !reason.value.trim()) {
        e.preventDefault();
        reasonError.textContent = "Enter a reason.";
        reason.setAttribute("aria-invalid", "true");
        reason.focus();
      }
    });
    reason?.addEventListener("input", () => {
      if (reason.value.trim()) {
        reasonError.textContent = "";
        reason.removeAttribute("aria-invalid");
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
