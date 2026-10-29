# LLM Bridge 1.1.1 patch verification

User requests after 1.1.0: repair local startup from any folder, add professional
semantic colours readable on dark/light terminals, and release all changes as
a thoroughly tested patch. The original directive remains unchanged.

## Local startup

The existing pipx editable package was recorded as 0.1.0 and exposed only three
older commands. Refreshing that same installation exposes all five commands,
including `~/.local/bin/llm-bridge`, and its installed metadata now reports 1.1.1.
Bare interactive `llm-bridge` previously failed with a required-subcommand error;
it now opens mission selection/creation using the current folder. Creation uses
the shared public handler, stores a task, and opens controls without provider
autostart. Explicit CLI commands remain available. Non-TTY bare use prints help.

Actual installed-command acceptance ran outside the checkout with PYTHONPATH
and PYTHONHOME removed, using dark/light terminals, isolated HOME/XDG/state,
a folder containing spaces, a literal `--help` objective and clean Ctrl-D exit.
A harmless installed-controller run reached VERIFYING then stopped with lock
release, using an injected capacity fixture and fake command, zero provider
calls (`local-startup-results.json`). Global local storage doctor reports
integrity `ok` and tmux available. CI permanently checks the installed command
from a temporary unrelated directory.

## UX and safety regressions

Semantic headings, current tab, selection, command input, status/warnings/errors
and secondary text use a shared dark/light palette and common role overrides.
Terminal backgrounds remain native. NO_COLOR/dumb/non-TTY fallbacks and limited
palettes are tested; light eight-colour text avoids ANSI bold-bright promotion.
The native provider retains its own colours. Actual installed-terminal captures
are `../img/llm-bridge-dark.png` and `../img/llm-bridge-light.png`, with isolated
acceptance state and no provider execution.

A25 repairs the real-PTY test's invalid assumption that terminal EOF proves
child exit: it now observes explicit stdin rejection, unchanged task count and
bounded child wait after one exit key. A26 preserves embedded Usage column
spacing, response indentation and navigation hitbox alignment while styling.
A27 preserves leading-dash objectives via literal parser arguments. Independent
Sol 6.1 High review closes all findings and approves final source conditional
on complete full/coverage/artifact/CI gates.

## Final frozen source gates

| Gate | Result |
| --- | --- |
| Python 3.11.14 | 1,299 passed in 199.94s |
| Python 3.15.1 | 1,299 passed in 191.83s |
| Global branch-coverage report | 89% both, required 85% |
| Bridge/terminal branch-coverage report | 91% both, required 90% |
| Independent final gate | 145 tests in 14.08s plus 39 launcher/color/editor tests in 3.87s |
| Native Codex two-prompt probe | Reply/full-stop/same owned UUID/new controller/context recall/cleanup PASS |
| Native zero-prompt comparison | Local input and equal-viewport wheel parity PASS; provider selection click NOT VERIFIED |
| Paid Claude/Copilot reply/restoration | NOT VERIFIED; not attempted |

Full runs use `/tmp/bridge-111-release-{311,315}/` with unique COVERAGE_FILE and
basetemp, `coverage run -m pytest -o addopts='' -q`, then the unchanged exact CI
reports `--fail-under=85` and
`--include='llm_tools/bridge*.py,llm_tools/terminal.py' --fail-under=90`.
No exclusions or gate reductions were introduced. Source stayed frozen during
these runs; earlier diagnostic runs are not final evidence.

Final wheel/sdist were rebuilt and installed into isolated 3.11/3.15 environments.
Installed metadata1.1.1, all five entry points/help, non-TTY bare help and actual
TTY home startup passed from unrelated temporary folders for both artifacts.
Published artifacts must be downloaded, digest/source-compared and reinstalled.
Exact-head CI on both interpreters is mandatory before merge/tag 1.1.1. GitHub
PR/release state records publication actions after this committed verification.
Structured live evidence is `patch-native-{mission,input}.json`; no raw screens,
credentials or provider transcript payloads are stored.
