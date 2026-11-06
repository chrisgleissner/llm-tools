# Coordinator API

The coordinator serves its OpenAPI 3.1 document at `/api/v1/openapi.json` and a
small local reference at `/api/docs`. API tokens are sent only in the
`Authorization` header. Create a read token for inspection, or grant `operate`
for mission and task changes; worker administration and diagnostics require
`admin`. Token secrets are shown once by `bridge-coordinator token create`.

The current version uses the durable `POST /api/v1/commands` adapter. A command
request has a caller-chosen `request_id`; retrying the same request ID returns
the journal entry rather than applying the operation again. `Idempotency-Key`
may supply that ID and takes precedence over the request body's value. If a worker is
offline, the command remains queued and the response has state `pending` and a
`Location` header for its command record.
`GET /api/v1/commands/{request_id}` retrieves its outcome. A completed command
can still report `uncertain`; clients must not treat that as success. Resource
endpoints, `Idempotency-Key`, `If-Match`, opaque cursor pagination and the
remaining CLI parity operations are being added in this follow-up.

Worker-owned mission resources take the worker name as a required `worker` query
parameter because mission IDs are unique only within a worker. `GET
/api/v1/missions/{id}`, `/tasks`, `/events` and `/operations` are thin adapters over
the typed `mission.status`, `task.list`, `mission.events` and `session.list` operations. They return
the durable command envelope; if the worker is offline, the response is `202`
with a command `Location` and can be checked with `GET /api/v1/commands/{id}`.
Read resource requests may include `wait_ms` (0 to 30000) to wait for a worker;
the default is immediate durable acceptance.
The current mutation adapters use the same command journal: `POST
/api/v1/missions/{id}/start|resume|pause|stop|cancel|continue`, `POST
/api/v1/missions/{id}/tasks`, `PATCH /api/v1/missions/{id}/tasks/{n}` and `POST
/api/v1/missions/{id}/tasks/{n}/verify`, and `POST
/api/v1/missions/{id}/tasks/{n}/pause|resume|cancel|retry`. Task creation accepts `title`, `prompt`,
`priority`, `order`, `depends_on` and `acceptance`; task patch changes exactly one
of `prompt`, `priority`, `order` or `depends_on`, and may include
`expected_revision`. Verification uses `approved` and `evidence`; rejection uses
`approved: false` and `reason`. Pass `Idempotency-Key` on any mutation to make
retries reuse the same durable command request ID.
Mission start accepts the worker's `headless`, `native_tui`, `providers` and
`routes` choices and validates the rotation on that worker before launching its
controller.
`GET /api/v1/missions/{id}/tasks/{n}` returns the task through `task.show` and,
when the worker answers synchronously, includes a strong `ETag` containing its
revision. Send that value as `If-Match` on a task mutation; it becomes
`expected_revision`, and a stale value returns `412`.
The stop action accepts `{"force":true}` to terminate only the worker-owned provider
processes and controller for that mission, matching `llm-bridge stop --force`.
The mission detail page and `llm-bridge --target` use these resource routes for
status, events, task lists and supported task/mission actions; commands without a
resource mapping continue through the low-level command endpoint while their
parity decision is tracked below.

```sh
export COORDINATOR=https://localhost:8443
export TOKEN='lbr_pat_…'              # read scope
export OPERATE_TOKEN='lbr_pat_…'    # read and operate scopes
export ADMIN_TOKEN='lbr_pat_…'      # admin scope
export WORKER=build-1
export MISSION='mission-id-from-the-list'
```

List missions, then inspect a mission’s tasks. The task operation is read-only
and requires `read`:

```sh
curl --cacert ca.pem -H "Authorization: Bearer $TOKEN" \
  "$COORDINATOR/api/v1/missions?limit=100"

curl --cacert ca.pem -H "Authorization: Bearer $TOKEN" \
  "$COORDINATOR/api/v1/missions/$MISSION/tasks?worker=$WORKER&limit=100&offset=0"

curl --cacert ca.pem -X POST -H "Authorization: Bearer $OPERATE_TOKEN" \
  -H 'Idempotency-Key: add-task-005' -H 'Content-Type: application/json' \
  -d '{"title":"Check the API","prompt":"Verify the resource route."}' \
  "$COORDINATOR/api/v1/missions/$MISSION/tasks?worker=$WORKER"

curl --cacert ca.pem -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"target":"build-1","request":{"schema_version":1,"operation":"task.list","params":{"limit":100,"offset":0},"mission_id":"mission-id-from-the-list","target":"build-1","request_id":"list-tasks-001"}}' \
  "$COORDINATOR/api/v1/commands"
```

Approve a task with evidence. Use its current revision and a new request ID;
the worker checks the revision under its write lock:

```sh
curl --cacert ca.pem -H "Authorization: Bearer $OPERATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"target":"build-1","request":{"schema_version":1,"operation":"task.verify","params":{"task_number":3,"expected_revision":2,"approved":true,"evidence":"Reviewed the result and verified the acceptance criteria."},"mission_id":"mission-id-from-the-list","target":"build-1","request_id":"approve-task-003"}}' \
  "$COORDINATOR/api/v1/commands"
```

Add a task with the same typed operation layer used by the CLI:

```sh
curl --cacert ca.pem -H "Authorization: Bearer $OPERATE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"target":"build-1","request":{"schema_version":1,"operation":"task.add","params":{"title":"Add contract coverage","prompt":"Add tests for the new contract.","priority":0,"depends_on":[],"acceptance":["The regression test fails before the fix."]},"mission_id":"mission-id-from-the-list","target":"build-1","request_id":"add-task-004"}}' \
  "$COORDINATOR/api/v1/commands"
```

Manage API tokens with an administrator session (the browser adds CSRF and Origin
checks) or a token that has `admin`. The create response includes the secret
once; the list response contains metadata only:

```sh
curl --cacert ca.pem -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"automation","scopes":["read","operate"],"expires_days":90}' \
  "$COORDINATOR/api/v1/tokens"

curl --cacert ca.pem -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$COORDINATOR/api/v1/tokens"

curl --cacert ca.pem -X DELETE -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$COORDINATOR/api/v1/tokens/automation"
```

Errors use `application/problem+json`. The stable `type` URI and `code` identify
the existing coordinator error code; `status` is the HTTP status. List APIs
currently use bounded `limit` with offset or event-cursor fields as described by
their operation. `v1` changes are intended to be additive; breaking changes require a
new version. A deprecated operation will carry a `Deprecation` header and a
documented removal date before removal.

Mutation requests with a body must use `Content-Type: application/json` (UTF-8
is accepted); other media types receive `415 unsupported-media-type`.
Bodies are limited to 1 MiB, 16 nested levels, 64 KiB UTF-8 strings, 1,000
items per array, and 128 fields per object (field names are limited to 128
bytes). These bounds are described by the generated OpenAPI request schema.
The API does not enable CORS. `OPTIONS` preflight requests receive `405` and no
`Access-Control-Allow-Origin`; `HEAD` follows the corresponding GET route.

See the full route and scope table in the [coordinator guide](coordinator.md#http-api).
