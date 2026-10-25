# LLM Bridge — Production Implementation Directive

## Mission

Implement **LLM Bridge**, a new, production-quality, terminal-native command centre for autonomous AI engineering, in the existing repository:

https://github.com/chrisgleissner/llm-tools

The executable shall be:

```bash
llm-bridge
```

LLM Bridge provides a persistent command bridge from which an engineer can manage, supervise and interact with long-running AI engineering missions involving multiple provider CLIs, models and agents.

It must be:

- Exceptionally reliable.
- Intuitive and pleasant to operate.
- Keyboard-first, with mouse interaction supported wherever practical.
- Entirely CLI- and file-driven.
- Faithful to the native interfaces of underlying provider CLIs.
- Responsive and resource-efficient.
- Durable across process termination and machine reboot.
- Capable of running unattended for days or weeks.
- Observable, testable and maintainable.
- Simple in architecture, without unnecessary abstraction or infrastructure.

The guiding metaphor is the bridge of the USS Enterprise: the operator retains situational awareness and command over a complex, largely autonomous mission.

The metaphor should inform the interface, navigation and product identity without creating gimmicky terminology or unnecessary complexity.

**This is an implementation request, not a research or design-only request.**

Complete the work autonomously, including architecture, implementation, integration, testing, review, remediation and documentation.

Do not stop after planning, scaffolding or producing a partial prototype.

If execution capacity becomes insufficient, preserve progress so that the work can resume deterministically without repeating completed analysis or implementation.

---

# 1. Mandatory development team and reasoning strategy

Use three distinct development personas:

| Persona | Model | Default reasoning effort |
|---|---|---|
| Planner | GPT-6.1 Sol | Medium |
| Implementor | GPT-6 Luna | High |
| Reviewer | GPT-6 Luna | Extra High |

Use the model and reasoning effort mechanisms actually supported by the execution environment.

Resolve exact model IDs and effort identifiers from the available provider configuration or CLI capabilities.

Do not invent model identifiers or assume a particular spelling is accepted by every CLI.

If an exact combination is unavailable, record the discrepancy and select the closest supported option consistent with the requested roles. Never report an unavailable model or effort level as having been used.

Where the platform supports independent agents, run these as separate agent contexts.

Where it supports explicit role configuration, establish these roles before implementation begins.

Do not create a complex multi-agent framework solely to implement this development workflow.

## 1.1 Reasoning effort policy

Reasoning effort must be selected deliberately.

Higher reasoning effort is not automatically better for every task.

The goal is to maximise **verified engineering quality per available token and per unit of provider capacity**.

### Sol 6.1 planner — Medium by default

Use Medium reasoning for:

- Repository investigation.
- Requirements decomposition.
- Architecture proposals.
- Component responsibilities.
- Implementation sequencing.
- Contract definitions.
- Acceptance criteria.
- Testing strategy.
- Straightforward architectural comparisons.

Escalate to High only for genuinely difficult decisions, such as:

- PTY and terminal multiplexing architecture.
- Native interactive sessions versus autonomous execution.
- Crash-consistent orchestration state.
- Exactly-once versus at-least-once execution semantics.
- Native provider restoration.
- Concurrency and process ownership.
- Conflicts between usability and native CLI fidelity.

Do not repeatedly escalate routine planning.

Do not have Sol produce hundreds of pages of speculative documentation.

The planner's job is to make the difficult architectural choices efficiently and hand over an implementable plan.

### Luna implementor — High by default

Use High reasoning for production implementation.

High reasoning should be the normal engineering mode because this tool is foundational infrastructure.

Escalate selectively to Extra High for:

- Terminal control and mouse event forwarding.
- Process management and PTY lifecycle.
- Cross-process concurrency.
- Durable state and crash recovery.
- Provider session reconstruction.
- Non-idempotent operation reconciliation.
- Difficult integration defects.
- Repeatedly failing tests where root cause is unclear.
- Significant deviations from the approved architecture.

Return to High after resolving the difficult problem.

For mechanical changes such as documentation, formatting, simple test fixtures or repetitive migrations, Medium may be used if safely supported.

Avoid expensive reasoning on trivial changes.

### Luna reviewer — Extra High for independent reviews

Use Extra High reasoning for substantive independent review.

This is particularly valuable because bugs in an orchestration foundation may remain hidden until a mission has been running for days.

Reviewers must challenge:

- Architecture.
- Correctness.
- Recovery guarantees.
- Process and terminal behaviour.
- Race conditions.
- Hidden state inconsistencies.
- Resource leaks.
- Test weaknesses.
- Mock/production divergence.
- Inaccurate observability.
- Unintended changes to existing tools.

Use High reasoning for narrow re-review after straightforward fixes.

Do not repeatedly conduct a full review of unchanged code.

## 1.2 Token allocation

Aim for an approximate total allocation of:

- 10–15% planner.
- 65–70% implementor.
- 15–25% reviewer.

Adapt based on engineering risk and the amount of unfinished work.

These percentages are guidance, not strict quotas.

Do not waste capacity enforcing an arbitrary allocation when the implementation needs more attention.

Reserve meaningful capacity for independent review and final verification.

## 1.3 Separate responsibilities

**Sol plans.**

It inspects the repository, evaluates important design alternatives, establishes interfaces and invariants, and defines executable milestones.

**Luna implements.**

It writes the production code, adds tests, runs tests, fixes defects and maintains progress records.

**Luna reviews.**

It independently inspects implementation and evidence, identifies concrete defects and challenges unproven claims.

Avoid duplicated investigation across personas.

The reviewer must still inspect enough actual code to be genuinely independent.

## 1.4 Agent handoffs

Use compact, durable handoffs.

Each handoff should contain:

```text
OBJECTIVE
RELEVANT FILES
DECISIONS
INVARIANTS
IMPLEMENTED CHANGES
TEST EVIDENCE
KNOWN RISKS
NEXT ACTION
```

Do not copy entire source files into handoffs.

Do not repeatedly reproduce the original specification.

Prefer references to repository paths, diffs, test reports and durable project records.

The repository is the authoritative source of implementation state.

---

# 2. Non-negotiable engineering principles

Throughout this work, apply the following principles.

## 2.1 Keep it simple

Prefer the smallest architecture that correctly satisfies the requirements.

Avoid:

- Excessive abstraction layers.
- Generic plugin frameworks.
- Duplicate provider adapters.
- Parallel orchestration engines.
- Microservices.
- Web servers.
- Message brokers.
- Distributed coordination.
- Unnecessary background daemons.
- Complex dependency injection.
- Speculative extensibility.
- Large frameworks for simple local operations.

Introduce an abstraction only where it removes actual duplication, establishes an important boundary or makes a difficult component testable.

Prefer clear Python modules and well-defined interfaces.

## 2.2 Test-driven development

Use a test-driven development approach.

For each significant capability:

1. Define its observable contract.
2. Write an automated test that initially fails.
3. Implement the smallest correct solution.
4. Run the test.
5. Refactor without changing behaviour.
6. Add failure and boundary cases.
7. Run relevant regression tests.
8. Record verification evidence.

Strictly apply red-green-refactor to durable state, scheduling, command dispatch and other deterministic logic.

For terminal rendering and native CLI integration, write contract and integration tests early, using controlled fixtures and real PTY behaviour.

Do not defer testing until the implementation is complete.

Do not treat code compilation or successful imports as sufficient verification.

## 2.3 Real software, not simulated success

Automated mocks are necessary, especially for CI.

However, passing mocked tests does not prove compatibility with actual provider CLIs.

Real CLI integration testing is a mandatory part of the development and verification strategy wherever the environment supports it.

Never claim a provider was verified against its actual CLI if only a mock was exercised.

## 2.4 Observability by design

Do not bolt monitoring onto the system at the end.

Define structured events, lifecycle states, progress evidence and diagnostics from the beginning.

The same authoritative runtime state should power:

- The TUI.
- CLI status commands.
- Machine-readable status.
- Event history.
- Recovery.
- Diagnostics.

Avoid multiple competing sources of truth.

## 2.5 Design for interruption

Assume that any process, provider or implementation agent can be interrupted at any time.

Persist enough authoritative state to distinguish:

- Completed work.
- Incomplete work.
- Work in progress.
- Work whose outcome is uncertain.
- Work requiring human intervention.

Never infer successful completion from process disappearance.

Never silently repeat potentially non-idempotent actions.

---

# 3. Repository investigation

Before making architectural changes, inspect the current repository.

Read:

- `README.md`
- `AGENTS.md`
- `PLANS.md`
- `WORKLOG.md`
- `config.example.toml`
- `pyproject.toml`
- `llm_tools/ralph_robin.py`
- `llm_tools/scheduler.py`
- `llm_tools/usage.py`
- `llm_tools/usage_service.py`
- `llm_tools/capacity.py`
- `llm_tools/routes.py`
- `llm_tools/config.py`
- `llm_tools/common.py`
- Relevant provider adapters.
- Existing tests.
- CI configuration.

Read the actual code, not just documentation.

The existing project already has:

- Provider-specific capacity readers.
- Model-aware routing.
- Route-level capacity policies.
- Delegated and opaque routes.
- Even-burn scheduling.
- PTY execution mechanisms.
- Headless and interactive launch modes.
- tmux support.
- Ralph Robin iteration and rotation.
- Capacity exhaustion handling.
- Verified RTC suspend/wake mechanisms.
- Local runtime state and logs.
- Usage history and snapshots.
- A substantial pytest suite.
- An existing 85% coverage gate.

Reuse these capabilities.

Do not duplicate them inside LLM Bridge.

Identify where existing code needs small, reusable programmatic interfaces rather than attempting to control other tools by parsing formatted terminal output.

Maintain compatibility with:

```text
llm-usage
llm-scheduler
ralph-robin
llm-sleep-soak
```

Preserve existing configuration semantics.

---

# 4. Architectural responsibilities

Introduce `llm-bridge` as a first-class entry point in the existing Python package.

The intended separation is:

## LLM Bridge

Owns:

- Missions.
- Task queue.
- Task dependencies and priorities.
- Operator commands.
- Command bridge UI.
- Session lifecycle.
- Provider station presentation.
- Progress verification.
- Mission event history.
- Durable orchestration state.
- Attach/detach.
- Stop/resume/reconstruction.
- Monitoring and diagnostics.

## Ralph Robin

Remains responsible for:

- Autonomous execution.
- Iteration and continuation.
- Route selection.
- Capacity-aware provider rotation.
- Exhaustion handling.
- Existing long-running policies.
- Existing suspend/wake orchestration.

## LLM Scheduler

Remains responsible for:

- Provider launching.
- Execution modes.
- Provider readiness gating.
- Existing retry and execution mechanics.

## LLM Usage

Remains responsible for:

- Quota and capacity data.
- Usage sampling.
- Capacity history.
- Available estimates.

Some existing modules may need focused refactoring to expose structured interfaces or events.

Make the smallest practical changes.

Never create two competing schedulers.

---

# 5. Product experience: the command bridge

LLM Bridge should feel like a sophisticated terminal engineering environment.

Design for immediate understanding rather than visual spectacle.

Requirements:

- ASCII-first.
- Optional restrained ANSI colour.
- Keyboard-first.
- Mouse-capable.
- Responsive.
- Low CPU overhead.
- Compact but readable.
- Clear focus indication.
- Excellent navigation.
- No flicker.
- No unnecessary animations.
- SSH-compatible.
- tmux-compatible.
- Functional without specialised fonts.
- Functional without colours.
- Entirely terminal-based.

No browser UI.

No desktop GUI.

No web service.

## 5.1 Main view

Conceptual layout:

```text
LLM BRIDGE                           MISSION ACTIVE     18h 42m
================================================================
MISSION  Implement persistent multi-provider orchestration
TASK     #04 Integration tests                7/10 verified
----------------------------------------------------------------

                    NATIVE PROVIDER CLI

                    Original interface
                    Native highlighting
                    Native shortcuts
                    Interactive menus
                    Skills and subagents
                    Mouse interactions

----------------------------------------------------------------
STATION  Codex | GPT-6 | High             Route: codex-high
ORDERS   3 done | 1 active | 8 queued | 0 blocked
CAPACITY 5h 24% | Weekly 71% | Est. exhaustion 38m
NEXT     Claude / Sonnet | READY
LOOP     Iteration 47 | Last progress 3m ago
SYSTEM   AUTONOMOUS | State persisted
----------------------------------------------------------------
COMMAND  Palette | Orders | Stations | Log | Systems | Sessions
```

This is an illustrative layout, not a mandatory literal design.

The actual design must preserve sufficient space and terminal behaviour for the native CLI.

## 5.2 Views

Provide:

**Bridge:** Native CLI and essential mission status.

**Orders:** Task queue, priorities, dependencies, amendments and verification.

**Stations:** Provider sessions, model settings and restoration states.

**Systems:** Quota, capacity, route selection, execution status and health.

**Mission Log:** Structured event history and diagnostics.

**Sessions:** Start, attach, detach, stop, resume and restoration.

**Command Palette:** Discoverable operator actions.

## 5.3 Responsive layout

Test at minimum:

- 80x24.
- 120x40.
- Large terminals.
- Dynamic resize.

Prioritise native CLI usability.

Provide compact, standard and detailed modes if useful.

Do not compromise responsiveness to implement elaborate visual features.

---

# 6. Keyboard-primary and mouse-secondary interaction

Every essential operation must work efficiently through the keyboard.

Mouse support must be available where terminals and underlying CLIs support it.

Keyboard interaction is the primary path.

Mouse interaction is a complementary convenience, not a requirement.

## 6.1 Keyboard interaction

Support:

- Switching views.
- Switching stations.
- Navigating tasks.
- Adding tasks.
- Editing and prioritising tasks.
- Searching.
- Opening the command palette.
- Inspecting status.
- Pausing missions.
- Stopping sessions.
- Resuming sessions.
- Returning focus to the native CLI.

Choose shortcuts carefully.

Research actual shortcuts used by provider CLIs.

Avoid breaking provider-native shortcuts.

Use a deliberate escape/prefix mechanism or another reliable focus model.

Make command discovery easy.

## 6.2 Mouse interaction

Support:

- Clicking navigation items.
- Selecting tasks.
- Selecting stations.
- Scrolling task lists.
- Scrolling logs.
- Clicking actions.
- Focus changes.
- Text selection where technically feasible.

All mouse-triggered actions should invoke the same command handlers as keyboard-triggered actions.

## 6.3 Mouse forwarding to native CLIs

Preserve provider-native mouse handling.

Investigate:

- SGR mouse reporting.
- Mouse coordinate translation.
- Mouse wheel behaviour.
- Alternate-screen modes.
- Bracketed paste.
- Native terminal selection.
- Focus events.

Mouse events inside the provider viewport should reach the provider where supported.

Mouse events targeting Bridge UI controls should be handled by Bridge.

Document unavoidable conflicts between terminal selection and application mouse reporting.

---

# 7. Native provider CLI fidelity

This is one of the highest-risk requirements.

Do not implement fake representations of provider CLIs.

Use real PTY-backed processes.

Preserve:

- ANSI styling.
- Cursor movements.
- Terminal resizing.
- Native interactive menus.
- Keyboard shortcuts.
- Mouse controls.
- Clipboard and paste behaviour where supported.
- Provider prompts.
- Skills.
- Subagents.
- Multi-agent functionality.
- Native session commands.

Research the actual capabilities of:

- Codex.
- Claude Code.
- GitHub Copilot CLI.
- Kilo.
- OpenCode.
- MiniMax.
- Other existing supported provider routes.

Do not assume all providers offer identical capabilities.

## 7.1 Resolve the architecture early

The planner must explicitly evaluate the interaction between:

1. Ralph Robin's headless autonomous execution.
2. Provider-native interactive terminal sessions.
3. The persistent Bridge UI.
4. Provider handoffs.
5. Detach/attach.
6. Complete session stop/resume.

These are different execution modes and cannot be assumed interchangeable.

Compare tmux-based and dedicated PTY approaches.

Select based on fidelity, simplicity, reliability, portability and testability.

## 7.2 Early proof of feasibility

Before implementing the full UI, build a narrow vertical slice proving:

- A real provider-like process runs in a PTY.
- Its ANSI output renders correctly.
- Keyboard input reaches it.
- Mouse input reaches it when enabled.
- Terminal resize works.
- Bridge controls remain accessible.
- The process exits cleanly.
- Terminal state is restored.

Test this with deterministic fake CLIs and at least one actual installed provider CLI where available.

**Do not build the entire interface on top of an unverified terminal architecture.**

## 7.3 Executing versus viewed stations

Distinguish:

- Active executing station.
- Currently viewed station.
- Historical station.
- Restored native station.
- Reconstructed station.

Switching the viewed station must not change autonomous scheduling.

Do not infer session state from terminal text when structured metadata is available.

---

# 8. Persistent missions and task orchestration

The initial user prompt becomes Task 1 of a durable mission.

Tasks must outlive provider processes.

Each mission should persist:

- Stable ID.
- Name.
- Original objective.
- Repository identity.
- Working directory.
- Creation time.
- Task queue.
- Execution history.
- Provider configuration.
- Scheduling policy.
- Session lifecycle.
- Verification results.
- Operator interventions.

Each task should persist:

- Stable ID.
- Original prompt, verbatim.
- Title.
- State.
- Priority.
- Dependencies.
- Acceptance criteria.
- Amendments.
- Verification evidence.
- Current checkpoint.
- Execution history.
- Blocking or failure reasons.

States:

```text
QUEUED
READY
RUNNING
PAUSED
BLOCKED
VERIFYING
COMPLETED
FAILED
CANCELLED
```

Define legal transitions.

Persist transitions atomically.

## 8.1 Task commands

Support:

- Add.
- List.
- Inspect.
- Amend.
- Reprioritise.
- Reorder.
- Change dependencies.
- Pause.
- Resume.
- Cancel.
- Retry.
- Verify.

Provide interactive multiline task entry.

Provide file-driven task entry.

Examples:

```bash
llm-bridge --prompt-file task.md
llm-bridge task add --mission 202 --file additional-task.md
llm-bridge task list --mission 202
```

Finalise exact syntax consistently.

## 8.2 Runtime steering

Distinguish:

- New independent task.
- Amendment to active task.
- Replacement of remaining objective.
- Request for verification.
- Request for review.
- Pause after iteration.
- Stop after task.
- Explicit provider handoff.

Persist every instruction.

Apply changes at safe execution boundaries.

Do not inject instructions silently into an active CLI operation.

## 8.3 Task scheduling

Respect priority and dependencies.

Reject dependency cycles.

Define deterministic ordering.

Distinguish operator tasks from agent-generated proposals.

A provider handoff must not reset task progress.

---

# 9. Evidence-based progress and convergence

Do not rely on subjective LLM completion claims.

Progress must be based on durable, inspectable evidence.

Examples:

- Acceptance criteria passed.
- Tests passed.
- Verified files created.
- Integration checks completed.
- Review findings resolved.
- Explicit operator approval.

Show:

- Active task.
- Current step.
- Verified criteria.
- Remaining criteria.
- Last meaningful progress.
- Current provider/model.
- Handoff history.
- Blockers.
- Failures.
- Recovery actions.

Never invent progress percentages.

Use counts and percentages only when the denominator is defensible.

## 9.1 Detect lack of progress

Monitor:

- Repeated identical failures.
- No meaningful repository changes.
- Repeated retries.
- Repeated reversals.
- Provider cycling without advancement.
- Long periods without verified progress.

Use conservative detection.

Long-running builds or tests are not automatically stalls.

When a task is likely stalled, surface the condition and follow a bounded recovery policy.

Do not endlessly consume provider capacity without an improvement in evidence.

---

# 10. Provider capacity and handoffs

Reuse the existing capacity and route infrastructure.

Display:

- Provider.
- Route.
- Configured model.
- Verified actual model, if available.
- Reasoning effort.
- Capacity scopes.
- Remaining quota or budget.
- Reset time.
- Data freshness.
- Estimated time until exhaustion.
- Next eligible route.
- Unavailability reasons.

Do not invent missing data.

Do not display a fabricated percentage for opaque routes.

Label estimates appropriately.

When a provider exhausts capacity:

1. Persist task state.
2. Record the exhaustion event.
3. Use existing route selection.
4. Construct bounded handoff context.
5. Continue with the next eligible provider.
6. Preserve verification evidence.
7. Update UI and event history.

Do not pretend native provider conversation state transfers between different providers.

---

# 11. Durable session lifecycle

This is distinct from machine suspend.

A mission must survive termination of every Bridge and provider process.

Support:

| Action | Expected behaviour |
|---|---|
| Pause | Stop scheduling while keeping processes available |
| Resume mission | Continue scheduling |
| Detach | Leave mission running without UI |
| Attach | Reconnect to running mission |
| Stop session | Save state and terminate managed processes |
| Resume session | Reconstruct after complete shutdown |
| Cancel | Explicitly abandon mission |
| Suspend machine | Preserve running processes across OS sleep |
| Wake machine | Continue after OS wake |

These operations must have different implementations and well-defined semantics.

## 11.1 Graceful stop

Support an explicit stop command.

Example:

```bash
llm-bridge stop --mission 202
```

A graceful stop must:

1. Prevent new work.
2. Reach a safe checkpoint where possible.
3. Persist authoritative state.
4. Persist provider session metadata.
5. Flush events and logs.
6. Terminate managed processes.
7. Restore the terminal.
8. Mark the session stopped.

Support an explicit forced stop with clearly documented consequences.

If the outcome of an interrupted operation is uncertain, record that uncertainty.

## 11.2 Resume after complete shutdown

Support:

```bash
llm-bridge resume --mission 202
```

Reconstruct:

- Mission.
- Task queue.
- Active task.
- Priorities.
- Dependencies.
- Amendments.
- Verification evidence.
- Provider configuration.
- Routes.
- Models and effort.
- Scheduling policy.
- Provider session references.
- UI state.
- Event history.

Refresh capacity before scheduling.

Reconcile incomplete operations before replay.

Do not rely on an old tmux server or PTY remaining alive.

## 11.3 Provider-native resume

Investigate each provider's supported session-resume mechanisms.

Where supported, restore native sessions through documented interfaces.

Verify session identity where possible.

Where unsupported or unreliable, create a new provider session and reconstruct context from durable mission state.

Use explicit restoration states:

```text
NOT_STARTED
RESTORING
RESTORED_NATIVE
RECONSTRUCTED
UNAVAILABLE
FAILED
```

Never describe contextual reconstruction as exact native restoration.

## 11.4 Independent machine suspend/wake

Reuse the existing Ralph Robin RTC suspend/wake implementation.

Do not reimplement it.

Do not couple session stop/resume to machine suspend.

Verify the independence with automated tests.

---

# 12. Persistence and concurrency

Choose a simple, robust local persistence architecture.

Evaluate SQLite versus atomic structured files.

Use the option that best supports:

- Crash consistency.
- Safe concurrent task submission.
- Single active controller.
- Multiple observers.
- Atomic state transitions.
- Recovery.
- Schema migration.
- Maintainability.

No external database server.

No web listener.

No message broker.

Use local IPC where required.

Persist structured events and authoritative mission state.

Avoid duplicate sources of truth.

Do not store provider credentials.

Do not log secrets.

Treat provider output and repository content as untrusted with respect to operator commands.

---

# 13. Monitoring and diagnostics

Monitoring must be useful both interactively and headlessly.

Provide:

- Compact always-visible operational status.
- Detailed mission status.
- Task queue status.
- Provider/route capacity status.
- Session restoration status.
- Event timeline.
- Errors and warnings.
- Last meaningful progress.
- Stalled-work indicators.
- Execution duration and iteration count.
- Suspend/wake status.
- Persistence/checkpoint status.

Provide machine-readable output for automation.

Example:

```bash
llm-bridge status --mission 202 --json
```

Prefer event-driven updates.

Avoid tight polling loops.

Do not block the terminal UI while reading slow provider capacity APIs.

Use caching and asynchronous/background refresh where appropriate, following existing repository conventions.

Show stale or unavailable data honestly.

Ensure the system remains responsive under:

- Heavy provider output.
- Slow usage queries.
- Large task queues.
- Large event histories.
- Provider crashes.
- Terminal resizing.

---

# 14. Production testing strategy

The testing strategy must have **three complementary layers**.

None may substitute completely for the others.

## Layer A — Unit and contract tests

These run automatically on CI without installed provider CLIs or paid provider accounts.

Use deterministic fakes, fixtures and contract mocks.

Test:

- Mission lifecycle.
- Task transitions.
- Dependencies and priorities.
- Persistence.
- Concurrency.
- Session management.
- Provider adapter contracts.
- Route selection.
- Capacity exhaustion.
- Handoff construction.
- Restoration decisions.
- Keyboard command dispatch.
- Mouse event dispatch.
- Event history.
- Progress verification.
- Diagnostics.

Mocks must model real provider behaviour, not merely return success for expected calls.

Include error paths, unusual output, unexpected termination and protocol edge cases.

## Layer B — PTY and CLI integration tests

These also run automatically on CI.

Build realistic fake CLI processes that behave like interactive terminal programs.

They must exercise:

- ANSI escape sequences.
- Cursor movement.
- Alternate-screen mode.
- Interactive input.
- Bracketed paste.
- Mouse reporting.
- Terminal resizing.
- Native prompts.
- Slow output.
- Large output.
- Process exits.
- Signals.
- Crashes.
- Detached operation.

Run these tests through the same PTY and orchestration code used in production.

Do not bypass the real transport simply to make tests easier.

## Layer C — Real provider CLI tests

These validate actual compatibility.

Use installed, authenticated provider CLIs when available.

Prioritise:

1. Codex.
2. Claude Code.
3. GitHub Copilot CLI.
4. Other configured providers.

Investigate their actual installed versions and supported commands.

Test:

- Launch.
- PTY rendering.
- Native keyboard behaviour.
- Supported mouse interactions.
- Terminal resizing.
- Session lifecycle.
- Native resume where available.
- Clean termination.
- Integration with the Bridge.

Use a disposable working directory and a deliberately harmless prompt for real-provider smoke tests.

Keep real-provider calls bounded and resource-conscious.

Never perform destructive operations merely to test native integration.

Never interfere with unrelated provider sessions.

Respect provider rate limits and available capacity.

Real-provider smoke tests should be explicitly separated from the hermetic CI suite, with a small configurable call budget and clear opt-in for paid or state-changing probes.

If a provider is unavailable, unauthenticated or cannot be safely exercised, mark that specific integration check as **NOT VERIFIED**, not passed.

Do not block CI on credentials or paid services.

## 14.1 Contract drift

Provider CLIs may change independently of LLM Bridge.

Make provider-specific assumptions explicit.

Add fixtures derived from observed CLI behaviour where appropriate, excluding secrets.

Record tested CLI versions.

Provide diagnostics for unsupported or changed capabilities.

Avoid fragile parsing of terminal text where native commands or structured protocols are available.

## 14.2 CI requirements

CI must run:

- Unit tests.
- Provider contract tests.
- PTY integration tests.
- Persistence and recovery tests.
- Relevant static checks.
- Existing regression tests.
- Coverage validation.

Preserve the existing **85% minimum coverage gate**.

Aim for stronger coverage of newly introduced deterministic orchestration logic.

Coverage is a useful indicator, not proof of correctness.

Do not game coverage through exclusions or trivial assertions.

No live provider credentials should be required in CI.

## 14.3 Review test quality

The independent Luna reviewer must inspect tests as carefully as production code.

Look for:

- Mocks that cannot fail.
- Assertions that test implementation details instead of behaviour.
- Missing failure scenarios.
- Fake sessions that do not resemble actual CLIs.
- Untested concurrency paths.
- Untested restoration paths.
- Tests that accidentally depend on developer machine state.
- False positives from skipped tests.
- Missing evidence for claimed compatibility.

Require regression tests for significant defects.

---

# 15. Implementation workflow and durable progress tracking

This implementation may require multiple sessions and may encounter provider capacity exhaustion.

Therefore, implementation progress itself must be durable.

Create and maintain a compact implementation ledger in the repository, for example:

```text
docs/llm-bridge/
    architecture.md
    implementation-plan.md
    progress.md
    test-matrix.md
    review-findings.md
```

Adapt filenames to existing repository conventions if necessary.

Do not produce redundant documentation.

## 15.1 Milestone tracking

Divide the implementation into bounded milestones.

Each milestone must have:

- ID.
- Objective.
- Status.
- Relevant files.
- Dependencies.
- Acceptance criteria.
- Test evidence.
- Reviewer status.
- Remaining issues.

Use states such as:

```text
NOT_STARTED
IN_PROGRESS
IMPLEMENTED
TESTED
REVIEWED
DONE
BLOCKED
```

Do not mark a milestone DONE unless its required tests and review gates have passed.

## 15.2 Suggested milestones

### M0 — Repository investigation

Map existing architecture and reusable functionality.

### M1 — Architecture and contracts

Define persistence, process lifecycle, execution interfaces and terminal design.

### M2 — Native PTY feasibility

Prove keyboard, mouse, ANSI, resizing and clean termination.

### M3 — Durable mission and task model

Implement persistence, state transitions, dependencies and task commands.

### M4 — Orchestration integration

Connect missions and tasks to Ralph Robin and existing scheduler/usage infrastructure.

### M5 — Terminal command bridge

Implement main screen, navigation, keyboard and mouse interaction.

### M6 — Monitoring

Implement systems, stations, event history, progress and diagnostics.

### M7 — Session lifecycle

Implement attach/detach, stop/resume, native restoration and reconstruction.

### M8 — Long-running reliability

Implement and verify recovery, capacity exhaustion and suspend/wake integration.

### M9 — Real CLI validation

Run supported live smoke tests and record actual provider compatibility.

### M10 — Independent review and remediation

Complete substantive review, fix findings and rerun tests.

### M11 — Final verification and documentation

Run full CI-equivalent tests, update documentation and verify release readiness.

The planner may adjust this breakdown but must preserve the same acceptance coverage.

## 15.3 Update progress continuously

Update the durable ledger after each meaningful milestone or verified capability.

The record must answer:

- What is finished?
- What was tested?
- What remains?
- What is blocked?
- What failed?
- What was reviewed?
- What should happen next?
- Which files are involved?
- What command should be run to continue verification?

Avoid vague statements such as "mostly complete" or "nearly done".

Progress must be backed by evidence.

## 15.4 Checkpointing implementation work

At safe milestones:

1. Save production code.
2. Save tests.
3. Run focused verification.
4. Update progress.
5. Record unresolved issues.
6. Commit coherent changes where repository permissions and workflow permit.

Use descriptive commits.

Do not create meaningless micro-commits.

Do not overwrite unrelated changes.

Do not discard unfinished work merely because an agent context or provider session ends.

## 15.5 Resume after credit exhaustion

At the start of any resumed implementation context:

1. Read the implementation ledger.
2. Inspect repository status and recent commits.
3. Identify the last verified milestone.
4. Check uncommitted changes.
5. Re-run the smallest tests needed to establish current correctness.
6. Resume at the first unfinished acceptance criterion.

Do not restart the entire investigation.

Do not repeat completed milestones.

Do not assume prior agents' claims are correct without checking recorded evidence.

## 15.6 Strong convergence controls

Avoid endless analysis, refactoring or review loops.

Maintain an explicit list of outstanding blockers.

Prioritise in this order:

1. Correctness and data integrity.
2. Critical architecture issues.
3. Working end-to-end functionality.
4. Automated testing.
5. Real-provider compatibility.
6. Recovery and operational reliability.
7. Terminal responsiveness and usability.
8. Documentation.
9. Cosmetic polish.

When an implementation approach repeatedly fails, investigate root cause instead of blindly retrying.

When a reviewer identifies a defect, convert it into an actionable fix with an associated test.

After correction, verify the specific defect before broad re-review.

Do not reopen settled architectural decisions without new evidence.

Use bounded, evidence-driven iteration.

---

# 16. Independent review process

The Luna reviewer shall perform substantive review at meaningful milestones.

Mandatory review gates:

1. Persistence and mission lifecycle.
2. Native PTY and terminal interaction.
3. Provider orchestration and session reconstruction.
4. Final integrated implementation.

Do not run a full independent review after every trivial change.

For each finding, record:

- Severity.
- File and location.
- Reproduction or technical reasoning.
- Impact.
- Required fix.
- Verification method.

Severity:

- Critical.
- High.
- Medium.
- Low.

Critical and High findings block completion.

Medium findings should be corrected unless a clear, documented rationale supports deferral.

Low findings may be deferred when they do not affect correctness or important usability.

The implementor fixes the problem.

The reviewer verifies the correction.

The planner is consulted again only when a major architectural decision must change.

---

# 17. Required end-to-end acceptance scenarios

Implement automated coverage for the following wherever possible.

## A. Create a mission

Start LLM Bridge with a prompt file.

Task 1 is persisted and scheduled.

The Bridge displays its state.

## B. Keyboard-only operation

Every core function works without mouse input.

Provider-native shortcuts remain usable.

## C. Mouse interaction

Bridge controls respond to mouse input.

Native provider mouse features continue to work.

## D. Add tasks during execution

A multiline task is added from the TUI or another terminal.

The active task continues at a safe boundary.

The new task is persisted.

## E. Steering

An active task receives an amendment.

The instruction is delivered safely and recorded.

## F. Provider exhaustion

An executing provider exhausts capacity.

Ralph Robin selects another eligible route.

Task state and verification evidence survive.

## G. All providers exhausted

Execution waits or uses the existing safe suspend/wake mechanism.

No task state is lost.

## H. Detach/attach

The UI detaches while execution continues.

The operator reconnects to the same running mission.

## I. Graceful stop/resume

All managed processes stop.

Later the mission is reconstructed from durable state.

## J. Resume after reboot

The mission is reconstructed without requiring previous processes to exist.

## K. Native provider restoration

Where a provider supports session resume, restore the native session and verify the result.

## L. Contextual reconstruction

Where native resume is unsupported, recreate the session using durable context and report that accurately.

## M. Multiple providers

A mission previously used several providers.

After restart, its logical multi-provider configuration and station history are restored.

## N. Crash recovery

Terminate the Bridge unexpectedly during an operation.

Recover state correctly without falsely marking the task complete.

## O. Terminal fidelity

Validate ANSI rendering, alternate-screen behaviour, keyboard, mouse, resizing and cleanup.

## P. Long-running execution

Simulate many iterations, provider handoffs, exhaustion cycles and task transitions.

Verify no unbounded growth or unnecessary resource consumption.

## Q. Existing CLI compatibility

The original tools retain their documented behaviour and pass existing tests.

## R. Real-provider compatibility

Run bounded smoke tests against available real provider CLIs.

Record each provider/version/capability result explicitly.

## S. Implementation interruption

Simulate interruption of the development workflow itself.

Verify that another agent context can read the implementation ledger, locate the last verified state and continue without repeating completed work.

---

# 18. Performance and responsiveness

Treat responsiveness as a functional requirement.

The Bridge must remain interactive while:

- Providers stream large volumes of output.
- Usage data refreshes.
- Tasks are being persisted.
- Logs are growing.
- Provider sessions are switching.
- Background execution continues.

Use buffered or event-driven I/O.

Avoid tight polling.

Do not allow slow provider APIs to block terminal input.

Bound in-memory histories.

Avoid repeatedly rendering unchanged content.

Measure performance using representative stress tests rather than relying only on visual inspection.

Monitor:

- UI responsiveness.
- CPU usage.
- Memory growth.
- Event queue behaviour.
- Output throughput.
- Storage growth.
- Process cleanup.
- Terminal redraw frequency.

Choose practical performance thresholds based on measured baseline behaviour and target terminals.

Record the chosen thresholds and test results.

Do not invent benchmark results.

---

# 19. User-facing documentation

Update:

- README.
- AGENTS.md.
- Relevant design documentation.
- Configuration examples.
- CLI help.
- Packaging.
- CI configuration where required.

Document:

- Installation.
- Quick start.
- Product architecture.
- Missions.
- Orders/tasks.
- Keyboard shortcuts.
- Mouse behaviour.
- Native provider stations.
- Monitoring.
- Provider handoffs.
- Session attach/detach.
- Session stop/resume.
- Native versus reconstructed restoration.
- Crash recovery.
- Machine suspend/wake.
- Headless commands.
- Configuration.
- Testing.
- Known limitations.

Documentation must reflect verified behaviour.

Do not describe a capability as working if only its interface has been implemented.

---

# 20. Definition of done

LLM Bridge is complete only when:

1. The new CLI is installed and usable.
2. Missions and tasks are durable.
3. Runtime task submission and steering work.
4. The command bridge is responsive and intuitive.
5. Keyboard control is complete.
6. Mouse support works where supported.
7. Native provider interfaces are preserved.
8. Provider orchestration reuses existing infrastructure.
9. Capacity information is accurately represented.
10. Progress is evidence-based.
11. Detach/attach works.
12. Complete session stop/resume works.
13. Logical multi-provider restoration works.
14. Native resume is used where reliably available.
15. Contextual reconstruction is explicit elsewhere.
16. Crash recovery handles uncertain work safely.
17. Existing suspend/wake behaviour remains intact.
18. Existing CLI compatibility is preserved.
19. Unit and contract tests pass.
20. PTY integration tests pass.
21. CI can run without provider CLIs or credentials.
22. Available real-provider integration checks have been performed and accurately reported.
23. Existing coverage requirements pass.
24. No unresolved Critical or High review findings remain.
25. All substantial requirements have traceable verification evidence.
26. User documentation is accurate.
27. Implementation progress and review history are durably recorded.

A provider that cannot be tested live must be reported as unverified, not silently treated as passing.

Do not weaken quality gates to manufacture completion.

---

# 21. Final report

At completion, provide a concise engineering report with:

## Delivered functionality

What was actually implemented.

## Architecture

Important decisions, reuse and boundaries.

## Implementation status

Completed milestones and remaining work, if any.

## Tests

Actual commands executed, results and coverage.

## Real-provider verification

A matrix covering:

- Provider.
- Installed CLI version.
- Capabilities tested.
- Passed.
- Failed.
- Not verified.
- Known limitations.

## Independent review

Significant findings and corrective actions.

## Performance

Measured behaviour and any remaining concerns.

## Compatibility

Results of the existing regression suite.

## Recovery

What is restored natively, what is reconstructed and what remains uncertain.

## Known limitations

Any unmet or partially verified requirements, stated explicitly.

## Resume instructions

If implementation could not be fully completed, provide the exact next unfinished milestone and the command or procedure required to continue from the durable ledger.

Never report a test as passed if it was skipped.

Never report a feature as complete based only on generated code.

---

# 22. Final execution instructions

Start now.

**Sol 6.1 planner, Medium reasoning:** Investigate, choose the simplest defensible architecture, define contracts and establish the durable implementation milestones. Escalate to High only when necessary.

**Luna implementor, High reasoning:** Implement test-first. Use Extra High selectively for difficult terminal, concurrency, recovery and integration problems. Produce working code and objective test evidence continuously.

**Luna reviewer, Extra High reasoning:** Independently challenge the implementation at the defined review gates. Find and document substantive defects. Verify corrections before accepting them.

All agents must cooperate through concise handoffs and authoritative repository state.

Do not waste capacity repeating the same investigation.

Do not perform speculative refactoring unrelated to the product.

Do not mistake mock success for real compatibility.

Do not sacrifice reliability for visual effects.

Do not sacrifice simplicity for abstraction.

Do not sacrifice testing to finish faster.

Do not abandon progress when a provider runs out of capacity.

Maintain durable checkpoints and resume from the last verified state whenever execution is restored.

Continue autonomously through the complete implementation and review process, subject only to genuine external limitations.

**The end result must be a simple, beautifully designed, thoroughly tested and operationally reliable command bridge that can serve as the foundation for substantial future autonomous engineering work.**
