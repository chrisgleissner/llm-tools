# Distributed Bridge: final report

Summary: the optional distributed mode for LLM Bridge is implemented on
`feat/distributed-bridge` and verified on Linux x86-64. Of 169 specification
IDs, 163 are verified and 6 are blocked. All 6 blocked IDs need ARM64 execution
or Raspberry Pi Zero 2 W measurements, which could not run on this host: the
emulated Pi (pi-sim) did not answer SSH, and the hardware arrives on 2026-11-06.
Standalone Bridge is unchanged and opens no network connection. This report is
derived from `SPEC_STATUS.md`. The decisions it refers to are in `DECISIONS.md`.

## 1. Implementation summary

- **Typed command layer** (`llm_tools/bridge_commands.py`). Each operation is
  declared once with typed parameters, for example `mission.pause`, `task.add`,
  `proposal.decide`, `session.list` and `usage.snapshot`. The local CLI, the
  worker and the console all use it. A request ID is the durable command ID,
  so a duplicate delivery returns the stored outcome. `expected_revision`
  rejects conflicting edits. An unexpected failure during a mutation is
  reported as `uncertain`.
- **Worker service** (`bridge_worker.py`, `bridge_ws.py`,
  `bridge_terminal_stream.py`). The worker opens one outbound TLS WebSocket and
  speaks protocol v1 (`protocol.md`). It is read-only unless enrolled with
  `--allow-control`. Terminals are shared only with `--allow-terminal-view` or
  `--allow-control`, and the worker grants at most one terminal writer itself.
  It writes an audit log and can run as a systemd user unit
  (`worker install-service`).
- **Remote CLI** (`bridge_remote.py`). `llm-bridge remote login` stores the
  coordinator URL, CA and API token. `llm-bridge --target WORKER COMMAND` runs a
  command on that worker, and `worker list` shows the registered workers. An
  unknown or offline target fails; it never falls back to local execution.
- **Coordinator** (`coordinator/`, Rust: axum, rustls with ring, SQLite,
  Argon2id). It keeps the worker index and the replicated events, relays
  commands and terminal sessions, serves the browser UI and handles one
  administrator login. It binds to loopback by default.
- **Browser UI** (`web/`, TypeScript, xterm.js). It has these views: overview,
  workers, missions, mission detail with tasks, approvals, lifecycle controls
  and a terminal, sessions, provider usage, diagnostics and account.
- **Deployment.** The container image is `FROM scratch`, holds static musl
  binaries for amd64 and arm64, runs as non-root with a read-only root
  filesystem and has a health check. The compose file publishes only on
  loopback by default. The alternative is a hardened systemd unit installed by
  `install.sh`. Backup and restore exist for both the worker and the
  coordinator.

## 2. Specification ID counts

| Status | Count |
|---|---|
| Verified `[x]` | 163 |
| Incomplete `[~]` | 0 |
| Blocked `[!]` | 6: COORD-001, COORD-003, OPS-001, TEST-013, TEST-014, CONV-012 |

Operator addendum: A1 and A3 are verified. A2 and A5 are done on x86-64 and in
the documentation, but have no ARM64 measurement. A4 (emulated Pi) is blocked.

## 3. Test results and coverage

Final gate on commit 51a1824:

- Python: 4181 passed, 18 skipped, 0 failed. Coverage is 92% total (gate 85%)
  and 94% for the Bridge modules (gate 90%). mypy reports no issues.
- Rust: `cargo fmt`, `clippy -D warnings`, and `cargo test` with 84 passed.
- Browser: Playwright 68/68 on chromium-desktop, chromium-mobile,
  firefox-desktop and firefox-mobile, including axe accessibility checks.
- No existing test lost lines.

The distributed tests cover the following:

- registration, discovery, remote control, approvals, events, reconnection and
  terminal access;
- races between processes;
- storage failure, partition after acknowledgement, and SIGKILL of the
  coordinator during a command;
- seeded fuzzing of the protocol, of every operation's parameters and of
  redelivery and reconciliation;
- route authentication;
- offline operation in a network namespace with only loopback.

## 4. Platform verification

| Platform | Result |
|---|---|
| Linux x86-64, native | Standalone, worker, remote CLI and coordinator verified. |
| Linux x86-64, container (rootless Docker 29.9) | Verified: `init` and repeated `init`, health check, loopback-only publishing, administrator setup, workers and tokens through `exec`, a native worker, backup and restore, password reset, certificate renewal, restart, SIGTERM stop. |
| Linux ARM64 | Cross-built only: static musl binary and image, reproducible. Not executed (blocked). |
| Raspberry Pi Zero 2 W, DietPi | Not measured (blocked). `install.md` has a target-profile section with sizing derived from the x86-64 measurements. |
| macOS | Not tested; no macOS host. |

Two image builds of the same commit, in separate directories, produced an
identical OCI archive and identical amd64 and arm64 binaries.

## 5. Measured performance and resources

All figures are from an x86-64 host (i7-6700K); details are in
`benchmarks.md`.

- **Native process:**
  - startup to healthy in 47 ms;
  - idle RSS 6.2 MiB;
  - RSS 29.9 MiB after 10 000 events;
  - API p50/p95 of 0.29/0.36 ms for reads and 0.52/0.84 ms for commands;
  - about 40 000 events per second ingested;
  - reconnect p50/p95 of 5.2/10.3 ms;
  - 557 bytes of database per event.
- **Container:**
  - image size 6.44 MB;
  - 1.5 MiB RSS before the first login;
  - peak 48.1 MiB with 30 concurrent logins, 200 concurrent commands and
    1500 events;
  - shipped memory limit 96 MiB.
- **Memory pressure:**
  - A login burst peaks at about 42 MiB, because two Argon2id hashes run at
    once.
  - At limits of 48 MiB and above, no restart occurred.
  - At 40 MiB and below, the process aborted during concurrent logins. Docker
    restarted it and it kept its data.

## 6. Security verification

- The threat model (`threat-model.md`) lists assets, trust boundaries, threats,
  mitigations and residual risk.
- One focused security review (SEC-009) reported no critical findings. All
  findings except M4 are fixed, and each fix has a test that fails when the fix
  is reverted. M4 is accepted and documented: control mode lets a remote
  administrator make the provider agent run commands. The findings and their
  handling are in `DECISIONS.md` D7. The fixes include:
  - a login limiter that cannot be bypassed by concurrent requests;
  - a name-constrained local CA;
  - API tokens that expire;
  - connection and body limits;
  - the `__Host-` session cookie;
  - terminal access as a separate opt-in;
  - generation fencing.
- Every coordinator route is tested without credentials and with invalid ones.
- `cargo audit` and `npm audit` report no vulnerabilities. The unmaintained
  `rustls-pemfile` crate was replaced.

## 7. Architecture deviations

- Rust is used only for the coordinator. Mission rules stay in Python, and the
  coordinator adds transport and projection only (D1).
- The worker uses a small standard-library WebSocket client instead of adding a
  dependency (D4).
- Tasks and approvals are typed commands sent to `POST /api/v1/commands`, not
  separate REST resources.
- Remote control and terminal sharing are opt-in on each worker. The worker
  enforces both itself, independent of the coordinator (D5, D7).
- No worker container image is provided, so provider credentials stay out of
  container configuration (D6).

## 8. Known limitations

- There is one administrator and no multi-user authorization. An API token has
  full administrator power.
- Docker's port forwarding can make all clients appear to come from one
  address, so they share the per-address rate limits and the connection cap.
- If the coordinator's names change after `init` and the new names are outside
  the CA's name constraints, the operator must run `cert new-ca`. Browsers and
  workers then need the new CA.
- ARM64 and Pi Zero 2 W behaviour is not measured.
- macOS is untested.

## 9. Installation and upgrade

- **Coordinator:**
  - Container (recommended): run `container/build.sh`, then use
    `container/compose.yaml`.
  - Native: use `deploy/install.sh` with the systemd unit.
  - Instructions for both, plus certificate trust, backup, restore and
    recovery, are in `install.md`.
- **Workers:** run `llm-bridge worker enroll`, then
  `llm-bridge worker install-service`. Operation and troubleshooting are in
  `operations.md`.
- **Upgrade:**
  - Container: replace the image and run `docker compose up -d`. The `/data`
    volume keeps all state, and the database schema migrates on start.
  - Native: run `install.sh` again; it is idempotent.
  - Back up before an upgrade with `bridge-coordinator backup` and
    `llm-bridge backup`.

## 10. Remaining blockers

1. ARM64 execution and Pi Zero 2 W measurements (COORD-001, COORD-003,
   OPS-001, TEST-013, TEST-014, A2, A4, A5).
   - pi-sim at `ssh -p 2222 root@127.0.0.1` timed out during the SSH banner
     exchange in every attempt on 2026-11-05. It was not modified.
   - The physical Pi Zero 2 W arrives on 2026-11-06.
   - The next steps are the same on either device:
     1. Load the arm64 image (`container/build.sh --oci`).
     2. Run the compose flow.
     3. Repeat the `benchmarks.md` measurements and the memory-pressure run.
     4. Fill in the target-profile section in `install.md`.
2. macOS is untested (TEST-013, "where practical"); no macOS host is available.
