# Coordinator console UX audit

Date: 2026-11-06. Scope: coordinator web console, evaluated at 1440×900 desktop and the Playwright Pixel 4 profile (353×745 CSS px, DPR 3), light and dark. The deterministic scenarios are defined in `web/e2e/screenshots.spec.ts`; screenshots live under `web/e2e/__screenshots__/{chromium-desktop,chromium-mobile}/`. Axe is run on each screenshot scenario in Chromium and Firefox, desktop and mobile, for both themes.

This is the working principal-designer audit for UX-007–010. The baseline screens and code were inspected before the changes below; the old suite had no task-time, click-count, INP, or CLS instrumentation. Click counts for the old UI are therefore reconstructed from its controls and the screenshots in the parent revision, not claimed as timed user-study results. Timings labelled “measured” come from the added deterministic Playwright probes.

## Reference patterns adopted

- **Attention before inventory.** Linear's Priority Inbox opens on work requiring attention and separates it from lower-priority updates. The console now puts “Needs attention” before summary tiles and running work. [Linear Inbox](https://linear.app/docs/inbox)
- **A readable event trail.** Vercel's activity log presents events chronologically with actor, event type and time, and reveals exact time on demand. The console retains the event type and relative/exact timestamp and renders safe state transitions; it no longer prints opaque event JSON. [Vercel Activity Log](https://vercel.com/docs/activity-log)
- **Contextual actions with visible permission boundaries.** Raycast's action panel presents actions for the selected item and distinguishes the primary action from secondary actions. Mission output is view-only by default; “Take control” remains explicit and separate. [Raycast Search Bar and Action Panel](https://manual.raycast.com/search-bar)
- **Compact, scannable running status.** Existing status badges keep their text labels; running adds a small pulsing dot, while waiting, failure and offline retain distinct icon/color/text treatments. Motion only changes opacity and stops under reduced motion.

## Findings and disposition

Severity: P1 blocks understanding or an important task; P2 creates avoidable hunting or ambiguity; P3 is polish. Screenshot references name the corresponding deterministic state in both themes. `mission-live-output-*` specifically contains live provider output; `mission-detail-*` records the surrounding first-screen hierarchy.

| Screen | Before finding | Severity | Disposition / after evidence |
|---|---|---:|---|
| Overview | Counts preceded attention. Running missions had no task/provider summary. A first visit with workers but no missions had no next step. Repeated approval, failure and blocked rows made the attention list tall; on Pixel 4, activity was below the fold. | P1/P2 | Attention now precedes counts and groups multiple signals under one mission link; “Running now” links each mission with its current task/provider/model; no-mission state tells the operator to create a mission. `overview-*` and `overview-no-missions-*`. Recent activity remains secondary and can sit below the first screen on a phone. |
| Missions list | Rows showed status and folder but not current work, so an operator had to open each mission. | P2 | Running rows include bounded current-task/provider/model context from mission status. `missions-*`; `screenshots.spec.ts` asserts the current task summary. |
| Mission detail | The operator had to find “Open terminal” after status, tasks and events. Provider/model/current-task context was split between the Sessions page and terminal. | P1 | Current-work card is directly below the mission identity. The page opens the terminal over the scoped view-only WebSocket and streams output automatically. Mission actions follow the terminal. `mission-detail-*`, `mission-live-output-*`. |
| Terminal | Read-only and writer controls were easy to miss; default xterm ANSI red failed contrast when terminal output was present. | P1/P2 | Read-only is automatic for running missions; “Take control” stays explicit and switches only through the existing writer grant. A high-contrast ANSI palette fixes the axe finding. The test proves output arrives without a click and that typed input sends no WebSocket input frame. `mission-live-output-*`. |
| Workers | Host identity and connection address were not visible, making multi-worker status harder to distinguish. | P2 | DOG-011 adds validated host name and current online peer address. The existing `workers-*` screenshot/test covers both fields and offline state. |
| Sessions | Provider/model/task detail existed only in this view, disconnected from mission context. | P2 | Mission current-work summary now joins session context with the active task; the Sessions page remains the detailed inventory. `sessions-*`. |
| Usage | Capacity values are technical and can be unfamiliar to new operators. | P2 | Existing scope labels, readiness guidance and provider-specific details are retained. More explanatory copy and metric visualisation are not justified in this task because they risk implying a shared capacity scale. `usage-*`. |
| Diagnostics | Memory, DB and queue values are useful to operators but lack explanatory thresholds. | P2 | Kept as an explicitly technical admin page; the coordinator health sentence stays on Overview. The page is not changed by UX-007–010. `diagnostics-*`. |
| Account | Token lifecycle/security settings are not first-run tasks and can overwhelm a mission operator. | P2 | Kept behind Account, with existing scope and expiry descriptions. Session/passkey hardening is tracked separately under SEC2/PK and is not treated as closed by this UX pass. `account-*`. |
| Sign-in | Failed credentials gave unclear correction guidance. | P1 | DOG-012 now uses non-enumerating username-or-password copy and focuses/selects the password. `login-wrong-password-*`; auth UX test asserts copy and focus. |
| First run | The no-worker state did not clearly explain what a worker does or what follows. | P2 | No-worker empty state explains the role of a worker and gives one “Add a worker” action. The follow-up sequence for creating a mission is shown on Overview once a worker exists. `overview-no-workers-*`, `setup-empty-*`. |

All screen states are rendered in both themes at desktop and Pixel 4 and scanned by axe in all four browser/device projects. Visual baselines are authoritative in Chromium desktop and Pixel 4 only. Screenshots provide reproducible evidence; this is not a substitute for the three requested live-user persona sessions. The first-run-on-phone, five-missions/three-workers, and notification-link personas still need a real task-time observation before the full UX-001..006 claim can close.

## Before / after task measures

| Task | Before | After |
|---|---|---|
| Sign in | One submit action; baseline time was not instrumented. | One submit action; DOG-012 copy/focus fix verified by `auth-ux.spec.ts`. No timing claim. |
| Find a mission waiting for approval | Attention was present but followed the summary tiles; selecting the mission required one link click. | Attention is first after the page heading; one mission link click. No separate search or scroll is needed. |
| Approve a task | Two actions: Approve, then confirm. | Two actions: Approve, then confirm; server outcome time not instrumented. |
| Open provider output from mission detail | One “Open terminal” click after scrolling past status, tasks and events. | Zero clicks: the view-only terminal connects automatically for a running mission. Status-response to visible output measured at **218 ms desktop** and **179 ms Pixel 4** in the fixed WebSocket fixture. The regression test enforces under 1,000 ms and no input frames. |
| Check worker health | One Workers navigation click, then scan the table. | One Workers navigation click; host/address and state are visible in the table. No task timer instrumented. |

The output latency is the interval from the mission-status HTTP response to visible provider output, not total navigation latency or a live-network measurement. The seeded output is on a WebSocket fixture; it does not include worker or LAN delays.

## Layout, interaction, bundle and accessibility

- Pixel 4 journey (Chromium, emulated 353×745 CSS px, 4× CPU throttle, loopback): five runs measured **CLS 0.000 median / 0.000 max** and maximum observed interaction-event duration (**INP lab proxy**) **48 ms median / 104 ms max**, with one click from Overview to the waiting mission. A prior single exploratory run recorded 0.071 CLS and 216 ms; the five-run probe is the maintained result. The regression test requires every run below 0.1 CLS.
- Current built bundle: initial JS **67,479 B raw / 22,940 B gzip**; initial CSS **30,517 B / 7,388 B**; total **97,996 B / 30,328 B**. Lazy chunks total **363,540 B / 95,691 B**. These remain under `web/budget.json` (100,000 B raw / 32,000 B gzip initial and 400,000 B / 100,000 B lazy). The budget has not been raised.
- Live-output screenshots pass axe with zero violations after replacing xterm's low-contrast ANSI palette. The existing full screenshot suite checks all impact levels, including serious and critical; the focused updated desktop/mobile states pass.
- Lighthouse scores and INP/CLS for the project site are not measured here. The full site UX-004/005 work remains open.

## Remaining work

This audit closes the specific live-output discovery gap and adds current-work summaries. The broader principal UX pass remains incomplete: capture repeatable timing for the other core tasks, complete the three persona walk-throughs, review/resolve every hesitation on every screen, measure the site with Lighthouse, collect five-run mobile performance summaries, and finish the site redesign. Do not mark UX-001–006 complete on the strength of this document alone.
