import { h } from "./dom";

let liveRegion: HTMLElement | null = null;
let toastBox: HTMLElement | null = null;

export function mountFeedback(root: HTMLElement): void {
  liveRegion = h("div", { id: "live-status", class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
  toastBox = h("div", { id: "toasts", class: "toasts" });
  root.append(liveRegion, toastBox);
}

/** Announce a result to screen readers and show it briefly. `error` toasts use role=alert. */
export function announce(message: string, kind: "info" | "error" = "info"): void {
  if (liveRegion) {
    liveRegion.textContent = "";
    window.setTimeout(() => liveRegion && (liveRegion.textContent = message), 30);
  }
  if (!toastBox) return;
  const t = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : null }, message);
  toastBox.append(t);
  window.setTimeout(() => t.remove(), kind === "error" ? 9000 : 4000);
}

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  reasonLabel?: string;
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
      reason && h("div", { class: "field" }, h("label", { for: "dialog-reason" }, opts.reasonLabel ?? ""), reason),
      h(
        "div",
        { class: "actions" },
        h("button", { type: "button", class: "btn", id: "dialog-cancel", onclick: () => dlg.close("cancel") }, "Go back"),
        h("button", { type: "submit", class: `btn ${opts.danger ? "danger" : "primary"}`, id: "dialog-confirm", value: "ok" }, opts.confirmLabel),
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
