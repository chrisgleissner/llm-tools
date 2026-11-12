# REV requirements

Requirement text is taken from spec-addendum-2026-11-06.md.

- REV-001: `origin/main` merged into the branch (merge commit) before the review, so the Pi changes are reviewed too.
- REV-002: Specification check of every register ID: evidence exists, the cited test fails when the implementation is reverted.
- REV-003: Correctness review (races, idempotency, uncertain outcomes, replay and cursors, leaks, time zones, platform differences).
- REV-004: Security review including every SEC2 and PK requirement and the REST API, with one independent second opinion on security and distributed-consistency findings.
- REV-005: Duplication and maintainability review (QUAL-001).
- REV-006: Performance review backed by measurements on a 512 MB ARM64 host.
- REV-007: Standalone compatibility of `llm-bridge`, `llm-usage`, `llm-scheduler`, `ralph-robin` and `llm-sleep-soak` without coordinator configuration.
- REV-008: Every finding in `review-findings.md` fixed or accepted with a reason; full gates re-run; PR merged with `--merge` after the Pi PR; `main` CI and Pages green afterwards.
