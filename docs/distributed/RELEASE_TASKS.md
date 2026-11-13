# Release 1.5.0 / 1.6.0 task list

This is the checklist for the convergence work towards releases 1.5.0 and 1.6.0. It is updated and
pushed to the branch `chore/release-tasks` after every merge or milestone, so work can resume from
here if a session ends unexpectedly. The register (`SPEC_STATUS.md`) stays the source of truth for
row status; this file tracks the tasks and pull requests that close those rows.

Resume procedure: read this file, run `gh pr list` and `gh pr checks <n>`, then
`python scripts/spec_open.py --release 1.5.0`. Continue with the first unticked item in
"Next" whose dependencies are met. Full row-by-row plan: work packages WP1 to WP10 below.

Team: the lead (Claude Opus) decides and reviews. Claude Sonnet supervisors design, brief, review
diffs, run tests, commit and push. Codex `gpt-6.1-sol` implements, Codex `gpt-6-luna` does very simple
edits, Claude Haiku does git and register work. Every agent commits and pushes after each milestone.

Last updated: 2026-11-13 21:00 UTC.

## Merged

- [x] #57, #77 to #93, #95, #96, #98, #102 (earlier batches)
- [x] #94 CI flake fixes (first batch)
- [x] #105 coordinator writer stall during backup (IMMEDIATE transactions, pragma only when needed)
- [x] #97 capacity caps, replay marker, migration v18 (FIND-009 stays open until a 24 h physical-Pi run)
- [x] #99 web control and live view (PRD-014/015/016 stay [~]: backend gaps); Linux web job split into 2 shards
- [x] #106 register: DOCS-003, FIND-013, PRD-008 ticked; DOG-020 deferred; decision D57

## Open pull requests

- [ ] #104 CI flakes (settle wait, toast hold, short-soak RSS floor, db_lock_burst): merge on green CI
- [ ] #100 UXR-053..057: home-page blocking-time regression fixed, merge main, then merge on green
- [ ] #101 PRD-013 diagnostics view: merge main after #100; PRD-013 must stay [~] (guided restore not done)
- [ ] #103 batch C (PRD-017/018 close; 019/020/021/032 stay [~]): merge main after #101
- [ ] #74 Playwright 1.65 (FIND-016): after the web PRs, regenerate baselines in the 1.65 image
- [ ] #107 WP10a release engineering (PRD-001/002/005/006): review said fix first; finishing every criterion
- [ ] #108 WP1 Resource API (API-001..016): in progress; needs a security review before ready
- [ ] #64 secapi draft: source for WP1 to WP4 only; close when the last split merges

## In progress (not yet a PR)

- [ ] WP9a coordinator operations: PRD-011 update check (off by default), PRD-012 support bundle,
      PRD-026 safe-defaults test, PRD-003 migration tests (branch `feat/coordinator-ops-wp9a`)

## Next

- [ ] WP2 response hardening and mutation budget (SEC2-001..005, 010, 012) after WP1 merges
- [ ] WP3 auth hardening (SEC2-006..009, 011, 013, 015, 016, 019) after WP2; high security review
- [ ] WP4 passkeys (PK-001, 002, 005..009, 011) after WP3; high security review
- [ ] WP7 web remainder: PRD-014/015/016 backend digest fields (rate-limit wait target, token use,
      typed last tool call, per-turn prompts and replies), DOG-017 delivery field, PRD-013 guided
      restore UI (needs WP3 step-up), UX-011 event-kind coverage, PRD-029 deprecation notice
- [ ] WP9b: PRD-010 coordinator retention, PRD-013 restore backend, PRD-027 abuse tests (after WP2/WP3)
- [ ] WP8 parity and evidence: PAR-001..012 matrices, FIND-020 and UX-001..011 restatement,
      UX-001/005/010 persona runs by agents (D57), UXR-040 critique, PRD-037
- [ ] WP10b reviews on the release candidate: REV-002..006, PRD-023 external-style pen test
- [ ] WP10c: PRD-028 release checklist, DOCS-004, TRACK-018, REV-008, PRD-035, PRD-030 RC dry run
- [ ] FIND-001: three green nightly runs

## Needs the operator

- [ ] PRD-024 private vulnerability reporting: enabled on GitHub 2026-11-13; register row needs evidence
- [ ] FIND-009 24 h physical-Pi memory.events run; PRD-007 24 h soak; PRD-036 password reset over SSH
- [ ] PI rows that cannot be measured remotely: waiver decision or one physical run
- [ ] Orca screen-reader run (PRD-019/020/021/032); overseer screenshot review (UXR-040)
- [ ] PyPI trusted publisher, if PRD-002 requires publishing the wheel to an index

## 1.6.0 (after 1.5.0 is tagged)

- [ ] PROV-006..010, ORCH-004/013/014, PRD-012/026/027 items carried to 1.6.0, DOG-020, ORCH-005 web display
- [ ] `python scripts/spec_open.py --release 1.6.0` exits 0
- [ ] Final adversarial review, fixes, merge, tags 1.5.0 and 1.6.0
