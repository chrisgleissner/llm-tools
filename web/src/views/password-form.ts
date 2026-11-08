import * as api from "../api";
import { field, h } from "../dom";
import { DEFAULT_POLICY } from "../password";
import { announce } from "../ui";
import { minutes, newPasswordFields, revealable } from "./login";

export interface PasswordFormOpts {
  me: api.AuthState | null;
  onPasswordChanged: () => void;
  /** Replaces the card and heading when the form is the whole page (after a reset on the host): the heading text and the paragraphs below it. */
  page?: { title: string; intro: HTMLElement[] };
}

/** The change-password form: the account page shows it, and so does the screen that blocks everything else after a host reset. */
export function passwordForm(opts: PasswordFormOpts): HTMLElement {
  const me = opts.me;
  const policy = me?.password_policy ?? DEFAULT_POLICY;
  const cur = field("current-password", "Current password", "password", { autocomplete: "current-password", required: true });
  revealable(cur, "current password");
  const np = newPasswordFields(policy, () => me?.username ?? "", { passId: "new-password", passLabel: "New password", confirmId: "new-password-confirm", confirmLabel: "Repeat new password" });
  const keep = h("input", { type: "checkbox", id: "keep-tokens" });
  const error = h("p", { class: "form-error box", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Change password");
  const form = h(
    "form",
    { class: opts.page ? "card auth-card" : "panel account-block", id: "password-form", novalidate: true },
    h(opts.page ? "h1" : "h2", {}, opts.page?.title ?? "Change password"),
    ...(opts.page?.intro ?? []),
    h("p", { class: "muted" }, "Changing the password signs out every session, including this one. You sign in again with the new password."),
    cur.wrap,
    ...np.nodes,
    h("label", { class: "check", for: "keep-tokens" }, keep, h("span", {}, "Keep existing API tokens valid")),
    h("p", { class: "hint" }, "By default every API token is revoked as well. Worker enrolment tokens are not affected."),
    error,
    h("div", { class: "actions" }, submit),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    cur.setError("");
    if (!cur.input.value) {
      cur.setError("Enter your current password.");
      cur.input.focus();
      return;
    }
    if (!np.validatePassword(true) || !np.validateConfirm(true)) return;
    submit.disabled = true;
    submit.classList.add("busy");
    try {
      await api.post("/api/v1/auth/password", { current: cur.input.value, new: np.pass.input.value, keep_api_tokens: keep.checked });
      announce("Password changed. Sign in again.", "applied");
      opts.onPasswordChanged();
    } catch (err) {
      submit.disabled = false;
      submit.classList.remove("busy");
      if (err instanceof api.ApiError && err.code === "invalid-credentials") {
        cur.setError("The current password is not correct.");
        cur.input.focus();
      } else if (err instanceof api.ApiError && err.status === 429) error.textContent = `Too many wrong current passwords. Try again in ${minutes(err.retryAfter ?? 60)}.`;
      else error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error. Try again.";
    }
  });
  return form;
}
