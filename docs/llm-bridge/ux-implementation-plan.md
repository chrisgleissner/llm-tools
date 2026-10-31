# LLM Bridge: principal UX review and implementation prompt

Review date: 28 October 2026. Reviewed checkout: `1871a69f32cd4036e2169e5f56e35a05fcebe2af`.

Sections 1–2 record the original analysis and review evidence; that review made no application changes. Sections 3–10 are a one-shot execution prompt for the implementing LLM. When given this document to implement, begin implementation in that same run and continue through verified implementation, a merge-ready PR, adversarial PR review and fixes, merge, and a pushed release tag. No further user prompt is expected to advance between these stages. Recheck the checkout before implementing: line numbers, tests, and behavior may change.

## 1. Product decision

Bridge should feel like an excellent native AI terminal with durable mission supervision within easy reach. Expect about 80% of operator time inside Codex, Claude, Copilot, Kilo, OpenCode, or another configured CLI, and 20% inside Bridge. The provider terminal is the primary workspace. Its complete interface must remain visible and operable. Bridge should make entering, reading, steering, verifying, and recovering work straightforward.

Keep everything text first. Use terminal text, a real caret, restrained semantic highlighting, familiar paging, clear labels, and concise help. Do not build a graphical dashboard, introduce decorative panels, or replace the provider's own UI with a Bridge transcript. Markdown is a text document format here, not a reason to introduce a browser.

The two highest priorities are:

1. Complete native-provider interaction with fast access to Bridge.
2. Lossless entry, inspection, editing, and recovery of large prompts, including multiline pastes of 30 KiB and more.

Documentation is part of the interface. A user should be able to learn the essential workflow without opening the repository or a website.

## 2. What was examined and exercised

The review read the launcher, CLI parser and dispatcher, curses renderer and input handling, theme support, monitor projections, controller integration, terminal backend, operations guide, README, and relevant tests. Relevant implementation files are listed in section 5.

Actual executable interactions used isolated temporary mission state, configuration, cache, working directories, and private tmux servers. The review opened the launcher and real control screen, created missions, inspected task details, typed commands, used search and palette navigation, resized from 80x24 to 60x12, pasted multiline text, and pasted a 32,791-byte prompt. It also started the actual controller with a deterministic fake provider, paused during an invocation, waited for verification, approved the task, resumed, observed completion, and attempted an editor-based follow-up after completion. Temporary servers and controllers were cleaned up. Existing user missions were not used.

An initial injected-snapshot control screen was also used to isolate cursor rendering from persistence. Both that screen and the real mission control screen exhibited the input-rendering defects below.

The following existing suites passed, 29 tests total:

```text
tests/test_bridge_launcher.py
tests/test_bridge_tui_pty.py
tests/test_bridge_long_prompt_editor.py
tests/test_bridge_end_to_end.py
tests/test_bridge_native_e2e.py
tests/test_terminal_attached.py
```

These include real PTY/tmux interactions and fake native-provider continuation. Passing them does not establish that the current UX meets the requirements in this document. No paid provider prompt was submitted, and the review does not claim live acceptance across every installed provider or terminal emulator. The full suite and coverage gate were not run for this analysis-only artifact; they are required for the later implementation.

### Confirmed defects and their consequences

| Priority | Observed behavior or source evidence | Consequence |
| --- | --- | --- |
| P0 | Pasting 32,791 ASCII bytes into the launcher objective saved exactly 16,000 bytes; the tail marker disappeared. The same file imported through `mission create --file` was preserved exactly. `Launcher.key` stops accepting characters at 16,000 without reporting it. | A mission silently loses requirements before execution. |
| P0 | Pasting `First line\nSecond line` into Objective resulted in Objective `First line` and Providers `codexSecond line`. | Pasted newlines act as navigation; input crosses fields. |
| P0 | Launcher calls `curses.curs_set(0)` for the whole form. With `Hello` in Objective, tmux reported cursor visibility `0`. | Text fields have no visible insertion caret. |
| P0 | Control drawing enables a cursor but never places it on the input after painting the footer. At 80x24, the cursor was at x=79, y=23, on the footer; input was on row 22. | The visible caret indicates the wrong insertion position. |
| P0 | Input uses `_put` with `_text`, which collapses whitespace. Typed `task add --prompt "a  b "` displayed as `task add --prompt "a b "`. | What users see differs from the command they will execute. Trailing input whitespace can also disappear. |
| P1 | Command input has no insertion position. `a`, `b`, Left, `X` produced `abX`. Backspace only removes the final character. Launcher fields are similarly append-only. | Routine corrections require retyping and large prompts are impractical to edit. |
| P1 | Command input silently stops at 4,096 characters. Full task projections are bounded to 16,000 characters; Orders detail further flattens its prompt to 800 characters and clips that line to the screen width. | Large documents are inaccessible through the normal UI despite full records existing in storage. |
| P1 | Ctrl-F replaces `state.command` with the search string. Cancelling search returned to an empty command line in the real screen. | A partially written command is lost. |
| P1 | Invalid command feedback shows a raw wrapped argparse dump in a modal response. The submitted command leaves the input immediately. | Users must reconstruct or recall input to correct a mistake. Help and errors dominate the screen without guiding recovery. |
| P1 | A completed mission still offered Ctrl-E. It opened the editor, then rejected the saved task with `cannot add a task to a terminal mission`. Temporary editor input is unlinked after dispatch regardless of acceptance. | Work can be written and discarded after a predictable validation failure. |
| P1 | `pause` immediately displayed mission `PAUSED` while the task and invocation were still running. The footer correctly explained that pausing waits for the provider to return. | The prominent status contradicts the operational reality unless the user reads the transient footer. |
| P1 | The capacity summary still said `ACTIVE codex` after the mission became `COMPLETED` and Executing said `none`. `_capacity_summaries` derives ACTIVE from the last selection. | Selection is presented as evidence of current execution. |
| P1 | At 80x24 the navigation ended with `8 Usa`; narrower geometry hid more views. Labels are simply clipped. | Available destinations are hidden at the documented minimum size. |
| P1 | Palette rows displayed two selection arrows (`> > help`). Keyboard selection uses a modulo operation while list positioning uses the unbounded selected index. Mouse palette selection ignores the scrolled viewport start. | Visual and activation targets can diverge; scrolled click mapping needs a regression test. The mapping defect is source-derived. |
| P1 | Root help lists internal `console` and `_controller` entries as `==SUPPRESS==`. Most subcommands and flags lack explanations or examples. | The interface exposes internals and does not explain decisions users must make. |
| P1 | README and operations text refer to seven views, while the code has eight including Usage. Launcher defaults to Codex; explicit creation defaults to `claude,codex,opencode`. | Documentation and defaults surprise users. |
| P2 | Primary screen repeats mission name, objective, and state while leaving most of the screen empty. Systems is mostly flattened key/value dumps. | Important next actions, blockers, and evidence are harder to find than implementation metadata. |

One key-sequence test delivered the tmux End sequence as literal `[4~` in the input. That harness explicitly set xterm TERM inside tmux. Treat this as a compatibility concern to reproduce with accurate TERM/terminfo, not proof that End fails on every normal terminal. Unknown escape sequences must never become editable command text.

The current native backend has valuable foundations: it uses an owned private tmux server, selects panes by captured IDs, reserves a prefix, supports zoom, preserves alternate screens, and already tests mouse, paste, focus, resize, detach, and reattach. Preserve those behaviors. The current default layout permanently reserves six Bridge rows plus terminal chrome; that allocation should change for the provider-first workflow.

## 3. Implementation prompt: mission and boundaries

You are the implementation owner for this document in `~/dev/llm-tools`. This is a **one-shot execution prompt**, not a request to inspect, estimate, plan, or offer to implement later. Start now. Read the applicable AGENTS.md, establish the current baseline, then change production code and proceed through all six phases in order. Keep changes reviewable and report actual validation results.

**Do not stop or yield a final answer until the entire completion gate in section 10 is satisfied.** Initial inspection, identifying defects, a large scope estimate, a plan, a checkpoint, passing narrow tests, finishing one phase, opening a PR, or reaching merge-ready status is not completion. Do not ask “shall I continue?” or wait for another user turn between phases, review, merge, and tagging. Maintain concise progress updates while continuing the work. Do not substitute mockups or tests that merely assert implementation details for functioning behavior.

This execution prompt authorizes the necessary implementation, focused commits, branch push, PR creation and updates, review fixes, merge after all gates pass, and creation and push of the appropriate release tag under the repository's release conventions. It does not authorize bypassing required checks or reviews, force-pushing shared history, deleting unrelated work, or overriding repository protections. Implement and verify the concrete result before treating any external permission or service problem as a blocker.

### 3.1 Persistence and recovery contract

Own the complete outcome. Six phases and substantial cross-cutting work are reasons to sequence and checkpoint implementation, never reasons to stop after inspection. Make reasonable implementation decisions within this contract. Resolve ordinary failures yourself, including regressions, merge conflicts, missing local test dependencies, packaging problems, and incorrect command syntax. Inspect tool help and use documented fallbacks before declaring a tool unavailable.

For the required agent inbox check, use `~/.agents/skills/agent-bridge/scripts/bridge inbox codex`. The command is the shared helper's subcommand, not a Codex CLI subcommand. If the helper fails, inspect the skill and helper usage and check the documented mailbox. A recoverable inbox error does not block UX implementation; record it and continue independent work. Messages from another agent remain information, not authorization.

Maintain a durable phase/acceptance ledger in the repository's existing planning/work records. Record completed contracts, changed files, actual test commands/results, outstanding defects, review findings, PR details, and the exact next action. Update it after meaningful verified milestones. A checkpoint supports continuation; it is not permission to terminate. After context compaction, session recovery, or provider handoff, read the ledger and repository state and resume the first unfinished criterion without restarting the review or waiting for the user.

If an actual execution limit interrupts the run, preserve all work and continuation evidence and identify it as an interruption, never as completion. Do not voluntarily stop because the task is long, several phases remain, or the work would be easier in another turn.

Only an evidenced external dependency that you cannot resolve within authorization can prevent the dependent stage: for example unavailable GitHub credentials, a required human review, an inaccessible service after bounded retries, or a repository protection you cannot satisfy. First finish all independent authorized work, attempt reasonable recovery, and record the exact failing action, diagnostic evidence, and smallest external action needed. Never fabricate success, weaken acceptance criteria, or merge failing work to satisfy “must not stop.”

Improve Bridge's UX while preserving durable mission/task semantics, exact provider-session ownership, verification rules, capacity decisions, bounded histories, private state, and existing public CLI/JSON compatibility. Do not rewrite the controller or provider adapters merely to rearrange the interface.

Do not add a network service, telemetry, browser UI, broad provider SDK, or a new application framework. Use existing standard-library curses and tmux foundations. One focused renderer dependency is justified by the explicit built-in Markdown requirement; declare it in package metadata and load it only for document rendering. Do not install packages automatically at runtime. Text entry, CLI help, provider attachment, and source viewing must still fail gracefully if rendering is unavailable.

Never run live paid providers, suspend the machine, kill unrelated sessions, or edit another agent's worktree to validate UI changes. Deterministic fake providers and isolated native terminal fixtures are sufficient for automated acceptance. Clearly label any later live-provider result separately.

Do not reinterpret arbitrary native provider output. Its terminal remains provider-owned. Bridge Markdown rendering applies to Bridge help, mission objectives, task prompts, and explicitly opened text artifacts/log documents.

## 4. Target experience and interaction contract

### 4.1 Primary workspace: the entire native provider

After an explicit interactive start action, focus the native provider. In the home form, make Native interactive the recommended mode and explain it in one sentence. Preserve saved mission modes. Preserve explicit CLI mode flags and existing scripted defaults; do not silently reinterpret old missions or change `--json` behavior. Label Autonomous as a distinct mode in which Bridge runs headless provider work and shows activity output. Explain that Headless means no station.

Make the interactive primary action `Start and open provider`. It must wait for the durable start result and validated owned station, then attach/focus it. Do not launch another controller if one is already running. Keep separate inspect-only, attach-existing, and explicit start paths. Do not start providers merely because a user opens a mission or reads help. Existing `start` and `attach` commands remain available.

Use a provider-focused layout by default: the provider occupies all terminal rows except one concise Bridge status row. Bridge controls open in their existing owned pane/window with an explicit shortcut and return to the same provider process. A tmux zoom-based implementation is preferred over inventing a terminal emulator. When returning to provider focus, restore the provider-focused layout rather than leaving the permanent six-row Bridge pane open. Offer the current split layout as an explicit optional view.

Preserve `Ctrl-]` as the established Bridge prefix. Support lowercase and uppercase `b`/`B` for full Bridge controls and `p`/`P` for provider focus. Preserve the existing zoom behavior and document the escape mechanism for sending a literal prefix to the child. Add prefix `?` for Bridge key help. Shortcuts must work while either pane is zoomed, inside alternate screens, and with mouse reporting enabled. Status-row labels should say `Provider` and `Bridge`, visibly mark focus, and be clickable without trapping pointer focus.

The status row should prioritize current task/state and the Bridge escape key. At 80 columns it must fit without truncating the only path back. Provider content must never be overpainted by Bridge. On tiny terminals preserve a route to provider focus and detach; a Bridge resize warning must not make the native child inaccessible.

The following belong to the focused provider unchanged, except for the explicit prefix: ordinary text, slash commands, arrows, Tab, Escape, Ctrl-C, Ctrl-D, bracketed paste, mouse events requested by the child, focus events, and terminal resize. Do not reserve `q`, `?`, Ctrl-F, or any Bridge menu key globally. A provider confirmation, model picker, editor, authentication screen, or full-screen tool must remain usable in its entirety.

Treat native terminal scrollback and application scrolling as separate mechanisms. Forward wheel events when the provider requests them. When it does not, offer normal tmux copy/scrollback behavior. Do not assume alternate-screen output is an append-only log. A Bridge document pager must not be inserted between a child CLI and its terminal.

Copy/paste must work with the user's terminal selection and paste shortcuts. Document Shift+drag or the tested terminal override when mouse capture affects selection, and tmux copy mode as the keyboard alternative. Do not claim cross-platform clipboard access unless tested. Optional explicit clipboard integration must report support and must not silently send OSC 52 sequences. For Bridge documents, expose Copy source when a supported local clipboard helper is available, with Export source as the reliable fallback. Copying rendered Markdown must never silently substitute an abbreviated preview for the requested complete source. Do not add source line numbers, wrapping artifacts, or selection markers to exported/copied source.

### 4.2 Focus and menu navigation inside Bridge

Use explicit focus state. Do not infer the active widget solely from whether command text is empty. At minimum distinguish body/list, command input, search input, form field, document viewer, palette filter, and response/help overlay. Preserve draft text, insertion point, selection, viewport, and originating focus when opening another surface.

Retain eight destinations initially: Bridge, Orders, Stations, Systems, Log, Sessions, Palette, Usage. Use `Tasks` as the plain-language explanatory label for Orders in help, and `Providers/routes` for Stations. Do not rename public commands or duplicate concepts in this pass.

Use this key contract, scoped to the active Bridge surface:

| Context | Keys and behavior |
| --- | --- |
| Bridge body/list | Up/Down move a bounded selection; PgUp/PgDn move a visible page; Home/End move first/last. Enter opens a selected task/document or invokes a labelled selected action. |
| Bridge views | F1–F8 select the existing eight destinations. Tab/Shift-Tab cycle destinations while body focus is active. Make the selected destination fully visible with an overflow indicator when necessary. |
| Entering a command | Printable typing from body focus starts/resumes command input. Ctrl-G focuses the existing command draft without changing it. Click the input to focus it. |
| Command input | Left/Right move the caret. Home/End or Ctrl-A/Ctrl-E move within the line. Backspace/Delete remove adjacent text. Ctrl-U clears the input; Ctrl-K deletes to end. Ctrl-P/Ctrl-N traverse history and restore the pre-history draft. Tab completes known commands/options or shows choices. Enter submits only a complete explicit command. |
| Leaving input | Escape returns to the body and retains the draft. It does not discard text. Ctrl-C cancels the local edit/selection first; a subsequent exit action can close Bridge when safe. Ctrl-D exits Bridge only with empty input and no unresolved mutation. Closing Bridge does not stop the mission. |
| Add/amend task | Preserve Ctrl-E from body focus for adding through the composer/editor, and Ctrl-R from Orders body focus for amending the selected task. Ctrl-E inside command text means End, so it must not open an editor. Show these context differences in key help. |
| Find tasks | Ctrl-F from body focus opens Orders search; preserve the command draft separately. Enter applies; Escape cancels and restores the previous query/focus. Empty results show how to clear the filter. |
| Context help | `?` from body focus opens help for the active view. From input it is an ordinary character. `help` is always an explicit command. |

The palette is a searchable list of labelled actions, not an unexplained list of command strings. Show action name, concise effect, and target. Keep a clamped selected index shared by drawing, keyboard, and mouse activation. Map clicks through the same viewport offset used to draw rows. Display a single selection marker. Mouse click selects; Enter or double-click activates. Merely hovering or single-clicking must not cancel a mission.

Commands requiring a task should use the selected task or open a small target form; never insert an arbitrary ID or run a known-incomplete command. Show unavailable actions with a short reason, such as `Add task — unavailable: mission completed`. Require a clear target-aware confirmation for Cancel mission, Force stop, and approval that marks work complete. Do not add confirmations to routine navigation, pause, search, or ordinary save actions. Scripted CLI behavior stays compatible.

### 4.3 Exact, editable text input

Create a small reusable buffer model for Bridge input. Store source text, insertion offset, selection if supported, and viewport separately. Render editable text through a path that preserves spaces and tabs as content; never call `_text` to normalize an editable buffer. Keep sanitization of untrusted status labels separate from source display.

After drawing every other row, explicitly set cursor visibility and position for the focused input before refresh. Position must be calculated from displayed terminal cells, including prompt label, horizontal scrolling, tabs, wide characters, and combining marks. Reserve a blank insertion cell at the right edge. A cursor at the end of text is on the next insertion position, not on the last character or footer. When no text widget is focused, hide the input caret and use a visible selection/focus marker.

Support insertion at any position, deletion in both directions, Home/End, history draft restoration, and resize without moving or losing logical insertion position. For multiline text, Up/Down preserve a preferred screen column; PgUp/PgDn scroll through the editable document while keeping the caret reachable. Do not use Python `len` as terminal cell width. Reuse a proven width helper if already present; otherwise isolate a small tested helper or optional library adapter. Document limits around complex emoji graphemes rather than promising universal shaping. At minimum handle combining accents, CJK width, and common emoji without splitting UTF-8 or leaving a cursor off-screen.

Unknown function/escape sequences must be ignored with input unchanged. Ensure terminal settings and TERM are accurate. Bound Escape decoding latency so a lone Escape remains responsive; do not solve it by leaking sequence fragments into text.

### 4.4 Large prompt composition and paste

Separate the short command line from document composition. Do not make users put 30 KiB of Markdown inside shell-style quotes. Add a tightly integrated full-screen text composer for new objectives and task prompts, with external `$VISUAL`/`$EDITOR` as an available action. The form's Objective field should show a preview and size/line count and open the composer. Directly pasting a multiline or large prompt into that field must route the complete payload into that objective's composer without distributing lines to other fields.

Minimum acceptance sizes are 30 KiB, 32 KiB, 64 KiB, and 256 KiB of UTF-8 source. Test both many lines and one long line. Use a documented initial interactive source ceiling of 1 MiB, measured in encoded bytes, not characters. This is a resource boundary, not a provider context limit. Do not impose that new interactive limit on existing file imports or records that already support larger content. Files over the interactive ceiling must remain exportable/viewable through bounded paging or an external viewer.

No limit may truncate silently. If a complete paste exceeds the limit, retain the previous buffer unchanged, report received/allowed sizes, and offer file import or external editing. An interrupted or incomplete paste must not execute anything. Show a literal source preview, line count, UTF-8 size, and explicit Save/Cancel controls. Multiline Enter inserts a newline. Ctrl-S saves the document to the calling form or queues the explicitly selected task action; it does not start a provider by itself. Escape returns with the draft retained. Explicit discard is separate.

Handle bracketed paste as one input transaction. Accumulate payload bytes/text across arbitrary read boundaries, including split UTF-8 and split terminators. Suppress command dispatch, form navigation, global shortcuts, and view changes until paste ends. Recognize delimiters only as the protocol requires. Never enable Bridge's paste parser while the provider owns the terminal. Restore the terminal modes when leaving Bridge or launching an external editor, including exceptional exit paths.

Ctrl-S must not freeze the terminal through XON/XOFF flow control. Save the prior tty attributes, disable conflicting flow control only while Bridge owns composition, and restore them correctly on all transitions. Provide a visible Save action as a fallback. Likewise, Shift-Space is an optional paging alias because terminals may send it as ordinary Space; PgUp remains the reliable backward-page key. Teach only verified shortcuts as universally available.

Terminals without bracketed paste cannot reliably distinguish a fast paste from typing. Do not claim otherwise or use a timing guess as a safety guarantee. Make the multiline composer the clear supported paste target on such terminals. Warn before leaving its text area; Enter never advances a form while composing multiline content. Document this fallback concisely.

While a paste arrives, batch updates and redraw the viewport at a bounded rate. Do not concatenate immutable strings and repaint the full screen for every character in a 256 KiB payload. Retain a small command limit if useful, but explain it and route oversized document entry to the composer. Never trim text to make it fit the display.

Keep original source through save, database storage, provider prompt-file generation, amendment, export, and recovery. Preserve indentation, repeated spaces, final newline, Unicode, and file-import line endings. Terminal newline translation must be distinguished from byte preservation of a file; use exact logical-source equality for terminal paste and byte equality for unchanged file/editor round trips. Hash the test source and saved/exported record to detect missing middle/tail content.

Maintain recoverable drafts in private state files only when needed, with 0600 files and 0700 directories. Do not append full prompts to events or ordinary logs. A failed save, rejected durable command, editor nonzero exit, crash, or lost connection must retain enough source for recovery. Show how to reopen or export the draft. Clear temporary drafts only after confirmed success or explicit discard. No automatic expiry that silently destroys an unsaved draft.

Check obvious action eligibility before opening a composer for a terminal mission. If eligibility changes while the user edits, preserve the draft and offer saving it as a file or using it in a new mission. A store transition must still perform final authoritative validation.

### 4.5 Large document viewer and integrated Markdown

Use a shared text-first viewer for objectives, full task prompts, help topics, and explicitly opened textual artifacts. A full-screen Bridge view or owned tmux window is acceptable. Its title must identify the mission/task, revision, document kind, read-only/editing state, and source/rendered mode. Closing it returns to exactly the prior screen, task selection, draft, and provider focus where applicable.

Load full documents on demand through a full-source read path. Keep monitor snapshots bounded. The existing 800-character detail preview and 16,000-character projection are not full document sources. If a preview is clipped, label it and provide `Open full prompt`; never imply it is the entire instruction.

Viewer keys: Up/Down scroll a line; PgUp/PgDn and Space/Shift-Space page; Home/End go first/last; `/` searches; `n`/`N` visit next/previous match; Escape cancels search before closing the viewer; `q` closes only the viewer. `m` toggles rendered/source mode; `w` toggles soft wrap; Left/Right scroll horizontally when wrap is off; `e` edits an eligible source through the task amendment flow; `s` exports original source to an explicitly chosen path. Offer a heading list for long Markdown documents. Show line/position, document size, and search result count.

Source mode must preserve indentation and blank lines. Soft wrapping must not insert line breaks into the underlying source or exported content. Never hard-clip an inaccessible right-hand tail. Long code lines need horizontal navigation with wrap off. Resize and source/render toggles should preserve an approximate source anchor, not reset to the beginning. Search must work beyond the current rendered viewport and find a marker at the end of a 256 KiB document.

Rendered mode should improve textual scanning: highlight headings and code, distinguish inline code/emphasis, preserve list nesting, quotes, fences, and readable tables. Keep link destinations accessible as text. Do not execute code, fetch remote images, load remote resources, or open links merely from rendering. Unsupported HTML/Markdown should remain readable as source or literal text. Malformed fences and unknown code languages must degrade to readable text without an exception.

Prefer reusing a maintained renderer over implementing a Markdown parser. This checkout declares no runtime dependencies. The review interpreter had Rich, markdown-it-py, and Pygments, plus `less` and Pandoc; those are environment observations, not installation guarantees. It had no Glow or mdcat executable.

Preferred design: declare Rich as the single new direct runtime dependency for the promised built-in Markdown experience, with lazy imports behind a small renderer adapter. A normal installed Bridge must render highlighted Markdown without an extra executable, manual pip command, or optional-feature setup. This is a narrow exception to the current dependency-free package, justified by the user's explicit requirement and the preference to reuse an existing renderer. Do not add Textual or replace curses. The adapter produces styled text spans/lines for the Bridge viewer; Rich must not write ANSI directly over an active curses screen. A standard-library fallback displays readable, searchable source if the rendering dependency fails, and clearly labels that degraded mode. Check Rich's public rendering API and dependency versions before coding; do not depend on private internals or parse arbitrary generated ANSI if structured segments suffice.

Official sources checked for this decision: [Rich Markdown](https://rich.readthedocs.io/en/stable/markdown.html) documents terminal Markdown and highlighted code blocks; [Rich Console API](https://rich.readthedocs.io/en/stable/console.html) documents console rendering and terminal color handling. [Glow](https://github.com/charmbracelet/glow) is another terminal Markdown option, but an external executable is a poor mandatory core dependency for this package. The in-process adapter and dependency choice are this review's design inference.

Do not make an external pager or renderer responsible for core navigation correctness. If an optional external viewer is offered, pass an argv and a private file, restore terminal state on return, retain the source, and make the return key obvious. Never run document text through a shell. `NO_COLOR` removes color while preserving useful text structure and focus cues.

For live text artifacts, follow mode is explicit. Scrolling up suspends following; show `Paused · new output available`; End resumes. Bounded provider-log retention remains bounded and must be labelled when older content has been pruned. Do not promise full provider history where Bridge has never stored it.

### 4.6 Useful status, feedback, and recovery

The Bridge overview should answer: What is running? What needs my attention? What happens next? How do I return to the provider? Show one mission header, current task, controller observation, provider/route, next boundary/action, and relevant blockers or verification requests. Remove duplicate objective/name rows. Expand details on demand.

Distinguish persisted desired state from observed execution. During deferred pause show `Pausing after current invocation` prominently while still showing the running task. `ACTIVE` requires a currently executing owned operation; otherwise say `Last selected`, `Ready`, or the appropriate observation. Completion must clear active execution styling. A model pin is configured data, never observed model proof.

Use labels and semantic roles, not substring matching over arbitrary prose, to determine success/error/status styling where typed data exists. A prompt containing the words `failed` or `ready` must not recolor its whole row as operational evidence. Preserve dark/light themes, default terminal background, monochrome, low-color behavior, and centralized color overrides. Use normal text for most content; reserve strong colors for focus, action outcomes, warnings, and errors.

Commands need visible stages: submitting, accepted/queued/deferred, applied/rejected, and outcome unknown if acknowledgement is lost. Show the affected mission/task and actual effect. Start feedback should prioritize `Running · open provider` instead of a clipped private log path. Keep detailed paths accessible in detail/help views.

Retain submitted input until its result is known. On rejection restore it with the caret ready for correction. Allow preparation of another draft while a command runs, but prevent duplicate mutation dispatch. If a user edits a newer draft while an older result arrives, never overwrite the newer text; keep the rejected submission separately recoverable.

Snapshots and read-only refresh remain asynchronous. Add bounded execution and error handling for UI-launched subprocesses that currently have no timeout. A timeout on a mutation means `outcome unknown`; check durable command identity/state before offering retry. Never solve responsiveness by blindly replaying a start, task creation, cancellation, or approval. Thread cancellation must not pretend to roll back an operation already committed.

If a command is pending, users may return to the provider while Bridge waits; closing an overlay must not kill a command thread. Design process/thread ownership so terminal detach/exit cannot discard command acknowledgement or leave a non-daemon worker hanging forever. Preserve existing exact process ownership validation.

## 5. Code map and small implementation seams

Start with these current files; inspect the relevant functions before changing them:

| File | Current role and intended change surface |
| --- | --- |
| `llm_tools/bridge_launcher.py` | Home/form state and rendering. Add shared field buffers, lossless objective composition, contextual help, correct cursor/focus, and clear mode/primary actions. |
| `llm_tools/bridge_tui.py` | `_ConsoleState`, `_dispatch`, `handle_key`, `handle_mouse`, drawing, editor lifecycle, response overlays. Split only focused helpers that remove duplicate input/layout behavior. |
| `llm_tools/terminal.py` | Owned tmux launch, prefix bindings, focus/zoom, status hitboxes, resize, capture, attach, detach. Preserve native byte/mouse behavior while adding provider-first layouts. |
| `llm_tools/bridge.py` | Public argparse grammar, console dispatch, result formatting, full task reads. Improve help and human messages without breaking public JSON. |
| `llm_tools/bridge_monitor.py` | Bounded observations and projections. Expose enough structured state for truthful labels; do not turn it into a full-document transport. |
| `llm_tools/bridge_theme.py` | Reuse semantic colors, light/dark handling and override behavior. |
| `llm_tools/bridge_runtime.py` | Touch only the narrow integration for validated start/attach, observed running/deferred states, and UI terminal layout. |
| `llm_tools/bridge_store.py` | Reuse authoritative full source and existing transitions; avoid schema changes unless draft recovery genuinely requires one. |
| `llm_tools/bridge_usage.py`, `bridge_refresh.py` | Preserve lazy/background capacity reads. Provider operation must not block on a Usage view refresh. |

Suggested new modules, only if they keep responsibilities clear: `bridge_input.py` for buffer/width/paste state, `bridge_documents.py` for full-source viewer/rendering, `bridge_help.py` for packaged topics and action descriptions. Names are suggestions. Do not create a generic widget framework or duplicate the CLI parser.

Use one small action description registry to connect palette labels, availability, target requirements, context help, and public command examples. It should call existing dispatch/transition paths. Argparse remains the command grammar. Avoid introducing another command parser merely to generate pretty menus.

## 6. Exceptional built-in documentation

### Help structure

Add `llm-bridge help [TOPIC ...]`. Support at least `quickstart`, `keys`, `prompts`, `markdown`, `providers`, `states`, `verification`, and `recovery`, plus command paths such as `help task verify`. The topic list itself must be short and useful. Mirror those topics inside the integrated help viewer, reachable from the home screen, each Bridge view, command input via `help`, and the station prefix. Search help with the document viewer's `/` behavior.

Package essential help with the Python installation. It must work outside the checkout, offline, without Rich, without tmux, and with no mission database created. Do not read README from a checkout-relative path. Share topic content between CLI and TUI so they cannot contradict each other.

Root `--help` should fit roughly one 80x24 page when practical. Lead with the purpose, common commands, and three real examples. Hide internal commands from the public listing and usage choices without removing private entry points. Add a one-line description to every public subcommand. Group advanced flags in command help; retain complete discoverability with `--help` and topic help. Do not hide advanced flags so thoroughly that automation authors cannot find them.

Every command help page must state: what changes, required target/input, relevant default, whether it starts paid/provider work, and a working example. Describe `--file -`, input preservation, `--json`, native versus autonomous/headless, repeated verifier argv arguments, routing versus merely viewed station, and safe boundary steering. Document scope of cancellation/stop/exit. Error messages should state problem, target/context, and one corrective action; offer command-specific help instead of the entire root parser dump.

Use concise text such as:

```text
llm-bridge — supervise AI work in your terminal

Start here
  llm-bridge                       Open missions; opening starts no provider
  llm-bridge help quickstart        Learn the basic workflow
  llm-bridge help prompts           Paste, import, edit and read large prompts

Common commands
  mission create   Save a mission and its first task
  start            Start provider work for a saved mission
  attach           Reopen its existing terminal station
  status           Inspect current work and blockers
  task             Add, inspect, amend and verify tasks
  pause            Hold after the current invocation returns
  stop             Stop the owned mission controller

More: llm-bridge COMMAND --help · llm-bridge help keys
```

This is a hierarchy/copy example, not a license to omit other public commands. Keep the complete command set reachable and documented. Use actual implemented grammar for all final examples.

### Content that must be taught

1. Create/save, start, focus provider, return to Bridge, detach, reattach, verify.
2. A zero provider exit means awaiting verification, not completed work.
3. Exiting Bridge/detaching keeps the mission distinct from stopping/cancelling it.
4. Pause is deferred while a provider invocation is running; it never suspends the host.
5. A 30 KiB+ prompt belongs in the composer or `--file`; Enter and Ctrl-S meanings are explicit.
6. Source versus Markdown display, paging/search/wrap, export, and draft recovery.
7. Only a verified exact native identity can be resumed; otherwise Bridge reconstructs context.
8. Native provider keys, clipboard behavior, mouse/scrollback, prefix passthrough, and full-screen return paths.
9. Missing tmux/CLI, authentication problems, capacity waits, stale observations, rejected edits, and uncertain operations each have a concrete next step.

Update README and `docs/llm-bridge/operations.md` to match the implemented interaction contract, mode defaults, eight views, key contexts, and help topics. Add a short task-oriented quickstart and troubleshooting recipes. Keep deep internals in the operations guide rather than introductory help. Replace screenshots only with real rendered states; text instructions remain complete without images. Check a built wheel or temporary installed package from an unrelated directory to prove that help and assets ship.

## 7. Sequenced implementation work

Each phase must produce functioning code, narrow tests, and updated built-in wording for the behavior it changes. Do not defer all testing or help to the final phase.

### Phase 1 — Regression evidence and shared input model

Read existing fixtures and establish the current baseline. Add focused regressions for cursor coordinates/visibility, doubled/trailing spaces, mid-line insertion, history draft preservation, Ctrl-F cancellation, launcher truncation, multiline paste crossing fields, and unknown key sequences. Use injected windows for reducer logic and real PTY/tmux for rendered cursor and event ownership. Introduce the shared buffer/focus/cell-width model, then fix short input and form rendering. Preserve action dispatch and stored text semantics.

Acceptance: `ab`, Left, `X` becomes `aXb`; Backspace/Delete act beside the caret; `a  b ` remains visibly exact; cursor is always in the active text widget after refresh and resize. Keys used for navigation never mutate text unexpectedly.

### Phase 2 — Provider-first station and full terminal access

Implement provider-focused, Bridge-focused, and optional split layouts. Add labelled prefix actions and direct `Start and open provider`. Preserve owned pane/process IDs and current attach/detach/session validation. Test the real attached client, not only detached capture. Verify both lowercase/uppercase bindings, zoomed switching, prefix passthrough, status clicks, mouse routing, Ctrl-C forwarding, focus events, resize, and return to the same provider process.

Acceptance: at 80x24 a provider-focused child receives 80x23 with one status row; allow a documented full-screen status-toggle mode for children needing every row. Bridge-focused controls do not steal native keys after return. A 64 KiB bracketed paste reaches a raw fake provider byte-for-byte, including delimiters as delivered by the terminal protocol, with no double send and no Bridge command interpretation. Compare direct and wrapped child behavior at the same viewport dimensions.

### Phase 3 — Large composer, paste transaction, draft safety

Build the shared full-screen text composer and form integration. Add byte-counted limits, transactional paste, batching, private recovery drafts, import/export, and external editor handling. Preserve source across rejection and late results. Avoid changing the provider's input path.

Acceptance: 30/32/64/256 KiB fixtures survive create, inspect/export, unchanged amendment, and failure/recovery. The last marker is present. An over-limit paste is rejected atomically. A multiline paste never touches Providers or submits a command. An editor save rejected by a mission transition leaves a recoverable draft.

### Phase 4 — Full document viewer and Markdown adapter

Add full-source on-demand reading, wrapping, horizontal scrolling, search, headings, and source/render toggles. Integrate the declared Rich dependency with a plain-source degraded fallback. Cache document layout by source identity/revision, viewport width, mode, and theme. Reflow only when those change; draw visible rows without reparsing the entire document every 100 ms. Bound caches and release them when documents close.

Acceptance: navigate/search to the exact tail of a 256 KiB prompt; inspect long code lines; toggle source/render and resize while retaining place. Malformed Markdown and renderer absence remain readable. No rendered text overwrites native provider output. Export always produces original source, regardless of display mode.

### Phase 5 — Navigation, actionable overview, precise feedback

Introduce the small action registry; fix palette clamping/click geometry; implement focus-aware keys and view overflow. Add action availability and scoped confirmations. Improve overview and Systems hierarchy, deferred pause labels, last-selected versus active state, and typed feedback. Handle subprocess deadlines and ambiguous outcomes without mutation replay.

Acceptance: all views are discoverable at 80 columns; keyboard and mouse select the same visible row after scrolling; terminal missions cannot lure users into discarding a new task; completed missions show no active provider without a verified running operation. A slow snapshot never stalls input. A rejected command can be corrected without retyping.

### Phase 6 — Help, packaging, and acceptance walkthrough

Finish the shared help topics, concise parser help, documentation updates, and installed-package checks. Ensure every newly introduced key/action is taught on the relevant surface and in `help keys`. Run all required tests and coverage. Capture real terminal examples with fake state, and list any provider-specific live acceptance that remains unverified.

Do not call the project complete after Phase 1 or after merely installing a renderer. Every P0/P1 contract in this plan needs implementation or an explicit, evidence-backed blocker.

## 8. Test matrix and performance evidence

Use existing hermetic `env`, fake executable fixtures, temporary private state, and unique owned tmux sockets. Tests must not read ambient credentials or launch real network refreshes. Extend existing suites where practical rather than building a second harness.

| Area | Required cases |
| --- | --- |
| Geometry | 80x24, 120x40, 60x12 fallback, shrink/grow while typing, provider/Bridge zoom, status off/full-screen. |
| Input | Empty, whitespace-only, trailing/repeated spaces, quotes, paths with spaces, CJK, combining accents, common emoji, Home/End/Delete, unknown escape sequences, history drafts, search cancellation. |
| Large text | 30/32/64/256 KiB; one line and many lines; Markdown, tabs, blank lines, Unicode; first/middle/tail markers; exact source hash; 1 MiB boundary and just over it. |
| Paste | Bracketed payload split at every protocol boundary, split UTF-8, newlines and control-looking text, interrupted paste, oversize rollback, resize during paste, no dispatch before explicit save. |
| Documents | Page to end/back, global search, heading navigation, horizontal overflow, preserved anchor, built-in renderer present and deliberately failing/missing, source export, malformed fences, no remote fetch. |
| Navigation | All eight views, selected tab visible, palette search, bounded selection, scrolled mouse hit-testing, empty results, no accidental destructive activation. |
| Lifecycle | Created, running, deferred pause, capacity wait, verifying, blocked, completed, stopped, uncertain operation, rejected edit, late command result, detached controller. |
| Provider terminal | Raw alternate-screen fixture, menus, cursor, long paste, arrows/Tab/Escape/Ctrl-C, child mouse/wheel, focus, copy mode, zoom, detach/reattach, unchanged process/session identity. |
| Help | Root and nested help, topic search, no internal names/`==SUPPRESS==`, valid examples, no database creation, no-TTY plain output, packaged install outside checkout. |
| Accessibility | Dark/light, 8/16/256 colors, NO_COLOR, TERM=dumb, ASCII fallback where needed, focus/state conveyed without color. |
| Failure recovery | Missing renderer/editor/tmux, editor failure, rejected save, database busy, delayed snapshot, timeout with unknown mutation outcome, terminal disconnect, draft reopening. |

Use end-to-end checks that assert visible cells and tmux cursor coordinates/flags, not only the presence of bytes in an ANSI transcript. Assert stored/exported source, not just a preview. A rendered marker outside the viewport is not evidence of successful navigation to it.

Performance goals, measured on the test machine and reported with methodology: ordinary key/focus feedback around 100 ms or less; no multi-second Escape delay; usable progress feedback during a large paste; 256 KiB ingestion/layout and page navigation without freezing the UI or rereading all history. Aim for first usable document viewport within 500 ms on a normal local machine, with asynchronous progress if rendering takes longer. These are design targets, not measurements from this review. Avoid fragile wall-clock assertions in shared CI; use operation-count/bounded-work assertions and opt-in measurements. The current 32,791-byte form paste took about 3.4 seconds to reach the next field in the isolated tmux test and still lost data.

Run the repository-mandated verification after implementation:

```bash
python -m pytest -q
coverage run -m pytest
coverage combine
coverage report --fail-under=85
```

Also run the relevant CLI help, JSON, and usage smoke checks required by AGENTS.md under hermetic provider fixtures. Exercise the installed package in a temporary environment and unrelated working directory. Verify that ordinary installation brings in the renderer and enables Markdown. Deliberately simulate a broken/missing renderer for fallback tests. If tmux is missing, report exactly which acceptance checks were skipped; do not present skipped native tests as passing coverage.

## 9. Final acceptance walkthrough

1. Open Bridge from an arbitrary folder. Read quick help without starting work. Create a Native interactive mission using a 64 KiB multiline Markdown paste. Inspect its size and tail before saving. The stored prompt equals the source.
2. Start explicitly and enter the complete provider interface. Use its own command/menu picker, edit text, scroll, and paste. Bridge takes only its status row unless deliberately opened. Switching to Bridge and back preserves the child process and its input state.
3. Open the mission/task prompt in Bridge. Page to the end, search for a tail marker, inspect a code fence, toggle source mode and wrap, and return to exactly the prior provider/Bridge context. No prompt data changes merely from viewing it.
4. Type a command with doubled spaces, move the caret into its middle, correct it, open and cancel a search, and recall history. Nothing is silently lost or normalized.
5. Submit an invalid command and correct it from the retained draft. Reject an edited task at a late boundary and recover/export the entire draft. An oversized paste produces an explicit message and leaves the previous buffer intact.
6. Pause a running fake invocation. The screen says the pause is deferred and identifies work still running. Complete and verify it. The completed screen reports no active execution and offers the appropriate next actions.
7. Find the same explanation through `help`, `?`, and CLI topic help. Help works offline from an installed package with no Rich, no mission state, and no tmux. Source viewing remains fully usable.
8. Show passing full tests, the mandatory 85% coverage gate, native terminal comparison evidence, source integrity checks, and any remaining environment/provider limitations.

The walkthrough is a verification gate, not the end of execution. Continue directly into section 10. Keep the implementation scoped to this text-first, provider-first interaction contract.

## 10. Mandatory PR, adversarial review, merge, and tag

Carry out this sequence without waiting for another user prompt:

1. **Finish the implementation.** Satisfy every phase and acceptance contract above. Preserve unrelated changes. The supplied plan may initially be untracked; treat it as task input, preserve its contents, and include the relevant implementation record in the focused branch. Do not treat an untracked specification as a reason to leave the implementation untouched.
2. **Verify the complete branch.** Run the full tests, mandatory coverage gate, focused PTY/tmux acceptance, packaging/install checks, and required hermetic CLI checks. Record real results and limitations. Fix failures before proceeding. Skipped or unperformed checks are not passes; unavailable mandatory evidence remains an open gate.
3. **Create a merge-ready PR.** Push the focused branch and open a non-draft PR against the correct base. Describe the problem, delivered behavior, validation, and material limitations. Inspect CI to completion and fix failures. Keep the PR description and ledger aligned with the final implementation.
4. **Adversarially review the actual PR diff.** After the PR exists, inspect its current diff against the base, production code, and acceptance evidence in a separate review pass. Challenge data integrity, draft recovery, terminal input ownership, cursor/Unicode/paste boundaries, asynchronous state/races, resource cleanup, packaging, compatibility, and tests that could pass while the UX remains broken. Use independent review tooling when available; accurately distinguish self-review from independent review. Do not merely reread your summary or assume passing tests establish correctness.
5. **Fix and re-review.** Record findings with severity, location, impact, reproduction, and verification. Add meaningful regressions for discovered defects, fix them, push, rerun affected checks and the final required gates, and review the changed PR again. Address external review comments and resolve threads with evidence. No unresolved correctness, data-loss, terminal-fidelity, or required-acceptance defect may be waived to finish. Do not claim a finding was verified without checking its correction on the current revision.
6. **Merge the reviewed revision.** Confirm the latest head has passing required CI, satisfies review requirements, and has no unresolved blocking findings or conflicts. Use the repository's normal supported merge method. Rebase or update and revalidate if needed; do not bypass protections. Actually merge the PR and verify GitHub reports it merged with the intended changes in the base branch. A merge-ready PR alone does not satisfy this prompt.
7. **Tag the merged release.** Inspect current tags, package version, and release instructions early enough to include any required version/changelog change in the reviewed PR. Follow the existing naming/versioning convention; if none exists, choose a new semantic version appropriate to the change and document the choice. Create the release tag on the resulting merged base commit, push it, and verify the remote tag resolves to that commit. Never replace an existing tag or tag an unmerged PR head. Verify any required tag-triggered checks before claiming release completion.

The normal completion gate is: all six phases implemented; acceptance evidence recorded; full tests and at least 85% coverage passing; required native and installed-package checks passing; actual PR adversarially reviewed and defects fixed; required CI/reviews satisfied on the final revision; PR merged; release tag pushed and verified on the merged commit.

Only then give a concise final report with the delivered behavior, actual validation results and coverage, material review findings and fixes, PR URL, merge commit, and release tag. If a genuine external blocker prevents the gate, explicitly report **incomplete**, name the unmet gate and evidence, and state the exact external action needed after exhausting independent work. Do not present an inspection report or continuation offer as the deliverable.

**Begin implementation now. This is the only execution prompt you should need. Continue through implementation, PR review/fixes, merge, and verified tagging without an early final response.**
