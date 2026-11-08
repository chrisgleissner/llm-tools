import * as api from "../api";
import { h } from "../dom";
import { DEFAULT_POLICY } from "../password";
import { announce } from "../ui";
import { check, checkbox, errorBox, failure, newPasswordFields, secret, submitButton, working } from "./login";

export interface PasswordFormOpts {
  me: api.AuthState | null;
  onPasswordChanged: () => void;
  /** Replaces the card and heading when the form is the whole page (after a reset on the host): the heading text and the paragraphs below it. */
  page?: { title: string; intro: Array<HTMLElement | null | undefined> };
}

/** The change-password form: the account page shows it, and so does the screen that blocks everything else after a host reset. */
export function passwordForm(opts: PasswordFormOpts): HTMLElement {
  const me = opts.me;
  const page = opts.page;
  const cur = secret("current-password", "Current password", "current-password", "current password");
  const np = newPasswordFields(me?.password_policy ?? DEFAULT_POLICY, () => me?.username ?? "", "new-password", "Repeat new password");
  const [keep, keepLabel] = checkbox("keep-tokens", "Keep existing API tokens valid");
  const error = errorBox();
  const submit = submitButton("Change password");
  const form = h(
    "form",
    { class: page ? "card auth-card" : "panel account-block", id: "password-form", novalidate: true },
    h(page ? "h1" : "h2", {}, page?.title ?? "Change password"),
    ...(page?.intro ?? []),
    h("p", { class: "muted" }, "Changing the password signs out every session, including this one. You sign in again with the new password."),
    cur.wrap,
    ...np.nodes,
    keepLabel,
    h("p", { class: "hint" }, "By default every API token is revoked as well. Worker enrolment tokens are not affected."),
    error,
    h("div", { class: "actions" }, submit),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    if (!check(cur, cur.input.value ? "" : "Enter your current password.", true) || !np.validatePassword(true) || !np.validateConfirm(true)) return;
    working(submit, true);
    try {
      await api.post("/api/v1/auth/password", { current: cur.input.value, new: np.pass.input.value, keep_api_tokens: keep.checked });
      announce("Password changed. Sign in again.", "applied");
      opts.onPasswordChanged();
    } catch (err) {
      working(submit, false);
      if (err instanceof api.ApiError && err.code === "invalid-credentials") check(cur, "The current password is not correct.", true);
      else error.textContent = failure(err, { 429: "Too many wrong current passwords." });
    }
  });
  return form;
}
