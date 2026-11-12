# COORD requirements

Requirement text is taken from SPEC.md.

- COORD-001: Implement in stable Rust for Linux ARM64 and x86-64.
- COORD-002: Prefer Tokio, Axum, Serde and a mature SQLite library. Avoid unnecessary dependencies.
- COORD-003: Design for Pi Zero 2 W constraints: 512 MB RAM, limited CPU, microSD and unreliable Wi-Fi.
- COORD-004: Bound queues, buffers, caches, histories and logs. Apply backpressure, timeouts and request-size limits.
- COORD-005: Provide versioned REST endpoints for workers, missions, tasks, commands, approvals, health and diagnostics.
- COORD-006: Provide authenticated real-time event streaming.
- COORD-007: Use local SQLite for coordinator-owned metadata, journals and projections. Never share worker SQLite over network filesystems.
- COORD-008: Clearly identify stale/offline state.
- COORD-009: Provide systemd deployment, health checks, graceful shutdown and reproducible ARM64/x86-64 builds.
- COORD-010: Serve prebuilt static frontend assets without a Node.js runtime.
