# FIND requirements

Requirement text is taken from spec-addendum-2026-11-12.md.

- FIND-001: Nightly soak is red. `soak-nightly` failed on the last four runs on main with `slope controllers_rss_kib` (median 4518 KiB/h against a bound of 1536 KiB/h over a 599 s fit window). Find out whether the cause is a leak or a fit window that is too short after the 1200 s warm-up of D16. Closed by a root cause in `soak-and-chaos.md`, a fix, and three consecutive green nightly runs named in the row.
- FIND-002: Coordinator database integrity check at start (chaos finding under `PRD-008`). A corrupted database is detected at start and reported with the restore step; the coordinator does not serve from it. Test: the existing corruption fault, which fails before the fix.
- FIND-003: A dropped command or result frame leaves the command pending until the worker reconnects. The command is re-delivered or resolved within a bounded time without a reconnect, exactly once. Test: the frame-drop fault of `scripts/bridge_chaos.py` with a ledger check.
- FIND-004: The audit insert ignores write errors. A failed audit write fails the audited action or is reported as a health fault; it is never silent. Test: fault injection on the audit insert.
- FIND-005: The four `TODO(phase B)` items in `soak-and-chaos.md` are each done or deferred with a decision.
- FIND-006: Capacity plan. `capacity-plan.md` findings F1 to F10 each have a disposition, the results table holds measurements on x86-64 and on the emulated Pi, and the provisional thresholds are decided.
- FIND-007: Web review findings. `WUX-001` to `WUX-033` each have a disposition (`TRACK-016`), written against the product after pull request #57.
- FIND-008: `ZAP-001` (console content security policy allows inline styles for xterm.js) is `[w]` with its decision and the existing narrow test.
- FIND-009: Coordinator memory on the Pi. `PI-SYS-005`, `PI-VERIFY-005` and `PI-VERIFY-011` record a kernel out-of-memory kill at about 93 MiB against a 96 MiB limit. Establish whether the `DOG-024` change removed the cause, by measurement on the emulated Pi under the soak load, and either fix the cause or change the limit with a decision. The three rows cite the one measurement.
- FIND-010: Prefix-key handshake test fails rarely under load (dog-food finding 32). Root cause, fix, and a repeat count that showed the failure before.
- FIND-011: Console pane left in tmux copy mode swallows keystrokes (finding 36). Test with a real tmux pane.
- FIND-012: `stop --force` refuses with "persisted identity does not match" under load (finding 46). This is a safety path. Root cause; test that reproduces the race.
- FIND-013: The dog-food queue `findings-next.md` (50 items, no statuses) is closed: every item has a disposition in `docs/llm-bridge/dogfood.md`, open ones as `DOG` rows. Known open candidates: 17, 24, 29, 41, 43, 44.
- FIND-014: Stale statements listed by the audit are corrected: `security-requirements.md` statuses, rows that say "waits for PR #31", `benchmarks.md:192` and `:371`, `install.md:707`, `TRACK-007`, `AUTH-001`, `DATA-007`.
- FIND-015: `INT-011` and `INT-016` each have a final disposition; `INT-016` (heartbeat and verifier interruption when a mission is stopping or cancelled) gets a test or a decision.
- FIND-016: Playwright upgrade (pull request #74) lands with regenerated baselines or the version is pinned with a reason.
- FIND-017: Rich dependency. `AGENTS.md` says "standard-library-first"; 1.2.0 added Rich as a runtime dependency. Record the decision and make `AGENTS.md` and `pyproject.toml` agree.
- FIND-018: `actual_mode='autonomous'` written by the runtime is renamed to `background` with a store migration that reads old rows.
- FIND-019: The site pins 1.3.0 in `docs/site/index.html` and the install text. A check compares the pinned version with `pyproject.toml` on a release tag.
- FIND-020: Evidence for `UX-001` to `UX-011` is re-stated against the redesigned console of pull request #57, so the two programmes do not both stay open for the same screens. Each `UX` row is closed by a `UXR` row it names, or keeps a concrete remainder.
