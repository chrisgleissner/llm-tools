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

## Message types

| Type | Direction | Payload |
|---|---|---|
| `hello` | worker → coordinator | `worker_id`, `name`, `software_version`, `protocol_versions`, `platform`, `capabilities` (the `operation_catalog()` output), `missions` (id, name, status, cwd basename, updated_at), `event_cursor` (highest local event id). |
| `welcome` | coordinator → worker | `generation`, `protocol_version`, `event_cursor` (highest event id the coordinator has stored for this worker). |
| `command` | coordinator → worker | A `CommandRequest` object. |
| `command.ack` | worker → coordinator | `request_id`, `status`: `received` or `duplicate`. Acknowledges delivery only. |
| `command.result` | worker → coordinator | A `CommandResult` object. |
| `events` | worker → coordinator | `after`, `through`, `events` (ascending by id). |
| `events.nack` | coordinator → worker | `expected_after`: the coordinator's stored cursor; the worker resends from there. |
| `ping` / `pong` | both | `{}`; sent every 20 s, connection closed after 60 s silence. |
| `error` | both | `code`, `message`, optional `details`, optional `in_reply_to`. |

## Command delivery (PROTO-003, PROTO-004, WORK-006, WORK-007)

1. The coordinator stores the command in its journal with state `pending`
   before sending it, and sends it again after every reconnect until it has a
   `command.result`.
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

## Generation fencing (PROTO-010)

The coordinator increments and persists a per-worker generation for every
accepted connection and closes any older connection of that worker. The
worker persists the highest generation it has seen and rejects a `command`
whose `generation` is lower, with `error` code `stale-generation`.

## Concurrency (PROTO-006)

Task mutations accept an optional `expected_revision`. The worker compares it
with the task's current revision inside the same SQLite write transaction as
the effect and rejects a mismatch with error code `conflict`.

## Capabilities (PROTO-007)

`hello.capabilities` lists the operations the worker supports. The coordinator
refuses a request for an operation the target worker did not advertise with
`unsupported-operation`, without sending it.
