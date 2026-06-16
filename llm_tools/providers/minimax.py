"""MiniMax provider adapter.

MiniMax is a model family served through the ``mmx`` CLI. The CLI exposes
quota in the same shape Claude Code and Codex use (a session window plus a
weekly window) but the source is local: ``mmx quota show --output json``
returns an array of ``model_remains`` entries with interval and weekly
remaining percentages.

The reader tries two sources in order, mirroring the Codex/Claude/Copilot
pattern of preferring real CLI output and falling back to deterministic env
vars:

1. ``mmx quota show --output json`` (when the ``mmx`` binary is on PATH and
   emits a parseable payload).
2. Environment variables for tests:

   * ``LLM_USAGE_MINIMAX_5H_PERCENT`` - remaining percent for the 5h
     session window (number 0..100).
   * ``LLM_USAGE_MINIMAX_5H_RESET_EPOCH`` - epoch seconds when the 5h
     window resets.
   * ``LLM_USAGE_MINIMAX_WEEKLY_PERCENT`` - remaining percent for the
     weekly window (number 0..100).
   * ``LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH`` - epoch seconds when the
     weekly window resets.
   * ``LLM_USAGE_MINIMAX_MODEL`` - the ``model_name`` to read from the
     ``mmx`` payload (default ``general``). The CLI returns a row per
     model; only the first matching row is consumed.

The reader only emits a snapshot when the ``mmx`` binary is on PATH or the
test env-var fallback supplies data, so ``llm-usage`` quietly hides the
MiniMax row when the CLI is not installed. The CLI's presence is also a
hard requirement for ``llm-scheduler`` and ``ralph-robin``: launching
MiniMax without the binary is meaningless, and ``read_minimax`` reports
``reason="missing-cli"`` when the binary is not on PATH and no env
fallback data is present.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_MINIMAX,
    ProviderSnapshot,
    SCOPE_5H,
    SCOPE_WEEKLY,
)


DEFAULT_MINIMAX_MODEL = "general"
DEFAULT_MINIMAX_TIMEOUT = 10


def minimax_cli(env: dict[str, str] | None = None) -> str | None:
    """Locate the ``mmx`` binary using ``env`` (defaults to ``os.environ``).

    Accepting an env parameter keeps callers deterministic in tests: the
    host's PATH may contain an unrelated ``mmx`` install, but a test
    fixture can still isolate itself.
    """
    if env is None:
        env = os.environ
    return shutil.which("mmx", path=env.get("PATH"))


def minimax_model(env: dict[str, str] | None = None) -> str:
    """Which ``model_name`` row to consume from the ``mmx quota show`` payload.

    Defaults to ``general``; the CLI also returns a ``video`` row (and
    any future model rows) which we deliberately ignore.
    """
    env = env or os.environ
    raw = (env.get("LLM_USAGE_MINIMAX_MODEL") or DEFAULT_MINIMAX_MODEL).strip()
    return raw or DEFAULT_MINIMAX_MODEL


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_for_model(payload: dict[str, Any], model: str) -> dict[str, Any] | None:
    rows = payload.get("model_remains")
    if not isinstance(rows, list):
        return None
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name")
        if isinstance(name, str) and name == model:
            return entry
    return None


def _parse_minimax_payload(payload: Any, model: str) -> dict[str, Any] | None:
    """Pull the small, stable subset of fields we need out of a ``mmx
    quota show`` JSON payload.

    We accept only the narrow ``model_remains[]`` shape; anything else
    falls through to ``None`` so the reader can fall back to env vars.
    """
    if not isinstance(payload, dict):
        return None
    row = _row_for_model(payload, model)
    if row is None:
        return None
    out: dict[str, Any] = {}
    interval_pct = _safe_float(row.get("current_interval_remaining_percent"))
    weekly_pct = _safe_float(row.get("current_weekly_remaining_percent"))
    if interval_pct is not None:
        out["interval_percent"] = max(0.0, min(100.0, interval_pct))
    if weekly_pct is not None:
        out["weekly_percent"] = max(0.0, min(100.0, weekly_pct))
    interval_reset = _safe_int(row.get("end_time"))
    if interval_reset is not None:
        out["interval_reset_ms"] = interval_reset
    weekly_reset = _safe_int(row.get("weekly_end_time"))
    if weekly_reset is not None:
        out["weekly_reset_ms"] = weekly_reset
    if not out:
        return None
    return out


def _epoch_seconds(value_ms: int | None) -> int | None:
    if value_ms is None:
        return None
    if value_ms > 10_000_000_000:
        return int(value_ms // 1000)
    return int(value_ms)


# Sentinel returned by ``_run_minimax_quota`` when the CLI itself succeeded
# (exit 0, valid JSON) but the payload is an error envelope rather than a
# ``model_remains`` array. Surfacing this as a distinct reason lets
# ``read_minimax`` distinguish "we couldn't measure" from "the user's account
# has no plan / no token" so the dashboard can tell the user something useful
# instead of the generic ``inconclusive-usage``.
MINIMAX_ERROR_PAYLOAD = "__minimax_error_payload__"


def _classify_minimax_error(message: str) -> str:
    """Map a MiniMax error message string to a stable reason code.

    The CLI returns error envelopes shaped like
    ``{"error": {"code": 1, "message": "API error: no active token plan subscription (HTTP 200)"}}``.
    The string is freeform but the meaningful substrings (auth, plan,
    network) are stable enough across releases to drive a small lookup.
    Unknown messages fall back to ``quota-error`` so the renderer still
    surfaces "we tried, the service said no" rather than a generic
    ``inconclusive-usage``.
    """
    text = (message or "").lower()
    if "no active token" in text or "no active plan" in text or "subscription" in text or "plan" in text and "subscription" in text:
        return "subscription-required"
    if "auth" in text or "token" in text or "login" in text or "credential" in text:
        return "not-authenticated"
    if "rate" in text or "limit" in text or "429" in text or "throttle" in text:
        return "rate-limited"
    if "timeout" in text or "network" in text or "connection" in text or "econn" in text or "unreachable" in text:
        return "network-error"
    return "quota-error"


def _extract_minimax_error_reason(payload: Any) -> str | None:
    """Return a stable reason code when ``payload`` is a MiniMax error envelope.

    The CLI returns ``{"error": {"code": N, "message": "..."}}`` when the
    underlying API call failed (auth, subscription, etc). Anything else
    (a real ``model_remains`` payload) returns ``None`` and the caller falls
    through to the normal parser.
    """
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    message = str(err.get("message") or "")
    return _classify_minimax_error(message)


def _json_payload_from_streams(stdout: str, stderr: str) -> Any | None:
    """Parse the first valid JSON payload from the CLI streams.

    ``mmx quota`` uses stdout for successful quota payloads and stderr for
    error envelopes. Some CLI versions also emit human warnings on stderr
    during otherwise successful runs, so concatenating the streams can corrupt
    perfectly valid stdout JSON. Try each stream independently first; the
    combined fallback exists only for odd wrappers that split a JSON object.
    """
    candidates = [stdout.strip(), stderr.strip()]
    both = (stdout + stderr).strip()
    if both and both not in candidates:
        candidates.append(both)
    for text in candidates:
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _run_minimax_quota(env: dict[str, str]) -> dict[str, Any] | None:
    cli = minimax_cli(env)
    if not cli:
        return None
    model = minimax_model(env)
    try:
        proc = subprocess.run(
            [cli, "quota", "show", "--output", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(env.get("LLM_USAGE_MINIMAX_TIMEOUT", str(DEFAULT_MINIMAX_TIMEOUT)) or str(DEFAULT_MINIMAX_TIMEOUT)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # The mmx CLI writes the JSON ``model_remains`` payload to stdout on
    # success, but it writes the error envelope ``{"error": {...}}`` to
    # stderr (and exits non-zero) when the underlying API rejects the request.
    # Parse streams independently so stderr warnings do not corrupt valid
    # stdout JSON.
    payload = _json_payload_from_streams(proc.stdout or "", proc.stderr or "")
    if payload is None:
        return None
    reason = _extract_minimax_error_reason(payload)
    if reason is not None:
        return {MINIMAX_ERROR_PAYLOAD: True, "reason": reason, "message": str((payload.get("error") or {}).get("message") or "")}
    if proc.returncode != 0:
        # Non-zero exit with a JSON payload that isn't the documented
        # error envelope shape — treat it as a generic failure so the
        # caller can still show a reason instead of ``unavailable``.
        return None
    return _parse_minimax_payload(payload, model)


def _interval_from_env(env: dict[str, str]) -> tuple[float | None, int | None]:
    percent = _safe_float(env.get("LLM_USAGE_MINIMAX_5H_PERCENT"))
    reset_ms = _safe_int(env.get("LLM_USAGE_MINIMAX_5H_RESET_EPOCH"))
    reset = _epoch_seconds(reset_ms)
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    return percent, reset


def _weekly_from_env(env: dict[str, str]) -> tuple[float | None, int | None]:
    percent = _safe_float(env.get("LLM_USAGE_MINIMAX_WEEKLY_PERCENT"))
    reset_ms = _safe_int(env.get("LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH"))
    reset = _epoch_seconds(reset_ms)
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    return percent, reset


def _build_scopes(
    interval_percent: float | None,
    interval_reset: int | None,
    weekly_percent: float | None,
    weekly_reset: int | None,
    source: str,
) -> list[CapacityScope]:
    scopes: list[CapacityScope] = []
    if interval_percent is not None:
        scopes.append(
            CapacityScope(
                name=SCOPE_5H,
                kind=CapacityKind.RESET_WINDOW,
                remaining_percent=interval_percent,
                reset_epoch=interval_reset,
                resets_at=interval_reset,
                source=source,
            )
        )
    if weekly_percent is not None:
        scopes.append(
            CapacityScope(
                name=SCOPE_WEEKLY,
                kind=CapacityKind.RESET_WINDOW,
                remaining_percent=weekly_percent,
                reset_epoch=weekly_reset,
                resets_at=weekly_reset,
                source=source,
            )
        )
    return scopes


def read_minimax(env: dict[str, str] | None = None) -> ProviderSnapshot:
    env = env or os.environ
    cli = minimax_cli(env)
    stats = _run_minimax_quota(env)
    interval_percent: float | None = None
    interval_reset: int | None = None
    weekly_percent: float | None = None
    weekly_reset: int | None = None
    source_parts: list[str] = []
    cli_error_reason: str | None = None
    if isinstance(stats, dict) and stats.get(MINIMAX_ERROR_PAYLOAD) is True:
        # The CLI returned an error envelope (auth/plan/network). Capture the
        # reason so we can surface it below; the env fallback may still rescue
        # the read into a usable snapshot, so we don't bail out immediately.
        cli_error_reason = str(stats.get("reason") or "quota-error")
    elif stats is not None:
        source_parts.append("mmx quota")
        if stats.get("interval_percent") is not None:
            interval_percent = stats["interval_percent"]
        if stats.get("interval_reset_ms") is not None:
            interval_reset = _epoch_seconds(stats["interval_reset_ms"])
        if stats.get("weekly_percent") is not None:
            weekly_percent = stats["weekly_percent"]
        if stats.get("weekly_reset_ms") is not None:
            weekly_reset = _epoch_seconds(stats["weekly_reset_ms"])
    env_interval_pct, env_interval_reset = _interval_from_env(env)
    env_weekly_pct, env_weekly_reset = _weekly_from_env(env)
    if any(
        env.get(k)
        for k in (
            "LLM_USAGE_MINIMAX_5H_PERCENT",
            "LLM_USAGE_MINIMAX_5H_RESET_EPOCH",
            "LLM_USAGE_MINIMAX_WEEKLY_PERCENT",
            "LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH",
        )
    ):
        source_parts.append("env")
    if interval_percent is None and env_interval_pct is not None:
        interval_percent = env_interval_pct
    if interval_reset is None and env_interval_reset is not None:
        interval_reset = env_interval_reset
    if weekly_percent is None and env_weekly_pct is not None:
        weekly_percent = env_weekly_pct
    if weekly_reset is None and env_weekly_reset is not None:
        weekly_reset = env_weekly_reset
    if not source_parts:
        source_parts.append("mmx cli")
    source = " + ".join(source_parts)
    scopes = _build_scopes(
        interval_percent,
        interval_reset,
        weekly_percent,
        weekly_reset,
        source,
    )
    if not scopes:
        # Prefer the CLI's own error reason when it returned an error
        # envelope — that is a more informative "why" than the catch-all
        # ``inconclusive-usage`` or the binary ``missing-cli``.
        if cli_error_reason is not None and cli is not None:
            return ProviderSnapshot(
                provider=PROVIDER_MINIMAX,
                available=False,
                reason=cli_error_reason,
                source="mmx cli",
            )
        return ProviderSnapshot(
            provider=PROVIDER_MINIMAX,
            available=False,
            reason="missing-cli" if not cli else "inconclusive-usage",
            source="mmx cli",
        )
    if not cli and not _env_fallback_present(env):
        return ProviderSnapshot(
            provider=PROVIDER_MINIMAX,
            available=False,
            reason="missing-cli",
            source=source,
            scopes=scopes,
        )
    return ProviderSnapshot(
        provider=PROVIDER_MINIMAX,
        available=True,
        source=source,
        selected_model=minimax_model(env),
        scopes=scopes,
    )


def _env_fallback_present(env: dict[str, str]) -> bool:
    return any(
        env.get(k)
        for k in (
            "LLM_USAGE_MINIMAX_5H_PERCENT",
            "LLM_USAGE_MINIMAX_5H_RESET_EPOCH",
            "LLM_USAGE_MINIMAX_WEEKLY_PERCENT",
            "LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH",
        )
    )


def minimax_command_argv(cfg_attached: bool, cwd: str, prompt: str) -> list[str]:
    """Build the default argv for launching MiniMax.

    The ``mmx`` CLI does not yet have a documented autonomous mode, so
    headless runs invoke ``mmx run --auto <prompt>`` mirroring the
    Kilo / OpenCode pattern. Attached/interactive runs use the bare
    ``mmx`` binary in the configured working directory.
    """
    if cfg_attached:
        return ["mmx"]
    return ["mmx", "run", "--auto", "-C", cwd, prompt]


__all__ = [
    "DEFAULT_MINIMAX_MODEL",
    "minimax_cli",
    "minimax_command_argv",
    "minimax_model",
    "read",
    "read_minimax",
]


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    """Consistent with the other provider modules: ``read(env)`` returns
    a :class:`ProviderSnapshot`. The actual implementation lives in
    :func:`read_minimax`; this is the public name used by
    :mod:`llm_tools.providers` callers.
    """
    return read_minimax(env)
