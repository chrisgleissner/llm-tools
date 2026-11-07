# Security requirements crosswalk

This is a traceability aid for the follow-up requirements in [SPEC_STATUS.md](SPEC_STATUS.md), not a security assessment or certification. A mapped clause is a useful verification reference; it does not mean the requirement is fully covered, the product conforms to that standard, or the mapped clause is the only relevant one. Status and evidence below reflect the register as last updated 2026-11-06. `[~]` is in progress and is not complete.

## Sources and ID conventions

* [OWASP ASVS 5.0.0 taxonomy](https://cornucopia.owasp.org/taxonomy/asvs-5.0/) is the official OWASP-hosted section index. References use its two-digit chapter and section numbering (for example, `ASVS 04.02`, HTTP Message Structure Validation). Use the linked section for the exact individual verification requirements; section mapping is intentionally not a claim that every requirement in a section applies.
* [OWASP API Security Top 10 2023](https://api-security.owasp.org/editions/2023/en/0x11-t10/) uses risk IDs `API1:2023` through `API10:2023`. It is an awareness/risk taxonomy, not a clause-level control standard.
* [NIST SP 800-63B-4 final](https://csrc.nist.gov/pubs/sp/800/63/b/4/final) (July 2025) is linked to its [normative HTML](https://pages.nist.gov/800-63-4/sp800-63b.html). NIST references below are exact section IDs. The clauses are relevant where this application acts as a verifier/authenticator service; they do not automatically apply to every deployment or establish NIST conformance.

## REST API follow-up

| ID | Relevant clauses | Status and register evidence / remaining work |
|---|---|---|
| API-001 | ASVS 04.01, 04.02; API1:2023, API3:2023 | `[~]` Resource reads and mission/task mutations adapt to typed worker commands; route tests recorded in register. Other task options and resource routes remain. |
| API-002 | ASVS 04.01, 08.02, 08.03; API5:2023 | `[~]` Resource adapters use the shared typed command handler; route adapter tests recorded. Remaining console views remain. |
| API-003 | ASVS 02.04, 04.02, 07.02; API4:2023 | `[~]` Idempotency key replay, pending-command `Location`, ETag/If-Match and stale-worker 412 behavior have named tests in register. Explicit uncertain mapping remains. |
| API-004 | ASVS 04.02, 16.05; API3:2023, API4:2023 | `[~]` RFC 9457 problem responses and mutation JSON media-type enforcement have named tests. Opaque cursor pagination, timestamp contract and response-wide schema tests remain. |
| API-005 | ASVS 08.02, 08.03, 10.03; API1:2023, API3:2023, API5:2023 | `[~]` Route scopes, read default, legacy-token migration and per-route matrix are implemented; green CI evidence pending. |
| API-006 | ASVS 06.04, 08.03, 16.03; API2:2023, API5:2023 | `[~]` Token list/create/revoke, metadata-only listing, one-time secret and Account controls have named tests in register. Green CI evidence pending. |
| API-007 | ASVS 03.03, 03.04, 03.05, 03.07, 08.02, 12.01, 16.03; API4:2023, API6:2023, API8:2023 | `[~]` Existing CSRF, Origin, TLS, body limits and no-CORS baseline. Mutation rate limits and complete audit coverage remain. |
| API-008 | ASVS 04.04, 08.02, 08.03; API1:2023, API3:2023, API5:2023 | `[~]` Terminal WebSocket path exists. Response-data and terminal-scope audit remains. |
| API-009 | ASVS 04.01, 04.02; API9:2023 | `[~]` OpenAPI 3.1 generated from routes/schemas; snapshot comparison and live route tests added. Breaking-change check and green CI evidence remain. |
| API-010 | ASVS 03.02, 04.01, 04.02; API8:2023, API9:2023 | `[~]` Local script-free `/api/docs` reference exists. Test and bundle evidence pending. |
| API-011 | ASVS 04.01, 04.02; API9:2023 | `[~]` Snapshot is committed and regeneration checked in CI; released-baseline breaking-change checker remains. |
| API-012 | ASVS 04.01, 04.02; API1:2023, API3:2023, API4:2023, API8:2023 | `[~]` OpenAPI route/scope/snapshot tests added; response-wide schema validation and Schemathesis fuzzing remain. |
| API-013 | ASVS 04.01, 08.02, 08.03; API1:2023, API5:2023 | `[~]` Remote CLI maps listed operations to resource routes (named Python test in register). Remaining operations use `/commands` or return explicit unmappable errors. |
| API-014 | ASVS 04.01, 04.02; API9:2023 | `[~]` Coordinator guide, API examples, README and site link updated. Full versioning, pagination and rate-limit contract remains. |
| API-015 | ASVS 04.01; API9:2023 | `[ ]` CLI/route parity table and argparse-tree test remain. |
| API-016 | ASVS 06.07, 12.03; API2:2023, API8:2023 | `[~]` Worker-side opt-ins and resolved-root enforcement cover typed mission creation, power suspend, and restore; coordinator advertises stored policy and resource routes dispatch typed operations. CI evidence remains. |

## Security hardening

| ID | Relevant clauses | Status and register evidence / remaining work |
|---|---|---|
| SEC2-001 | ASVS 12.01, 12.02; API8:2023 | `[~]` `min_tls` defaults to `1.3`; configuration accepts only `1.3` or explicit `1.2`. `coordinator/tests/certs.rs::tls13_is_the_default_and_tls12_requires_explicit_opt_in` proves the TLS 1.2-only client fails by default and succeeds after opt-in, and checks tickets/resumption and 0-RTT are disabled. The provider restricts suites to AEAD; the pinned testssl.sh scan rejects medium-or-higher findings. CI evidence remains. |
| SEC2-002 | ASVS 03.04, 03.05, 03.07; API8:2023 | `[~]` Global security headers have route/static tests in register. API CSP explicitly sets `default-src 'none'; form-action 'none'; frame-ancestors 'none'`. The xterm console's inline-style allowance is the reviewed exception in `review-findings.md`; CI evidence remains. |
| SEC2-003 | ASVS 02.02, 04.02; API4:2023, API8:2023 | `[~]` JSON media type, bounded parser, schema limits, OPTIONS/HEAD behavior have named tests. Unknown-field and identifier audits remain. |
| SEC2-004 | ASVS 03.05, 03.07; API8:2023 | `[~]` `fetch_metadata_guard` rejects cross-site cookie API/fetch and WebSocket requests; only top-level console document navigation is exempt. Unit and coordinator route regressions cover the policy. Exact-Origin and CSRF checks remain independent. |
| SEC2-005 | ASVS 03.05, 04.01, 04.02; API8:2023 | `[~]` No-CORS baseline and preflight rejection have a named test. Explicit HTTPS origin allow-list is deferred unless CORS is introduced. |
| SEC2-006 | ASVS 06.04, 06.05, 06.07; NIST 3.2.5, 4.1, 4.2; API2:2023 | `[~]` Password first run exists. TOTP, recovery codes, origin-gated passkeys and Internet-mode MFA remain. |
| SEC2-007 | ASVS 06.02, 06.03; NIST 3.1.1.2; API2:2023 | `[~]` Current password policy and Argon2id exist. NIST length policy, offline breached-password blocklist and measured configurable cost remain. |
| SEC2-008 | ASVS 03.03, 03.04, 07.02, 07.04, 07.05; API2:2023 | `[ ]` Session rotation, per-session CSRF, active-session management and step-up remain. |
| SEC2-009 | ASVS 07.02, 08.02, 09.01, 10.03; NIST 3.2.7; API2:2023 | `[~]` Scoped SHA-256-hashed bearer tokens use the detectable `lbr_pat_` prefix, a 256-bit secret and constant-time comparison. Expiry is configurable and bounded; the admin list now shows the last-use timestamp and client address. `coordinator/tests/web_api.rs::token_last_use_address_uses_only_configured_trusted_proxy_headers` verifies that the recorded address uses trusted proxy configuration, `token_management_lists_metadata_and_shows_secret_only_at_creation` verifies public metadata and the one-time secret, and `coordinator/tests/passkeys.rs::schema_10_tokens_migrate_with_empty_last_use_addresses` covers upgrade from schema 10. Client/worker bindings, Internet-mode expiry restrictions and one-time WebSocket tickets remain. |
| SEC2-010 | ASVS 02.03, 02.04, 06.03, 08.02; NIST 3.2.2; API4:2023, API6:2023 | `[~]` Login limits exist. Authenticated mutations default to 30/token and 120/address per rolling 60 seconds, configurable within 1–10,000. The Rust hardening test covers both default budgets and `Retry-After`; exponential back-off, repeated-failure alerts and a global request cap remain. |
| SEC2-011 | ASVS 08.02, 08.03; API1:2023, API3:2023, API5:2023 | `[~]` Route scope and principal declarations exist. Centralized object policy and cross-principal negative tests remain. |
| SEC2-012 | ASVS 04.02, 16.05; API3:2023, API4:2023 | `[~]` Problem details exist. Schema-driven least-data responses, generic correlated 5xx and page caps remain. |
| SEC2-013 | ASVS 16.02, 16.03, 16.04; API8:2023 | `[~]` Existing audit rows cover several mutations. Append-only hash-chained security log and admin API remain. |
| SEC2-014 | ASVS 06.04, 16.02, 16.03; NIST 4.1, 4.2; API2:2023 | `[ ]` New-device/failure/token notifications and optional outbound webhook remain. |
| SEC2-015 | ASVS 15.02; API8:2023, API9:2023 | `[~]` `.github/workflows/supply-chain.yml` audits Rust, production npm and Python development dependencies; `tests/test_supply_chain_config.py` checks full-SHA pins, audit wiring, Dependabot, image signature, provenance and CycloneDX publication. Local cargo audit/deny, npm audit, Python `pip-audit` and config tests pass; release artifact execution awaits CI. |
| SEC2-016 | ASVS 15.01, 04.01, 04.02; API1:2023–API10:2023 | `[~]` Schemathesis fuzzes read-only operations from the committed OpenAPI snapshot under a 4 GiB address-space limit and bounded runtime; authored integration tests exercise mutating routes. ZAP scans the isolated coordinator, and a JSON gate rejects medium-or-higher findings except the reviewed xterm inline-style CSP alert in `review-findings.md`. Local Schemathesis passed 4,201 cases over 23 GET operations; local pinned ZAP scans generated all four reports under 5 GiB and the gate accepted only the documented exception. Hosted run 42340818129 passed Schemathesis, ZAP API/baseline, the report gate and testssl.sh, with no medium-or-higher TLS issue. The broad SEC2 negative-test matrix remains incomplete. |
| SEC2-017 | ASVS 12.01, 12.02, 12.03, 04.01; API8:2023, API9:2023 | `[~]` Trusted proxy CIDRs and bounded parsing are covered by `coordinator/src/client_ip.rs::tests`; `coordinator/tests/web_api.rs::token_last_use_address_uses_only_configured_trusted_proxy_headers` checks trusted address resolution in token auditing, last-use metadata and `auth/state`, and `token_mutation_budget_keys_on_trusted_client_address` verifies mutation budgets distinguish configured forwarded client addresses. `coordinator/tests/api.rs::split_listener_groups_expose_only_the_selected_route_family` checks console/REST, worker and health isolation and headers, while `coordinator/tests/limits.rs::split_listeners_share_the_global_connection_budget` checks caps across listeners. Configuration preserves combined mode unless split groups are specified. `coordinator.md` and `install.md` document VPN-first access. SEC2-018 Internet-mode preconditions remain. |
| SEC2-018 | ASVS 13.01, 13.02, 13.04; API8:2023 | `[ ]` `exposure` migration and Internet-mode startup precondition validation remain. |
| SEC2-019 | ASVS 06.07, 08.02, 09.01, 10.03, 11.06; NIST 3.2.5, 3.2.7, 4.1; API2:2023 | `[~]` `coordinator/src/certs.rs::CertificateProvider` defines internal-CA and operator-file implementations; the ACME implementation remains a documented extension point. Passkey origin and credential storage is principal-keyed and covered by `coordinator/tests/passkeys.rs`; the authenticator interface, complete principal-keying, and sender-constrained token model remain. |
| SEC2-020 | ASVS 06.07, 12.03, 13.03; NIST 3.2.5, 4.1; API2:2023, API8:2023 | `[ ]` Optional worker mTLS and token rotation remain; worker-side opt-ins are tracked under API-016. |

## Passkeys

| ID | Relevant clauses | Status and register evidence / remaining work |
|---|---|---|
| PK-001 | ASVS 06.07, 12.01; NIST 3.2.5, 4.1 | `[~]` `container/compose.yaml` adds an opt-in Tailscale sidecar and certificate renewal service; `coordinator/tests/certs.rs::configured_certificate_files_reload_for_new_tls_handshakes` verifies reload and internal-CA fallback; compose/install tests check persistence and concise setup. Live tailnet and Android checks remain. Tailscale configuration has no direct clause match. |
| PK-002 | ASVS 06.07, 06.04, 08.02; NIST 3.2.5, 4.1.2 | `[ ]` Canonical public origin and RP ID change/re-enrolment safeguards remain. |
| PK-003 | ASVS 06.07, 06.04, 11.04, 11.06; NIST 3.1.7, 4.1 | `[~]` `webauthn-rs` 0.6.2 registration/assertion start/finish routes and typed credential persistence are wired. Single-use challenges are session/browser-ticket bound and capped at 300 seconds. `coordinator/tests/web_api.rs` covers named-origin option generation, wrong-origin denial, IP-origin refusal, missing credentials and challenge replay; `coordinator/tests/passkeys.rs` covers expiry, binding and single use. Discoverable registration is enabled by a narrow vendor patch; the verifier advertises ES256, EdDSA and RS256; canonical origin is bound at registration; bounded AAGUID extraction is covered by `coordinator/src/passkeys.rs::aaguid_tests`, including the zero value for no-attestation responses and malformed/oversized inputs. Fixed-vector valid/UV-negative ceremonies remain. The crate's optional RSA verifier dependency is narrowly audit-ignored; rationale is in `DECISIONS.md` and `threat-model.md`. |
| PK-004 | ASVS 06.03, 06.05, 06.07; NIST 2, 3.1.7, 3.2.5 | `[ ]` Conditional and button passkey sign-in with password fallback remain. |
| PK-005 | ASVS 06.04, 06.07; NIST 4.1, 4.1.2 | `[ ]` Password and passkey first-run variants and mobile flow tests remain. |
| PK-006 | ASVS 06.04, 06.05, 07.04; NIST 4.1, 4.2 | `[ ]` Passkey management, step-up and last-method safeguards remain. |
| PK-007 | ASVS 06.04; NIST 4.2, 4.2.1.1 | `[ ]` Recovery codes and local passkey reset remain. |
| PK-008 | ASVS 07.06, 08.03; NIST 2, 3.2.5 | `[ ]` Fresh passkey assertions for sensitive actions remain. |
| PK-009 | ASVS 06.05, 08.03; NIST 2, 3.2.5 | `[ ]` Passkey step-up for token creation remains. |
| PK-010 | ASVS 06.01, 06.03, 06.07; NIST 3.1.7, 3.2.5 | `[ ]` Browser capability detection and calm unavailable state remain. No direct standards clause prescribes the UI state. |
| PK-011 | ASVS 06.07, 15.01; NIST 3.1.7, 3.2.5, 3.2.7, 4.1.2 | `[ ]` WebAuthn vectors, Playwright virtual-authenticator flow and negative tests remain. |
| PK-012 | ASVS 06.07, 15.01; NIST 3.1.7, 3.2.5 | `[ ]` Physical Android validation belongs to the separate Pi verification prompt; no cited clause mandates a particular device test. |

## Mapping limits

ASVS references are exact chapter/section IDs from the v5.0 taxonomy. They identify the closest sections, not a claim that every individual requirement in each section is applicable. The API Top 10 IDs identify associated risk areas, not pass/fail controls. NIST 800-63B-4 applies to its stated identity and authenticator roles; several product-specific items (browser UX, deployment profiles, scanning methods and device testing) have no direct normative clause and are mapped only to adjacent verification topics. Reassess applicability against the final design and the exact requirements before using this crosswalk as a verification plan.
