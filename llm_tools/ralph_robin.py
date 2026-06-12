from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import common
from . import scheduler


APP_NAME = "ralph-robin"


USAGE = """Usage: ralph-robin (--prompt TEXT | --prompt-file FILE) [options]

Round-robin prompt submission across local LLM CLIs. By default it keeps using
the usable provider with the highest weekly remaining allowance per day, so
weekly quotas burn down more evenly. Disable this with --no-even-burn to keep
using the current provider until it is exhausted.

By default the selected CLI uses llm-scheduler's autonomous headless adapter
even from an interactive terminal. This avoids provider prompts blocking the
rotation. Use llm-scheduler directly for an attached interactive run.

Examples:
  ralph-robin --prompt-file task.md
  ralph-robin --prompt "Continue until tests pass"
  ralph-robin --tools claude,codex,copilot --prompt-file task.md
  ralph-robin --prompt-file task.md --tmux llm-work
  ralph-robin --prompt-file task.md --dry-run

Options:
  --tools LIST                Comma-separated tools in rotation (default: claude,codex).
                              Values: codex, claude, copilot.
  --prompt TEXT               Prompt text.
  --prompt-file FILE          Read prompt from FILE, preserving content.
  --window auto|5h|weekly|monthly  Usage window to gate on (default: auto).
  --min-remaining PERCENT     Minimum required remaining percentage (default: 1).
  --poll-interval SECONDS     Poll interval passed to llm-scheduler (default: 60).
  --max-unavailable-wait SECONDS  Bound inconclusive usage waits before optimistic
                              launch (default: 900; 0 waits forever).
  --retry-delays LIST         Comma-separated retry delays (default: 60,180,600).
  --no-retry                  Disable retries after failed submission.
  --even-burn                 Prefer the usable tool with the highest weekly
                              remaining allowance per day (default).
  --no-even-burn              Keep using the current provider until exhausted.
  --cwd DIR                   Working directory for the target CLI (default: current directory).
  --fresh                     Launch a fresh CLI process through llm-scheduler (default).
  --headless                  Always use the non-interactive provider command
                              and captured PTY, even on a terminal.
  --tmux SESSION[:WINDOW]     Execute through tmux via llm-scheduler.
  --command-template TEMPLATE Override provider command; placeholders: {tool}, {prompt}, {prompt_file}, {cwd}.
  --auto-confirm              Acknowledge only known safe prompts (default).
  --no-auto-confirm           Disable automatic prompt acknowledgement.
  --headless-idle-timeout SECONDS
                              Abort headless runs with no output progress
                              (default: LLM_SCHEDULER_IDLE_TIMEOUT or 600; 0 disables).
  --headless-question-timeout SECONDS
                              Abort headless runs that ask a question then stop
                              making progress (default: LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT or 30; 0 disables).
  --log-dir DIR               Log directory (default: ~/.cache/llm-tools/ralph-robin/logs).
  --state-file FILE           Rotation state file (default: ~/.cache/llm-tools/ralph-robin/state.json).
  --wake                      Pass best-effort wake scheduling to llm-scheduler.
  --suspend-until-ready       Suspend even for the selected tool's own wait gates.
  --dry-run                   Resolve rotation and usage state without submitting.
  -h, --help                  Show this help.
"""


@dataclass
class RalphConfig:
    tools_spec: str = "claude,codex"
    tools: list[str] = field(default_factory=list)
    prompt_text: str = ""
    prompt_file: str = ""
    prompt_source: str = ""
    window: str = "auto"
    min_remaining: str = "1"
    poll_interval: str = "60"
    max_unavailable_wait: str = "900"
    retry_delays: str = "60,180,600"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    exec_mode: str = "fresh"
    tmux_target: str = ""
    command_template: str = ""
    auto_confirm: bool = True
    headless: bool = True
    log_dir: Path = field(default_factory=common.ralph_log_dir)
    state_file: Path = field(default_factory=common.ralph_state_file)
    wake: bool = False
    suspend_until_ready: bool = False
    dry_run: bool = False
    even_burn: bool = True


def trim(value: str) -> str:
    return value.strip()


def color_enabled() -> bool:
    return bool(
        sys.stderr.isatty()
        and os.environ.get("TERM") != "dumb"
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("LLM_USAGE_NO_COLOR")
    )


def style(text: str, role: str) -> str:
    return common.ansi_wrap(text, role) if color_enabled() else text


def status_line(message: str, *, level: str = "info") -> None:
    role = level if level in common.ANSI_COLOR_ROLES else "info"
    prefix = style(f"{common.symbol_prefix('brand')}ralph-robin", "brand")
    marker = common.symbol_prefix(role).rstrip()
    marker_text = f"{style(marker, role)} " if marker else ""
    print(f"{prefix}: {marker_text}{message}", file=sys.stderr)


def decision_summary(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason", "unknown"))
    windows = decision.get("windows")
    parts: list[str] = []
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            remaining = window.get("remaining")
            name = window.get("name", "?")
            if isinstance(remaining, (int, float)):
                parts.append(f"{name} {common.fmt_pct(remaining)}% left")
    wait_until = decision.get("wait_until")
    if reason == "rate-limited" and isinstance(wait_until, int):
        parts.append(f"until {common.format_local_epoch(wait_until)}")
    detail = ", ".join(parts) if parts else "-"
    return f"{reason} ({detail})"


def print_usage_summary(selection: dict[str, Any]) -> None:
    decisions = selection.get("decisions")
    if not isinstance(decisions, list):
        return
    rendered: list[str] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", "?"))
        summary = decision_summary(item)
        if item.get("usable") is True:
            rendered.append(f"{style(tool, 'ok')}: {summary}")
        elif item.get("reason") == "rate-limited":
            rendered.append(f"{style(tool, 'error')}: {summary}")
        else:
            rendered.append(f"{style(tool, 'warn')}: {summary}")
    status_line("usage " + " | ".join(rendered), level="dim")


def ralph_runtime_context(cfg: RalphConfig, selected_tool: str, selection: dict[str, Any]) -> str:
    decisions = selection.get("decisions")
    summaries: list[str] = []
    if isinstance(decisions, list):
        for item in decisions:
            if isinstance(item, dict):
                summaries.append(f"- {item.get('tool', '?')}: {decision_summary(item)}")
    decision_text = "\n".join(summaries) if summaries else "- unavailable"
    return (
        "RALPH ROBIN RUNTIME CONTEXT\n"
        "This block is injected by ralph-robin and takes precedence for scheduling, handoff, and session-window decisions.\n"
        f"- Current selected provider: {selected_tool}\n"
        f"- Configured provider rotation: {', '.join(cfg.tools)}\n"
        "- Treat any original prompt instruction to check or schedule a different provider as stale unless Ralph's latest decisions show the current provider is unusable.\n"
        f"- For stop thresholds such as session window, credits, capacity, or below 25%, evaluate the current selected provider ({selected_tool}), not a previously-used provider named in the prompt.\n"
        "- Do not run provider-specific llm-scheduler --suspend-until-ready commands from the original prompt while the current selected provider is usable; Ralph owns cross-provider rotation and suspend decisions.\n"
        "- Latest Ralph usage decisions:\n"
        f"{decision_text}\n"
        "END RALPH ROBIN RUNTIME CONTEXT\n"
    )


def provider_prompt_for(cfg: RalphConfig, selected_tool: str, selection: dict[str, Any], prompt: str) -> str:
    return f"{ralph_runtime_context(cfg, selected_tool, selection)}\n{prompt}"


def parse_tools(raw: str) -> list[str]:
    tools: list[str] = []
    for part in raw.split(","):
        tool = trim(part)
        if not tool:
            continue
        if tool not in {"codex", "claude", "copilot"}:
            common.err(f"invalid tool in --tools: {tool}")
            raise SystemExit(2)
        tools.append(tool)
    if not tools:
        common.err("--tools must name at least one tool")
        raise SystemExit(2)
    return tools


def parse_args(argv: list[str]) -> RalphConfig:
    cfg = RalphConfig()
    i = 0
    while i < len(argv):
        arg = argv[i]
        def need_value(msg: str) -> str:
            nonlocal i
            if i + 1 >= len(argv):
                common.err(msg)
                raise SystemExit(2)
            value = argv[i + 1]
            i += 2
            return value

        if arg == "--tools":
            cfg.tools_spec = need_value("--tools requires a value")
        elif arg == "--prompt":
            cfg.prompt_text = need_value("--prompt requires text")
            cfg.prompt_source = "inline"
        elif arg == "--prompt-file":
            cfg.prompt_file = need_value("--prompt-file requires a file")
            cfg.prompt_source = f"file:{cfg.prompt_file}"
        elif arg == "--window":
            cfg.window = need_value("--window requires a value")
        elif arg == "--min-remaining":
            cfg.min_remaining = need_value("--min-remaining requires a value")
        elif arg == "--poll-interval":
            cfg.poll_interval = need_value("--poll-interval requires seconds")
        elif arg == "--max-unavailable-wait":
            cfg.max_unavailable_wait = need_value("--max-unavailable-wait requires seconds")
        elif arg == "--retry-delays":
            cfg.retry_delays = need_value("--retry-delays requires a list")
        elif arg == "--no-retry":
            cfg.retry_delays = ""
            i += 1
        elif arg == "--even-burn":
            cfg.even_burn = True
            i += 1
        elif arg == "--no-even-burn":
            cfg.even_burn = False
            i += 1
        elif arg == "--cwd":
            cfg.cwd = need_value("--cwd requires a directory")
        elif arg == "--fresh":
            cfg.exec_mode = "fresh"
            cfg.tmux_target = ""
            i += 1
        elif arg == "--headless":
            cfg.headless = True
            i += 1
        elif arg == "--tmux":
            cfg.exec_mode = "tmux"
            cfg.tmux_target = need_value("--tmux requires SESSION[:WINDOW]")
        elif arg == "--command-template":
            cfg.command_template = need_value("--command-template requires a template")
        elif arg == "--auto-confirm":
            cfg.auto_confirm = True
            i += 1
        elif arg == "--no-auto-confirm":
            cfg.auto_confirm = False
            i += 1
        elif arg == "--headless-idle-timeout":
            os.environ["LLM_SCHEDULER_IDLE_TIMEOUT"] = need_value("--headless-idle-timeout requires seconds")
        elif arg == "--headless-question-timeout":
            os.environ["LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT"] = need_value("--headless-question-timeout requires seconds")
        elif arg == "--log-dir":
            cfg.log_dir = Path(need_value("--log-dir requires a directory"))
        elif arg == "--state-file":
            cfg.state_file = Path(need_value("--state-file requires a file"))
        elif arg == "--wake":
            cfg.wake = True
            i += 1
        elif arg == "--suspend-until-ready":
            cfg.suspend_until_ready = True
            cfg.wake = True
            i += 1
        elif arg == "--dry-run":
            cfg.dry_run = True
            i += 1
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        else:
            common.err(f"unknown option: {arg}")
            print(USAGE, end="", file=sys.stderr)
            raise SystemExit(2)
    return cfg


def validate_args(cfg: RalphConfig) -> None:
    cfg.tools = parse_tools(cfg.tools_spec)
    common.validate_prompt_args(cfg.prompt_text, cfg.prompt_file)
    for tool in cfg.tools:
        common.validate_tool_window(tool, cfg.window)
    common.validate_gate_args(cfg.cwd, cfg.min_remaining, cfg.poll_interval, cfg.max_unavailable_wait, cfg.retry_delays)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")):
        common.err("LLM_SCHEDULER_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")):
        common.err("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)


def safe_args_json(cfg: RalphConfig) -> dict[str, Any]:
    return {
        "tools_spec": cfg.tools_spec,
        "tools": cfg.tools,
        "window": cfg.window,
        "min_remaining": float(cfg.min_remaining),
        "poll_interval": int(cfg.poll_interval),
        "max_unavailable_wait": int(cfg.max_unavailable_wait),
        "retry_delays": cfg.retry_delays,
        "cwd": cfg.cwd,
        "mode": cfg.exec_mode,
        "tmux": cfg.tmux_target,
        "prompt_source": cfg.prompt_source,
        "log_dir": str(cfg.log_dir),
        "state_file": str(cfg.state_file),
        "headless_idle_timeout": int(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")),
        "headless_question_timeout": int(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")),
        "auto_confirm": cfg.auto_confirm,
        "headless": cfg.headless,
        "dry_run": cfg.dry_run,
        "wake": cfg.wake,
        "suspend_until_ready": cfg.suspend_until_ready,
        "even_burn": cfg.even_burn,
    }


def current_index_from_state(cfg: RalphConfig) -> int:
    if cfg.state_file.is_file() and cfg.state_file.stat().st_size > 0:
        try:
            obj = json.loads(cfg.state_file.read_text(encoding="utf-8"))
            if obj.get("tools_spec") == cfg.tools_spec:
                index = int(obj.get("current_index", 0))
            else:
                index = 0
        except (OSError, ValueError, json.JSONDecodeError):
            index = 0
    else:
        index = 0
    return index if 0 <= index < len(cfg.tools) else 0


def save_state(cfg: RalphConfig, selected_index: int, selected_tool: str) -> None:
    if cfg.dry_run:
        return
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg.state_file.parent.chmod(0o700)
    except OSError:
        pass
    obj = {
        "tools_spec": cfg.tools_spec,
        "tools": cfg.tools,
        "current_tool": selected_tool,
        "current_index": selected_index,
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).astimezone().isoformat(),
    }
    cfg.state_file.write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        cfg.state_file.chmod(0o600)
    except OSError:
        pass


def rotation_order_indices(length: int, current_index: int) -> list[int]:
    return [(current_index + i) % length for i in range(length)]


def weekly_allowance_per_day(decision: dict[str, Any], env: dict[str, str] | None = None) -> float | None:
    env = env or os.environ
    windows = decision.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, dict) or window.get("name") != "weekly":
            continue
        remaining = window.get("remaining")
        reset_epoch = window.get("reset_epoch")
        if not isinstance(remaining, (int, float)) or not isinstance(reset_epoch, int):
            return None
        seconds_until_reset = reset_epoch - common.now_epoch(env)
        if seconds_until_reset <= 0:
            return None
        days_until_reset = seconds_until_reset / 86400.0
        return float(remaining) / days_until_reset
    return None


def even_burn_index(cfg: RalphConfig, decisions: list[dict[str, Any]], current_index: int, skipped: set[str]) -> int | None:
    usable_indices = [
        i
        for i, decision in enumerate(decisions)
        if decision.get("usable") is True and cfg.tools[i] not in skipped
    ]
    if len(usable_indices) < 2:
        return None
    scored: list[tuple[float, int, int]] = []
    rotation_rank = {idx: rank for rank, idx in enumerate(rotation_order_indices(len(cfg.tools), current_index))}
    for i in usable_indices:
        score = weekly_allowance_per_day(decisions[i])
        if score is None:
            return None
        scored.append((score, -rotation_rank[i], i))
    scored.sort(reverse=True)
    return scored[0][2] if scored else None


def select_tool(cfg: RalphConfig, logs: common.RunLogs, current_index: int, skipped: set[str]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    for tool in cfg.tools:
        snapshot = common.usage_snapshot_for_tool(tool)
        decision = common.usage_decision_for_tool(tool, cfg.window, cfg.min_remaining, cfg.poll_interval, snapshot)
        decisions.append(decision)
        common.log_event(logs, "usage_snapshot", {"tool": tool, "snapshot": snapshot})
        common.log_event(logs, "usage_decision", decision)
    if cfg.even_burn:
        balanced_index = even_burn_index(cfg, decisions, current_index, skipped)
        if balanced_index is not None:
            return {
                "index": balanced_index,
                "tool": cfg.tools[balanced_index],
                "rotation_reason": "even-burn",
                "all_rate_limited": False,
                "decision": decisions[balanced_index],
                "decisions": decisions,
            }
    if decisions[current_index].get("usable") is True and cfg.tools[current_index] not in skipped:
        return {"index": current_index, "tool": cfg.tools[current_index], "rotation_reason": "current-usable", "all_rate_limited": False, "decision": decisions[current_index], "decisions": decisions}
    for i in range(1, len(cfg.tools)):
        nxt = (current_index + i) % len(cfg.tools)
        if decisions[nxt].get("usable") is True and cfg.tools[nxt] not in skipped:
            return {"index": nxt, "tool": cfg.tools[nxt], "rotation_reason": "advanced-to-usable", "all_rate_limited": False, "decision": decisions[nxt], "decisions": decisions}

    for i in range(len(cfg.tools)):
        fallback = (current_index + i) % len(cfg.tools)
        if cfg.tools[fallback] in skipped:
            continue
        if decisions[fallback].get("reason") != "rate-limited":
            return {
                "index": fallback,
                "tool": cfg.tools[fallback],
                "rotation_reason": "advanced-to-undetermined",
                "all_rate_limited": False,
                "decision": decisions[fallback],
                "decisions": decisions,
            }

    active_decisions = [(i, decision) for i, decision in enumerate(decisions) if cfg.tools[i] not in skipped]
    all_active_rate_limited = bool(active_decisions) and all(
        decision.get("reason") == "rate-limited" and isinstance(decision.get("wait_until"), int)
        for _i, decision in active_decisions
    )
    best_index = -1
    best_wait: int | None = None
    for i, decision in active_decisions:
        wait_until = decision.get("wait_until")
        if decision.get("reason") == "rate-limited" and isinstance(wait_until, int):
            if best_wait is None or wait_until < best_wait:
                best_wait = wait_until
                best_index = i
    if best_index == -1:
        for i in range(len(cfg.tools)):
            fallback = (current_index + i) % len(cfg.tools)
            if cfg.tools[fallback] not in skipped:
                best_index = fallback
                break
    if best_index == -1:
        return {"index": -1, "tool": "", "rotation_reason": "all-skipped", "all_rate_limited": False, "decision": {"usable": False, "reason": "all-skipped"}, "decisions": decisions}
    return {
        "index": best_index,
        "tool": cfg.tools[best_index],
        "rotation_reason": "all-unusable",
        "all_rate_limited": all_active_rate_limited and best_wait is not None,
        "decision": decisions[best_index],
        "decisions": decisions,
    }


def scheduler_config_for(cfg: RalphConfig, selected_tool: str, logs: common.RunLogs, force_suspend: bool, provider_prompt: str) -> scheduler.SchedulerConfig:
    return scheduler.SchedulerConfig(
        tool=selected_tool,
        prompt_text=provider_prompt,
        prompt_source=f"ralph-runtime:{selected_tool}",
        window=cfg.window,
        min_remaining=cfg.min_remaining,
        poll_interval=cfg.poll_interval,
        max_unavailable_wait=cfg.max_unavailable_wait,
        retry_delays=cfg.retry_delays,
        cwd=cfg.cwd,
        exec_mode=cfg.exec_mode,
        tmux_target=cfg.tmux_target,
        command_template=cfg.command_template,
        auto_confirm=cfg.auto_confirm,
        headless=cfg.headless,
        log_dir=logs.run_dir,
        run_dir=logs.run_dir / "scheduler",
        dry_run=cfg.dry_run,
        wake=cfg.wake,
        suspend_until_ready=cfg.suspend_until_ready or force_suspend,
        exact_stdout=True,
        claude_stream_json=selected_tool == "claude" and not cfg.command_template,
        ralph_robin_active=True,
        ralph_robin_tools=",".join(cfg.tools),
    )


def run_scheduler_inline(scfg: scheduler.SchedulerConfig) -> int:
    scheduler.resolve_attach_mode(scfg)
    child_logs = common.setup_run_logs(scfg.log_dir, scfg.tool or "wake", scfg.tool or "", scfg.run_dir)
    prompt, prompt_sha = common.load_prompt(scfg.prompt_text, scfg.prompt_file, child_logs)
    scfg.prompt_text = prompt
    common.log_text(child_logs, f"start provider={scfg.tool} cwd={scfg.cwd} attached={1 if scfg.attached else 0}")
    common.log_event(child_logs, "start", scheduler.safe_args_json(scfg))
    common.log_event(child_logs, "prompt", {"source": scfg.prompt_source, "sha256": prompt_sha, "prompt": prompt})
    try:
        scheduler.wait_until_usable(scfg, child_logs)
    except SystemExit as exc:
        return int(exc.code or 0)
    argv = scheduler.command_argv(scfg, child_logs, prompt)
    common.log_event(child_logs, "resolved_command", {"argv": argv})
    if scfg.dry_run:
        common.log_text(child_logs, "dry-run complete")
        common.log_event(child_logs, "final", {"status": "dry-run"})
        print("dry-run: no prompt submitted", file=sys.stderr)
        return 0
    delays = [int(x) for x in scfg.retry_delays.split(",") if x] if scfg.retry_delays else []
    attempt = 1
    result = scheduler.submit_once(scfg, child_logs, attempt, argv)
    if result == 0:
        common.log_event(child_logs, "final", {"status": "success"})
        return 0
    if result == common.AUTONOMY_ABORT_STATUS:
        common.log_event(child_logs, "final", {"status": "autonomy-abort"})
        return common.AUTONOMY_ABORT_STATUS
    for delay in delays:
        common.log_event(child_logs, "retry", {"after_attempt": attempt, "delay": delay})
        import time

        time.sleep(delay)
        attempt += 1
        result = scheduler.submit_once(scfg, child_logs, attempt, argv)
        if result == 0:
            common.log_event(child_logs, "final", {"status": "success"})
            return 0
        if result == common.AUTONOMY_ABORT_STATUS:
            common.log_event(child_logs, "final", {"status": "autonomy-abort"})
            return common.AUTONOMY_ABORT_STATUS
    common.log_event(child_logs, "final", {"status": "failed"})
    return 1


def main(argv: list[str] | None = None) -> int:
    common.migrate_legacy_cache_dirs()
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    validate_args(cfg)
    logs = common.setup_run_logs(cfg.log_dir, "ralph-robin")
    prompt, prompt_sha = common.load_prompt(cfg.prompt_text, cfg.prompt_file, logs)
    cfg.prompt_text = prompt
    common.log_text(logs, f"start tools={cfg.tools_spec} cwd={cfg.cwd}")
    common.log_text(logs, f"run directory: {logs.run_dir}")
    common.log_event(logs, "start", safe_args_json(cfg))
    common.log_event(logs, "prompt", {"source": cfg.prompt_source, "sha256": prompt_sha, "prompt": prompt})
    current_index = current_index_from_state(cfg)
    status_line(f"logs: {logs.run_dir}", level="dim")
    skipped: set[str] = set()
    while True:
        common.log_event(logs, "state", {"state_file": str(cfg.state_file), "current_index": current_index})
        selection = select_tool(cfg, logs, current_index, skipped)
        common.log_event(logs, "selection", {**selection, "skipped": sorted(skipped)})
        selected_index = int(selection.get("index", -1))
        selected_tool = str(selection.get("tool", ""))
        if selected_index == -1 or not selected_tool:
            common.log_event(logs, "final", {"status": "autonomy-abort", "reason": "all-providers-skipped", "skipped": sorted(skipped)})
            status_line(f"autonomy-abort: all configured tools blocked; logs: {logs.run_dir}", level="error")
            return common.AUTONOMY_ABORT_STATUS
        reason = str(selection.get("rotation_reason"))
        all_rate_limited = bool(selection.get("all_rate_limited"))
        common.log_text(logs, f"selected provider={selected_tool} reason={reason} all_rate_limited={str(all_rate_limited).lower()}")
        print_usage_summary(selection)
        level = "warn" if all_rate_limited else "ok"
        status_line(f"selected {selected_tool} ({reason})", level=level)
        if all_rate_limited:
            wait_until = selection.get("decision", {}).get("wait_until")
            wait_display = f"{common.format_local_epoch(int(wait_until))} (epoch {wait_until})" if isinstance(wait_until, int) else "unknown"
            status_line(f"all configured tools are rate-limited; suspending via llm-scheduler until {wait_display}", level="warn")
        save_state(cfg, selected_index, selected_tool)
        if cfg.dry_run:
            common.log_event(logs, "final", {"status": "dry-run"})
            print("dry-run: no prompt submitted", file=sys.stderr)
            return 0
        provider_prompt = provider_prompt_for(cfg, selected_tool, selection, prompt)
        scfg = scheduler_config_for(cfg, selected_tool, logs, all_rate_limited, provider_prompt)
        common.log_event(logs, "scheduler_command", {"argv": ["llm-scheduler", "--tool", selected_tool]})
        status = run_scheduler_inline(scfg)
        common.log_event(logs, "scheduler_result", {"status": status})
        if status == 0:
            common.log_event(logs, "final", {"status": "success"})
            return 0
        if status == common.AUTONOMY_ABORT_STATUS:
            common.log_text(logs, f"scheduler autonomy-abort for provider={selected_tool}; re-evaluating rotation")
            common.log_event(logs, "provider_autonomy_abort", {"tool": selected_tool, "index": selected_index})
            status_line(f"{selected_tool} blocked autonomously; re-evaluating rotation", level="warn")
            skipped.add(selected_tool)
            if len(skipped) >= len(cfg.tools):
                common.log_event(logs, "final", {"status": "autonomy-abort", "reason": "all-providers-skipped", "skipped": sorted(skipped)})
                status_line(f"autonomy-abort: all configured tools blocked; logs: {logs.run_dir}", level="error")
                return common.AUTONOMY_ABORT_STATUS
            current_index = (selected_index + 1) % len(cfg.tools)
            continue
        common.log_event(logs, "final", {"status": "failed", "exit_code": status})
        return status


if __name__ == "__main__":
    raise SystemExit(main())
