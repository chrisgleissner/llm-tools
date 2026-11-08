import * as api from "../api";
import { field, h, relFromMs, timeEl } from "../dom";
import { passkeyProviderName } from "../passkey-provider";
import { withStepUp } from "../step-up";
import { minutes } from "./login";
import { passwordForm } from "./password-form";

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
  const allowedWorkers = h("select", { id: "api-token-workers", name: "allowed_workers", multiple: true, size: 4, "aria-label": "Allowed workers (optional)" });
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
    h("label", { for: allowedWorkers.id }, "Restrict to workers (optional)"),
    allowedWorkers,
    h("p", { class: "hint" }, "Leave no workers selected to allow every worker. Worker-bound tokens cannot use global diagnostics, audit, token, or low-level command endpoints."),
    h("p", { class: "hint" }, "The secret appears once. Save it in your password manager; it cannot be shown again."),
    status,
    create,
  );
  const refresh = async (): Promise<void> => {
    try {
      const result = await api.get<{ tokens: api.ApiTokenInfo[] }>("/api/v1/tokens");
      const workers = await api.get<{ workers: Array<{ name: string }> }>("/api/v1/workers");
      allowedWorkers.replaceChildren(...workers.workers.map((worker) => h("option", { value: worker.name }, worker.name)));
      rows.replaceChildren();
      for (const token of result.tokens) {
        const revoke = h("button", { class: "btn small", type: "button", "aria-label": `Revoke ${token.name}` }, "Revoke");
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          try {
            await withStepUp(() => api.del(`/api/v1/tokens/${encodeURIComponent(token.name)}`));
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
        rows.append(h("li", {}, h("strong", {}, token.name), h("span", {}, token.scopes.join(", ")), h("span", {}, `Expires ${token.expires_at ? new Date(token.expires_at).toLocaleString() : "never"}`), h("span", {}, token.allowed_cidrs.length ? `Networks ${token.allowed_cidrs.join(", ")}` : "Any client network"), h("span", {}, token.allowed_workers.length ? `Workers ${token.allowed_workers.join(", ")}` : "All workers"), h("span", {}, lastUsed), revoke));
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
      const created = await withStepUp(() => api.post<api.ApiTokenCreated>("/api/v1/tokens", {
        name: name.input.value.trim(), scopes, expires_days: Number(expiry.value),
        allowed_cidrs: allowedNetworks.input.value.split(",").map((value) => value.trim()).filter(Boolean),
        allowed_workers: Array.from(allowedWorkers.selectedOptions, (option) => option.value),
      }));
      oneTime.hidden = false;
      oneTime.replaceChildren(
        h("p", {}, `Copy this secret for ${created.token.name}. It will not be shown again.`),
        h("pre", {}, h("code", {}, created.secret)),
        h("button", { type: "button", class: "btn", "data-copy-token": "" }, "Copy token"),
      );
      status.textContent = `Token ${created.token.name} created.`;
      name.input.value = "";
      allowedNetworks.input.value = "";
      Array.from(allowedWorkers.options).forEach((option) => { option.selected = false; });
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

function activeSessions(): HTMLElement {
  const section = h("section", { class: "card narrow", "aria-labelledby": "active-sessions-title" });
  const status = h("p", { role: "status", "aria-live": "polite" }, "Loading sessions…");
  const rows = h("ul", { class: "token-list", "aria-label": "Active sessions" });
  const refresh = async (): Promise<void> => {
    try {
      const result = await api.get<{ sessions: Array<{ id: string; created_at: string; last_seen: string; expires_at: string; remember: boolean; current: boolean }> }>("/api/v1/auth/sessions");
      rows.replaceChildren(...result.sessions.map((session) => {
        const created = new Date(session.created_at).toLocaleString();
        const revoke = h("button", {
          class: "btn small", type: "button",
          "aria-label": session.current ? "Current session" : `Revoke session created ${created}`,
        }, session.current ? "Current session" : "Revoke");
        revoke.disabled = session.current;
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          try {
            await api.del(`/api/v1/auth/sessions/${encodeURIComponent(session.id)}`);
            window.dispatchEvent(new Event("bridge-security-notification"));
            status.textContent = "Session revoked.";
            await refresh();
          } catch (error) {
            revoke.disabled = false;
            status.textContent = error instanceof api.ApiError ? error.message : "Could not revoke session.";
          }
        });
        return h("li", {}, h("strong", {}, session.current ? "This device" : "Other device"),
          h("span", {}, session.remember ? "Remembered device" : "Browser session"),
          h("span", {}, `Signed in ${created}`),
          h("span", {}, `Last active ${new Date(session.last_seen).toLocaleString()}`),
          h("span", {}, `Expires ${new Date(session.expires_at).toLocaleString()}`), revoke);
      }));
      if (!result.sessions.length) rows.append(h("li", { class: "muted" }, "No active sessions."));
      status.textContent = "";
    } catch (error) {
      status.textContent = error instanceof api.ApiError ? error.message : "Could not load sessions.";
    }
  };
  section.append(h("h2", { id: "active-sessions-title" }, "Active sessions"),
    h("p", { class: "muted" }, "Review signed-in browsers and revoke sessions you no longer use. The current session cannot revoke itself here; use Sign out."), status, rows);
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

function securityActivity(): { element: HTMLElement; dispose: () => void } {
  const section = h("section", { class: "card narrow", "aria-labelledby": "security-activity-title" });
  const status = h("p", { role: "status", "aria-live": "polite" }, "Loading security activity…");
  const chain = h("p", { class: "muted small", "aria-live": "polite" });
  const rows = h("ul", { class: "token-list", "aria-label": "Security activity" });
  const older = h("button", { class: "btn", type: "button", hidden: true }, "Load older activity");
  let cursor: string | null = null;
  let loading = false;
  let reloadAfterLoad = false;
  const load = async (next: string | null, append: boolean): Promise<void> => {
    if (loading) return;
    loading = true;
    status.textContent = "Loading security activity…";
    older.disabled = true;
    try {
      const query = new URLSearchParams({ limit: "50" });
      if (next) query.set("cursor", next);
      const page = await api.get<{
        entries: Array<{ id: number; ts: string; actor: string; action: string; remote: string }>;
        chain_valid: boolean;
        next_cursor: string | null;
      }>(`/api/v1/audit?${query}`);
      const items = page.entries.map((entry) => h("li", {},
        h("strong", {}, entry.action),
        h("span", {}, `By ${entry.actor}`),
        h("span", {}, entry.remote ? `From ${entry.remote}` : "Address unavailable"),
        h("time", { datetime: entry.ts }, new Date(entry.ts).toLocaleString()),
      ));
      if (append) rows.append(...items);
      else rows.replaceChildren(...items);
      if (!rows.childElementCount) rows.append(h("li", { class: "muted" }, "No security activity yet."));
      cursor = page.next_cursor;
      older.hidden = !cursor;
      chain.textContent = page.chain_valid ? "Audit integrity verified." : "Audit integrity check failed.";
      chain.className = page.chain_valid ? "muted small" : "form-error";
      status.textContent = "";
    } catch (err) {
      status.textContent = err instanceof api.ApiError ? err.message : "Could not load security activity.";
    } finally {
      loading = false;
      older.disabled = false;
      if (reloadAfterLoad) {
        reloadAfterLoad = false;
        void load(null, false);
      }
    }
  };
  older.addEventListener("click", () => { void load(cursor, true); });
  const events = new AbortController();
  window.addEventListener("bridge-security-notification", () => {
    if (loading) reloadAfterLoad = true;
    else void load(null, false);
  }, { signal: events.signal });
  section.append(h("h2", { id: "security-activity-title" }, "Security activity"),
    h("p", { class: "muted" }, "Recent sign-ins, token activity and security changes. Sensitive request details and credential material are not shown."),
    chain, status, rows, older);
  void load(null, false);
  return { element: section, dispose: () => events.abort() };
}

export function account(root: HTMLElement, opts: AccountOpts): () => void {
  const me = opts.me;
  const s = me?.session;
  const form = passwordForm({ me, onPasswordChanged: opts.onPasswordChanged });
  const exp = s ? new Date(s.expires_at) : null;
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
  const activity = securityActivity();
  root.append(h("h1", {}, "Account"), h("div", { class: "grid-2" }, session, form), activeSessions(), tokenManagement(),
    activity.element, me?.passkey_canonical_origin ? passkeyList() : h("div", {}),
    me?.passkey_canonical_origin ? passkeyReenrollment() : h("div", {}), passkeyGuidance);
  return activity.dispose;
}
