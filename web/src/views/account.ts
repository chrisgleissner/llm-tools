import * as api from "../api";
import { absTime, button, copyButton, field, h, isoTime, relFromMs } from "../dom";
import { menu } from "../menu";
import { DEFAULT_POLICY } from "../password";
import { announce, openPanel } from "../ui";
import { answerLine, onceDialog } from "./remote";
import { minutes, newPasswordFields, revealable } from "./login";

export interface AccountOpts {
  me: api.AuthState | null;
  signOut: () => void;
  onPasswordChanged: () => void;
}

const SCOPE_NAMES = ["read", "operate", "terminal:view", "terminal:write", "admin"] as const;
/** What each permission allows, in a few words; the last two are powerful and say so. */
const SCOPE_HELP: Record<(typeof SCOPE_NAMES)[number], string> = {
  read: "See missions and usage.",
  operate: "Start, pause, stop missions.",
  "terminal:view": "Watch live terminals.",
  "terminal:write": "Type into terminals. Powerful.",
  admin: "Manage workers and tokens. Powerful.",
};

/** The dialog that creates a token. It closes itself and hands the secret to `onCreated`, which shows it once. */
function createTokenDialog(onCreated: (created: api.ApiTokenCreated) => void): void {
  const name = field("api-token-name", "Token name", "text", { autocomplete: "off", maxlength: 63, required: true });
  const scopeInputs = SCOPE_NAMES.map((scope, i) => {
    const input = h("input", { type: "checkbox", id: `token-scope-${i}`, value: scope, checked: scope === "read" });
    input.setAttribute("aria-describedby", `${input.id}-help`);
    // The name of the checkbox stays the permission; the description is read after it.
    return { scope, input, label: h("div", {}, h("label", { class: "check", for: input.id }, input, h("span", {}, scope)), h("p", { class: "hint", id: `${input.id}-help` }, SCOPE_HELP[scope])) };
  });
  const scopeError = h("p", { class: "field-error", id: "token-scope-error", role: "alert" });
  const expiry = h("select", { id: "api-token-expiry", name: "expires_days" }, h("option", { value: "30" }, "30 days"), h("option", { value: "90", selected: true }, "90 days"), h("option", { value: "365" }, "1 year"), h("option", { value: "0" }, "Never"));
  const failure = h("p", { class: "form-error box", role: "alert" });
  const create = h("button", { type: "submit", class: "btn primary" }, "Create token");
  const form = h(
    "form",
    { class: "token-create", id: "api-token-form", novalidate: true },
    h(
      "div",
      { class: "dialog-body" },
      name.wrap,
      h("fieldset", { "aria-describedby": "token-scope-error" }, h("legend", {}, "Permissions"), h("p", { class: "hint" }, "Grant the least access the tool needs."), ...scopeInputs.map((x) => x.label)),
      scopeError,
      h("div", { class: "field" }, h("label", { for: "api-token-expiry" }, "Expires after"), expiry),
      h("p", { class: "hint" }, "The secret appears once. Save it in your password manager; it cannot be shown again."),
      failure,
    ),
    h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => (document.getElementById("token-dialog") as HTMLDialogElement | null)?.close() }, "Cancel"), create),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    failure.textContent = "";
    scopeError.textContent = "";
    name.setError("");
    const scopes = scopeInputs.filter((x) => x.input.checked).map((x) => x.scope);
    if (!name.input.value.trim()) {
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
      const created = await api.post<api.ApiTokenCreated>("/api/v1/tokens", { name: name.input.value.trim(), scopes, expires_days: Number(expiry.value) });
      (document.getElementById("token-dialog") as HTMLDialogElement | null)?.close();
      onCreated(created);
    } catch (err) {
      failure.textContent = err instanceof api.ApiError ? err.message : "Could not create token.";
    } finally {
      create.disabled = false;
    }
  });
  openPanel({ title: "Create an API token", id: "token-dialog", body: form });
  name.input.focus();
}

function tokenManagement(): HTMLElement {
  const section = h("section", { class: "panel account-block", "aria-labelledby": "api-tokens-title" });
  const status = h("p", { role: "status", "aria-live": "polite", class: "muted small" });
  const rows = h("ul", { class: "token-list", "aria-label": "API tokens" });
  const refresh = async (): Promise<void> => {
    try {
      const result = await api.get<{ tokens: api.ApiTokenInfo[] }>("/api/v1/tokens");
      rows.replaceChildren();
      for (const token of result.tokens) {
        const revoke = async (): Promise<void> => {
          try {
            await api.del(`/api/v1/tokens/${encodeURIComponent(token.name)}`);
            status.textContent = `Token ${token.name} revoked.`;
            await refresh();
          } catch (err) {
            status.textContent = err instanceof api.ApiError ? err.message : "Could not revoke token.";
          }
        };
        rows.append(
          h(
            "li",
            { "data-token": token.name },
            h("div", { class: "token-main" }, h("strong", { class: "nowrap" }, token.name), h("span", { class: "token-scopes" }, token.scopes.join(", ")), h("span", { class: "token-actions" }, menu({ label: `Actions for token ${token.name}`, kind: "ghost" }, [{ label: "Revoke token", danger: true, onSelect: () => void revoke() }]))),
            h("div", { class: "token-meta muted small" }, h("span", {}, `Expires ${token.expires_at ? isoTime(token.expires_at) : "never"}`), h("span", {}, `Last used ${token.last_used ? isoTime(token.last_used) : "never"}`)),
          ),
        );
      }
      if (!result.tokens.length) rows.append(h("li", { class: "muted token-none" }, "No API tokens yet."));
    } catch (err) {
      rows.replaceChildren(h("li", { class: "form-error", role: "alert" }, err instanceof api.ApiError ? err.message : "Could not load API tokens."));
    }
  };
  const showSecret = (created: api.ApiTokenCreated): void => {
    const done = h("button", { type: "button", class: "btn primary" }, "Done");
    const copy = copyButton(created.secret, "token");
    const body = h("div", { class: "token-secret", "aria-live": "polite" }, h("p", {}, `Copy this secret for ${created.token.name}. It will not be shown again.`), h("div", { class: "secret" }, h("code", {}, created.secret), copy), h("div", { class: "actions end" }, done));
    const dlg = onceDialog({ id: "token-secret", title: "Token created", body, warning: "This secret is shown once. If you close this window without saving it, revoke the token and create a new one." });
    done.addEventListener("click", () => dlg.close());
    copy.focus();
    status.textContent = `Token ${created.token.name} created.`;
    void refresh();
  };
  const create = button("Create token", () => createTokenDialog(showSecret), { kind: "primary", icon: "plus" });
  section.append(h("div", { class: "block-head" }, h("h2", { id: "api-tokens-title" }, "API tokens"), create), h("p", { class: "muted" }, "Use tokens for scripts and tools. Grant only the permissions they need."), rows, status);
  void refresh();
  return section;
}

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

export function account(root: HTMLElement, opts: AccountOpts): () => void {
  const me = opts.me;
  const s = me?.session;
  const form = passwordForm({ me, onPasswordChanged: opts.onPasswordChanged });
  const exp = s ? new Date(s.expires_at * 1000) : null;
  const session = h(
    "section",
    { class: "panel account-block", "aria-labelledby": "acct-session" },
    h("h2", { id: "acct-session" }, "This session"),
    h(
      "dl",
      { class: "facts account-facts", "aria-label": "Session details" },
      h("dt", {}, "Signed in as"),
      h("dd", {}, me?.username ?? "-"),
      h("dt", {}, "Device"),
      h("dd", {}, s ? (s.remember ? "Remembered" : "Not remembered (browser session)") : "-"),
      h("dt", {}, "Signs out"),
      h("dd", {}, exp ? `${relFromMs(exp.getTime())} (${absTime(exp).slice(11)}) at the latest` : "-"),
      h("dt", {}, "Idle limit"),
      h("dd", {}, s ? `${minutes(s.idle_timeout_secs)} without activity` : "-"),
    ),
    h("div", { class: "actions" }, h("button", { type: "button", class: "btn", id: "account-signout", onclick: opts.signOut }, "Sign out")),
  );
  const answers = answerLine({
    what: `Signed in as ${me?.username ?? "this account"}${s ? (s.remember ? " on a remembered device" : " for this browser session") : ""}.`,
    needMe: "Nothing needs you.",
    next: "Change the password or create an API token below if you need one.",
  });
  root.append(h("h1", {}, "Account"), answers.el, h("div", { class: "account-col" }, session, form, tokenManagement()));
  return () => {};
}
