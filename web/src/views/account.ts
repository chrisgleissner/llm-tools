import * as api from "../api";
import { field, h, relFromMs, timeEl } from "../dom";
import { DEFAULT_POLICY } from "../password";
import { announce } from "../ui";
import { minutes, newPasswordFields, revealable } from "./login";

export interface AccountOpts {
  me: api.AuthState | null;
  signOut: () => void;
  onPasswordChanged: () => void;
}

export function account(root: HTMLElement, opts: AccountOpts): () => void {
  const me = opts.me;
  const policy = me?.password_policy ?? DEFAULT_POLICY;
  const s = me?.session;
  const cur = field("current-password", "Current password", "password", { autocomplete: "current-password", required: true });
  revealable(cur, "current password");
  const np = newPasswordFields(policy, () => me?.username ?? "", { passId: "new-password", passLabel: "New password", confirmId: "new-password-confirm", confirmLabel: "Repeat new password" });
  const keep = h("input", { type: "checkbox", id: "keep-tokens" });
  const error = h("p", { class: "form-error box", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Change password");
  const form = h(
    "form",
    { class: "card narrow", id: "password-form", novalidate: true },
    h("h2", {}, "Change password"),
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
  const exp = s ? new Date(s.expires_at * 1000) : null;
  const session = h(
    "section",
    { class: "card narrow", "aria-labelledby": "acct-session" },
    h("h2", { id: "acct-session" }, "This session"),
    h(
      "table",
      { class: "kv" },
      h("caption", { class: "sr-only" }, "Session details"),
      h(
        "tbody",
        {},
        h("tr", {}, h("th", { scope: "row" }, "Signed in as"), h("td", {}, me?.username ?? "-")),
        h("tr", {}, h("th", { scope: "row" }, "Device"), h("td", {}, s ? (s.remember ? "Remembered" : "Not remembered (browser session)") : "-")),
        h("tr", {}, h("th", { scope: "row" }, "Signs out at the latest"), h("td", {}, exp ? timeEl(exp.toISOString()) : "-")),
        h("tr", {}, h("th", { scope: "row" }, "Idle limit"), h("td", {}, s ? `${minutes(s.idle_timeout_secs)} without activity` : "-")),
      ),
    ),
    h("div", { class: "actions" }, h("button", { type: "button", class: "btn", id: "account-signout", onclick: opts.signOut }, "Sign out")),
    exp ? h("p", { class: "muted small" }, `That is ${relFromMs(exp.getTime()).replace("in ", "in about ")}.`) : null,
  );
  root.append(h("h1", {}, "Account"), h("div", { class: "grid-2" }, session, form));
  return () => {};
}
