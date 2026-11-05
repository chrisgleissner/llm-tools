import * as api from "../api";
import { field, h, type Field } from "../dom";
import { icon } from "../icons";
import { DEFAULT_POLICY, byteLength, requiredRules, strength } from "../password";
import { announce } from "../ui";

export type NoticeKind = "expired" | "signed-out" | "changed" | "info";

export interface LoginOpts {
  setupRequired: boolean;
  policy?: api.PasswordPolicy;
  sessionPolicy?: api.AuthState["session_policy"];
  notice?: { kind: NoticeKind; text: string } | null;
  onDone: (welcome: string) => void;
}

const USERNAME_RE = /^[A-Za-z0-9_.@-]{1,64}$/;

export function minutes(secs: number): string {
  if (secs < 90) return `${Math.max(1, Math.round(secs))} s`;
  if (secs < 5400) return `${Math.round(secs / 60)} min`;
  const hrs = secs / 3600;
  return hrs < 48 ? `${Number.isInteger(hrs) ? hrs : hrs.toFixed(1)} h` : `${Math.round(secs / 86400)} days`;
}

/** Wire a "show / hide" toggle to a password input. */
export function revealable(f: Field, what = "password"): HTMLElement {
  const btn = h("button", { type: "button", class: "btn ghost icon reveal", "aria-label": `Show ${what}`, "aria-pressed": "false" }, icon("eye", 20));
  btn.addEventListener("click", () => {
    const show = f.input.type === "password";
    f.input.type = show ? "text" : "password";
    btn.setAttribute("aria-pressed", String(show));
    btn.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${what}`);
    btn.replaceChildren(icon(show ? "eyeOff" : "eye", 20));
  });
  const row = h("div", { class: "input-row" }, f.input, btn);
  f.wrap.insertBefore(row, f.wrap.querySelector(".hint"));
  return row;
}

function capsWarning(f: Field): void {
  const w = h("p", { class: "caps-warning", id: `${f.input.id}-caps`, role: "status", hidden: true }, icon("caps", 18), "Caps Lock is on.");
  f.wrap.append(w);
  const check = (e: KeyboardEvent) => {
    if (typeof e.getModifierState === "function") w.hidden = !e.getModifierState("CapsLock");
  };
  f.input.addEventListener("keydown", check);
  f.input.addEventListener("keyup", check);
  f.input.addEventListener("blur", () => (w.hidden = true));
}

function authShell(root: HTMLElement, form: HTMLElement): void {
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
  const error = h("p", { class: "form-error box", role: "alert", id: "form-error" });
  const user = field("username", "Username", "text", { autocomplete: "username", required: true, autocapitalize: "none", spellcheck: "false" });
  const pass = field("password", "Password", "password", { autocomplete: "current-password", required: true });
  revealable(pass);
  capsWarning(pass);
  const sp = opts.sessionPolicy;
  const remember = h("input", { type: "checkbox", id: "remember", name: "remember" });
  const rememberHint = sp
    ? `Leave this off on a shared computer: you are signed out after ${minutes(sp.default.idle_secs)} without activity or ${minutes(sp.default.absolute_secs)} at most, and when the browser closes. On, you stay signed in for up to ${minutes(sp.remembered.absolute_secs)}, until ${minutes(sp.remembered.idle_secs)} pass without activity.`
    : "Leave this off on a shared computer.";
  const submit = h("button", { type: "submit", class: "btn primary", id: "login-submit" }, "Sign in");
  const form = h(
    "form",
    { class: "card auth-card", id: "login-form", novalidate: true },
    h("div", { class: "auth-head" }, icon("shield", 32), h("h1", {}, "Sign in")),
    h("p", { class: "muted" }, "LLM Bridge coordinator"),
    noticeEl(opts.notice),
    user.wrap,
    pass.wrap,
    h("label", { class: "check", for: "remember" }, remember, h("span", {}, "Remember this device")),
    h("p", { class: "hint", id: "remember-hint" }, rememberHint),
    error,
    h("div", { class: "actions" }, submit),
  );
  remember.setAttribute("aria-describedby", "remember-hint");
  let timer = 0;
  let blockedUntil = 0;
  const label = "Sign in";
  const tick = (): void => {
    const left = Math.ceil((blockedUntil - Date.now()) / 1000);
    if (left <= 0) {
      window.clearInterval(timer);
      error.textContent = "";
      submit.disabled = false;
      submit.textContent = label;
      return;
    }
    const at = new Date(blockedUntil);
    error.textContent = `Too many failed sign-in attempts from this network. You can try again in ${left >= 60 ? `${Math.floor(left / 60)} min ${left % 60} s` : `${left} s`} (at ${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}:${String(at.getSeconds()).padStart(2, "0")}).`;
    submit.textContent = `Try again in ${left >= 60 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : `${left} s`}`;
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    user.setError("");
    pass.setError("");
    if (!user.input.value.trim()) {
      user.setError("Enter your username.");
      user.input.focus();
      return;
    }
    if (!pass.input.value) {
      pass.setError("Enter your password.");
      pass.input.focus();
      return;
    }
    submit.disabled = true;
    submit.classList.add("busy");
    try {
      await api.login(user.input.value, pass.input.value, remember.checked);
      opts.onDone(`Signed in as ${user.input.value}.`);
    } catch (err) {
      submit.classList.remove("busy");
      if (err instanceof api.ApiError && err.status === 429) {
        blockedUntil = Date.now() + (err.retryAfter ?? 60) * 1000;
        tick();
        window.clearInterval(timer);
        timer = window.setInterval(tick, 1000);
        announce("Sign-in is temporarily blocked.", "error");
        return;
      }
      submit.disabled = false;
      if (err instanceof api.ApiError && err.status === 401) error.textContent = "The username or password is not correct. Check both and try again.";
      else if (err instanceof api.ApiError && err.status === 0) error.textContent = "Cannot reach the coordinator. Check your network connection and try again.";
      else error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error. Try again.";
      pass.input.select();
    }
  });
  authShell(root, form);
  user.input.focus();
}

// ------------------------------------------------------------------ first-run setup

function setupView(root: HTMLElement, opts: LoginOpts): void {
  const policy = opts.policy ?? DEFAULT_POLICY;
  const error = h("p", { class: "form-error box", role: "alert", id: "form-error" });
  const token = field("setup-token", "Setup token", "password", { autocomplete: "off", required: true, spellcheck: "false" }, "Proves that you control the machine that runs the coordinator. It works once.");
  const user = field("username", "Administrator username", "text", { autocomplete: "username", required: true, autocapitalize: "none", spellcheck: "false" }, "Letters, digits and . _ - @ (1 to 64 characters).");
  const np = newPasswordFields(policy, () => user.input.value, { passId: "password", passLabel: "New password", confirmId: "password-confirm", confirmLabel: "Repeat password" });
  const { pass, confirm } = np;
  const submit = h("button", { type: "submit", class: "btn primary", id: "setup-submit" }, "Create administrator");

  function validateToken(focus: boolean): boolean {
    const bad = !token.input.value.trim();
    token.setError(bad ? "Enter the setup token. See the steps above for where to find it." : "");
    if (bad && focus) token.input.focus();
    return !bad;
  }
  function validateUser(focus: boolean): boolean {
    const v = user.input.value;
    const msg = !v ? "Choose a username." : !USERNAME_RE.test(v) ? "Use only letters, digits and . _ - @, up to 64 characters." : "";
    user.setError(msg);
    if (msg && focus) user.input.focus();
    return !msg;
  }
  const notSubmit = (e: FocusEvent): boolean => (e.relatedTarget as HTMLButtonElement | null)?.type !== "submit";
  token.input.addEventListener("blur", (e) => notSubmit(e) && token.input.value && validateToken(false));
  user.input.addEventListener("blur", (e) => notSubmit(e) && user.input.value && validateUser(false));

  const form = h(
    "form",
    { class: "card auth-card", id: "setup-form", novalidate: true },
    h("div", { class: "auth-head" }, icon("shield", 32), h("h1", {}, "First-run setup")),
    h("p", {}, "This coordinator has no administrator yet. Create one to start using it."),
    noticeEl(opts.notice),
    h(
      "div",
      { class: "notice info", id: "token-help" },
      h("strong", {}, "Where to find the setup token"),
      h(
        "ol",
        { class: "steps" },
        h("li", {}, "On the machine that runs the coordinator, run ", h("code", {}, "bridge-coordinator setup-token"), "."),
        h("li", {}, "Or read the file ", h("code", {}, "setup-token"), " in the coordinator data directory. In a container: ", h("code", {}, "docker compose cp coordinator:/data/setup-token ."), "."),
        h("li", {}, "The token is deleted when the administrator is created."),
      ),
    ),
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
    const checks = [validateToken, validateUser, np.validatePassword, np.validateConfirm];
    let first = true;
    let ok = true;
    for (const c of checks) {
      const good = c(first);
      if (!good) first = false;
      ok = ok && good;
    }
    if (!ok) {
      error.textContent = "Fix the highlighted fields and try again.";
      return;
    }
    submit.disabled = true;
    submit.classList.add("busy");
    try {
      await api.setup(token.input.value.trim(), user.input.value, pass.input.value);
    } catch (err) {
      submit.disabled = false;
      submit.classList.remove("busy");
      if (err instanceof api.ApiError && err.code === "invalid-setup-token") {
        token.setError("This is not the setup token. Copy it again from the file or command above.");
        token.input.focus();
      } else if (err instanceof api.ApiError && err.code === "invalid-password") {
        pass.setError(err.message.charAt(0).toUpperCase() + err.message.slice(1) + ".");
        pass.input.focus();
      } else if (err instanceof api.ApiError && err.code === "invalid-username") {
        user.setError(err.message.charAt(0).toUpperCase() + err.message.slice(1) + ".");
        user.input.focus();
      } else if (err instanceof api.ApiError && err.status === 429) {
        error.textContent = `Too many wrong setup tokens from this network. Try again in ${minutes(err.retryAfter ?? 60)}.`;
      } else if (err instanceof api.ApiError && err.code === "setup-complete") {
        error.textContent = "An administrator already exists. Reload the page to sign in.";
      } else error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error. Try again.";
      return;
    }
    // Success: show the result, then sign in with the password just chosen.
    const name = user.input.value;
    form.replaceChildren(
      h("div", { class: "auth-head" }, icon("checkCircle", 32), h("h1", {}, "Administrator created")),
      h("p", { role: "status", id: "setup-success" }, `The account "${name}" exists and the setup token is used up. Signing you in...`),
    );
    try {
      await api.login(name, pass.input.value, false);
      opts.onDone(`Setup complete. You are signed in as ${name}.`);
    } catch {
      form.append(h("p", { class: "form-error box", role: "alert" }, "The account was created, but signing in failed. Reload the page and sign in."));
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

/** New-password and repeat fields with show/hide, the server's rules, a strength estimate and inline validation. */
export function newPasswordFields(policy: api.PasswordPolicy, username: () => string, ids: { passId: string; passLabel: string; confirmId: string; confirmLabel: string }): NewPasswordFields {
  const pass = field(ids.passId, ids.passLabel, "password", { autocomplete: "new-password", required: true }, "", "strength-text strength-tip policy-list");
  const confirm = field(ids.confirmId, ids.confirmLabel, "password", { autocomplete: "new-password", required: true });
  revealable(pass);
  revealable(confirm, "repeated password");
  capsWarning(pass);
  const meter = h("div", { class: "meter", "data-level": "0", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"));
  const meterText = h("p", { class: "hint", id: "strength-text", role: "status" });
  const rules = h("ul", { class: "policy", id: "policy-list", "aria-label": "Password rules the server enforces" });
  const tipEl = h("p", { class: "hint", id: "strength-tip" });
  pass.wrap.insertBefore(h("div", {}, meter, meterText, tipEl, h("p", { class: "label small", style: "margin-top:12px" }, "Required by the server"), rules), pass.wrap.querySelector(".field-error"));
  const repaint = (): void => {
    const pw = pass.input.value;
    const rs = requiredRules(pw, policy);
    rules.replaceChildren(...rs.map((r) => h("li", { class: r.met ? "met" : "", "data-rule": r.id }, icon(r.met ? "checkCircle" : "info", 16), h("span", {}, r.text), h("span", { class: "sr-only" }, r.met ? " (met)" : " (not met)"))));
    const s = strength(pw, username(), policy);
    meter.dataset["level"] = String(s.level);
    meterText.textContent = pw ? `Strength: ${s.label}${byteLength(pw) < policy.min_length ? ` (${byteLength(pw)} of ${policy.min_length} characters)` : ""}` : "";
    tipEl.textContent = s.tip + (pw ? " This is an estimate and is not enforced." : "");
  };
  const validatePassword = (focus: boolean): boolean => {
    const bad = requiredRules(pass.input.value, policy).find((r) => !r.met);
    const msg = bad ? `The password does not meet the rule: ${bad.text.toLowerCase()}.` : "";
    pass.setError(msg);
    if (msg && focus) pass.input.focus();
    return !msg;
  };
  const validateConfirm = (focus: boolean): boolean => {
    const msg = confirm.input.value !== pass.input.value ? "The two passwords do not match." : "";
    confirm.setError(msg);
    if (msg && focus) confirm.input.focus();
    return !msg;
  };
  pass.input.addEventListener("input", () => {
    repaint();
    if (pass.input.getAttribute("aria-invalid")) validatePassword(false);
    if (confirm.input.value) validateConfirm(false);
  });
  // A blur caused by pressing the submit button must not insert an error message and move the button away from the pointer.
  const notSubmit = (e: FocusEvent): boolean => (e.relatedTarget as HTMLButtonElement | null)?.type !== "submit";
  pass.input.addEventListener("blur", (e) => notSubmit(e) && pass.input.value && validatePassword(false));
  confirm.input.addEventListener("blur", (e) => notSubmit(e) && confirm.input.value && validateConfirm(false));
  confirm.input.addEventListener("input", () => confirm.input.getAttribute("aria-invalid") && validateConfirm(false));
  repaint();
  return { pass, confirm, nodes: [pass.wrap, confirm.wrap], repaint, validatePassword, validateConfirm };
}
