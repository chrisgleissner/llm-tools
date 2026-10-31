"""Check a Bridge rotation against the providers the llm-tools config enables.

``[usage].providers`` in the llm-tools config (or ``LLM_USAGE_PROVIDERS``) lists
the providers every tool may use. ``ralph-robin`` drops rotation entries whose
launch CLI is not listed and exits when none are left. Bridge applies the same
rule up front so a mission cannot be created, started or re-rotated into a
rotation that can never launch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import config as toolconfig

ENV_NAME = "LLM_USAGE_PROVIDERS"


@dataclass(frozen=True)
class EnabledProviders:
    """The enabled launch CLIs and where the restriction comes from."""

    names: frozenset[str] | None  # None: every provider is enabled
    source: str  # "env", "config" or "frozen"


def _names(raw: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def current_enabled() -> EnabledProviders:
    """Providers enabled by the current environment and llm-tools config."""
    names = toolconfig.enabled_providers()
    if names is None:
        return EnabledProviders(None, "config")
    return EnabledProviders(names, "env" if os.environ.get(ENV_NAME, "").strip() else "config")


def frozen_enabled(frozen_config: dict[str, Any]) -> EnabledProviders:
    """Providers enabled for a mission: the environment wins over its frozen config."""
    raw = os.environ.get(ENV_NAME, "")
    if raw.strip():
        return EnabledProviders(_names(raw), "env")
    usage = frozen_config.get("usage")
    listed = usage.get("providers") if isinstance(usage, dict) else None
    if isinstance(listed, list) and listed:
        return EnabledProviders(frozenset(str(item).strip().lower() for item in listed), "frozen")
    return EnabledProviders(None, "frozen")


def parse_frozen(text: Any) -> dict[str, Any]:
    """The frozen tool config text as a dict (empty when absent or unreadable)."""
    if not isinstance(text, str) or not text:
        return {}
    import tomllib

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _split(raw: Any) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in str(raw or "").split(",") if item.strip()))


def entries(conf: dict[str, Any], providers: Any, routes: Any) -> list[tuple[str, str]]:
    """``(rotation name, launch CLI)`` for each entry; routes win over providers."""
    route_names = _split(routes)
    if not route_names:
        return [(name, name) for name in _split(providers)]
    from .routes import route_or_implicit_provider

    found: list[tuple[str, str]] = []
    for name in route_names:
        try:
            policy = route_or_implicit_provider(conf, name)
        except (ValueError, TypeError, AttributeError, KeyError):
            policy = None
        if policy is not None:
            found.append((name, str(policy.provider)))
    return found


def disabled_entries(enabled: EnabledProviders, conf: dict[str, Any], providers: Any, routes: Any) -> list[tuple[str, str]]:
    """Rotation entries whose launch CLI is not enabled."""
    if enabled.names is None:
        return []
    return [(name, cli) for name, cli in entries(conf, providers, routes) if cli not in enabled.names]


def describe(disabled: list[tuple[str, str]], enabled: EnabledProviders, *, config_label: str) -> str:
    """One-line, actionable reason for refusing ``disabled`` entries."""
    labels = [name if name == cli else f"{name} (launch CLI {cli})" for name, cli in disabled]
    verb = "is" if len(labels) == 1 else "are"
    if enabled.source == "env":
        where = f"{ENV_NAME} · add it there"
    elif enabled.source == "frozen":
        where = f"[usage].providers in this mission's saved config · choose another rotation or set {ENV_NAME}"
    else:
        where = f"[usage].providers in {config_label} · add it there or set {ENV_NAME}"
    return f"{', '.join(labels)} {verb} disabled by {where}"


def check_rotation(
    enabled: EnabledProviders,
    conf: dict[str, Any],
    providers: Any,
    routes: Any,
    *,
    config_label: str,
    explicit: bool,
) -> str | None:
    """Return a refusal message, or ``None`` when the rotation can launch.

    An explicit rotation (typed by the operator) is refused when any entry is
    disabled, because the dropped entry would silently never run. A saved or
    default rotation is refused only when no entry is left, which matches
    ``ralph-robin`` dropping disabled entries and continuing with the rest.
    """
    disabled = disabled_entries(enabled, conf, providers, routes)
    if not disabled:
        return None
    total = len(entries(conf, providers, routes))
    if explicit or len(disabled) >= total:
        return describe(disabled, enabled, config_label=config_label)
    return None


def usable_entries(enabled: EnabledProviders, conf: dict[str, Any], providers: Any, routes: Any) -> tuple[str, str]:
    """The ``(providers, routes)`` pair without disabled entries."""
    if enabled.names is None:
        return str(providers or ""), str(routes or "")
    if _split(routes):
        keep = [name for name, cli in entries(conf, providers, routes) if cli in enabled.names]
        return str(providers or ""), ",".join(keep)
    return ",".join(name for name in _split(providers) if name in enabled.names), ""
