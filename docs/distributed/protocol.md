# Worker–coordinator protocol, version 1

Normative for PROTO-001..010, WORK-002/003/006/008 and SEC-002/003. The command
payloads are the typed command layer in `llm_tools/bridge_commands.py`
(`CommandRequest`, `CommandResult`); this document defines only the transport
around them.

## Transport

- The worker opens an outbound WebSocket over TLS to
  `wss://<coordinator>/api/v1/worker/connect`. The coordinator never connects
  to a worker, so workers need no listening port.
- The worker authenticates with `Authorization: Bearer <worker token>` on the
  upgrade request. The token is issued once at enrolment, stored only as a
  hash on the coordinator, and revoked by deleting the worker record. The
  worker verifies the coordinator certificate against the CA it trusted at
  enrolment.
- Each WebSocket text frame carries exactly one JSON message of at most 1 MiB.
- An `events` payload is limited to 500 events and 128 KiB of serialized event data, whichever comes first. The worker uses smaller batches under normal conditions.
  A larger frame closes the connection with code 1009.
- Terminal byte streams use a separate WebSocket per terminal session
  (`/api/v1/worker/terminal/<session>`), so a slow terminal cannot delay
  control messages (PROTO-009).

## Envelope

```json
{"v": 1, "type": "command", "id": "<message id>", "worker_id": "<worker id>",
 "generation": 7, "payload": {}}
```

| Field | Meaning |
|---|---|
| `v` | Protocol version. A peer that does not support it answers `error` with code `unsupported-version` and closes. |
| `type` | One of the message types below. Unknown types are answered with `error` code `unsupported-message`. |
| `id` | Unique message ID chosen by the sender (1–128 of `[A-Za-z0-9_.-]`). |
| `worker_id` | Stable worker identity (WORK-003). |
| `generation` | Connection generation issued by the coordinator in `welcome` (PROTO-010). `0` in `hello`. |
| `payload` | Typed body for `type`; unknown payload fields are rejected. |

Version negotiation: the worker lists the versions it speaks in `hello`
(`protocol_versions`) and the coordinator answers with the selected version in
`welcome`. When there is no common version, the coordinator closes the connection
with WebSocket code 1002 and reason `unsupported version`. Only version 1 exists.
Versions are whole numbers; there is no minor version. A peer from an adjacent release
interoperates when it also lists a version the other side speaks (a newer worker lists
`[1, 2]`): the coordinator selects the highest common one. The Python parser
(`bridge_protocol.negotiate_version`, applied to every decoded `hello`) rejects a list with no
common version with `unsupported-version`, naming both lists, and a list that is not
whole numbers with `invalid-message`.

## Message types

| Type | Direction | Payload |
|---|---|---|
| `hello` | worker → coordinator | `worker_id`, `name`, `software_version`, `protocol_versions`, `platform`, `capabilities` (the `operation_catalog()` output), `missions` (mission summaries, see below), `event_cursor` (highest local event id). |
| `welcome` | coordinator → worker | `generation`, `protocol_version`, `event_cursor` (highest event id the coordinator has stored for this worker). |
| `command` | coordinator → worker | A `CommandRequest` object. |
| `command.ack` | worker → coordinator | `request_id`, `status`: `received` or `duplicate`. Acknowledges delivery only. |
| `command.result` | worker → coordinator | A `CommandResult` object. |
| `events` | worker → coordinator | `after`, `through`, `events` (ascending by id), optional `mission_cursor`, changed `missions` summaries and `deleted_missions` (mission tombstones, see Mission deletion). A summary-only update has an empty event list and `through == after`; workers use this bounded path to replicate a newly pending provider question that has no Bridge database event. |
| `events.nack` | coordinator → worker | `expected_after`: the coordinator's stored cursor; the worker resends from there. |
| `ping` / `pong` | both | `{}`; sent every 20 s, connection closed after 60 s silence. |
| `error` | both | `code`, `message`, optional `details`, optional `in_reply_to`. An `error` is never answered with another message. |

A `hello` with more than 5000 missions gets an `error` with code `invalid-message`; the connection stays
open until the hello deadline. With summaries of about 300 bytes, the 1 MiB message limit is reached at
roughly 3000 missions, before the 5000 limit.

The coordinator keeps at most 10 000 mission summaries per worker. A `hello` or an `events` upsert is never
refused for this: when a worker exceeds the limit, the coordinator drops that worker's oldest finished
summaries (by `updated_at`) first. If the remaining summaries are all active, it does not index the newest
excess rows and reports the warning `missions-over-cap` (with the worker name and a fix) in
`GET /api/v1/diagnostics`. The warning clears when a later update fits.

Fatal refusals. After the error codes `worker-id-mismatch` and `unsupported-version`, or the close reason
`worker revoked`, reconnecting cannot succeed until the operator acts. The Python worker stops, logs one error
that names the fix and exits with status 1; `llm-bridge worker status` then shows the connection as `refused`
with the code and the fix. Every other close or error leads to a reconnect with jittered exponential
backoff. The backoff returns to its start value only after a `welcome`, not after any other message.

## Mission summaries

A mission summary is a small object the worker sends in `hello` and in the `missions` list of
an `events` message. The coordinator stores the newest summary of each mission and returns it
in `mission` of every `GET /api/v1/missions` entry, so a console can draw its lists without
asking the worker. Keys:

| Key | Meaning |
|---|---|
| `id`, `name`, `status`, `folder`, `updated_at` | Identity and lifecycle. `folder` is the basename of the working directory. |
| `task_counts` | Task count per task status (`{"COMPLETED": 36, "QUEUED": 4}`); states with no tasks are absent. |
| `current_task` | `{ordinal, title}` of the task that is running or next, or `null`. Live missions only; `title` is at most 200 characters. |
| `station`, `model` | The provider and model that are working, or `null`. Live missions only. |
| `needs_kind`, `needs_tasks` | What needs the operator, as a kind (`approval`, `capacity`, `provider`, ...) and the number of tasks involved; `null` and `0` when nothing does. Live missions only. |
| `needs_you` | Present on a running native mission: the provider's pending question (`{kind, message, command}`, message at most 2 KiB) or `null`. |

"Live" means `CREATED`, `RUNNING`, `PAUSED` or `STOPPING`. A summary is about 300 bytes and
never contains prompt, evidence or terminal text. The worker sends the summary of a mission
when an event of that mission is in a batch, so the counts follow the replicated events; it does
not send summaries on a timer, apart from the provider-question check described under event
replication.

## Command delivery (PROTO-003, PROTO-004, WORK-006, WORK-007)

1. The coordinator stores the command in its journal with state `pending`
   before sending it. It sends a mutating command again after every
   reconnect until it has a `command.result`. A read-only command is sent
   only while the worker is connected and is never sent again: see
   "Read-only commands and offline workers".
2. The worker answers `command.ack` immediately. An ack never implies
   execution.
3. The worker executes the request through `bridge_commands.execute`. A
   mutating request's `request_id` is the durable `commands.id` in the worker
   database, so a redelivered request returns the stored outcome and does not
   repeat the effect. Results of read operations are not stored; a repeated
   read is simply executed again.
4. The worker sends `command.result`. The coordinator stores it and marks the
   journal entry `done`. A result for an unknown or already completed request
   is ignored.
5. A mutating command that stays `pending`, `sent` or `acked` for 10 seconds
   without a result is sent again on the same connection (checked every 5
   seconds), because a `command` or `command.result` frame can be lost on a
   live connection. A replay after a reconnect restarts the same 10 second
   clock. This adds no message type and no field (FIND-003).
6. The worker deduplicates every mutating request by `request_id`, whatever the
   operation does internally. It records the final `command.result` in a small
   private SQLite file, `<state dir>/worker/outcomes.sqlite3` (mode 0600, newest
   10,000 results and at most 7 days, pruned on every write), before it sends the
   result. A request id with a recorded result is answered with
   `command.ack` status `duplicate` and the stored result, and is not executed
   again. The worker handles messages of one connection one at a time, so a copy
   that arrives while the first is still running is handled after it finishes and
   finds its result. Remaining window: a crash after the effect and before the
   result is recorded can run the command once more after the restart. Reads are
   not recorded and run again.

### Read-only commands and offline workers (DOG-021)

A read-only command (an operation whose advertised `mutating` is false) is useful only while a
client waits for its answer. Replaying such commands after a reconnect sent 230 results of about
81 KB each (18.6 MB in 30 s) to the coordinator, so reads are handled differently from writes:

- The coordinator refuses a read for a worker that is not connected at once, with HTTP 503 and
  error code `worker-offline`. It does not journal or queue it. A read whose `request_id` is already
  in the journal returns the stored entry, as before.
- When a worker disconnects, and again before the coordinator registers its next connection,
  every unfinished read of that worker is finished with the failure result
  `{"outcome": "failed", "error": {"code": "worker-offline", ...}}`. A client that waits on the
  journal entry sees that failure; the read is not delivered after the reconnect.
- Writes are unchanged: they are queued while the worker is offline (202, `state: pending`),
  delivered in order after every reconnect and answered exactly once.

### Result sizes

Results of reads are bounded because they travel over the worker link and are stored in the
coordinator's command journal:

- `mission.status` returns the status projection without task prompts, evidence or event bodies:
  at most `task_limit` (default 5, at most 50) tasks, unfinished ones first, each with identity,
  title, status, priority, revision and timestamps but no prompt; `events` and `operator_events` are
  empty unless `event_limit` (at most 20) asks for the newest ones; the mission object has no frozen
  tool configuration and at most 160 characters of the objective; every string is clipped to 2 KiB and
  every list to 20 items. The result is `bounded: true` and about 5 KB for a mission with a few dozen
  tasks (the full projection was 125 KB measured). `task_total`, `task_counts`
  and `event_page` still describe the whole mission. `prompt_chars` (at most 2000) adds that many
  characters of each listed task's `prompt` and `original_prompt`.
- `task.list` returns whole tasks unless `prompt_chars` clips their prompt fields. The web console
  passes it. `task.show` returns one whole task, which is the way to read a full prompt.
- `full: true` returns the whole local projection (the worker's own `llm-bridge status` sends it). The coordinator's resource route does not pass `full`, so a browser cannot ask for it; a client that posts `/commands` can, and then pays the size.

Outcomes are `ok`, `accepted`, `applied`, `rejected`, `failed` and `uncertain`.
`uncertain` means an unexpected failure happened during a mutation; the
coordinator shows it as such and never retries it under a new request ID.

## Event replication (PROTO-005, WORK-008)

Worker event ids are the `events.id` values of the worker database: strictly
increasing across all missions. Pruned plumbing events leave legitimate holes,
so contiguity of ids is not required. The coordinator keeps one cursor per
worker. A batch is accepted only when its `after` equals the stored cursor;
otherwise the coordinator answers `events.nack` and the worker resends from
`expected_after`. On `welcome` the worker resumes from the returned cursor.

The coordinator sends nothing for an accepted batch. A worker therefore keeps
at most one `events` message in flight and sends a `ping` immediately after it.
The coordinator handles and answers one connection's messages in order, so the
`pong` for that ping shows the batch was processed, and an `events.nack` for the
batch arrives before that pong. The worker sends the next batch only after that
pong. This needs no message beyond the ones above, so it works with every
protocol v1 coordinator.

A worker honours an `events.nack` only while a batch is in flight and only when
`expected_after` differs from that batch's `after`. Otherwise the nack answers a
batch that has already been replaced, and honouring it would resend data the
coordinator has just accepted. A nack that is honoured replaces the in-flight
batch, so one batch causes at most one rewind. An `expected_after` beyond the
batch's `through` moves the worker forward to that cursor.

Batches are bounded: at most 25 events and 16 KiB of serialized events (one
larger event is still sent alone), and event traffic is held to a sustained
20 KiB/s. A batch may carry fewer events than the ids it spans: the worker does
not replicate `controller-heartbeat` events or any `capacity-refresh` that a
newer refresh of the same mission has superseded. These are bookkeeping that
the controller writes every few seconds; the coordinator does not interpret
them and the console only lists them as hidden plumbing. The skipped ids still
advance `through`, and a batch with no events and `through > after` is a
valid way to move the cursor past them.
The worker sends ordinary mission summaries (see "Mission summaries") only for mission IDs in an event
batch. Every five seconds it checks active native missions for provider
questions, and sends only summaries whose bounded `needs_you` value changed;
it does not resend the full inventory with every task event or heartbeat. The
coordinator applies summary upserts and cursors atomically. `needs_you` contains
only the existing status projection's provider message and answer command, not
terminal content or transcript records.

## Mission deletion (PRD-038)

`llm-bridge mission delete` removes a mission from the worker (database rows, runtime directory with provider
run logs and verifier files, lock files, the socket of a dead station) and from the coordinator. The change is
additive to protocol v1: one new operation, `mission.delete`, and one new optional field of `events`.

**Operation.** `mission.delete` is a mutating operation with the parameter `confirm` (boolean). The worker
rejects a request without `confirm: true` with error code `confirmation-required`. A mission that never started
or has ended or been stopped (`CREATED`, `COMPLETED`, `FAILED`, `CANCELLED`, `STOPPED`) is deleted and the result is
`{"status": "DELETED", "mission_id": <full id>, "name", "previous_status", "deleted_at", "removed": {...}}` with
outcome `applied`. `RUNNING`, `PAUSED` and `STOPPING`, a mission whose controller lock is held and a mission with a
live provider station are rejected with error code `conflict` and nothing changes. A mission that does not exist
(an ID or prefix that matches nothing, or a repeated delete) is rejected with `not-found`; a prefix that matches
several missions is also `not-found`. The operation is advertised only by workers enrolled with `--allow-control`,
like the other mutating operations.

**Tombstone.** The same transaction that deletes the rows records a tombstone in the worker database (table
`mission_tombstones`: mission ID, name, final status, deletion time, replication time; created by the first deletion,
so the Bridge schema number does not change; at most 1000 rows and 90 days are kept). The worker replicates pending
tombstones in an `events` message that carries them alone:

```json
{"after": 812, "through": 812, "events": [], "deleted_missions": ["<mission id>", "..."]}
```

`after` and `through` are the worker's current cursor, so the message moves no cursor. It holds at most 100 mission
IDs (1-128 characters of `[A-Za-z0-9_.-]`). Like every `events` message it is the one batch in flight, and the
worker marks the tombstones replicated when the `pong` that follows the message arrives. A tombstone that is not
confirmed (connection lost, `events.nack`) is sent again after the next welcome.

**Coordinator.** For each ID the coordinator, in one transaction, inserts a tombstone for the pair (worker, mission
ID), deletes its mission summary and every stored event whose `mission_id` is that ID. It then writes the audit entry
`mission.delete` (only the first time) and tells browsers to read the mission list again. A repeated tombstone, and a
tombstone for a mission the coordinator never stored, are accepted and change nothing. While a tombstone exists the
coordinator drops, without error, every event and every mission summary of that mission in later `events` messages
(the cursor still advances past them). This is what keeps a batch that was already in flight, or one that an offline
worker had queued, from bringing the mission back. A `hello` that lists the mission removes its tombstone, because
the worker's own list is authoritative (for example after the worker restored an older database backup). Tombstones are
kept for 90 days and at most 10 000 per worker; the oldest go first.

The coordinator also applies the deletion when it sees the result of a `mission.delete` command: for outcome
`applied` it uses the `mission_id` of the result, and for a `not-found` rejection it uses the request's mission ID
when that is exactly a stored mission. Consoles therefore see the mission disappear without waiting for the next
replication. Any other result (`conflict`, `confirmation-required`) leaves the coordinator's copy as it is.

**Older coordinators.** A coordinator from before this change rejects the unknown `deleted_missions` field with an
`error` message (`invalid-message`, `in_reply_to` the message ID). The worker keeps the tombstones pending, does
not mark them replicated and stops offering them for the rest of that connection. Nothing else is lost, because
the tombstone message carries no events. Upgrade the coordinator before the workers; until then the coordinator keeps
listing the deleted mission as stale.

## Generation fencing (PROTO-010)

The coordinator increments and persists a per-worker generation for every
accepted connection and closes any older connection of that worker. The
worker persists the highest generation it has seen and rejects a `command`
whose `generation` is lower, with `error` code `stale-generation`. A `welcome`
whose generation is not higher than that value is answered with
`stale-generation` and the worker closes the connection and reconnects. Enrolling
with a new worker token resets the stored value, because the coordinator starts a
new registration's generations again.

## Concurrency (PROTO-006)

Task mutations accept an optional `expected_revision`. The worker compares it
with the task's current revision inside the same SQLite write transaction as
the effect and rejects a mismatch with error code `conflict`.

## Capabilities (PROTO-007)

`hello.capabilities` lists the operations the worker supports. The coordinator
refuses a request for an operation the target worker did not advertise with
`unsupported-operation`, without sending it. Workers on different software versions
can therefore be connected at the same time.

## Terminal sessions (PROTO-009, SEC-006, WEB-007, WEB-008)

Terminal bytes never travel on the control connection. The control connection
only opens and closes sessions:

| Type | Direction | Payload |
|---|---|---|
| `terminal.open` | coordinator → worker | `session_id` (1–128 of `[A-Za-z0-9_.-]`), `mission_id`, `mode` (`view` or `write`), `cols`, `rows` (1–1000). |
| `terminal.close` | coordinator → worker | `session_id`. |
| `terminal.status` | worker → coordinator | `session_id`, `state` (`opened`, `refused`, `closed`), `mode` (the highest mode the worker allows this session; the live role is reported in `s` frames), `reason` (text, may be empty). |

A worker shares terminals only when enrolled with `--allow-control` (view and
write) or `--allow-terminal-view` (view only); otherwise it refuses
`terminal.open`. On `terminal.open` the worker validates the mission, decides the granted mode
and opens a second WebSocket to
`wss://<coordinator>/api/v1/worker/terminal/<session_id>` with the same worker
token. The coordinator accepts that connection only for a session it opened for
that worker and that has not been attached yet. Frames on it are JSON text:

| Frame | Direction | Meaning |
|---|---|---|
| `{"t":"o","d":"<base64>"}` | worker → coordinator | Output bytes of the provider pane. The first `o` frame is a full screen snapshot: a clear-screen sequence, the pane rows joined by CRLF without a trailing newline, then `CSI row;col H` placing the cursor where the pane's cursor is. |
| `{"t":"s","cols":N,"rows":N,"mode":"view"\|"write","reason":"..."}` | worker → coordinator | Pane size and granted mode; sent first and whenever they change. |
| `{"t":"x","reason":"..."}` | worker → coordinator | Session ended; the worker closes the socket. |
| `{"t":"i","d":"<base64>"}` | coordinator → worker | Keyboard or paste input, at most 64 KiB decoded. Dropped by the worker unless the session holds the writer role. |
| `{"t":"r","cols":N,"rows":N}` | coordinator → worker | Resize request from the writer (1–1000 columns and rows; the coordinator rejects other values); applied only when no local tmux client is attached to the station. |

Single writer: the worker grants `write` to at most one session per mission,
and only while no local tmux client is attached to that station. Any other
request is granted `view` with a reason. Losing the writer role (a local
client attached) is reported with an `s` frame; the worker also checks for a
local client before typing each input frame. Viewers are unlimited.

Bounds: the worker reads at most 64 KiB per frame and its sends block, so a
slow coordinator slows reading. The tmux control client runs with
`pause-after=5`: when it falls 5 s behind, tmux pauses the pane's output and
the worker resumes it and sends a fresh snapshot instead of the backlog.
The coordinator keeps at most 256 KiB per browser viewer and closes a viewer
that falls further behind (close code 1013); the browser reconnects and
receives a fresh snapshot.

## Byte budgets (DOG-020, DOG-021)

With 3 running and 31 finished missions, the worker link carries under 2 KB/s in steady state, and
the first seconds after a reconnect under 200 KB, including the `hello` (about 17 KB for 34 missions)
and every queued write. The coordinator counts messages and bytes by direction and message type since
it started (`link_bytes` in `GET /api/v1/diagnostics`: `worker_in`, `worker_out`, `browser_out`). The
counters are in memory and are not sent anywhere. `tests/test_distributed_link_budget.py` measures the
budgets with a real coordinator and worker.
