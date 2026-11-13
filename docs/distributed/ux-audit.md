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
| Workers | Host identity and connection address were not visible, making multi-worker status harder to distinguish; the online state looked static beside the running mission indicators. | P2 | DOG-011 adds validated host name and current online peer address. Online now uses the same restrained activity dot as running missions and stops animating under reduced motion. `worker.spec.ts` verifies both preferences in Chromium and Firefox on desktop and Pixel 4; `workers-*` screenshots cover both themes. |
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

| Task | Before (pre-redesign `9ad0c03`) | After (current UI) |
|---|---|---|
| First-run sign-in | One submit click; **555 ms desktop / 604 ms Pixel 4** from page navigation to Overview. | One submit click; **517 ms desktop / 503 ms Pixel 4** on the same setup fixture. Failure recovery copy/focus is separately verified by `auth-ux.spec.ts`. |
| Find a mission waiting for approval | Approval link followed the summary tiles; one link click, **53 ms desktop / 56 ms Pixel 4** to mission heading. | Attention is above the summary tiles; one mission link click, **68 ms desktop / 44 ms Pixel 4** to mission heading. Neither version needs a search or scroll action in this seeded scenario. |
| Approve a task | Two clicks (Approve, then confirm), **151 ms desktop / 150 ms Pixel 4** to outcome toast. | Two clicks, **171 ms desktop / 141 ms Pixel 4** to outcome toast. |
| Open provider output from mission detail | One “Open terminal” click; **348 ms desktop / 374 ms Pixel 4** from navigation to visible output in the fixed WebSocket fixture. | Zero clicks: the view-only terminal connects automatically for a running mission; **458 ms desktop / 429 ms Pixel 4** from navigation to visible output in the same fixture. Status-response to output was 163 ms / 159 ms in this rerun. The regression test enforces under 1,000 ms and no input frames. |
| Check worker health | One Workers navigation click, **106 ms desktop / 87 ms Pixel 4** to online state. | One Workers navigation click, **100 ms desktop / 96 ms Pixel 4** to online state; host/address are also visible in the table. |

The task timings are single-run deterministic Playwright measurements on seeded state, not representative user-study timings. They start at navigation/submit/click and stop when the destination heading, toast or online state is visible; the UI timings have normal local-run noise, so differences of a few dozen milliseconds are not treated as product speedups. Click counts are the actual visible actions in those flows. The before run used the old source and bundled assets at `9ad0c03`; both before and after used the same current coordinator binary and worker fixture.

The output latency is the interval from the mission-status HTTP response to visible provider output, not total navigation latency or a live-network measurement. The seeded output is on a WebSocket fixture; it does not include worker or LAN delays.

## Layout, interaction, bundle and accessibility

- Pixel 4 journey (Chromium, emulated 353×745 CSS px, 4× CPU throttle, loopback): the latest normal-mode five-run probe on 2026-11-07 measured **CLS 0.000 median / 0.000 max** and maximum observed interaction-event duration (**INP lab proxy**) **56 ms median / 112 ms max**, with one click from Overview to the waiting mission. A status skeleton reserves the mission-status space while online data is pending; the prior uncushioned state reached 0.409 CLS under the same loaded scenario. The regression test requires every run below 0.1 CLS.
- Current built bundle after the online-worker marker: initial JS, including transitive static imports, **88,506 B raw / 30,849 B gzip**; initial CSS **30,037 B raw / 7,534 B gzip**; total **118,543 B / 38,383 B**. The terminal-only lazy payload is **352,470 B / 91,000 B**. `web/e2e/budget.spec.ts` now follows the built static/dynamic import graph; its earlier accounting excluded shared static chunks from initial load. `web/budget.json` sets measured caps of 124,000 B raw / 40,000 B gzip initial and 360,000 B / 93,000 B lazy.
- Updated Chromium desktop and Pixel 4 screenshots pass with the current hierarchy in both themes. The 2026-11-07 normal-mode run exercised axe for every captured state, including serious and critical impact levels; 147 tests passed and three viewport-specific states were skipped. It also reran the Pixel 4 journey and live-output checks.
- Project-site Lighthouse and five-run Pixel 4 lab stability measurements are recorded below. The event-duration proxy is not field INP and should not be read as a real-user Web Vitals sample; UX-004/005 stay partial pending personas and CI evidence.

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

The two reports were collected from the pre-edit `HEAD` snapshot and the changed site using local assets and a local static server. These are single-run Lighthouse scores, not repeated performance or live-user measurements. Persona sessions and broader task timings remain open under UX-001/005.

For a site stability probe, Chromium loaded the built local site five times at Pixel 4 emulation (353×745 CSS px, DPR 3) with 4× CPU throttling. Each visit switched the install tab and theme. Cumulative layout shift was **0.000 on all five visits**. The maximum observed `PerformanceEventTiming.duration` values were **408, 64, 72, 56 and 56 ms** (median 64 ms); this is a lab interaction-duration proxy, not field INP. The initial visit's 408 ms outlier motivates keeping the startup path under observation even though CLS stayed at zero.

## Click-path inventory (UXR-043 to UXR-046)

Every operator task of the web console, the shortest path that keeps destructive actions confirmed, and how many interactions it takes. The numbers are measured, not estimated: `web/e2e/redesign/uxr-paths.spec.ts` performs each path with the pointer in the real application on the deterministic mock and counts what `web/e2e/redesign/interactions.ts` counts (a trusted pointer press, a run of typing as one interaction, every other key press that is not a bare modifier). The test fails when a path takes more interactions than its row (a regression) and when it takes fewer (the row must be lowered), at 1440x900, 320x568, 360x640 and the Pixel 4 profile (353x745); the phone column holds for all three phone sizes. The table below is generated from `web/e2e/redesign/click-paths-data.ts` by `node web/scripts/click-paths-doc.mjs`, and a static test fails when the document differs from the data, so the document, the data and the measurement cannot drift apart. Wording of one step: "(phone: ...)" marks the step a phone needs and a desktop does not (the Tasks tab, because the side column is a tab below 60rem).

Three tasks are not offered by the web console and are listed so the gap is visible: amending a task prompt, reordering tasks and force-stopping a mission are CLI-only. The design has no drag reordering (spec 8.1), and Stop is the graceful stop.

<!-- click-paths:begin (generated by web/scripts/click-paths-doc.mjs from web/e2e/redesign/click-paths-data.ts; do not edit by hand) -->

| Task | Starts at | Path | 1440x900 | 320x568 | Keyboard alone (desktop) |
|---|---|---|---:|---:|---|
| Sign in | Sign-in page | type the username (the field has focus), Tab, type the password, Enter | 4 | 4 | type, Tab, type, Enter |
| Sign out | Any page | account menu, Sign out | 2 | 2 | Ctrl+K, type "sign out", Enter |
| See what needs me | Inbox (the home page) | the Needs you list is the first thing on the page | 0 | 0 | none needed |
| Open a live session from the Inbox | Inbox | mission title in a Running row | 1 | 1 | Ctrl+K, type the mission name, Enter |
| Open a session from the Sessions page | Any page | Sessions in the navigation, mission link of the session | 2 | 2 | g s, Tab to the mission link, Enter |
| Watch a running session | Deep link to a mission (a notification, a bookmark) | the session connects read-only by itself | 0 | 0 | none needed |
| Take control and type | Mission page | Take control, type, Enter | 3 | 3 | c, Enter (Take control), type, Enter |
| Steer: pause after this iteration | Mission page | Steer menu, Pause after this iteration | 2 | 2 | c, Tab to Steer, Enter, ArrowDown to the item, Enter |
| Steer: replace a task prompt | Mission page | Steer menu, Replace a task prompt, type, Send | 4 | 4 | c, Tab to Steer, Enter, ArrowDown to the item, Enter, type, Enter |
| Answer a provider question | Inbox, a row that says the provider asks | Answer on the row (the page opens with Take control focused), Take control, type, Enter | 4 | 4 | j (select the row), Tab to Answer, Enter, Enter (Take control), type, Enter |
| Approve a task | Inbox | Approve on the row, Approve in the dialog | 2 | 2 | j, a, Enter (the dialog's Approve has focus) |
| Approve a task from the mission page | Mission page, state Needs approval | Approve in the header, Approve in the dialog | 2 | 2 | a, Enter |
| Reject a task with a reason | Inbox | Reject on the row, type the reason, Reject in the dialog | 3 | 3 | j, r, type, Tab to Reject, Enter |
| Pause a mission | Mission page, running | Pause | 1 | 1 | p |
| Resume a mission | Mission page, paused | Resume | 1 | 1 | Tab to Resume, Enter |
| Stop a mission | Mission page, running | Stop, Stop in the dialog | 2 | 2 | Tab to Stop, Enter, Enter |
| Start a mission that is not running | Mission page, created or stopped | Start | 1 | 1 | Tab to Start, Enter |
| Retry a failed task | Mission page, failed | Retry task | 1 | 1 | Tab to Retry, Enter |
| Add a task | Mission page | (phone: Tasks tab,) Add task, type, Enter | 3 | 4 | Tab to Add task, Enter, type, Enter |
| Cancel a task | Mission page | (phone: Tasks tab,) the task's menu, Cancel task, Cancel task in the dialog | 3 | 4 | Tab to the task menu, Enter, ArrowDown to Cancel task, Enter, Enter |
| Read the evidence of a task | Mission page | (phone: Tasks tab,) the task title | 1 | 2 | Tab to the task title, Enter |
| Read what happened (Activity) | Mission page | Activity tab | 1 | 1 | Tab to the tab list, ArrowRight to Activity |
| Find a mission by name | Any page | search button or Ctrl+K, type the name, Enter | 3 | 3 | Ctrl+K, type, Enter |
| Check provider capacity | Any page | the chips in the header show provider, scope and percent | 0 | 0 | none needed |
| Open Usage for every provider | Any page | a capacity chip (phone: Usage in the bottom bar) | 1 | 1 | g u |
| Check a worker | Any page | Workers in the navigation | 1 | 1 | g w |
| Add a worker and read its enrolment token | Workers page | Add worker, type the name, Enter | 3 | 3 | Tab to Add worker, Enter, type, Enter |
| Find out how to reset a forgotten password | Sign-in page | the hint is on the page; Reset it from the host shows the command | 1 | 1 | Tab to Reset it from the host, Enter |
| Amend a task prompt | - | not offered in the web console (llm-bridge task amend) | n/a | n/a | - |
| Reorder tasks | - | not offered in the web console (llm-bridge task move) | n/a | n/a | - |
| Force stop a mission | - | not offered in the web console (Stop is graceful; llm-bridge stop --force) | n/a | n/a | - |

<!-- click-paths:end -->

### Every typing task above an on-screen keyboard (UXR-045)

`web/e2e/redesign/uxr-keyboard.spec.ts` performs each task that needs typing (sign in, take control and type, steer with a note, answer a provider question, reject with a reason, add a task, add a worker, find a mission) at 320x568, 360x640 and 353x745 with an on-screen keyboard that covers half of the screen. The keyboard is simulated the way the viewport meta declares it (`interactive-widget=resizes-content`): when a field is about to take typing the window height is halved (284 px at 320x568, 320 px at 360x640) and the field is focused again, which makes the browser scroll it into view as it does when a real keyboard opens. The test then requires that the focused field, the control that submits it and any validation message are inside the visible half and that nothing sits over their centre, without the test scrolling. Four more tests trigger the validation message of the sign-in (wrong password), the reject reason (empty), the new task (empty) and the worker name (invalid) with the keyboard open and require the message, the field and the submit control to be visible together. One test covers a keyboard that overlays the page (iOS Safari: the visual viewport is halved, the layout viewport is not) for the composer.

Layout changes this needed:

- **Dialogs with a form** (reject reason, steer note, add worker, create token): the body of the dialog scrolls and the buttons sit below it in a flex column, so on a phone the buttons are always visible and nothing can sit over the field. A sticky button row was tried first; Chromium's focus scroll does not account for it and left the reason field under the buttons.
- **Sign-in on a phone with a field focused**: the heading, intro, "Remember this device" and the hints give way (`body[data-typing]`, set by `shell.ts` while a text field has focus and cleared when focus leaves or the field is removed), so the two fields, the error and Sign in fit the 284 px. The reset hint stays unless an error is showing. Tab to the options brings them back.
- **Validation messages** are scrolled just into view (`reveal()` in `dom.ts`) when they are set, so a message under a field is not left below the keyboard.
- **The bottom bar** is hidden while a text field has focus on a phone (`body[data-typing]`), as it already was for the composer (`body[data-kb]`).
- **Mission page at 320x568.** The composer and the key bar sat below the fold (composer at y 550-599, key bar 599-689, bottom bar at 511). Now: the key bar fits one row (seven keys of 44 px need the pane edge to edge and a 1 px gap at 320), the session bar is one row of icon buttons below 36rem height (labels stay for screen readers), a key bar whose keys are all disabled (view only) is not shown below 40rem height (taking control shows it), the empty "pan" note no longer takes 16 px of margin in the pane (it carries its text for screen readers now), and the state's actions are docked above the bottom bar (next section). The composer ends at y 457 and the dock starts at 458.
- **`?focus=composer`** (the Answer link of an Inbox row, spec 8.3) was ignored by the mission page. The page now consumes it, and the session pane moves focus to the message field or to Take control as soon as either can take focus, which is after the socket connects.

### Thumb reach and keyboard paths (UXR-046)

Measured rule for phones (`web/e2e/redesign/uxr-reach.spec.ts`): the centre of the primary control of a state is in the lower 60% of the visible viewport (its y is at least 0.4 of the height). 60% of a 568 to 745 px tall screen is about 9 cm on a 6 inch phone, the arc a thumb sweeps from the lower corner of the grip. A control inside a scrolling list (an Inbox row) may be higher on first view because the list scrolls it into the zone; the test then requires that the page can scroll far enough. The Approve, Reject, Pause, Stop, Resume, Start and Retry buttons of the mission page were in the top third of the screen (y about 184 at 568 and 745); on phones they are now docked in a bar above the bottom navigation, hidden while a field has the keyboard and not used on screens below 30rem height (400% zoom, landscape phones), where two fixed bars would leave no room.

Keyboard alone, on a desktop (`web/e2e/redesign/uxr-keyboard-only.spec.ts`): each inventoried task is completed with Tab, Enter, Escape, the arrows, the shortcuts of the `?` dialog and the command palette (last column of the table), and the test fails if any step is a pointer press. The three tasks the console does not offer have no keyboard path to test.

## Remaining work

This audit closes the live-output discovery gap, adds current-work summaries, records the site Lighthouse scores and Pixel 4 mission-journey metrics, and documents the site hero/distributed-section changes. The broader principal UX pass remains incomplete: complete the three persona walk-throughs, review/resolve every hesitation on every screen, obtain field INP (the lab proxy above is not a field sample), and finish the remaining console refinements. Do not mark UX-001–006 complete on the strength of this document alone.

## Fresh-eyes critique (UXR-040)

One screenshot per page, width (1440x900 and 360x640) and theme, taken on the deterministic mock (dense data for Inbox and Missions, a running mission with a terminal for Mission). The files are produced by `web/e2e/redesign/uxr-critique-capture.spec.ts` (`CRITIQUE_CAPTURE=1`; see the header of that file). The check `T46` in `uxr-critique.spec.ts` requires each file listed below to exist. The paragraph of a page applies to all four of its screenshots.

### Inbox

At 1440 the page answers the first question (15 need you, split by reason) and lists failed and approval rows before running ones, with the primary action (Retry, Approve) in each row and a preview of the selected row beside the list. At 360 the same rows fit as cards with the action under the title, the bottom navigation carries the badge, and no horizontal scroll occurs. Faults: at 1440 the reason line of an approval row is cut in the middle of a word ("Task 2 awaits yo...- 2 files") and the "Needs approval" pill wraps to two lines; at 360 the quota pill in the top bar shows "Claude resets 12:25" without the remaining percentage and omits Codex (see Proposed rows).

- inbox 1440 light: `docs/distributed/ux-evidence/UXR-040/inbox-1440-light.png`
- inbox 1440 dark: `docs/distributed/ux-evidence/UXR-040/inbox-1440-dark.png`
- inbox 360 light: `docs/distributed/ux-evidence/UXR-040/inbox-360-light.png`
- inbox 360 dark: `docs/distributed/ux-evidence/UXR-040/inbox-360-dark.png`

### Missions

The list is dense and scannable at 1440: state pill, name, task progress, provider, worker and age in aligned columns, with the filters above it and the attention states sorted first. At 360 each row becomes a two-line card with the pill on the first line and the facts on the second. Fault: at 1440 rows of failed missions leave the provider column blank, so the age column looks detached.

- missions 1440 light: `docs/distributed/ux-evidence/UXR-040/missions-1440-light.png`
- missions 1440 dark: `docs/distributed/ux-evidence/UXR-040/missions-1440-dark.png`
- missions 360 light: `docs/distributed/ux-evidence/UXR-040/missions-360-light.png`
- missions 360 dark: `docs/distributed/ux-evidence/UXR-040/missions-360-dark.png`

### Mission

At 1440 the terminal, tasks and evidence sit side by side, the state pill and the one-line reason are beside the title, and the primary action (Approve) is the first button. At 360 the page switches to Session, Tasks and Activity tabs and pins the Approve, Pause and Stop bar above the navigation, so the decision is always one tap away. Fault: at 360 the terminal is wider than the screen and its text is cut at the right edge; it scrolls horizontally, but nothing indicates that.

- mission 1440 light: `docs/distributed/ux-evidence/UXR-040/mission-1440-light.png`
- mission 1440 dark: `docs/distributed/ux-evidence/UXR-040/mission-1440-dark.png`
- mission 360 light: `docs/distributed/ux-evidence/UXR-040/mission-360-light.png`
- mission 360 dark: `docs/distributed/ux-evidence/UXR-040/mission-360-dark.png`

### Sessions

A one-sentence answer states that one session runs and nothing needs the operator, which matches the first question a visitor asks. The running session shows provider, model, mission link, task and age; history is collapsed. At 360 the row wraps cleanly. No fault found; the page is mostly empty space at 1440, which is correct for one session.

- sessions 1440 light: `docs/distributed/ux-evidence/UXR-040/sessions-1440-light.png`
- sessions 1440 dark: `docs/distributed/ux-evidence/UXR-040/sessions-1440-dark.png`
- sessions 360 light: `docs/distributed/ux-evidence/UXR-040/sessions-360-light.png`
- sessions 360 dark: `docs/distributed/ux-evidence/UXR-040/sessions-360-dark.png`

### Workers

The summary names the offline worker and the command to start it, and the offline worker is listed first with a "Why offline?" disclosure. Add worker is the single primary button. At 360 the rows keep the same content on two lines and the primary button keeps its size. No fault found.

- workers 1440 light: `docs/distributed/ux-evidence/UXR-040/workers-1440-light.png`
- workers 1440 dark: `docs/distributed/ux-evidence/UXR-040/workers-1440-dark.png`
- workers 360 light: `docs/distributed/ux-evidence/UXR-040/workers-360-light.png`
- workers 360 dark: `docs/distributed/ux-evidence/UXR-040/workers-360-dark.png`

### Usage

The headline sentence states which provider waits and until when. Each provider card shows one meter per window with the percentage left and the reset time, with details collapsed. At 360 the cards stack. Fault: at 360 the "All workers" filter next to the heading is clipped ("All worker"; see Proposed rows). At 1440 the 4 percent bar of the Claude meter in the top bar is nearly invisible in the light theme.

- usage 1440 light: `docs/distributed/ux-evidence/UXR-040/usage-1440-light.png`
- usage 1440 dark: `docs/distributed/ux-evidence/UXR-040/usage-1440-dark.png`
- usage 360 light: `docs/distributed/ux-evidence/UXR-040/usage-360-light.png`
- usage 360 dark: `docs/distributed/ux-evidence/UXR-040/usage-360-dark.png`

### Diagnostics

The opening paragraph turns the numbers into a statement (a command waits for a worker, check the named workers), and the grids below list the facts. At 360 the grids keep their label and value columns without wrapping. No fault found; at 1440 the cards use about 45 percent of the width, which keeps lines short.

- diagnostics 1440 light: `docs/distributed/ux-evidence/UXR-040/diagnostics-1440-light.png`
- diagnostics 1440 dark: `docs/distributed/ux-evidence/UXR-040/diagnostics-1440-dark.png`
- diagnostics 360 light: `docs/distributed/ux-evidence/UXR-040/diagnostics-360-light.png`
- diagnostics 360 dark: `docs/distributed/ux-evidence/UXR-040/diagnostics-360-dark.png`

### Account

The first card shows who is signed in, how long the session lasts and the Sign out button; the password form follows with the server's rules listed as they are met. At 360 the cards stack with 12 px gutters. No fault beyond the shared top-bar quota pill.

- account 1440 light: `docs/distributed/ux-evidence/UXR-040/account-1440-light.png`
- account 1440 dark: `docs/distributed/ux-evidence/UXR-040/account-1440-dark.png`
- account 360 light: `docs/distributed/ux-evidence/UXR-040/account-360-light.png`
- account 360 dark: `docs/distributed/ux-evidence/UXR-040/account-360-dark.png`

### Sign in

A single centred card with the focus in the Username field, a visible focus ring, the password reveal button and the recovery path as a disclosure. At 360 the card fills the width with 12 px margins. No fault found.

- sign-in 1440 light: `docs/distributed/ux-evidence/UXR-040/sign-in-1440-light.png`
- sign-in 1440 dark: `docs/distributed/ux-evidence/UXR-040/sign-in-1440-dark.png`
- sign-in 360 light: `docs/distributed/ux-evidence/UXR-040/sign-in-360-light.png`
- sign-in 360 dark: `docs/distributed/ux-evidence/UXR-040/sign-in-360-dark.png`

### Proposed rows (faults not fixed in this unit)

- Phone top bar (360): the quota pill drops the remaining percentage and the second provider; it reads "Claude resets 12:25" where the desktop reads "Claude 5h 4% left, resets 12:25". Show the percentage first on phones.
- Usage and Sessions heading (360): the worker filter in `.fleet-filter` has `flex: 1 1 9.5rem`, which clips "All workers". A basis of 11rem wraps the filter under the heading. This changes the Pixel 4 baselines of Usage and Sessions, which are generated only in the Playwright image, so it was not done here.
- Inbox approval row (1440): the reason line is truncated in the middle of a word and the "Needs approval" pill wraps; shorten the pill label or give the reason line its own row.
- Missions rows (1440): blank provider cell for failed missions.
- Mission terminal (360): no cue that the terminal scrolls horizontally.
