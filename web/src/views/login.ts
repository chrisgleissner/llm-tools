import * as api from "../api";
import { field, h } from "../dom";

export interface LoginOpts {
  setupRequired: boolean;
  notice?: string;
  onDone: () => void;
}

export function loginView(root: HTMLElement, opts: LoginOpts): void {
  const setupMode = opts.setupRequired;
  const error = h("p", { class: "form-error", role: "alert", id: "form-error" });
  const token = field("setup-token", "Setup token", "password", { autocomplete: "off", required: true });
  const user = field("username", "Username", "text", { autocomplete: "username", required: true, autocapitalize: "none", spellcheck: "false" });
  const pass = field("password", setupMode ? "New password" : "Password", "password", { autocomplete: setupMode ? "new-password" : "current-password", required: true });
  const confirm = field("password-confirm", "Repeat password", "password", { autocomplete: "new-password", required: true });
  const submit = h("button", { type: "submit", class: "btn primary" }, setupMode ? "Create administrator" : "Sign in");

  const form = h(
    "form",
    { class: "card narrow", id: setupMode ? "setup-form" : "login-form", novalidate: true },
    h("h1", {}, setupMode ? "First-run setup" : "Sign in"),
    setupMode
      ? h("p", { class: "muted" }, "Create the administrator. The setup token is in the file ", h("code", {}, "setup-token"), " in the coordinator data directory.")
      : h("p", { class: "muted" }, "LLM Bridge coordinator"),
    opts.notice ? h("p", { class: "notice", role: "status" }, opts.notice) : null,
    setupMode ? token.wrap : null,
    user.wrap,
    pass.wrap,
    setupMode ? confirm.wrap : null,
    error,
    h("div", { class: "actions" }, submit),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    if (!user.input.value || !pass.input.value || (setupMode && (!token.input.value || !confirm.input.value))) {
      error.textContent = "Fill in every field.";
      return;
    }
    if (setupMode && pass.input.value !== confirm.input.value) {
      error.textContent = "The passwords do not match.";
      confirm.input.focus();
      return;
    }
    submit.disabled = true;
    try {
      if (setupMode) await api.setup(token.input.value, user.input.value, pass.input.value);
      await api.login(user.input.value, pass.input.value);
      opts.onDone();
    } catch (err) {
      error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error.";
      submit.disabled = false;
    }
  });
  root.replaceChildren(h("main", { id: "main", class: "login", tabindex: "-1" }, form));
  (setupMode ? token.input : user.input).focus();
}
