"""Provider-specific adapters.

The legacy code hard-coded provider conditionals throughout the codebase.
Each provider now lives behind a small adapter module that exposes a
``read_<provider>`` function returning a :class:`ProviderSnapshot` and a
``command_argv`` helper for launching the provider CLI.

Adding a new provider is a small, well-defined change:

1. Add ``llm_tools/providers/<name>.py`` with a ``read(env)`` that
   returns a :class:`ProviderSnapshot`.
2. Re-export it from this ``__init__``.
3. Register it in :data:`llm_tools.capacity.PROVIDER_SCOPES`.
4. Add a default command under ``llm_tools.scheduler.provider_default_argv``
   and (optionally) highlighting under
   ``llm_tools.scheduler.highlight_provider_text``.
"""

from . import claude, codex, copilot, kilo
from .claude import (
    PROVIDER_CLAUDE,
    normalize as claude_normalize,
    read as read_claude_snapshot,
    read_claude,
    read_claude_api,
)
from .codex import (
    PROVIDER_CODEX,
    normalize as codex_normalize,
    read as read_codex_snapshot,
    read_codex,
)
from .copilot import (
    PROVIDER_COPILOT,
    read as read_copilot_snapshot,
    read_copilot,
    read_copilot_live,
)
from .kilo import (
    KILO_MODES,
    kilo_cli,
    kilo_command_argv,
    kilo_currency,
    kilo_min_balance,
    kilo_mode,
    kilo_monthly_reset_epoch,
    read_kilo,
)


__all__ = [
    "KILO_MODES",
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "PROVIDER_COPILOT",
    "claude",
    "claude_normalize",
    "codex",
    "codex_normalize",
    "copilot",
    "kilo_cli",
    "kilo_command_argv",
    "kilo_currency",
    "kilo_min_balance",
    "kilo_mode",
    "kilo_monthly_reset_epoch",
    "read_claude",
    "read_claude_api",
    "read_claude_snapshot",
    "read_codex",
    "read_codex_snapshot",
    "read_copilot",
    "read_copilot_live",
    "read_copilot_snapshot",
    "read_kilo",
]

