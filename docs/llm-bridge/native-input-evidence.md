# Zero-prompt native input probe

The opt-in probe is `scripts/bridge_native_input_probe.py`. It takes an explicit Codex session UUID and its exact working directory, verifies structured session metadata for that pair, and uses no model prompt. It launches each provider in a native terminal. The comparison path runs the provider directly as the sole pane in a clean private tmux server; the Bridge path uses `TmuxTerminalBackend` with the provider and Bridge panes. Both paths are attached through an outer PTY.

The probe uses only Codex `/model` and Claude `/help`, then a click on the current Codex model row and a wheel event. It never saves screen text or transcripts. It reports mouse effects only from provider-visible UI state. A wheel scroll passes only if the known `NATIVE_BRIDGE_OK` smoke marker changes viewport row while `pane_in_mode` is zero before and after the event. An absent marker is reported as not verified.

Run the exact owned-session probe with:

```sh
python scripts/bridge_native_input_probe.py \
  --codex-session-id 01a17795-0e6a-7a00-9080-e5db0c68a535 \
  --codex-cwd /tmp/lb-native-owned/work \
  --provider-timeout 30 \
  --history-marker NATIVE_BRIDGE_OK \
  --output /tmp/bridge-native-input-results.json
```

Observed versions were Codex CLI 0.164.0 and Claude Code 2.1.310. Local Codex model-picker input and Claude `/help` rendered in both the direct single-pane and Bridge terminal paths. Clicking the current Codex model row produced no visible effect in either path, so provider mouse selection remains not verified. With tmux `pane_in_mode=0` before and after the wheel event, the known marker moved from rows 3, 6, 14 to rows 5, 8, 16 through Bridge tmux. The direct single-pane control stayed at rows 5, 8, 16, so direct-path wheel scrolling is not verified. The probe submitted zero model prompts and both private terminal servers were cleaned up. See [native-input-results.json](native-input-results.json) for the machine-readable observations.
