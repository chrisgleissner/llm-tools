---
title: Structured provider questions need no idle delay and must travel without an event
author: Christian
date: 2026-11-06
branch: feat/distributed-followup
id: l-b1ed8599
scope: repo
status: active
src: agent
touches:
  - llm_tools/terminal.py
  - llm_tools/bridge_worker.py
  - tests/fixtures/codex-question-tui-capture.txt
  - tests/test_bridge_output_idle.py
---

## Decided
A structured unanswered `request_user_input` is already a definitive wait signal, so a one-shot status read must surface it without a process-local idle timer. For coordinator freshness, send only changed bounded mission attention summaries over the existing event cursor, and scan active native missions in a rotating bounded batch so large workers do not starve later missions.

## Tried and ruled out
Waiting for the stable-screen 60-second threshold failed for one-shot CLI status because that process starts with no observation history. Sending the full mission inventory on every event would waste bandwidth and database work; event-only replication also misses provider questions because they do not create Bridge events.

## Watch out
A real screen fixture can prove the UI shape, but do not call a constructed JSONL envelope a real transcript capture. Keep the Codex screen capture, observed structured event shape, and documented-but-not-real-capture Claude shape distinguished in evidence. Keep periodic polling bounded and fair.

In the code:
- `llm_tools/terminal.py::read_native_provider_wait`
- `llm_tools/bridge_worker.py::WorkerSession.next_events`, `_question_refresh_batch`
- `tests/fixtures/codex-question-tui-capture.txt`
- `tests/test_bridge_output_idle.py::test_codex_structured_question_is_visible_on_the_first_status_read`
