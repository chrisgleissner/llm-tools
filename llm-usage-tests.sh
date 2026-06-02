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
{"rate_limits":{"primary":{"used_percent":53,"window_minutes":300,"resets_at":"2026-06-02T13:49:00Z"},"secondary":{"used_percent":59,"window_minutes":10080,"resets_at":"2026-06-07T16:25:00Z"}}}
JSON

cat > "$HOME_FIXTURE/.claude/projects/proj.jsonl" <<'JSON'
{"rate_limits":{"five_hour":{"used_percentage":0,"resets_at":"2026-06-02T13:20:00Z"},"seven_day":{"used_percentage":25,"resets_at":"2026-06-04T13:00:00Z"}}}
JSON

run_tool() {
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

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$fixture_zero"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+95%[[:space:]]+5(\.0)?%[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_zero"
assert_grep '^Copilot[[:space:]]+ai-credits[[:space:]]+unknown[[:space:]]+0[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_zero"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 42% used · AI Credits: 17' \
  run_tool "$fixture_nonzero"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+58%[[:space:]]+42(\.0)?%[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_nonzero"
assert_grep '^Copilot[[:space:]]+ai-credits[[:space:]]+unknown[[:space:]]+17[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_nonzero"

LLM_USAGE_COPILOT_CAPTURE_TEXT='No footer here' \
  run_tool "$fixture_missing"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+unavailable[[:space:]]+unavailable[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_missing"
assert_grep '^Copilot[[:space:]]+ai-credits[[:space:]]+unavailable[[:space:]]+unavailable[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_missing"

LLM_USAGE_COPILOT_CAPTURE_CMD='sleep 99' \
LLM_USAGE_COPILOT_TIMEOUT=1 \
  run_tool "$fixture_timeout"
assert_grep '^Codex[[:space:]]+5h' "$fixture_timeout"
assert_grep '^Claude[[:space:]]+5h' "$fixture_timeout"
assert_grep '^Copilot[[:space:]]+monthly[[:space:]]+unavailable[[:space:]]+unavailable[[:space:]]+-[[:space:]]+copilot cli$' "$fixture_timeout"

LLM_USAGE_COPILOT_CAPTURE_TEXT='Monthly: 5% used · AI Credits: 0' \
  run_tool "$json_zero" --json
jq -e '.copilot.monthly.remaining == 95 and .copilot.monthly.used == 5 and .copilot.ai_credits.used == 0' "$json_zero" >/dev/null \
  || fail "unexpected Copilot JSON for zero-credits fixture"

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

jq -S '{codex,claude}' "$json_baseline" > "$tmpdir/baseline-cq.json"
jq -S '{codex,claude}' "$json_with_copilot" > "$tmpdir/with-copilot-cq.json"
cmp -s "$tmpdir/baseline-cq.json" "$tmpdir/with-copilot-cq.json" \
  || fail "Codex/Claude JSON changed when Copilot rows were added"

printf 'ok\n'
