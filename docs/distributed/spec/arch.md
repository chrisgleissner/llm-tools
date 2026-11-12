# ARCH requirements

Requirement text is taken from SPEC.md.

- ARCH-001: Support standalone and distributed operation without duplicating the execution engine.
- ARCH-002: Each mission has exactly one authoritative worker owning execution, provider processes, state transitions, evidence, verification, credentials and session recovery.
- ARCH-003: The coordinator owns worker registration, mission indexing, command routing, event projections, web access and coordinator metadata.
- ARCH-004: The coordinator MUST NOT execute provider sessions or implement competing provider-selection, retry or mission-state logic.
- ARCH-005: Local and remote operations MUST use a common typed application command layer.
- ARCH-006: Standalone execution MUST invoke that layer directly without HTTP or a daemon.
- ARCH-007: Workers MUST continue safely during coordinator or network outages.
- ARCH-008: Separate domain logic, persistence, transport, process supervision and presentation through clear, minimal interfaces.
- ARCH-009: Do not implement automatic cross-worker task migration, global scheduling or distributed execution ownership transfer.
