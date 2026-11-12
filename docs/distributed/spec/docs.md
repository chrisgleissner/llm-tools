# DOCS requirements

Requirement text is taken from spec-addendum-2026-11-12.md.

- DOCS-001: Retire stale planning documents. `docs/HANDOVER.md`, `PLANS.md` and `docs/features/llm-bridge/HANDOVER.md` are removed or moved to `docs/archive/`. `docs/llm-bridge/spec-audit.md` and `progress.md` get a first line that states the release they describe and links the register.
- DOCS-002: `AGENTS.md` module list names every `llm_tools/bridge*.py` module. A test fails for a module that is not named.
- DOCS-003: `docs/llm-bridge/provider-capabilities.md` is generated from the provider registry (`PROV-002`).
- DOCS-004: Release verification document `docs/llm-bridge/release-1.5.0-verification.md` exists and is the evidence for `PRD-028`; 1.3.0 and 1.4.0 release notes are copied into the repository changelog (`PRD-029`).
