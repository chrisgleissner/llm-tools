# Distributed Bridge operations

This document covers running the optional worker service, auditing it, and
backing up and restoring Bridge state. Coordinator installation is in
`coordinator.md`.

## Worker service

Enrol the host once, then install the systemd user unit:

```bash
llm-bridge worker enroll --name build-1 --coordinator wss://bridge.home.arpa:8443 \
  --token-file token.txt --ca-file coordinator-ca.pem
# Remote access levels: none of the flags = read-only operations, no terminals;
# --allow-terminal-view = also stream mission terminals for viewing;
# --allow-control = also mutations and terminal input. Control lets a remote
# administrator (or a stolen API token) make the provider agent run commands
# through prompts or keystrokes; enable it only where that is acceptable.
llm-bridge worker install-service          # writes ~/.config/systemd/user/llm-bridge-worker.service and starts it
loginctl enable-linger                     # optional: keep it running while you are logged out
```

`llm-bridge worker install-service --print` shows the unit without installing
it. The unit runs `python -P -m llm_tools.bridge worker run` with the PATH,
package location and `LLM_BRIDGE_STATE_DIR` that were in effect at install
time, `UMask=0077` and `NoNewPrivileges=yes`. It restarts only on failure;
`systemctl --user stop llm-bridge-worker` sends SIGTERM and the worker exits
cleanly. Missions keep running while the worker is stopped or disconnected;
only replication and remote commands wait.

`llm-bridge worker uninstall-service` stops and removes the unit.
`llm-bridge worker unenroll` deletes the token and coordinator CA.

## Worker audit log

`<state>/worker/audit.jsonl` (mode 0600, at most 1 MiB plus one rotated file
`audit.jsonl.1`) records:

| Action | Recorded fields |
|---|---|
| `enroll`, `unenroll` | worker id, name, coordinator URL, whether control is allowed, whether a CA file was given |
| `service.install`, `service.uninstall` | unit path |
| `worker.start`, `worker.stop` | whether control is allowed |
| `connection.accepted`, `connection.refused`, `connection.ended` | generation, reason |
| `command` | operation, request id, mission id, outcome, error code, duplicate flag; for every mutation and every failed or refused request |
| `command.refused` | stale generation |
| `terminal.requested`, `terminal.opened`, `terminal.refused`, `terminal.closed` | session id, mission id, mode, reason |
| `terminal.input` | session id, mission id, byte count and number of input frames |

Tokens, request parameters (prompts, reasons), terminal output and terminal
input bytes are never written. Read the log with
`llm-bridge worker audit [--limit N] [--json]`. Coordinator-side actions
(logins, setup, password changes, worker and API-token changes) are in the
coordinator's own audit table.

## Backup and restore of a worker or standalone Bridge

`llm-bridge backup --output FILE` writes a consistent copy of the Bridge
database with the SQLite online backup API. It is safe while missions run.
The file is mode 0600, is checked with `PRAGMA integrity_check`, and an
existing file is never overwritten. The backup contains missions, tasks,
events, operator commands and evidence records. It does not contain provider
logs, verifier artifacts or the worker enrolment (token and CA): after
restoring on a new host, enrol the worker again.

Restore procedure:

1. Stop every mission controller (`llm-bridge stop --mission ID`) and the
   worker (`systemctl --user stop llm-bridge-worker`).
2. Run `llm-bridge restore --input FILE`. It refuses while a controller is
   running, refuses a file that fails the integrity check, is not a Bridge
   database, or has a newer schema than this Bridge, and leaves the current
   database untouched in those cases.
3. The replaced database is kept as `bridge.sqlite3.before-restore-<UTC time>`
   in the state directory. Delete it once the restored state is confirmed.
4. Start the worker again.

Restore raises the event id sequence to at least its value before the
restore. Events that the coordinator replicated after the backup was taken
therefore keep their ids, and new events on the worker receive higher ids, so
the coordinator's event cursor never skips them. The coordinator still holds
the replicated events from the replaced timeline; they are history, not
current state.

Tested by `tests/test_bridge_backup.py` (backup during an open write
transaction, restore to the snapshot, event-id sequence, refusals, restore
on a host without a database).
