# Coordinator API

The coordinator serves its OpenAPI 3.1 document at `/api/v1/openapi.json` and a
small local reference at `/api/docs`. API tokens are sent only in the
`Authorization` header. Create a read token for inspection, or grant `operate`
for mission and task changes; worker administration and diagnostics require
`admin`. Token secrets are shown once by `bridge-coordinator token create`.

`GET /api/v1/workers` and `GET /api/v1/workers/{name}` include the hostname
reported at enrolment and the peer address of the active worker connection. The
address is null while the worker is offline; forwarded headers do not affect it.

The mission index may include `mission.needs_you` for an online native worker
when its provider is waiting on a structured question. It contains the bounded
question and choice text, plus the local CLI answer command; use the linked
mission detail to open the provider terminal and answer it. No raw terminal
content or surrounding transcript is returned.

For native Codex sessions, mission status can include the bounded text and
choices of the currently expanded queued question. When the exact session is
known, Bridge reads the provider's structured question record; the local
terminal capture parser is limited to the visible `Queued follow-up inputs`
block. For Claude Code, Bridge reads only the exact session's documented
`AskUserQuestion` tool input and drops it when the matching `tool_result`
arrives. Neither path returns the surrounding transcript or captured screen.
The CLI/TUI and web console show the question and choices; take control of the
mission terminal and use the provider's own UI to answer. Terminal input stays
on the scoped WebSocket.

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
The mission task, event and operation list routes accept `limit` and an opaque
`cursor`; the command result includes `next_cursor` when another page is
available. Use that value unchanged with the same worker, mission, resource and
limit. Cursors are authenticated and encrypted, and reject altered or mismatched
queries. Legacy numeric `offset` (tasks and operations) and `after` (events)
remain available but cannot be combined with a cursor. The default page sizes
are 50 tasks, 50 events and 100 operations; the maximum is 1000.
The stop action accepts `{"force":true}` to terminate only the worker-owned provider
processes and controller for that mission, matching `llm-bridge stop --force`.
The mission detail page and `llm-bridge --target` use these resource routes for
status, events, task lists and supported task/mission actions; commands without a
resource mapping continue through the low-level command endpoint while their
parity decision is tracked below.

## CLI parity exclusions

`llm-bridge screen --mission ID` is a local read-only snapshot of the provider
pane. It has no REST resource because returning raw terminal content through the
API is prohibited; remote operators can use the scoped `/api/v1/terminal`
WebSocket to watch the pane.

```sh
export COORDINATOR=https://localhost:8443
export TOKEN='lbr_pat_…'              # read scope
export OPERATE_TOKEN='lbr_pat_…'    # read and operate scopes
export ADMIN_TOKEN='lbr_pat_…'      # admin scope
export WORKER=build-1
export MISSION='mission-id-from-the-list'
```

List missions, then inspect a mission’s tasks. The task operation is read-only
and requires `read`. Mission lists default to 100 rows and cap pages at 500;
follow `next_cursor` as the next request's `cursor`. Cursors are opaque,
authenticated and encrypted, and bound to the original filters and sort order. They are
rejected if changed or reused with different filters. `offset` remains available
for compatibility but cannot be combined with `cursor`.

```sh
curl --cacert ca.pem -H "Authorization: Bearer $TOKEN" \
  "$COORDINATOR/api/v1/missions?limit=100"

# If next_cursor is non-null, use it unchanged for the next page:
curl --cacert ca.pem -H "Authorization: Bearer $TOKEN" \
  "$COORDINATOR/api/v1/missions?limit=100&cursor=$NEXT_CURSOR"

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
the existing coordinator error code; `status` is the HTTP status. List APIs use
stable ordering, bounded `limit`, and either opaque page cursors or event
cursors as described by their operation. Mission-list cursors use authenticated
encryption with a coordinator database key and expose no filter or offset
claims. `v1` changes are
intended to be additive; breaking changes require a
new version. A deprecated operation will carry a `Deprecation` header and a
documented removal date before removal.

Mutation requests with a body must use `Content-Type: application/json` (UTF-8
is accepted); other media types receive `415 unsupported-media-type`.
Bodies are limited to 1 MiB, 16 nested levels, 64 KiB UTF-8 strings, 1,000
items per array, and 128 fields per object (field names are limited to 128
bytes). These bounds are described by the generated OpenAPI request schema.
The API does not enable CORS. `OPTIONS` preflight requests receive `405` and no
`Access-Control-Allow-Origin`; `HEAD` follows the corresponding GET route.
Cookie-authenticated browser requests also pass a Fetch Metadata check: only
`Sec-Fetch-Site: same-origin` requests are accepted, except a top-level GET
navigation (`navigate` / `document`) to a console HTML page. Cross-site API,
asset and WebSocket requests receive a problem response. This supplements exact
Origin and CSRF checks; it does not replace them. Requests without Fetch Metadata
remain available to non-browser clients and still need normal authentication.

Bearer-authenticated mutations are limited to 30 requests per token and 120 per
client address in a rolling 60-second window. The limit applies after authentication
and scope checks; rejected requests return `429` with `Retry-After`. Each bearer
request is audited with token name, granted scopes, method, path, client address
and request ID when supplied. The bearer secret is never written to the audit log.
The in-memory counters reset when the coordinator restarts.

See the full route and scope table in the [coordinator guide](coordinator.md#http-api).

## API-015 parity table

Each parser leaf and option has its own decision. “Typed command” means the worker executes the existing `bridge_commands` operation; REST resource paths are adapters to those operations. An exclusion names why the CLI surface has no remote parity and the available replacement.

| CLI leaf | REST resource or typed command mapping / exclusion |
|---|---|
| `help` | Exclusion: offline help has no state/API operation; replacement is `llm-bridge help` locally. |
| `view` | Exclusion: opens a local filesystem artifact; replacement is local `llm-bridge view --file`. |
| `mission create` | Typed `mission.create`; resource `POST /api/v1/missions` with `worker` in the JSON body; worker requires `--allow-create` and a cwd inside an enrolled `--allow-root`. Local-only creation options return `unsupported-remote-option`. |
| `mission list` | Typed `mission.list` via `POST /api/v1/commands`; coordinator resource equivalent is `GET /api/v1/missions`. |
| `mission settings` | Typed `mission.settings.get`; resource `GET /api/v1/missions/{id}/settings`. |
| `mission set` | Typed `mission.settings.patch`; resource `PATCH /api/v1/missions/{id}/settings`. |
| `mission colour` | Typed `mission.settings.patch` field `colour`; resource `PATCH /api/v1/missions/{id}/settings`. |
| `mission color` | Alias of `mission colour`; same typed settings field. |
| `status` | Typed `mission.status`; resource `GET /api/v1/missions/{id}`. |
| `screen` | Exclusion: raw pane snapshot is intentionally not exposed; replacement is scoped `GET /api/v1/terminal` WebSocket. |
| `agents` | Typed `mission.agents`; mission-filtered reads use `GET /api/v1/missions/{id}/agents`, while all-mission reads use `GET /api/v1/workers/{name}/agents`. |
| `suspend-until-credits` | Typed `mission.suspend_until_credits`; admin-scoped resource `POST /api/v1/missions/{id}/suspend-until-credits`; worker requires `--allow-power`. |
| `task add` | Typed `task.add`; resource `POST /api/v1/missions/{id}/tasks`. |
| `task list` | Typed `task.list`; resource `GET /api/v1/missions/{id}/tasks`. |
| `task show` | Typed `task.show`; resource `GET /api/v1/missions/{id}/tasks/{n}` (number selector only). |
| `task amend` | Typed `task.amend`; resource `PATCH /api/v1/missions/{id}/tasks/{n}`. |
| `task priority` | Typed `task.priority`; resource `PATCH /api/v1/missions/{id}/tasks/{n}`. |
| `task reorder` | Typed `task.reorder`; resource `PATCH /api/v1/missions/{id}/tasks/{n}`. |
| `task dependencies` | Exclusion: dependency-add operation has no REST resource adapter and remote typed mapping is not implemented; run locally. |
| `task pause` | Typed `task.pause`; resource `POST /api/v1/missions/{id}/tasks/{n}/pause`. |
| `task resume` | Typed `task.resume`; resource `POST /api/v1/missions/{id}/tasks/{n}/resume`. |
| `task cancel` | Typed `task.cancel`; resource `POST /api/v1/missions/{id}/tasks/{n}/cancel`. |
| `task retry` | Typed `task.retry`; resource `POST /api/v1/missions/{id}/tasks/{n}/retry`. |
| `task verify` | Typed `task.verify`; resource `POST /api/v1/missions/{id}/tasks/{n}/verify`. |
| `task reject` | Typed `task.reject`; resource `POST /api/v1/missions/{id}/tasks/{n}/verify` with `approved:false`. |
| `proposal create` | Typed `proposal.create`; resource `POST /api/v1/missions/{id}/proposals`. Source operation is checked by the worker. |
| `proposal list` | Typed `proposal.list`; resource `GET /api/v1/missions/{id}/proposals`. |
| `proposal accept` | Typed `proposal.accept`; resource `POST /api/v1/missions/{id}/proposals/{n}/accept`. |
| `proposal reject` | Typed `proposal.reject`; resource `POST /api/v1/missions/{id}/proposals/{n}/reject`. |
| `events` | Typed `mission.events`; resource `GET /api/v1/missions/{id}/events`. |
| `start` | Typed `mission.start`; resource `POST /api/v1/missions/{id}/start`. |
| `pause` | Typed `mission.pause`; resource `POST /api/v1/missions/{id}/pause`. |
| `resume` | Typed `mission.resume`; resource `POST /api/v1/missions/{id}/resume`. |
| `continue` | Typed `mission.continue`; resource `POST /api/v1/missions/{id}/continue`. |
| `stop` | Typed `mission.stop`; resource `POST /api/v1/missions/{id}/stop`. |
| `cancel` | Typed `mission.cancel`; resource `POST /api/v1/missions/{id}/cancel`. |
| `attach` | Exclusion: attaches a local tmux station; replacement is local attach or remote terminal WebSocket for viewing. |
| `detach` | Exclusion: detaches a local tmux client; no remote station-control API; run locally. |
| `missions` | Exclusion: interactive picker/station switching is local; use `GET /api/v1/missions` to list remotely. |
| `gc` | Typed `worker.gc`; operate-scoped resource `POST /api/v1/workers/{name}/gc`. |
| `backup` | **API-016 incomplete:** `backup --target` is explicitly rejected. The worker only has an outbound WebSocket carrying durable `command` and `command.result` messages; the coordinator journals both request and result JSON. There is no ephemeral worker rendezvous, binary frame, or download channel. Returning bounded chunks as command results would therefore persist prompt/evidence bytes in the coordinator journal. A safe implementation needs a protocol extension for authenticated, bounded, non-journaled streaming with cancellation and cleanup. Until then run backup locally on the worker and transfer it outside the coordinator. |
| `restore` | Typed `worker.restore`; admin-scoped resource `POST /api/v1/workers/{name}/restore`; worker requires `--allow-restore` and an input file inside an enrolled `--allow-root`. Restore refuses while controllers run and keeps a pre-restore backup. |
| `worker enroll` | Exclusion: writes local worker credentials; coordinator-side worker creation is `POST /api/v1/workers` by an admin. |
| `worker status` | Exclusion: reads this host enrolment; no remote equivalent; run locally. |
| `worker run` | Exclusion: starts the outbound worker service; run locally on enrolled host. |
| `worker audit` | Typed `worker.audit`; admin-scoped resource `GET /api/v1/workers/{name}/audit`. |
| `worker install-service` | Exclusion: installs local systemd unit; run locally. |
| `worker uninstall-service` | Exclusion: removes local systemd unit; run locally. |
| `worker unenroll` | Exclusion: deletes local worker credentials; no remote route; run locally. |
| `worker list` | REST `GET /api/v1/workers` (configured coordinator; remote CLI helper). |
| `remote login` | Exclusion: stores local API credentials; no API operation; configure this client locally. |
| `remote status` | Exclusion: reads local remote-client configuration; run locally. |
| `remote logout` | Exclusion: deletes local remote-client configuration; run locally. |
| `finish` | Typed `mission.finish`; operate-scoped resource `POST /api/v1/missions/{id}/finish`; the worker checks its native provider state and sends the provider's known exit command. |
| `doctor` | Typed `worker.doctor`; read-scoped resource `GET /api/v1/workers/{name}/doctor` returns bounded diagnostics without host paths. |
| `theme` | Exclusion: previews local terminal colors; no REST operation. |
| `console` | Exclusion: opens local curses control station; remote web UI is the replacement. |
| `control` | Exclusion: opens local curses control station; remote web UI is the replacement. |
| `usage` | Exclusion: embeds local `llm-usage` data; no coordinator usage route. |
| `station` | Exclusion: selects local station; remote terminal view is `GET /api/v1/terminal` WebSocket. |
| `rotation` | Typed `mission.rotation.get` / `mission.rotation.put`; resources `GET` / `PUT /api/v1/missions/{id}/rotation`. |
| `steer` | Typed `mission.steer`; operate-scoped resource `POST /api/v1/missions/{id}/steer`; supports every CLI kind. Prompt files are read client-side and prompt text is sent in the typed request. |
| `_station-gc` | Exclusion: private internal tmux cleanup command; never remote. |
| `_controller` | Exclusion: private internal controller launch command; worker runtime owns it. |

| CLI option | REST field/transport mapping or exclusion |
|---|---|
| `--file` | Local prompt/artifact input; for remote `task add`/`amend`, prompt text is sent as typed operation `prompt` (file is read client-side). |
| `--follow` | Local file-follow behavior; no API equivalent. |
| `--acceptance` | `task.add` or `mission.create` resource body `acceptance`. |
| `--auto-continue` | Local mission configuration; `mission create --target` returns the typed `unsupported-remote-option` result. |
| `--capacity-refresh-seconds` | Local controller setting; remote mission creation returns `unsupported-remote-option`. |
| `--command-template` | Local test/custom launch override; remote mission creation returns `unsupported-remote-option`. |
| `--cwd` | `mission.create` resource body `cwd`; worker resolves it and enforces enrolled allowed roots. |
| `--headless` | `mission.start` params and `/start` body field `headless`. |
| `--include-config` | Exclusion: local-only mission creation response option; remote create returns `unsupported-remote-option`. |
| `--json` | Client-side output format; applies to typed command/resource responses, not a request field. |
| `--log-max-bytes` | Local provider log retention setting; remote mission creation returns `unsupported-remote-option`. |
| `--log-retained-runs` | Local provider log retention setting; remote mission creation returns `unsupported-remote-option`. |
| `--log-rotations` | Local provider log retention setting; remote mission creation returns `unsupported-remote-option`. |
| `--name` | Local mission/worker naming or remote `mission.create`/worker admin body. |
| `--native` | `mission.start` params or `mission.create` body `native_tui`. |
| `--prompt` | `task.add`/`task.amend` or `mission.create` typed `prompt` field; local file read if `--prompt-file`. |
| `--prompt-file` | Read client-side then map text to typed `prompt`; file path is never sent. |
| `--providers` | `mission.start` params/body or `mission.create` body `providers`. |
| `--routes` | `mission.start` params/body or `mission.create` body `routes`. |
| `--teams` | `mission.create` body `teams`; mission edit maps to `mission.settings.patch`. |
| `--token-spend` | `mission.create` body `token_spend`; mission edit maps to `mission.settings.patch`. |
| `--trust-folder` | `mission.create` body `trust_folder`; worker only accepts a cwd inside its enrolled real-path roots. |
| `--verifier-arg` | Local verifier configuration; remote mission creation returns `unsupported-remote-option`. |
| `--verifier-file` | Local verifier tracking; remote mission creation returns `unsupported-remote-option`. |
| `--verifier-timeout` | Local verifier execution setting; remote mission creation returns `unsupported-remote-option`. |
| `--mission` | Mission selector populates `mission_id` in typed request or `worker`-qualified mission REST path. |
| `--suspend-when-rate-limited` | `mission.settings.patch` field `suspend_when_rate_limited`. |
| `--all-missions` | Local inspection filter; no remote typed operation. |
| `--all` | Remote task-list `--all` is explicitly refused; use paged `--limit` and `--offset` on `GET .../tasks`. |
| `--limit` | Maps to list-operation `limit` or resource query `limit` for tasks/events/proposals. |
| `--offset` | Maps to typed `task.list` offset / resource query offset; coordinator mission index uses cursor pagination instead. |
| `--task` | Task number maps to `task_number` and resource `{n}` selector. |
| `--task-id` | `mission.steer` typed field `task_id`; task resource adapters still require numeric `{n}`. |
| `--title` | `task.add` operation/resource body `title`. |
| `--priority` | `task.add` body or `task.priority` typed operation/resource patch field `priority`. |
| `--order` | `task.add` body or `task.reorder` typed operation/resource patch field `order`. |
| `--depends-on` | Exclusion remotely: task-add dependency field is refused; `task dependencies` is local-only. Add dependencies locally. |
| `--value` | Maps to `task.priority` (`priority`) or `task.reorder` (`order`) typed operation and PATCH resource. |
| `--add` | Exclusion: `task dependencies` has no remote operation; run locally. |
| `--evidence` | `task.verify` typed operation/resource body `evidence`. |
| `--approve` | `task.verify` typed operation/resource body `approved`. |
| `--reason` | `task.reject` and `proposal.reject` typed field `reason`. |
| `--operation` | `proposal.create` source operation ID; worker validates source operation. |
| `--status` | Proposal list filter maps typed `proposal.list` status. |
| `--after` | Maps typed `mission.events.after`/event cursor or `proposal.list.after` as applicable. |
| `--client` | Local tmux client selection; no REST parameter. |
| `--force` | `mission.stop` typed param/resource body `force`. |
| `--pick` | Interactive local picker; remote replacement is `GET /api/v1/missions`. |
| `--from` | Local station-picker origin; no API parameter. |
| `--output` | Local backup destination; `backup --target` is explicitly excluded because the coordinator has no streaming artifact route and must not journal backup prompts/evidence. |
| `--input` | `worker.restore` resource body `input`; the path is checked against enrolled real-path roots by the worker. |
| `--allow-control` | Local worker enrollment control policy; coordinator accepts worker token separately. |
| `--allow-create` | Local worker opt-in required for remote mission creation; no request can override the worker setting. |
| `--allow-root` | Local repeatable real-path boundary for remote creation and restore input; paths are resolved and enforced by the worker. |
| `--allow-power` | Local worker opt-in required for remote power operations; no request can override the worker setting. |
| `--allow-restore` | Local worker opt-in required for remote restore; no request can override the worker setting. |
| `--allow-terminal-view` | Local worker enrollment terminal-view policy; terminal authorization remains scoped WebSocket. |
| `--ca-file` | Local TLS trust configuration for worker/remote clients; not sent in API requests. |
| `--coordinator` | Local connection configuration; API base URL is client config, not request field. |
| `--token-file` | Local credential input; token is stored privately and sent only as Authorization header. |
| `--print` | Local service-unit preview; no API. |
| `--color` | Local terminal rendering preference; no API. |
| `--colors` | Local terminal capability preview; no API. |
| `--theme` | Local theme preview; no API. |
| `--set` | `mission.rotation.put` field `set`. |
| `--kind` | `mission.steer` typed field `kind`; all six CLI kinds are supported. |
| `--route` | `mission.steer` typed field `route` for handoff. |
| `--exclude-client` | Internal local tmux cleanup selector; never remote. |
| `--startup-token` | Internal controller startup handoff; never remote. |
| `--help` | Local argparse help; no API call. |
| `-h` | Alias for `--help`; local argparse help, no API call. |

Coordinator router declarations (`http::ROUTES`), including supporting auth, worker, token, stream, terminal and documentation routes:

| Method and route |
|---|
| `GET /healthz` |
| `GET /api/v1/auth/state` |
| `POST /api/v1/auth/setup` |
| `POST /api/v1/auth/login` |
| `POST /api/v1/auth/logout` |
| `POST /api/v1/auth/password` |
| `GET /api/v1/workers` |
| `POST /api/v1/workers` |
| `GET /api/v1/workers/{name}` |
| `DELETE /api/v1/workers/{name}` |
| `POST /api/v1/workers/{name}/restore` |
| `GET /api/v1/workers/{name}/doctor` |
| `GET /api/v1/workers/{name}/agents` |
| `GET /api/v1/workers/{name}/audit` |
| `POST /api/v1/workers/{name}/gc` |
| `GET /api/v1/tokens` |
| `POST /api/v1/tokens` |
| `DELETE /api/v1/tokens/{name}` |
| `GET /api/v1/missions` |
| `POST /api/v1/missions` |
| `GET /api/v1/missions/{id}` |
| `GET /api/v1/missions/{id}/events` |
| `GET /api/v1/missions/{id}/operations` |
| `GET /api/v1/missions/{id}/agents` |
| `GET /api/v1/missions/{id}/settings` |
| `PATCH /api/v1/missions/{id}/settings` |
| `POST /api/v1/missions/{id}/steer` |
| `POST /api/v1/missions/{id}/finish` |
| `GET /api/v1/missions/{id}/rotation` |
| `PUT /api/v1/missions/{id}/rotation` |
| `GET /api/v1/missions/{id}/proposals` |
| `POST /api/v1/missions/{id}/proposals` |
| `POST /api/v1/missions/{id}/proposals/{n}/{action}` |
| `POST /api/v1/missions/{id}/{action}` |
| `POST /api/v1/missions/{id}/suspend-until-credits` |
| `GET /api/v1/missions/{id}/tasks` |
| `POST /api/v1/missions/{id}/tasks` |
| `GET /api/v1/missions/{id}/tasks/{n}` |
| `PATCH /api/v1/missions/{id}/tasks/{n}` |
| `POST /api/v1/missions/{id}/tasks/{n}/verify` |
| `POST /api/v1/missions/{id}/tasks/{n}/{action}` |
| `POST /api/v1/commands` |
| `GET /api/v1/commands/{id}` |
| `GET /api/v1/events` |
| `GET /api/v1/stream` |
| `GET /api/v1/diagnostics` |
| `GET /api/v1/openapi.json` |
| `GET /api/docs` |
| `GET /api/v1/worker/connect` |
| `GET /api/v1/terminal` |
| `GET /api/v1/worker/terminal/{session_id}` |

The regression test enumerates the argparse tree and Rust route policy table. Adding a command, option, or route requires updating this inventory; route method/path pairs are checked for exact coverage.
