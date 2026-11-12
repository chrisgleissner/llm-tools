# AGENT requirements

Requirement text is taken from SPEC.md.

- AGENT-001: Default to a single Sonnet 5.5 agent performing implementation, integration, testing and documentation.
- AGENT-002: Use a second Sonnet 5.5 agent only when a substantial, independently bounded task demonstrably benefits from parallel execution or specialised review. Do not maintain a permanent team.
- AGENT-003: Consult Opus 5.5 only for difficult architectural decisions, distributed consistency, security-critical design, or problems that remain unresolved after focused Sonnet investigation. Use one narrowly scoped question per consultation, providing only essential code references and constraints.
- AGENT-004: Never exceed three active agents, including the orchestrator. Prefer one. Agent creation must be justified by expected token savings or a material reduction in correctness risk, not speed alone.
- AGENT-005: Minimise handovers. Prefer the same agent completing a coherent vertical slice. Never spawn agents merely for repository exploration, routine review, documentation or test execution.
- AGENT-006: Agent handovers, when unavoidable, MUST contain only. Objective and relevant spec IDs. Essential file paths/interfaces. Existing decisions and constraints. Required verification. Exact deliverable.
- AGENT-007: Minimise tool output and context consumption. Search symbols before reading files. Read only relevant file ranges. Avoid repeated repository-wide scans. Use concise commands and targeted tests. Avoid verbose progress reports. Reuse existing documentation and test infrastructure. Do not repeatedly consult Opus on settled decisions.
- AGENT-008: Token efficiency MUST NOT compromise correctness, security, compatibility or required verification.
- AGENT-009: Work autonomously. Ask the user only when an external dependency, irreversible action or genuinely ambiguous product decision prevents safe progress.
