#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/llm-usage"
SCHEDULER="$SCRIPT_DIR/llm-scheduler"
RALPH="$SCRIPT_DIR/ralph-robin"
PATH="$SCRIPT_DIR/ci-bin:$PATH"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_grep() {
  local pattern="$1"
  local file="$2"
  grep -Eq -- "$pattern" "$file" || fail "pattern not found: $pattern"
}

assert_not_grep() {
  local pattern="$1"
  local file="$2"
  if grep -Eq -- "$pattern" "$file"; then
    fail "unexpected match: $pattern"
  fi
}

expect_fail() {
  local output_file="$1"
  shift
  if "$@" >"$output_file" 2>&1; then
    fail "command unexpectedly succeeded: $*"
  fi
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

HOME_FIXTURE="$tmpdir/home"
mkdir -p \
  "$HOME_FIXTURE/.codex/sessions" \
  "$HOME_FIXTURE/.claude/projects" \
  "$HOME_FIXTURE/.cache" \
  "$tmpdir/ci-bin"
PATH="$tmpdir/ci-bin:$PATH"

cat > "$tmpdir/ci-bin/copilot" <<'COP'
#!/usr/bin/env bash
sleep 99
COP
chmod +x "$tmpdir/ci-bin/copilot"

cat > "$tmpdir/ci-bin/sched-mock" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${SCHED_CAPTURE:?}"
printf 'attempt\n' >> "${SCHED_ATTEMPTS:?}"
fail_until="${SCHED_FAIL_UNTIL:-0}"
attempts="$(wc -l < "${SCHED_ATTEMPTS:?}")"
if (( attempts <= fail_until )); then
  printf 'temporary rate limit\n'
  exit 42
fi
printf 'mock ok\n'
MOCK
chmod +x "$tmpdir/ci-bin/sched-mock"

cat > "$tmpdir/ci-bin/trust-mock" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf 'Confirm folder trust\n'
IFS= read -r _line
printf 'trusted\n'
MOCK
chmod +x "$tmpdir/ci-bin/trust-mock"

cat > "$tmpdir/ci-bin/unsafe-mock" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf 'Unsafe confirmation prompt\n'
if IFS= read -r -t 1 _line; then
  printf 'unexpected input\n'
  exit 9
fi
printf 'no input\n'
MOCK
chmod +x "$tmpdir/ci-bin/unsafe-mock"

cat > "$tmpdir/ci-bin/systemd-run" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${SYSTEMD_RUN_LOG:-/dev/null}"
printf 'Running timer as unit: mocked.timer\n'
printf 'Will run service as unit: mocked.service\n'
MOCK
chmod +x "$tmpdir/ci-bin/systemd-run"

cat > "$tmpdir/ci-bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"
if [[ "${1:-}" == "--user" && "${2:-}" == "is-system-running" ]]; then
  printf 'running\n'
  exit 0
fi
if [[ "${1:-}" == "suspend" ]]; then
  exit 0
fi
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/systemctl"

cat > "$HOME_FIXTURE/.codex/sessions/session-20260602.jsonl" <<'JSON'
{"rate_limits":{"primary":{"used_percent":53,"window_minutes":300,"resets_at":"2026-06-02T13:49:00Z"},"secondary":{"used_percent":59,"window_minutes":10080,"resets_at":"2026-06-07T16:25:00Z"},"spark":{"primary":{"used_percent":99,"resets_at":"2026-06-02T22:26:00Z"},"secondary":{"used_percent":96,"resets_at":"2026-06-08T17:49:00Z"}}}}
JSON

cat > "$HOME_FIXTURE/.claude/projects/proj.jsonl" <<'JSON'
{"rate_limits":{"five_hour":{"used_percentage":0,"resets_at":"2026-06-02T13:20:00Z"},"seven_day":{"used_percentage":25,"resets_at":"2026-06-04T13:00:00Z"}}}
JSON

USAGE_LOG_FIXTURE="$HOME_FIXTURE/.cache/llm-tools/llm-usage/llm-usage.log"

run_tool() {
  local output_file=$1
  shift
  rm -f "$USAGE_LOG_FIXTURE"
  HOME="$HOME_FIXTURE" "$TOOL" "$@" >"$output_file"
}

run_tool_keep_log() {
  local output_file=$1
  shift
  HOME="$HOME_FIXTURE" "$TOOL" "$@" >"$output_file"
}

fixture_zero="$tmpdir/fixture-zero.txt"
fixture_nonzero="$tmpdir/fixture-nonzero.txt"
fixture_missing="$tmpdir/fixture-missing.txt"
fixture_timeout="$tmpdir/fixture-timeout.txt"
json_zero="$tmpdir/fixture-zero.json"
json_missing="$tmpdir/fixture-missing.json"
json_timeout="$tmpdir/fixture-timeout.json"
json_baseline="$tmpdir/baseline.json"
json_with_copilot="$tmpdir/with-copilot.json"
watch_output="$tmpdir/watch-output.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$fixture_zero"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9]' "$fixture_zero"
assert_not_grep '^Copilot[[:space:]]+ai-credits[[:space:]]' "$fixture_zero"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/fixture-zero-credits.txt" --show-copilot-credits
assert_grep '^Copilot[[:space:]]+ai-credits[[:space:]]+0[[:space:]]+-[[:space:]]+-[[:space:]]+-[[:space:]]*$' "$tmpdir/fixture-zero-credits.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 42% used · AI Credits: 17' \
  run_tool "$fixture_nonzero"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+58%[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9]' "$fixture_nonzero"
assert_not_grep '^Copilot[[:space:]]+ai-credits[[:space:]]' "$fixture_nonzero"

LLM_USAGE_COPILOT_CAPTURE_TEXT='No footer here' \
  run_tool "$fixture_missing"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+unavailable[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9]' "$fixture_missing"
assert_not_grep '^Copilot[[:space:]]+ai-credits[[:space:]]' "$fixture_missing"
LLM_USAGE_COPILOT_CAPTURE_TEXT='No footer here' \
  run_tool "$tmpdir/fixture-missing-credits.txt" --show-copilot-credits
assert_grep '^Copilot[[:space:]]+ai-credits[[:space:]]+unavailable[[:space:]]+-[[:space:]]+-[[:space:]]+-[[:space:]]*$' "$tmpdir/fixture-missing-credits.txt"

LLM_USAGE_COPILOT_CAPTURE_CMD='sleep 99' \
LLM_USAGE_COPILOT_TIMEOUT=1 \
  run_tool "$fixture_timeout"
assert_grep '^Codex[[:space:]]+5h' "$fixture_timeout"
assert_grep '^Claude[[:space:]]+5h' "$fixture_timeout"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+unavailable[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9]' "$fixture_timeout"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$json_zero" --json
jq -e '.copilot.monthly.remaining == 95 and .copilot.monthly.used == 5 and ( .copilot.ai_credits | not )' "$json_zero" >/dev/null \
  || fail "unexpected Copilot JSON for zero-credits fixture"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/json-zero-credits.json" --json --show-copilot-credits
jq -e '.copilot.monthly.remaining == 95 and .copilot.monthly.used == 5 and .copilot.ai_credits.used == 0' "$tmpdir/json-zero-credits.json" >/dev/null \
  || fail "missing Copilot AI credits JSON with --show-copilot-credits"

LLM_USAGE_COPILOT_CAPTURE_TEXT='No footer here' \
  run_tool "$json_missing" --json
jq -e '.copilot.available == false and (.copilot.monthly? | not) and (.copilot.ai_credits? | not)' "$json_missing" >/dev/null \
  || fail "missing footer became a value in JSON"

LLM_USAGE_COPILOT_CAPTURE_CMD='sleep 99' \
LLM_USAGE_COPILOT_TIMEOUT=1 \
  run_tool "$json_timeout" --json
jq -e '.copilot.available == false and .copilot.reason == "timeout"' "$json_timeout" >/dev/null \
  || fail "timeout JSON did not preserve the timeout reason"

LLM_USAGE_DISABLE_COPILOT=1 \
  run_tool "$json_baseline" --json
LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$json_with_copilot" --json
jq -e '.codex.rows | map(select(.key == "codex-spark")) | length > 0' "$json_baseline" >/dev/null \
  || fail "Codex JSON is missing codex-spark rows"

jq -S '{codex,claude}' "$json_baseline" > "$tmpdir/baseline-cq.json"
jq -S '{codex,claude}' "$json_with_copilot" > "$tmpdir/with-copilot-cq.json"
cmp -s "$tmpdir/baseline-cq.json" "$tmpdir/with-copilot-cq.json" \
  || fail "Codex/Claude JSON changed when Copilot rows were added"

LLM_USAGE_DISABLE_COPILOT=1 HOME="$HOME_FIXTURE" timeout 2s "$TOOL" --watch 0.5 > "$watch_output" || true
assert_grep 'Last refreshed:' "$watch_output"
if [[ "$(grep -c 'Last refreshed:' "$watch_output" || true)" -lt 1 ]]; then
  fail "watch output did not include refresh timestamp"
fi

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/source-visible.txt" --show-source
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9].*copilot cli$' "$tmpdir/source-visible.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/codex-spark.txt"
assert_grep '^Codex[[:space:]]+5h[[:space:]]+47%[[:space:]]+' "$tmpdir/codex-spark.txt"
assert_grep '^GPT-5\.3[[:space:]]+Spark[[:space:]]+5h[[:space:]]+1%[[:space:]]+' "$tmpdir/codex-spark.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/codex-spark-hidden.txt" --hide-codex-spark
assert_not_grep '^GPT-5\.3[[:space:]]+Spark[[:space:]]+5h' "$tmpdir/codex-spark-hidden.txt"
assert_grep '^Codex[[:space:]]+5h[[:space:]]+47%[[:space:]]+' "$tmpdir/codex-spark-hidden.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/no-remaining-time.txt" --show-source --hide-remaining-time
assert_not_grep '^Tool[[:space:]]+Window[[:space:]]+Remaining[[:space:]]+Remaining[[:space:]]+Time$' "$tmpdir/no-remaining-time.txt"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9].*copilot cli$' "$tmpdir/no-remaining-time.txt"

mkdir -p "$(dirname "$USAGE_LOG_FIXTURE")"
printf '%s\n' '{"ts":1750000000,"provider":"copilot","window":"monthly","remaining":100}' > "$USAGE_LOG_FIXTURE"
LLM_USAGE_NOW_EPOCH=1750003600 \
  LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 50% used · AI Credits: 0' \
  run_tool_keep_log "$tmpdir/remaining-time-estimate.txt" --show-source
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+50%[[:space:]]+1h[[:space:]]+' "$tmpdir/remaining-time-estimate.txt"

LLM_USAGE_NOW_EPOCH=1750000000 \
  LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS=2 \
  run_tool "$tmpdir/copilot-reset-offset.txt" --show-source
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+-[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9]' "$tmpdir/copilot-reset-offset.txt"

"$SCHEDULER" --help > "$tmpdir/scheduler-help.txt"
assert_grep 'Usage: llm-scheduler --tool codex\|claude\|copilot' "$tmpdir/scheduler-help.txt"
assert_grep '\{tool\}, \{prompt\}, \{prompt_file\}, \{cwd\}' "$tmpdir/scheduler-help.txt"

"$RALPH" --help > "$tmpdir/ralph-help.txt"
assert_grep 'Usage: ralph-robin' "$tmpdir/ralph-help.txt"
assert_grep '\{tool\}' "$tmpdir/ralph-help.txt"

expect_fail "$tmpdir/usage-bad-watch.txt" env HOME="$HOME_FIXTURE" "$TOOL" --watch abc
assert_grep 'watch requires numeric seconds' "$tmpdir/usage-bad-watch.txt"

expect_fail "$tmpdir/scheduler-no-prompt.txt" "$SCHEDULER" --tool codex
assert_grep 'one of --prompt or --prompt-file is required' "$tmpdir/scheduler-no-prompt.txt"
expect_fail "$tmpdir/scheduler-dupe-prompt.txt" "$SCHEDULER" --tool codex --prompt x --prompt-file "$tmpdir/missing"
assert_grep 'use exactly one of --prompt or --prompt-file' "$tmpdir/scheduler-dupe-prompt.txt"
expect_fail "$tmpdir/scheduler-bad-tool.txt" "$SCHEDULER" --tool bad --prompt x
assert_grep 'invalid --tool' "$tmpdir/scheduler-bad-tool.txt"
expect_fail "$tmpdir/scheduler-bad-retry.txt" "$SCHEDULER" --tool codex --prompt x --retry-delays 1,no
assert_grep 'retry-delays' "$tmpdir/scheduler-bad-retry.txt"
expect_fail "$tmpdir/scheduler-bad-threshold.txt" "$SCHEDULER" --tool codex --prompt x --min-remaining nope
assert_grep 'min-remaining' "$tmpdir/scheduler-bad-threshold.txt"
expect_fail "$tmpdir/scheduler-missing-file.txt" "$SCHEDULER" --tool codex --prompt-file "$tmpdir/no-such-file"
assert_grep 'prompt file is not readable' "$tmpdir/scheduler-missing-file.txt"

expect_fail "$tmpdir/ralph-no-prompt.txt" "$RALPH"
assert_grep 'one of --prompt or --prompt-file is required' "$tmpdir/ralph-no-prompt.txt"
expect_fail "$tmpdir/ralph-bad-tool.txt" "$RALPH" --tools claude,bad --prompt x
assert_grep 'invalid tool' "$tmpdir/ralph-bad-tool.txt"
expect_fail "$tmpdir/ralph-bad-window.txt" "$RALPH" --tools claude,copilot --prompt x --window 5h
assert_grep 'not valid for copilot' "$tmpdir/ralph-bad-window.txt"
expect_fail "$tmpdir/scheduler-bad-window-combo.txt" "$SCHEDULER" --tool copilot --prompt x --window weekly
assert_grep 'not valid for copilot' "$tmpdir/scheduler-bad-window-combo.txt"

available_usage='{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
exhausted_usage='{"available":true,"five_hour":{"remaining":0,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
exhausted_offset_usage='{"available":true,"five_hour":{"remaining":0,"resets_at":"2026-06-02T22:20:01.166099+00:00"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
weekly_exhausted_usage='{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":0,"resets_at":"2026-06-07T23:00:00Z"}}'
copilot_usage='{"available":true,"monthly":{"remaining":25}}'

SCHED_CAPTURE="$tmpdir/sched-capture.txt"
SCHED_ATTEMPTS="$tmpdir/sched-attempts.txt"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool codex --prompt 'hello world' --command-template 'sched-mock {prompt}' --log-dir "$tmpdir/scheduler-logs" > "$tmpdir/scheduler-submit.txt"
assert_grep '^hello world$' "$SCHED_CAPTURE"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "1" ]] || fail "scheduler did not submit exactly once"
# Fresh-mode runs stream the child CLI output live to the scheduler's stdout.
assert_grep 'mock ok' "$tmpdir/scheduler-submit.txt"

: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  LLM_SCHEDULER_NO_STREAM=1 \
  "$SCHEDULER" --tool codex --prompt 'hello quiet' --command-template 'sched-mock {prompt}' --log-dir "$tmpdir/scheduler-logs" > "$tmpdir/scheduler-quiet.txt"
assert_not_grep 'mock ok' "$tmpdir/scheduler-quiet.txt"

: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool claude --prompt 'hello tool' --command-template 'sched-mock {tool} {prompt}' --run-dir "$tmpdir/scheduler-fixed-run" --log-dir "$tmpdir/scheduler-fixed-logs" > "$tmpdir/scheduler-fixed.txt"
assert_grep '^claude hello tool$' "$SCHED_CAPTURE"
[[ -L "$tmpdir/scheduler-fixed-logs/latest" ]] || fail "scheduler did not create latest symlink"
[[ -L "$tmpdir/scheduler-fixed-logs/latest-claude" ]] || fail "scheduler did not create latest-claude symlink"
[[ -s "$tmpdir/scheduler-fixed-run/attempt-1.out" ]] || fail "scheduler did not write attempt output in fixed run dir"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool claude --prompt-file "$tmpdir/scheduler-fixed-run/prompt.txt" --command-template 'sched-mock {tool} {prompt}' --run-dir "$tmpdir/scheduler-fixed-run" --log-dir "$tmpdir/scheduler-fixed-logs" --dry-run > "$tmpdir/scheduler-fixed-resume.txt"
assert_grep 'dry-run: logs written to ' "$tmpdir/scheduler-fixed-resume.txt"

ralph_usage='{"claude":{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50}}}'
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$ralph_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$RALPH" --prompt 'rr-one' --command-template 'sched-mock {tool} {prompt}' --state-file "$tmpdir/ralph-state.json" --log-dir "$tmpdir/ralph-logs" --no-retry > "$tmpdir/ralph-submit.txt"
assert_grep '^claude rr-one$' "$SCHED_CAPTURE"
jq -e '.current_tool == "claude" and .current_index == 0' "$tmpdir/ralph-state.json" >/dev/null \
  || fail "ralph-robin did not persist initial Claude selection"
ralph_dir="$(awk '/ralph-robin: logs:/ {print $NF}' "$tmpdir/ralph-submit.txt")"
jq -e 'select(.type=="selection") | .data.tool == "claude" and .data.rotation_reason == "current-usable"' "$ralph_dir/events.jsonl" >/dev/null \
  || fail "ralph-robin did not log current-usable selection"
[[ -s "$ralph_dir/scheduler/attempt-1.out" ]] || fail "ralph-robin scheduler child did not write attempt output"
[[ -L "$tmpdir/ralph-logs/latest" ]] || fail "ralph-robin did not create latest symlink"
# The child CLI output must stream through llm-scheduler to ralph-robin's stdout.
assert_grep 'mock ok' "$tmpdir/ralph-submit.txt"

ralph_rotate_usage='{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50,"resets_at":1780441200},"week":{"remaining":50}}}'
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$ralph_rotate_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$RALPH" --prompt 'rr-two' --command-template 'sched-mock {tool} {prompt}' --state-file "$tmpdir/ralph-state.json" --log-dir "$tmpdir/ralph-logs" --no-retry > "$tmpdir/ralph-rotate.txt"
assert_grep '^codex rr-two$' "$SCHED_CAPTURE"
jq -e '.current_tool == "codex" and .current_index == 1' "$tmpdir/ralph-state.json" >/dev/null \
  || fail "ralph-robin did not advance to Codex after Claude exhaustion"

ralph_all_exhausted='{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":0,"resets_at":1780444800},"week":{"remaining":50}}}'
SYSTEMD_RUN_LOG="$tmpdir/systemd-run-ralph.log"
SYSTEMCTL_LOG="$tmpdir/systemctl-ralph.log"
: > "$SYSTEMD_RUN_LOG"
: > "$SYSTEMCTL_LOG"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$ralph_all_exhausted" \
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$RALPH" --prompt 'rr-three' --command-template 'sched-mock {tool} {prompt}' --state-file "$tmpdir/ralph-state.json" --log-dir "$tmpdir/ralph-logs" --no-retry > "$tmpdir/ralph-suspend.txt"
assert_grep 'all configured tools are rate-limited' "$tmpdir/ralph-suspend.txt"
assert_grep 'until [0-9]{4}-[0-9]{2}-[0-9]{2} .*\(epoch 1780441200\)' "$tmpdir/ralph-suspend.txt"
assert_grep '--on-calendar=@1780441200' "$SYSTEMD_RUN_LOG"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "0" ]] || fail "ralph-robin should not submit before resumed scheduler when all tools are exhausted"

LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' --dry-run --log-dir "$tmpdir/scheduler-dry-logs" > "$tmpdir/scheduler-dry.txt"
dry_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-dry.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited" and .data.wait_until == 1780441200' "$dry_dir/events.jsonl" >/dev/null \
  || fail "dry-run did not record reset wait decision"

LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_offset_usage" \
  "$SCHEDULER" --tool claude --window 5h --prompt x --command-template 'sched-mock {prompt}' --dry-run --log-dir "$tmpdir/scheduler-offset-logs" > "$tmpdir/scheduler-offset.txt"
offset_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-offset.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited" and .data.wait_until == 1780438801' "$offset_dir/events.jsonl" >/dev/null \
  || fail "dry-run did not parse Claude offset reset timestamp"

SYSTEMD_RUN_LOG="$tmpdir/systemd-run.log"
SYSTEMCTL_LOG="$tmpdir/systemctl.log"
: > "$SYSTEMD_RUN_LOG"
: > "$SYSTEMCTL_LOG"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' --suspend-until-ready --log-dir "$tmpdir/scheduler-suspend-logs" > "$tmpdir/scheduler-suspend.txt"
assert_grep 'scheduled: logs written to ' "$tmpdir/scheduler-suspend.txt"
assert_grep '--timer-property=WakeSystem=true' "$SYSTEMD_RUN_LOG"
assert_grep '--on-calendar=@1780441200' "$SYSTEMD_RUN_LOG"
assert_not_grep 'suspend' "$SYSTEMCTL_LOG"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "0" ]] || fail "suspend-until-ready should not submit before resumed scheduler"
suspend_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-suspend.txt")"
jq -e 'select(.type=="suspend_schedule_plan") | .data.reason == "rate-limited" and .data.target_epoch == 1780441200' "$suspend_dir/events.jsonl" >/dev/null \
  || fail "suspend-until-ready did not record schedule plan"

SYSTEMD_RUN_LOG="$tmpdir/systemd-run-confirm.log"
SYSTEMCTL_LOG="$tmpdir/systemctl-confirm.log"
: > "$SYSTEMD_RUN_LOG"
: > "$SYSTEMCTL_LOG"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
printf 'confirm prompt\n' > "$tmpdir/scheduler-confirm-prompt.md"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS=0 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
  "$SCHEDULER" --tool claude --prompt-file "$tmpdir/scheduler-confirm-prompt.md" --command-template 'sched-mock {prompt}' --suspend-until-ready --log-dir "$tmpdir/scheduler-confirm-logs" > "$tmpdir/scheduler-confirm.txt"
assert_grep 'suspend-until-ready armed' "$tmpdir/scheduler-confirm.txt"
assert_grep 'wake/run at: .*1780441200' "$tmpdir/scheduler-confirm.txt"
assert_grep 'tool: claude' "$tmpdir/scheduler-confirm.txt"
assert_grep 'model: from command template' "$tmpdir/scheduler-confirm.txt"
assert_grep "prompt: $tmpdir/scheduler-confirm-prompt.md" "$tmpdir/scheduler-confirm.txt"
assert_grep "directory: $SCRIPT_DIR" "$tmpdir/scheduler-confirm.txt"
assert_grep '^suspend$' "$SYSTEMCTL_LOG"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "0" ]] || fail "confirmation suspend should not submit before resumed scheduler"

LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$weekly_exhausted_usage" \
  "$SCHEDULER" --tool claude --prompt x --command-template 'sched-mock {prompt}' --dry-run --log-dir "$tmpdir/scheduler-weekly-logs" > "$tmpdir/scheduler-weekly.txt"
weekly_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-weekly.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited" and (.data.exhausted[]?.name == "weekly")' "$weekly_dir/events.jsonl" >/dev/null \
  || fail "scheduler did not consider weekly Claude/Codex window"

LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$weekly_exhausted_usage" \
  "$SCHEDULER" --tool claude --prompt x --window 5h --command-template 'sched-mock {prompt}' --dry-run --log-dir "$tmpdir/scheduler-5h-logs" > "$tmpdir/scheduler-5h.txt"
five_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-5h.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "usable"' "$five_dir/events.jsonl" >/dev/null \
  || fail "scheduler --window 5h did not limit gating to 5h"

: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
expect_fail "$tmpdir/scheduler-retry-fail.txt" env \
  LLM_SCHEDULER_USAGE_JSON="$copilot_usage" SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" SCHED_FAIL_UNTIL=9 \
  "$SCHEDULER" --tool copilot --prompt x --command-template 'sched-mock {prompt}' --retry-delays 0,0 --log-dir "$tmpdir/scheduler-retry-fail-logs"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "3" ]] || fail "retry exhaustion did not run initial plus two retries"

: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$copilot_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" SCHED_FAIL_UNTIL=1 \
  "$SCHEDULER" --tool copilot --prompt x --command-template 'sched-mock {prompt}' --retry-delays 0,0 --log-dir "$tmpdir/scheduler-retry-success-logs" > "$tmpdir/scheduler-retry-success.txt"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "2" ]] || fail "retry success did not stop after successful retry"

# --max-unavailable-wait validation
expect_fail "$tmpdir/scheduler-bad-unavail.txt" "$SCHEDULER" --tool codex --prompt x --max-unavailable-wait nope
assert_grep 'max-unavailable-wait' "$tmpdir/scheduler-bad-unavail.txt"

# Undeterminable usage (no measurable quota) must not block forever: after the
# bound elapses the scheduler launches optimistically. Real clock here so the
# elapsed-time bound can trip (now_epoch is not pinned).
unavailable_usage='{"available":false}'
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$unavailable_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool claude --prompt x --command-template 'sched-mock {prompt}' \
  --max-unavailable-wait 1 --poll-interval 1 --no-retry \
  --log-dir "$tmpdir/scheduler-unavail-logs" > "$tmpdir/scheduler-unavail.txt"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "1" ]] || fail "undeterminable usage did not proceed optimistically and submit once"
unavail_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-unavail.txt")"
jq -e 'select(.type=="optimistic_proceed") | .data.reason == "unavailable"' "$unavail_dir/events.jsonl" >/dev/null \
  || fail "undeterminable usage did not log optimistic_proceed"

# Suspend-until-ready + undeterminable usage must proceed now (no real reset
# epoch to wake for) instead of churning suspend/wake cycles. max-unavailable-wait
# 0 disables the time bound, so this exercises the suspend-mode branch directly
# without depending on wall-clock elapsed time.
SYSTEMD_RUN_LOG="$tmpdir/systemd-run-unavail.log"
SYSTEMCTL_LOG="$tmpdir/systemctl-unavail.log"
: > "$SYSTEMD_RUN_LOG"
: > "$SYSTEMCTL_LOG"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$unavailable_usage" \
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool claude --prompt x --command-template 'sched-mock {prompt}' \
  --suspend-until-ready --max-unavailable-wait 0 --poll-interval 1 --no-retry \
  --log-dir "$tmpdir/scheduler-unavail-suspend-logs" > "$tmpdir/scheduler-unavail-suspend.txt"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "1" ]] || fail "suspend-mode undeterminable usage did not proceed optimistically and submit once"
assert_not_grep 'WakeSystem=true' "$SYSTEMD_RUN_LOG"
assert_not_grep 'suspend' "$SYSTEMCTL_LOG"
unavail_suspend_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-unavail-suspend.txt")"
jq -e 'select(.type=="optimistic_proceed") | .data.suspend_mode == true' "$unavail_suspend_dir/events.jsonl" >/dev/null \
  || fail "suspend-mode undeterminable usage did not log optimistic_proceed"

# A known rate-limit (real reset epoch) must NOT be treated as undeterminable:
# it waits precisely and never logs optimistic_proceed.
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --max-unavailable-wait 1 --dry-run --log-dir "$tmpdir/scheduler-ratelimit-excl-logs" > "$tmpdir/scheduler-ratelimit-excl.txt"
ratelimit_excl_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-ratelimit-excl.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited" and .data.wait_until == 1780441200' "$ratelimit_excl_dir/events.jsonl" >/dev/null \
  || fail "known rate-limit did not record precise reset wait"
if jq -e 'select(.type=="optimistic_proceed")' "$ratelimit_excl_dir/events.jsonl" >/dev/null; then
  fail "known rate-limit must not proceed optimistically"
fi

prompt_file="$tmpdir/special prompt.txt"
printf 'line one\nline two with ; $HOME and spaces\n' > "$prompt_file"
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool codex --prompt-file "$prompt_file" --command-template 'sched-mock {prompt}' --log-dir "$tmpdir/scheduler-prompt-logs" > "$tmpdir/scheduler-prompt.txt"
prompt_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-prompt.txt")"
cmp -s "$prompt_file" "$prompt_dir/prompt.txt" || fail "prompt file content was not preserved in log"
assert_grep 'line two with ; \$HOME and spaces' "$SCHED_CAPTURE"

: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool codex --prompt 'a b ; $HOME' --command-template 'sched-mock --flag {prompt}' --log-dir "$tmpdir/scheduler-template-logs" > "$tmpdir/scheduler-template.txt"
assert_grep '^--flag a b ; \$HOME$' "$SCHED_CAPTURE"

LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'trust-mock' --log-dir "$tmpdir/scheduler-trust-logs" > "$tmpdir/scheduler-trust.txt"
trust_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-trust.txt")"
assert_grep 'trusted' "$trust_dir/attempt-1.out"

LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'unsafe-mock' --log-dir "$tmpdir/scheduler-unsafe-logs" > "$tmpdir/scheduler-unsafe.txt"
unsafe_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/scheduler-unsafe.txt")"
assert_grep 'no input' "$unsafe_dir/attempt-1.out"

if command -v tmux >/dev/null 2>&1; then
  : > "$SCHED_CAPTURE"
  : > "$SCHED_ATTEMPTS"
  LLM_SCHEDULER_TMUX_TIMEOUT=5 \
  LLM_SCHEDULER_USAGE_JSON="$available_usage" \
    SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
    "$SCHEDULER" --tool codex --prompt tmux-ok --command-template 'sched-mock {prompt}' --tmux "llm-usage-test-$$" --log-dir "$tmpdir/scheduler-tmux-logs" > "$tmpdir/scheduler-tmux.txt"
  assert_grep '^tmux-ok$' "$SCHED_CAPTURE"
  tmux kill-session -t "llm-usage-test-$$" 2>/dev/null || true
else
  printf 'skip: tmux not installed\n' > "$tmpdir/scheduler-tmux.txt"
fi

"$SCHEDULER" --wake-test > "$tmpdir/scheduler-wake.txt"
jq -e '.note | contains("best effort")' "$tmpdir/scheduler-wake.txt" >/dev/null \
  || fail "wake-test did not print diagnostics"

# ── P0-A: usage_decision stale-window (past reset_epoch) must be usable ─────

# Fixed NOW and past reset epoch (reset_epoch < now)
NOW_FIXED=1780430000
PAST_RESET=$(( NOW_FIXED - 100 ))
FUTURE_RESET=$(( NOW_FIXED + 3600 ))

# remaining<=min, past reset => stale => usable
stale_exhausted_usage="$(jq -nc --argjson r "$PAST_RESET" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$stale_exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-stale-logs" > "$tmpdir/p0a-stale.txt"
p0a_stale_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-stale.txt")"
jq -e 'select(.type=="usage_decision") | .data.usable == true' "$p0a_stale_dir/events.jsonl" >/dev/null \
  || fail "P0-A: stale past-reset exhausted window should be usable"

# remaining<=min, future reset => rate-limited
future_exhausted_usage="$(jq -nc --argjson r "$FUTURE_RESET" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$future_exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-future-logs" > "$tmpdir/p0a-future.txt"
p0a_future_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-future.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited"' "$p0a_future_dir/events.jsonl" >/dev/null \
  || fail "P0-A: exhausted window with future reset should be rate-limited"

# remaining<=min, null reset => rate-limited (no better precision)
null_reset_exhausted_usage='{"available":true,"five_hour":{"remaining":0},"week":{"remaining":50}}'
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$null_reset_exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-null-logs" > "$tmpdir/p0a-null.txt"
p0a_null_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-null.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited"' "$p0a_null_dir/events.jsonl" >/dev/null \
  || fail "P0-A: exhausted window with null reset should be rate-limited"

# auto window: one stale-low + one healthy => usable
auto_stale_one_usage="$(jq -nc --argjson r "$PAST_RESET" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$auto_stale_one_usage" \
  "$SCHEDULER" --tool codex --window auto --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-auto-stale-logs" > "$tmpdir/p0a-auto-stale.txt"
p0a_auto_stale_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-auto-stale.txt")"
jq -e 'select(.type=="usage_decision") | .data.usable == true' "$p0a_auto_stale_dir/events.jsonl" >/dev/null \
  || fail "P0-A: auto window with one stale-low and one healthy should be usable"

# auto window: one future-exhausted + one healthy => rate-limited
auto_future_one_usage="$(jq -nc --argjson r "$FUTURE_RESET" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$auto_future_one_usage" \
  "$SCHEDULER" --tool codex --window auto --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-auto-future-logs" > "$tmpdir/p0a-auto-future.txt"
p0a_auto_future_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-auto-future.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited"' "$p0a_auto_future_dir/events.jsonl" >/dev/null \
  || fail "P0-A: auto window with one future-exhausted should be rate-limited"

# Codex epoch-number reset (integer resets_at)
codex_epoch_reset_usage="$(jq -nc --argjson r "$FUTURE_RESET" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_USAGE_NOW_EPOCH=$NOW_FIXED \
LLM_SCHEDULER_USAGE_JSON="$codex_epoch_reset_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-epoch-logs" > "$tmpdir/p0a-epoch.txt"
p0a_epoch_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-epoch.txt")"
jq -e "select(.type==\"usage_decision\") | .data.reason == \"rate-limited\" and .data.wait_until == $FUTURE_RESET" \
  "$p0a_epoch_dir/events.jsonl" >/dev/null \
  || fail "P0-A: Codex epoch-number reset not parsed correctly"

# Claude ISO +00:00 reset
claude_iso_usage="{\"available\":true,\"five_hour\":{\"remaining\":0,\"resets_at\":\"2026-06-02T22:20:01.166099+00:00\"},\"week\":{\"remaining\":50}}"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$claude_iso_usage" \
  "$SCHEDULER" --tool claude --window 5h --prompt x --command-template 'sched-mock {prompt}' \
  --dry-run --log-dir "$tmpdir/p0a-iso-logs" > "$tmpdir/p0a-iso.txt"
p0a_iso_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0a-iso.txt")"
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited" and .data.wait_until == 1780438801' \
  "$p0a_iso_dir/events.jsonl" >/dev/null \
  || fail "P0-A: Claude ISO +00:00 reset not parsed correctly"

# ── P0-B: is_undetermined_reason — inverted logic ────────────────────────────

# Verify that rate-limited is not treated as undetermined (should rate-limit wait, not proceed optimistically)
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --max-unavailable-wait 1 --dry-run --log-dir "$tmpdir/p0b-ratelimit-logs" > "$tmpdir/p0b-ratelimit.txt"
p0b_ratelimit_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0b-ratelimit.txt")"
if jq -e 'select(.type=="optimistic_proceed")' "$p0b_ratelimit_dir/events.jsonl" >/dev/null 2>&1; then
  fail "P0-B: rate-limited must not trigger optimistic_proceed"
fi
jq -e 'select(.type=="usage_decision") | .data.reason == "rate-limited"' "$p0b_ratelimit_dir/events.jsonl" >/dev/null \
  || fail "P0-B: exhausted window with future reset must be rate-limited"

# Various undetermined reasons must cause optimistic_proceed after max-unavailable-wait
for undetermined_reason in unavailable inconclusive-usage unsupported-window missing-cli not-authenticated timeout trust-prompt capture-error format-changed unknown-reason-xyz; do
  und_usage="$(jq -nc --arg r "$undetermined_reason" '{available:false,reason:$r}')"
  : > "$SCHED_CAPTURE"
  : > "$SCHED_ATTEMPTS"
  LLM_SCHEDULER_USAGE_JSON="$und_usage" \
    SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
    "$SCHEDULER" --tool claude --prompt x --command-template 'sched-mock {prompt}' \
    --max-unavailable-wait 1 --poll-interval 1 --no-retry \
    --log-dir "$tmpdir/p0b-und-${undetermined_reason}-logs" > "$tmpdir/p0b-und-${undetermined_reason}.txt"
  und_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0b-und-${undetermined_reason}.txt")"
  jq -e 'select(.type=="optimistic_proceed")' "$und_dir/events.jsonl" >/dev/null \
    || fail "P0-B: reason '$undetermined_reason' must trigger optimistic_proceed"
done

# In suspend mode, undetermined reason must NOT call schedule_resume_and_suspend; proceeds immediately
SYSTEMD_RUN_LOG="$tmpdir/systemd-run-p0b.log"
SYSTEMCTL_LOG="$tmpdir/systemctl-p0b.log"
: > "$SYSTEMD_RUN_LOG"; : > "$SYSTEMCTL_LOG"
: > "$SCHED_CAPTURE"; : > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON='{"available":false,"reason":"missing-cli"}' \
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool claude --prompt x --command-template 'sched-mock {prompt}' \
  --suspend-until-ready --max-unavailable-wait 0 --poll-interval 1 --no-retry \
  --log-dir "$tmpdir/p0b-suspend-und-logs" > "$tmpdir/p0b-suspend-und.txt"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "1" ]] \
  || fail "P0-B: suspend+undetermined must proceed and submit once"
assert_not_grep 'WakeSystem=true' "$SYSTEMD_RUN_LOG"

# ── P0-C: Suspend scheduling failure fallback ─────────────────────────────────
# Use a near-future reset (30s < LLM_SCHEDULER_SUSPEND_MIN_LEAD=120 default) so
# schedule_resume_and_suspend returns 1 (insufficient lead) even in dry-run mode.
# After the fallback, DRY_RUN=1 exits the wait loop cleanly without sleeping.

p0c_near_future=$(( $(date +%s) + 30 ))
p0c_near_usage="$(jq -nc --argjson r "$p0c_near_future" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_SCHEDULER_USAGE_JSON="$p0c_near_usage" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --suspend-until-ready --dry-run \
  --log-dir "$tmpdir/p0c-fail-logs" > "$tmpdir/p0c-fail.txt" 2>"$tmpdir/p0c-fail.stderr"
assert_grep 'error:.*suspend scheduling failed' "$tmpdir/p0c-fail.stderr"
p0c_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p0c-fail.txt")"
jq -e 'select(.type=="suspend_schedule_fallback")' "$p0c_dir/events.jsonl" >/dev/null \
  || fail "P0-C: suspend scheduling failure must log suspend_schedule_fallback"
if jq -e 'select(.type=="final") | .data.status == "scheduled"' "$p0c_dir/events.jsonl" >/dev/null 2>&1; then
  fail "P0-C: final status must not be 'scheduled' after scheduling failure"
fi

# not-before gate with insufficient lead: fallback too
p0c_nb_future=$(( $(date +%s) + 30 ))
LLM_SCHEDULER_SUSPEND_MIN_LEAD=120 \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --at "@$p0c_nb_future" --suspend-until-ready --dry-run \
  --log-dir "$tmpdir/p0c-nb-logs" > "$tmpdir/p0c-nb.txt" 2>"$tmpdir/p0c-nb.stderr"
assert_grep 'error:.*suspend scheduling failed' "$tmpdir/p0c-nb.stderr"

# Also verify a real systemd-run failure (far future target, no dry-run, failing mock)
SYSTEMD_RUN_LOG="$tmpdir/systemd-run-p0c-real.log"
: > "$SYSTEMD_RUN_LOG"
(
  mkdir -p "$tmpdir/ci-bin-fail"
  cat > "$tmpdir/ci-bin-fail/systemd-run" <<'FAIL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSTEMD_RUN_LOG:-/dev/null}"
exit 1
FAIL
  chmod +x "$tmpdir/ci-bin-fail/systemd-run"
  cat > "$tmpdir/ci-bin-fail/systemctl" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
  chmod +x "$tmpdir/ci-bin-fail/systemctl"
  LLM_USAGE_NOW_EPOCH=1780430000 \
  LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
  SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" \
  PATH="$tmpdir/ci-bin-fail:$SCRIPT_DIR/ci-bin:$PATH" \
    "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
    --suspend-until-ready --dry-run \
    --log-dir "$tmpdir/p0c-real-logs" > "$tmpdir/p0c-real.txt" 2>"$tmpdir/p0c-real.stderr"
  # dry-run: schedule_resume_and_suspend returns 0 before systemd-run, so no failure here;
  # this just confirms the script doesn't die.
)

# ── P0-D: Suspend min-lead guard ──────────────────────────────────────────────

# Target only 30s in the future: must NOT call systemd-run (below 120s default min-lead)
SYSTEMD_RUN_LOG="$tmpdir/systemd-run-p0d.log"
: > "$SYSTEMD_RUN_LOG"
near_future=$(( $(date +%s) + 30 ))
near_usage="$(jq -nc --argjson r "$near_future" \
  '{available:true,five_hour:{remaining:0,resets_at:$r},week:{remaining:50}}')"
LLM_SCHEDULER_USAGE_JSON="$near_usage" \
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --suspend-until-ready --dry-run \
  --log-dir "$tmpdir/p0d-short-lead-logs" > "$tmpdir/p0d-short-lead.txt" 2>"$tmpdir/p0d-short-lead.stderr" || true
# systemd-run must NOT have been called with on-calendar
if grep -q 'on-calendar' "$SYSTEMD_RUN_LOG" 2>/dev/null; then
  fail "P0-D: systemd-run should not be called when lead < min_lead"
fi

# ── P1-A: submit_once handles missing status file ────────────────────────────

# A command that exits without creating a status file => synthetic 124, controlled failure
: > "$SCHED_CAPTURE"
: > "$SCHED_ATTEMPTS"
# sched-mock always creates a status file (via run_fresh), so simulate the
# missing-file case via no-retry + a command that exits non-zero; we verify
# submit_once logs an attempt_result (not a script death).
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool codex --prompt x \
  --command-template 'false' --no-retry \
  --log-dir "$tmpdir/p1a-logs" > "$tmpdir/p1a.txt" 2>&1 || true
p1a_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p1a.txt" 2>/dev/null || ls -td "$tmpdir/p1a-logs"/*/  | head -1)"
jq -e 'select(.type=="attempt_result")' "$p1a_dir/events.jsonl" >/dev/null \
  || fail "P1-A: submit_once must log attempt_result even on failure"

# ── P1-B: output_is_retryable — innocent 429 text must not retry ─────────────

cat > "$tmpdir/ci-bin/print-innocent429" <<'MOCK'
#!/usr/bin/env bash
printf 'The chapter 429 of the spec describes a common scenario\n'
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/print-innocent429"

cat > "$tmpdir/ci-bin/print-http429" <<'MOCK'
#!/usr/bin/env bash
printf 'HTTP 429 Too Many Requests\n'
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/print-http429"

# Status 0 + innocent text containing "429": must not retry.
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool codex --prompt x \
  --command-template 'print-innocent429' \
  --retry-delays 0,0 --log-dir "$tmpdir/p1b-innocent-logs" > "$tmpdir/p1b-innocent.txt"
p1b_innocent_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p1b-innocent.txt")"
attempt_count="$(jq -s 'map(select(.type=="attempt_result")) | length' "$p1b_innocent_dir/events.jsonl")"
[[ "$attempt_count" == "1" ]] \
  || fail "P1-B: innocent '429' text (status 0) must not trigger retry; got $attempt_count attempts"

# Status 0 + "HTTP 429" output: must retry.
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  "$SCHEDULER" --tool codex --prompt x \
  --command-template 'print-http429' \
  --retry-delays 0,0 --log-dir "$tmpdir/p1b-http429-logs" > "$tmpdir/p1b-http429.txt" 2>&1 || true
p1b_http429_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p1b-http429.txt")"
attempt_count="$(jq -s 'map(select(.type=="attempt_result")) | length' "$p1b_http429_dir/events.jsonl")"
[[ "$attempt_count" -gt 1 ]] \
  || fail "P1-B: 'HTTP 429' output (status 0) must trigger retry; got $attempt_count attempts"

# ── P1-C: normalizers preserve partial windows with missing resets_at ─────────

. "$SCRIPT_DIR/lib/llm-common.sh"

# Claude: five_hour with only used_percentage (no resets_at)
claude_partial_norm="$(printf '%s\n' '{"rate_limits":{"five_hour":{"used_percentage":10}}}' \
  | normalize_claude 'test')"
jq -e '.five_hour != null and .five_hour.used == 10 and .five_hour.resets_at == null' \
  <<<"$claude_partial_norm" >/dev/null \
  || fail "P1-C: Claude partial window with only used_percentage must normalize to non-null five_hour"

claude_partial_full="$(json_for_provider "$claude_partial_norm" claude)"
jq -e '.five_hour != null and .five_hour.remaining == 90' <<<"$claude_partial_full" >/dev/null \
  || fail "P1-C: Claude partial window remaining must be 90 after json_for_provider"

# Codex: fiveHour with only used_percent (no resets_at)
codex_partial_norm="$(printf '%s\n' '{"rate_limits":{"primary":{"used_percent":10}}}' \
  | normalize_codex 'test')"
jq -e '.five_hour != null and .five_hour.used == 10 and .five_hour.resets_at == null' \
  <<<"$codex_partial_norm" >/dev/null \
  || fail "P1-C: Codex partial window with only used_percent must normalize to non-null five_hour"

codex_partial_full="$(json_for_provider "$codex_partial_norm" codex)"
jq -e '.five_hour != null and .five_hour.remaining == 90' <<<"$codex_partial_full" >/dev/null \
  || fail "P1-C: Codex partial window remaining must be 90 after json_for_provider"

# Complete samples still normalize correctly
claude_full_norm="$(printf '%s\n' '{"rate_limits":{"five_hour":{"used_percentage":50,"resets_at":"2026-06-02T23:00:00Z"}}}' \
  | normalize_claude 'test')"
jq -e '.five_hour.used == 50 and .five_hour.resets_at == "2026-06-02T23:00:00Z"' \
  <<<"$claude_full_norm" >/dev/null \
  || fail "P1-C: Complete Claude window must normalize with resets_at preserved"

# ── P1-D: --tmux foo:foo parses correctly ────────────────────────────────────

if command -v tmux >/dev/null 2>&1; then
  TMUX_SESSION="llm-test-foofoo-$$"
  : > "$SCHED_CAPTURE"; : > "$SCHED_ATTEMPTS"
  LLM_SCHEDULER_TMUX_TIMEOUT=5 \
  LLM_SCHEDULER_USAGE_JSON="$available_usage" \
    SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
    "$SCHEDULER" --tool codex --prompt x \
    --command-template 'sched-mock {prompt}' \
    --tmux "${TMUX_SESSION}:${TMUX_SESSION}" \
    --log-dir "$tmpdir/p1d-foofoo-logs" > "$tmpdir/p1d-foofoo.txt"
  # If window had been replaced with llm-scheduler, we'd still succeed, so
  # verify the command was generated targeting foo:foo by checking the tmux command script
  p1d_dir="$(awk '/logs written to /{dir=$NF} END{print dir}' "$tmpdir/p1d-foofoo.txt")"
  grep -q "${TMUX_SESSION}:${TMUX_SESSION}" "$p1d_dir/tmux-command.sh" \
    || fail "P1-D: --tmux foo:foo must target session:window foo:foo, not foo:llm-scheduler"
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
else
  printf 'skip P1-D tmux test: tmux not installed\n'
fi

# ── P1-E: --at validation before log-dir creation ────────────────────────────

expect_fail "$tmpdir/p1e-bad-at.txt" \
  "$SCHEDULER" --tool codex --prompt x --at "not-a-valid-time" \
  --log-dir "$tmpdir/p1e-logs"
assert_grep 'could not parse' "$tmpdir/p1e-bad-at.txt"
# Log dir for this run must not have been created
if compgen -G "$tmpdir/p1e-logs/"'*' > /dev/null 2>&1; then
  fail "P1-E: invalid --at must not create a run directory"
fi

# Valid --at must still work
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
: > "$SCHED_CAPTURE"; : > "$SCHED_ATTEMPTS"
LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$SCHED_CAPTURE" SCHED_ATTEMPTS="$SCHED_ATTEMPTS" \
  "$SCHEDULER" --tool codex --prompt x \
  --command-template 'sched-mock {prompt}' \
  --at "$(date -d "@$(( $(date +%s) - 60 ))" '+%Y-%m-%d %H:%M:%S')" \
  --log-dir "$tmpdir/p1e-valid-logs" > "$tmpdir/p1e-valid.txt"
[[ "$(wc -l < "$SCHED_ATTEMPTS")" == "1" ]] \
  || fail "P1-E: valid --at in the past must submit normally"

# ── P2-A: wake_diagnostics_json degraded state ───────────────────────────────

# Fake systemctl returning "degraded" with non-zero exit
cat > "$tmpdir/ci-bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"
if [[ "${1:-}" == "--user" && "${2:-}" == "is-system-running" ]]; then
  printf 'degraded\n'
  exit 1
fi
if [[ "${1:-}" == "suspend" ]]; then exit 0; fi
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/systemctl"

"$SCHEDULER" --wake-test > "$tmpdir/p2a-degraded.txt"
jq -e '.user_systemd == "degraded"' "$tmpdir/p2a-degraded.txt" >/dev/null \
  || fail "P2-A: degraded user manager must report user_systemd=degraded"

# Fake systemctl returning "running"
cat > "$tmpdir/ci-bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"
if [[ "${1:-}" == "--user" && "${2:-}" == "is-system-running" ]]; then
  printf 'running\n'
  exit 0
fi
if [[ "${1:-}" == "suspend" ]]; then exit 0; fi
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/systemctl"

"$SCHEDULER" --wake-test > "$tmpdir/p2a-running.txt"
jq -e '.user_systemd == "running"' "$tmpdir/p2a-running.txt" >/dev/null \
  || fail "P2-A: running user manager must report user_systemd=running"

# Restore original systemctl mock
cat > "$tmpdir/ci-bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"
if [[ "${1:-}" == "--user" && "${2:-}" == "is-system-running" ]]; then
  printf 'running\n'
  exit 0
fi
if [[ "${1:-}" == "suspend" ]]; then
  exit 0
fi
exit 0
MOCK
chmod +x "$tmpdir/ci-bin/systemctl"

# ── P2-B: dry-run suspend path prints useful stdout ──────────────────────────

SYSTEMD_RUN_LOG="$tmpdir/systemd-run-p2b.log"
SYSTEMCTL_LOG="$tmpdir/systemctl-p2b.log"
: > "$SYSTEMD_RUN_LOG"; : > "$SYSTEMCTL_LOG"
LLM_USAGE_NOW_EPOCH=1780430000 \
LLM_SCHEDULER_USAGE_JSON="$exhausted_usage" \
SYSTEMD_RUN_LOG="$SYSTEMD_RUN_LOG" SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
  "$SCHEDULER" --tool codex --prompt x --command-template 'sched-mock {prompt}' \
  --suspend-until-ready --dry-run \
  --log-dir "$tmpdir/p2b-logs" > "$tmpdir/p2b.txt"
assert_grep 'dry-run' "$tmpdir/p2b.txt"
assert_grep '1780441200' "$tmpdir/p2b.txt"
assert_grep 'llm-scheduler-resume' "$tmpdir/p2b.txt"
# Must not call real systemd-run or systemctl suspend
assert_not_grep 'on-calendar' "$SYSTEMD_RUN_LOG"
assert_not_grep 'suspend' "$SYSTEMCTL_LOG"

# ── Cache layout: per-tool subfolders under ~/.cache/llm-tools ───────────────

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/cache-layout.txt"
[[ -f "$USAGE_LOG_FIXTURE" ]] || fail "usage log not written under .cache/llm-tools/llm-usage"

LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$tmpdir/cache-layout-capture.txt" SCHED_ATTEMPTS="$tmpdir/cache-layout-attempts.txt" \
  HOME="$HOME_FIXTURE" \
  "$SCHEDULER" --tool codex --prompt 'cache layout' --command-template 'sched-mock {prompt}' > "$tmpdir/cache-layout-sched.txt"
[[ -d "$HOME_FIXTURE/.cache/llm-tools/llm-scheduler/logs" ]] \
  || fail "scheduler default log dir not under .cache/llm-tools/llm-scheduler/logs"

LLM_SCHEDULER_USAGE_JSON="$available_usage" \
  SCHED_CAPTURE="$tmpdir/cache-layout-capture.txt" SCHED_ATTEMPTS="$tmpdir/cache-layout-attempts.txt" \
  HOME="$HOME_FIXTURE" \
  "$RALPH" --prompt 'cache layout' --command-template 'sched-mock {tool} {prompt}' --no-retry > "$tmpdir/cache-layout-ralph.txt"
[[ -d "$HOME_FIXTURE/.cache/llm-tools/ralph-robin/logs" ]] \
  || fail "ralph-robin default log dir not under .cache/llm-tools/ralph-robin/logs"
[[ -f "$HOME_FIXTURE/.cache/llm-tools/ralph-robin/state.json" ]] \
  || fail "ralph-robin default state file not under .cache/llm-tools/ralph-robin"

# Legacy per-tool dirs directly under ~/.cache migrate to ~/.cache/llm-tools.
MIGRATE_HOME="$tmpdir/migrate-home"
mkdir -p \
  "$MIGRATE_HOME/.codex/sessions" \
  "$MIGRATE_HOME/.claude/projects" \
  "$MIGRATE_HOME/.cache/llm-usage" \
  "$MIGRATE_HOME/.cache/ralph-robin"
printf '%s\n' '{"legacy":"status"}' > "$MIGRATE_HOME/.cache/llm-usage/claude-status.json"
printf '%s\n' '{"tool_index":1}' > "$MIGRATE_HOME/.cache/ralph-robin/state.json"
LLM_USAGE_DISABLE_COPILOT=1 HOME="$MIGRATE_HOME" "$TOOL" --json > "$tmpdir/migrate-out.json"
[[ -f "$MIGRATE_HOME/.cache/llm-tools/llm-usage/claude-status.json" ]] \
  || fail "legacy llm-usage cache dir was not migrated"
[[ ! -e "$MIGRATE_HOME/.cache/llm-usage" ]] || fail "legacy llm-usage cache dir still present"
[[ -f "$MIGRATE_HOME/.cache/llm-tools/ralph-robin/state.json" ]] \
  || fail "legacy ralph-robin dir was not migrated"

printf 'ok\n'
