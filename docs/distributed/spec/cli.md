# CLI requirements

Requirement text is taken from SPEC.md.

- CLI-001: Define typed commands with stable operation IDs, validated arguments, explicit targets, request IDs, structured results and machine-readable errors.
- CLI-002: CLI, TUI and web interfaces MUST invoke the same domain operations without duplicating business rules.
- CLI-003: Extend existing CLI syntax through explicit targeting where compatible:
- CLI-004: `--target all` initially supports read-only aggregation only.
- CLI-005: Unknown or unreachable targets MUST fail explicitly. Never silently fall back to local execution.
- CLI-006: Preserve existing machine-readable output. New metadata SHOULD be additive and versioned where necessary.
- CLI-007: Do not use shell commands or human-readable CLI output as the remote protocol.
- CLI-008: Provide consistent help, target discovery and actionable errors.
