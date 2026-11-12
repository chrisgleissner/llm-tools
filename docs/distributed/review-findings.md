# Adversarial review findings

This file records security and correctness findings from the follow-up review. Accepted findings
must have a concrete reason and a narrowly scoped regression gate. The full adversarial review and
independent security/distributed-consistency second opinion are still pending.

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

## Dispositions

| Finding | Disposition |
| --- | --- |
| ZAP-001 | no change (accepted: xterm.js needs inline styles; gate test `tests/test_zap_reports.py::test_zap_alert_gate_accepts_only_reviewed_xterm_style_csp_alert`; register FIND-008) |
