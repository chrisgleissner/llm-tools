# Distributed Bridge: final report

Summary: the optional distributed mode for LLM Bridge is implemented on
`feat/distributed-bridge` (pull request #30). Its original 169 specification IDs and operator
addenda 2 to 7 remain verified. The unified register now has 497 IDs, including tracking
requirements, the physical Pi work, the follow-up groups (`API`, `SEC2`, `PK`, `UX`, `REV`, `DOG`, `PAR`)
the release-readiness group `PRD` and the redesign group `UXR`; current in-progress and blocked rows are listed below.
CI run 42181869238 on c81ccde is green on every job,
across Linux x86-64 and ARM64, macOS ARM64 and Intel, and Windows x86-64 and ARM64.
ARM64 deployment was verified under emulation and on the physical Raspberry Pi Zero 2 W.
The physical board's deployment and coordinator measurements are recorded separately
from QEMU and derived figures in `benchmarks.md` and `install.md`. The first physical
card-write sample is retained with its unresolved writer attribution; the operator waived
a second capture and real-phone passkey checks. Other open hardware checks remain listed
as limitations, not reported as verified. This report is derived from `SPEC_STATUS.md`; the decisions it cites are in
`DECISIONS.md`.
Standalone Bridge is unchanged and opens no network connection. This report is derived
from `SPEC_STATUS.md`; the decisions it cites are in `DECISIONS.md`.
Main CI run 42258350255 exposed a tmux 3.4 first-run test expectation mismatch. PR #33
corrected it and merged; main CI run 42269767860 is green on every job.

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
- **Coordinator** (`coordinator/`, Rust 1.99.1 pinned in `rust-toolchain.toml`:
  axum, rustls with ring, SQLite with WAL and `synchronous=FULL`, Argon2id). It
  keeps the worker index and the replicated events, relays commands and terminal
  sessions, serves the browser UI and handles one administrator login. It binds to
  loopback by default.
- **Browser UI** (`web/`, TypeScript, xterm.js, no UI framework). Views: overview,
  workers, missions, mission detail with tasks, approvals, lifecycle controls and a
  terminal, sessions, provider usage, diagnostics and account. Setup, sign-in,
  remember-device and account flows; phone layouts with touch targets of at least
  44 px and a terminal key bar. A live refresh keeps keyboard focus on the element
  that had it.
- **Deployment.** The container image is `FROM scratch`, holds static musl
  binaries for amd64 and arm64, runs as non-root with a read-only root
  filesystem and has a health check. The compose file publishes only on
  loopback by default. The alternative is a hardened systemd unit installed by
  `install.sh`. Backup and restore exist for both the worker and the
  coordinator.
- **Low-write hosts** (operator addendum 6). `container/compose.low-write.yaml`
  switches logging to journald (`BRIDGE_LOG_DRIVER`) and health checks to every
  five minutes (`BRIDGE_HEALTHCHECK_INTERVAL`); the defaults in `compose.yaml` are
  unchanged. The coordinator no longer commits a write for every worker heartbeat:
  `last_seen` is written at most every 10 minutes and session and token touches at
  most every 5 minutes, while `synchronous=FULL` is kept. The API reports a
  connected worker as seen now. `install.md` has a hardware-neutral "Low-write host"
  section.
- **Project site** (operator addendum 5). `docs/site` has a section on the web
  console with screenshots, and its copy states the platform scope below.

## 2. Specification ID counts

| Status | Count |
|---|---:|
| Verified `[x]` | 270 |
| Waived `[w]` | 12 |
| Deferred `[d]` | 4 |
| Incomplete `[~]` | 80 |
| Blocked `[!]` | 0 |
| Not started `[ ]` | 131 |
| **Total** | **497** |

The original implementation IDs and operator addenda 2 to 7 remain verified. New physical and shared-tracking rows are counted from `SPEC_STATUS.md`; open rows are not described as complete. Rows closed by an operator decision rather than by verification (DOG-023, PI-VERIFY-013, PK-012) and the hardware-limited Pi rows are waived `[w]` by D46. The convergence addendum of 2026-11-12 adds 62 IDs (D40 to D47). `python scripts/spec_open.py --release 1.5.0` and `--release 1.6.0` list the open IDs per release.
## 3. Test results and coverage

CI run 42181869238 on c81ccde:

- Python on Linux x86-64 (3.11 and 3.15): full suite. Coverage is 92% total (gate
  85%) and 94% for the Bridge modules (gate 90%). mypy reports no issues.
- Python on Linux ARM64 against the ARM64 coordinator binary; on macOS ARM64 and
  Intel with the documented platform skips (section 4); on Windows x86-64 and
  ARM64, the `WINDOWS_TESTS` allowlist in `.github/workflows/test.yml`.
- Rust: `cargo fmt`, `clippy -D warnings`, `cargo test` and a release build on all
  six platforms.
- Browser: Playwright on chromium-desktop, chromium-mobile (Pixel 4),
  firefox-desktop and firefox-mobile on Linux; Chromium and Firefox on macOS and
  Windows. 118 screenshot baselines cover every page and state in both themes and
  are compared in the Playwright container image. axe reports no violations in any
  state. The bundle budget (`web/budget.json`) and the Pixel 4 load time with 4x
  CPU throttling are enforced.
- ARM64 container: built natively on the GitHub-hosted ARM64 runner, smoke test,
  memory limits and the benchmark harness within 512 MB and one CPU.
- No existing test lost lines.

The distributed tests cover:

- registration, discovery, remote control, approvals, events, reconnection and
  terminal access;
- races between processes;
- storage failure, partition after acknowledgement, and SIGKILL of the
  coordinator during a command;
- seeded fuzzing of the protocol, of every operation's parameters and of
  redelivery and reconciliation;
- route authentication;
- offline operation in a network namespace with only loopback;
- unreliable Wi-Fi through a fault-injecting proxy (`tests/fault_proxy.py`);
- storage-write bounds, including an idle coordinator with a connected worker,
  which writes nothing per heartbeat.

## 4. Platform support and verification

Platform scope is decision D8:

| Component | Linux | macOS | Windows |
|---|---|---|---|
| Coordinator and web interface | supported | supported | supported |
| Remote client (`--target`, `remote`, `worker list`) | supported | supported | supported |
| Standalone Bridge: tmux station, console | supported | not supported | not supported |
| `llm-usage`, `llm-scheduler`, `ralph-robin` | supported | supported, without systemd suspend or RTC wake | not supported |
| Mission controller, verifier, force-stop, `worker run` | supported | not supported (needs Linux procfs) | not supported |

On Windows, every other `llm-bridge` command exits with status 2 and one line, or
with `--json` returns the error code `unsupported-platform`. On macOS, 41
real-terminal and tmux tests timed out or rendered differently (CI run
42159627299); the console and station were not ported, and those tests are skipped
with a reason.

| Platform | Result |
|---|---|
| Linux x86-64, native and container | All components (CI and host runs). |
| Linux ARM64, GitHub-hosted runner (not a Pi) | Coordinator tests, Python suite against the ARM64 binary, container smoke test, memory limits, benchmark within 512 MB and one CPU. |
| DietPi v10.8.2 arm64, emulated (QEMU raspi3b TCG, mem=512M) | 84/84 Rust tests in the guest. Native service: `install.sh --lan`, in-place upgrade, state kept across `systemctl reboot`. Container: Docker from `dietpi-software`, arm64 image, compose with `compose.low-write.yaml`, administrator setup over HTTPS, host worker with remote commands, state kept across `systemctl reboot`. |
| macOS ARM64 and Intel | Coordinator, web UI (Playwright), usage tools and remote client in CI. |
| Windows x86-64 and ARM64 | Coordinator and remote client in CI; web UI (Playwright) on Windows x86-64. |
| Raspberry Pi Zero 2 W, physical | Debian 13.7 arm64; physical coordinator deployed and health-checked; Chrome/Firefox HTTPS login; worker control and terminal view verified. ARM64 benchmark values are in `benchmarks.md`. |

Two image builds of the same commit, in separate directories, produced an
identical OCI archive and identical amd64 and arm64 binaries.

## 5. Measured performance and resources

Details and methods are in `benchmarks.md`; every figure there names its platform.

- **x86-64 host, native:** startup to healthy in 47 ms; idle RSS 6.2 MiB; 29.9 MiB
  after 10 000 events; API p50/p95 0.29/0.36 ms for reads and 0.52/0.84 ms for
  commands; about 40 000 events per second; reconnect p50/p95 5.2/10.3 ms;
  557 bytes of database per event.
- **x86-64 container:** image 6.44 MB; peak 48.1 MiB with 30 concurrent logins,
  200 concurrent commands and 1500 events; shipped memory limit 96 MiB. At limits of
  48 MiB and above no restart occurred; at 40 MiB and below the process aborted
  during concurrent logins, and Docker restarted it with its data intact.
- **GitHub-hosted ARM64 runner, 512 MB and one CPU:** startup 32 ms; idle RSS
  5.0 MiB; peak 25.3 MiB; 67 550 events per second; container `memory.peak`
  40.9 to 41.1 MiB under a login burst at 96, 64 and 48 MiB limits, with no restart
  and no OOM kill.
- **DietPi arm64, emulated:** idle RSS 3.2 MiB; peak 22.1 MiB (harness),
  22.2 MiB (native service) and 21.7 MiB (container); 558 bytes per event. Guest
  memory used is 80 MB of 478 MB with the native service and 146 MB with Docker and
  the container. Time figures from emulation are far slower than real hardware and
  do not predict the Pi.
- **Raspberry Pi Zero 2 W, physical:** benchmark startup 339 ms, idle RSS 3.3 MiB,
  peak RSS 22.3 MiB, 5933 events/s, 558 bytes/event; production-container sample
  19.02 MiB of 96 MiB after login. Host baseline: 462 MiB visible, 325 MiB available,
  `dockerd` 85 MiB RSS and `containerd` 44 MiB RSS. See `benchmarks.md` for method
  and the separate production-container diagnostics sample.
- **Storage writes:** 545 bytes per event and about 68 KiB per command; an idle
  coordinator with one connected worker writes 0 bytes per heartbeat in the 50-second
  test and about 104 KiB per hour in a one-hour run.

## 6. Security verification

- The threat model (`threat-model.md`) lists assets, trust boundaries, threats,
  mitigations and residual risk.
- One focused security review (SEC-009) reported no critical findings. All
  findings except M4 are fixed, and each fix has a test that fails when the fix
  is reverted. M4 is accepted and documented: control mode lets a remote
  administrator make the provider agent run commands. The findings and their
  handling are in `DECISIONS.md` D7. The fixes include a login limiter that
  concurrent requests cannot bypass, a name-constrained local CA, expiring API
  tokens, connection and body limits, the `__Host-` session cookie, terminal access
  as a separate opt-in, and generation fencing.
- Server certificates pass strict X.509 verification (Python 3.15
  `VERIFY_X509_STRICT`: key usage, authority and subject key identifiers).
- Every coordinator route is tested without credentials and with invalid ones.
- `cargo audit` and `npm audit` report no vulnerabilities.

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
- The Bridge console, station, controller and worker are Linux-only; macOS and
  Windows support the subsets in section 4 (D8).

## 8. Known limitations

- There is one administrator and no multi-user authorization. An API token has
  full administrator power.
- Docker's port forwarding can make all clients appear to come from one
  address, so they share the per-address rate limits and the connection cap.
- If the coordinator's names change after `init` and the new names are outside
  the CA's name constraints, the operator must run `cert new-ca`. Browsers and
  workers then need the new CA.
- If the host LAN or another Docker network overlaps 172.17.0.0/16, Docker's
  default bridge must be moved with `bip` and `default-address-pools`
  (`install.md`).
- Run either the native service or the container on one host, not both; both use
  port 8443.

## 9. Installation and upgrade

- **Coordinator:** container (recommended) with `container/build.sh` and
  `container/compose.yaml`, plus `compose.low-write.yaml` on flash storage; or
  native with `deploy/install.sh` and the systemd unit. `install.md` covers both,
  certificate trust, backup, restore, recovery, Windows, and the Raspberry Pi Zero 2 W
  target profile with the emulated figures labelled as such. On DietPi, run
  `apt update && apt full-upgrade` and reboot before installing (CVE-2026-98432).
- **Workers:** `llm-bridge worker enroll`, then `llm-bridge worker install-service`.
  Operation and troubleshooting are in `operations.md`.
- **Upgrade:** container: replace the image and run `docker compose up -d`; the
  `/data` volume keeps all state and the schema migrates on start. Native: run
  `install.sh` again; it is idempotent and was verified as an in-place upgrade on the
  emulated DietPi. Back up first with `bridge-coordinator backup` and
  `llm-bridge backup`.

## 10. Physical verification notes

The physical deployment and benchmark follow-up is closed by the measured results in
`benchmarks.md` and the target profile in `install.md`. The device was booted headlessly,
secured with key-only administration and a LAN firewall, and ran the coordinator with
volatile logs and a persistent data volume. Desktop Chrome and Firefox trusted the
coordinator CA and signed in without certificate bypass; the worker reconnected after a
reboot and a browser terminal view was exercised.

The separate private hardware verification records the remaining items: attribution of
the high first 24-hour card-write sample, end-to-end unattended kernel upgrade/reboot (no
update was available and downgrading the installed kernel was not treated as a safe
substitute), physical power-loss recovery (requires a person to unplug the Pi), and
Android Chrome/Firefox browser checks (the attached phone lacks both browsers). The
operator waived a second card-write capture and real-phone passkey verification. These
limitations are not presented as verified measurements.
