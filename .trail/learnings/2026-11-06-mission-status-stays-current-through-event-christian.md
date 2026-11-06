---
title: Mission status stays current through event cursor snapshots
author: Christian
date: 2026-11-06
branch: feat/distributed-followup
id: l-a92e50ac
scope: repo
status: active
src: agent
touches:
  - coordinator/src/db.rs
  - coordinator/src/http.rs
  - coordinator/src/protocol.rs
  - coordinator/src/ws_worker.rs
  - coordinator/tests/worker.rs
  - docs/distributed/WORKLOG.md
  - llm_tools/bridge_protocol.py
  - llm_tools/bridge_worker.py
  - tests/test_bridge_protocol.py
  - tests/test_bridge_worker.py
---

## Decided
Mission summaries should travel with the existing event stream only for mission IDs touched by that batch. The worker advances a separate mission cursor on every event batch, and the coordinator applies mission upserts and both cursors atomically. This keeps newly created and status-changed missions current without polling, reconnecting, or rewriting every mission on routine task and operation events. Older workers that send events without a mission cursor remain explicitly stale after their copy may have fallen behind.

## Tried and ruled out
Sending the worker's full mission list with every event batch would fix freshness but repeat unnecessary mission reads and coordinator writes during frequent task activity, and increase frame size on low-write hosts. Relying on hello timestamps also marked online, caught-up workers stale after unrelated events.

## Watch out
When a new mission-level event is introduced, ensure it carries the mission ID so its updated summary is included in the batch. Keep stale detection based on worker online state and the event-versus-mission cursor comparison.

In the code:
`llm_tools/bridge_worker.py` `WorkerSession.next_events` collects the mission IDs from `events_since` and emits `mission_cursor`. `llm_tools/bridge_protocol.py` and `coordinator/src/protocol.rs` accept and bound the optional fields. `coordinator/src/db.rs` stores mission upserts and `mission_cursor` in the event transaction; `coordinator/src/http.rs` computes `stale`. Regression coverage is in `tests/test_bridge_worker.py` and `coordinator/tests/worker.rs`.
