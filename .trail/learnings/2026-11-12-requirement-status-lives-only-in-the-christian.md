---
title: "Requirement status lives only in the specification register, never copied into other documents"
author: Christian
date: 2026-11-12
branch: chore/spec-register-2026-11-12
cost: { minutes: 4, tokens: 158280 }
id: l-049efc7c
scope: repo
status: active
src: agent
touches:
  - docs/distributed/DECISIONS.md
  - docs/distributed/FINAL_REPORT.md
  - docs/distributed/SPEC_STATUS.md
  - docs/distributed/benchmarks.md
  - docs/distributed/install.md
  - docs/distributed/security-requirements.md
---

## Decided
Status and evidence for each specification ID are kept in the register alone. Other documents (the security clause crosswalk, benchmark notes, install notes) map or describe requirements but do not repeat their status, because copied statuses drifted out of date (the crosswalk said "as of 2026-11-06" and was wrong for many rows by 2026-11-12).

## Watch out
Rows closed by an operator decision rather than by verification are waived `[w]` with the decision number, not `[x]`. Release membership (PROV, ORCH and LB-004 gate 1.6.0; the rest gate 1.5.0) is decided by D40.

In the code: docs/distributed/SPEC_STATUS.md (register), docs/distributed/security-requirements.md (clause map only), docs/distributed/DECISIONS.md D40, D41, D46.
