# DOG requirements

Requirement text is taken from spec-addendum-2026-11-06.md, spec-addendum-2026-11-12.md. DOG-017 to DOG-025 were registered directly from dog-food and review findings without a separate requirement text; their lines summarise the obligation stated in the register row.

- DOG-001: Container image serves the web console (fixed on the Pi branch, PR #32; the follow-up registers it `[x]` citing that PR after merging `main`).
- DOG-002: Coordinator mission list current while a worker is online; `stale` only when actually behind; every row links (DF-002 and DF-006; mission task 2).
- DOG-003: Start a mission from the web console (covered by API and UX work).
- DOG-004: `llm-bridge finish` ends the provider of a PAUSED native mission, or the stop message names what works.
- DOG-005: `llm-bridge worker status` shows connection state in human-readable form.
- DOG-006: Every mission row in the console links to its mission (with DOG-002).
- DOG-007: A native provider idle at its prompt after ending a turn is surfaced as waiting for the operator (`needs_you`, console, notifications).
- DOG-008: Web terminal: Take control upgrades an open view in place; typed text followed by Enter is submitted by the provider.
- DOG-009: Read-only screen snapshot of a native mission from the CLI (or the provider's last message in `status`).
- DOG-010: A provider's structured question to the operator (Codex question UI, Claude Code AskUserQuestion) is surfaced as `needs_you` with its text and can be answered from the CLI and the console.
- DOG-011: Workers show host name and connection address beside the enrolment name (mission task 4).
- DOG-012: Failed sign-in shows a clear, announced, non-enumerating error (mission task 6).
- DOG-013: A pending provider question on screen (Codex "? N questions … to answer") sets `needs_you` with its text everywhere (mission task 7).
- DOG-014: Slow password sign-in shows a visible, announced progress state (mission task 7).
- DOG-017: A task added or amended while a native provider works is announced to that provider as one coalesced notice.
- DOG-018: An early wake must not leave an unmatched suspend start in the suspend ledger that makes later starts warn about a wedged resume.
- DOG-019: Worker sends must tolerate a slow link without the connection dropping every few seconds (the slow-link chaos case).
- DOG-020: Replication after a reconnect must be flow-controlled and must not replicate plumbing events in bulk.
- DOG-021: The console must not issue per-event status requests for every active mission on each stream message.
- DOG-022: Event payloads, command requests and terminal queues are bounded in size and count, and overload is shed rather than buffered without limit.
- DOG-023: Browser overview, list and detail views use compact replicated summaries and bounded read-only event pages, so host SD-card writes stay low.
- DOG-024: Backup must not block serving the coordinator database.
- DOG-025: A native provider's final line must not be lost when it exits at once on tmux 3.4.
- DOG-027: `llm-bridge finish` typed `/quit` into Codex without submitting it. Test with the fake provider that requires the submit key.
