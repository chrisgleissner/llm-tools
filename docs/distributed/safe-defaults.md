# Safe defaults review (PRD-026)

This page lists every Python-side setting that has a security or safety effect, the
default it ships with, the reason for that default, the code that enforces it, and the
test that asserts it. The tests read the defaults from the code, not from this page.
`tests/test_safe_defaults.py::test_every_documented_test_exists` fails when this table
names a test that does not exist, or when a test in that file is missing from this table.

Reviewed on 2026-11-06 against the Python client side: Bridge, the worker service, the
remote CLI, the `llm-usage` sampler service, `llm-scheduler`, `ralph-robin` and
`llm-sleep-soak`. The Rust coordinator is not covered here; see
[Coordinator defaults that remain for phase B](#coordinator-defaults-that-remain-for-phase-b).

## Network exposure

| Setting | Default | Reason | Enforced in | Test |
|---|---|---|---|---|
| Network listener of Bridge and the Python tools | None. The only server in the package is the `llm-usage` sampler, and it listens on a Unix socket. Every other network use is an outbound client connection. | A local tool that opens a TCP port is reachable by other hosts and by other local users. | No module in `llm_tools/` binds an inet socket. `usage_service.ThreadingUnixServer` uses `AF_UNIX`. | `tests/test_safe_defaults.py::test_package_has_no_network_listener` |
| Telemetry and analytics | None. The source names only provider quota endpoints, documentation links and example host names. | Usage data and mission text must not leave the machine unless the operator enrols a worker. | There is no telemetry code. The test lists every host name in the source and fails on a new one, so adding a host needs a review of this list. | `tests/test_safe_defaults.py::test_package_contacts_only_provider_and_documentation_hosts` |

## Worker and remote CLI

| Setting | Default | Reason | Enforced in | Test |
|---|---|---|---|---|
| `worker enroll --allow-control` | Off. The worker is read-only remotely. | A coordinator or API token must not be able to change missions or type into terminals on a host unless its owner opted in. | `bridge.py` argument parser (`store_true`), `bridge_worker.WorkerConfig.allow_control`, `WorkerSession.handle` refuses mutating operations. | `tests/test_safe_defaults.py::test_worker_opt_ins_default_off` |
| `worker enroll --allow-terminal-view` | Off. A read-only worker streams no terminals. | A terminal shows prompts, file contents and secrets. | `WorkerConfig.allow_terminal_view`, `WorkerConfig.terminal_view`. An enrolment file without the key reads as off. | `tests/test_safe_defaults.py::test_worker_opt_ins_default_off` |
| Operations a read-only worker advertises | Read-only operations only. | The coordinator offers only what the worker advertises in `hello`. | `WorkerSession.hello` filters `operation_catalog()` by `mutating`. | `tests/test_safe_defaults.py::test_read_only_worker_advertises_no_mutating_operation` |
| Certificate and hostname verification (worker and remote CLI) | On: `CERT_REQUIRED`, host name checked, TLS 1.2 minimum. The enrolled CA file is trusted in addition to nothing else; without a CA file the system store is used. | A bearer token sent to an unverified server can be stolen. | `bridge_worker.tls_context`, `bridge_remote.Client.__init__`, `bridge_ws.WebSocketClient.connect` (minimum version). There is no option to disable verification. | `tests/test_safe_defaults.py::test_worker_and_remote_contexts_verify_certificates_and_hostnames` |
| Plain-text coordinator URL (worker) | Refused. A worker URL must start with `wss://`. | The worker sends its token in the handshake. | `bridge_worker.enroll` and, since this review, `bridge_worker.load_config`. | `tests/test_safe_defaults.py::test_worker_refuses_a_plaintext_coordinator_at_enrolment_and_when_loading` |
| Plain-text coordinator URL (remote CLI) | Refused. The URL must start with `https://`. | The CLI sends an administrator-level API token. | `bridge_remote.login` and, since this review, `bridge_remote.load`. | `tests/test_safe_defaults.py::test_remote_refuses_a_plaintext_coordinator_at_login_and_when_loading` |
| HTTP redirects (remote CLI) | Never followed. | A redirect would resend the bearer token to another URL. | `bridge_remote._RefuseRedirect`. | `tests/test_safe_defaults.py::test_remote_client_never_follows_redirects` |
| Token storage | Worker token, API token and CA in files with mode 0600 inside directories with mode 0700. The token is absent from `status` output, audit entries and the systemd unit. | A token is a credential. | `bridge_worker._atomic_private_write` (also used by `bridge_remote`), `WorkerConfig.public`, `bridge_worker.audit`, `service_unit_text`. | `tests/test_safe_defaults.py::test_enrolment_login_and_audit_files_are_private_and_hold_the_token_only_where_needed` |
| State directory created by the first worker or remote command | 0700 on the state directory and every directory below it that was created. | Bridge refuses a state directory that other users can enter. Before this review `worker enroll` or `remote login` run as the first command created the state directory with the default umask mode, so later commands failed with a permission error. | `bridge_worker._make_private_dir`. | `tests/test_safe_defaults.py::test_worker_enrolment_as_the_first_command_leaves_a_private_state_directory`, `tests/test_safe_defaults.py::test_remote_login_as_the_first_command_leaves_a_private_state_directory` |
| Worker systemd user unit | User scope (`WantedBy=default.target`), `UMask=0077`, `NoNewPrivileges=yes`, restart on failure only, no token in the unit. | The worker holds a credential and runs as the user. | `bridge_worker.service_unit_text`. | `tests/test_safe_defaults.py::test_service_units_are_user_scoped_and_the_worker_unit_is_hardened` |

## Local files and directories

| Setting | Default | Reason | Enforced in | Test |
|---|---|---|---|---|
| Bridge state directory | Owned by the user, mode 0700. Bridge refuses to open a database in a directory that is group- or world-accessible and prints the `chmod` that fixes it. | Missions hold prompts, task text and evidence. | `bridge_store.BridgeStore.__init__`. | `tests/test_safe_defaults.py::test_database_files_are_private_and_a_loose_state_directory_is_refused` |
| Database files | `bridge.sqlite3`, `-wal` and `-shm` with mode 0600. | As above. | `BridgeStore._secure_sqlite_files`. | `tests/test_safe_defaults.py::test_database_files_are_private_and_a_loose_state_directory_is_refused` |
| Mission runtime and tmux socket directories | 0700, owned by the user, never a symlink. A directory that does not meet this is refused or skipped. | Another user must not reach a mission's controller files or terminal. | `bridge_runtime._runtime_directory`, `mission_socket_path`, `_trusted_tmp_directory`. | `tests/test_safe_defaults.py::test_runtime_and_socket_directories_are_private` |
| Backups on restore | `llm-bridge restore` keeps the replaced database as `bridge.sqlite3.before-restore-<time>` (mode 0600), and `llm-bridge backup` writes mode 0600 and never overwrites a file. | A restore cannot lose the current data. | `bridge_backup.backup`, `bridge_backup.restore`. | `tests/test_safe_defaults.py::test_backup_is_private_and_restore_keeps_the_replaced_database` |
| Suspend ledger | Mode 0600. | It records when this host suspends. Before this review it was created with the default umask mode. | `common._append_ledger`. | `tests/test_safe_defaults.py::test_suspend_ledger_is_private` |
| Cache root and everything under it (`${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`) | Directories 0700, files 0600: provider usage caches, the usage log, route blocks, Ralph Robin state, the suspend ledger, shared snapshots, scheduler and Ralph Robin run directories (prompt copy, `attempt-N.out`, `attempt-N.status`, the tmux command script). A directory that an earlier release created with the default umask mode, and a file written with it, is tightened the next time llm-tools writes there. Only the cache root and the directories below it are changed; the user's parent directories such as `~/.cache` keep their mode. | The caches name accounts, usage and reset times, and the run directories hold prompts and provider output. | `common.ensure_private_cache_dir`, `common.write_private_text`, `common.open_private_text` and `common.touch_private_file`, used at every creation site (`common.py`, `scheduler.py`, `ralph_robin.py`, `routes.py`, `copilot_refresh.py`, `bridge_power.py`, `usage.py`). | `tests/test_safe_defaults.py::test_cache_directories_and_files_are_private_under_a_permissive_umask`, `tests/test_safe_defaults.py::test_cache_directories_made_by_an_older_release_are_tightened_when_llm_tools_next_writes`, `tests/test_safe_defaults.py::test_scheduler_run_files_and_copilot_refresh_cache_are_private` |
| Copy of the Claude statusline input | Mode 0600; the cache directory is created 0700 when it does not exist. | The input names working folders, session ids and transcript paths. Before this review it was written with the default umask mode. | `usage.statusline_mode`. | `tests/test_safe_defaults.py::test_claude_statusline_copy_is_private` |

## llm-usage sampler service

| Setting | Default | Reason | Enforced in | Test |
|---|---|---|---|---|
| Transport | Unix socket only, in a directory owned by the user with mode 0700, socket mode 0600. A loose directory left by an older release is tightened; one owned by someone else is refused. | The service shares capacity data and accepts a shutdown request. | `usage_service.trusted_directory`, `ThreadingUnixServer`. | `tests/test_safe_defaults.py::test_usage_service_is_a_private_unix_socket_and_nothing_else` |
| Peers | A connection from another user's process gets no answer (`SO_PEERCRED`). | Defence in depth if the socket directory is ever shared. | `usage_service._peer_is_us`. | `tests/test_safe_defaults.py::test_usage_service_ignores_other_users` |
| Snapshot and history files | Service directory 0700; `latest.json`, `history.jsonl` and the pid file mode 0600. Before this review `history.jsonl` used the default umask mode. | Snapshots name accounts and usage. | `usage_service._atomic_json_write`, `_append_history`, `run_service`. | `tests/test_safe_defaults.py::test_usage_service_files_are_private` |
| Service unit | systemd user unit or launchd LaunchAgent in the user's home. Never a system unit. The systemd unit sets `UMask=0077` and `NoNewPrivileges=yes`, as the worker unit does. | The service needs no privilege. The sampler only runs provider CLIs as the user (`codex app-server`, `claude`, `copilot` through `script`, `kilo`, `mmx`, `gh auth token`); it never calls `sudo`, `rtcwake` or another setuid program, so `NoNewPrivileges` blocks nothing it uses. `UMask=0077` makes files that those CLIs create 0600. | `usage_service.systemd_unit_path`, `launchd_plist_path`. | `tests/test_safe_defaults.py::test_service_units_are_user_scoped_and_the_worker_unit_is_hardened` |

## Suspend, launch and approval

| Setting | Default | Reason | Enforced in | Test |
|---|---|---|---|---|
| Mission `suspend_when_rate_limited` | Off. A mission never suspends the machine or arms a wake alarm unless `llm-bridge mission set --suspend-when-rate-limited on` was run. | Suspending interrupts other work and other users of the machine. | `bridge_runtime.BridgeMissionHooks.suspend_allowed`, `bridge_refresh` (wake requests need `True`). | `tests/test_safe_defaults.py::test_mission_does_not_suspend_the_machine_unless_asked`, `tests/test_safe_defaults.py::test_mission_suspend_is_refused_without_the_opt_in` |
| Mission `auto_continue` | Off for native missions. Each task waits for the operator's approval. | Bridge never marks work done without the operator or a configured verifier. | `bridge._mission_creation_inputs`. | `tests/test_safe_defaults.py::test_mission_does_not_suspend_the_machine_unless_asked` |
| `llm-scheduler` and `ralph-robin` wake alarms, suspend and watchdog | Off. `--wake`, `--suspend-until-ready` and `--watchdog` are opt-in. | Arming an RTC wake alarm or a hardware watchdog changes machine state. | `scheduler.SchedulerConfig`, `ralph_robin.RalphConfig`. | `tests/test_safe_defaults.py::test_scheduler_ralph_and_soak_do_not_suspend_or_arm_wakes_by_default` |
| Provider launch flags | No flag that skips permission prompts or sandboxing (`--dangerously-skip-permissions`, `--yolo`, `--full-auto` and similar) is added. What a provider may do without asking stays governed by the provider's own configuration. | The operator, not this tool, decides what a provider may do unattended. | `scheduler.provider_default_argv`. | `tests/test_safe_defaults.py::test_provider_launch_commands_add_no_permission_bypass_flag` |
| Changed verifier files | A verifier that passes after its files changed does not complete a task without operator approval. | An agent must not edit the check that grades its own work. | `bridge_verifier_guard`, `bridge_runtime` (verification). | `tests/test_bridge_verifier_guard.py::test_verifier_edited_by_the_agent_requires_operator_approval` |

## Findings of this review

These defaults were unsafe or inconsistent and were changed. Each has a test that failed
before the change.

1. `worker enroll` or `remote login` as the first command on a host created the state
   directory with the default umask mode (0755 under umask 022). Bridge then refused to
   open its own database. The state directory is now created 0700.
2. A `worker.json` or `remote.json` edited to a plain-text URL (`ws://`, `http://`) was
   accepted when loaded, although enrolment and login refuse it. Loading now refuses it.
3. The suspend ledger and the sampler's `history.jsonl` were created with the default
   umask mode (0644). They are now 0600.
4. The copy of the Claude statusline input (`claude-status.json`) was written with the
   default umask mode. It names working folders and session ids. It is now 0600.
5. The other files under the cache root (provider usage caches, the usage log, route
   blocks, Ralph Robin state, shared snapshots, scheduler run files) were written with the
   default umask mode, and the cache directories were 0755. They are now 0600 and 0700,
   and an existing directory or file from an older release is tightened on the next
   write. The three `test_cache_*` and `test_scheduler_run_files_*` tests fail on the
   previous code with modes 0755 and 0644.
6. The `llm-usage` systemd unit lacked `UMask=0077` and `NoNewPrivileges=yes`. It has
   both now. `test_service_units_are_user_scoped_and_the_worker_unit_is_hardened` fails on
   the previous unit text.

## Open items

- The RTC wake paths try `rtcwake -m no` first and fall back to `systemd-run --user`.
  `rtcwake` only succeeds when the process may write the RTC (normally root); the
  fallback needs no privilege. Neither path is reached unless a suspend was requested
  (see the suspend rows above).

## Coordinator defaults that remain for phase B

The Rust coordinator is being rewritten by another mission. Its defaults need the same
table, with a test per row, once that work lands. Settings to review:

| Setting | Documented default (`coordinator.md`) |
|---|---|
| Bind address | Loopback; a non-loopback bind needs `lan_enabled = true` |
| CORS | None (same-origin UI; state changes need a CSRF header and an exact `Origin`) |
| API token expiry | 90 days; `--expires-days 0` is an explicit opt-out |
| Browser session lifetime | 2 h idle and 12 h absolute; remembered sessions 8 h idle and 7 days absolute |
| Session cookie | `__Host-` prefix, HttpOnly, Secure, SameSite=Strict |
| Login rate limits | Per address and per user, reserved before password hashing |
| Connection, header, body limits | Per-address caps and timeouts |
| TLS | 1.2 minimum with AEAD suites; name-constrained CA |
| Backups | `bridge-coordinator backup` exists; no scheduled backup is on by default |
| Worker registration | Token per worker, revocable, generations per name |

The "backups on" requirement needs a decision in phase B: either schedule a backup by
default in the coordinator container and native unit, or state why a manual `backup`
command satisfies it.
