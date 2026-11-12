# CONV requirements

Requirement text is taken from SPEC.md.

- CONV-001: Implement in this dependency order. Repository baseline and tracking files. Typed Python command layer. Standalone compatibility verification. Versioned worker protocol. Optional Python worker service. Rust coordinator. HTTPS, authentication and LAN setup. Unified remote CLI. Browser UI. Terminal streaming. Recovery, security and performance hardening. Documentation and final verification.
- CONV-002: Each step MUST produce a working, independently testable increment. Avoid large speculative scaffolding.
- CONV-003: Stabilise shared interfaces before implementing dependent components.
- CONV-004: Prefer sequential implementation by one agent. Parallelise only when expected total token use decreases or correctness materially benefits.
- CONV-005: Run focused tests after changes and broader suites at integration boundaries.
- CONV-006: Fix root causes rather than adding workarounds or weakening assertions.
- CONV-007: Do not repeatedly redesign accepted architecture without new evidence.
- CONV-008: After each meaningful work unit, batch-update the work log and affected spec IDs, then select the next highest-value unblocked task.
- CONV-009: Do not mark milestones complete based on superficial functionality; required verification must pass.
- CONV-010: When blocked externally, complete independent work and record precise remaining blockers.
- CONV-011: Never relax mandatory requirements merely to declare convergence.
- CONV-012: Do not stop at planning, scaffolding or partial implementation. Continue until mandatory requirements are verified or genuinely blocked.
