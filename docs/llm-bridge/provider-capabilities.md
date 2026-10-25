# Installed provider capability evidence

Observed 2026-10-25 on the implementation host. Version/help inspection is
not proof of authenticated runtime, mouse, model identity, or native restoration.
No credentials or provider conversation payloads belong in this record.

| Provider | Installed version | Observed native entry / resume interface | Runtime verification |
| --- | --- | --- | --- |
| Codex | 0.164.0 | `codex [PROMPT]`; `codex resume SESSION_ID [PROMPT]`; `--no-alt-screen` available | NOT VERIFIED |
| Claude Code | 2.1.310 | `claude [prompt]`; `--resume ID`; `--session-id UUID`; `--effort low/medium/high/xhigh/max`; background sessions have separate attach/stop commands | NOT VERIFIED |
| GitHub Copilot | 1.0.95 | `copilot -i PROMPT`; `--resume ID`; `--session-id UUID`; `--mouse on/off`; `--reasoning-effort` available | NOT VERIFIED |
| Kilo | 7.3.45 | `kilo --prompt TEXT`; `--session ID`; `kilo run` is a distinct noninteractive mode | NOT VERIFIED |
| OpenCode | 1.17.6 | `opencode --prompt TEXT`; `--session ID`; `opencode run` is a distinct noninteractive mode | NOT VERIFIED |
| MiniMax mmx | 1.0.16 | `mmx text chat --message TEXT`; no documented autonomous coding/session-resume interface in installed help | NOT VERIFIED; engineering-native session unavailable |

Commands inspected: each binary's `--version` and `--help`, plus
`codex resume --help`, `copilot help commands`, and `mmx text chat --help`.
Copilot documents `/resume`, `/session`, `/tasks`, `/model`, `/usage`, and
`/terminal-setup` for Shift+Enter. Bridge must leave native keys unchanged unless
the operator explicitly enters its own prefix/focus mode. tmux's configurable
prefix gives Bridge an independent control path; typing a prefix twice must
send the literal prefix to the provider.

Cross-provider continuation reconstructs task context; it does not transfer a
native conversation. Native resume must use an explicitly captured session ID,
never the global most-recent session. An accepted resume flag alone does not
prove native session restoration. Do not mark `RESTORED_NATIVE` without identity
confirmation.

The execution environment exposes the requested development models directly:
planner `gpt-6.1-sol` / `medium`, implementor `gpt-6-luna` / `high`, independent
reviewer `gpt-6-luna` / `xhigh`. These are separate agent contexts. No model-ID
substitution was needed.

The configured web search tool returned an error. Official pages were then
fetched directly with curl on 2026-10-25 and inspected:

- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
  (redirects to the consolidated developer commands page).
- [Claude interactive mode](https://code.claude.com/docs/en/interactive-mode).
- [Copilot CLI reference](https://docs.github.com/en/copilot/reference/cli-command-reference).

Claude uses Ctrl+C for interruption, Ctrl+D for exit, Shift+Tab for permissions,
and a Ctrl+X chord for subagent control. Its menus support mouse interaction.
Copilot documents mouse interaction for its session picker and diff view.
Bridge should reserve a tmux prefix and preserve these native keys. These
documentation observations do not establish runtime compatibility. Runtime
smoke results must be added separately, with an explicit call budget.

## Executed native PTY probes

`python scripts/bridge_provider_smoke.py --provider codex --provider claude
--provider copilot --provider kilo --provider opencode --seconds 5` used the
production `TmuxTerminalBackend`, private servers and disposable git working
directories. All five processes produced startup output and remained alive;
all private servers were removed. ANSI attributes were present in tmux screen
captures for Codex, Copilot, Kilo and OpenCode; Claude styling was NOT VERIFIED.
Geometry changed at 80x24, 120x40 and 180x55. These probes had no attached client,
so they establish pane geometry, not provider SIGWINCH handling. Raw provider
screens are deliberately excluded. Details: `provider-startup-results.json`.

An explicit budget of three initial harmless prompts was then exercised:
`python scripts/bridge_provider_smoke.py --provider codex --provider claude
--provider copilot --seconds 20 --model-call-budget 3`. None produced the expected
answer before the deadline. All three remain NOT VERIFIED for model execution;
startup output is not a successful engineering run. No automatic second attempt
was made. Details: `provider-model-results.json`. Native resume and actual
provider mouse/keyboard response remain NOT VERIFIED. The budget controls initial
prompt submissions, not internal requests or retries made by a provider CLI.

The hermetic `tests/test_terminal_attached.py` uses an actual outer PTY and
production attach method to verify the terminal transport independently of paid
providers; its latest recorded result belongs in progress.md and the test matrix.
