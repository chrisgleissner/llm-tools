# UX requirements

Requirement text is taken from spec-addendum-2026-11-06.md.

- UX-001: Audit with the three personas at 1440x900 and Pixel 4, both themes, times and clicks per core task, recorded in `ux-audit.md` (before).
- UX-002: Reference patterns adopted, each with its reason.
- UX-003: Console redesign where the audit shows a need: hierarchy, speed (prefetch, optimistic updates, skeletons, no layout shift, Ctrl/Cmd-K palette), clarity, craft, mobile, first run.
- UX-004: Site: one-line hero showing the product in the first screen; shortest distributed section.
- UX-005: After-measurements next to the before figures: time and clicks per task, Lighthouse four scores for the site, bundle size, INP and CLS at Pixel 4 with CPU throttling; axe-core zero serious or critical.
- UX-006: Visual baselines updated in the same commits as the visible change, every hesitation point resolved or justified, bundle budget kept or raised for a measured reason.
- UX-007: Provider output (Codex, Claude Code, Copilot session) visible live on the mission page and summarised on the overview without a click; typing stays an explicit `terminal:write` action.
- UX-008: Subtle, professional activity indicators on every running item; distinct idle, waiting, rate-limited and offline states; reduced motion respected; no layout shift.
- UX-009: Clarity and guidance on every page: what is happening, does it need me, what next; teaching empty states; waiting-for-you most prominent.
- UX-010: Principal UX designer critique of every screen at desktop and Pixel 4 in both themes, every fault fixed or justified in `ux-audit.md`.
- UX-011: Meaningful event stream: plumbing (heartbeats, capacity refreshes) collapsed by default, humanised phrases shared with the TUI, distributed exchanges (commands, delivery, outcomes, worker connections, terminal sessions, provider turns) visible with filters.
