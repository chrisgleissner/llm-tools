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

## 2026-10-26: actual native Codex reply and exact-ID resume

Two bounded prompt submissions through the **full Bridge mission controller**
passed using installed Codex 0.164.0. The first native reply matched an anchored
response oracle; prompt echo cannot satisfy it. Bridge captured the conversation
UUID from its owned no-daemon process's writable transcript metadata. After
complete controller/PTY shutdown, the second task resumed the same exact UUID
in a new controller and returned a new marker requiring prior conversation
context. Operations recorded NATIVE_CREATED then RESTORED_NATIVE. Cleanup
released the controller lock. Raw response text/transcripts were not retained.

Executed: `python scripts/bridge_native_mission_smoke.py --model-call-budget 2
--seconds 35 --output /tmp/bridge-native-mission-results.json`.
Results: `native-mission-results.json`. The earlier lower-level backend probe
also passed reply/resume (`native-reply-resume-results.json`), but the full
mission result is the stronger evidence. No paid Claude/Copilot call was made.

**Provider mouse remains NOT VERIFIED.** An actual attached PTY SGR click in
Codex's model picker did not activate an effort picker. This is not a mouse
compatibility pass. Compare the same interaction against a direct native CLI
before attributing the limitation to Bridge; preserve supported native mouse
behaviour and document unsupported provider interactions explicitly.

Before major native/session changes are committed, rerun the bounded mission
script and relevant zero-budget startup/input/mouse probe. Live probes are
ad-hoc only; CI runs deterministic provider transport and oracle contracts.
