# Web console and Pages site: expert UX review (PRD-032)

Reviewer: principal-designer pass on the distributed web console and the project Pages site.
Date: 2026-11-07. Product reviewed: PR #31 head (`origin/feat/distributed-followup`, `5a4d018`), coordinator `0.1.0`, workers `llm-bridge 1.4.0`.

## Summary

33 findings: 5 high, 14 medium, 14 low. No critical findings. The console is accessible by automated measures (axe-core found no WCAG A/AA violation on any console page in either theme, apart from one best-practice item, WUX-025). The problems are in task flow, state honesty, permissions and a few layout and focus cases that automated checks do not see.

Top five issues:

1. **WUX-001 (high).** On a running mission the live terminal (a 36 rem black box) sits above the mission actions and the task table. At 1440x900 the Pause/Cancel buttons start at y=1093 and the first task Approve button at y=1883; the page is 3,498 px high. The attention card says "run `llm-bridge task verify --task 2 --approve`" instead of offering the button.
2. **WUX-003 (high).** Permissions are not shown before the admin acts. On a view-only worker the Approve, Reject, Pause, Cancel and "Take control" buttons are all enabled. The refusal arrives after the confirmation dialog as `worker "beta" did not advertise operation "task.verify"`. The Workers page does not show which access each worker has.
3. **WUX-002 (high).** The worker enrolment token is shown once in a dialog that closes on Escape or a click on the backdrop, with no warning. Closing it destroys the only copy.
4. **WUX-004 (high).** The sign-in lockout message is a `role="alert"` element whose text is rewritten every second. Screen readers announce it every second for the whole lockout (5 changes in 5.2 s measured).
5. **WUX-005 (high).** A long unbroken mission name (for example a slug such as `payment-reconciliation-service-to-the-new-ledger-api-...`) overlaps the "Recent activity" column on the Overview at 1440 px and makes the Overview scroll horizontally by 690 px at 320 px width (WCAG 1.4.10 Reflow).

Method:

- Built `bridge-coordinator` (release) and the web bundle from the PR head. Started two coordinators on 127.0.0.1 with throwaway data directories: one fresh (first-run, empty states, password change, lockout, coordinator-down) and one seeded.
- The seeded coordinator had three real Python workers (`alpha` with `--allow-control`, `beta` with `--allow-terminal-view` only, `gamma` with no remote access, later killed to be offline), each with its own throwaway Bridge state, `HOME`, and `XDG_*` directories, run through `/home/chris/llm-tools-infra/prd-py.sh`. Missions: 42, in the states running, failed, completed, paused, stopped, created. One mission has 40 tasks (completed, verifying, failed, queued) and a 130-character name with a 100-character folder name. Real tmux stations with an echo-loop provider backed the terminal viewer. The usage reader was fed a fixed Codex payload with `HOME` pointing at an empty directory.
- Playwright (Chromium, from `web/node_modules`) at 1440x900 and the Pixel 4 profile (353x745 CSS px), light and dark, for every page. For each page and state: screenshot, horizontal-overflow measurement, axe-core (WCAG 2 A/AA, 2.1, 2.2 AA, best-practice tags), and in the desktop-light run a full ARIA snapshot and a Tab-key walk recording the focus ring. Additional runs: 320 px width, 200% text size with WCAG text-spacing overrides, `forced-colors: active`, `prefers-reduced-motion: reduce`, delayed and failing API responses, cookie removal (session expiry), killing the coordinator while a page was open, and killing a worker.
- Keyboard walk: skip link, route-change focus, shortcuts (`g` sequences, `/`, `?`, `t`), account menu, More sheet, dialogs (Escape, focus return), terminal (Tab inside xterm, `F6`), and a check of whether each focused element is covered by the sticky header, the tab bar or a toast.
- Screen-reader semantics were checked from ARIA snapshots and axe, not with a real screen reader.
- The deterministic mock from `web/e2e/mock.ts` supplied states that need a native provider question, a rate-limit response and an empty mission list.
- Pages site: `docs/site/` (single static page plus `404.html`, no build step) served locally; same viewports, themes, axe, tab walk, tap-target measurement and a no-JavaScript load.
- The scripts are in the scratchpad (`.../scratchpad/uxreview/*.mjs`), not in either repository. All processes were stopped and the throwaway directories were not reused.

Not covered:

- A real screen reader (NVDA, VoiceOver, TalkBack) and real mobile hardware. Firefox and WebKit (the e2e suite runs Firefox; this review used Chromium only).
- A task editor, mission creation form and add-task form: **the console has none** (WUX-013). Passkey screens: not present in this build (PK-004 onward are open).
- Read-scope token in the browser: the browser UI only signs in with username and password, so a read-scope token cannot reach it. The API behaviour was checked with curl (see WUX-003).
- A real "provider is asking a question" mission. It was rendered with the mock only. No live provider ran; the seeded RUNNING missions have no provider operation, so the Sessions page was empty and "Provider unknown · Model unknown" appears (WUX-029).
- The existing visual-baseline suite (`web/e2e/screenshots.spec.ts`) was not run; UX-006 is not re-checked.
- Performance (INP, CLS, bundle size) was not re-measured.
- A very large number of missions or workers (beyond 42 missions and 3 workers). Slow real networks: only simulated delay.

Evidence screenshots are in `docs/distributed/ux-review-web/`. File names start with the finding ID. Full-resolution captures were downscaled to at most 1100 px wide.

## Re-check of UX-001 to UX-011

| ID | Claim in `SPEC_STATUS.md` / `ux-audit.md` | Result on this product |
|---|---|---|
| UX-001 | Persona scenarios; live-user sessions still open. | Still open, and the review found gaps in two of the three scenarios. First run on a phone: the setup-token help box fills the first screen and the token field starts below the fold (WUX-026). Five missions and three workers: the Overview works, but acting on a mission fails the task (WUX-001, WUX-003). The notification-link scenario was not exercised. |
| UX-002 | Patterns: attention first, readable event trail, contextual actions with visible permission boundaries, compact running status. | Attention first: holds on the Overview. Event trail: holds (events are phrases, no raw JSON). Compact running status: holds. **Visible permission boundaries: does not hold.** View-only workers show full action sets (WUX-003). |
| UX-003 | Attention/current-work hierarchy, live output, mobile terminal controls. | Partly holds. Live output opens by itself and the mobile key bar works. The hierarchy regressed on the mission page: the terminal pushes tasks and actions out of the first screen (WUX-001), and the attention card shows CLI text. Command palette, prefetch and optimistic updates are still absent. |
| UX-004 | Site hero shows the full demo in the first viewport. | Holds on desktop (hero and demo visible at 1440x900). On the phone the demo table is clipped at the right edge with no keyboard access to scroll it (WUX-018). |
| UX-005 | Lighthouse, axe, bundle, CLS recorded. | Console axe claim holds (clean in both themes, all pages). The site is not covered by axe in the repository, and axe reports a serious violation on the phone layout (WUX-018) and a minor one on desktop (WUX-032). |
| UX-006 | Visual baselines pass. | Not re-checked (suite not run). Note that none of the findings below is detectable by the existing baselines; they need the assertions listed per finding. |
| UX-007 | Mission output streams read-only without a click and sends no input. | Holds. The view-only terminal opened by itself on every running mission with a station; typing `hi` into it produced no output and no input frame. Caveat: the same automatic open produces the layout problem in WUX-001, and unavailable terminals show an empty black box (WUX-014). |
| UX-008 | Running and online indicators; reduced motion stops them. | Holds. With `prefers-reduced-motion: reduce` the animation name is `none` on the worker and mission markers. In forced-colors mode the markers are not needed because every state has text. |
| UX-009 | Attention before counters; next steps for no workers and no missions. | Holds. Attention precedes the tiles on desktop and phone. The empty states exist and have one action. Caveats: while data loads the tiles show `0/0 ... all connected` (WUX-007), and the next step for "no missions" is a CLI command that the page cannot perform (WUX-013). |
| UX-010 | Audit document with findings and measures. | Document exists and its claims about the covered screens hold. It did not find the findings in this document; its "remaining work" list is still accurate. |
| UX-011 | Events humanised, filters, collapsed system events. | Holds. Events read as phrases; the "Show N system events" toggle and both filters work. One related defect: the live-region toast "New event: Command applied" duplicates the "Applied. Approve task 15." toast (WUX-015). |

## Findings

Severity: high = blocks a core admin task, loses data, or fails a WCAG AA criterion for many users; medium = causes avoidable errors, hunting or misleading state; low = polish or edge case. Viewport/theme tags: D = 1440x900 desktop, M = Pixel 4; L = light, K = dark. Input: P = pointer/touch, KB = keyboard, SR = screen reader (by ARIA snapshot).

### High

#### WUX-001 (high) Mission detail: terminal pushes tasks, actions and approval out of the first screen

- Page/state: `/missions/{worker}/{id}`, mission RUNNING with a station and a task awaiting approval.
- Viewport/theme/input: D-L and M-K; P and KB.
- What happens: the page order is breadcrumb, title, current-work card, terminal (about 520 px tall on desktop), mission actions, status table, tasks, activity. Measured on the seeded mission at 1440x900: terminal top y=389, mission action buttons y=1093, status y=1153, tasks y=1731, first task Approve y=1883, page height 3,498 px. On the phone for the long mission: terminal y=770, actions y=1474, tasks y=2604, page height 11,710 px. Reaching the first task control takes 30 Tab stops (22 on the phone). The current-work card tells the admin to run `llm-bridge task verify --task 2 --approve (or reject: llm-bridge task reject --task 2 --reason ...)`; the same text repeats in the Status table as "needs you".
- Why it matters: approving or rejecting finished work is the main reason to open this page. The admin has to scroll past an output pane that, for most missions, is a black rectangle, and is told to use a terminal command although the button exists below.
- Fix: (1) Put a single action bar directly under the title: primary action chosen by state (Approve and Reject when a task is VERIFYING or BLOCKED; Start when CREATED or STOPPED; Continue when PAUSED), then Pause and Cancel mission in an overflow menu. (2) Replace the CLI sentence in the attention card with the same Approve and Reject buttons, using the existing confirmation dialog; keep the CLI text only as a "Copy command" secondary link. (3) Give the terminal a collapsed default: a 6-row preview (about 9 rem) with "Expand" and "Take control"; remember the choice per browser. (4) Move Tasks above Status; show Status as a one-line summary with a "Details" disclosure. (5) Remove the duplicate "needs you" row from the status table.
- Test: Playwright on the seeded fixture: `expect(approveButton).toBeInViewport()` at 1440x900 and at Pixel 4 without scrolling; assert `getBoundingClientRect().top < innerHeight` for the primary action; assert the attention card contains no text starting with `llm-bridge`. Keep a baseline screenshot of the first screen for a mission with a pending approval.
- Evidence: `ux-review-web/wux-001-mission-detail-desktop-light.png`, `wux-001-mission-detail-mobile-dark.png`.

#### WUX-002 (high) One-time worker token is lost when the dialog is dismissed

- Page/state: Workers, "Worker added" dialog after `Add worker`.
- Viewport/theme/input: D-L; KB and P.
- What happens: the dialog shows the worker token ("shown only once") and the enrol commands. Escape closes it (tested), and so does a click or tap on the backdrop (from `openPanel` in `ui.ts`, not exercised); no confirmation appears and the token is not recoverable. Focus lands on the Close button, not on the token or the copy button. The token wraps over two lines and is not pre-selected. Measured: after Escape `#enrol-result` no longer exists. Recovery means removing the worker and adding it again (the revoke dialog says its missions disappear).
- Why it matters: a stray Escape or tap discards the only copy of a credential, and the cost is a second round trip including re-enrolment on the worker machine.
- Fix: make the enrolment dialog non-dismissable by Escape and backdrop (`cancel` event `preventDefault`, no backdrop handler) until the admin has pressed "Copy token" or ticked "I have saved the token"; show "Done" disabled until then. Put initial focus on the "Copy token" button. Select the token text on focus. Add an `aria-describedby` that says the token cannot be shown again. Offer a recovery path: `Regenerate token` on the worker row (revokes the old token, shows a new one).
- Test: Playwright: add a worker, press Escape, assert the dialog is still open and the token element is still in the DOM; click Copy, press Escape, assert the dialog closes.
- Evidence: `ux-review-web/wux-002-enrol-token-dialog-desktop-light.png`.

#### WUX-003 (high) Permissions are not visible before an action; refusal uses internal wording

- Page/state: mission detail and Workers for a worker enrolled with `--allow-terminal-view` only (`beta`) and for one enrolled with no remote access (`gamma`).
- Viewport/theme/input: D-L; P.
- What happens: on `beta` every task button (Approve, Reject, Cancel, Pause) and every mission button is enabled. Pressing Approve opens the confirmation dialog; confirming gives a toast: `Rejected. Approve task 1. worker "beta" did not advertise operation "task.verify"`. The terminal offers "Take control"; pressing it gives a toast "Terminal: view only (this worker accepts read-only remote control)" and the button stays enabled. On `gamma` the terminal buttons "Open terminal" and "Take control" are enabled and the terminal pane says "Session ended (this worker does not share terminals; enrol it with --allow-terminal-view or --allow-control)". The Workers page lists host, address, state and version, but not whether a worker allows control, terminal view, or neither. The API check: a read-scope token gets `403 insufficient-scope` ("token requires scope admin") for `GET /api/v1/workers`, `/tokens` and `/diagnostics`, but can read `/missions` and `/events`; this is not reflected anywhere in the browser, which only has administrator sessions.
- Why it matters: the console's job is to separate "I may" from "I may not". Discovering the boundary after a confirmation dialog, in wording taken from the worker protocol, makes the admin retry and doubt whether the click did something.
- Fix: (1) Add an "Access" column to Workers ("control", "view only", "none") from the capabilities the worker advertises; add a row-level explanation of how to change it (`--allow-control`). (2) On mission and task pages disable action buttons the worker does not advertise, with `aria-disabled` and a visible reason in a single line under the action bar ("beta is view-only. Re-enrol it with --allow-control to approve from here."). (3) Hide "Take control" when the worker is view-only; show "View only" as a badge on the terminal. (4) Map `did not advertise operation` to a sentence: "This worker does not allow remote changes."
- Test: Playwright with a view-only worker fixture: assert `Approve` buttons are `disabled` or `aria-disabled=true` and a visible element with the reason text exists; assert no dialog opens on click; assert the Workers table has an "Access" column with the value "view only".
- Evidence: `ux-review-web/wux-003-view-only-denied-desktop-light.png`, `wux-003-view-only-take-control-desktop-light.png`.

#### WUX-004 (high) Sign-in lockout message is announced again every second

- Page/state: `/login` after too many failed attempts (lockout of 5 to 60 s in the real coordinator; 5 min in the mock).
- Viewport/theme/input: D-L; SR.
- What happens: the message "Too many failed sign-in attempts from this network. You can try again in 53 s (at 03:17:45)." is in `<p role="alert" id="form-error">`. The script rewrites its text every second and also rewrites the button label ("Try again in 53 s"). A MutationObserver counted 5 changes in 5.2 s. An assertive live region announces each change, so a screen reader reads the message again each second until the lockout ends, and interrupts whatever the user is doing.
- Why it matters: the user cannot use the page with a screen reader during the lockout, and the error cannot be reread at their own pace.
- Fix: write the lockout message once into the `role="alert"` element with a static sentence ("Too many failed sign-in attempts. You can try again at 03:17:45.") and update only a visual countdown that is `aria-hidden="true"` (the button label), plus one polite announcement when the lockout ends ("You can sign in again."). Re-enable the button and move focus to the password field at that moment.
- Test: Playwright: trigger the lockout (mock `login: "limited"`), attach a MutationObserver to `[role=alert]`, wait 3 s, assert at most 1 mutation; assert `aria-live` text changes only at expiry.
- Evidence: `ux-review-web/wux-004-login-lockout-desktop-light.png`.

#### WUX-005 (high) Long unbroken mission names break the Overview layout and fail Reflow

- Page/state: Overview, "Running now" list; also the activity list and mission pages.
- Viewport/theme/input: D-K at 1440x900; at 320x568 and at 200% text size with text-spacing overrides.
- What happens: the mission name link in "Running now" is not allowed to wrap. At 1440 px it runs over the "Recent activity" column (the underlined link crosses the activity text). At 320 px the Overview scrolls horizontally by 690 px (page width 1,010 px); at 200% text size by 1,341 px. WCAG 1.4.10 requires no two-dimensional scrolling at 320 px. The only clipping is by `.running-list li > .muted` (ellipsis), which is applied to the description, not to the link. Names such as `payment-reconciliation-service-to-the-new-ledger-api-...` (slugs) are normal in practice. The other pages passed the same check (overflow 0), including the mission page with the same name.
- Why it matters: layout break on real data, and the overflow hides the attention and activity columns at a narrow width.
- Fix: `overflow-wrap: anywhere; min-width: 0` on `.running-list li`, `.running-list a`, `.attention-details a` and the activity rows; make `.running-list li` a grid with `grid-template-columns: auto minmax(0,1fr)`; keep one line with `text-overflow: ellipsis` and a `title` carrying the full name where a single line is preferred.
- Test: Playwright: seed a mission named with a 130-character unbroken slug, load `/` at 320x568, assert `document.documentElement.scrollWidth <= innerWidth`; at 1440 assert the bounding boxes of the running list and the activity list do not intersect.
- Evidence: `ux-review-web/wux-005-overview-long-name-overlap-desktop-dark.png`, `wux-005-overview-320px-overflow-light.png`.

### Medium

#### WUX-006 (medium) Offline mission shows contradictory "worker offline" and "No tasks match"

- Page/state: mission detail of a mission on an offline worker (`gamma`).
- Viewport/theme/input: D-K, M-L; P, SR.
- What happens: under "Tasks" the page shows the notice "The worker is offline, so tasks cannot be loaded." and directly below it an empty state "No tasks match / Nothing matches the current search and filters." with a "Clear search and filters" button. No filter is active. The terminal below still offers "Open terminal" and "Take control", and the sentence "Press F6 inside the terminal to move focus back to the buttons" appears under a terminal that is not connected.
- Why it matters: the second message is false and offers an action that does nothing; the admin may think tasks were filtered away.
- Fix: in `renderTasks`, show the empty state only when `taskLoaded` is true; when tasks never loaded and the worker is offline show only the offline notice, with "Last known" data if the coordinator has it. Hide terminal buttons and the F6 hint while the worker is offline (render the terminal slot as a one-line "Terminal unavailable: worker offline").
- Test: Playwright: mission on an offline worker; assert `getByText("No tasks match")` has count 0 and the offline notice is visible.
- Evidence: `ux-review-web/wux-006-offline-mission-contradiction-desktop-dark.png`.

#### WUX-007 (medium) Overview shows zeros as facts while data loads

- Page/state: Overview with the worker and mission requests still pending (4 s delay injected).
- Viewport/theme/input: D-L, M-K; P.
- What happens: the tiles read `0/0 Workers online - all connected`, `0 Missions running - 0 missions in total`, `0 Awaiting approval`, `0 Failed tasks`. "all connected" is shown for zero workers. Only later do real values replace them.
- Why it matters: an admin returning to the page sees "everything is fine, nothing is running" for the time of a slow request. This is the page's state summary.
- Fix: while `store.loaded` is false render skeleton tiles with `aria-busy="true"` and the label "Loading"; never derive a hint such as "all connected" from an empty list; show "No workers" when loaded and zero.
- Test: Playwright: delay `/api/v1/workers` by 3 s; assert no tile contains `0/0` or "all connected" before the response; assert the tiles carry `aria-busy`.
- Evidence: `ux-review-web/wux-007-overview-loading-zeros-desktop-light.png`.

#### WUX-008 (medium) Overview tile links and counts do not match their labels

- Page/state: Overview tiles.
- Viewport/theme/input: D-L; P.
- What happens: "Awaiting approval" (a count of tasks) links to `/missions` with no filter. "Failed tasks" (a count of tasks) links to `/missions?status=FAILED,RUNNING`, which lists 6 missions in this data, not 2 failed tasks. "Missions running" counts 4 including a mission on an offline worker (shown stale elsewhere). The hint under the approval and failed tiles is "7 active missions", which is not what the number counts; when more than 12 active missions exist the hint changes to "checked 12 of N" without saying that the tile may under-count.
- Why it matters: the tile is the entry to the correct list. A click that lands on a different set than the number erodes trust in the numbers.
- Fix: link "Awaiting approval" to a mission list filtered by "has task awaiting approval" (add a `needs=approval` query to `/missions`, backed by the existing `needs_you`/task counts), and "Failed tasks" to `needs=failed`. Count only missions on online workers in "Missions running", and show stale missions separately ("1 stale"). When the snapshot limit applies, label the tile "at least N" with a tooltip.
- Test: Playwright: click each tile; assert the destination URL carries a filter that matches the tile and that the row count equals the tile number for the seeded data.
- Evidence: `ux-review-web/wux-008-overview-tiles-desktop-light.png`.

#### WUX-009 (medium) Browser tab title on a mission page is the mission ID

- Page/state: `/missions/{worker}/{id}`.
- Viewport/theme/input: D and M; P, SR.
- What happens: `document.title` is set once from the first `<h1>` at route time. At that moment the heading holds the 32-character hex mission ID, so the tab and history entry read `a7896e2029404936994a22d6a33d8c20 - LLM Bridge`, and the title is not updated when the name arrives. Measured for all four seeded missions.
- Why it matters: the tab title is the first thing a screen reader announces on navigation, and history, bookmarks and the tab strip show an unreadable ID.
- Fix: update `document.title` whenever the mission header re-renders (`renderHeader`) and use `Mission name - Worker - LLM Bridge`.
- Test: Playwright: open a mission, `await expect(page).toHaveTitle(/Refactor the parser/)`.
- Evidence: none (DOM measurement: `document.title` read after load on all four seeded missions).

#### WUX-010 (medium) First Tab after load skips the skip link and the navigation

- Page/state: every page after a full load.
- Viewport/theme/input: D and M; KB, SR.
- What happens: after load, `main` receives focus (route code calls `main.focus()`). The first Tab then goes to the first control inside the page (`Add worker` on `/workers`). The skip link and navigation precede `main` in the DOM, so they are reached only with Shift+Tab (two presses reach the last nav link). The skip link therefore has no function for a keyboard user at load, and the user starts in the middle of the page.
- Why it matters: focusing `main` is right after an in-app route change but not on first load, where the browser's default start (top of the document) is expected.
- Fix: call `main.focus()` only when the route was triggered by in-app navigation (`go()`), not on the initial `route()`. On first load leave focus on the body.
- Test: Playwright: `page.goto('/workers')`, press Tab once, assert the active element is the "Skip to content" link.
- Evidence: none (focus log: first Tab lands on `#add-worker-button`).

#### WUX-011 (medium) Focused elements are covered by the sticky header and by toasts

- Page/state: mission detail, missions list (any long page).
- Viewport/theme/input: D-L, M-L; KB.
- What happens: `scroll-padding-top` is `auto` while the header is sticky (`position: sticky; top: 0`), so Chrome can scroll a focused element under it. On the long mission page 12 of 90 Tab stops (desktop) and 15 (phone) were covered at the time of focus; the covered items include the Tasks table region and its column-header sort buttons (under the header) and task buttons (under a toast). On the phone the fixed tab bar and the toast stack (up to 4 toasts, 4.5 s for info and 10 s for failures) overlay content. WCAG 2.4.11 (Focus Not Obscured, AA, 2.2).
- Why it matters: a keyboard user cannot see which control has focus.
- Fix: `html { scroll-padding-top: calc(var(--head-h) + 8px); scroll-padding-bottom: calc(var(--tab-h) + 16px) }` and `scroll-margin` on `.table-wrap`; pause toast timers while a toast or the page has keyboard focus, and place toasts in a fixed region that does not overlap the main content on phones (see WUX-015).
- Test: Playwright: tab through the mission page; for each focused element assert `elementFromPoint` at its centre resolves to the element or its child.
- Evidence: none (measurements: desktop 12 and phone 15 covered focus positions out of 90).

#### WUX-012 (medium) API token secret appears below the fold; focus is lost

- Page/state: Account, "Create an API token".
- Viewport/theme/input: D-L; P, KB, SR.
- What happens: the secret panel is added after the form, under the "Create token" button. At 1440x900 the page already needs scrolling (button top y=905); after creating a token the secret panel's top is at y=965 in a 900 px viewport, so it is not visible, and the page does not scroll to it. The create button is disabled during the request, so focus falls to `body`. The secret is announced through a polite live region whose content is a `<pre>`. Validation text "Enter a name and select at least one permission." is a plain status line above the button, with no `aria-invalid` and no link to the empty field. The token list entry for an empty list is a box that looks like an input ("No API tokens yet.").
- Why it matters: this is another one-time secret, and the admin may not notice it.
- Fix: render the secret panel above the form (or in a dialog like the worker token), move focus to it, scroll it into view, keep the button enabled while busy (`aria-busy`), mark the invalid field with `aria-invalid` and inline message. Same "do not dismiss before copy" rule as WUX-002.
- Test: Playwright: create a token at 1440x900; assert the secret element is in the viewport and has focus; submit with no name and assert the name field has `aria-invalid="true"` and focus.
- Evidence: `ux-review-web/wux-012-token-secret-position-desktop-light.png`.

#### WUX-013 (medium) The console cannot create missions or edit tasks

- Page/state: Missions, mission detail, Overview empty states.
- Viewport/theme/input: all.
- What happens: the only mutations are start/pause/continue/cancel for missions, approve/reject/pause/resume/retry/cancel for tasks, workers, tokens and password. There is no mission creation, no add-task, amend, reorder, priority or dependency editing, although the API has `task.add`, `task.amend`, `task.reorder`, `task.priority` and `task.dependency` (the e2e mock handles them). Empty states send the admin to the CLI ("Run `llm-bridge mission create` on an enrolled worker", "Add tasks on the worker with `llm-bridge task add`"). The Approve action sends the fixed evidence "approved in web UI"; the admin cannot add a comment.
- Why it matters: an admin on a phone can monitor and approve but cannot correct a plan, which is the likeliest reason to open the console when a task fails.
- Fix: (1) Task editor in the mission page: inline edit of title and prompt with a revision check (`expected_revision` is already in the API), drag or move up/down buttons for order, a priority select, and "Add task". (2) Mission creation: a dialog with name, folder (from the worker's allowed roots), prompt, provider list. (3) A "Note for the record" field in the Approve dialog. If these are out of scope for the release, state that in the first-run text and on the site ("review and approve; plan on the worker") so the page does not look unfinished.
- Test: Playwright on the e2e fixture: edit a task title, assert the new title and a `task-amended` event; assert 409 revision conflict is shown with the current value.
- Evidence: none (absence).

#### WUX-014 (medium) Terminal pane when no terminal is available

- Page/state: mission detail on a worker with no remote terminal (`gamma`) or with no provider station (`alpha`, long mission).
- Viewport/theme/input: D-L; P, SR.
- What happens: the pane is an empty black rectangle of the full height (min 12 rem, up to 36 rem) with the text "Session ended (this worker does not share terminals; enrol it with --allow-terminal-view or --allow-control)" or "Session ended (No provider station is running for mission e5210976 - start it with: llm-bridge start --mission e5210976)". No session started, so "ended" is wrong. The same message also appears as a toast, so it is shown and announced twice. The key bar (Esc, Tab, Ctrl, arrows) is visible in view-only mode where it can do nothing. The header says "Working now" for a mission with no running station.
- Why it matters: a large empty box with an alarming wording hides the real state ("no terminal for this mission").
- Fix: when no session exists, replace the pane with a compact state card: "No live terminal. Reason." plus one action. Use "Not connected" instead of "Session ended" unless a session ended. Show the key bar only in control mode. Do not toast a message that is already visible.
- Test: Playwright on a mission with no station: assert the terminal host is hidden or at most 80 px tall, assert no element contains the text "Session ended", and `#toasts` has no child.
- Evidence: `ux-review-web/wux-014-terminal-unavailable-desktop-light.png`.

#### WUX-015 (medium) Mobile toasts cover content; duplicate toasts

- Page/state: any page on the phone after an action or a terminal message.
- Viewport/theme/input: M-K; P.
- What happens: toasts are fixed above the tab bar with up to four stacked, each 90 to 130 px tall at phone width. On the long mission page the "Terminal ended" toast covers half of the attention card in the first screen, and later toasts cover task buttons. An action produces two toasts: "New event: Command applied" (from the event stream) and "Applied. Approve task 15.". Info toasts disappear after 4.5 s, failures after 10 s, with no pause on hover or focus.
- Why it matters: toasts obscure the page they refer to and repeat each other.
- Fix: one toast per user action (suppress the stream-event toast for events caused by the same request ID); cap to two on phones; render as a single-line bar of 56 px; pause the timer on focus and pointer; do not toast messages that are already shown on the page (terminal state).
- Test: Playwright: approve a task; assert `#toasts` has exactly one child; on Pixel 4 assert the toast box height is at most 25% of the viewport.
- Evidence: `ux-review-web/wux-015-mobile-toast-over-dialog-mobile-dark.png`.

#### WUX-016 (medium) Phone tables become very long cards

- Page/state: Missions, mission tasks, Workers on the phone.
- Viewport/theme/input: M-K, M-L; P.
- What happens: each table row becomes a card with one labelled row per column. A completed task card is about 200 px (label rows "Priority 0" and an "Actions" label with nothing next to it). 25 tasks per page produce about 5,000 px; the long mission page is 11,710 px high. In the Missions list the card puts the label "Mission" on the left and a right-aligned cell stack (ID chip, current work, needs-you text, link) in a narrow column, so the text is ragged. Each worker card fills about one screen.
- Why it matters: finding the one task that needs attention requires long scrolling; there is no way to jump to "next task needing action".
- Fix: render rows on the phone as compact two-line list items (title and status badge on the first line, one secondary line, actions in an overflow button); omit empty cells such as "Actions"; add a sticky "Needs action (N)" shortcut on the mission page that scrolls to the first VERIFYING, BLOCKED or FAILED task; set task page size to 10 on the phone.
- Test: Playwright on Pixel 4: assert a completed task row height is at most 96 px and no card contains an empty labelled cell.
- Evidence: `ux-review-web/wux-016-missions-cards-mobile-dark.png`, `wux-001-mission-detail-mobile-dark.png`.

#### WUX-017 (medium) Usage and Sessions are single-worker views with raw codes

- Page/state: Usage, Sessions.
- Viewport/theme/input: D-L; P, SR.
- What happens: both pages have a worker selector and show one worker at a time; there is no overview across workers, and the Sessions page cannot show "all running sessions". Usage shows raw reason codes (`no-local-data`), an ISO-like observation time with a source word ("Observed 2026-11-07 03:17 (source: direct)"), a row for each scope where the provider name and "Ready" are blank on the second row, both "Remaining" and "Used" (always summing to 100), no bar, and a guidance such as "↓ conserve" without explanation. In this run the Sessions page said "No provider sessions yet" on the same worker that has a running mission with a live terminal (seeded without a provider operation, so this may be seed-only; the page does not say what a session is).
- Why it matters: capacity is the reason to look at this page. An admin wants "which provider is ready, until when", across workers.
- Fix: Usage: one table across workers with columns Provider, Ready (yes/no/unavailable with a plain reason), Remaining as a bar with the percentage, Resets (relative with absolute in a tooltip), Worker. Map reason codes to sentences ("Not signed in", "No local usage data"). Drop "Used". Explain guidance values with a tooltip. Sessions: an "All workers" default and a mission link per row.
- Test: Playwright with two workers: assert one row per (provider, worker), no cell text matching `/^[a-z]+(-[a-z]+)+$/` (raw codes), and a `role=progressbar` or `meter` per measured scope.
- Evidence: `ux-review-web/wux-017-usage-desktop-light.png`.

#### WUX-018 (medium) Site: horizontally scrolling code and terminal blocks cannot be reached by keyboard on phones

- Page/state: Pages site, phone layout.
- Viewport/theme/input: M-L and M-K; KB, P.
- What happens: axe reports a serious `scrollable-region-focusable` violation for four elements (`.only-narrow` terminal blocks and `#cmd-ralph`) on the phone layout in both themes. The hero terminal on a 353 px screen is clipped at the right edge ("Reset", "4h 30" cut off) and there is no scroll hint. The installer command block scrolls the same way.
- Why it matters: clipped data without a keyboard route fails WCAG 2.1.1, and the clipped column ("Resets in") is the useful one.
- Fix: add `tabindex="0"` and `role="region"` with `aria-label` to every `pre` or `.cmd` that can overflow (the narrow terminals already have `role="img"`: use `role="group"` with a label or `tabindex="0"` on the wrapper), or shorten the narrow variants to fit 320 px (drop a column) as the page already does for the table in `#limits`.
- Test: axe in CI for `docs/site/` at 353x745 (rule `scrollable-region-focusable`); fail on any violation.
- Evidence: `ux-review-web/wux-018-site-mobile-dark.png`.

#### WUX-019 (medium) Site: no section navigation on phones; page is 18,874 px high

- Page/state: Pages site, phone.
- Viewport/theme/input: M-L, M-K; P.
- What happens: below 681 px the header keeps only the theme toggle and the GitHub link; the `Limits / Hand-off / Bridge / Console / Control / Install` links are hidden (0 visible), and no menu replaces them. The page is 18,874 px high on the phone (11,941 px on desktop). The web console section starts at 7,177 px on desktop; the install steps start at 11,026 px (desktop). The console, which is the subject of this review, is a section in the middle of a page about llm-usage and ralph-robin.
- Why it matters: a visitor who wants the console or the install steps must scroll for many screens.
- Fix: add a compact menu button on phones (disclosure with the six links, 44 px targets) or a bottom "Contents" link row; add a back-to-top link at section ends; consider a separate `console/` page with its install and screenshots so the landing page stays short.
- Test: Playwright on Pixel 4: assert at least five visible links to in-page sections or a menu button with `aria-expanded`; assert each section link navigates to the matching `id`.
- Evidence: none (see `wux-018` for the header on the phone).

### Low

#### WUX-020 (low) Expired session on in-app navigation shows no explanation

- Page/state: any page after the session cookie expires, followed by a click on a navigation link.
- Viewport/theme/input: D-L; P.
- What happens: an API call that fails with 401 shows the notice "Your session expired or was ended. Sign in again to continue." Navigating by a nav link after expiry goes through the auth-state check and lands on `/login` with no notice (`#login-notice` count 0).
- Why it matters: the admin sees a sign-in page without knowing why.
- Fix: when `route()` finds `authed` was true before the check, show the same "expired" notice.
- Test: Playwright: clear cookies, click "Missions" in the nav, assert `#login-notice` has the expired text.
- Evidence: none.

#### WUX-021 (low) Reject dialog uses the browser's native validation bubble

- Page/state: Reject task dialog with an empty reason.
- Viewport/theme/input: D-L, M-K; P, KB.
- What happens: the confirm button with an empty required field shows the browser's native "Please fill out this field." bubble, which differs from every other form in the console (inline `aria-invalid` messages). The label "Reason" does not say it is required.
- Fix: use the console's field error component, mark the label "Reason (required)", and set `aria-describedby`.
- Test: Playwright: assert `#dialog-reason` has `aria-invalid="true"` and an inline error after pressing Reject with no text.
- Evidence: none.

#### WUX-022 (low) Failure and disconnection states are thin

- Page/state: coordinator unreachable, API 500, live stream lost.
- Viewport/theme/input: D-L; P, KB, SR.
- What happens: (1) when `/auth/state` fails the page shows "Coordinator unreachable / Cannot reach the coordinator. / Retry" with generic tab title "LLM Bridge", focus on `body`, no automatic retry and no hint (check the address, the coordinator process, the certificate). (2) A failed list request shows only "internal error" with no Retry button and the filter controls remain. (3) While the coordinator is down and the page is open the connection pill reads "Reconnecting" and data stays on screen with no "last updated" time; the live announcement "Connection reconnecting" is made once. Add-worker in this state shows "Cannot reach the coordinator." correctly.
- Fix: unreachable screen: focus the heading, set the title to "Coordinator unreachable", retry automatically with backoff and show the time of the next try; add the three likely causes. Error notices: add a Retry button. When the stream is down for more than 10 s show a page-level banner "Showing data from HH:MM. Reconnecting..." and mark stale values.
- Test: Playwright: abort `/auth/state`; assert focus is on the heading and `document.title` contains "unreachable"; fail an API call with 500 and assert a button named "Retry".
- Evidence: `ux-review-web/wux-022-coordinator-unreachable-desktop-light.png`, `wux-022-missions-500-desktop-light.png`.

#### WUX-023 (low) Diagnostics values are raw

- Page/state: Diagnostics.
- Viewport/theme/input: D-L; SR.
- What happens: "Uptime 197 s" (the Overview says minutes), "Connected workers alpha,beta,gamma" (a list with no spaces under a count-style label), "Version 0.1.0" for the coordinator while workers report 1.4.0 with no note on compatibility, database in KiB. No indication of health thresholds.
- Fix: format uptime as "3 min 17 s", show "3 connected: alpha, beta, gamma", add "Workers on a different version" when versions differ, and add an overall "Healthy / Needs attention" line at the top.
- Test: Playwright: assert the uptime cell matches `/\d+ (min|h|d)/` and the workers cell contains ", ".
- Evidence: none.

#### WUX-024 (low) Small touch targets and phone tab bar at narrow width

- Page/state: Overview "Running now" links, Workers mission-count link, tab bar at 320 px.
- Viewport/theme/input: M-L; P.
- What happens: the mission links in "Running now" are 24 px high; the mission-count link on Workers is 44 px high but 9 to 18 px wide ("2", "38"); at 320 px the tab label "Overview" breaks inside the word ("Overvie / w"). Everything else measured at least 44 px (the account page checkboxes are 22 px but sit in 44 px labels).
- Fix: `min-height: 44px; display: inline-flex; align-items: center` on those links and 44 px minimum width via padding; `font-size: 0.75rem; letter-spacing: 0` for tab labels under 360 px.
- Test: Playwright on Pixel 4: assert every `a, button` outside paragraphs has a bounding box of at least 24x24 CSS px (WCAG 2.5.8) and 44 px high where primary.
- Evidence: none.

#### WUX-025 (low) Toast text sits outside every landmark (axe best-practice `region`)

- Page/state: any page where a toast is showing.
- Viewport/theme/input: D and M; SR.
- What happens: axe reports a moderate `region` violation ("All page content should be contained by landmarks") for `.toast-text` whenever a toast is present (seen on mission pages and the overview after sign-in). The toast container and live region are appended to `#app`, outside the `main`, `banner` and `nav` landmarks.
- Fix: give `#toasts` `role="region"` with `aria-label="Notifications"`.
- Test: the existing axe helper with the `best-practice` tag on a page with a toast; expect no violation.
- Evidence: none (axe log).

#### WUX-026 (low) First-run and sign-in forms are heavy on a phone

- Page/state: first-run setup, sign-in.
- Viewport/theme/input: M-L, M-K; P, KB.
- What happens: on the phone the setup page is 1,167 px high; the "Where to find the setup token" box (three steps and two commands) fills the first screen and the Setup token field starts near its bottom edge; the help title is `<strong>`, not a heading. The setup token field is `type=password`, so a pasted token cannot be checked (the page has a show/hide button only on password fields, not on this one). The sign-in page's "Remember this device" hint is a five-line paragraph. These forms were otherwise good: errors are inline with `role="alert"`, the focus moves to the failing field, labels and rules are announced, and axe is clean.
- Fix: collapse the token help in a `<details>` (open when the field is empty and focus is not in it, closed after the first keystroke), make its title a heading, add a show/hide button to the setup token, and shorten the remember hint to one line with the numbers in a `<details>`.
- Test: Playwright on Pixel 4: assert the Setup token field top is below 400 px; assert the help is a `<details>`.
- Evidence: `ux-review-web/wux-026-setup-mobile-light.png`, `wux-026-login-mobile-light.png`.

#### WUX-027 (low) Account page repeats itself and has an empty gap

- Page/state: Account.
- Viewport/theme/input: D-L; P.
- What happens: "Signs out at the latest: in 11 h" and the line "That is in about 11 h." say the same thing; the two top cards stretch to the same height, which leaves a large empty area under "This session"; the API token section is below the fold, and there is no entry for theme or other preferences (the theme is a bare icon button in the header).
- Fix: drop the duplicate sentence, align cards to the top (`align-items: start`), anchor-link "API tokens" from the account menu, and put theme choice as a labelled radio group on this page.
- Test: Playwright screenshot baseline for the page.
- Evidence: `ux-review-web/wux-027-account-desktop-light.png`.

#### WUX-028 (low) Overview "Needs attention" is unbounded and its sentences are ambiguous

- Page/state: Overview, "Needs attention".
- Viewport/theme/input: D and M.
- What happens: one row per mission with a signal plus one row per failed mission and per offline worker, with no limit and no sorting by urgency; with 30 failing missions the list pushes the tiles off the screen. The sentence "1 task waiting for approval, 1 failed task, 1 blocked task in Refactor the parser" attaches the mission name only to the last item. At most 12 active missions are inspected; the other missions are not examined and the list does not say so.
- Fix: cap at 5 rows with "Show all N", sort by severity (failed, approval, blocked, capacity), write "Refactor the parser: 1 awaiting approval, 1 failed, 1 blocked", and add "Checked 12 of 40 active missions" under the list when the limit applies.
- Test: Playwright with 20 failing missions: assert at most 6 list items and a "Show all" button.
- Evidence: none (see `wux-008` for the list with five rows).

#### WUX-029 (low) "Provider unknown · Model unknown" is shown as content

- Page/state: current-work card and Overview "Running now" for running missions without a provider session.
- Viewport/theme/input: D and M.
- What happens: the text "Task 2 · Implement the new tokenizer · Provider unknown · Model unknown" appears in the card, the Overview list and the missions list. In the seeded data no provider session exists, so this is the normal state for a mission that is starting or waiting.
- Fix: replace with "Waiting for a provider" for starting or waiting missions and omit the fields that are unknown for other states.
- Test: unit test of `currentWorkSummary` with no provider: assert the output does not contain "unknown".
- Evidence: see `wux-001-mission-detail-desktop-light.png`.

#### WUX-030 (low) Mobile terminal is clipped at the right edge

- Page/state: mission detail terminal on the phone.
- Viewport/theme/input: M-L; P.
- What happens: the terminal renders the remote pane at its own column count and the pane is wider than the 330 px host: "Implement the new tok" is cut off with no scroll indicator. The key bar and F6 hint are present.
- Fix: send the viewer's size to the worker for the view-only session where the worker allows it, or fit with `FitAddon` and wrap; otherwise show a visible horizontal scroll hint.
- Test: Playwright on Pixel 4: assert `.xterm-screen` width is at most the host's client width.
- Evidence: none (screenshot `90-terminal-view-mobile-light` in the scratchpad).

#### WUX-031 (low) Site ignores the operating-system colour preference

- Page/state: Pages site.
- Viewport/theme/input: D with OS light.
- What happens: without `?theme=` the site loads dark although the browser reports `prefers-color-scheme: light`; the README states "Dark is the default theme". The toggle label then reads "Switch to light theme". The console follows the OS.
- Fix: default to the OS preference when no stored choice exists; keep the stored choice otherwise.
- Test: Playwright with `colorScheme: "light"` and clean storage: assert the computed page background is light.
- Evidence: `ux-review-web/wux-031-site-light-os-dark-default.png` (browser set to light, no `?theme=`: the site renders dark).

#### WUX-032 (low) Site: link targets of 20 to 24 px and one ARIA role issue

- Page/state: Pages site, list links ("Key reference and operations guide", "Install the coordinator and add a worker", "README", "benchmark" and similar).
- Viewport/theme/input: D and M; P.
- What happens: stand-alone list links are 20 to 24 px high (22 on phones for several); WCAG 2.5.8 asks for 24 px unless inline in a sentence. axe reports a minor `aria-allowed-role` for `#panel-plan` (an `article` with `role="tabpanel"`).
- Fix: `display: inline-block; padding-block: 6px` for link lists; use a `div` for the tab panel.
- Test: axe on the site with the `best-practice` tag; a Playwright size check for list links.
- Evidence: none (axe and size measurements).

#### WUX-033 (low) Active navigation item is weak in forced-colors mode

- Page/state: console shell with `forced-colors: active`.
- Viewport/theme/input: D; P.
- What happens: the active nav item shows as slightly bolder text; the background highlight disappears. `aria-current="page"` is present, so assistive technology is fine. Status badges stay readable because they carry text. There are no `forced-colors` rules in `app.css`.
- Fix: add `@media (forced-colors: active) { .side a[aria-current=page] { border-inline-start: 4px solid Highlight; } }` and a `1px solid CanvasText` border on badges.
- Test: Playwright with `forcedColors: "active"`: screenshot baseline of the shell.
- Evidence: none.

## What was good

Recorded so that fixes do not regress it:

- axe-core is clean on every console page in both themes and both viewports (apart from WUX-025). Contrast passes in light and dark. Focus rings are 3 px solid with 2 px offset on every control and have enough contrast.
- Sign-in, setup and password-change forms: labels, inline errors, `aria-invalid`, focus to the failing field, caps-lock warning, show/hide buttons with state, visible server-side rules, non-enumerating wrong-password copy, explicit session lifetime text.
- Landmarks and headings: one `h1` per page, `banner`, `navigation "Main"`, `main`, named regions, table captions, `aria-sort`, `aria-current`.
- Dialog behaviour: modal `<dialog>`, Escape cancels, focus returns to the invoking button (confirmed for Approve).
- Reduced motion and text size: 200% text size and text-spacing overrides cause no overflow except WUX-005; reduced motion removes the activity animation.
- The terminal: automatic view-only output, `F6` escape from the terminal documented on the page, key bar on phones, input accepted only in control mode.
- Tab order followed the visual order in every walk, and no keyboard trap was found (the terminal captures Tab, and the page documents `F6` as the way out).

## Evidence index

| File | Finding |
|---|---|
| `wux-001-mission-detail-desktop-light.png`, `wux-001-mission-detail-mobile-dark.png` | WUX-001 |
| `wux-002-enrol-token-dialog-desktop-light.png` | WUX-002 |
| `wux-003-view-only-denied-desktop-light.png`, `wux-003-view-only-take-control-desktop-light.png` | WUX-003 |
| `wux-004-login-lockout-desktop-light.png` | WUX-004 |
| `wux-005-overview-long-name-overlap-desktop-dark.png`, `wux-005-overview-320px-overflow-light.png` | WUX-005 |
| `wux-006-offline-mission-contradiction-desktop-dark.png` | WUX-006 |
| `wux-007-overview-loading-zeros-desktop-light.png` | WUX-007 |
| `wux-008-overview-tiles-desktop-light.png` | WUX-008 |
| `wux-012-token-secret-position-desktop-light.png` | WUX-012 |
| `wux-014-terminal-unavailable-desktop-light.png` | WUX-014 |
| `wux-015-mobile-toast-over-dialog-mobile-dark.png` | WUX-015 |
| `wux-016-missions-cards-mobile-dark.png` | WUX-016 |
| `wux-017-usage-desktop-light.png` | WUX-017 |
| `wux-018-site-mobile-dark.png` | WUX-018 |
| `wux-022-coordinator-unreachable-desktop-light.png`, `wux-022-missions-500-desktop-light.png` | WUX-022 |
| `wux-026-setup-mobile-light.png`, `wux-026-login-mobile-light.png` | WUX-026 |
| `wux-027-account-desktop-light.png` | WUX-027 |
| `wux-031-site-light-os-dark-default.png` | WUX-031 |

## Dispositions

| Finding | Disposition |
| --- | --- |
| WUX-001 | FIND-007 |
| WUX-002 | FIND-007 |
| WUX-003 | FIND-007 |
| WUX-004 | FIND-007 |
| WUX-005 | FIND-007 |
| WUX-006 | FIND-007 |
| WUX-007 | FIND-007 |
| WUX-008 | FIND-007 |
| WUX-009 | FIND-007 |
| WUX-010 | FIND-007 |
| WUX-011 | FIND-007 |
| WUX-012 | FIND-007 |
| WUX-013 | FIND-007 |
| WUX-014 | FIND-007 |
| WUX-015 | FIND-007 |
| WUX-016 | FIND-007 |
| WUX-017 | FIND-007 |
| WUX-018 | FIND-007 |
| WUX-019 | FIND-007 |
| WUX-020 | FIND-007 |
| WUX-021 | FIND-007 |
| WUX-022 | FIND-007 |
| WUX-023 | FIND-007 |
| WUX-024 | FIND-007 |
| WUX-025 | FIND-007 |
| WUX-026 | FIND-007 |
| WUX-027 | FIND-007 |
| WUX-028 | FIND-007 |
| WUX-029 | FIND-007 |
| WUX-030 | FIND-007 |
| WUX-031 | FIND-007 |
| WUX-032 | FIND-007 |
| WUX-033 | FIND-007 |
