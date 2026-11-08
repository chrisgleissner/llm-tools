import * as api from "./api";
import { h } from "./dom";
import { openPanel } from "./ui";

async function requestStepUp(): Promise<boolean> {
  return new Promise((resolve) => {
    const password = h("input", { type: "password", autocomplete: "current-password", required: true, "aria-label": "Current password" });
    const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 64, "aria-label": "Authenticator or recovery code" });
    const status = h("p", { role: "alert", class: "form-error" });
    const submit = h("button", { type: "submit", class: "btn primary" }, "Verify and continue");
    const form = h("form", { novalidate: true },
      h("p", { class: "muted" }, "For your security, verify your password again. If you use an authenticator, enter its code or a recovery code."),
      h("label", {}, "Current password", password),
      h("label", {}, "Authenticator or recovery code (if enabled)", code),
      status,
      h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => panel.close("cancel") }, "Cancel"), submit),
    );
    const panel = openPanel({ title: "Verify it is you", id: "step-up", body: form });
    let verified = false;
    panel.addEventListener("close", () => resolve(verified), { once: true });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!password.value) { status.textContent = "Enter your current password."; password.focus(); return; }
      submit.disabled = true;
      status.textContent = "";
      const value = code.value.trim();
      const mfa = value ? (/^\d{6}$/.test(value) ? { totp_code: value } : { recovery_code: value }) : {};
      try {
        await api.post("/api/v1/auth/step-up", { password: password.value, ...mfa });
        verified = true;
        panel.close("verified");
      } catch (error) {
        submit.disabled = false;
        status.textContent = error instanceof api.ApiError ? error.message : "Could not verify your credentials.";
      }
    });
    password.focus();
  });
}

/** Retry a rejected mutation only after the server asks for fresh authentication. */
export async function withStepUp<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    if (!(error instanceof api.ApiError) || error.code !== "step-up-required") throw error;
    if (!(await requestStepUp())) throw error;
    return operation();
  }
}
