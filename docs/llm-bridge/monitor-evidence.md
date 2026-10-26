# Bridge monitor projection evidence

`llm_tools.bridge_monitor.project_snapshot` projects one mission from its SQLite
store. It reads no provider API and makes no schema migration. Task pages are
limited to 500 rows (200 by default), task search and offset execute in SQL,
task totals and acceptance criteria counts are SQL aggregates, and event,
operation, and queued-command histories are bounded. Latest selection, wait,
and progress events use direct queries forced through the `(mission_id, kind,
id)` index, so lookup cost does not scale with a mission's full event history
and more than 100 newer events cannot hide them. Suspend lookup performs one
indexed `LIMIT 1` query per fixed allowed kind, then compares at most four rows
in Python; no query sorts the lifetime suspend-event history. Runtime capacity is shown only when a persisted
`capacity-refresh`, `provider-selection`, or `provider-wait` event exists; the
newest relevant event ID controls selection versus wait state. A newer usable
capacity refresh clears an older wait. The projection includes the observation
timestamp and age and marks the data as persisted rather than live. Heartbeat
data is shown as last observed with age and never proves current liveness.
`next_eligible`
comes only from the newest readiness or wait event's `wait_until` /
`target_epoch`, not the earliest raw window reset; a newer usable selection
clears an older wait.
Station rows are a list for the TUI, with configured model/effort separated from
observed values. Scheduler-pinned operation models are not treated as observed;
an observed model requires a verified `station-observation` event. Controller
ownership remains `unknown` until a durable `controller-ownership` event
explicitly marks ownership verified; mission lifecycle status is not used as a
proxy for controller liveness. Suspend state is included only from a persisted
suspend event; absent such an event it is `unknown`. Configured mission mode is
separate from actual controller mode. Actual mode stays `unknown` until a
verified `controller-mode` event is persisted, including when `start --native`
overrides the mode frozen at mission creation.

Criteria count an acceptance item as verified only when its task is COMPLETED;
an empty denominator has `complete: null`. This is a task-level verification
count, not a claim that each criterion had separate evidence. Stall warning is
limited to three or more consecutive exited operations with non-zero codes;
elapsed build time is not treated as a stall.

Focused verification on 2026-10-25:

```text
python -m pytest tests/test_bridge_monitor.py tests/test_bridge_refresh.py tests/test_bridge_tui.py tests/test_bridge_cli.py -q --basetemp=/tmp/bridge-monitor-27
31 passed
```

The tests cover SQL task paging/search over a 120-task queue, TUI station-row
compatibility, verified versus pinned model and configured versus actual mode,
wait-target capacity projection including clearing stale waits after a newer
usable provider selection or worker refresh, controller heartbeat and worker
capacity-refresh event integration through real SQLite, latest
progress/capacity/controller/suspend facts after more than 100 newer events and
many mixed suspend events, query plans without a
temporary B-tree for each exact-kind lookup, and projection
after database reopen with repeated failures. A larger 10k task path is bounded by the same SQL page and
aggregate queries; this focused test does not claim a new latency or memory
benchmark.
