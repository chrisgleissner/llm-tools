import * as api from "../api";
import { field, h, relFromMs, timeEl } from "../dom";
import { DEFAULT_POLICY } from "../password";
import { announce } from "../ui";
import { passkeyProviderName } from "../passkey-provider";
import { minutes, newPasswordFields, revealable } from "./login";

export interface AccountOpts {
  me: api.AuthState | null;
  signOut: () => void;
  onPasswordChanged: () => void;
}

function tokenManagement(): HTMLElement {
  const section = h("section", { class: "card narrow", "aria-labelledby": "api-tokens-title" });
  const status = h("p", { role: "status", "aria-live": "polite" });
  const rows = h("ul", { class: "token-list", "aria-label": "API tokens" });
  const oneTime = h("div", { class: "token-secret", hidden: true, "aria-live": "polite" });
  const name = field("api-token-name", "Token name", "text", { autocomplete: "off", maxlength: 63, required: true });
  const scopeNames = ["read", "operate", "terminal:view", "terminal:write", "admin"] as const;
  const scopeInputs = scopeNames.map((scope, i) => {
    const input = h("input", { type: "checkbox", id: `token-scope-${i}`, value: scope, checked: scope === "read" });
    return { scope, input, label: h("label", { class: "check", for: input.id }, input, h("span", {}, scope)) };
  });
  const expiry = h(
    "select",
    { id: "api-token-expiry", name: "expires_days", "aria-label": "Token expiry" },
    h("option", { value: "30" }, "30 days"),
    h("option", { value: "90", selected: true }, "90 days"),
    h("option", { value: "365" }, "1 year"),
    h("option", { value: "0" }, "Never"),
  );
  const allowedNetworks = field("api-token-networks", "Allowed client networks (optional)", "text", {
    autocomplete: "off", maxlength: 2048, placeholder: "192.0.2.0/24, 2001:db8::/32",
  });
  const create = h("button", { type: "submit", class: "btn primary" }, "Create token");
  const form = h(
    "form",
    { class: "token-create", id: "api-token-form", novalidate: true },
    h("h3", {}, "Create an API token"),
    name.wrap,
    h("fieldset", {}, h("legend", {}, "Permissions"), ...scopeInputs.map((x) => x.label)),
    h("label", { for: "api-token-expiry" }, "Expires after"),
    expiry,
    allowedNetworks.wrap,
    h("p", { class: "hint" }, "Leave blank to allow requests from any address. Separate CIDR ranges with commas."),
    h("p", { class: "hint" }, "The secret appears once. Save it in your password manager; it cannot be shown again."),
    status,
    create,
  );
  const refresh = async (): Promise<void> => {
    try {
      const result = await api.get<{ tokens: api.ApiTokenInfo[] }>("/api/v1/tokens");
      rows.replaceChildren();
      for (const token of result.tokens) {
        const revoke = h("button", { class: "btn small", type: "button", "aria-label": `Revoke ${token.name}` }, "Revoke");
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          try {
            await api.del(`/api/v1/tokens/${encodeURIComponent(token.name)}`);
            status.textContent = `Token ${token.name} revoked.`;
            await refresh();
          } catch (err) {
            revoke.disabled = false;
            status.textContent = err instanceof api.ApiError ? err.message : "Could not revoke token.";
          }
        });
        const lastUsed = token.last_used
          ? `Last used ${new Date(token.last_used).toLocaleString()} from ${token.last_address ?? "unknown address"}`
          : "Not used yet";
        rows.append(h("li", {}, h("strong", {}, token.name), h("span", {}, token.scopes.join(", ")), h("span", {}, `Expires ${token.expires_at ? new Date(token.expires_at).toLocaleString() : "never"}`), h("span", {}, token.allowed_cidrs.length ? `Networks ${token.allowed_cidrs.join(", ")}` : "Any client network"), h("span", {}, lastUsed), revoke));
      }
      if (!result.tokens.length) rows.append(h("li", { class: "muted" }, "No API tokens yet."));
    } catch (err) {
      rows.replaceChildren(h("li", { class: "form-error", role: "alert" }, err instanceof api.ApiError ? err.message : "Could not load API tokens."));
    }
  };
  oneTime.addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-copy-token]");
    if (button) {
      const secret = oneTime.querySelector("code")?.textContent ?? "";
      if (!navigator.clipboard) status.textContent = "Clipboard access is unavailable in this browser.";
      else void navigator.clipboard.writeText(secret).then(() => { status.textContent = "Token copied."; });
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "";
    const scopes = scopeInputs.filter((x) => x.input.checked).map((x) => x.scope);
    if (!name.input.value.trim() || !scopes.length) {
      status.textContent = "Enter a name and select at least one permission.";
      return;
    }
    create.disabled = true;
    try {
      const created = await api.post<api.ApiTokenCreated>("/api/v1/tokens", {
        name: name.input.value.trim(), scopes, expires_days: Number(expiry.value),
        allowed_cidrs: allowedNetworks.input.value.split(",").map((value) => value.trim()).filter(Boolean),
      });
      oneTime.hidden = false;
      oneTime.replaceChildren(
        h("p", {}, `Copy this secret for ${created.token.name}. It will not be shown again.`),
        h("pre", {}, h("code", {}, created.secret)),
        h("button", { type: "button", class: "btn", "data-copy-token": "" }, "Copy token"),
      );
      status.textContent = `Token ${created.token.name} created.`;
      name.input.value = "";
      allowedNetworks.input.value = "";
      await refresh();
    } catch (err) {
      status.textContent = err instanceof api.ApiError ? err.message : "Could not create token.";
    } finally {
      create.disabled = false;
    }
  });
  section.append(h("h2", { id: "api-tokens-title" }, "API tokens"), h("p", { class: "muted" }, "Use tokens for scripts and tools. Grant only the permissions they need."), rows, form, oneTime);
  void refresh();
  return section;
}

function passkeyReenrollment(): HTMLElement {
  const section = h("section", { class: "card narrow", "aria-labelledby": "passkey-reenroll-title" });
  const current = field("passkey-reenroll-password", "Current password", "password", { autocomplete: "current-password", required: true });
  const confirm = h("input", { id: "passkey-reenroll-confirm", type: "checkbox", required: true });
  const status = h("p", { role: "status", "aria-live": "polite" });
  const submit = h("button", { class: "btn danger", type: "submit" }, "Reset passkeys for this address");
  const form = h("form", { novalidate: true },
    h("p", { class: "muted" }, "To change the trusted address, open this page at the new HTTPS address. All existing passkeys will be removed and must be registered again; your password remains available."),
    current.wrap,
    h("label", { class: "check", for: confirm.id }, confirm, h("span", {}, "I understand this removes every registered passkey.")),
    status,
    submit,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "";
    if (!current.input.value || !confirm.checked) {
      status.textContent = "Enter your current password and confirm the passkey reset.";
      return;
    }
    submit.disabled = true;
    try {
      const result = await api.reenrollPasskeys(current.input.value, confirm.checked);
      current.input.value = "";
      status.textContent = `Passkeys were removed. Register a new passkey for ${result.origin}.`;
    } catch (error) {
      status.textContent = error instanceof api.ApiError ? error.message : "Could not reset passkeys.";
    } finally {
      submit.disabled = false;
    }
  });
  section.append(h("h2", { id: "passkey-reenroll-title" }, "Change passkey address"), form);
  return section;
}

function passkeyList(): HTMLElement {
  const section = h("section", { class: "card narrow", "aria-labelledby": "passkey-list-title" });
  const rows = h("ul", { class: "token-list", "aria-label": "Registered passkeys" });
  const status = h("p", { role: "status", "aria-live": "polite" }, "Loading passkeys…");
  section.append(h("h2", { id: "passkey-list-title" }, "Passkeys"), h("p", { class: "muted" }, "Registered sign-in devices. Provider names are best-effort AAGUID labels; some passkeys do not identify a provider. Synced means the passkey provider backs it up across devices."), status, rows);
  void api.get<{ passkeys: Array<{ id: string; name: string; provider_aaguid: string | null; created_at: string; last_used: string | null; synced: boolean }> }>("/api/v1/auth/passkeys")
    .then(({ passkeys }) => {
      status.textContent = "";
      if (!passkeys.length) {
        rows.replaceChildren(h("li", { class: "muted" }, "No passkeys registered."));
        return;
      }
      rows.replaceChildren(...passkeys.map((passkey) => h("li", {},
        h("strong", {}, passkey.name),
        h("span", {}, `Provider ${passkeyProviderName(passkey.provider_aaguid)}`),
        h("span", {}, passkey.synced ? "Synced passkey" : "Not reported as synced"),
        h("span", {}, `Added ${new Date(passkey.created_at).toLocaleString()}`),
        h("span", {}, passkey.last_used ? `Last used ${new Date(passkey.last_used).toLocaleString()}` : "Not used yet"),
      )));
    })
    .catch((error) => { status.textContent = error instanceof api.ApiError ? error.message : "Could not load passkeys."; });
  return section;
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
  const passkeyGuidance = h("section", { id: "passkey-support-guidance", class: "card narrow", hidden: true });
  void import("../passkeys").then(async ({ detectPasskeySupport, supportsPasskeysOnHost }) => {
    const capability = await detectPasskeySupport();
    if (!capability.supported || !supportsPasskeysOnHost(window.location.hostname)) {
      passkeyGuidance.hidden = false;
      passkeyGuidance.replaceChildren(
        h("h2", {}, "Passkeys"),
        h("p", { class: "muted" }, "Passkeys need a trusted address such as Tailscale."),
        h("a", {
          href: "https://github.com/chrisgleissner/llm-tools/blob/main/docs/distributed/install.md#enable-passkeys-optional",
          target: "_blank",
          rel: "noopener",
        }, "Enable passkeys (optional)"),
      );
    }
  }).catch(() => {});
  root.append(h("h1", {}, "Account"), h("div", { class: "grid-2" }, session, form), tokenManagement(),
    me?.passkey_canonical_origin ? passkeyList() : h("div", {}),
    me?.passkey_canonical_origin ? passkeyReenrollment() : h("div", {}), passkeyGuidance);
  return () => {};
}
