# LB requirements

Requirement text is taken from spec-addendum-2026-11-12.md.

- LB-001: Baseline. The original directive's "Definition of done" items 1 to 27 and scenarios A to S are frozen as verified at 1.1.1 by `docs/llm-bridge/spec-audit.md`. They are not re-registered. Regressions are caught by the test suite and the golden screens. The row cites the audit.
- LB-002: Platform claims match CI. The README platform badges and `docs/llm-bridge/first-run.md` claim only what the `cross-platform` CI jobs run. Suspend and wake are documented as Linux with systemd only.
- LB-003: Mouse clicks inside native provider panes are documented as unsupported; the wheel is supported (`[w]`, D46).
- LB-004: Module size. No file under `llm_tools/` matching `bridge*.py` exceeds 3000 lines (today `bridge_tui.py` has 6145 and `bridge.py` 3505). The split changes no behaviour: every golden screen passes without regeneration. A test holds a ceiling per file that may only fall. This precedes the `ORCH` work that adds views.
