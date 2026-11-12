# TEST requirements

Requirement text is taken from SPEC.md.

- TEST-001: Preserve existing tests and coverage thresholds. Never weaken tests to hide regressions.
- TEST-002: Unit-test domain commands, validation, transitions, persistence, routing and authorisation.
- TEST-003: Run equivalent scenarios through local and remote transports and verify semantic parity.
- TEST-004: Use deterministic fake-provider integration tests where possible.
- TEST-005: Test standalone first-run, registration, discovery, remote control, approvals, events, terminal access and reconnection.
- TEST-006: Inject partitions, crashes, duplicates, lost acknowledgements, stale revisions and storage failures.
- TEST-007: Test concurrency, locking, deduplication and conflicting approvals.
- TEST-008: Extend fuzz/property tests to protocol parsing, commands and reconciliation.
- TEST-009: Test authentication bypass, malformed input, injection, CSRF, Origin enforcement and terminal writer contention.
- TEST-010: Verify HTTPS, certificates, login/logout, expiry, rate limiting and unauthenticated endpoint denial.
- TEST-011: Run Chromium and Firefox browser E2E tests, including mobile viewports.
- TEST-012: Verify offline LAN operation.
- TEST-013: Test Linux x86-64 standalone/worker and Linux ARM64 coordinator builds. Preserve macOS where practical.
- TEST-014: Benchmark startup, idle/peak RSS, API latency, event throughput, reconnect latency, database growth and memory pressure.
- TEST-015: Distinguish physical Pi measurements from emulation and estimates.
- TEST-016: Extend CI for Python, Rust, TypeScript and cross-component integration.
- TEST-017: Do not declare production readiness with failing mandatory security, compatibility or recovery tests.
