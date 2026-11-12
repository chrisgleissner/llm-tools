---
title: Any D-number written in the register counts as a decision citation and must exist
author: Christian
date: 2026-11-12
branch: chore/spec-register-2026-11-12
cost: { minutes: 6, tokens: 41288 }
id: l-49afe9eb
scope: repo
status: active
src: agent
touches:
  - docs/distributed/FINAL_REPORT.md
  - docs/distributed/SPEC_STATUS.md
  - docs/distributed/spec/dog.md
  - docs/distributed/spec/prd.md
  - docs/distributed/spec/uxr.md
  - tests/test_distributed_spec_status.py
  - AGENTS.md
  - docs/archive/HANDOVER-llm-bridge.md
  - docs/archive/HANDOVER-root.md
  - docs/archive/PLANS.md
---

## Watch out
The decision check treats every token of the form D<number> anywhere in a register row as a citation, and fails if that decision has no heading. Mentioning a missing number in prose (for example "gaps D18, D28") breaks the check. Describe gaps without writing the numbers, or cite only decisions that exist.

The other two failures that session were intentional reverts done to prove that the checks can fail. They do not need a fix.

In the code: scripts/spec_register.py DECISION_CITATION and decision_problems; tests/test_distributed_spec_status.py::test_decision_headings_are_unique_and_cited_decisions_exist.
