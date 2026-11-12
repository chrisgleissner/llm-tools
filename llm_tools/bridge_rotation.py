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


def validate_names(conf: dict[str, Any], raw: str, *, routes_allowed: bool) -> list[str]:
    """Names in ``raw`` after checking each is a provider (or, when allowed, a declared route).

    ``rotation --set``, ``mission create`` and ``start`` share this check. An
    unknown name raises ``ValueError`` with the closest known names and the full list.
    """
    import difflib

    from .capacity import ALL_PROVIDERS

    names = _split(raw)
    if not names:
        raise ValueError("a rotation needs at least one provider or route name")
    declared = list(conf.get("routes", {})) if routes_allowed and isinstance(conf.get("routes"), dict) else []
    known = [*declared, *(name for name in ALL_PROVIDERS if name not in declared)]
    for name in names:
        if name not in known:
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            hint = f" · did you mean {', '.join(close)}?" if close else ""
            raise ValueError(f"unknown rotation entry '{name}'{hint} · available: {', '.join(known)}")
    return names


def unknown_entry_problem(conf: dict[str, Any], providers: Any, routes: Any) -> str | None:
    """Why a rotation names an entry that is neither a provider nor a declared route, or ``None``.

    A mission saved without any rotation (both values unset) uses the default rotation, which is
    resolved when the controller starts, so there is nothing to check here.
    """
    if providers is None and routes is None:
        return None
    route_text = str(routes or "").strip()
    route_names = [] if route_text.lower() in {"none", "off"} else _split(route_text)
    try:
        if _split(providers):
            validate_names(conf, str(providers), routes_allowed=False)
        elif not route_names:
            raise ValueError("a rotation needs at least one provider or route name")
        if route_names:
            validate_names(conf, ",".join(route_names), routes_allowed=True)
    except ValueError as exc:
        return str(exc)
    return None


def signin_warnings(conf: dict[str, Any], providers: Any, routes: Any, env: Any = None) -> list[str]:
    """One warning per rotation entry whose CLI is installed but whose sign-in files are missing.

    Offline: it reads only the files ``llm-bridge doctor`` reads. ``LLM_BRIDGE_NO_SIGNIN_CHECK=1``
    turns it off (the test suite sets it so results do not depend on the developer's home).
    """
    if os.environ.get("LLM_BRIDGE_NO_SIGNIN_CHECK", "").strip() not in {"", "0", "false", "no"}:
        return []
    from .bridge_doctor import provider_checks

    warnings = []
    for check in provider_checks([(name, cli) for name, cli in entries(conf, providers, routes)], env):
        if check.get("installed") and check.get("logged_in") is False:
            name = str(check["name"])
            detail = str(check.get("detail") or "").strip()
            how = f" ({detail})" if detail else ""
            warnings.append(f"warning: {name} is not signed in{how}; it will be skipped until it is")
    return warnings


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


def native_refusal(conf: dict[str, Any], providers: Any, routes: Any) -> str | None:
    """PROV-012: why a rotation cannot run as a native mission (names the route alternative), or ``None``.

    Only entries whose launch CLI has no native session (MiniMax, z.ai) are refused; an unset rotation
    uses the default, which has none of them.
    """
    from . import bridge_providers

    for name, cli in entries(conf, providers, routes):
        message = bridge_providers.native_unavailable_message(cli)
        if message is not None:
            return f"{name} cannot run a native mission: {message}"
    return None


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

    An entry that is neither a provider nor a declared route is always refused:
    it can never launch, whether the operator typed it or it was saved earlier.
    An explicit rotation (typed by the operator) is refused when any entry is
    disabled, because the dropped entry would silently never run. A saved or
    default rotation is refused only when no entry is left, which matches
    ``ralph-robin`` dropping disabled entries and continuing with the rest.
    """
    unknown = unknown_entry_problem(conf, providers, routes)
    if unknown:
        return unknown
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
