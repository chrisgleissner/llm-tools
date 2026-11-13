"""The provider registry: one typed record per provider, one source for each provider fact.

Facts that differ by provider (launch argv, session identity and resume form, exit command, input
prompt, usage-limit notice, question detection, notice delivery, prompt channel) live here and
nowhere else. ``scheduler``, ``bridge_sessions``, ``bridge_finish``, ``bridge_limit_notice``,
``bridge_task_notice`` and ``terminal`` read this registry at call time, so a change to a record
changes every consumer. ``bridge_capabilities`` generates the capability matrix from it.
Standard library only.

Argv templates are tuples of tokens. ``{model}`` expands to the model flags (zero or more
tokens); ``{cwd}`` and ``{prompt}`` expand to one token each.

Kilo and OpenCode sessions (PROV-003/004), Kilo 7.3.45 and OpenCode 1.18.3, from ``--help`` and the
database schema only: ``-s/--session <id>`` continues a session, ``-c/--continue`` the last one (never
used). Sessions live in the SQLite ``session`` table of ``session_store`` under the XDG data home;
neither CLI prints or accepts a session id. ``exit_command`` ``/exit`` and the ``idle_prompt``
footer markers come from the upstream TUI, not from ``--help``: unverified against a live screen.
``docs/llm-bridge/operations.md`` has the capture rule.

Prompt channel (PROV-013). Kilo 7.3.45 and OpenCode 1.18.3, from ``kilo run --help`` and
``opencode run --help`` (read-only, 2026-11-12): ``run [message..]`` takes the prompt as positional
words; ``--file`` attaches a file to the message but does not replace it; neither help text
documents reading the prompt from standard input or from a prompt file; the interactive form takes
``--prompt TEXT``. Both therefore stay on ``argv`` and the prompt is visible in the process list.
Sending a prompt to probe undocumented standard-input handling is outside this change. A record
whose ``prompt_channel`` is not ``argv`` must not contain ``{prompt}`` in its templates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

PROMPT_ARGV = "argv"
PROMPT_STDIN = "stdin"
PROMPT_FILE = "file"

# A composer line: optional box-drawing border, then a prompt glyph.
COMPOSER_LINE = re.compile(r"^[\s│┃|]*[›❯>](?:\s|$)")

QUESTION_NONE = "none"
QUESTION_CODEX_QUEUED = "codex-queued"
QUESTION_CLAUDE_IDLE = "claude-idle"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")  # a leading dash would be read as an option by the provider CLI

_DECORATION = r"^[\s■●⎿✻▐│|>*•·]*"
_APOSTROPHE = "['’]"
_CLAUDE_LIMIT = re.compile(
    _DECORATION + r"(?:"
    r"(?:claude )?usage limit reached\b"
    r"|(?:\d+-hour|weekly|monthly|session|opus|sonnet)(?: \w+)? limit reached\b"
    rf"|you{_APOSTROPHE}ve hit your (?:(?:usage|session|weekly|\d+-hour) )?limit\b"
    r")",
    re.IGNORECASE,
)
_CODEX_LIMIT = re.compile(
    _DECORATION + rf"you(?:{_APOSTROPHE}ve| have) (?:hit|reached) your usage limit\b",
    re.IGNORECASE,
)


def _flag(flag: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\S)" + re.escape(flag) + r"(?:\s|[=<]|$)")


ZAI_LAUNCH_HINT = (
    "--provider zai has no launch CLI. Launch Kilo or OpenCode instead: use a route "
    '(e.g. [routes.kilo-zai-glm-47] with capacity.policy = "delegate" and provider = "zai") '
    'or set [providers.kilo] capacity_provider = "zai"'
)
MINIMAX_NATIVE_HINT = (
    "MiniMax mmx exposes text chat, not a native engineering session for LLM Bridge. "
    "Run MiniMax models through a route on Kilo or OpenCode instead "
    '(e.g. [routes.kilo-minimax-m3] with provider = "kilo" and model = "minimax-m3")'
)


@dataclass(frozen=True)
class IdlePrompt:
    """A native screen waits at its input prompt when ``pattern`` matches a line and ``required_text`` (if any) is on screen."""

    pattern: re.Pattern[str]
    required_text: str | None = None
    busy_text: tuple[str, ...] = ()  # screen text that means the provider is working, not waiting
    glyph_composer: bool = True  # finish also accepts any composer-glyph line (``›``, ``❯``, ``>``); False: only ``pattern`` counts

    def matches(self, screen: str) -> bool:
        folded = screen.casefold()
        if self.required_text is not None and self.required_text.casefold() not in folded:
            return False
        if any(text.casefold() in folded for text in self.busy_text):
            return False
        return bool(self.pattern.search(screen))


_CODEX_IDLE = IdlePrompt(re.compile(r"(?m)^\s*›(?:\s+Ask Codex to do anything)?\s*$"), "? for shortcuts")
_CLAUDE_IDLE = IdlePrompt(re.compile(r"(?m)^\s*❯\s*$"))
# Copilot CLI 1.0: an empty composer line or the placeholder hint ("Type @ to mention files ...").
_COPILOT_IDLE = IdlePrompt(re.compile(r"(?m)^[\s│┃|]*>\s+(?:Type|Enter) @ to mention files"))
# Kilo and OpenCode share one TUI lineage. The input prompt is on screen when the footer lists the
# command palette key and no "esc interrupt" hint (shown only while a turn runs). NOT verified
# against a live screen (starting the TUI was out of scope); a mismatch makes finish refuse, never type.
_OPENCODE_IDLE = IdlePrompt(re.compile(r"(?mi)\bctrl\+p\s+commands\b"), busy_text=("esc interrupt",), glyph_composer=False)


@dataclass(frozen=True)
class ProviderSpec:
    """Every provider-specific fact Bridge needs, in one record."""

    name: str
    display_name: str
    executable: str
    native_argv: tuple[str, ...] | None = None
    headless_argv: tuple[str, ...] | None = None
    headless_stream_argv: tuple[str, ...] | None = None
    native_unavailable: str | None = None  # why a native session cannot start, naming the alternative
    headless_unavailable: str | None = None
    prompt_channel: str = PROMPT_ARGV
    prompt_channel_note: str = ""
    session_sources: frozenset[str] = frozenset()
    session_store: str | None = None  # SQLite session database, relative to the XDG data home (Kilo, OpenCode)
    resume_syntax: tuple[str, ...] = ()
    resume_help_pattern: re.Pattern[str] | None = None
    id_pattern: re.Pattern[str] = _UUID
    new_session_syntax: tuple[str, ...] = ()
    new_session_pattern: re.Pattern[str] | None = None
    exit_command: str | None = None
    idle_prompt: IdlePrompt | None = None  # how a native screen shows "waiting at the input prompt"; None: not detectable
    finish_idle_prompt: IdlePrompt | None = None  # input prompt used only by finish when ``idle_prompt`` is None (Copilot's is not trusted for notices)
    limit_notice_pattern: re.Pattern[str] | None = None
    question_detector: str = QUESTION_NONE
    notice_delivery: bool = False
    version_flag: str = "--version"

    @property
    def finish_prompt(self) -> IdlePrompt | None:
        """The prompt finish checks before typing its exit command; ``None`` means finish refuses."""
        return self.idle_prompt or self.finish_idle_prompt

    @property
    def native_supported(self) -> bool:
        return self.native_argv is not None and self.native_unavailable is None


REGISTRY: dict[str, ProviderSpec] = {
    "codex": ProviderSpec(
        "codex", "Codex", "codex",
        native_argv=("codex", "{model}", "-C", "{cwd}", "{prompt}"),
        headless_argv=("codex", "exec", "{model}", "-C", "{cwd}", "{prompt}"),
        session_sources=frozenset({"codex:session_meta"}),
        resume_syntax=("resume",), resume_help_pattern=re.compile(r"(?im)^\s*resume(?:\s|$)"),
        exit_command="/quit", idle_prompt=_CODEX_IDLE, limit_notice_pattern=_CODEX_LIMIT,
        question_detector=QUESTION_CODEX_QUEUED, notice_delivery=True,
    ),
    "claude": ProviderSpec(
        "claude", "Claude Code", "claude",
        native_argv=("claude", "{model}", "{prompt}"),
        headless_argv=("claude", "--print", "{model}", "{prompt}"),
        headless_stream_argv=("claude", "--print", "--output-format", "stream-json", "--verbose", "{model}", "{prompt}"),
        session_sources=frozenset({"claude:system.init", "claude:transcript"}),
        resume_syntax=("--resume",), resume_help_pattern=_flag("--resume"),
        new_session_syntax=("--session-id",), new_session_pattern=_flag("--session-id"),
        exit_command="/exit", idle_prompt=_CLAUDE_IDLE, limit_notice_pattern=_CLAUDE_LIMIT,
        question_detector=QUESTION_CLAUDE_IDLE, notice_delivery=True,
    ),
    "copilot": ProviderSpec(
        "copilot", "Copilot", "copilot",
        native_argv=("copilot", "{model}", "-C", "{cwd}", "-i", "{prompt}"),
        headless_argv=("copilot", "{model}", "-C", "{cwd}", "--prompt", "{prompt}"),
        session_sources=frozenset({"copilot:session.start"}),
        resume_syntax=("--resume",), resume_help_pattern=_flag("--resume"),
        new_session_syntax=("--session-id",), new_session_pattern=_flag("--session-id"),
        exit_command="/exit", finish_idle_prompt=_COPILOT_IDLE, notice_delivery=True,
    ),
    "kilo": ProviderSpec(
        "kilo", "Kilo", "kilo",
        # kilo uses `--dir` for the working directory and `-m, --model provider/model`. No
        # permission-bypassing flag is injected: the user's own Kilo `permission` config governs.
        native_argv=("kilo", "{model}", "--prompt", "{prompt}"),
        headless_argv=("kilo", "run", "{model}", "--dir", "{cwd}", "{prompt}"),
        prompt_channel_note="kilo 7.3.45 help: `run [message..]` positional; --file attaches, no prompt-file or stdin option documented",
        resume_syntax=("--session",), resume_help_pattern=_flag("--session"), id_pattern=_OPAQUE_ID,
        session_sources=frozenset({"kilo:session.db"}), session_store="kilo/kilo.db",
        exit_command="/exit", idle_prompt=_OPENCODE_IDLE,
    ),
    "opencode": ProviderSpec(
        "opencode", "OpenCode", "opencode",
        native_argv=("opencode", "{model}", "--prompt", "{prompt}"),
        headless_argv=("opencode", "run", "{model}", "--dir", "{cwd}", "{prompt}"),
        prompt_channel_note="opencode 1.18.3 help: `run [message..]` positional; --file attaches, no prompt-file or stdin option documented",
        resume_syntax=("--session",), resume_help_pattern=_flag("--session"), id_pattern=_OPAQUE_ID,
        session_sources=frozenset({"opencode:session.db"}), session_store="opencode/opencode.db",
        exit_command="/exit", idle_prompt=_OPENCODE_IDLE,
    ),
    "minimax": ProviderSpec(
        "minimax", "MiniMax", "mmx",
        headless_argv=("mmx", "text", "chat", "--message", "{prompt}"),
        native_unavailable=MINIMAX_NATIVE_HINT,
    ),
    "zai": ProviderSpec(
        "zai", "z.ai", "zai",
        native_unavailable=ZAI_LAUNCH_HINT, headless_unavailable=ZAI_LAUNCH_HINT,
    ),
}


def get(provider: str) -> ProviderSpec | None:
    """The record for ``provider`` (case-insensitive), or ``None`` for an unknown name."""
    return REGISTRY.get(provider.strip().lower())


def expand(template: tuple[str, ...], *, model_flags: list[str], cwd: str, prompt: str) -> list[str]:
    """Fill an argv template: ``{model}`` expands to the model flags, ``{cwd}`` and ``{prompt}`` to one token."""
    argv: list[str] = []
    for token in template:
        if token == "{model}":
            argv.extend(model_flags)
        elif token == "{cwd}":
            argv.append(cwd)
        elif token == "{prompt}":
            argv.append(prompt)
        else:
            argv.append(token)
    return argv


def native_unavailable_message(provider: str) -> str | None:
    """The message for a provider that cannot run a native mission (it names the route alternative), else ``None``."""
    spec = get(provider)
    return spec.native_unavailable if spec is not None else None


def check_native_start(provider: str) -> None:
    """Raise ``ValueError`` with the route alternative when ``provider`` cannot run a native mission (PROV-012)."""
    message = native_unavailable_message(provider)
    if message is not None:
        raise ValueError(f"{provider} cannot run a native mission: {message}")


def prompt_in_argv(provider: str) -> bool:
    """Whether the provider's launch command carries the prompt in the process arguments."""
    spec = get(provider)
    return spec is None or spec.prompt_channel == PROMPT_ARGV


def specs() -> Mapping[str, ProviderSpec]:
    return REGISTRY
