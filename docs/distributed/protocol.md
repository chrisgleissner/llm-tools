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
| `events` | worker → coordinator | `after`, `through`, `events` (ascending by id), optional `mission_cursor` and changed `missions` summaries. A summary-only update has an empty event list and `through == after`; workers use this bounded path to replicate a newly pending provider question that has no Bridge database event. |
| `events.nack` | coordinator → worker | `expected_after`: the coordinator's stored cursor; the worker resends from there. |
| `ping` / `pong` | both | `{}`; sent every 20 s, connection closed after 60 s silence. |
| `error` | both | `code`, `message`, optional `details`, optional `in_reply_to`. An `error` is never answered with another message. |

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
