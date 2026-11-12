# DATA requirements

Requirement text is taken from SPEC.md.

- DATA-001: Worker databases remain authoritative; coordinator state is reconcilable.
- DATA-002: Preserve transactional state/event/command consistency.
- DATA-003: Keep database and event schema versions distinct; test migrations.
- DATA-004: Preserve secure permissions, durable writes and corruption detection.
- DATA-005: Coordinator recovery MUST NOT duplicate execution.
- DATA-006: Document and test representative backup/restore procedures.
- DATA-007: Bound coordinator storage growth without deleting authoritative worker evidence.
