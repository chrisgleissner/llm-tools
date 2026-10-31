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
from typing import Any, Callable

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
codex exec, copilot --prompt, kilo run --dir, opencode run, mmx text chat)
runs on a captured PTY instead. MiniMax has no interactive form; it always
runs headless.

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
    # Optional inherited route id (set by ``ralph-robin`` when a route
    # is selected). When present, the scheduler uses
    # ``llm_tools.routes.usage_snapshot_and_decision_for_route`` to gate
    # on the route's capacity / cost policy instead of the legacy
    # provider-only decision. The launch CLI is still resolved from
    # the route's ``provider`` and the model pin from ``route.model``.
    # Never set by the public CLI; set internally by ralph.
    route_id: str = ""
    scope: str = "auto"
    min_remaining: str = "1"
    poll_interval: str = "60"
    max_unavailable_wait: str = "900"
    retry_delays: str = "60,180,600"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    exec_mode: str = "fresh"
    tmux_target: str = ""
    # Bridge-owned native terminal station. These stay empty for ordinary CLI
    # use, preserving the public --tmux behaviour and global tmux socket.
    tmux_socket: str = ""
    tmux_pane: str = ""
    native_tui: bool = False
    process_started: Callable[[int], None] | None = field(default=None, repr=False, compare=False)
    process_observer: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    # Internal Bridge callback used to interrupt long capacity waits promptly.
    should_interrupt_wait: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    command_argv_transform: Callable[[list[str]], list[str]] | None = field(default=None, repr=False, compare=False)
    # Internal environment additions supplied by a managed Bridge operation.
    env_overrides: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    max_output_bytes: int = 0
    max_output_rotations: int = 0
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
    # Internal: file the headless provider reads its prompt from (set by
    # command_argv), so the prompt is not in the process arguments.
    prompt_stdin: str = field(default="", repr=False, compare=False)
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
    if cfg.provider not in {"codex", "claude", "copilot", "kilo", "opencode", "minimax", "zai"}:
        if not cfg.provider:
            common.err("--provider is required")
        else:
            common.err(f"invalid --provider: {cfg.provider}")
        raise SystemExit(2)
    enabled = toolconfig.enabled_providers()
    if enabled is not None and cfg.provider not in enabled:
        common.err(f"provider {cfg.provider} is disabled by [usage].providers / LLM_USAGE_PROVIDERS (enabled: {', '.join(sorted(enabled))})")
        raise SystemExit(2)
    common.validate_prompt_args(cfg.prompt_text, cfg.prompt_file)
    if model_missing_slash(cfg.provider, cfg.model):
        common.err(f"warning: {cfg.provider} requires --model in 'provider/model' format (e.g. 'zai/glm-4.7'); model={cfg.model!r} is missing the '/' and the launch CLI will likely reject it")
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
    if cfg.exec_mode == "tmux" and cfg.native_tui:
        cfg.attached = True
        return
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
        "tmux_socket": cfg.tmux_socket,
        "tmux_pane": cfg.tmux_pane,
        "native_tui": cfg.native_tui,
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


def _record_spawned_process(cfg: SchedulerConfig, process_pid: int) -> None:
    callback = cfg.process_started
    if callback is None:
        return
    try:
        callback(process_pid)
    except BaseException:
        try:
            os.killpg(process_pid, 9)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(process_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        raise


def _record_popen_process(cfg: SchedulerConfig, proc: subprocess.Popen[Any]) -> None:
    try:
        _record_spawned_process(cfg, proc.pid)
    except BaseException:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise


def provider_env(cfg: SchedulerConfig) -> dict[str, str] | None:
    """Environment for the provider process: other providers' credentials removed.

    Returns None (inherit unchanged) only when nothing needs to change.
    """
    foreign = common.credential_variables_to_remove(os.environ, cfg.provider)
    exposes_root = common.package_root() in os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if not cfg.ralph_robin_active and not cfg.env_overrides and not foreign and not exposes_root:
        return None
    env = common.scrubbed_environment(os.environ, cfg.provider)
    if cfg.ralph_robin_active:
        env["LLM_TOOLS_RALPH_ROBIN_ACTIVE"] = "1"
        env["LLM_TOOLS_RALPH_ROBIN_SELECTED_PROVIDER"] = cfg.provider
        if cfg.route_id:
            env["LLM_TOOLS_RALPH_ROBIN_SELECTED_ROUTE"] = cfg.route_id
        if cfg.ralph_robin_providers:
            env["LLM_TOOLS_RALPH_ROBIN_PROVIDERS"] = cfg.ralph_robin_providers
        env.setdefault("LLM_TOOLS_RALPH_ROBIN_SCHEDULER", "guarded")
    for key, value in cfg.env_overrides.items():
        if not re.fullmatch(r"LLM_BRIDGE_[A-Z0-9_]+", key):
            raise ValueError(f"invalid managed provider environment name: {key}")
        env[key] = str(value)
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
# default command. Kilo and OpenCode accept ``-m, --model provider/model``;
# that lets the same harness switch between Claude, OpenAI, z.ai (zai/glm-*),
# etc. without dropping a config file. MiniMax and zai (as a no-launch
# capacity provider) select their model through routes, not the launch
# flag, so a direct ``--provider zai`` invocation would ignore a model pin
# (with a warning in main).
MODEL_FLAG_PROVIDERS = frozenset({"claude", "codex", "copilot", "kilo", "opencode"})

# Kilo and OpenCode both require the ``-m/--model`` value to be a
# ``provider/model`` pair; a bare model name (no slash) is not rejected up
# front, it is silently misparsed as a provider id with an empty model. Kilo
# then fails at launch with a cryptic ``Model not found: <name>/`` (the
# trailing slash is Kilo echoing the empty model half back). Flagging this at
# config-resolution time catches the mistake before it burns through several
# real provider-rotation attempts.
SLASH_MODEL_PROVIDERS = frozenset({"kilo", "opencode"})

ZAI_LAUNCH_HINT = (
    "--provider zai has no launch CLI. Launch Kilo or OpenCode instead: use a route "
    '(e.g. [routes.kilo-zai-glm-47] with capacity.policy = "delegate" and provider = "zai") '
    'or set [providers.kilo] capacity_provider = "zai"'
)


def provider_model_flags(provider: str, model: str) -> list[str]:
    """The `--model` tokens to inject for ``provider``, or empty when unsupported."""
    if model and provider in MODEL_FLAG_PROVIDERS:
        return ["--model", model]
    return []


def model_missing_slash(provider: str, model: str | None) -> bool:
    """True when ``model`` is pinned for a provider that requires ``provider/model`` but lacks the separator."""
    return bool(model) and provider in SLASH_MODEL_PROVIDERS and "/" not in model


def provider_default_argv(cfg: SchedulerConfig, prompt: str) -> list[str]:
    m = provider_model_flags(cfg.provider, cfg.model)
    if cfg.attached:
        if cfg.provider == "codex":
            return ["codex", *m, "-C", cfg.cwd, prompt]
        if cfg.provider == "claude":
            return ["claude", *m, prompt]
        if cfg.provider == "kilo":
            return ["kilo", *m, "--prompt", prompt]
        if cfg.provider == "opencode":
            return ["opencode", *m, "--prompt", prompt]
        if cfg.provider == "minimax":
            common.err("MiniMax mmx exposes text chat, not a native engineering session for LLM Bridge")
            raise SystemExit(2)
        if cfg.provider == "zai":
            # zai is a capacity-only provider: it is launched through a
            # route (``provider=kilo``/``opencode``), not directly. A bare
            # ``--provider zai`` invocation has no launch CLI, so we surface
            # the contract here.
            common.err(ZAI_LAUNCH_HINT)
            raise SystemExit(2)
        return ["copilot", *m, "-C", cfg.cwd, "-i", prompt]
    if cfg.provider == "codex":
        return ["codex", "exec", *m, "-C", cfg.cwd, prompt]
    if cfg.provider == "claude":
        if cfg.claude_stream_json:
            return ["claude", "--print", "--output-format", "stream-json", "--verbose", *m, prompt]
        return ["claude", "--print", *m, prompt]
    if cfg.provider == "kilo":
        # kilo uses `--dir` for the working directory and ``-m, --model
        # provider/model`` to pick a model (e.g. ``-m zai/glm-4.7``). We
        # do not inject any permission-bypassing flag (e.g. ``--auto``):
        # whether a headless run may act without prompting is governed by
        # the user's own Kilo `permission` config, not by this framework.
        return ["kilo", "run", *m, "--dir", cfg.cwd, prompt]
    if cfg.provider == "opencode":
        # opencode uses `--dir` (not `-C`) for the working directory. The
        # model flag uses the same ``provider/model`` convention as Kilo
        # (e.g. ``-m zai/glm-5.2``). We do not inject any
        # permission-bypassing flag: whether a headless run may act
        # without prompting is governed by the user's own OpenCode
        # `permission` config, not by this framework.
        return ["opencode", "run", *m, "--dir", cfg.cwd, prompt]
    if cfg.provider == "minimax":
        return ["mmx", "text", "chat", "--message", prompt]
    if cfg.provider == "zai":
        common.err(ZAI_LAUNCH_HINT)
        raise SystemExit(2)
    return ["copilot", *m, "-C", cfg.cwd, "--prompt", prompt]


def command_argv(cfg: SchedulerConfig, logs: common.RunLogs, prompt: str) -> list[str]:
    if cfg.command_template:
        argv = common.template_argv(cfg.command_template, provider=cfg.provider, prompt=prompt, prompt_file=logs.run_dir / "prompt.txt", cwd=cfg.cwd)
    else:
        argv = provider_default_argv(cfg, prompt)
    if cfg.command_argv_transform is not None:
        argv = cfg.command_argv_transform(argv)
    argv = prompt_to_stdin(cfg, logs, argv, prompt)
    # The provider is started in the mission folder. Resolve it now, through
    # absolute PATH entries only, so a file of that name in the folder cannot
    # take its place.
    return [common.resolve_trusted_executable(argv[0]), *argv[1:]] if argv else argv


def prompt_to_stdin(cfg: SchedulerConfig, logs: common.RunLogs, argv: list[str], prompt: str) -> list[str]:
    """Move the prompt of a headless Codex or Claude run from argv to stdin.

    Arguments of a running process are readable by every process of the same
    user through ``/proc/<pid>/cmdline`` (and by ``ps``). ``codex exec -`` and
    ``claude --print`` read the prompt from stdin, so the run reads the 0600
    ``prompt.txt`` of its run directory instead. Native (tmux) launches and
    attached terminals need the prompt as an argument and keep it there; custom
    command templates and other providers are left unchanged.
    """
    cfg.prompt_stdin = ""
    if cfg.command_template or cfg.command_argv_transform is not None or cfg.attached or cfg.exec_mode == "tmux":
        return argv
    if not prompt or not argv or argv[-1] != prompt or not (logs.run_dir / "prompt.txt").is_file():
        return argv
    if cfg.provider == "codex" and argv[:2] == ["codex", "exec"]:
        rewritten = [*argv[:-1], "-"]
    elif cfg.provider == "claude" and argv[:1] == ["claude"] and "--print" in argv:
        rewritten = argv[:-1]
    else:
        return argv
    cfg.prompt_stdin = str(logs.run_dir / "prompt.txt")
    return rewritten


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
        "zai": "Z.ai capacity source (launch via [routes.<id>])",
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
    py_path = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH", ""))))
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
            *common.python_module_argv("llm_tools.scheduler", *argv[1:]),
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


def _sleep_interruptibly(cfg: SchedulerConfig, target: int) -> bool:
    """Sleep to an epoch in bounded slices; return False on a mission request."""
    while True:
        if cfg.should_interrupt_wait is not None and cfg.should_interrupt_wait():
            return False
        remaining = target - common.now_epoch()
        if remaining <= 0:
            return True
        time.sleep(min(float(remaining), 1.0))


def _route_or_provider_decision(cfg: SchedulerConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """Snapshot + decision for ``cfg``, honouring an inherited route id.

    When ``cfg.route_id`` is set, the scheduler uses the route-level
    decision helper so an opaque / delegate / fixed-subscription
    route can be gated on its real capacity policy. Otherwise the
    legacy provider decision path is used (no behaviour change for
    existing callers).
    """
    if cfg.route_id:
        from .routes import usage_snapshot_and_decision_for_route

        try:
            conf = toolconfig.load_config()
        except (OSError, SystemExit):
            conf = {}
        route = toolconfig.route_policy(conf, cfg.route_id)
        if route is not None:
            return usage_snapshot_and_decision_for_route(
                route,
                cfg.scope,
                cfg.min_remaining,
                cfg.poll_interval,
                model=cfg.model or None,
                allow_fallback=cfg.allow_fallback,
            )
    return common.usage_snapshot_and_decision(
        cfg.provider,
        cfg.capacity_provider or None,
        cfg.scope,
        cfg.min_remaining,
        cfg.poll_interval,
        model=cfg.model or None,
        allow_fallback=cfg.allow_fallback,
    )


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
            if not _sleep_interruptibly(cfg, not_before):
                raise SystemExit(143)
        snapshot, decision = _route_or_provider_decision(cfg)
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
        if not _sleep_interruptibly(cfg, target):
            raise SystemExit(143)


def run_fresh_attached(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    command_line = common.argv_to_command_line(argv)
    # Keep the normal foreground process group and controlling terminal. In
    # particular, SIGWINCH must reach `script` so it can resize its PTY child.
    # Bridge-managed headless/native work has its own explicit process-group
    # ownership path and does not use this attached launch mode.
    proc = subprocess.Popen(["script", "--return", "--quiet", "--flush", "--command", command_line, str(output_file)], cwd=cfg.cwd, env=provider_env(cfg))
    _record_popen_process(cfg, proc)
    return_code = proc.wait()
    status_file.write_text(str(return_code), encoding="utf-8")
    common.clean_capture_file(output_file)
    return return_code


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
        on_process_started=lambda pid: _record_spawned_process(cfg, pid) if cfg.process_started else None,
        max_output_bytes=cfg.max_output_bytes,
        stdin_path=Path(cfg.prompt_stdin) if cfg.prompt_stdin else None,
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


# Stands in for tool calls and tool results in the text scanned for blocking
# prompts. Their content comes from the repository or from the model's tool
# input, never from the provider CLI, but they still end any pending question.
TOOL_ACTIVITY_SCAN_MARKER = "\n[tool activity]\n"


class ClaudeStreamRenderer:
    def __init__(self) -> None:
        self.rendered_assistant_text = False
        # Bytes of the last rendered line that may be scanned for blocking
        # prompts: model text and the final result, never tool input or output.
        self.scannable = b""

    @staticmethod
    def scan_event(event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        if event_type in {"assistant", "user"}:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else [content]
            texts: list[str] = []
            tool_activity = event_type == "user"
            for block in blocks:
                if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}:
                    tool_activity = True
                elif event_type == "assistant":
                    if isinstance(block, str):
                        texts.append(block)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
            return "".join(texts) + (TOOL_ACTIVITY_SCAN_MARKER if tool_activity else "")
        if event_type in {"assistant_delta", "content_block_delta"}:
            delta = event.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
            return text if isinstance(text, str) else ""
        if event_type == "result" and isinstance(event.get("result"), str):
            return str(event["result"])
        return ""

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
        self.scannable = b""
        if not stripped:
            return b""
        try:
            event = json.loads(stripped.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.scannable = line
            return line
        if not isinstance(event, dict):
            return b""
        self.scannable = self.scan_event(event).encode("utf-8", "replace")
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
        combined_tail = common.blocking_tail(combined_tail)
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


def _popen_with_prompt(cfg: SchedulerConfig, argv: list[str]) -> "subprocess.Popen[bytes]":
    """Start a headless provider with piped output, reading its prompt from the run's prompt file when set."""
    stdin: Any = None
    if cfg.prompt_stdin:
        stdin = open(cfg.prompt_stdin, "rb")
    try:
        return subprocess.Popen(argv, cwd=cfg.cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=provider_env(cfg), start_new_session=True)
    finally:
        if stdin is not None:
            stdin.close()


def run_fresh_claude_stream_json(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    import os
    import select
    import signal

    proc = _popen_with_prompt(cfg, argv)
    _record_popen_process(cfg, proc)
    assert proc.stdout is not None and proc.stderr is not None
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    open_fds = {stdout_fd: "stdout", stderr_fd: "stderr"}
    stdout_color = stream_color_enabled(sys.stdout)
    stderr_color = stream_color_enabled(sys.stderr)
    stdout_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, cfg.model, usage_ttl=cfg.output_prefix_usage_ttl)
    stderr_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, cfg.model, usage_ttl=cfg.output_prefix_usage_ttl)
    renderer = ClaudeStreamRenderer()
    stdout_buffer = b""
    combined_parts: list[bytes] = []
    retained_bytes = 0
    output_truncated = False

    def append_combined(data: bytes) -> None:
        nonlocal retained_bytes, output_truncated
        combined_parts.append(data)
        retained_bytes += len(data)
        limit = int(cfg.max_output_bytes)
        if limit <= 0:
            return
        while combined_parts and retained_bytes > limit:
            excess = retained_bytes - limit
            if len(combined_parts[0]) <= excess:
                retained_bytes -= len(combined_parts.pop(0))
            else:
                combined_parts[0] = combined_parts[0][excess:]
                retained_bytes -= excess
            output_truncated = True
    start = time.time()
    timeout = int(os.environ.get("LLM_SCHEDULER_PTY_TIMEOUT", "3600") or "3600")
    exit_code = 124
    blocking = False
    abort_reason = ""
    guard = ProgressGuard()

    scan_buffer = b""

    def scan_tail(scannable: bytes) -> bool:
        nonlocal scan_buffer
        scan_buffer = (scan_buffer + scannable)[-8000:]
        return guard.note_output(common.strip_ansi(scan_buffer.decode("utf-8", "replace")))

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
                        append_combined(rendered)
                        try:
                            sys.stdout.buffer.write(stdout_ts.apply(highlight_provider_text(rendered, stream_name="stdout", enabled=stdout_color)))
                            sys.stdout.buffer.flush()
                        except OSError:
                            pass
                        if scan_tail(renderer.scannable):
                            blocking = True
                    stdout_buffer = b""
                open_fds.pop(fd, None)
                continue
            guard.last_progress = time.time()
            if open_fds.get(fd) == "stdout":
                stdout_buffer += chunk
                if cfg.max_output_bytes > 0 and len(stdout_buffer) > cfg.max_output_bytes:
                    stdout_buffer = stdout_buffer[-cfg.max_output_bytes:]
                lines = stdout_buffer.splitlines(keepends=True)
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    stdout_buffer = lines.pop()
                else:
                    stdout_buffer = b""
                for line in lines:
                    rendered = renderer.render_line(line)
                    if not rendered:
                        continue
                    append_combined(rendered)
                    try:
                        sys.stdout.buffer.write(stdout_ts.apply(highlight_provider_text(rendered, stream_name="stdout", enabled=stdout_color)))
                        sys.stdout.buffer.flush()
                    except OSError:
                        pass
                    if scan_tail(renderer.scannable):
                        blocking = True
                        break
            else:
                append_combined(chunk)
                try:
                    sys.stderr.buffer.write(stderr_ts.apply(highlight_provider_text(chunk, stream_name="stderr", enabled=stderr_color)))
                    sys.stderr.buffer.flush()
                except OSError:
                    pass
                if scan_tail(chunk):
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
        append_combined(abort_line)
        try:
            sys.stderr.buffer.write(stderr_ts.apply(highlight_provider_text(abort_line, stream_name="stderr", enabled=stderr_color)))
            sys.stderr.buffer.flush()
        except OSError:
            pass
    output_text = b"".join(combined_parts).decode("utf-8", "replace")
    if output_truncated:
        output_text = "[Bridge retained only the latest bounded provider output. Earlier bytes were discarded.]\n" + output_text
    output_file.write_text(output_text, encoding="utf-8")
    status_file.write_text(str(exit_code), encoding="utf-8")
    return exit_code


def run_fresh_exact_stdout(cfg: SchedulerConfig, argv: list[str], output_file: Path, status_file: Path) -> int:
    import os
    import select
    import signal

    proc = _popen_with_prompt(cfg, argv)
    _record_popen_process(cfg, proc)
    assert proc.stdout is not None and proc.stderr is not None
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    open_fds = {stdout_fd: "stdout", stderr_fd: "stderr"}
    stdout_color = stream_color_enabled(sys.stdout)
    stderr_color = stream_color_enabled(sys.stderr)
    stdout_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, cfg.model, usage_ttl=cfg.output_prefix_usage_ttl)
    stderr_ts = common.LinePrefixer(cfg.output_prefix_fields, cfg.provider, cfg.model, usage_ttl=cfg.output_prefix_usage_ttl)
    combined_parts: list[bytes] = []
    retained_bytes = 0
    output_truncated = False

    def append_combined(data: bytes) -> None:
        nonlocal retained_bytes, output_truncated
        combined_parts.append(data)
        retained_bytes += len(data)
        limit = int(cfg.max_output_bytes)
        if limit <= 0:
            return
        while combined_parts and retained_bytes > limit:
            excess = retained_bytes - limit
            if len(combined_parts[0]) <= excess:
                retained_bytes -= len(combined_parts.pop(0))
            else:
                combined_parts[0] = combined_parts[0][excess:]
                retained_bytes -= excess
            output_truncated = True
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
            append_combined(chunk)
            if open_fds.get(fd) == "stdout":
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
        append_combined(f"\nllm-scheduler: autonomous abort: {abort_reason}\n".encode())
    output_text = b"".join(combined_parts).decode("utf-8", "replace")
    if output_truncated:
        output_text = "[Bridge retained only the latest bounded provider output. Earlier bytes were discarded.]\n" + output_text
    output_file.write_text(output_text, encoding="utf-8")
    status_file.write_text(str(exit_code), encoding="utf-8")
    return exit_code


# Seconds between checks that a Bridge-owned provider pane is still alive. The
# status file normally reports a finished provider within a second; the pane
# check only recovers a provider killed before it could write one.
TMUX_PANE_CHECK_SECONDS = 10


def run_tmux(cfg: SchedulerConfig, logs: common.RunLogs, argv: list[str], output_file: Path, status_file: Path) -> int:
    if not common.have_cmd("tmux"):
        output_file.write_text("tmux not installed\n", encoding="utf-8")
        status_file.write_text("127", encoding="utf-8")
        return 127
    tmux_prefix = ["tmux", *( ["-S", cfg.tmux_socket] if cfg.tmux_socket else [] )]

    def tmux(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return subprocess.run([*tmux_prefix, *args], **kwargs)

    if ":" in cfg.tmux_target:
        session, window = cfg.tmux_target.split(":", 1)
        if not session or not window:
            output_file.write_text(f"invalid tmux target: {cfg.tmux_target}\n", encoding="utf-8")
            status_file.write_text("2", encoding="utf-8")
            return 2
    else:
        session, window = cfg.tmux_target, "llm-scheduler"
    target = cfg.tmux_pane or f"{session}:{window}"
    command_line = common.argv_to_command_line(argv)
    cmd_file = logs.run_dir / "tmux-command.sh"
    # The script changes into the mission folder first: keep relative PATH entries out of every lookup.
    guard_exports = f"export PATH={shlex.quote(common.trusted_path())}\n"
    if cfg.ralph_robin_active:
        guard_exports += "export LLM_TOOLS_RALPH_ROBIN_ACTIVE=1\nexport LLM_TOOLS_RALPH_ROBIN_SCHEDULER=guarded\n"
    if cfg.ralph_robin_active and cfg.route_id:
        guard_exports += f"export LLM_TOOLS_RALPH_ROBIN_SELECTED_ROUTE={shlex.quote(cfg.route_id)}\n"
    for key, value in cfg.env_overrides.items():
        if not re.fullmatch(r"LLM_BRIDGE_[A-Z0-9_]+", key):
            raise ValueError(f"invalid managed provider environment name: {key}")
        guard_exports += f"export {key}={shlex.quote(str(value))}\n"
    # A tmux pane inherits the server's environment, not ours; remove other providers' credentials there.
    for key in sorted(common.credential_variables_to_remove(os.environ, cfg.provider)):
        guard_exports += f"unset {shlex.quote(key)}\n"
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
    owned_pane = bool(cfg.tmux_socket and cfg.tmux_pane)
    if tmux(["has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
        if cfg.tmux_socket:
            output_file.write_text("Bridge-owned tmux station is no longer available\n", encoding="utf-8")
            status_file.write_text("143", encoding="utf-8")
            return 143
        tmux(["new-session", "-d", "-s", session, "-n", window, invocation], check=False)
    elif owned_pane:
        pane_state = tmux(["display-message", "-p", "-t", target, "#{pane_dead}\t#{pane_start_command}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout
        pane_dead, _, start_command = pane_state.rstrip("\n").partition("\t")
        for _ in range(20):
            if pane_dead == "1" or "exit 0" not in start_command:
                break
            # The idle placeholder (a shell running `exit 0`) may not have exited yet right after the
            # station was created; give it up to two seconds rather than reporting a live native process.
            time.sleep(0.1)
            pane_state = tmux(["display-message", "-p", "-t", target, "#{pane_dead}\t#{pane_start_command}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout
            pane_dead, _, start_command = pane_state.rstrip("\n").partition("\t")
        if pane_dead != "1":
            # A Bridge "waiting for capacity" message pane holds no provider; replace it.
            if "llm-bridge-wait" in start_command:
                pane_dead = "1"
        if pane_dead != "1":
            output_file.write_text("Bridge provider pane is busy; refusing to replace a live native process\n", encoding="utf-8")
            status_file.write_text(str(common.AUTONOMY_ABORT_STATUS), encoding="utf-8")
            return common.AUTONOMY_ABORT_STATUS
        if cfg.max_output_bytes > 0:
            pipe_command = common.argv_to_command_line([
                *common.rooted_module_argv(
                    "llm_tools.bridge_log_sink",
                    "--path", str(output_file),
                    "--max-bytes", str(cfg.max_output_bytes),
                    "--rotations", str(cfg.max_output_rotations),
                ),
            ])
        else:
            pipe_command = f"cat >> {shlex.quote(str(output_file))}"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.touch()
        respawn = tmux(["respawn-pane", "-k", "-t", target, "-c", common.tmux_literal(cfg.cwd), f"sleep 0.1; {invocation}"], check=False)
        if respawn.returncode != 0:
            output_file.write_text("could not respawn provider pane\n", encoding="utf-8")
            status_file.write_text("127", encoding="utf-8")
            return 127
        if cfg.process_started is not None:
            pane_pid = tmux(["display-message", "-p", "-t", target, "#{pane_pid}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout.strip()
            if pane_pid.isdigit():
                _record_spawned_process(cfg, int(pane_pid))
        tmux(["pipe-pane", "-t", target, common.tmux_literal(pipe_command)], check=False)
    else:
        windows = tmux(["list-windows", "-t", session, "-F", "#W"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout.splitlines()
        if window not in windows:
            tmux(["new-window", "-d", "-t", session, "-n", window, invocation], check=False)
        else:
            tmux(["send-keys", "-t", target, invocation, "C-m"], check=False)
    waited = 0
    configured_timeout = os.environ.get("LLM_SCHEDULER_TMUX_TIMEOUT")
    # Bridge owns the pane lifecycle and waits for the provider to return or
    # an operator to stop the mission. The legacy one-hour bound remains for
    # user-managed tmux targets; an explicit environment value still applies
    # to either mode.
    timeout = int(configured_timeout) if configured_timeout else (None if owned_pane else 3600)
    while (not status_file.is_file() or status_file.stat().st_size == 0) and (timeout is None or waited < timeout):
        if owned_pane and cfg.process_observer is not None:
            cfg.process_observer()
        if owned_pane:
            # A provider that finishes writes its status file, which the loop
            # condition sees. The checks below only cover a station or provider
            # that vanished without one, so they need not start tmux every second.
            station_gone = not common.unix_socket_accepts(cfg.tmux_socket) and tmux(["has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0
            if station_gone:
                status_file.write_text("143", encoding="utf-8")
                break
            if waited % TMUX_PANE_CHECK_SECONDS == TMUX_PANE_CHECK_SECONDS - 1:
                pane_dead = tmux(["display-message", "-p", "-t", target, "#{pane_dead}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout.strip()
                if pane_dead == "1":
                    time.sleep(0.05)
                    if not status_file.is_file() or status_file.stat().st_size == 0:
                        status_file.write_text("124", encoding="utf-8")
                        break
        elif cfg.tmux_socket and tmux(["has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
            status_file.write_text("143", encoding="utf-8")
            break
        else:
            pane = tmux(["capture-pane", "-p", "-t", target], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
            output_file.write_text(pane.stdout, encoding="utf-8")
        time.sleep(1)
        waited += 1
    if owned_pane:
        tmux(["pipe-pane", "-t", target], check=False)
        time.sleep(0.05)
        common.clean_capture_file(output_file)
    else:
        pane = tmux(["capture-pane", "-p", "-t", target], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        output_file.write_text(pane.stdout, encoding="utf-8")
    if not status_file.is_file() or status_file.stat().st_size == 0:
        status_file.write_text("124", encoding="utf-8")
    try:
        return int(status_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return 124


def submit_once(cfg: SchedulerConfig, logs: common.RunLogs, attempt: int, argv: list[str]) -> int:
    attempt_started = time.monotonic()
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
    if (
        cfg.provider == "claude"
        and status not in (0, 124, 130, 143, common.AUTONOMY_ABORT_STATUS)
        and time.monotonic() - attempt_started <= common.TRUST_DECLINE_MAX_SECONDS
        and common.output_shows_declined_trust_prompt(output)
    ):
        # The operator chose "No, exit" at Claude Code's folder-trust prompt.
        # Starting it again only shows the same prompt, so report a distinct
        # status that callers do not retry and do not count as a provider fault.
        common.log_event(logs, "operator_declined", {"attempt": attempt, "provider": cfg.provider, "reason": "folder-trust-declined", "exit_code": status})
        return common.OPERATOR_DECLINED_STATUS
    if status == common.AUTONOMY_ABORT_STATUS:
        common.log_event(logs, "autonomy_abort", {"attempt": attempt, "output": output})
        # An autonomy abort on a route means the provider ran but produced no
        # observable progress (idle timeout, blocked prompt, hung tool call,
        # etc.). For opaque routes this is the *only* signal the orchestrator
        # gets that the route's upstream quota is exhausted — the CLI keeps
        # reporting "ready" until something actually tries to use it. Without
        # a runtime block, ralph-robin would re-select the same opaque route
        # on the next iteration and burn another full idle-timeout window.
        # Recording the block here is what gives the orchestrator a chance to
        # rotate to a different route until the backoff expires.
        if cfg.route_id:
            _record_route_runtime_block_autonomy(cfg, logs, output)
        return common.AUTONOMY_ABORT_STATUS
    # Gateway credit/payment exhaustion can surface on a CLEAN (status 0)
    # exit: a gateway such as Kilo keeps a positive aggregate balance while a
    # specific prepaid model (e.g. kilo/minimax/minimax-m3) is out of credit,
    # so the CLI prints ``Payment Required: {"error_type":
    # "usage_limit_exceeded"}`` and returns 0. Detect that structured error
    # envelope here — BEFORE the trust_clean_exit guard — and treat it as a
    # route/model capacity exhaustion instead of a successful increment.
    # Otherwise ralph-robin re-selects the same route/model every few seconds
    # forever, because the capacity reader still reports the gateway balance
    # as usable. Returning AUTONOMY_ABORT_STATUS makes ralph rotate away (and
    # skip retries on the same exhausted model).
    if common.output_signals_credit_exhausted(output):
        if cfg.route_id:
            _record_route_runtime_block(cfg, logs, output, reason="credit-exhausted")
        elif cfg.model and cfg.provider:
            _record_model_runtime_block(cfg, logs, output)
        common.log_event(
            logs,
            "credit_exhausted",
            {"route": cfg.route_id, "provider": cfg.provider, "model": cfg.model, "attempt": attempt},
        )
        return common.AUTONOMY_ABORT_STATUS
    retryable = common.output_is_retryable(status, output, cfg.attached, trust_clean_exit=cfg.ralph_robin_active)
    # A clean exit on a route is a real successful increment; the
    # route's local block (if any) is no longer needed.
    if status == 0 and cfg.route_id:
        clear_route_runtime_block(cfg.route_id)
    # A clean exit with a pinned model in provider mode also clears any
    # stale model runtime block — the model is working again.
    if status == 0 and not cfg.route_id and cfg.model and cfg.provider:
        clear_model_runtime_block(cfg.provider, cfg.model)
    # When the route is opaque, a real provider-runtime failure is the
    # only signal we get. Record a local block so the orchestrator
    # (ralph) stops selecting the route until the retry window passes.
    if retryable and cfg.route_id:
        _record_route_runtime_block(cfg, logs, output)
    return 1 if retryable else 0


def _record_route_runtime_block(cfg: SchedulerConfig, logs: common.RunLogs, output: str, *, reason: str = "runtime-failure") -> None:
    """Persist a local block for ``cfg.route_id`` when the provider
    returned a retryable runtime failure.

    The block's ``blocked_until`` is computed from a parseable retry
    hint in the output (``retry-after``, ``retry in Xm``, ``reset in
    Xh``); when no hint is present we use the route's
    :func:`routes.default_backoff_seconds`. A successful run clears
    the block via :func:`clear_route_runtime_block`. The block is now
    consulted by every capacity policy (not only opaque) via
    :func:`routes._apply_local_block_override`, so a delegate / balance
    route whose pinned model runs out of prepaid credit is rotated
    away from instead of re-selected forever.
    """
    from . import routes

    retry_after = _parse_retry_after_seconds(output)
    backoff = routes.default_backoff_seconds()
    blocked_until = common.now_epoch() + (retry_after if retry_after is not None else backoff)
    routes.record_local_block(
        cfg.route_id,
        reason=reason,
        blocked_until=blocked_until,
        last_message=output[:2000],
        backoff_seconds=backoff,
    )
    common.log_event(
        logs,
        "route_runtime_block",
        {
            "route": cfg.route_id,
            "blocked_until": blocked_until,
            "reason": reason,
        },
    )


def _record_model_runtime_block(cfg: SchedulerConfig, logs: common.RunLogs, output: str) -> None:
    """Persist a runtime block for a provider+model pin (provider mode).

    The legacy provider-mode equivalent of a route runtime block. When a
    pinned model (e.g. kilo → ``kilo/minimax/minimax-m3``) exhausts its
    prepaid credit at runtime while the gateway balance stays positive,
    this records a block keyed by :func:`routes.model_block_key` so
    :func:`llm_tools.ralph_robin.effective_model_for` can drop the pin on
    the next iteration (when ``allow_fallback`` is set) and the launch CLI
    picks a different model. A subsequent successful run clears it via
    :func:`clear_model_runtime_block`.
    """
    from . import routes

    key = routes.model_block_key(cfg.provider, cfg.model)
    retry_after = _parse_retry_after_seconds(output)
    backoff = routes.default_backoff_seconds()
    blocked_until = common.now_epoch() + (retry_after if retry_after is not None else backoff)
    routes.record_local_block(
        key,
        reason="credit-exhausted",
        blocked_until=blocked_until,
        last_message=output[:2000],
        backoff_seconds=backoff,
    )
    common.log_event(
        logs,
        "model_runtime_block",
        {
            "provider": cfg.provider,
            "model": cfg.model,
            "key": key,
            "blocked_until": blocked_until,
            "reason": "credit-exhausted",
        },
    )


def _record_route_runtime_block_autonomy(cfg: SchedulerConfig, logs: common.RunLogs, output: str) -> None:
    """Persist a local block for ``cfg.route_id`` after an autonomy abort.

    Autonomy-abort (status 75) means the framework had to kill the run
    because it produced no observable progress — typically an idle
    timeout, a blocked prompt, or, for opaque Kilo routes pinned to a
    model whose backend silently 429s, an API quota that exhausts
    without surfacing in the captured output. The CLI still reports the
    route as "ready" (CLI present, no local block), so without this
    record the orchestrator would re-select the same opaque route on
    the next iteration and burn another full idle-timeout window.

    The block uses the standard default backoff so the rotation has a
    chance to land on a different route while the failed one cools
    down. A retry hint in the output (rare for autonomy aborts, since
    they usually have empty output) is honoured when present.
    """
    from . import routes

    retry_after = _parse_retry_after_seconds(output)
    backoff = routes.default_backoff_seconds()
    blocked_until = common.now_epoch() + (retry_after if retry_after is not None else backoff)
    routes.record_local_block(
        cfg.route_id,
        reason="autonomy-abort",
        blocked_until=blocked_until,
        last_message=output[:2000],
        backoff_seconds=backoff,
    )
    common.log_event(
        logs,
        "route_runtime_block",
        {
            "route": cfg.route_id,
            "blocked_until": blocked_until,
            "reason": "autonomy-abort",
        },
    )


_RETRY_HINT_RE = re.compile(
    r"\bretry[- ]after[: ]+(\d+)\s*(s|sec|seconds|m|min|minutes|h|hr|hours)?",
    re.I,
)


def _parse_retry_after_seconds(output: str) -> int | None:
    """Best-effort parse of a retry hint from provider output.

    Looks for ``retry-after: 60s`` / ``retry after 5 minutes`` style
    phrases and returns the implied seconds. Returns ``None`` when
    no hint is present.
    """
    if not output:
        return None
    match = _RETRY_HINT_RE.search(output)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    unit = (match.group(2) or "s").lower()
    if unit.startswith("m"):
        return value * 60
    if unit.startswith("h"):
        return value * 3600
    return value


def clear_route_runtime_block(route_id: str) -> None:
    """Drop the local block for ``route_id`` after a successful run."""
    from . import routes

    routes.clear_local_block(route_id)


def clear_model_runtime_block(provider: str, model: str) -> None:
    """Drop the runtime block for a provider+model pin after a successful run."""
    from . import routes

    if provider and model:
        routes.clear_local_block(routes.model_block_key(provider, model))


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
    if result == common.OPERATOR_DECLINED_STATUS:
        common.log_text(logs, "final status: operator-declined")
        common.log_event(logs, "final", {"status": "operator-declined"})
        print(f"operator-declined: logs written to {logs.run_dir}", file=sys.stderr)
        return common.OPERATOR_DECLINED_STATUS
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
        if result == common.OPERATOR_DECLINED_STATUS:
            common.log_text(logs, "final status: operator-declined")
            common.log_event(logs, "final", {"status": "operator-declined"})
            print(f"operator-declined: logs written to {logs.run_dir}", file=sys.stderr)
            return common.OPERATOR_DECLINED_STATUS
    common.log_text(logs, "final status: failed")
    common.log_event(logs, "final", {"status": "failed"})
    print(f"failed: logs written to {logs.run_dir}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
