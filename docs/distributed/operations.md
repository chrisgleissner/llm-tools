# Distributed Bridge operations

This document covers running the optional worker service, auditing it, backing up
and restoring Bridge state, and troubleshooting. Coordinator installation, certificate
trust and coordinator backup are in `install.md`; coordinator configuration and limits
are in `coordinator.md`.

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

## Troubleshooting

Start with `llm-bridge worker list` (state and last seen per worker, from any machine
with `remote login`), `llm-bridge worker status` on the worker, and
`bridge-coordinator info` on the coordinator. The worker writes its connection
messages to standard error; under the systemd user unit read them with
`journalctl --user -u llm-bridge-worker`. `llm-bridge worker audit` shows what the
worker accepted and refused.

### Worker shows offline

A worker is offline when it has no open control connection. The worker retries with an
exponential backoff of at most 60 s, so a worker that comes back reconnects by
itself. Check these causes in order:

1. The service is not running: `systemctl --user status llm-bridge-worker`.
2. The token was revoked (`bridge-coordinator worker revoke NAME`). The coordinator
   answers `invalid or revoked worker token` (HTTP 401), and a connected worker is
   closed with `worker revoked`. Create a new token with `bridge-coordinator worker add
   NAME` and enrol again with `llm-bridge worker enroll`.
3. The certificate is not trusted. The worker trusts the `ca.pem` it was given at
   enrolment. After `bridge-coordinator cert new-ca` that file is the old CA, so the
   TLS handshake fails (the journal shows `worker connection ended: SSLCertVerificationError`
   or similar). Copy the new `ca.pem` and enrol the worker again with `--ca-file`.
   `cert renew` keeps the CA and does not need re-enrolment.
4. The system clock is wrong on the worker or the coordinator host. Certificate
   validity is checked against the local time, so a clock far from real time makes the
   handshake fail with the same error. Fix the clock and restart the service.
5. The URL does not match the certificate (next section) or is not reachable (firewall,
   wrong port; see `install.md`, Network placement).

A worker that was enrolled again with a new token starts with a fresh connection
generation (`protocol.md`, Generation fencing); no other action is needed.

### Certificate name mismatch

The coordinator URL host must be one of the names in the server certificate. `info`
flags a URL that the certificate does not cover. Which command to use depends on the
name:

- Name within the CA's name constraints (`coordinator.md`, CA constraints): run
  `bridge-coordinator cert renew --hostname NAME --ip ADDR` and restart. The CA is
  unchanged, so browsers and workers keep their trust.
- Name outside the constraints: `cert renew` refuses with `not renewed: NAME outside the
  name constraints of the CA in ca.pem`. Run `cert new-ca` with the full list of names,
  import the new `ca.pem` in every browser and enrol every worker again, as in
  `install.md` (Recovery).

Do not click through the browser warning; see `install.md` (Trusting the certificate).

### Login is rate limited

The coordinator answers a login with HTTP 429 and error code `rate-limited` ("too many
failed attempts; try again later", with a `Retry-After` header). The limits are per
client address and per user name; the numbers and the exemption for known addresses
are in `coordinator.md` (Login rate limiting). Wait for the window (at most a minute),
or restart the coordinator, which clears the counters.

If you see the error without having mistyped a password, other clients may share your
address. A port published through Docker's userland proxy (loopback publishing) or
through rootless Docker replaces every client address with a gateway address, so all
clients and their failures count together. See `install.md` (Container deployment,
Client addresses). A connection that exceeds `max_connections_per_ip` is closed
immediately without an HTTP answer.

### A command is reported `uncertain`

`uncertain` means the worker hit an unexpected failure while applying a mutation, so it
cannot say whether the change was made (`protocol.md`, Command delivery). The remote
CLI prints `outcome uncertain on the worker: MESSAGE; check the mission before
retrying` and exits with status 2. The coordinator never retries the command under a
new request id.

To find out what happened:

1. Run `llm-bridge --target WORKER status --mission ID` and `llm-bridge --target
   WORKER events --mission ID` and compare the state with what the command should have
   produced. A mission operation that could not record its outcome appears as an
   `operation-uncertain` event.
2. On the worker, `llm-bridge worker audit` lists the command with its request id,
   outcome and error code. The coordinator journal entry is available with
   `GET /api/v1/commands/REQUEST_ID` (API token or session).
3. Repeat the command only after the check. A repeated mutation with the same request
   id returns the stored outcome; a new request id applies the change again.

A different case is a result that does not arrive within 15 s. The remote CLI then
reports that the target did not answer and that the request stays queued. The
coordinator delivers it when the worker reconnects; it is not `uncertain`.

### Terminal view refused

The browser or API reports the reason sent by the worker. When it says `this worker does
not share terminals; enrol it with --allow-terminal-view or --allow-control`, enrol
the worker again with one of those options (`llm-bridge worker enroll ... --allow-terminal-view`).
The access levels are described under Worker service above, and the security
consequences in `threat-model.md`. Other refusal reasons are `unknown mission` and
`too many terminal sessions or duplicate session id`. Each refusal is recorded as
`terminal.refused` in the worker audit log.

### Terminal shows "view only"

The terminal grants one writer per mission and only while no local tmux client is
attached (`protocol.md`, Terminal sessions). The reason shown next to "view only" is one of:

- `another session holds the writer role; viewing only`: close the other browser tab or
  session that is writing, then reopen this one.
- `a local terminal is attached to this station; viewing only`: detach the local tmux
  client on the worker. The worker checks for a local client before typing each input
  frame, so the writer role returns when you reopen the session after detaching.

A worker enrolled with `--allow-terminal-view` only (without `--allow-control`) can
grant view mode only.

### Coordinator container is stopped or restarted by the memory limit

The container has a hard memory limit and no swap (`install.md`, Container deployment,
Resources). If `docker inspect` shows `OOMKilled` or `docker stats --no-stream` shows
usage close to the limit, raise `BRIDGE_MEMORY_LIMIT` in `.env` and run
`docker compose up -d`. Memory grows after the first logins because each password
verification uses 19 MiB, at most two at a time (`benchmarks.md`). The same applies to
`MemoryMax=96M` in the native unit.
