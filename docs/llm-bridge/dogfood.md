# Bridge improving Bridge

The first real engineering mission ran through `llm-bridge`, not a mocked
provider: `390561034e274b82a41dd09a41b04b43`, in the isolated worktree
`/tmp/llm-bridge-dogfood-cancel`, based on candidate snapshot `2cc4142`.
The provider was the installed Codex CLI with `gpt-6-luna`, High effort,
`workspace-write` sandbox and explicit `never` approval policy. Only Codex
credits were used. The original prompt is retained in the mission database.

The mission fixed failed-task cancellation and rejected-command rollback in
`bridge.py` and `bridge_store.py`. Two independent reviewer regressions were
confirmed red before launch. Bridge's configured verifier subsequently passed,
and its mission/task records are COMPLETED with verifier evidence. The child
reported 54 focused tests passed; after applying its patch to the integration
checkout, Root independently ran review/CLI/atomicity/store/steering tests:
**53 passed in 1.03 seconds**. Counts differ because the integration tree has
continued evolving. No test was changed merely to accept the defect.

## Discrepancies to address

- The chosen sandbox could edit the worktree but could not write its shared Git
  metadata under the original repository's `.git/worktrees/`. The requested
  local commit therefore did not happen inside that invocation. Provider-native
  permission options must remain explicitly configurable and their effective
  scope visible; Bridge must not silently broaden permissions. A follow-up
  dogfood run must verify the intended Git-metadata permission path.
- The child incorrectly described itself as direct Codex rather than a managed
  Bridge provider. Runtime context needs explicit mission/task/operation identity
  and supported control entry points, with a regression for that context.
- A verifier proves only its explicit checks. This mission's verifier checked
  the cancellation regressions, not the requested Git commit. Subsequent dogfood
  acceptance must include every required deliverable, including commit state.

These are tracked as specification and implementation work, not silently
accepted product limitations. Native CLI fidelity and permission propagation
must be tested independently from whether a model claims success.
