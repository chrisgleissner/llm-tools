---
title: Preserve provider error provenance before deciding whether work can resume
author: Christian
date: 2026-11-05
branch: fix/bridge-dogfood-controls
id: l-ee2d8f1e
scope: repo
status: active
src: agent
touches:
  - AGENTS.md
  - README.md
  - config.example.toml
  - docs/llm-bridge/operations.md
  - docs/llm-bridge/test-matrix.md
  - llm_tools/bridge.py
  - llm_tools/bridge_actions.py
  - llm_tools/bridge_documents.py
  - llm_tools/bridge_events.py
  - llm_tools/bridge_format.py
---

## Decided
A provider quota response and an interactive guard abort can share exit status 75. Normalize a verified provider error at the transport boundary, preserve its native status in bounded diagnostic metadata, and leave genuine guard aborts held for operator action. Apply pending task amendments only at the completed invocation boundary. Recovery must leave a runnable task so capacity selection can reach the shared wake planner.

## Tried and ruled out
A live controller heartbeat and a background capacity refresh did not prove continuation: two unattended dogfood attempts left the task BLOCKED after quota recovery, so before_iteration waited for ready work indefinitely. Shared-backend sleep can work independently of that broken task transition. Counting a timer or an accepted suspend command as end-to-end recovery hides this distinction.

## Watch out
Quoted historical diagnostics in tool output must not become either an input prompt or a gateway-credit failure during postprocessing. Keep the verified provider channel separate from retained tool output. Simulated wake fixtures prove state transitions only; physical sleep needs suspend-inclusive versus suspend-exclusive clock evidence, and resumed model work needs a new verified provider process linked to that cycle.

In the code: llm_tools/scheduler.py structured Codex adapter and result diagnostics; llm_tools/bridge_runtime.py task continuation and process identity; tests/test_bridge_quota_recovery_e2e.py. Preserve the genuine interactive-75 hold tests while checking a quota response, pending amendment, exact reset target and next operation together.
