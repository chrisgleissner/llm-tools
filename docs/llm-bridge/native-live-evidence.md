# Live Codex probe evidence

This note records the Codex runtime evidence that has a saved result. It does
not convert a chat recollection into a pass. No further Codex prompts were sent
for this note.

## Native terminal probe

The saved multi-provider native PTY probe was invoked as:

```bash
python scripts/bridge_provider_smoke.py \
  --provider codex --provider claude --provider copilot \
  --seconds 20 --model-call-budget 3
```

The script uses the production `TmuxTerminalBackend` to launch each real native
CLI in a private tmux server rooted in a disposable git worktree. Its single
initial prompt per selected provider is harmless and requests one fixed reply;
the budget of three covers the three providers in this invocation. It captures
provider output in memory and excludes raw screens from the report. Codex was
`codex-cli 0.164.0` in the result.

The durable source is `provider-model-results.json`, recorded in
`provider-capabilities.md`. Its Codex row records:

| Check | Result |
| --- | --- |
| Startup output | Observed |
| Process alive after startup | Yes |
| ANSI rendering | Pass |
| Pane geometry after resize | 80x17, 120x33, 180x48; geometry pass |
| Expected model reply | **Not verified** before the deadline |
| Mouse input | Not verified |
| Native session resume | Not verified |
| Keyboard | Ctrl-C was sent twice; response not verified |
| Private tmux server cleanup | Pass |
| Probe duration | 20.639 seconds |

The probe did not attach an outer terminal client. Attach and detach behavior
are therefore **not verified by this live provider probe**. Its cleanup result
means the private tmux server was removed after the probe; it does not prove
that a user-attached client detached cleanly.

## Separate headless Bridge reply

A separate one-prompt headless Bridge smoke has the saved, payload-free result
`/tmp/bridge-live-codex.json`:

```json
{
  "provider": "codex",
  "initial_prompt_budget": 1,
  "model_execution": "PASS: expected reply through Bridge scheduler",
  "native_restore": "NOT VERIFIED",
  "controller_started": true,
  "task_state": "RUNNING",
  "cleanup": "PASS",
  "duration_seconds": 7.129
}
```

This proves one expected reply through the headless Bridge scheduler. It does
not exercise a native provider terminal, outer-client attach/detach, or native
session-ID capture and restoration. The result file does not preserve the
original shell argv; the smoke entry point and its bounded one-call budget are
in `scripts/bridge_live_smoke.py`.

## Conclusion

The available evidence supports native Codex startup output, ANSI rendering,
pane resizing, and private-server cleanup, plus a separate successful
headless Bridge reply. It does **not** support a successful native first reply,
native attach/detach, or native session-ID restoration. Codex native
restoration remains **NOT VERIFIED**.
