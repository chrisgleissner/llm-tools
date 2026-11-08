import * as api from "./api";
import { h } from "./dom";
import { openPanel } from "./ui";
import { detectPasskeySupport, requestOptions, serializeAssertion } from "./passkeys";

async function requestStepUp(requirePasskey = false): Promise<boolean> {
  return new Promise((resolve) => {
    const password = h("input", { type: "password", autocomplete: "current-password", required: true, "aria-label": "Current password" });
    const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 64, "aria-label": "Authenticator or recovery code" });
    const status = h("p", { role: "alert", class: "form-error" });
    const submit = h("button", { type: "submit", class: "btn primary" }, "Verify and continue");
    const passkey = h("button", { type: "button", class: "btn", hidden: true }, "Use a passkey instead");
    const form = h("form", { novalidate: true },
      h("p", { class: "muted" }, "For your security, verify your password again. If you use an authenticator, enter its code or a recovery code."),
      h("label", {}, "Current password", password),
      h("label", {}, "Authenticator or recovery code (if enabled)", code),
      status,
      h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => panel.close("cancel") }, "Cancel"), passkey, submit),
    );
    const panel = openPanel({ title: requirePasskey ? "Use a passkey to continue" : "Verify it is you", id: "step-up", body: form });
    if (requirePasskey) {
      password.closest("label")!.hidden = true;
      code.closest("label")!.hidden = true;
      submit.hidden = true;
    }
    let verified = false;
    panel.addEventListener("close", () => resolve(verified), { once: true });
    void (async () => {
      try {
        const [{ passkeys }, support] = await Promise.all([
          api.get<{ passkeys: unknown[] }>("/api/v1/auth/passkeys"),
          detectPasskeySupport(),
        ]);
        passkey.hidden = !passkeys.length || !support.supported;
        if (requirePasskey && !passkeys.length) status.textContent = "Register a passkey in Account settings before creating an API token.";
        if (requirePasskey && passkeys.length && !support.supported) status.textContent = "This browser cannot complete a passkey assertion.";
      } catch { passkey.hidden = true; }
    })();
    passkey.addEventListener("click", async () => {
      passkey.disabled = true;
      submit.disabled = true;
      status.textContent = "";
      try {
        const ceremony = await api.beginPasskeyStepUp();
        const credential = await navigator.credentials.get({ publicKey: requestOptions(ceremony.options) });
        if (!credential) throw new Error("No passkey was selected.");
        await api.finishPasskeyStepUp(ceremony.challenge_id, serializeAssertion(credential));
        verified = true;
        panel.close("verified");
      } catch (error) {
        passkey.disabled = false;
        submit.disabled = false;
        status.textContent = error instanceof api.ApiError ? error.message : "Could not verify with a passkey.";
      }
    });
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
    if (!(error instanceof api.ApiError) || !["step-up-required", "passkey-step-up-required"].includes(error.code)) throw error;
    if (!(await requestStepUp(error.code === "passkey-step-up-required"))) throw error;
    return operation();
  }
}
