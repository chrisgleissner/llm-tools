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

## Activity language and filters (UX-011)

Overview and mission detail now share a browser phrase table generated from the production TUI humanizers. Task, command and provider activity is visible by default; bookkeeping events can be expanded as system activity. Mission detail offers both category and event-kind filters. Event payload rendering uses an explicit field allowlist, and a command-details link appears only when an event includes a valid request ID. `web/test_event_phrases.py` checks the generated artifact against every TUI kind; `web/e2e/ui-features.spec.ts` exercises humanized events, filters, collapsed system activity, command links, and rejection of an unlisted private payload field on desktop and Pixel 4.

The event stream does not currently persist worker online/offline or terminal lifecycle history. The mission page adds worker-state entries only from real coordinator stream messages and terminal-session entries only from real terminal status/end frames, so these are live-only and appear after the page is open. Terminal frames do not include a session ID. Mission command events store a local command ID rather than the coordinator request ID; command detail links are shown only when the actual request ID is present.

## Before / after task measures

| Task | Before | After |
|---|---|---|
| Sign in | One submit action; baseline time was not instrumented. | One submit action; DOG-012 copy/focus fix verified by `auth-ux.spec.ts`. No timing claim. |
| Find a mission waiting for approval | Attention was present but followed the summary tiles; selecting the mission required one link click. | Attention is first after the page heading; one mission link click. No separate search or scroll is needed. |
| Approve a task | Two actions: Approve, then confirm. | Two actions: Approve, then confirm; server outcome time not instrumented. |
| Open provider output from mission detail | One “Open terminal” click after scrolling past status, tasks and events. | Zero clicks: the view-only terminal connects automatically for a running mission. Latest pinned Chromium status-response to visible output measurements are **193 ms desktop** and **153 ms Pixel 4** in the fixed WebSocket fixture. The regression test enforces under 1,000 ms and no input frames. |
| Check worker health | One Workers navigation click, then scan the table. | One Workers navigation click; host/address and state are visible in the table. No task timer instrumented. |

The output latency is the interval from the mission-status HTTP response to visible provider output, not total navigation latency or a live-network measurement. The seeded output is on a WebSocket fixture; it does not include worker or LAN delays.

## Layout, interaction, bundle and accessibility

- Pixel 4 journey (Chromium, emulated 353×745 CSS px, 4× CPU throttle, loopback): the latest five-run probe measured **CLS 0.000 median / 0.000 max** and maximum observed interaction-event duration (**INP lab proxy**) **72 ms median / 96 ms max**, with one click from Overview to the waiting mission. A status skeleton reserves the mission-status space while online data is pending; the prior uncushioned state reached 0.409 CLS under the same loaded scenario. The regression test requires every run below 0.1 CLS.
- Current built bundle: initial JS, including transitive static imports, **88,506 B raw / 30,849 B gzip**; initial CSS **29,977 B / 7,526 B**; total **118,483 B / 38,375 B**. The terminal-only lazy payload is **352,470 B / 91,000 B**. `web/e2e/budget.spec.ts` now follows the built static/dynamic import graph; its earlier accounting excluded shared static chunks from initial load. `web/budget.json` sets measured caps of 124,000 B raw / 40,000 B gzip initial and 360,000 B / 93,000 B lazy.
- Updated desktop and Pixel 4 screenshots pass with the current hierarchy in both themes. The latest pinned screenshot run exercised axe for every captured state, including serious and critical impact levels; 147 tests passed and three viewport-specific states were skipped. The Pixel 4 journey and live-output tests pass independently.
- Project-site Lighthouse scores are recorded below. Site INP/CLS and five-run metrics remain unmeasured; UX-004/005 stay partial.

## Project site first screen and distributed section

The site was audited from its rendered first viewport before editing, using the same local static server and Chromium viewport sizes before and after. These are layout measurements, not user-study, Lighthouse, INP or CLS scores.

| Viewport | Before: hero demo position | After: hero demo position |
|---|---|---|
| 1440×900 | Terminal begins at y=716 and its frame ends at y=1140; only 184 px of the 424 px demo frame is visible. | The direct headline is one line; the complete demo and caption end at y=627. |
| 393×851 | Terminal begins at y=679 and its frame ends at y=1210; the demo is cut off after 172 px. | The complete demo and caption end at y=699. The install tabs start at y=731. |

The old headline (“Run out of tokens, not momentum”) expressed a benefit but did not name the product's job. It now says “Track AI usage. Keep agents moving.” The short lede explains provider switching and Bridge mission review, and the terminal demonstration now appears before installation details. The install tabs, pinned 1.3.0 command, Python/platform requirements, Apache-2.0 statement and first-run link remain in the hero.

The distributed section now has a direct heading and a short description of the deployment boundary: provider CLIs and credentials stay on workers, workers connect out, and the browser console or remote CLI uses the coordinator. Its summary is three points instead of four; loopback/LAN opt-in, local assets, shared control surface, the measured Pi memory figure and install/benchmark links remain. The architecture diagram and sample console screens are preserved.

| Lighthouse 13.6.0, mobile emulation | Before | After |
|---|---:|---:|
| Performance | 97 | 97 |
| Accessibility | 100 | 100 |
| Best practices | 100 | 100 |
| SEO | 100 | 100 |

The two reports were collected from the pre-edit `HEAD` snapshot and the changed site using local assets and a local static server. These are single-run Lighthouse scores, not repeated performance or live-user measurements. The project-site scores are now measured; persona sessions, broader task timings, and site INP/CLS remain open under UX-001/005.

## Remaining work

This audit closes the live-output discovery gap, adds current-work summaries, records the site Lighthouse scores and Pixel 4 mission-journey metrics, and documents the site hero/distributed-section changes. The broader principal UX pass remains incomplete: capture repeatable timing for the other core tasks, complete the three persona walk-throughs, review/resolve every hesitation on every screen, collect site INP/CLS, and finish the console redesign. Do not mark UX-001–006 complete on the strength of this document alone.
