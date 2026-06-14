"""Provider-specific adapters.

The legacy code hard-coded provider conditionals throughout the codebase.
Each provider now lives behind a small adapter module that exposes a
``read_<provider>`` function returning a :class:`ProviderSnapshot` and a
``command_argv`` helper for launching the provider CLI.
"""

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
    "kilo_cli",
    "kilo_command_argv",
    "kilo_currency",
    "kilo_min_balance",
    "kilo_mode",
    "kilo_monthly_reset_epoch",
    "read_kilo",
]
