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

## Dispositions of findings-next

The dog-food queue `findings-next.md` held 50 items without statuses. Each item now has a disposition: `fixed` (with the code or test that covers it), `no change` (with the reason), or `open` (with a `DOG` row). Dispositions were checked against the code on `main`; "Evidence" names the code location or test, and commit hashes where one commit introduced the change.

| Item | Summary | Disposition | Evidence |
|---|---|---|---|
| 1 | Disabled provider accepted at create, controller fails with no reason | fixed | `bridge_launcher.py` marks "disabled in config"; failure reason shown by `lifecycle.failure_reason` in `bridge_tui.py`; 02d33bc9 |
| 2 | STOPPED mission status says heartbeat ACTIVE | fixed | `bridge_format.py` prints "not running ... run: llm-bridge start --mission ID"; 1668bcbb |
| 3 | Completed missions keep their station | fixed | `bridge_switcher.py` `station_idle`, `close_station`, `gc_stations` |
| 4 | `mission create` has no --acceptance | fixed | `mission create --help` lists `--acceptance TEXT` |
| 5 | `task list` lacks dependency column | fixed | `bridge_format.py` prints `depends_on_ordinals`; 02d33bc9 |
| 6 | Light theme selected-tab reverse rendering | no change | Verification item; role styles are covered by the golden-screen suite |
| 7 | Cross-station attach lands on Bridge pane | no change | Marked "maybe intended"; the Bridge pane is the intended landing place |
| 8 | Task IDs global across missions | fixed | Per-mission task numbers (`BridgeStore` next per-mission number; `depends_on_ordinals`) |
| 9 | No headline when all providers are out of capacity | fixed | "Waiting for capacity" in `bridge_event_text.py`; strip text in `bridge_tui.py` |
| 10 | "Command rejected" without reason | fixed | `bridge_event_text.py` rejection wording (e.g. "Proposal rejected: ...") |
| 11 | Redundant Overview Controller/Now/Next rows | no change | Overview rows kept; labels now "not started"/"stopped" and slash form used |
| 12 | Persistence path wraps mid-word | fixed | `middle_ellipsis` and `short_home` in `bridge_launcher.py` |
| 13 | Activity shows bookkeeping by default | fixed | `_PLUMBING_EVENTS` hidden unless toggled, `bridge_tui.py` |
| 14 | CLI and home screen default to different modes | fixed | `--mode {native,background,headless}`; help states the default |
| 15 | Cramped 80-col nav labels | no change | Cosmetic suggestion; navigation is covered by the 80x24 golden screens |
| 16 | Home FOLDER column ellipsis | fixed | Folder shown with `short_home`; column widths rebalanced in `bridge_launcher.py` |
| 17 | Stale RUNNING mission sorts as live | fixed | `bridge_missions.order_missions` ranks by `mission_live` (controller or station), not status; launcher marks stale (tests/test_bridge_launcher_routes.py::test_stale_running_mission_is_marked_and_explained) |
| 18 | Overview Recent empty while running | fixed | Snapshot field `operator_events`; 02d33bc9 |
| 19 | Status strip repeats route and model | fixed | `bridge_tui.py` shows the model only when the owner is not contained in it |
| 20 | Ambiguous capacity wording | fixed | "empty in ~X at this pace" in `bridge_tui.py` |
| 21 | Wire failure_reason and waiting into strip | fixed | `bridge_tui.py` lines using `lifecycle.failure_reason`; `waiting_message`; 02d33bc9 |
| 22 | Flaky test_attached_client_input_paste_mouse_resize... | open: FIND-010 | PR #77 |
| 23 | Docs for new console features | fixed | `docs/llm-bridge/operations.md`, `troubleshooting.md` and README cover picker, `/missions`, `LLM_BRIDGE_FINISH_<PROVIDER>` |
| 24 | Native missions start next task without review | fixed | Hold for review by default; `--auto-continue`, `auto_continue_default`; tests/test_bridge_tui_review_hold.py |
| 25 | Approve confirm replaces the body | no change | Confirm dialog kept; evidence context added with review hold (tests/test_bridge_tui_review_hold.py) |
| 26 | Home form shows absolute folder | fixed | `short_home` in `bridge_launcher.py` |
| 27 | Overview uses top third at 110x30 | fixed | Live preview below the Orders list when it fits (`bridge_tui.py`) |
| 28 | Ctrl-] B lands on remembered Actions view | no change | Typing fix made the case harmless; no extra focus rule added |
| 29 | Friendly detach message | fixed | `terminal.py` prints "Detached from <label> · it keeps running · llm-bridge attach to return"; 02d33bc9 |
| 30 | Provider checklist navigation | fixed | `bridge_launcher.py` checklist stops at first and last item (docstring and `_draw_checklist`) |
| 31 | Folder whitespace and error text | fixed | "Folder does not exist" path in `bridge_launcher.py`; 02d33bc9 |
| 32 | Rare prefix_key handshake flake | open: FIND-010 | PR #77 |
| 33 | Picker counts finished missions as live | fixed | `station_idle`, `close_station`, `gc_stations` in `bridge_switcher.py` |
| 34 | Picker header misaligned | fixed | `_header` pads each column with `pad(..., widths)`, same widths as rows |
| 35 | Picker shows stale RUNNING as green | fixed | `bridge_switcher.py` uses `warn` role for `entry.stale` |
| 36 | Bridge pane stuck in copy-mode | open: FIND-011 | PR #77 |
| 37 | Idle redraw/CPU performance | fixed | `bridge_redraw.py` `PRAGMA data_version` probe, `BackgroundReload`, cached glyph tables |
| 38 | `--native --headless` accepted at create | fixed | `--mode` selects one of native/background/headless; old flags folded in `bridge.py` |
| 39 | "Autonomous" names two things | fixed | Launcher Mode field uses Native / Background / Headless |
| 40 | README images and architecture view names | no change | Images are regenerated by the demo recorder at release; names are checked at release time |
| 41 | `minimax_command_argv` stale | fixed | Function no longer exists in `providers/minimax.py` or tests (added in 124fb072, removed in later refactor) |
| 42 | No awaiting-review mission state | fixed | `state_review` for `in_review` in launcher and picker; "task N awaits your review" |
| 43 | Picker switch stacks attach sessions | fixed | `TmuxTerminalBackend.switch_client_to` used by `bridge_switcher.py` and `terminal.py`; 41cff550 |
| 44 | tmux paints transient screens on attach | open: DOG-029 | |
| 45 | Site video aria-label describes old story | fixed | aria-label now describes the 5-hour limit hand-off recording |
| 46 | Flaky stop --force identity mismatch | open: FIND-012 | PR #77 |
| 47 | "(last selected)" for never-run provider | fixed | `bridge_tui.py` labels "last selected" only with a recorded operation for that owner |
| 48 | Internal "current-usable" wording | fixed | `bridge_format.py` maps `current-usable` to "has capacity" |
| 49 | Station status clips at 60 columns | fixed | `terminal.py` compact context "T1 ●" for narrow clients |
| 50 | Usage view unavailable in demo | no change | Item states no action; demo docs mention real providers |
