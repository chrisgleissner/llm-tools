#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/llm-usage"
PATH="$SCRIPT_DIR/ci-bin:$PATH"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_grep() {
  local pattern="$1"
  local file="$2"
  grep -Eq "$pattern" "$file" || fail "pattern not found: $pattern"
}

assert_not_grep() {
  local pattern="$1"
  local file="$2"
  if grep -Eq "$pattern" "$file"; then
    fail "unexpected match: $pattern"
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

cat > "$tmpdir/ci-bin/copilot" <<'COP'
#!/usr/bin/env bash
sleep 99
COP
chmod +x "$tmpdir/ci-bin/copilot"

cat > "$HOME_FIXTURE/.codex/sessions/session-20260602.jsonl" <<'JSON'
{"rate_limits":{"primary":{"used_percent":53,"window_minutes":300,"resets_at":"2026-06-02T13:49:00Z"},"secondary":{"used_percent":59,"window_minutes":10080,"resets_at":"2026-06-07T16:25:00Z"},"spark":{"primary":{"used_percent":99,"resets_at":"2026-06-02T22:26:00Z"},"secondary":{"used_percent":96,"resets_at":"2026-06-08T17:49:00Z"}}}}
JSON

cat > "$HOME_FIXTURE/.claude/projects/proj.jsonl" <<'JSON'
{"rate_limits":{"five_hour":{"used_percentage":0,"resets_at":"2026-06-02T13:20:00Z"},"seven_day":{"used_percentage":25,"resets_at":"2026-06-04T13:00:00Z"}}}
JSON

run_tool() {
  local output_file=$1
  shift
  rm -f "$(dirname "$TOOL")/llm-usage.log"
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
assert_grep '^GPT-5\.3-Codex-Spark[[:space:]]+5h[[:space:]]+1%[[:space:]]+' "$tmpdir/codex-spark.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/codex-spark-hidden.txt" --hide-codex-spark
assert_not_grep '^GPT-5\.3-Codex-Spark[[:space:]]+5h' "$tmpdir/codex-spark-hidden.txt"
assert_grep '^Codex[[:space:]]+5h[[:space:]]+47%[[:space:]]+' "$tmpdir/codex-spark-hidden.txt"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$tmpdir/no-remaining-time.txt" --show-source --hide-remaining-time
assert_not_grep '^Tool[[:space:]]+Window[[:space:]]+Remaining[[:space:]]+Remaining[[:space:]]+Time$' "$tmpdir/no-remaining-time.txt"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]]+[0-9]{2}:[0-9]{2}[[:space:]]+[0-9].*copilot cli$' "$tmpdir/no-remaining-time.txt"

printf '%s\n' '{"ts":1750000000,"provider":"copilot","window":"monthly","remaining":100}' > "$(dirname "$TOOL")/llm-usage.log"
LLM_USAGE_NOW_EPOCH=1750003600 \
  LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 50% used · AI Credits: 0' \
  run_tool_keep_log "$tmpdir/remaining-time-estimate.txt" --show-source
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+50%[[:space:]]+1h[[:space:]]+2025-07-01[[:space:]]+00:00[[:space:]]+[0-9]' "$tmpdir/remaining-time-estimate.txt"

LLM_USAGE_NOW_EPOCH=1750000000 \
  LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS=2 \
  run_tool "$tmpdir/copilot-reset-offset.txt" --show-source
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+-[[:space:]]+2025-07-03[[:space:]]+00:00[[:space:]]+[0-9]' "$tmpdir/copilot-reset-offset.txt"

printf 'ok\n'
