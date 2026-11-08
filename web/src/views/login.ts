import * as api from "../api";
import { field, h, type Field } from "../dom";
import { icon, type IconName } from "../icons";
import { DEFAULT_POLICY, byteLength, requiredRules, strength } from "../password";
import { announce } from "../ui";
import { detectPasskeySupport, requestOptions, serializeAssertion } from "../passkeys";

export type NoticeKind = "expired" | "signed-out" | "changed" | "info";

/** Where the steps for a forgotten password are written down. */
export const installDocs = (anchor: string): string => `https://github.com/chrisgleissner/llm-tools/blob/main/docs/distributed/install.md#${anchor}`;
const RESET_DOCS_URL = installDocs("locked-out-or-forgot-the-password");

export interface LoginOpts {
  setupRequired: boolean;
  passkeySignInAvailable?: boolean;
  passkeyCanonicalOrigin?: string | null;
  policy?: api.PasswordPolicy;
  sessionPolicy?: api.AuthState["session_policy"];
  notice?: { kind: NoticeKind; text: string } | null;
  onDone: (welcome: string) => void;
}

const USERNAME_RE = /^[A-Za-z0-9_.@-]{1,64}$/;
const NO_NETWORK = "Cannot reach the coordinator. Check your network connection and try again.";
const PASSKEY_LABEL = "Sign in with a passkey";
const PASSKEY_WAIT = "Waiting for your passkey…";

export function minutes(secs: number): string {
  if (secs < 90) return `${Math.max(1, Math.round(secs))} s`;
  if (secs < 5400) return `${Math.round(secs / 60)} min`;
  const hrs = secs / 3600;
  return hrs < 48 ? `${hrs % 1 ? hrs.toFixed(1) : hrs} h` : `${Math.round(secs / 86400)} days`;
}

/** The text for a failed request: `known` maps an error code or an HTTP status to text, otherwise the server's own message. A 429 entry is a sentence that gets the retry delay appended. */
export function failure(err: unknown, known: Record<string | number, string> = {}, fallback = "Unexpected error. Try again."): string {
  if (!(err instanceof api.ApiError)) return fallback;
  const text = known[err.code] ?? known[err.status];
  return text === undefined ? err.message : err.status === 429 ? `${text} Try again in ${minutes(err.retryAfter ?? 60)}.` : text;
}

/** Show `message` as the field's error (empty clears it) and, when `focus` is set and there is an error, focus the field. True when the field is valid. */
export function check(f: Field, message: string, focus: boolean): boolean {
  f.setError(message);
  if (message && focus) f.input.focus();
  return !message;
}

/** Validate on blur, but not when the blur comes from pressing the submit button: that would insert an error message and move the button away from the pointer. */
const validateOnBlur = (f: Field, validate: (focus: boolean) => boolean): void =>
  f.input.addEventListener("blur", (e) => {
    if ((e.relatedTarget as HTMLButtonElement | null)?.type !== "submit" && f.input.value) validate(false);
  });

/** A button while its request is running: disabled and styled as busy. */
export function working(b: HTMLButtonElement, on: boolean): void {
  b.disabled = on;
  b.classList.toggle("busy", on);
}

export const errorBox = (id?: string, text?: string): HTMLElement => h("p", { class: "form-error box", role: "alert", id }, text);
export const submitButton = (label: string, id?: string): HTMLButtonElement => h("button", { type: "submit", class: "btn primary", id }, label);
/** A password field with its show / hide toggle. */
export function secret(id: string, label: string, autocomplete: string, what = "password", describedBy = ""): Field {
  const f = field(id, label, "password", { autocomplete, required: true }, "", describedBy);
  revealable(f, what);
  return f;
}

export function checkbox(id: string, text: string, name?: string): [HTMLInputElement, HTMLElement] {
  const input = h("input", { type: "checkbox", id, name });
  return [input, h("label", { class: "check", for: id }, input, h("span", {}, text))];
}

const userField = (label: string, hint = ""): Field => field("username", label, "text", { autocomplete: "username", required: true, autocapitalize: "none", spellcheck: "false" }, hint);
const head = (name: IconName, title: string): HTMLElement => h("div", { class: "auth-head" }, icon(name, 32), h("h1", {}, title));
const aside = (summary: string, id: string | undefined, ...hint: Array<Node | string>): HTMLElement => h("details", { class: "aside" }, h("summary", {}, summary), h("p", { class: "hint", id }, ...hint));

/** Wire a "show / hide" toggle to a password input. */
export function revealable(f: Field, what = "password"): void {
  const btn: HTMLButtonElement = h(
    "button",
    {
      type: "button",
      class: "btn ghost icon reveal",
      "aria-label": `Show ${what}`,
      "aria-pressed": "false",
      onclick: () => {
        const show = f.input.type === "password";
        f.input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", String(show));
        btn.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${what}`);
        btn.replaceChildren(icon(show ? "eyeOff" : "eye", 20));
      },
    },
    icon("eye", 20),
  );
  f.wrap.insertBefore(h("div", { class: "input-row" }, f.input, btn), f.wrap.querySelector(".hint"));
}

function capsWarning(f: Field): void {
  const w = h("p", { class: "caps-warning", id: `${f.input.id}-caps`, role: "status", hidden: true }, icon("caps", 18), "Caps Lock is on.");
  f.wrap.append(w);
  const sync = (e: KeyboardEvent) => {
    if (typeof e.getModifierState === "function") w.hidden = !e.getModifierState("CapsLock");
  };
  f.input.addEventListener("keydown", sync);
  f.input.addEventListener("keyup", sync);
  f.input.addEventListener("blur", () => (w.hidden = true));
}

export function authShell(root: HTMLElement, form: HTMLElement): void {
  root.replaceChildren(h("main", { id: "main", class: "auth", tabindex: "-1" }, form));
}

export function loginView(root: HTMLElement, opts: LoginOpts): void {
  if (opts.setupRequired) setupView(root, opts);
  else signInView(root, opts);
}

function noticeEl(n: LoginOpts["notice"]): HTMLElement | null {
  if (!n) return null;
  return h("p", { class: `notice ${n.kind === "expired" ? "" : n.kind === "changed" ? "ok" : "info"}`.trim(), role: "status", id: "login-notice", "data-kind": n.kind }, n.text);
}

// ------------------------------------------------------------------ sign in

function signInView(root: HTMLElement, opts: LoginOpts): void {
  const error = errorBox("form-error");
  const user = userField("Username");
  const pass = secret("password", "Password", "current-password");
  capsWarning(pass);
  const sp = opts.sessionPolicy;
  const [remember, rememberLabel] = checkbox("remember", "Remember this device", "remember");
  const progress = h("p", { class: "hint login-progress", id: "login-progress", role: "status", "aria-live": "polite", "aria-atomic": "true", hidden: true });
  const rememberHint = sp
    ? `Not remembered: signed out after ${minutes(sp.default.idle_secs)} without activity or ${minutes(sp.default.absolute_secs)} at most, and when the browser closes. Remembered: signed in for up to ${minutes(sp.remembered.absolute_secs)}, or until ${minutes(sp.remembered.idle_secs)} idle.`
    : "Leave this off on a shared computer.";
  const label = "Sign in";
  const submit = submitButton(label, "login-submit");
  const passkey = h("button", { type: "button", class: "btn", id: "passkey-login", hidden: true, onclick: () => void runPasskey(false) }, PASSKEY_LABEL);
  let canonical = "";
  try {
    const url = new URL((!opts.passkeySignInAvailable && opts.passkeyCanonicalOrigin) || "");
    if (url.protocol === "https:" && url.origin !== location.origin) canonical = url.origin;
  } catch {
    // Ignore an invalid origin; the password form remains available.
  }
  const form = h(
    "form",
    { class: "card auth-card", id: "login-form", novalidate: true },
    head("shield", label),
    h("p", { class: "muted" }, "LLM Bridge coordinator"),
    noticeEl(opts.notice),
    user.wrap,
    pass.wrap,
    rememberLabel,
    aside("What does remembering do?", "remember-hint", rememberHint),
    error,
    progress,
    h("div", { class: "actions login-actions" }, passkey, submit),
    canonical ? h("p", { class: "hint", id: "passkey-canonical-link" }, "Passkeys are available at ", h("a", { href: canonical }, "the canonical coordinator address")) : null,
    h(
      "div",
      { id: "forgot-password-hint" },
      h("p", { class: "hint" }, "Forgot the password? See ", h("a", { href: RESET_DOCS_URL, target: "_blank", rel: "noopener noreferrer" }, "Locked out or forgot the password"), "."),
      aside("Reset it from the host", undefined, "Over SSH on the coordinator host run ", h("code", {}, "llm-bridge-coordinator reset-password"), "."),
    ),
  );
  remember.setAttribute("aria-describedby", "remember-hint");
  let timer = 0;
  let blockedUntil = 0;
  let announcedAt = 0;
  let passkeyController: AbortController | null = null;
  let passkeyBusy = false;
  const showProgress = (text: string): void => {
    progress.textContent = text;
    progress.hidden = !text;
  };
  /** The button counts down every second; the message in the live region is rewritten only every 30 s and on a minute boundary, so a screen reader is not interrupted each second. */
  const tick = (): void => {
    const left = Math.ceil((blockedUntil - Date.now()) / 1000);
    if (left <= 0) {
      window.clearInterval(timer);
      error.textContent = "";
      error.setAttribute("role", "alert");
      submit.disabled = false;
      submit.textContent = label;
      return;
    }
    const minLeft = Math.floor(left / 60);
    if (announcedAt === 0 || left % 30 === 0) {
      announcedAt = left;
      error.textContent = `Too many failed sign-in attempts from this network. You can try again in ${minLeft ? `${minLeft} min ${left % 60} s` : `${left} s`} (at ${new Date(blockedUntil).toTimeString().slice(0, 8)}).`;
    }
    submit.textContent = `Try again in ${minLeft ? `${minLeft}:${String(left % 60).padStart(2, "0")}` : `${left} s`}`;
  };
  const resetProgress = (): void => {
    working(submit, false);
    working(passkey, false);
    submit.textContent = label;
    passkey.textContent = PASSKEY_LABEL;
    showProgress("");
    passkeyBusy = false;
  };
  const runPasskey = async (conditional: boolean, prepared?: api.PasskeyAssertionStart): Promise<void> => {
    if (!conditional) {
      if (passkeyBusy) return;
      passkeyBusy = true;
      working(passkey, true);
      submit.disabled = true;
      passkey.textContent = PASSKEY_WAIT;
      showProgress(PASSKEY_WAIT);
    }
    passkeyController?.abort();
    const controller = new AbortController();
    passkeyController = controller;
    try {
            const start = prepared ?? await api.beginPasskeySignIn();
      const credential = await navigator.credentials.get({
        publicKey: requestOptions(start.options),
        mediation: conditional ? "conditional" : "required",
        signal: controller.signal,
      });
      if (!credential) return;
      await api.finishPasskeySignIn(start.challenge_id, serializeAssertion(credential));
      opts.onDone("Signed in with a passkey.");
    } catch (err) {
      const denied = err instanceof DOMException && err.name === "NotAllowedError";
      if (controller.signal.aborted || (conditional && denied)) return;
      error.textContent = denied
        ? "No passkey was selected. Try again or use your password."
        : failure(err, { 401: "That passkey could not sign you in. Try again or use your password.", 0: NO_NETWORK }, "Passkey sign-in did not complete. Try again or use your password.");
    } finally {
      if (passkeyController === controller) passkeyController = null;
      if (!conditional) resetProgress();
    }
  };
  form.addEventListener("submit", async (e) => {
    passkeyController?.abort();
    e.preventDefault();
    error.textContent = "";
    pass.setError("");
    if (!check(user, user.input.value.trim() ? "" : "Enter your username.", true) || !check(pass, pass.input.value ? "" : "Enter your password.", true)) return;
    working(submit, true);
    submit.textContent = "Signing in…";
    showProgress("Signing in…");
    try {
      await api.login(user.input.value, pass.input.value, remember.checked);
      opts.onDone(`Signed in as ${user.input.value}.`);
    } catch (err) {
      submit.classList.remove("busy");
      showProgress("");
      if (err instanceof api.ApiError && err.status === 429) {
        blockedUntil = Date.now() + (err.retryAfter ?? 60) * 1000;
        announcedAt = 0;
        error.setAttribute("role", "status");
        tick();
        window.clearInterval(timer);
        timer = window.setInterval(tick, 1000);
        announce("Sign-in is temporarily blocked.", "error");
        return;
      }
      submit.disabled = false;
      submit.textContent = label;
      error.setAttribute("role", "alert");
      error.textContent = failure(err, { 401: "Username or password is incorrect. Check both and try again.", 0: NO_NETWORK });
      error.scrollIntoView?.({ block: "nearest" });
      pass.input.focus();
      pass.input.select();
    }
  });
  authShell(root, form);
  user.input.focus();
  if (opts.passkeySignInAvailable) {
    void (async () => {
      try {
                const capability = await detectPasskeySupport();
        if (!capability.supported || !form.isConnected) return;
        passkey.hidden = false;
        if (capability.platformAuthenticator) passkey.dataset.platformAuthenticator = "available";
        if (!capability.conditional) return;
        const start = await api.beginPasskeySignIn();
        // Conditional UI requires discoverable credentials. Until the server advertises them,
        // keep the normal button available and leave username autocomplete unchanged.
        if (requestOptions(start.options).allowCredentials?.length) return;
        user.input.setAttribute("autocomplete", "username webauthn");
        void runPasskey(true, start);
      } catch {
        // The password form stays usable if capability probing or challenge creation fails.
      }
    })();
  }
}

// ------------------------------------------------------------------ first-run setup

function setupView(root: HTMLElement, opts: LoginOpts): void {
  const error = errorBox("form-error");
  const token = field("setup-token", "Setup token", "password", { autocomplete: "off", required: true, spellcheck: "false" }, "Proves that you control the machine that runs the coordinator. It works once.");
  const user = userField("Administrator username", "Letters, digits and . _ - @ (1 to 64 characters).");
  const np = newPasswordFields(opts.policy ?? DEFAULT_POLICY, () => user.input.value, "password", "Repeat password");
  const { pass } = np;
  const submit = submitButton("Create administrator", "setup-submit");
  const help = h(
    "details",
    { class: "notice info aside", id: "token-help" },
    h("summary", {}, "Where do I find the setup token?"),
    h(
      "ol",
      { class: "steps" },
      h("li", {}, "On the machine that runs the coordinator, run ", h("code", {}, "bridge-coordinator setup-token"), "."),
      h("li", {}, "Or read the file ", h("code", {}, "setup-token"), " in the coordinator data directory. In a container: ", h("code", {}, "docker compose cp coordinator:/data/setup-token ."), "."),
      h("li", {}, "The token is deleted when the administrator is created."),
    ),
  );

  function validateToken(focus: boolean): boolean {
    const ok = check(token, token.input.value.trim() ? "" : "Enter the setup token. Open \"Where do I find the setup token?\" above if you need help.", focus);
    if (!ok) help.open = true;
    return ok;
  }
  function validateUser(focus: boolean): boolean {
    const v = user.input.value;
    return check(user, !v ? "Choose a username." : !USERNAME_RE.test(v) ? "Use only letters, digits and . _ - @, up to 64 characters." : "", focus);
  }
  validateOnBlur(token, validateToken);
  validateOnBlur(user, validateUser);

  const form = h(
    "form",
    { class: "card auth-card", id: "setup-form", novalidate: true },
    head("shield", "First-run setup"),
    h("p", {}, "This coordinator has no administrator yet. Create one to start using it."),
    noticeEl(opts.notice),
    help,
    token.wrap,
    user.wrap,
    ...np.nodes,
    error,
    h("div", { class: "actions" }, submit),
  );
  user.input.addEventListener("input", np.repaint);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    let clean = true;
    for (const validate of [validateToken, validateUser, np.validatePassword, np.validateConfirm]) if (!validate(clean)) clean = false;
    if (!clean) {
      error.textContent = "Fix the highlighted fields and try again.";
      return;
    }
    working(submit, true);
    try {
      await api.setup(token.input.value.trim(), user.input.value, pass.input.value);
    } catch (err) {
      working(submit, false);
      const code = err instanceof api.ApiError ? err.code : "";
      const rejected = (message: string): string => message.charAt(0).toUpperCase() + message.slice(1) + ".";
      if (code === "invalid-setup-token") {
        check(token, "This is not the setup token. Copy it again from the file or command described above.", true);
        help.open = true;
      } else if (code === "invalid-password" || code === "invalid-username") check(code === "invalid-password" ? pass : user, rejected((err as Error).message), true);
      else error.textContent = failure(err, { 429: "Too many wrong setup tokens from this network.", "setup-complete": "An administrator already exists. Reload the page to sign in." });
      return;
    }
    // Success: show the result, then sign in with the password just chosen.
    const name = user.input.value;
    form.replaceChildren(head("checkCircle", "Administrator created"), h("p", { role: "status", id: "setup-success" }, `The account "${name}" exists and the setup token is used up. Signing you in...`));
    try {
      await api.login(name, pass.input.value, false);
      opts.onDone(`Setup complete. You are signed in as ${name}.`);
    } catch {
      form.append(errorBox(undefined, "The account was created, but signing in failed. Reload the page and sign in."));
    }
  });
  authShell(root, form);
  token.input.focus();
}

export interface NewPasswordFields {
  pass: Field;
  confirm: Field;
  nodes: HTMLElement[];
  repaint: () => void;
  validatePassword: (focus: boolean) => boolean;
  validateConfirm: (focus: boolean) => boolean;
}

/** New-password and repeat fields (ids `passId` and `passId-confirm`) with show/hide, the server's rules, a strength estimate and inline validation. */
export function newPasswordFields(policy: api.PasswordPolicy, username: () => string, passId: string, confirmLabel: string): NewPasswordFields {
  const pass = secret(passId, "New password", "new-password", "password", "strength-text strength-tip policy-list");
  const confirm = secret(`${passId}-confirm`, confirmLabel, "new-password", "repeated password");
  capsWarning(pass);
  const meter = h("div", { class: "meter", "data-level": "0", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"));
  const meterText = h("p", { class: "hint", id: "strength-text", role: "status" });
  const rules = h("ul", { class: "policy", id: "policy-list", "aria-label": "Password rules the server enforces" });
  const tipEl = h("p", { class: "hint", id: "strength-tip" });
  pass.wrap.insertBefore(h("div", {}, meter, meterText, tipEl, h("p", { class: "label small", style: "margin-top:12px" }, "Required by the server"), rules), pass.wrap.querySelector(".field-error"));
  const repaint = (): void => {
    const pw = pass.input.value;
    const n = byteLength(pw);
    rules.replaceChildren(...requiredRules(pw, policy).map((r) => h("li", { class: r.met ? "met" : "", "data-rule": r.id }, icon(r.met ? "checkCircle" : "info", 16), h("span", {}, r.text), h("span", { class: "sr-only" }, ` (${r.met ? "met" : "not met"})`))));
    const s = strength(pw, username(), policy);
    meter.dataset["level"] = String(s.level);
    meterText.textContent = pw ? `Strength: ${s.label}${n < policy.min_length ? ` (${n} of ${policy.min_length} characters)` : ""}` : "";
    tipEl.textContent = s.tip + (pw ? " This is an estimate and is not enforced." : "");
  };
  const validatePassword = (focus: boolean): boolean => {
    const bad = requiredRules(pass.input.value, policy).find((r) => !r.met);
    return check(pass, bad ? `The password does not meet the rule: ${bad.text.toLowerCase()}.` : "", focus);
  };
  const validateConfirm = (focus: boolean): boolean => check(confirm, confirm.input.value !== pass.input.value ? "The two passwords do not match." : "", focus);
  pass.input.addEventListener("input", () => {
    repaint();
    if (pass.input.getAttribute("aria-invalid")) validatePassword(false);
    if (confirm.input.value) validateConfirm(false);
  });
  validateOnBlur(pass, validatePassword);
  validateOnBlur(confirm, validateConfirm);
  confirm.input.addEventListener("input", () => confirm.input.getAttribute("aria-invalid") && validateConfirm(false));
  repaint();
  return { pass, confirm, nodes: [pass.wrap, confirm.wrap], repaint, validatePassword, validateConfirm };
}
