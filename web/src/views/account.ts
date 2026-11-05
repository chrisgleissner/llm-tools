import * as api from "../api";
import { field, h } from "../dom";
import { announce } from "../ui";

export function account(root: HTMLElement, onPasswordChanged: () => void): () => void {
  const cur = field("current-password", "Current password", "password", { autocomplete: "current-password", required: true });
  const next = field("new-password", "New password", "password", { autocomplete: "new-password", required: true });
  const again = field("new-password-confirm", "Repeat new password", "password", { autocomplete: "new-password", required: true });
  const error = h("p", { class: "form-error", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Change password");
  const form = h(
    "form",
    { class: "card narrow", id: "password-form" },
    h("h2", {}, "Change password"),
    h("p", { class: "muted" }, "Changing the password signs out every session, including this one."),
    cur.wrap,
    next.wrap,
    again.wrap,
    error,
    h("div", { class: "actions" }, submit),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    if (next.input.value !== again.input.value) {
      error.textContent = "The new passwords do not match.";
      again.input.focus();
      return;
    }
    submit.disabled = true;
    try {
      await api.changePassword(cur.input.value, next.input.value);
      announce("Password changed. Sign in again.");
      onPasswordChanged();
    } catch (err) {
      error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error.";
      submit.disabled = false;
    }
  });
  root.append(h("h1", {}, "Account"), form);
  return () => {};
}
