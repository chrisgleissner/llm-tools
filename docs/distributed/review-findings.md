# Adversarial review findings

This file records security and correctness findings from the follow-up review. Accepted findings
must have a concrete reason and a narrowly scoped regression gate. The independent auth review is recorded below; the full adversarial review and broader distributed-consistency second opinion remain pending.

## ZAP-001 — Console CSP permits inline styles

- **Severity:** Medium (ZAP plugin 10055)
- **Location:** coordinator console CSP in `coordinator/src/web.rs`; observed on `/`, `/robots.txt`
  and `/sitemap.xml` by ZAP 2.17.0.
- **Evidence:** Local ZAP API and baseline scans against the full coordinator completed under a
  5 GiB container cap and generated both JSON/HTML reports. The baseline report identified
  `style-src 'self' 'unsafe-inline'`. ZAP reported no other medium-or-higher finding after the API
  CSP added `form-action 'none'`.
- **Disposition:** Accepted with a narrow gate exception. The terminal view uses xterm.js, whose
  renderer injects inline style rules; removing inline styles breaks terminal rendering. Script
  execution remains restricted to `script-src 'self'`, and the console also sets `default-src
  'self'`, `object-src 'none'`, `base-uri 'none'` and `form-action 'self'`. The report checker accepts
  only plugin 10055 named `CSP: style-src unsafe-inline` in the console report, on `/`,
  `/robots.txt` or `/sitemap.xml`, and only when every reported instance has the expected policy
  evidence. API-report CSP findings, findings at other paths, missing policy directives,
  inline-script allowances, and all other medium-or-higher alerts still fail the gate.
- **Regression evidence:** `tests/test_zap_reports.py::test_zap_alert_gate_accepts_only_reviewed_xterm_style_csp_alert`
  and `::test_zap_alert_gate_does_not_accept_inline_script_csp`.


## Independent review findings — TOTP and Internet sign-in

### AUTH-001 — Internet password sign-in must have a second factor

- **Severity:** High
- **Location:** `coordinator/src/http.rs::login`
- **Evidence:** The independent reviewer found that an Internet-mode administrator with a passkey enrolled to satisfy startup checks could still use the password endpoint without TOTP. Passkey enrollment at startup did not protect the password fallback.
- **Disposition:** Fixed by rejecting password sign-in in Internet mode unless TOTP is enabled; password plus TOTP is accepted, and passkey sign-in remains available when TOTP is disabled. `coordinator/src/http.rs::problem_tests::internet_mode_never_allows_password_only_sign_in` tests the policy matrix. The route gate calls this decision before creating a session.

### AUTH-002 — Local recovery must reset a lost TOTP factor

- **Severity:** Medium
- **Location:** `coordinator/src/db.rs::admin_set_password` and `coordinator/src/main.rs::admin reset-password`
- **Evidence:** The independent reviewer found that resetting a lost password left TOTP enabled, so an administrator who lost both the authenticator and recovery codes remained unable to sign in. Clearing MFA during ordinary browser password changes would be unsafe.
- **Disposition:** Fixed with a separate `admin_reset_password_local` transaction used only by the local shell recovery command. It clears TOTP and recovery codes and deletes sessions; ordinary browser password changes continue to preserve MFA. `coordinator/tests/ops.rs::recovery_commands_reset_password_and_certificates` covers both behaviors. Docs now describe the recovery effect and require TOTP re-enrollment.

## Independent API data-minimization review

### API-LEAK-001 — Generic command resource exposed internal request and result fields

- **Severity:** Medium
- **Location:** `coordinator/src/http.rs::command_response_json`
- **Evidence:** A read-scoped command lookup returned durable request/result JSON without a public
  projection. Mission creation request values included private working paths and prompts; event
  results could contain working paths and raw terminal output.
- **Disposition:** Fixed by recursively removing private path, prompt, credential and terminal field
  names from generic command responses. Resource-specific endpoints remain the preferred public
  projection.
- **Regression evidence:** `coordinator/tests/web_api.rs::command_resource_redacts_private_request_and_event_fields`
  checks distinctive request-path, prompt, event-path and terminal-output sentinels through a
  read-scoped token while retaining a safe event summary.
