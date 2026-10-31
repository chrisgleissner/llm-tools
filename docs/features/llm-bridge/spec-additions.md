# Additional accepted Bridge requirements

These extend the original directive in `prompt.md`; later user instructions
supersede earlier defaults. Implementations and verification remain traceable in
`../../llm-bridge/spec-audit.md` and `HANDOVER.md`.

- Bridge code has a separate **90% branch coverage CI gate**, retaining global
  85%. CI tests Python 3.11 minimum and current 3.15 series.
- Use Sol 6.1 **High** for independent review. Live paid checks currently use
  Codex only; installed Claude command propagation is checked without claiming
  an authenticated completion.
- Before major changes are committed, run the relevant opt-in native probe
  scripts: native model response, exact-owned-ID resume and observable native
  mouse behavior. CI tests their evidence oracles and deterministic terminal
  transport; it never initiates paid requests. Unobserved capabilities remain
  NOT VERIFIED and cannot be converted to passing startup/geometry checks.
- A deliberate outer terminal prefix returns from any provider to Bridge,
  including when zoomed. A persistent clickable Bridge target gives the same
  operation. Provider keystrokes/paste/mouse inside its pane remain native;
  double prefix forwards a literal reserved key. Document nested-tmux conflicts.
- Every standalone operator command uses the same parser/validation/handlers in
  the interactive REPL. The mission may be implicit. Multiline interactive
  entry retains task priority/dependencies/acceptance settings. Direct provider
  input remains available, while durable amendments require Bridge submission.
- Show estimated time to exhaustion for the active model's 5h and weekly scopes,
  plus alternate eligible providers/routes. Reuse llm-usage's sample history and
  confidence rules; show unknown when evidence is insufficient. Distinguish
  runout estimates from resets, configured model from observed model, and data
  freshness from liveness. Opaque routes never receive invented percentages.
- Provide the **full llm-usage dashboard inside Bridge**, using its existing
  Python readers/builders/renderer without launching an llm-usage subprocess.
  Refresh outside the input/render thread, preserving all available columns,
  provider/model scopes, route and budget information through scrolling.
- Dogfood Bridge for its own implementation. Any direct CLI versus facade
  discrepancy becomes a documented implementation/spec issue. README includes
  an actual Bridge screenshot. Merge and release only after all gates pass.

## Operator experience requirements (2026-10-29)

- **Primary scenario: a user sitting at the terminal.** Bridge must be
  exceptionally easy to use and intuitive without reading documentation.
  Keyboard first, mouse second: every action is reachable from the keyboard,
  and each screen shows the keys that apply to it.
- **Secondary scenario: remote operation.** A user who logs into the machine
  over SSH (or any second terminal) must be able to find a running mission,
  inspect it, attach to its provider station and detach again without
  disturbing the local session. Station discovery must not depend on
  environment details that differ between login types (for example
  `XDG_RUNTIME_DIR`). Clipboard copy from the station works over SSH (OSC 52).
- **Screen real estate.** Views use the available rows and columns for
  information, not boilerplate: no repeated identifiers on every row, no raw
  Python/JSON reprs, no `None` placeholders, aligned columns with visible
  separators, relative times instead of epochs, and table rows that are never
  split across lines.
- **Familiar to Codex and Claude Code users.** The command line at the bottom
  behaves like those CLIs: slash commands (`/pause`, `/status`, `/help`) are
  accepted alongside bare commands, `?` shows shortcuts, Esc returns focus
  without side effects, and the provider terminal keeps native key latency
  (no multiplexer escape delay) and native modified keys such as Shift-Enter.
- **Human output by default.** CLI commands print concise human-readable text
  unless `--json` is given; `--json` remains the stable machine interface.
  Missions can be addressed by a unique ID prefix, and the mission is inferred
  from the current folder when exactly one mission matches.
- **Colour and style.** Colour carries meaning (state, severity, focus,
  provider identity, capacity health) and is used generously but consistently.
  Styles are configurable (built-in themes plus per-role overrides in the
  llm-tools config and environment). The default style must be legible on dark
  and light backgrounds in the common Ubuntu/GNOME, KDE Konsole and other Linux
  terminals and, best effort without a test machine, macOS Terminal.app and
  iTerm2. It degrades to monochrome for `NO_COLOR`, `TERM=dumb` and
  non-terminals, and to ASCII glyphs when the locale is not UTF-8.
- **Style and substance together.** Every UX change ships with automated tests
  against mocked backends and is exercised by manual dogfooding with real
  providers (Codex, Claude Code, Copilot).
- **First-run fixes (2026-10-29).** `stop` never cancels (only `cancel` does);
  with no controller it is a no-op that says so. Task numbers are per mission
  (`ordinal`, schema 7); `--task N` is that number and `--task-id` the global ID.
  `task verify` without `--approve` only records evidence. Console read-only
  commands print the CLI text unless `--json` is typed; free text after
  `reject`, `verify N`, `steer` and `proposal reject` is taken literally. The
  saved llm-tools config is never printed and is dropped from `mission create`
  and `mission list` JSON unless `--include-config`. `status` ends with a
  `Needs you` line (`needs_you` in the snapshot). `task add` reopens a completed
  mission (`mission-reopened`). `task amend` applies at once when no provider
  step runs the task. `doctor` checks each rotation provider's command and
  sign-in files. `detach` lists attached terminals when it needs a choice.
- **Mouse wheel.** The scroll wheel works everywhere as an alternative to
  PgUp/PgDn and the cursor keys: console views, overlays, document viewer,
  composer, home screen, provider checklist and mission picker, locally and
  over SSH (SGR mouse reporting), and inside the tmux station.
- **Chaotic input.** Bridge is fuzzed: seeded property tests in CI and a
  longer local fuzz runner cover raw key and escape streams, bracketed paste,
  mouse noise, resize storms, console command lines, CLI arguments, arbitrary
  snapshots at any terminal size, document text and store state sequences.
  Chaotic input never crashes Bridge, never corrupts state and never triggers a
  mutating action without an explicit operator decision.
