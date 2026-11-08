// What the web interface shows after the host reset the password (PRD-036).
import * as api from "../api";
import { h } from "../dom";
import { mountFeedback } from "../ui";
import { passwordForm } from "./password-form";

const TITLE = "Choose a new password";
const SEEN_KEY = "bridge-reset-notice-seen";
let shownAt = 0;

/** After a temporary password: only the password change is available until it is done. */
export function forcedChangeView(app: HTMLElement, state: api.AuthState, hooks: { onChanged: () => void; signOut: () => void }): void {
  const intro = [h("p", { class: "muted", id: "forced-change-text" }, "An administrator reset your password on the host. Enter the temporary password, then choose a new one.")];
  if (state.password_reset_notice) intro.unshift(h("p", { class: "notice info", role: "status", id: "reset-notice" }, state.password_reset_notice.message));
  const form = passwordForm({ me: state, page: { title: TITLE, intro }, onPasswordChanged: hooks.onChanged });
  form.querySelector(".actions")?.append(h("button", { class: "btn ghost", type: "button", id: "forced-signout", onclick: hooks.signOut }, "Sign out"));
  app.replaceChildren(h("main", { id: "main", class: "auth", tabindex: "-1" }, form));
  document.title = `${TITLE} - LLM Bridge`;
  mountFeedback(app);
  form.querySelector<HTMLElement>("#current-password")?.focus();
}

/** The notice that the host reset the password, shown once per browser session; null when it was shown already. */
export function resetNotice(notice: { at: number; message: string }): HTMLElement | null {
  let seen = shownAt === notice.at;
  try {
    seen = seen || sessionStorage.getItem(SEEN_KEY) === String(notice.at);
    sessionStorage.setItem(SEEN_KEY, String(notice.at));
  } catch {
    /* storage can be unavailable; the module variable still prevents a second display during this page load */
  }
  shownAt = notice.at;
  if (seen) return null;
  const box = h(
    "div",
    { class: "notice info", role: "status", id: "reset-notice" },
    h("span", {}, notice.message),
    " ",
    h("button", { class: "btn ghost", type: "button", id: "reset-notice-dismiss", onclick: () => box.remove() }, "Dismiss"),
  );
  return box;
}
