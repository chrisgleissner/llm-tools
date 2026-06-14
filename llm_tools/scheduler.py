from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import common
from . import config as toolconfig


APP_NAME = "llm-scheduler"


USAGE = """Usage: llm-scheduler
  llm-scheduler -P PROVIDER (-p TEXT | -f FILE) [options]

Submit a prompt to a local LLM CLI as soon as shared llm-usage data says it is usable.

When fresh mode runs on an interactive terminal, the provider CLI is launched
in its normal interactive form attached directly to that terminal: output,
key input, window resizes, and Ctrl-C behave exactly as if the CLI had been
started directly in the shell. Without a terminal (pipes, cron, systemd
resume) or with --headless, the non-interactive form (claude --print,
codex exec, copilot --prompt, kilo run --auto, opencode run, mmx run --auto)
runs on a captured PTY instead.

Examples:
  llm-scheduler --provider codex --prompt-file task.md
  llm-scheduler --provider claude --prompt "Continue the work in this repo until CI is green"
  llm-scheduler --provider copilot --prompt-file task.md --retry-delays 60,180,600
  llm-scheduler --provider kilo --prompt-file task.md
  llm-scheduler --provider opencode --prompt-file task.md
  llm-scheduler --provider minimax --prompt-file task.md
  llm-scheduler --provider codex --prompt-file task.md --at "23:05"
  llm-scheduler --provider codex --prompt-file task.md --tmux llm-work
  llm-scheduler --provider codex --prompt-file task.md --wake
  llm-scheduler --provider claude --prompt-file task.md --scope 5h --suspend-until-ready
  llm-scheduler --provider codex --prompt-file task.md --dry-run

Options:
  -P, --provider PROVIDER                 Provider: codex, claude, copilot, kilo, opencode, minimax.
  -M, --model MODEL                       Pin the provider model (overrides the config file policy).
  -p, --prompt TEXT                       Prompt text.
  -f, --prompt-file FILE                  Read prompt from FILE, preserving content.
  -a, --at TIME                           Do not submit before date-compatible local time.
  -a, --not-before TIME                   Alias for --at.
  -s, --scope SCOPE                       Capacity scope to gate on (default: auto).
  -W, --window SCOPE                      Deprecated alias for --scope.
  -m, --min-remaining PERCENT             Minimum required remaining percentage (default: 1).
  -i, --poll-interval SECONDS             Poll interval when reset data is unavailable (default: 60).
  -u, --max-unavailable-wait SECONDS      Max inconclusive-data wait before optimistic launch (default: 900; 0 forever).
  -r, --retry-delays LIST                 Comma-separated retry delays (default: 60,180,600).
  -R, --no-retry                          Disable retries after failed submission.
  -C, --cwd DIR                           Working directory for the target CLI (default: current directory).
  -F, --fresh                             Launch a fresh CLI process (default).
  -H, --headless                          Always use non-interactive provider command on a captured PTY.
  -T, --tmux SESSION[:WINDOW]             Execute via tmux instead of foreground process.
  -e, --command-template TEMPLATE         Override provider command; placeholders: {provider}, {prompt}, {prompt_file}, {cwd}.
  -y, --auto-confirm                      Acknowledge only known safe prompts (default).
  -Y, --no-auto-confirm                   Disable automatic prompt acknowledgement.
  -I, --headless-idle-timeout SECONDS     Abort headless runs with no output progress (0 disables).
  -Q, --headless-question-timeout SECONDS Abort headless runs that ask a question then stall (0 disables).
  -L, --log-dir DIR                       Log directory.
  -O, --run-dir DIR                       Reuse/write one specific run directory.
  -d, --dry-run                           Resolve state and command plan without submitting.
  -k, --wake                              Best-effort wake scheduling diagnostics/logging.
  -U, --suspend-until-ready               Suspend until the next reset/not-before time.
  -x, --wake-test                         Print wake capability diagnostics and exit.
  -h, --help                              Show this help.

Scopes:
  codex/claude/minimax: auto, 5h, weekly
  copilot:              auto, monthly
  kilo/opencode:        auto, balance, budget, byok, ungated
"""


@dataclass
class SchedulerConfig:
    provider: str = ""
    prompt_text: str = ""
    prompt_file: str = ""
    prompt_source: str = ""
    at_time: str = ""
    not_before_epoch: int | None = None
    # Pinned model the provider CLI runs (via --model), and whether to fall back
    # to another model when this one's rate limit is exhausted. Both resolved
    # from the shared config file's per-provider policy (CLI --model wins).
    model: str = ""
    allow_fallback: bool = False
    # When set, gate on this other provider's usage windows instead of
    # ``provider``'s own (e.g. opencode running the minimax model). The
    # ``provider`` CLI is still the one launched.
    capacity_provider: str = ""
    scope: str = "auto"
    min_remaining: str = "1"
    poll_interval: str = "60"
    max_unavailable_wait: str = "900"
    retry_delays: str = "60,180,600"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    exec_mode: str = "fresh"
    tmux_target: str = ""
    command_template: str = ""
    auto_confirm: bool = True
    headless: bool = False
    attached: bool = False
    log_dir: Path = field(default_factory=common.scheduler_log_dir)
    run_dir: Path | None = None
    dry_run: bool = False
    wake: bool = False
    wake_test: bool = False
    suspend_until_ready: bool = False
    pre_suspend_confirmation_seconds: int = 5
    wake_armed_target: int = 0
    exact_stdout: bool = False
    claude_stream_json: bool = False
    ralph_robin_active: bool = False
    ralph_robin_providers: str = ""
    # Ordered fields stamped on each relayed provider line (see
    # common.LINE_PREFIX_FIELDS). Empty disables the marker entirely.
    output_prefix_fields: list[str] = field(default_factory=list)
    # Seconds between refreshes of the "usage" prefix field (cached in between).
    output_prefix_usage_ttl: float = 15.0
    # CLI flags the user passed explicitly, so config-file values never clobber
    # them (precedence: built-in defaults < config file < CLI flags).
    explicit: set[str] = field(default_factory=set)


def parse_args(argv: list[str]) -> SchedulerConfig:
    cfg = SchedulerConfig()
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

        if arg in ("-P", "--provider"):
            cfg.provider = need_value("--provider requires a value")
            cfg.explicit.add("provider")
        elif arg in ("-p", "--prompt"):
            cfg.prompt_text = need_value("--prompt requires text")
            cfg.prompt_source = "inline"
        elif arg in ("-f", "--prompt-file"):
            cfg.prompt_file = need_value("--prompt-file requires a file")
            cfg.prompt_source = f"file:{cfg.prompt_file}"
        elif arg in ("-a", "--at", "--not-before"):
            cfg.at_time = need_value(f"{arg} requires a time")
        elif arg in ("-M", "--model"):
            cfg.model = need_value("--model requires a value")
            cfg.explicit.add("model")
        elif arg in ("-s", "--scope", "-W", "--window"):  # --window is a deprecated alias for --scope
            cfg.scope = need_value("--scope requires a value")
            cfg.explicit.add("scope")
        elif arg in ("-m", "--min-remaining"):
            cfg.min_remaining = need_value("--min-remaining requires a value")
            cfg.explicit.add("min_remaining")
        elif arg in ("-i", "--poll-interval"):
            cfg.poll_interval = need_value("--poll-interval requires seconds")
            cfg.explicit.add("poll_interval")
        elif arg in ("-u", "--max-unavailable-wait"):
            cfg.max_unavailable_wait = need_value("--max-unavailable-wait requires seconds")
            cfg.explicit.add("max_unavailable_wait")
        elif arg in ("-r", "--retry-delays"):
            cfg.retry_delays = need_value("--retry-delays requires a list")
            cfg.explicit.add("retry_delays")
        elif arg in ("-R", "--no-retry"):
            cfg.retry_delays = ""
            cfg.explicit.add("retry_delays")
            i += 1
        elif arg in ("-C", "--cwd"):
            cfg.cwd = need_value("--cwd requires a directory")
        elif arg in ("-F", "--fresh"):
            cfg.exec_mode = "fresh"
            cfg.tmux_target = ""
            i += 1
        elif arg in ("-H", "--headless"):
            cfg.headless = True
            i += 1
        elif arg in ("-T", "--tmux"):
            cfg.exec_mode = "tmux"
            cfg.tmux_target = need_value("--tmux requires SESSION[:WINDOW]")
        elif arg in ("-e", "--command-template"):
            cfg.command_template = need_value("--command-template requires a template")
        elif arg in ("-y", "--auto-confirm"):
            cfg.auto_confirm = True
            i += 1
        elif arg in ("-Y", "--no-auto-confirm"):
            cfg.auto_confirm = False
            i += 1
        elif arg in ("-I", "--headless-idle-timeout"):
            os.environ["LLM_SCHEDULER_IDLE_TIMEOUT"] = need_value("--headless-idle-timeout requires seconds")
        elif arg in ("-Q", "--headless-question-timeout"):
            os.environ["LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT"] = need_value("--headless-question-timeout requires seconds")
        elif arg in ("-L", "--log-dir"):
            cfg.log_dir = Path(need_value("--log-dir requires a directory"))
        elif arg in ("-O", "--run-dir"):
            cfg.run_dir = Path(need_value("--run-dir requires a directory"))
        elif arg in ("-d", "--dry-run"):
            cfg.dry_run = True
            i += 1
        elif arg in ("-k", "--wake"):
            cfg.wake = True
            i += 1
        elif arg in ("-U", "--suspend-until-ready"):
            cfg.suspend_until_ready = True
            cfg.wake = True
            i += 1
        elif arg in ("-x", "--wake-test"):
            cfg.wake_test = True
            i += 1
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        else:
            common.err(f"unknown option: {arg}")
            print(USAGE, end="", file=sys.stderr)
            raise SystemExit(2)

    raw = os.environ.get("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS", "5") or "5"
    if not common.is_integer(raw):
        common.err("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS must be integer seconds")
        raise SystemExit(2)
    cfg.pre_suspend_confirmation_seconds = int(raw)
    return cfg


def parse_date_d(text: str) -> int | None:
    parsed = common.parse_epoch(text)
    if parsed is not None:
        return parsed
    if common.have_cmd("date"):
        proc = subprocess.run(["date", "-d", text, "+%s"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        if proc.returncode == 0:
            try:
                return int(proc.stdout.strip())
            except ValueError:
                return None
    return None


def validate_args(cfg: SchedulerConfig) -> None:
    if cfg.wake_test:
        return
    if cfg.provider not in {"codex", "claude", "copilot", "kilo", "opencode", "minimax"}:
        if not cfg.provider:
            common.err("--provider is required")
        else:
            common.err(f"invalid --provider: {cfg.provider}")
        raise SystemExit(2)
    common.validate_prompt_args(cfg.prompt_text, cfg.prompt_file)
    # A delegated provider is gated on its capacity_provider's windows, so the
    # scope must be valid for that provider (e.g. opencode->minimax allows 5h).
    common.validate_provider_scope(cfg.capacity_provider or cfg.provider, cfg.scope)
    common.validate_gate_args(cfg.cwd, cfg.min_remaining, cfg.poll_interval, cfg.max_unavailable_wait, cfg.retry_delays)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")):
        common.err("LLM_SCHEDULER_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")):
        common.err("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)
    # pre_suspend_confirmation_seconds is validated and parsed in parse_args().
    if cfg.at_time:
        cfg.not_before_epoch = parse_date_d(cfg.at_time)
        if cfg.not_before_epoch is None:
            common.err(f"could not parse --at/--not-before time: {cfg.at_time}")
            raise SystemExit(2)
    if (
        cfg.suspend_until_ready
        and os.environ.get("LLM_TOOLS_RALPH_ROBIN_ACTIVE") == "1"
        and os.environ.get("LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND") != "1"
    ):
        common.err("--suspend-until-ready is disabled inside an active ralph-robin provider run; let ralph-robin rotate and suspend only after all configured providers are rate-limited")
        raise SystemExit(common.AUTONOMY_ABORT_STATUS)


def resolve_attach_mode(cfg: SchedulerConfig) -> None:
    cfg.attached = (
        cfg.exec_mode == "fresh"
        and not cfg.headless
        and os.environ.get("LLM_SCHEDULER_HEADLESS", "0") != "1"
        and os.environ.get("LLM_SCHEDULER_NO_STREAM", "0") != "1"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and common.have_cmd("script")
    )


def safe_args_json(cfg: SchedulerConfig) -> dict[str, Any]:
    return {
        "provider": cfg.provider,
        "model": cfg.model,
        "allow_fallback": cfg.allow_fallback,
        "capacity_provider": cfg.capacity_provider,
        "scope": cfg.scope,
        "min_remaining": float(cfg.min_remaining),
        "poll_interval": int(cfg.poll_interval),
        "max_unavailable_wait": int(cfg.max_unavailable_wait),
        "retry_delays": cfg.retry_delays,
        "cwd": cfg.cwd,
        "mode": cfg.exec_mode,
        "tmux": cfg.tmux_target,
        "prompt_source": cfg.prompt_source,
        "log_dir": str(cfg.log_dir),
        "headless_idle_timeout": int(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")),
        "headless_question_timeout": int(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")),
        "auto_confirm": cfg.auto_confirm,
        "headless": cfg.headless,
        "attached": cfg.attached,
        "dry_run": cfg.dry_run,
        "wake": cfg.wake,
        "suspend_until_ready": cfg.suspend_until_ready,
        "claude_stream_json": cfg.claude_stream_json,
        "ralph_robin_active": cfg.ralph_robin_active,
        "ralph_robin_providers": cfg.ralph_robin_providers,
        "output_prefix_fields": list(cfg.output_prefix_fields),
        "output_prefix_usage_ttl": cfg.output_prefix_usage_ttl,
    }


def provider_env(cfg: SchedulerConfig) -> dict[str, str] | None:
    if not cfg.ralph_robin_active:
        return None
    env = os.environ.copy()
    env["LLM_TOOLS_RALPH_ROBIN_ACTIVE"] = "1"
    env["LLM_TOOLS_RALPH_ROBIN_SELECTED_PROVIDER"] = cfg.provider
    if cfg.ralph_robin_providers:
        env["LLM_TOOLS_RALPH_ROBIN_PROVIDERS"] = cfg.ralph_robin_providers
    env.setdefault("LLM_TOOLS_RALPH_ROBIN_SCHEDULER", "guarded")
    return env


def stream_color_enabled(stream: Any) -> bool:
    return bool(
        getattr(stream, "isatty", lambda: False)()
        and os.environ.get("TERM") != "dumb"
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("LLM_USAGE_NO_COLOR")
    )


def highlight_provider_text(raw: bytes, *, stream_name: str, enabled: bool) -> bytes:
    text = raw.decode("utf-8", "replace")
    out: list[str] = []
    for line in text.splitlines(True):
        bare = line.rstrip("\r\n")
        ending = line[len(bare):]
        stripped = bare.lstrip()
        role = ""
        if "\033[" in bare:
            out.append(line)
            continue
        if re.match(r"^(diff --git|@@\s)", stripped):
            role = "diff_hunk"
        elif stripped.startswith("+") and not stripped.startswith("+++"):
            role = "diff_add"
        elif stripped.startswith("-") and not stripped.startswith("---"):
            role = "diff_remove"
        elif re.search(r"\b(tool call|function call|exec_command|apply_patch|running command|command:)\b", stripped, re.I):
            role = "tool"
        elif re.match(r"^(\$|>|python\b|pytest\b|git\b|gh\b|./|llm-|codex\b|claude\b|copilot\b|kilo\b|opencode\b|minimax\b|mmx\b|bash\b|make\b|npm\b|pnpm\b)", stripped):
            role = "command"
        elif re.search(r"\b(error|failed|failure|rate[- ]limit|autonomous abort|blocked)\b", stripped, re.I):
            role = "error"
        elif re.search(r"\b(warn|warning|deprecated)\b", stripped, re.I):
            role = "warn"
        elif re.match(r"^[A-Z][A-Za-z0-9 _/-]{2,40}:$", stripped):
            role = "heading"
        if role:
            rendered = bare
            out.append((common.ansi_wrap(rendered, role) if enabled else rendered) + ending)
        else:
            out.append(line)
    return "".join(out).encode("utf-8", "replace")


# Providers whose CLI accepts a `--model NAME` flag we can splice into the
# default command. Kilo and MiniMax select their model through config/env, not
# a launch flag, so a pinned model there is ignored (with a warning in main).
MODEL_FLAG_PROVIDERS = frozenset({"claude", "codex", "copilot", "opencode"})


def provider_model_flags(provider: str, model: str) -> list[str]:
    """The `--model` tokens to inject for ``provider``, or empty when unsupported."""
    if model and provider in MODEL_FLAG_PROVIDERS:
        return ["--model", model]
    return []


def provider_default_argv(cfg: SchedulerConfig, prompt: str) -> list[str]:
    m = provider_model_flags(cfg.provider, cfg.model)
    if cfg.attached:
        if cfg.provider == "codex":
            return ["codex", *m, "-C", cfg.cwd, prompt]
        if cfg.provider == "claude":
            return ["claude", *m, prompt]
        if cfg.provider == "kilo":
            return ["kilo", "run", prompt]
        if cfg.provider == "opencode":
            return ["opencode", *m]
        if cfg.provider == "minimax":
            return ["mmx"]
        return ["copilot", *m, "-C", cfg.cwd, "-i", prompt]
    if cfg.provider == "codex":
        return ["codex", "exec", *m, "-C", cfg.cwd, prompt]
    if cfg.provider == "claude":
        if cfg.claude_stream_json:
            return ["claude", "--print", "--output-format", "stream-json", "--verbose", *m, prompt]
        return ["claude", "--print", *m, prompt]
    if cfg.provider == "kilo":
        # kilo uses `--dir` for the working directory. We do not inject any
        # permission-bypassing flag (e.g. `--auto`): whether a headless run may
        # act without prompting is governed by the user's own Kilo `permission`
        # config, not by this framework.
        return ["kilo", "run", "--dir", cfg.cwd, prompt]
    if cfg.provider == "opencode":
        # opencode uses `--dir` (not `-C`) for the working directory. We do not
        # inject any permission-bypassing flag: whether a headless run may act
        # without prompting is governed by the user's own OpenCode `permission`
        # config, not by this framework.
        return ["opencode", "run", *m, "--dir", cfg.cwd, prompt]
    if cfg.provider == "minimax":
        return ["mmx", "run", "--auto", "-C", cfg.cwd, prompt]
    return ["copilot", *m, "-C", cfg.cwd, "--prompt", prompt]


def command_argv(cfg: SchedulerConfig, logs: common.RunLogs, prompt: str) -> list[str]:
    if cfg.command_template:
        return common.template_argv(cfg.command_template, provider=cfg.provider, prompt=prompt, prompt_file=logs.run_dir / "prompt.txt", cwd=cfg.cwd)
    return provider_default_argv(cfg, prompt)


def scheduler_model_description(cfg: SchedulerConfig) -> str:
    if cfg.command_template:
        return "from command template"
    if cfg.model:
        return f"{cfg.provider} model {cfg.model}"
    return {
        "codex": "Codex CLI default/configured model",
        "claude": "Claude Code default/configured model",
        "copilot": "GitHub Copilot CLI default/configured model",
        "kilo": "Kilo Code CLI default/configured model",
        "opencode": "OpenCode CLI default/configured model",
        "minimax": "MiniMax default/configured model",
    }[cfg.provider]


def print_wake_test() -> None:
    print(json.dumps(common.wake_diagnostics(), indent=2))


def log_wake_plan(cfg: SchedulerConfig, logs: common.RunLogs, target_epoch: int) -> None:
    if not cfg.wake:
        return
    now = common.now_epoch()
    min_lead = int(os.environ.get("LLM_SCHEDULER_WAKE_MIN_LEAD", "120") or "120")
    if target_epoch - now < min_lead or target_epoch <= cfg.wake_armed_target:
        return
    wake_epoch = target_epoch - 120
    if wake_epoch <= now:
        wake_epoch = target_epoch
    unit = f"llm-scheduler-wake-{int(time.time())}"
    command = f"systemd-run --user --unit={unit} --on-calendar=@{wake_epoch} --timer-property=WakeSystem=true true"
    common.log_event(logs, "wake_diagnostics", common.wake_diagnostics())
    common.log_text(logs, f"wake best-effort command: {command}")
    common.log_text(logs, f"rtcwake fallback, if appropriate and run manually with privileges: sudo rtcwake -m no -t {wake_epoch}")
    if not cfg.dry_run and common.have_cmd("systemd-run"):
        proc = subprocess.run(["systemd-run", "--user", f"--unit={unit}", f"--on-calendar=@{wake_epoch}", "--timer-property=WakeSystem=true", "true"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        common.log_text(logs, f"wake systemd-run status={proc.returncode} output={proc.stdout.strip()}")
        common.log_event(logs, "wake_attempt", {"method": "systemd-run", "unit": unit, "wake_epoch": wake_epoch, "status": proc.returncode, "output": proc.stdout})
    cfg.wake_armed_target = target_epoch


def scheduler_resume_argv(cfg: SchedulerConfig, logs: common.RunLogs) -> list[str]:
    script = str(Path(__file__).resolve().parent.parent / "llm-scheduler")
    args = [
        script,
        "--provider",
        cfg.provider,
        "--prompt-file",
        str(logs.run_dir / "prompt.txt"),
        "--scope",
        cfg.scope,
        "--min-remaining",
        cfg.min_remaining,
        "--poll-interval",
        cfg.poll_interval,
        "--max-unavailable-wait",
        cfg.max_unavailable_wait,
        "--headless-idle-timeout",
        os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600"),
        "--headless-question-timeout",
        os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30"),
        "--cwd",
        cfg.cwd,
        "--auto-confirm" if cfg.auto_confirm else "--no-auto-confirm",
        "--log-dir",
        str(cfg.log_dir),
        "--run-dir",
        str(logs.run_dir),
    ]
    args += ["--retry-delays", cfg.retry_delays] if cfg.retry_delays else ["--no-retry"]
    if cfg.exec_mode == "tmux":
        args += ["--tmux", cfg.tmux_target]
    else:
        args.append("--fresh")
    if cfg.headless:
        args.append("--headless")
    if cfg.command_template:
        args += ["--command-template", cfg.command_template]
    return args


def schedule_resume_and_suspend(cfg: SchedulerConfig, logs: common.RunLogs, target_epoch: int, reason: str) -> bool:
    if not common.have_cmd("systemd-run"):
        common.log_text(logs, "suspend-until-ready unavailable: systemd-run missing")
        common.log_event(logs, "suspend_schedule_failed", {"reason": "missing-systemd-run"})
        return False
    if not common.have_cmd("systemctl"):
        common.log_text(logs, "suspend-until-ready unavailable: systemctl missing")
        common.log_event(logs, "suspend_schedule_failed", {"reason": "missing-systemctl"})
        return False
    now = common.now_epoch()
    min_lead = int(os.environ.get("LLM_SCHEDULER_SUSPEND_MIN_LEAD", "120") or "120")
    lead = target_epoch - now
    if lead < min_lead:
        common.log_text(logs, f"suspend-until-ready lead={lead}s < min_lead={min_lead}s; falling back to in-process wait")
        common.log_event(logs, "suspend_schedule_failed", {"reason": "insufficient-lead", "lead": lead, "min_lead": min_lead})
        return False
    unit = f"llm-scheduler-resume-{cfg.provider}-{int(time.time())}"
    argv = scheduler_resume_argv(cfg, logs)
    command_line = common.argv_to_command_line(argv)
    common.log_text(logs, f"suspend-until-ready scheduling unit={unit} target={target_epoch} reason={reason}")
    common.log_event(logs, "suspend_schedule_plan", {"unit": unit, "reason": reason, "target_epoch": target_epoch, "argv": argv})
    if cfg.dry_run:
        common.log_text(logs, f"dry-run suspend-until-ready command: systemd-run --user --unit={unit} --on-calendar=@{target_epoch} --timer-property=WakeSystem=true --setenv=PATH=... /bin/bash -lc '{command_line}'")
        common.log_event(logs, "suspend_schedule_dry_run", {"unit": unit, "target_epoch": target_epoch, "command": command_line})
        print(f"dry-run: would schedule {unit}.timer at epoch {target_epoch} ({common.format_local_epoch(target_epoch)}); logs: {logs.run_dir}")
        return True
    env_path = os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    py_path = os.environ.get("PYTHONPATH") or str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={unit}",
            f"--on-calendar=@{target_epoch}",
            "--timer-property=WakeSystem=true",
            f"--setenv=PATH={env_path}",
            f"--setenv=PYTHONPATH={py_path}",
            f"--working-directory={cfg.cwd}",
            sys.executable,
            "-m",
            "llm_tools.scheduler",
            *argv[1:],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    common.log_text(logs, f"suspend-until-ready systemd-run status={proc.returncode} output={proc.stdout.strip()}")
    common.log_event(logs, "suspend_schedule_attempt", {"unit": unit, "status": proc.returncode, "output": proc.stdout, "target_epoch": target_epoch})
    if proc.returncode != 0:
        return False
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{unit}.timer"], check=False)
    if active.returncode != 0:
        common.log_text(logs, f"suspend-until-ready timer {unit}.timer not active after systemd-run; aborting suspend")
        common.log_event(logs, "suspend_schedule_failed", {"reason": "timer-not-active", "unit": unit})
        return False
    if os.environ.get("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "0") == "1":
        common.log_text(logs, "suspend skipped by LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1")
        common.log_event(logs, "suspend_skipped", {"reason": "env"})
        print(f"scheduled: logs written to {logs.run_dir}")
        return True
    print_pre_suspend_confirmation(cfg, logs, target_epoch, unit, reason)
    common.log_event(logs, "pre_suspend_confirmation", {"unit": unit, "reason": reason, "target_epoch": target_epoch, "model": scheduler_model_description(cfg), "prompt": cfg.prompt_source, "cwd": cfg.cwd, "seconds": cfg.pre_suspend_confirmation_seconds})
    common.log_text(logs, "systemctl suspend requested")
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "suspend"], check=False)
    print(f"scheduled: logs written to {logs.run_dir}")
    return True


def print_pre_suspend_confirmation(cfg: SchedulerConfig, logs: common.RunLogs, target_epoch: int, unit: str, reason: str) -> None:
    prompt_display = f"{cfg.prompt_file} (saved copy: {logs.run_dir / 'prompt.txt'})" if cfg.prompt_file else f"inline prompt (saved copy: {logs.run_dir / 'prompt.txt'})"
    print("suspend-until-ready armed")
    print(f"  wake/run at: {common.format_local_epoch(target_epoch)} (epoch {target_epoch})")
    print(f"  reason: {reason}")
    print(f"  provider: {cfg.provider}")
    print(f"  model: {scheduler_model_description(cfg)}")
    print(f"  prompt: {prompt_display}")
    print(f"  directory: {cfg.cwd}")
    print(f"  timer unit: {unit}")
    print(f"  logs: {logs.run_dir}")
    if cfg.pre_suspend_confirmation_seconds > 0:
        print(f"suspending in {cfg.pre_suspend_confirmation_seconds} seconds...")
        time.sleep(cfg.pre_suspend_confirmation_seconds)


def is_undetermined_reason(reason: str) -> bool:
    return reason != "rate-limited"


def sleep_until(target: int) -> None:
    seconds = target - common.now_epoch()
    if seconds > 0:
        time.sleep(seconds)


def wait_until_usable(cfg: SchedulerConfig, logs: common.RunLogs) -> None:
    undetermined_since: int | None = None
    not_before = cfg.not_before_epoch if cfg.not_before_epoch is not None else common.now_epoch()
    while True:
        now = common.now_epoch()
        if now < not_before:
            common.log_text(logs, f"not-before gate active until {not_before}")
            common.log_event(logs, "wait_decision", {"reason": "not-before", "wait_until": not_before})
            log_wake_plan(cfg, logs, not_before)
            if cfg.suspend_until_ready:
                if schedule_resume_and_suspend(cfg, logs, not_before, "not-before"):
                    common.log_event(logs, "final", {"status": "scheduled"})
                    raise SystemExit(0)
                print("error: suspend scheduling failed; falling back to in-process wait", file=sys.stderr)
                common.log_event(logs, "suspend_schedule_fallback", {"reason": "schedule_resume_and_suspend failed", "gate": "not-before"})
            if cfg.dry_run:
                return
            sleep_until(not_before)
        snapshot, decision = common.usage_snapshot_and_decision(
            cfg.provider,
            cfg.capacity_provider or None,
            cfg.scope,
            cfg.min_remaining,
            cfg.poll_interval,
            model=cfg.model or None,
            allow_fallback=cfg.allow_fallback,
        )
        common.log_event(logs, "usage_snapshot", snapshot)
        common.log_event(logs, "usage_decision", decision)
        reason = str(decision.get("reason"))
        common.log_text(logs, f"usage decision: {reason}")
        if decision.get("usable") is True:
            return
        if is_undetermined_reason(reason):
            if undetermined_since is None:
                undetermined_since = now
            waited = now - undetermined_since
            max_wait = int(cfg.max_unavailable_wait)
            if max_wait > 0 and waited >= max_wait:
                common.log_text(logs, f"usage undeterminable for {waited}s (reason={reason}); proceeding optimistically")
                common.log_event(logs, "optimistic_proceed", {"reason": reason, "waited": waited, "max_unavailable_wait": max_wait})
                return
            if cfg.suspend_until_ready:
                common.log_text(logs, f"usage undeterminable (reason={reason}) in suspend mode; proceeding optimistically instead of suspend-polling")
                common.log_event(logs, "optimistic_proceed", {"reason": reason, "waited": waited, "max_unavailable_wait": max_wait, "suspend_mode": True})
                return
        else:
            undetermined_since = None
        target = decision.get("wait_until") or (common.now_epoch() + int(cfg.poll_interval))
        target = int(target)
        log_wake_plan(cfg, logs, target)
        if cfg.suspend_until_ready:
            if schedule_resume_and_suspend(cfg, logs, target, reason):
                common.log_event(logs, "final", {"status": "scheduled"})
                raise SystemExit(0)
            print("error: suspend scheduling failed; falling back to in-process wait", file=sys.stderr)
            common.log_event(logs, "suspend_schedule_fallback", {"reason": "schedule_resume_and_suspend failed", "gate": reason})
        if cfg.dry_run:
            return
        sleep_until(target)


def run_fresh_attached(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    command_line = common.argv_to_command_line(argv)
    proc = subprocess.run(["script", "--return", "--quiet", "--flush", "--command", command_line, str(output_file)], cwd=cfg.cwd, env=provider_env(cfg), check=False)
    status_file.write_text(str(proc.returncode), encoding="utf-8")
    common.clean_capture_file(output_file)
    return proc.returncode


def run_fresh_headless(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    if cfg.claude_stream_json:
        return run_fresh_claude_stream_json(cfg, argv, output_file, status_file)
    if cfg.exact_stdout:
        return run_fresh_exact_stdout(cfg, argv, output_file, status_file)
    status, _text = common.run_pty_capture(
        argv,
        Path(cfg.cwd),
        int(os.environ.get("LLM_SCHEDULER_PTY_TIMEOUT", "3600") or "3600"),
        stream=os.environ.get("LLM_SCHEDULER_NO_STREAM", "0") != "1",
        auto_confirm=cfg.auto_confirm,
        output_path=output_file,
        status_path=status_file,
        idle_timeout=int(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600") or "600"),
        question_idle_timeout=int(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30") or "30"),
        env=provider_env(cfg),
    )
    return status


def render_claude_content_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    block_type = str(block.get("type", ""))
    if block_type == "text":
        return str(block.get("text", ""))
    if block_type == "tool_use":
        name = str(block.get("name") or "tool")
        rendered = f"Tool call: {name}\n"
        tool_input = block.get("input")
        if tool_input not in (None, "", {}, []):
            try:
                rendered += json.dumps(tool_input, ensure_ascii=False, indent=2) + "\n"
            except (TypeError, ValueError):
                rendered += str(tool_input) + "\n"
        return rendered
    if block_type == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            rendered = "".join(render_claude_content_block(item) for item in content)
        elif content is None:
            rendered = ""
        else:
            rendered = str(content)
        if not rendered:
            return ""
        prefix = "Tool error:\n" if block.get("is_error") else "Tool result:\n"
        return prefix + rendered
    return ""


class ClaudeStreamRenderer:
    def __init__(self) -> None:
        self.rendered_assistant_text = False

    def render_event(self, event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                return ""
            content = message.get("content")
            blocks = content if isinstance(content, list) else [content]
            rendered = "".join(render_claude_content_block(block) for block in blocks)
            has_text = any(
                (isinstance(block, str) and bool(block))
                or (isinstance(block, dict) and block.get("type") == "text" and bool(block.get("text")))
                for block in blocks
            )
            if has_text:
                self.rendered_assistant_text = True
            return rendered
        if event_type == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                return ""
            content = message.get("content")
            blocks = content if isinstance(content, list) else [content]
            return "".join(render_claude_content_block(block) for block in blocks)
        if event_type in {"assistant_delta", "content_block_delta"}:
            delta = event.get("delta")
            if isinstance(delta, dict):
                text = delta.get("text")
                if isinstance(text, str):
                    self.rendered_assistant_text = True
                    return text
        if event_type == "result" and not self.rendered_assistant_text:
            result = event.get("result")
            if isinstance(result, str):
                return result
        return ""

    def render_line(self, line: bytes) -> bytes:
        stripped = line.strip()
        if not stripped:
            return b""
        try:
            event = json.loads(stripped.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return b""
        rendered = self.render_event(event)
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"
        return rendered.encode("utf-8", "replace")


class ProgressGuard:
    """Detect no-progress and waiting-for-input conditions in a captured run.

    Distinguishes a model that is merely thinking (no output, allowed up to the
    full idle timeout) from one that has asked a question or hit an interactive
    credit/limit prompt and is now stalled (terminated quickly). The orchestrator
    forcefully kills such a run and treats it as an autonomous abort so it can
    re-route to another provider.
    """

    def __init__(self) -> None:
        self.idle_timeout = int(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600") or "600")
        self.question_idle_timeout = int(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30") or "30")
        now = time.time()
        self.last_progress = now
        self.question_seen_at: float | None = None

    def note_output(self, combined_tail: str) -> bool:
        """Record fresh output. Returns True if an interactive prompt was seen."""
        self.last_progress = time.time()
        if any(pattern.search(combined_tail) for pattern in common.BLOCKING_PROMPT_PATTERNS):
            return True
        # Only treat the run as "waiting on a question" when the most recent
        # output still ends in a question. If the model asked something and then
        # resumed producing output, clear the pending-question state so we do not
        # kill a provider that is actually making progress.
        if common.QUESTION_LINE_RE.search(combined_tail[-4000:]):
            self.question_seen_at = time.time()
        else:
            self.question_seen_at = None
        return False

    def overdue(self) -> str | None:
        """Return an abort reason when the run has stalled, else None."""
        now = time.time()
        if self.idle_timeout > 0 and now - self.last_progress > self.idle_timeout:
            return f"no output progress for {self.idle_timeout}s"
        if (
            self.question_idle_timeout > 0
            and self.question_seen_at is not None
            and now - self.question_seen_at > self.question_idle_timeout
        ):
            return f"question required a response for {self.question_idle_timeout}s"
        return None


def run_fresh_claude_stream_json(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    import os
    import select
    import signal

    proc = subprocess.Popen(argv, cwd=cfg.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=provider_env(cfg))
    assert proc.stdout is not None and proc.stderr is not None
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    open_fds = {stdout_fd: "stdout", stderr_fd: "stderr"}
    stdout_color = stream_color_enabled(sys.stdout)
    stderr_color = stream_color_enabled(sys.stderr)
    stdout_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, usage_ttl=cfg.output_prefix_usage_ttl)
    stderr_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, usage_ttl=cfg.output_prefix_usage_ttl)
    renderer = ClaudeStreamRenderer()
    stdout_buffer = b""
    combined_parts: list[bytes] = []
    start = time.time()
    timeout = int(os.environ.get("LLM_SCHEDULER_PTY_TIMEOUT", "3600") or "3600")
    exit_code = 124
    blocking = False
    abort_reason = ""
    guard = ProgressGuard()

    def scan_tail() -> bool:
        tail = common.strip_ansi(b"".join(combined_parts)[-8000:].decode("utf-8", "replace"))
        return guard.note_output(tail)

    while open_fds:
        if time.time() - start > timeout:
            break
        reason = guard.overdue()
        if reason:
            abort_reason = reason
            exit_code = common.AUTONOMY_ABORT_STATUS
            break
        ready, _, _ = select.select(list(open_fds), [], [], 0.2)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                if open_fds.get(fd) == "stdout" and stdout_buffer:
                    rendered = renderer.render_line(stdout_buffer)
                    if rendered:
                        combined_parts.append(rendered)
                        try:
                            sys.stdout.buffer.write(stdout_ts.apply(highlight_provider_text(rendered, stream_name="stdout", enabled=stdout_color)))
                            sys.stdout.buffer.flush()
                        except OSError:
                            pass
                        if scan_tail():
                            blocking = True
                    stdout_buffer = b""
                open_fds.pop(fd, None)
                continue
            guard.last_progress = time.time()
            if open_fds.get(fd) == "stdout":
                stdout_buffer += chunk
                lines = stdout_buffer.splitlines(keepends=True)
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    stdout_buffer = lines.pop()
                else:
                    stdout_buffer = b""
                for line in lines:
                    rendered = renderer.render_line(line)
                    if not rendered:
                        continue
                    combined_parts.append(rendered)
                    try:
                        sys.stdout.buffer.write(stdout_ts.apply(highlight_provider_text(rendered, stream_name="stdout", enabled=stdout_color)))
                        sys.stdout.buffer.flush()
                    except OSError:
                        pass
                    if scan_tail():
                        blocking = True
                        break
            else:
                combined_parts.append(chunk)
                try:
                    sys.stderr.buffer.write(stderr_ts.apply(highlight_provider_text(chunk, stream_name="stderr", enabled=stderr_color)))
                    sys.stderr.buffer.flush()
                except OSError:
                    pass
                if scan_tail():
                    blocking = True
            if blocking:
                break
        if blocking:
            abort_reason = "interactive prompt detected"
            exit_code = common.AUTONOMY_ABORT_STATUS
            break
        polled = proc.poll()
        if polled is not None and not open_fds:
            exit_code = polled
            break
    if blocking or exit_code in (124, common.AUTONOMY_ABORT_STATUS):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                break
            try:
                proc.send_signal(sig)
            except OSError:
                break
            time.sleep(0.2)
    if proc.poll() is not None and not blocking and exit_code == 124:
        exit_code = int(proc.returncode)
    if abort_reason:
        abort_line = f"\nllm-scheduler: autonomous abort: {abort_reason}\n".encode()
        combined_parts.append(abort_line)
        try:
            sys.stderr.buffer.write(stderr_ts.apply(highlight_provider_text(abort_line, stream_name="stderr", enabled=stderr_color)))
            sys.stderr.buffer.flush()
        except OSError:
            pass
    output_file.write_text(b"".join(combined_parts).decode("utf-8", "replace"), encoding="utf-8")
    status_file.write_text(str(exit_code), encoding="utf-8")
    return exit_code


def run_fresh_exact_stdout(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    import os
    import select
    import signal

    proc = subprocess.Popen(argv, cwd=cfg.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=provider_env(cfg))
    assert proc.stdout is not None and proc.stderr is not None
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    open_fds = {stdout_fd: "stdout", stderr_fd: "stderr"}
    stdout_color = stream_color_enabled(sys.stdout)
    stderr_color = stream_color_enabled(sys.stderr)
    stdout_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, usage_ttl=cfg.output_prefix_usage_ttl)
    stderr_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, usage_ttl=cfg.output_prefix_usage_ttl)
    stdout_parts: list[bytes] = []
    combined_parts: list[bytes] = []
    start = time.time()
    timeout = int(os.environ.get("LLM_SCHEDULER_PTY_TIMEOUT", "3600") or "3600")
    exit_code = 124
    blocking = False
    abort_reason = ""
    guard = ProgressGuard()
    while open_fds:
        if time.time() - start > timeout:
            break
        reason = guard.overdue()
        if reason:
            abort_reason = reason
            exit_code = common.AUTONOMY_ABORT_STATUS
            break
        ready, _, _ = select.select(list(open_fds), [], [], 0.2)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                open_fds.pop(fd, None)
                continue
            combined_parts.append(chunk)
            if open_fds.get(fd) == "stdout":
                stdout_parts.append(chunk)
                try:
                    sys.stdout.buffer.write(stdout_ts.apply(highlight_provider_text(chunk, stream_name="stdout", enabled=stdout_color)))
                    sys.stdout.buffer.flush()
                except OSError:
                    pass
            else:
                try:
                    sys.stderr.buffer.write(stderr_ts.apply(highlight_provider_text(chunk, stream_name="stderr", enabled=stderr_color)))
                    sys.stderr.buffer.flush()
                except OSError:
                    pass
            tail = common.strip_ansi(b"".join(combined_parts)[-8000:].decode("utf-8", "replace"))
            if guard.note_output(tail):
                blocking = True
                break
        if blocking:
            abort_reason = "interactive prompt detected"
            exit_code = common.AUTONOMY_ABORT_STATUS
            break
        polled = proc.poll()
        if polled is not None and not open_fds:
            exit_code = polled
            break
    if blocking or exit_code in (124, common.AUTONOMY_ABORT_STATUS):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                break
            try:
                proc.send_signal(sig)
            except OSError:
                break
            time.sleep(0.2)
    if proc.poll() is not None and not blocking and exit_code == 124:
        exit_code = int(proc.returncode)
    if abort_reason:
        combined_parts.append(f"\nllm-scheduler: autonomous abort: {abort_reason}\n".encode())
    output_file.write_text(b"".join(combined_parts).decode("utf-8", "replace"), encoding="utf-8")
    status_file.write_text(str(exit_code), encoding="utf-8")
    return exit_code


def run_tmux(cfg: SchedulerConfig, logs: common.RunLogs, argv: list[str], output_file: Path, status_file: Path) -> int:
    if not common.have_cmd("tmux"):
        output_file.write_text("tmux not installed\n", encoding="utf-8")
        status_file.write_text("127", encoding="utf-8")
        return 127
    if ":" in cfg.tmux_target:
        session, window = cfg.tmux_target.split(":", 1)
        if not session or not window:
            output_file.write_text(f"invalid tmux target: {cfg.tmux_target}\n", encoding="utf-8")
            status_file.write_text("2", encoding="utf-8")
            return 2
    else:
        session, window = cfg.tmux_target, "llm-scheduler"
    target = f"{session}:{window}"
    command_line = common.argv_to_command_line(argv)
    cmd_file = logs.run_dir / "tmux-command.sh"
    guard_exports = "export LLM_TOOLS_RALPH_ROBIN_ACTIVE=1\nexport LLM_TOOLS_RALPH_ROBIN_SCHEDULER=guarded\n" if cfg.ralph_robin_active else ""
    cmd_file.write_text(
        "#!/usr/bin/env bash\n"
        f"# tmux target: {target}\n"
        "set -euo pipefail\n"
        f"cd {shlex.quote(cfg.cwd)}\n"
        f"{guard_exports}"
        "set +e\n"
        f"{command_line}\n"
        "status=$?\n"
        f"printf %s \"$status\" > {shlex.quote(str(status_file))}\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    cmd_file.chmod(0o700)
    invocation = f"bash {shlex.quote(str(cmd_file))}"
    if subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", window, invocation], check=False)
    else:
        windows = subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#W"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout.splitlines()
        if window not in windows:
            subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", window, invocation], check=False)
        else:
            subprocess.run(["tmux", "send-keys", "-t", target, invocation, "C-m"], check=False)
    waited = 0
    timeout = int(os.environ.get("LLM_SCHEDULER_TMUX_TIMEOUT", "3600") or "3600")
    while (not status_file.is_file() or status_file.stat().st_size == 0) and waited < timeout:
        pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", target], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        output_file.write_text(pane.stdout, encoding="utf-8")
        time.sleep(1)
        waited += 1
    pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", target], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
    output_file.write_text(pane.stdout, encoding="utf-8")
    if not status_file.is_file() or status_file.stat().st_size == 0:
        status_file.write_text("124", encoding="utf-8")
    try:
        return int(status_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return 124


def submit_once(cfg: SchedulerConfig, logs: common.RunLogs, attempt: int, argv: list[str]) -> int:
    output_file = logs.run_dir / f"attempt-{attempt}.out"
    status_file = logs.run_dir / f"attempt-{attempt}.status"
    common.log_text(logs, f"attempt {attempt} command: {json.dumps(argv, separators=(',', ':'))}")
    common.log_text(logs, f"attempt {attempt} output: {output_file}")
    common.log_text(logs, f"tail attempt {attempt}: tail -f '{output_file}'")
    common.log_event(logs, "command_plan", {"mode": cfg.exec_mode, "argv": argv})
    if cfg.exec_mode == "tmux":
        status = run_tmux(cfg, logs, argv, output_file, status_file)
    elif cfg.attached:
        status = run_fresh_attached(cfg, argv, output_file, status_file)
    else:
        status = run_fresh_headless(cfg, argv, output_file, status_file)
    if not status_file.is_file() or status_file.stat().st_size == 0:
        status_file.write_text("124", encoding="utf-8")
    if not output_file.exists():
        output_file.touch()
    try:
        status = int(status_file.read_text(encoding="utf-8").strip())
    except ValueError:
        status = 124
    output = output_file.read_text(encoding="utf-8", errors="replace")
    with logs.text_log.open("a", encoding="utf-8") as fh:
        fh.write(output)
    common.log_event(logs, "attempt_result", {"attempt": attempt, "status": status, "output": output})
    if status == common.AUTONOMY_ABORT_STATUS:
        common.log_event(logs, "autonomy_abort", {"attempt": attempt, "output": output})
        return common.AUTONOMY_ABORT_STATUS
    return 1 if common.output_is_retryable(status, output, cfg.attached, trust_clean_exit=cfg.ralph_robin_active) else 0


# Config keys (merged defaults + [scheduler]) that map to a string cfg field.
_SCHEDULER_CONFIG_FIELDS = ("scope", "min_remaining", "poll_interval", "max_unavailable_wait", "retry_delays")


def _coerce_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def apply_config(cfg: SchedulerConfig, env: dict[str, str] | None = None) -> None:
    """Fill config-file values for any flag the user did not pass explicitly.

    Precedence: built-in defaults < config file < CLI flags. The per-provider
    routing policy supplies the pinned ``model`` and ``allow_fallback``.
    """
    conf = toolconfig.load_config(env)
    if not conf:
        return
    tool = toolconfig.merged_tool_config(conf, "scheduler")
    if not cfg.provider and tool.get("provider"):
        cfg.provider = str(tool["provider"])
    for key in _SCHEDULER_CONFIG_FIELDS:
        if key not in cfg.explicit and tool.get(key) is not None:
            setattr(cfg, key, _coerce_scalar(tool[key]))
    if cfg.provider:
        policy = toolconfig.provider_policy(conf, cfg.provider)
        cfg.allow_fallback = policy.allow_fallback
        if policy.capacity_provider:
            cfg.capacity_provider = policy.capacity_provider
        if "model" not in cfg.explicit and policy.model:
            cfg.model = policy.model
        if "scope" not in cfg.explicit and policy.scope:
            cfg.scope = policy.scope
        if "min_remaining" not in cfg.explicit and policy.min_remaining:
            cfg.min_remaining = policy.min_remaining
        if cfg.model and cfg.provider not in MODEL_FLAG_PROVIDERS:
            common.err(f"warning: model pinning is not supported for provider '{cfg.provider}'; ignoring model={cfg.model}")
            cfg.model = ""


def main(argv: list[str] | None = None) -> int:
    common.migrate_legacy_cache_dirs()
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    apply_config(cfg)
    validate_args(cfg)
    if cfg.wake_test:
        print_wake_test()
        return 0
    resolve_attach_mode(cfg)
    logs = common.setup_run_logs(cfg.log_dir, cfg.provider or "wake", cfg.provider or "", cfg.run_dir)
    prompt, prompt_sha = common.load_prompt(cfg.prompt_text, cfg.prompt_file, logs)
    cfg.prompt_text = prompt
    common.log_text(logs, f"start provider={cfg.provider} cwd={cfg.cwd} attached={1 if cfg.attached else 0}")
    common.log_event(logs, "start", safe_args_json(cfg))
    common.log_event(logs, "prompt", {"source": cfg.prompt_source, "sha256": prompt_sha, "prompt": prompt})
    wait_until_usable(cfg, logs)
    argv_resolved = command_argv(cfg, logs, prompt)
    common.log_event(logs, "resolved_command", {"argv": argv_resolved})
    if cfg.dry_run:
        common.log_text(logs, "dry-run complete")
        common.log_event(logs, "final", {"status": "dry-run"})
        print(f"dry-run: logs written to {logs.run_dir}")
        return 0
    retry_delays = [int(x) for x in cfg.retry_delays.split(",") if x] if cfg.retry_delays else []
    attempt = 1
    result = submit_once(cfg, logs, attempt, argv_resolved)
    if result == 0:
        common.log_text(logs, "final status: success")
        common.log_event(logs, "final", {"status": "success"})
        print(f"success: logs written to {logs.run_dir}")
        return 0
    if result == common.AUTONOMY_ABORT_STATUS:
        common.log_text(logs, "final status: autonomy-abort")
        common.log_event(logs, "final", {"status": "autonomy-abort"})
        print(f"autonomy-abort: logs written to {logs.run_dir}", file=sys.stderr)
        return common.AUTONOMY_ABORT_STATUS
    for delay in retry_delays:
        common.log_text(logs, f"retry after {delay}s")
        common.log_event(logs, "retry", {"after_attempt": attempt, "delay": delay})
        time.sleep(delay)
        attempt += 1
        result = submit_once(cfg, logs, attempt, argv_resolved)
        if result == 0:
            common.log_text(logs, "final status: success")
            common.log_event(logs, "final", {"status": "success"})
            print(f"success: logs written to {logs.run_dir}")
            return 0
        if result == common.AUTONOMY_ABORT_STATUS:
            common.log_text(logs, "final status: autonomy-abort")
            common.log_event(logs, "final", {"status": "autonomy-abort"})
            print(f"autonomy-abort: logs written to {logs.run_dir}", file=sys.stderr)
            return common.AUTONOMY_ABORT_STATUS
    common.log_text(logs, "final status: failed")
    common.log_event(logs, "final", {"status": "failed"})
    print(f"failed: logs written to {logs.run_dir}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
