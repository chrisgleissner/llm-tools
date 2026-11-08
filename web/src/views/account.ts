import * as api from "../api";
import { absTime, button, copyButton, field, h, isoTime, relFromMs } from "../dom";
import { apiMsg, btn, facts } from "../kit-b";
import { detectPasskeySupport, supportsPasskeysOnHost } from "../passkeys";
import { menu } from "../menu";
import { passkeyProviderName } from "../passkey-provider";
import { openPanel } from "../ui";
import { answerLine, onceDialog } from "./remote";
import { installDocs, minutes } from "./login";
import { passwordForm } from "./password-form";

export interface AccountOpts {
  me: api.AuthState | null;
  signOut: () => void;
  onPasswordChanged: () => void;
}

/** Each permission and what it allows, in a few words; the last two are powerful and say so. */
const SCOPE_HELP: Record<string, string> = {
  read: "See missions and usage.",
  operate: "Start, pause, stop missions.",
  "terminal:view": "Watch live terminals.",
  "terminal:write": "Type into terminals. Powerful.",
  admin: "Manage workers and tokens. Powerful.",
};

/** One panel of the account page, named by its heading. */
const block = (labelledBy: string, ...children: Array<Node | string>): HTMLElement => h("section", { class: "panel account-block", "aria-labelledby": labelledBy }, ...children);
const hint = (text: string): HTMLElement => h("p", { class: "hint" }, text);
const when = (iso: string): string => new Date(iso).toLocaleString();

/** The dialog that creates a token. It closes itself and hands the secret to `onCreated`, which shows it once. */
function createTokenDialog(onCreated: (created: api.ApiTokenCreated) => void): void {
  const name = field("api-token-name", "Token name", "text", { autocomplete: "off", maxlength: 63, required: true });
  const scopeInputs = Object.keys(SCOPE_HELP).map((scope, i) => {
    const id = `token-scope-${i}`;
    // The name of the checkbox stays the permission; the description is read after it.
    const input = h("input", { type: "checkbox", id, value: scope, checked: scope === "read", "aria-describedby": `${id}-help` });
    return { scope, input, label: h("div", {}, h("label", { class: "check", for: id }, input, h("span", {}, scope)), h("p", { class: "hint", id: `${id}-help` }, SCOPE_HELP[scope])) };
  });
  const scopeError = h("p", { class: "field-error", id: "token-scope-error", role: "alert" });
  const expiry = h("select", { id: "api-token-expiry", name: "expires_days" }, ...[["30", "30 days"], ["90", "90 days"], ["365", "1 year"], ["0", "Never"]].map(([value, label]) => h("option", { value, selected: value === "90" }, label)));
  const allowedNetworks = field("api-token-networks", "Allowed client networks (optional)", "text", { autocomplete: "off", maxlength: 2048, placeholder: "192.0.2.0/24, 2001:db8::/32" });
  const allowedWorkers = h("select", { id: "api-token-workers", name: "allowed_workers", multiple: true, size: 4, "aria-label": "Allowed workers (optional)" });
  void api.get<{ workers: Array<{ name: string }> }>("/api/v1/workers").then(({ workers }) => allowedWorkers.replaceChildren(...workers.map((worker) => h("option", { value: worker.name }, worker.name)))).catch(() => {});
  const failure = h("p", { class: "form-error box", role: "alert" });
  const create = h("button", { type: "submit", class: "btn primary" }, "Create token");
  const close = (): void => dlg.close();
  const form = h(
    "form",
    { class: "token-create", id: "api-token-form", novalidate: true },
    h(
      "div",
      { class: "dialog-body" },
      name.wrap,
      h("fieldset", { "aria-describedby": "token-scope-error" }, h("legend", {}, "Permissions"), hint("Grant the least access the tool needs."), ...scopeInputs.map((x) => x.label)),
      scopeError,
      h("div", { class: "field" }, h("label", { for: "api-token-expiry" }, "Expires after"), expiry),
      allowedNetworks.wrap,
      hint("Leave blank to allow requests from any address. Separate CIDR ranges with commas."),
      h("div", { class: "field" }, h("label", { for: allowedWorkers.id }, "Restrict to workers (optional)"), allowedWorkers),
      hint("Leave no workers selected to allow every worker. Worker-bound tokens cannot use global diagnostics, audit, token, or low-level command endpoints."),
      hint("The secret appears once. Save it in your password manager; it cannot be shown again."),
      failure,
    ),
    h("div", { class: "actions end" }, btn("Cancel", close), create),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    failure.textContent = scopeError.textContent = "";
    name.setError("");
    const scopes = scopeInputs.filter((x) => x.input.checked).map((x) => x.scope);
    const tokenName = name.input.value.trim();
    if (!tokenName) {
      name.setError("Enter a name for the token.");
      name.input.focus();
      return;
    }
    if (!scopes.length) {
      scopeError.textContent = "Select at least one permission.";
      return;
    }
    create.disabled = true;
    try {
      const created = await api.post<api.ApiTokenCreated>("/api/v1/tokens", {
        name: tokenName, scopes, expires_days: Number(expiry.value),
        allowed_cidrs: allowedNetworks.input.value.split(",").map((value) => value.trim()).filter(Boolean),
        allowed_workers: Array.from(allowedWorkers.selectedOptions, (option) => option.value),
      });
      close();
      onCreated(created);
    } catch (err) {
      failure.textContent = apiMsg(err, "Could not create token.");
    } finally {
      create.disabled = false;
    }
  });
  const dlg = openPanel({ title: "Create an API token", id: "token-dialog", body: form });
  name.input.focus();
}

function tokenManagement(): HTMLElement {
  const status = h("p", { role: "status", "aria-live": "polite", class: "muted small" });
  const rows = h("ul", { class: "token-list", "aria-label": "API tokens" });
  const refresh = async (): Promise<void> => {
    try {
      const result = await api.get<{ tokens: api.ApiTokenInfo[] }>("/api/v1/tokens");
      rows.replaceChildren(...result.tokens.map((token) => {
        const revoke = async (): Promise<void> => {
          try {
            await api.del(`/api/v1/tokens/${encodeURIComponent(token.name)}`);
            status.textContent = `Token ${token.name} revoked.`;
            await refresh();
          } catch (err) {
            status.textContent = apiMsg(err, "Could not revoke token.");
          }
        };
        return h(
            "li",
            { "data-token": token.name },
            h("div", { class: "token-main" }, h("strong", { class: "nowrap" }, token.name), h("span", { class: "token-scopes" }, token.scopes.join(", ")), h("span", { class: "token-actions" }, menu({ label: `Actions for token ${token.name}`, kind: "ghost" }, [{ label: "Revoke token", danger: true, onSelect: () => void revoke() }]))),
            h(
              "div",
              { class: "token-meta muted small" },
              ...[
                `Expires ${token.expires_at ? isoTime(token.expires_at) : "never"}`,
                token.last_used ? `Last used ${isoTime(token.last_used)} from ${token.last_address ?? "unknown address"}` : "Not used yet",
                token.allowed_cidrs.length ? `Networks ${token.allowed_cidrs.join(", ")}` : "Any client network",
                token.allowed_workers.length ? `Workers ${token.allowed_workers.join(", ")}` : "All workers",
              ].map((text) => h("span", {}, text)),
            ),
          );
      }));
      if (!result.tokens.length) rows.append(h("li", { class: "muted token-none" }, "No API tokens yet."));
    } catch (err) {
      rows.replaceChildren(h("li", { class: "form-error", role: "alert" }, apiMsg(err, "Could not load API tokens.")));
    }
  };
  const showSecret = (created: api.ApiTokenCreated): void => {
    const copy = copyButton(created.secret, "token");
    const body = h("div", { class: "token-secret", "aria-live": "polite" }, h("p", {}, `Copy this secret for ${created.token.name}. It will not be shown again.`), h("div", { class: "secret" }, h("code", {}, created.secret), copy), h("div", { class: "actions end" }, btn("Done", () => dlg.close(), "btn primary")));
    const dlg = onceDialog({ id: "token-secret", title: "Token created", body, warning: "This secret is shown once. If you close this window without saving it, revoke the token and create a new one." });
    copy.focus();
    status.textContent = `Token ${created.token.name} created.`;
    void refresh();
  };
  const create = button("Create token", () => createTokenDialog(showSecret), { kind: "primary", icon: "plus" });
  void refresh();
  return block("api-tokens-title", h("div", { class: "block-head" }, h("h2", { id: "api-tokens-title" }, "API tokens"), create), h("p", { class: "muted" }, "Use tokens for scripts and tools. Grant only the permissions they need."), rows, status);
}

function passkeyReenrollment(): HTMLElement {
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
      const result = await api.post<{ origin: string }>("/api/v1/auth/passkeys/re-enroll", { current_password: current.input.value, new_origin: location.origin, confirm: confirm.checked });
      current.input.value = "";
      status.textContent = `Passkeys were removed. Register a new passkey for ${result.origin}.`;
    } catch (error) {
      status.textContent = apiMsg(error, "Could not reset passkeys.");
    } finally {
      submit.disabled = false;
    }
  });
  return block("passkey-reenroll-title", h("h2", { id: "passkey-reenroll-title" }, "Change passkey address"), form);
}

function passkeyList(): HTMLElement {
  const rows = h("ul", { class: "token-list", "aria-label": "Registered passkeys" });
  const status = h("p", { role: "status", "aria-live": "polite" }, "Loading passkeys…");
  const section = block("passkey-list-title", h("h2", { id: "passkey-list-title" }, "Passkeys"), h("p", { class: "muted" }, "Registered sign-in devices. Provider names are best-effort AAGUID labels; some passkeys do not identify a provider. Synced means the passkey provider backs it up across devices."), status, rows);
  void api.get<{ passkeys: Array<{ id: string; name: string; provider_aaguid: string | null; created_at: string; last_used: string | null; synced: boolean }> }>("/api/v1/auth/passkeys")
    .then(({ passkeys }) => {
      status.textContent = "";
      if (!passkeys.length) {
        rows.replaceChildren(h("li", { class: "muted" }, "No passkeys registered."));
        return;
      }
      rows.replaceChildren(...passkeys.map((passkey) => h("li", {},
        h("strong", {}, passkey.name),
        ...[
          `Provider ${passkeyProviderName(passkey.provider_aaguid)}`,
          passkey.synced ? "Synced passkey" : "Not reported as synced",
          `Added ${when(passkey.created_at)}`,
          passkey.last_used ? `Last used ${when(passkey.last_used)}` : "Not used yet",
        ].map((text) => h("span", {}, text)),
      )));
    })
    .catch((error) => { status.textContent = apiMsg(error, "Could not load passkeys."); });
  return section;
}

export function account(root: HTMLElement, opts: AccountOpts): () => void {
  const me = opts.me;
  const s = me?.session;
  const form = passwordForm({ me, onPasswordChanged: opts.onPasswordChanged });
  const exp = s ? new Date(s.expires_at * 1000) : null;
  const session = block(
    "acct-session",
    h("h2", { id: "acct-session" }, "This session"),
    facts("facts account-facts", "Session details", [
      ["Signed in as", me?.username],
      ["Device", s && (s.remember ? "Remembered" : "Not remembered (browser session)")],
      ["Signs out", exp && `${relFromMs(exp.getTime())} (${absTime(exp).slice(11)}) at the latest`],
      ["Idle limit", s && `${minutes(s.idle_timeout_secs)} without activity`],
    ]),
    h("div", { class: "actions" }, btn("Sign out", opts.signOut, "btn", { id: "account-signout" })),
  );
  const answers = answerLine({
    what: `Signed in as ${me?.username ?? "this account"}${s ? (s.remember ? " on a remembered device" : " for this browser session") : ""}.`,
    needMe: "Nothing needs you.",
    next: "Change the password or create an API token below if you need one.",
  });
  const passkeyGuidance = h("section", { id: "passkey-support-guidance", class: "panel account-block", hidden: true });
  void (async () => {
    const capability = await detectPasskeySupport();
    if (!capability.supported || !supportsPasskeysOnHost(window.location.hostname)) {
      passkeyGuidance.hidden = false;
      passkeyGuidance.replaceChildren(
        h("h2", {}, "Passkeys"),
        h("p", { class: "muted" }, "Passkeys need a trusted address such as Tailscale."),
        h("a", { class: "btn", href: installDocs("enable-passkeys-optional"), target: "_blank", rel: "noopener" }, "Enable passkeys (optional)"),
      );
    }
  })().catch(() => {});
  root.append(
    h("h1", {}, "Account"),
    answers.el,
    h("div", { class: "account-col" }, session, form, tokenManagement(), ...(me?.passkey_canonical_origin ? [passkeyList(), passkeyReenrollment()] : []), passkeyGuidance),
  );
  return () => {};
}
