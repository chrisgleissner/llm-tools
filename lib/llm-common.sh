#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Shared helpers for llm-usage, llm-scheduler, and ralph-robin: provider readers,
# normalization, time/reset formatting, and common CLI plumbing (validation,
# run-dir logging, argv/JSON helpers).
# This file expects the caller to run under bash strict mode and to set app-specific paths if defaults are not suitable.

: "${LLM_COMMON_APP_NAME:=llm-common}"
: "${LLM_TOOLS_CACHE_DIR:=${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools}"
: "${CACHE_DIR:=$LLM_TOOLS_CACHE_DIR/llm-usage}"
: "${CLAUDE_CACHE:=$CACHE_DIR/claude-status.json}"
: "${CLAUDE_API_CACHE:=$CACHE_DIR/claude-usage-api.json}"
: "${COPILOT_RESULT_CACHE:=$CACHE_DIR/copilot-usage.json}"
: "${COPILOT_REFRESH_LOCK:=$CACHE_DIR/copilot-refresh.lock}"
: "${USAGE_LOG_FILE:=$CACHE_DIR/llm-usage.log}"
: "${USAGE_LOG_TAIL_LINES:=${LLM_USAGE_LOG_TAIL_LINES:-20000}}"
: "${COPILOT_MONTHLY_RESET_OFFSET_DAYS:=${LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS:-0}}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: required command not found: $1" >&2
    exit 127
  }
}

# One-time migration from the pre-llm-tools cache layout, where each tool kept
# its own directory directly under ~/.cache. A legacy directory is moved only
# while the new per-tool directory does not exist yet.
migrate_legacy_cache_dirs() {
  local legacy_root="${XDG_CACHE_HOME:-$HOME/.cache}"
  local name
  for name in llm-usage llm-scheduler ralph-robin; do
    if [[ -d "$legacy_root/$name" && ! -e "$LLM_TOOLS_CACHE_DIR/$name" ]]; then
      mkdir -p "$LLM_TOOLS_CACHE_DIR" 2>/dev/null || return 0
      mv "$legacy_root/$name" "$LLM_TOOLS_CACHE_DIR/$name" 2>/dev/null || true
    fi
  done
}
migrate_legacy_cache_dirs

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}


# Format reset timestamps. Handles epoch seconds and ISO timestamps. Empty/null -> empty.
fmt_reset() {
  local ts="${1:-}"
  [[ -n "$ts" && "$ts" != "null" ]] || return 0
  if [[ "$ts" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    ts="${ts%%.*}"
    date -d "@$ts" '+%Y-%m-%d %H:%M' 2>/dev/null || true
  else
    date -d "$ts" '+%Y-%m-%d %H:%M' 2>/dev/null || true
  fi
}

now_epoch() {
  if [[ -n "${LLM_USAGE_NOW_EPOCH:-}" ]]; then
    printf '%s\n' "$LLM_USAGE_NOW_EPOCH"
    return
  fi
  date +%s
}

parse_epoch() {
  local ts="${1:-}"
  [[ -n "$ts" && "$ts" != "null" ]] || { printf '\n'; return 1; }
  if [[ "$ts" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    printf '%s\n' "${ts%%.*}"
    return 0
  fi
  date -d "$ts" +%s 2>/dev/null || return 1
}

copilot_monthly_reset_epoch() {
  local now month_start next_reset this_reset
  now="$(now_epoch)"
  month_start="$(date -d "@$now" '+%Y-%m-01 00:00:00')"
  this_reset="$(date -d "$month_start + ${COPILOT_MONTHLY_RESET_OFFSET_DAYS} days" +%s 2>/dev/null || true)"
  [[ -n "$this_reset" ]] || { printf '-'; return; }
  if (( this_reset <= now )); then
    next_reset="$(date -d "$month_start + 1 month + ${COPILOT_MONTHLY_RESET_OFFSET_DAYS} days" +%s 2>/dev/null || true)"
    [[ -n "$next_reset" ]] || { printf '%s\n' "$this_reset"; return; }
    printf '%s\n' "$next_reset"
    return
  fi
  printf '%s\n' "$this_reset"
}

fmt_duration() {
  local seconds="${1:-}"
  [[ -n "$seconds" && "$seconds" != "-" ]] || { printf '-'; return; }
  if ! [[ "$seconds" =~ ^[0-9]+$ ]]; then
    printf '-'
    return
  fi

  if (( seconds <= 0 )); then
    printf '0m'
    return
  fi

  local days hours mins remainder
  days=$(( seconds / 86400 ))
  remainder=$(( seconds % 86400 ))
  hours=$(( remainder / 3600 ))
  mins=$(( (remainder % 3600) / 60 ))

  local out=()
  [[ "$days" -gt 0 ]] && out+=("${days}d")
  [[ "$hours" -gt 0 || "$days" -gt 0 ]] && out+=("${hours}h")
  [[ "$mins" -gt 0 || "${#out[@]}" -eq 0 ]] && out+=("${mins}m")
  printf '%s' "${out[*]}"
}

time_until() {
  local ts="$1"
  local now reset_epoch seconds
  now=$(now_epoch)
  if ! reset_epoch=$(parse_epoch "$ts"); then
    printf '-'
    return
  fi
  (( seconds = reset_epoch - now ))
  if (( seconds <= 0 )); then
    printf '0m'
  else
    fmt_duration "$seconds"
  fi
}

estimate_remaining_time_from_log() {
  local provider="$1"
  local window="$2"
  local remaining="${3:-}"
  [[ "$remaining" == "-" || "$remaining" == "unknown" || -z "$remaining" ]] && { printf '-'; return; }
  [[ -s "$USAGE_LOG_FILE" ]] || { printf '-'; return; }

  local now cutoff prev_ts now_tenths
  local stale_mult
  local first_decrease_ts=""
  local last_decrease_ts=""
  local trend_start_ts=""
  local decay_window=0
  local stale_seconds=0
  local stale_threshold=0
  local max_stale_seconds
  local dt prev_rem_tenths rem_tenths
  prev_ts=""
  prev_rem_tenths=0
  local total_reduction_tenths=0
  local total_seconds=0
  stale_mult="${LLM_USAGE_REMAINING_TIME_STALE_MULTIPLIER:-3}"
  max_stale_seconds="${LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS:-120}"
  now=$(now_epoch)
  cutoff=$(( now - 604800 ))

  # jq emits remaining pre-scaled to integer tenths so the loop needs no
  # per-line process spawns; this keeps long logs cheap to scan.
  while IFS=$'\t' read -r ts rem_tenths; do
    [[ -z "$ts" || -z "$rem_tenths" ]] && continue

    if [[ -n "$prev_ts" ]]; then
      dt=$(( ts - prev_ts ))
      if (( dt > 0 )); then
        if [[ -z "$trend_start_ts" ]]; then
          trend_start_ts="$prev_ts"
        fi
        if (( rem_tenths < prev_rem_tenths )); then
          total_reduction_tenths=$(( total_reduction_tenths + (prev_rem_tenths - rem_tenths) ))
          total_seconds=$(( total_seconds + dt ))
          if [[ -z "$first_decrease_ts" ]]; then
            first_decrease_ts="$trend_start_ts"
          fi
          last_decrease_ts="$ts"
        elif (( rem_tenths > prev_rem_tenths )); then
          # usage window reset or quota refill; discard older trend history.
          trend_start_ts="$ts"
          first_decrease_ts=""
          last_decrease_ts=""
          total_reduction_tenths=0
          total_seconds=0
        else
          total_seconds=$(( total_seconds + dt ))
        fi
      fi
    fi

    prev_ts="$ts"
    prev_rem_tenths="$rem_tenths"
  done < <(tail -n "$USAGE_LOG_TAIL_LINES" "$USAGE_LOG_FILE" 2>/dev/null \
    | jq -R -r --arg provider "$provider" --arg window "$window" --argjson cutoff "$cutoff" '
      select(length > 0)
      | (fromjson? // empty) as $o
      | select(($o | type) == "object")
      | select($o.provider == $provider and $o.window == $window and $o.remaining != null and $o.ts != null)
      | select((($o.ts | tonumber) >= $cutoff))
      | "\($o.ts)\t\((($o.remaining | tonumber? // 0) * 10 + 0.5) | floor)"')

  now_tenths=$(awk -v v="$remaining" 'BEGIN {printf "%d", (v*10 + 0.5)}')
  if (( total_seconds <= 0 || total_reduction_tenths <= 0 || now_tenths <= 0 )); then
    printf '-'
    return
  fi

  if [[ -n "$first_decrease_ts" && -n "$last_decrease_ts" ]]; then
    stale_seconds=$(( now - last_decrease_ts ))
    if (( max_stale_seconds > 0 && stale_seconds > max_stale_seconds )); then
      printf '-'
      return
    fi

    decay_window=$(( last_decrease_ts - first_decrease_ts ))
    stale_threshold=$(( decay_window * stale_mult ))
    if (( max_stale_seconds > 0 && stale_threshold > max_stale_seconds )); then
      stale_threshold=$max_stale_seconds
    fi
    if (( decay_window > 0 && stale_seconds > stale_threshold )); then
      printf '-'
      return
    fi
  fi

  # remaining_hours = now / (reduction_per_second) and reduction_per_second = reduction_tenths / total_seconds / 10
  # tenths and total_seconds cancel -> remaining_seconds = (now_tenths * total_seconds) / reduction_tenths
  local remaining_seconds
  remaining_seconds=$(awk -v rem="$now_tenths" -v reduction="$total_reduction_tenths" -v elapsed="$total_seconds" \
    'BEGIN { printf "%d", (rem * elapsed) / reduction }')
  if (( remaining_seconds <= 0 )); then
    printf '-'
  elif (( remaining_seconds < 60 )); then
    printf '1m'
  else
    fmt_duration "$remaining_seconds"
  fi
}

log_usage_sample() {
  local provider="$1"
  local window="$2"
  local remaining="$3"
  local now_ts
  [[ -n "$remaining" && "$remaining" != "-" && "$remaining" != "unknown" ]] || return
  now_ts="$(now_epoch)"
  jq -nc --arg provider "$provider" --arg window "$window" --argjson remaining "$remaining" --argjson ts "$now_ts" \
    '{ts: $ts, provider: $provider, window: $window, remaining: $remaining}' >> "$USAGE_LOG_FILE"
}


# Convert a used percentage into remaining percentage with one decimal where needed.
remaining_from_used() {
  local used="${1:-}"
  [[ -n "$used" && "$used" != "null" ]] || return 0
  jq -nr --argjson u "$used" '([0, (100 - $u), 100] | sort | .[1]) | if . == floor then tostring else (.*10|round/10|tostring) end' 2>/dev/null || true
}

# Print percent with at most one decimal.
fmt_pct() {
  local p="${1:-}"
  [[ -n "$p" && "$p" != "null" ]] || { printf '-'; return; }
  jq -nr --argjson p "$p" 'if $p == floor then ($p|tostring) else ($p*10|round/10|tostring) end' 2>/dev/null || printf '%s' "$p"
}

# Emit the most recent line from JSONL files matching a jq predicate.
latest_matching_line() {
  local root="$1"
  local predicate="$2"
  [[ -d "$root" ]] || return 1

  # Newest files first, bounded to avoid crawling huge histories on every run.
  # Tune MAX_FILES if your local history is very large and relevant data is older.
  local max_files="${LLM_USAGE_MAX_FILES:-250}"
  local file
  while IFS= read -r file; do
    [[ -r "$file" ]] || continue
    # Search from the end of each file. tail keeps the command cheap for long sessions.
    local line
    line=$(tail -n "${LLM_USAGE_TAIL_LINES:-2000}" "$file" \
      | jq -c "select($predicate)" 2>/dev/null \
      | tail -n 1 || true)
    if [[ -n "$line" ]]; then
      printf '%s\n' "$line"
      return 0
    fi
  done < <(find "$root" -type f \( -name '*.jsonl' -o -name 'rollout-*.jsonl' \) -printf '%T@ %p\n' 2>/dev/null \
            | sort -nr \
            | head -n "$max_files" \
            | cut -d' ' -f2-)

  return 1
}

# Normalize Codex rate-limit object into:
#   - rows: an array of named window snapshots (for Codex and optional Codex Spark)
#   - source note for provenance.
# Handles observed snake_case/camelCase variants from local rollout JSONL and app-server payloads.
normalize_codex() {
  jq -c --arg source "$1" '
    def num: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
    def pct($x): ($x.used_percent // $x.usedPercent // null) | if . == null then null else num end;
    def reset($x): ($x.resets_at // $x.resetsAt // null);
    def mins($x): ($x.window_minutes // $x.windowDurationMins // null) | if . == null then null else num end;
    def as_window($x; $default_minutes):
      if ($x|type) == "object" then
        {used: pct($x), resets_at: reset($x), window_minutes: (mins($x) // $default_minutes)}
      else
        null
      end;
    def as_row($name; $key; $obj):
      if ($obj|type) != "object" then
        null
      else
        ($obj.primary // $obj.five_hour // $obj.fiveHour // $obj.primary_window // null) as $p
        | ($obj.secondary // $obj.week // $obj.weekly // $obj.seven_day // $obj.sevenDay // $obj.secondary_window // null) as $s
        | if (($p|type) != "object" and ($s|type) != "object") then
            null
          else
            {
              key: $key,
              name: $name,
              source: $source,
              five_hour: (if ($p|type)=="object" then as_window($p; 300) else null end),
              week: (if ($s|type)=="object" then as_window($s; 10080) else null end)
            }
          end
      end;
    def collect_spark_rows($obj):
      [($obj | to_entries[] | select(.value|type=="object") | select((.key|ascii_downcase | contains("spark"))))
        | . as $e
        | as_row("GPT-5.3-Codex-Spark"; "codex-spark"; $e.value)]
      | map(select(. != null and . != {}));

    (.rate_limits // .rateLimits // .rateLimits.rateLimits // .msg.rate_limits // .msg.rateLimits // .payload.rate_limits // .payload.rateLimits // null) as $rl
    | if ($rl|type) == "object" then
        ([
          as_row("Codex"; "codex"; $rl),
          as_row(
            "GPT-5.3-Codex-Spark";
            "codex-spark";
            (
              $rl.spark // $rl.codex_spark // $rl.codexSpark // $rl["gpt-5.3-codex-spark"]
              // $rl["GPT-5.3-Codex-Spark"] // $rl["gpt_5_3_codex_spark"] // $rl["gpt53-codex-spark"]
              // $rl.gpt_5_3_codex_spark // null
            )
          )
        ] + collect_spark_rows($rl)) as $rows
        | {
            provider: "codex",
            source: $source,
            plan: ($rl.plan_type // $rl.planType // null),
            rows: ([ $rows[] | .? // empty | select(. != null) ] | unique_by(.key)),
            five_hour: ((([ $rows[] | .? | select(.key == "codex") ] | .[0]) // null).five_hour // null),
            week: ((([ $rows[] | .? | select(.key == "codex") ] | .[0]) // null).week // null)
          }
      else empty end'
}

# Normalize Claude statusline/transcript shape into {five_hour, week, source_note}.
normalize_claude() {
  jq -c --arg source "$1" '
    def num: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
    def pct($x): ($x.used_percentage // $x.usedPercent // $x.used_percent // $x.utilization // null) | if . == null then null else num end;
    def reset($x): ($x.resets_at // $x.resetsAt // null);

    (.rate_limits // .rateLimits // .message.rate_limits // .message.rateLimits // {five_hour: .five_hour, seven_day: .seven_day, seven_day_sonnet: .seven_day_sonnet, extra_usage: .extra_usage}) as $rl
    | if ($rl|type) == "object" then
        ($rl.five_hour // $rl.fiveHour // $rl.primary // null) as $p
        | ($rl.seven_day // $rl.sevenDay // $rl.weekly // $rl.secondary // null) as $s
        | {
            provider: "claude",
            source: $source,
            plan: null,
            five_hour: (if ($p|type)=="object" then {used: pct($p), resets_at: reset($p), window_minutes: 300} else null end),
            week: (if ($s|type)=="object" then {used: pct($s), resets_at: reset($s), window_minutes: 10080} else null end)
          }
      else empty end'
}

read_codex() {
  local line norm
  line=$(latest_matching_line "$HOME/.codex/sessions" '(.rate_limits? // .rateLimits? // .rateLimits.rateLimits? // .msg.rate_limits? // .msg.rateLimits? // .payload.rate_limits? // .payload.rateLimits?) != null' || true)
  [[ -n "$line" ]] || return 1
  # '~/.codex/sessions' is a human-readable provenance label, not a path to expand.
  # shellcheck disable=SC2088
  norm=$(printf '%s\n' "$line" | normalize_codex '~/.codex/sessions')
  [[ -n "$norm" ]] || return 1
  printf '%s\n' "$norm"
}

read_claude_api() {
  local access_token resp norm
  [[ -r "$HOME/.claude/.credentials.json" ]] || return 1
  access_token=$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null || true)
  [[ -n "$access_token" ]] || return 1

  resp=$(curl -fsS --max-time 20 \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $access_token" \
    -H 'anthropic-beta: oauth-2025-04-20' \
    'https://api.anthropic.com/api/oauth/usage' 2>/dev/null || true)
  if [[ -n "$resp" ]]; then
    mkdir -p "${CLAUDE_API_CACHE%/*}" 2>/dev/null || true
    printf '%s\n' "$resp" > "$CLAUDE_API_CACHE" 2>/dev/null || true
    norm=$(printf '%s\n' "$resp" | normalize_claude 'api.anthropic.com/api/oauth/usage')
    [[ -n "$norm" ]] || return 1
    printf '%s\n' "$norm"
    return 0
  fi

  if [[ -s "$CLAUDE_API_CACHE" ]]; then
    # First arg is a provenance label, not an output target; normalize_claude only reads stdin.
    # shellcheck disable=SC2094
    norm=$(normalize_claude "$CLAUDE_API_CACHE" < "$CLAUDE_API_CACHE" || true)
    [[ -n "$norm" ]] || return 1
    printf '%s\n' "$norm"
    return 0
  fi

  return 1
}

read_claude() {
  local norm line
  norm=$(read_claude_api || true)
  if [[ -n "$norm" ]]; then
    printf '%s\n' "$norm"
    return 0
  fi

  if [[ -s "$CLAUDE_CACHE" ]]; then
    # First arg is a provenance label, not an output target; normalize_claude only reads stdin.
    # shellcheck disable=SC2094
    norm=$(normalize_claude "$CLAUDE_CACHE" < "$CLAUDE_CACHE" || true)
    if [[ -n "$norm" ]]; then
      printf '%s\n' "$norm"
      return 0
    fi
  fi

  # Fallback only. Claude's documented, reliable machine-readable source is statusline stdin.
  line=$(latest_matching_line "$HOME/.claude/projects" '(.rate_limits? // .rateLimits? // .message.rate_limits? // .message.rateLimits?) != null' || true)
  [[ -n "$line" ]] || return 1
  # '~/.claude/projects' is a human-readable provenance label, not a path to expand.
  # shellcheck disable=SC2088
  norm=$(printf '%s\n' "$line" | normalize_claude '~/.claude/projects')
  [[ -n "$norm" ]] || return 1
  printf '%s\n' "$norm"
}

find_copilot_cli() {
  if command -v copilot >/dev/null 2>&1; then
    command -v copilot
  elif command -v github-copilot >/dev/null 2>&1; then
    command -v github-copilot
  else
    return 1
  fi
}

COPILOT_CAPTURE_STATUS=""
COPILOT_CAPTURE_OUTPUT=""

capture_copilot_screen() {
  local cli python_bin capture_cwd timeout_seconds output status helper_cmd
  COPILOT_CAPTURE_STATUS=""
  COPILOT_CAPTURE_OUTPUT=""

  if [[ "${LLM_USAGE_DISABLE_COPILOT:-0}" == "1" ]]; then
    COPILOT_CAPTURE_STATUS="disabled"
    return 1
  fi

  if [[ -n "${LLM_USAGE_COPILOT_CAPTURE_TEXT+x}" ]]; then
    COPILOT_CAPTURE_STATUS="fixture"
    COPILOT_CAPTURE_OUTPUT="$LLM_USAGE_COPILOT_CAPTURE_TEXT"
    printf '%s\n' "$COPILOT_CAPTURE_OUTPUT"
    return 0
  fi

  cli=$(find_copilot_cli || true)
  if [[ -z "$cli" ]]; then
    COPILOT_CAPTURE_STATUS="missing-cli"
    return 1
  fi

  if ! command -v timeout >/dev/null 2>&1; then
    COPILOT_CAPTURE_STATUS="no-timeout"
    return 1
  fi

  python_bin=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
  if [[ -z "$python_bin" ]]; then
    COPILOT_CAPTURE_STATUS="no-pty-helper"
    return 1
  fi

  capture_cwd="${LLM_USAGE_COPILOT_CWD:-$(cd "$(dirname "$0")" && pwd)}"
  timeout_seconds="${LLM_USAGE_COPILOT_TIMEOUT:-10}"
  helper_cmd="${LLM_USAGE_COPILOT_CAPTURE_CMD:-}"

  set +e
  output=$(
    LLM_USAGE_COPILOT_CAPTURE_CWD="$capture_cwd" \
    LLM_USAGE_COPILOT_CAPTURE_CMD="$helper_cmd" \
    timeout "$timeout_seconds" "$python_bin" - "$cli" <<'PY'
import os
import pty
import re
import select
import signal
import sys
import time

cli = sys.argv[1]
capture_cwd = os.environ.get("LLM_USAGE_COPILOT_CAPTURE_CWD") or os.getcwd()
override_cmd = os.environ.get("LLM_USAGE_COPILOT_CAPTURE_CMD", "")

pid, fd = pty.fork()
if pid == 0:
    try:
        os.chdir(capture_cwd)
    except OSError:
        pass
    if override_cmd:
        os.execvp("bash", ["bash", "-lc", override_cmd])
    os.execvp(cli, [cli, "--screen-reader", "-C", capture_cwd])

parts = []
trust_sent = False
trust_seen = False
start = time.time()

while time.time() - start < 60:
    ready, _, _ = select.select([fd], [], [], 0.2)
    if fd in ready:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        parts.append(chunk.decode("utf-8", "replace"))

    text = "".join(parts)
    if "Confirm folder trust" in text or "Do you trust the files in this folder?" in text:
        trust_seen = True
        if not trust_sent:
            # Best effort only. In some terminal modes Copilot ignores raw PTY newlines.
            try:
                os.write(fd, b"\r")
            except OSError:
                pass
            trust_sent = True

    if "Monthly:" in text and "AI Credits:" in text:
        break

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        break
    time.sleep(0.15)

text = "".join(parts)
text = text.replace("\r", "\n").replace("\a", "")
text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
text = re.sub(r"[^\S\n]+", " ", text)
text = re.sub(r"\n{3,}", "\n\n", text)
if trust_seen and "Monthly:" not in text:
    text += "\ntrust_prompt_seen\n"
print(text.strip())
PY
  )
  status=$?
  set -e

  case "$status" in
    0) COPILOT_CAPTURE_STATUS="ok" ;;
    124) COPILOT_CAPTURE_STATUS="timeout" ;;
    *) COPILOT_CAPTURE_STATUS="capture-error" ;;
  esac

  COPILOT_CAPTURE_OUTPUT="$output"
  [[ -n "$COPILOT_CAPTURE_OUTPUT" ]] || return 1
  printf '%s\n' "$COPILOT_CAPTURE_OUTPUT"
}

parse_copilot_monthly_used() {
  local text="${1:-}"
  if [[ "$text" =~ Monthly:[[:space:]]*([0-9]+([.][0-9]+)?)%[[:space:]]*used ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

parse_copilot_ai_credits() {
  local text="${1:-}"
  if [[ "$text" =~ AI[[:space:]]+Credits:[[:space:]]*([0-9]+([.][0-9]+)?) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

read_copilot_live() {
  local screen monthly_used ai_credits reason
  capture_copilot_screen >/dev/null 2>&1 || true
  screen="$COPILOT_CAPTURE_OUTPUT"
  monthly_used=$(parse_copilot_monthly_used "$screen")
  ai_credits=$(parse_copilot_ai_credits "$screen")

  if [[ -n "$monthly_used" || -n "$ai_credits" ]]; then
    jq -nc \
      --arg source 'copilot cli' \
      --arg capture_status "$COPILOT_CAPTURE_STATUS" \
      --arg monthly_used "$monthly_used" \
      --arg ai_credits "$ai_credits" '
        def num_or_null($x): if $x == "" then null else ($x | tonumber?) end;
        {
          provider: "copilot",
          source: $source,
          capture_status: $capture_status,
          monthly: (num_or_null($monthly_used) | if . == null then null else {used: ., remaining: ([0, (100 - .), 100] | sort | .[1])} end),
          ai_credits: (num_or_null($ai_credits) | if . == null then null else {used: .} end)
        }'
    return 0
  fi

  reason="$COPILOT_CAPTURE_STATUS"
  if [[ "$screen" == *"trust_prompt_seen"* ]]; then
    reason="trust-prompt"
  elif [[ "$screen" =~ [Ll]og[[:space:]-]?[Ii]n|[Aa]uth ]]; then
    reason="not-authenticated"
  elif [[ -n "$screen" ]]; then
    reason="format-changed"
  fi

  jq -nc --arg source 'copilot cli' --arg reason "$reason" \
    '{provider:"copilot", source:$source, available:false, reason:$reason}'
}

# ── Cached copilot read ──────────────────────────────────────────────────────
# The PTY screen capture can take up to LLM_USAGE_COPILOT_TIMEOUT (10s) per
# run, which dominated llm-usage wall time. The monthly window moves slowly,
# so results are cached and refreshed by a detached background capture
# (stale-while-revalidate). Fixture and override knobs keep the original
# synchronous capture so tests and explicit overrides see unchanged behavior.

file_mtime_epoch() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true
}

copilot_cache_is_fresh() {
  local ttl="$1" mtime now
  [[ -s "$COPILOT_RESULT_CACHE" ]] || return 1
  mtime="$(file_mtime_epoch "$COPILOT_RESULT_CACHE")"
  [[ -n "$mtime" ]] || return 1
  now="$(date +%s)"
  (( now - mtime <= ttl ))
}

copilot_cache_bypass() {
  [[ -n "${LLM_USAGE_COPILOT_CAPTURE_TEXT+x}" ]] && return 0
  [[ -n "${LLM_USAGE_COPILOT_CAPTURE_CMD:-}" ]] && return 0
  [[ "${LLM_USAGE_DISABLE_COPILOT:-0}" == "1" ]] && return 0
  [[ "${LLM_USAGE_COPILOT_CACHE_TTL:-300}" == "0" ]] && return 0
  return 1
}

# Start one detached background capture that rewrites the cache atomically.
# A lock directory keeps concurrent runs (and --watch frames) from stacking
# captures; locks older than the capture timeout plus slack are reclaimed.
refresh_copilot_cache_async() {
  local lock="$COPILOT_REFRESH_LOCK"
  local stale_after=$(( ${LLM_USAGE_COPILOT_TIMEOUT:-10} + 30 ))
  mkdir -p "$CACHE_DIR" 2>/dev/null || return 0
  if ! mkdir "$lock" 2>/dev/null; then
    local lock_mtime now
    lock_mtime="$(file_mtime_epoch "$lock")"
    now="$(date +%s)"
    if [[ -n "$lock_mtime" ]] && (( now - lock_mtime <= stale_after )); then
      return 0
    fi
    rmdir "$lock" 2>/dev/null || true
    mkdir "$lock" 2>/dev/null || return 0
  fi
  # All fds detach from the caller so command substitutions never block on the
  # refresher, and the refresher survives this process exiting.
  (
    trap 'rmdir "$lock" 2>/dev/null || true' EXIT
    local tmp
    tmp="$(mktemp "$COPILOT_RESULT_CACHE.XXXXXX" 2>/dev/null)" || exit 0
    if read_copilot_live > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
      mv -f "$tmp" "$COPILOT_RESULT_CACHE"
    else
      rm -f "$tmp"
    fi
  ) </dev/null >/dev/null 2>&1 &
  disown $! 2>/dev/null || true
}

read_copilot() {
  if copilot_cache_bypass; then
    read_copilot_live
    return
  fi

  local ttl="${LLM_USAGE_COPILOT_CACHE_TTL:-300}"
  if copilot_cache_is_fresh "$ttl"; then
    cat "$COPILOT_RESULT_CACHE"
    return 0
  fi

  refresh_copilot_cache_async

  # Give a fast capture a brief window to land before serving stale data; a
  # refresher that fails fast (e.g. missing CLI) releases the lock early.
  local wait_budget="${LLM_USAGE_COPILOT_REFRESH_WAIT:-1}"
  local iterations i
  iterations="$(awk -v w="$wait_budget" 'BEGIN { printf "%d", (w / 0.05) }' 2>/dev/null || echo 0)"
  for (( i = 0; i < iterations; i++ )); do
    sleep 0.05
    if copilot_cache_is_fresh "$ttl"; then
      cat "$COPILOT_RESULT_CACHE"
      return 0
    fi
    [[ -d "$COPILOT_REFRESH_LOCK" ]] || break
  done

  if [[ -s "$COPILOT_RESULT_CACHE" ]]; then
    # Stale-while-revalidate: serve the last snapshot; the background capture
    # refreshes it for subsequent runs.
    cat "$COPILOT_RESULT_CACHE"
    return 0
  fi

  jq -nc --arg source 'copilot cli' \
    '{provider:"copilot", source:$source, available:false, reason:"refresh-pending"}'
}

json_for_provider() {
  local provider_json="${1:-}"
  local provider="$2"
  if [[ -z "$provider_json" ]]; then
    jq -nc --arg provider "$provider" '{provider:$provider, available:false}'
  else
    printf '%s\n' "$provider_json" | jq -c '
      def remain($x): if $x == null or $x.used == null then null else ([0, (100 - $x.used), 100] | sort | .[1]) end;
      def decorate($w): if $w == null then null else $w + {remaining: remain($w)} end;
      def decorate_row($r):
        {
          key: ($r.key // ""),
          name: ($r.name // ""),
          source: ($r.source // .source // ""),
          five_hour: decorate($r.five_hour),
          week: decorate($r.week)
        };
      if (.rows? | type) == "array" and ((.rows | length) > 0) then
        # Pick the primary "codex" row without a streaming select: a missing match
        # would otherwise emit `empty` and drop the whole provider object.
        (.rows | map(select(.key=="codex")) | .[0]) as $codex_row
        | . + {
          available: true,
          rows: ([.rows[] | decorate_row(.)]),
          five_hour: decorate($codex_row.five_hour // null),
          week: decorate($codex_row.week // null)
        }
      else
        . + {
          available: true,
          five_hour: decorate(.five_hour),
          week: decorate(.week)
        }
      end'
  fi
}

json_for_copilot() {
  local copilot_json="${1:-}"
  local show_credits="${2:-0}"
  if [[ -z "$copilot_json" ]]; then
    jq -nc '{provider:"copilot", source:"copilot cli", available:false, reason:"unavailable"}'
  else
    if [[ "$show_credits" -eq 1 ]]; then
      printf '%s\n' "$copilot_json" | jq -c '
        if .available? == false then .
        else . + {available: ((.monthly != null) or (.ai_credits != null))}
        end'
    else
      printf '%s\n' "$copilot_json" | jq -c '
        del(.ai_credits)
        | if .available? == false then .
        else . + {available: ((.monthly != null))}
        end'
    fi
  fi
}

# ── Shared CLI plumbing for llm-scheduler and ralph-robin ────────────────────
# These helpers operate on the callers' conventional globals: PROMPT_TEXT,
# PROMPT_FILE, RUN_DIR, PROMPT_SHA, LOG_DIR, TEXT_LOG, EVENT_LOG, CWD,
# MIN_REMAINING, POLL_INTERVAL, MAX_UNAVAILABLE_WAIT, RETRY_DELAYS.

err() {
  printf 'error: %s\n' "$*" >&2
}

is_number() {
  [[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

is_integer() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

format_local_epoch() {
  date -d "@$1" '+%Y-%m-%d %H:%M:%S %Z'
}

# Serialize argv (NUL-safe) into a JSON array string.
argv_to_json() {
  printf '%s\0' "$@" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.buffer.read().decode().split("\0")[:-1]))'
}

# Render a JSON argv array as one shell-quoted command line.
argv_json_to_command_line() {
  ARGV_JSON="$1" python3 -c 'import json,os,shlex; print(" ".join(shlex.quote(x) for x in json.loads(os.environ["ARGV_JSON"])))'
}

validate_retry_delays() {
  local list="${1:-}" part
  local parts=()
  [[ -n "$list" ]] || return 0
  IFS=',' read -r -a parts <<< "$list"
  for part in "${parts[@]}"; do
    if ! is_integer "$part"; then
      err "--retry-delays must be comma-separated integer seconds"
      return 1
    fi
  done
}

validate_prompt_args() {
  if [[ -n "$PROMPT_TEXT" && -n "$PROMPT_FILE" ]]; then
    err "use exactly one of --prompt or --prompt-file"
    return 1
  fi
  if [[ -z "$PROMPT_TEXT" && -z "$PROMPT_FILE" ]]; then
    err "one of --prompt or --prompt-file is required"
    return 1
  fi
  if [[ -n "$PROMPT_FILE" && ! -r "$PROMPT_FILE" ]]; then
    err "prompt file is not readable: $PROMPT_FILE"
    return 1
  fi
}

# MIN_REMAINING/POLL_INTERVAL are the callers' option globals, not misspellings.
# shellcheck disable=SC2153
validate_gate_args() {
  [[ -d "$CWD" ]] || { err "--cwd is not a directory: $CWD"; return 1; }
  is_number "$MIN_REMAINING" || { err "--min-remaining must be numeric"; return 1; }
  is_integer "$POLL_INTERVAL" || { err "--poll-interval must be integer seconds"; return 1; }
  if (( POLL_INTERVAL < 1 )); then
    err "--poll-interval must be at least 1"
    return 1
  fi
  is_integer "$MAX_UNAVAILABLE_WAIT" || { err "--max-unavailable-wait must be integer seconds (0 to wait forever)"; return 1; }
  validate_retry_delays "$RETRY_DELAYS" || return 1
}

# A usage window must actually exist for the chosen tool, otherwise gating can
# never resolve to a known limit (copilot has only a monthly window; codex and
# claude have 5h/weekly windows but no monthly one).
validate_tool_window() {
  local tool="$1" window="$2"
  case "$window" in
    auto|5h|weekly|monthly) ;;
    *) err "invalid --window: $window"; return 1 ;;
  esac
  case "$tool:$window" in
    codex:auto|codex:5h|codex:weekly) ;;
    claude:auto|claude:5h|claude:weekly) ;;
    copilot:auto|copilot:monthly) ;;
    copilot:*) err "--window $window is not valid for copilot (use auto or monthly)"; return 1 ;;
    *) err "--window $window is not valid for $tool (use auto, 5h, or weekly)"; return 1 ;;
  esac
}

log_text() {
  local msg="$1"
  [[ -n "${TEXT_LOG:-}" ]] || return 0
  printf '[%s] %s\n' "$(date -Is)" "$msg" >> "$TEXT_LOG"
}

log_event() {
  local type="$1"
  local data
  if [[ $# -ge 2 && -n "${2:-}" ]]; then
    data="$2"
  else
    data="{}"
  fi
  [[ -n "${EVENT_LOG:-}" ]] || return 0
  jq -nc --arg ts "$(date -Is)" --arg type "$type" --argjson data "$data" \
    '{ts:$ts,type:$type,data:$data}' >> "$EVENT_LOG"
}

# Create or reuse RUN_DIR under LOG_DIR with restrictive permissions, point the
# TEXT_LOG/EVENT_LOG globals at it, and refresh the latest symlinks.
setup_run_logs() {
  local name_suffix="$1"
  local tool_link="${2:-}"
  local stamp
  umask 077
  mkdir -p "$LOG_DIR"
  chmod 700 "$LOG_DIR" 2>/dev/null || true
  if [[ -z "$RUN_DIR" ]]; then
    stamp="$(date '+%Y%m%d-%H%M%S')"
    RUN_DIR="$(mktemp -d "$LOG_DIR/${stamp}-${name_suffix}-XXXXXX")"
  else
    mkdir -p "$RUN_DIR"
  fi
  chmod 700 "$RUN_DIR" 2>/dev/null || true
  TEXT_LOG="$RUN_DIR/run.log"
  EVENT_LOG="$RUN_DIR/events.jsonl"
  : >> "$TEXT_LOG"
  : >> "$EVENT_LOG"
  chmod 600 "$TEXT_LOG" "$EVENT_LOG" 2>/dev/null || true
  ln -sfn "$RUN_DIR" "$LOG_DIR/latest" 2>/dev/null || true
  if [[ -n "$tool_link" ]]; then
    ln -sfn "$RUN_DIR" "$LOG_DIR/latest-$tool_link" 2>/dev/null || true
  fi
}

# Snapshot the prompt into RUN_DIR/prompt.txt, load PROMPT_TEXT, and set
# PROMPT_SHA. Safe to call again on a resumed run dir (no self-copy).
load_prompt() {
  local prompt_dest prompt_src_real prompt_dest_real
  prompt_dest="$RUN_DIR/prompt.txt"
  if [[ -n "$PROMPT_FILE" ]]; then
    prompt_src_real="$(readlink -f "$PROMPT_FILE" 2>/dev/null || printf '%s' "$PROMPT_FILE")"
    prompt_dest_real="$(readlink -f "$prompt_dest" 2>/dev/null || printf '%s' "$prompt_dest")"
    if [[ "$prompt_src_real" != "$prompt_dest_real" ]]; then
      cp "$PROMPT_FILE" "$prompt_dest"
    fi
    IFS= read -r -d '' PROMPT_TEXT < "$prompt_dest" || true
  else
    printf '%s' "$PROMPT_TEXT" > "$prompt_dest"
  fi
  chmod 600 "$prompt_dest"
  # shellcheck disable=SC2034  # PROMPT_SHA is consumed by the sourcing CLIs.
  PROMPT_SHA="$(sha256sum "$prompt_dest" | awk '{print $1}')"
}

llm_usage_snapshot_for_tool() {
  local tool="$1"
  local raw provider_json
  if [[ -n "${LLM_SCHEDULER_USAGE_JSON:-}" ]]; then
    raw="$LLM_SCHEDULER_USAGE_JSON"
    provider_json="$(jq -c --arg tool "$tool" 'if has($tool) then .[$tool] else . end' <<<"$raw")"
    printf '%s\n' "$provider_json"
    return
  fi

  case "$tool" in
    codex)
      raw="$(read_codex || true)"
      json_for_provider "$raw" codex
      ;;
    claude)
      raw="$(read_claude || true)"
      json_for_provider "$raw" claude
      ;;
    copilot)
      raw="$(read_copilot || true)"
      json_for_copilot "$raw" 0
      ;;
    *)
      jq -nc --arg provider "$tool" '{provider:$provider, available:false, reason:"unsupported-tool"}'
      ;;
  esac
}

llm_usage_decision_for_tool() {
  local tool="$1"
  local window="$2"
  local min_remaining="$3"
  local poll_interval="$4"
  local snapshot="$5"
  jq -nc \
    --arg tool "$tool" \
    --arg window "$window" \
    --argjson min "$min_remaining" \
    --argjson now "$(now_epoch)" \
    --argjson poll "$poll_interval" \
    --argjson copilot_reset "$(copilot_monthly_reset_epoch | sed 's/^-$/null/')" \
    --argjson snapshot "$snapshot" '
      def num: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
      def reset_epoch($r):
        if $r == null then null
        elif ($r|type) == "number" then ($r|floor)
        elif ($r|type) == "string" and ($r|test("^[0-9]+(\\.[0-9]+)?$")) then ($r|tonumber|floor)
        elif ($r|type) == "string" then
          (($r | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | sub("-00:00$"; "Z") | fromdateiso8601?) // null)
        else null end;
      def win($name; $obj):
        if $obj == null then null
        else {name:$name, remaining:($obj.remaining|num), resets_at:($obj.resets_at // null), reset_epoch:reset_epoch($obj.resets_at)}
        end;
      def selected:
        if $tool == "copilot" then
          if ($window == "auto" or $window == "monthly") then
            [ {name:"monthly", remaining:(.monthly.remaining|num), resets_at:($copilot_reset|tostring), reset_epoch:$copilot_reset} ]
          else [] end
        elif $window == "auto" then
          [win("5h"; .five_hour), win("weekly"; .week)] | map(select(. != null))
        elif $window == "5h" then
          [win("5h"; .five_hour)] | map(select(. != null))
        elif $window == "weekly" then
          [win("weekly"; .week)] | map(select(. != null))
        else [] end;
      # A window is actively exhausted only if remaining is low AND the reset
      # epoch is in the future (or unknown). A past reset_epoch means the
      # snapshot is stale: the window has already refilled.
      def is_active_exhausted($w):
        $w.remaining != null and $w.remaining <= $min
        and ($w.reset_epoch == null or $w.reset_epoch > $now);
      $snapshot
      | selected as $windows
      | ($windows | map(select(.remaining != null))) as $known
      | ($known | map(select(is_active_exhausted(.)))) as $exhausted
      | ($exhausted | map(select(.reset_epoch != null and .reset_epoch > $now) | .reset_epoch) | max? // null) as $latest_reset
      | if (.available? == false) then
          {tool:$tool, usable:false, reason:(.reason // "unavailable"), wait_until:($now + $poll), windows:$windows}
        elif ($windows|length) == 0 then
          {tool:$tool, usable:false, reason:"unsupported-window", wait_until:($now + $poll), windows:$windows}
        elif ($known|length) == 0 then
          {tool:$tool, usable:false, reason:"inconclusive-usage", wait_until:($now + $poll), windows:$windows}
        elif ($exhausted|length) > 0 then
          {tool:$tool, usable:false, reason:"rate-limited", wait_until:(if $latest_reset != null then $latest_reset else ($now + $poll) end), windows:$windows, exhausted:$exhausted}
        else
          {tool:$tool, usable:true, reason:"usable", wait_until:null, windows:$windows}
        end'
}
