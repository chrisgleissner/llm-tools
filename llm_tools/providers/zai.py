"""z.AI (Zhipu AI) provider adapter.

z.AI exposes the GLM family (e.g. ``GLM-4.7``, ``GLM-5.2``) through an
OpenAI/Anthropic-compatible API and a per-account quota monitor:

    GET https://api.z.ai/api/monitor/usage/quota/limit
    GET https://open.bigmodel.cn/api/monitor/usage/quota/limit  (CN fallback)

The endpoint returns a JSON envelope of the form::

    {
      "code": 200,
      "msg": "success",
      "data": {
        "level": "lite",
        "limits": [
          {"type": "TIME_LIMIT",    "percentage": 0,  "remaining": 0, "nextResetTime": 1781431200000},
          {"type": "TOKENS_LIMIT",  "percentage": 18, "remaining": 82, "nextResetTime": 1781431200000}
        ]
      }
    }

The reader tries three sources in order, mirroring the Codex / Claude /
Copilot / minimax pattern of preferring real data and falling back to
deterministic env vars:

1. ``GET https://api.z.ai/api/monitor/usage/quota/limit`` with a bearer
   token from ``ZAI_API_KEY`` (preferred), or
   ``https://open.bigmodel.cn/api/monitor/usage/quota/limit`` as the
   China fallback.
2. An injected usage payload via
   ``LLM_USAGE_ZAI_QUOTA_LIMIT_JSON`` for hermetic tests.
3. Environment variables:

   * ``LLM_USAGE_ZAI_5H_PERCENT`` - remaining percent for the 5h
     session window (0..100).
   * ``LLM_USAGE_ZAI_5H_RESET_EPOCH`` - epoch seconds (or
     milliseconds) when the 5h window resets.
   * ``LLM_USAGE_ZAI_WEEKLY_PERCENT`` - remaining percent for the
     weekly window (0..100).
   * ``LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH`` - epoch seconds (or
     milliseconds) when the weekly window resets.
   * ``LLM_USAGE_ZAI_MODEL`` - the GLM model pin the route selected
     (display-only; not used for gating).
   * ``LLM_USAGE_ZAI_TIMEOUT`` - HTTP timeout in seconds (default 10).
   * ``LLM_USAGE_ZAI_API_KEY`` - overrides ``ZAI_API_KEY``.

The reader only emits a snapshot when at least one of the two scopes
(5h, weekly) has data, so ``llm-usage`` quietly hides the zai row when
no API key / endpoint is reachable and no env-var fallback is
configured. The provider is a pure *capacity* source: launching a
``zai`` model is done through Kilo (or OpenCode) with the model's
``zai/<model>`` id, so the snapshot reader does not need a CLI on
PATH. The route layer wires launch + capacity together.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_ZAI,
    ProviderSnapshot,
    SCOPE_5H,
    SCOPE_WEEKLY,
)


DEFAULT_ZAI_TIMEOUT = 10


# Known z.AI quota endpoint hosts. The international endpoint is tried
# first; the China fallback is only consulted on network failure.
ZAI_QUOTA_ENDPOINTS: tuple[str, ...] = (
    "https://api.z.ai/api/monitor/usage/quota/limit",
    "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
)


# Mapping of z.AI ``type`` field to the 5h/weekly bucket we surface.
# z.AI today uses ``TIME_LIMIT`` (5h time limit) and ``TOKENS_LIMIT``
# (token-based monthly cap), plus a weekly quota label. We treat the
# *shortest* reset horizon as the 5h window and the *longest* reset
# horizon as the weekly window, but the explicit ``type`` mapping wins
# when the labels are unambiguous.
ZAI_TYPE_5H = frozenset({"TIME_LIMIT", "TIME_LIMIT_5H", "5H_LIMIT"})
ZAI_TYPE_WEEKLY = frozenset({"WEEKLY_LIMIT", "WEEKLY", "WEEKLY_QUOTA"})


# z.AI reports each limit's window length as ``number`` x ``unit``, where
# ``unit`` is a time-unit enum (observed: 3=hour, 5=month, 6=week). We
# classify the 5h/weekly rows by this *window length* rather than the
# ``type`` label, because z.AI reuses ``TIME_LIMIT`` / ``TOKENS_LIMIT``
# across several windows: the coding plan's 5-hour and weekly prompt
# quotas (the rows we surface) plus a separate ~monthly tool/search quota
# that must NOT be mistaken for the 5-hour window. The label-based mapping
# below is kept only as a fallback for payloads without ``unit``/``number``.
_ZAI_UNIT_SECONDS: dict[int, int] = {
    1: 1,            # second
    2: 60,           # minute
    3: 3600,         # hour
    4: 86_400,       # day
    5: 30 * 86_400,  # month (~30d)
    6: 7 * 86_400,   # week
}

# A window <= 1 day is the short rolling ("5h") bucket; a window of roughly
# a week is the weekly bucket; anything longer (monthly+) is surfaced by
# neither row.
_ZAI_5H_MAX_WINDOW = 86_400
_ZAI_WEEKLY_MIN_WINDOW = 3 * 86_400
_ZAI_WEEKLY_MAX_WINDOW = 10 * 86_400
_ZAI_WEEKLY_TARGET = 7 * 86_400


# Co-installed agents whose credential stores we read. z.AI is reached
# through Kilo (or OpenCode); their ``auth.json`` (mode 0600, owner-only)
# is the zero-config source of the key, so llm-tools never has to store
# an API key of its own.
_AGENT_AUTH_APPS: tuple[str, ...] = ("kilo", "opencode")


def _agent_auth_key(env: dict[str, str], provider: str) -> str | None:
    """Return ``provider``'s API key from a co-installed agent's auth store.

    Kilo and OpenCode persist provider credentials in
    ``$XDG_DATA_HOME/<app>/auth.json`` (defaulting to
    ``~/.local/share/<app>/auth.json``), owner-only, as
    ``{"<provider>": {"type": "api", "key": "..."}}``. We read the key
    from there the same way the Claude/Codex readers read their CLIs'
    own credential files -- so adding a z.AI account to Kilo lights up
    the dashboard row with no llm-tools-side configuration.

    The path is derived *strictly* from ``env`` (no fallback to the
    real home directory), so an env without ``HOME``/``XDG_DATA_HOME``
    discovers nothing and tests stay hermetic.
    """
    data_home = (env.get("XDG_DATA_HOME") or "").strip()
    if not data_home:
        home = (env.get("HOME") or "").strip()
        if not home:
            return None
        data_home = os.path.join(home, ".local", "share")
    for app in _AGENT_AUTH_APPS:
        path = os.path.join(data_home, app, "auth.json")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        entry = data.get(provider)
        if isinstance(entry, dict):
            key = entry.get("key")
            if isinstance(key, str) and key.strip():
                return key.strip()
    return None


def zai_api_key(env: dict[str, str] | None = None) -> str | None:
    """Resolve the bearer token used against the z.AI quota endpoint.

    Precedence: ``LLM_USAGE_ZAI_API_KEY`` (test override) > ``ZAI_API_KEY``
    > the key Kilo/OpenCode already persisted in their owner-only
    ``auth.json`` (``$XDG_DATA_HOME/{kilo,opencode}/auth.json``). The
    final step keeps llm-tools zero-config: authenticating z.AI in Kilo
    is enough -- no API key is ever configured in llm-tools itself.
    """
    env = env or os.environ
    override = (env.get("LLM_USAGE_ZAI_API_KEY") or "").strip()
    if override:
        return override
    primary = (env.get("ZAI_API_KEY") or "").strip()
    if primary:
        return primary
    discovered = _agent_auth_key(env, "zai")
    if discovered:
        return discovered
    return None


def zai_model(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    raw = (env.get("LLM_USAGE_ZAI_MODEL") or "").strip()
    return raw or None


def zai_timeout(env: dict[str, str] | None = None) -> int:
    env = env or os.environ
    raw = env.get("LLM_USAGE_ZAI_TIMEOUT", str(DEFAULT_ZAI_TIMEOUT))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_ZAI_TIMEOUT
    return value if value > 0 else DEFAULT_ZAI_TIMEOUT


# --- Payload parsing ----------------------------------------------------------


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


def _epoch_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    if value > 10_000_000_000:
        return int(value // 1000)
    return int(value)


def _extract_limits(payload: Any) -> list[dict[str, Any]]:
    """Pull ``data.limits`` out of a z.AI quota payload, tolerating
    minor envelope drift.

    The endpoint has historically wrapped the result in a ``{code, msg,
    data}`` envelope, but we accept a flat ``{limits: [...]}`` shape
    too so a future API tweak does not silently brick the reader.
    """
    if not isinstance(payload, dict):
        return []
    if "limits" in payload and isinstance(payload["limits"], list):
        return [item for item in payload["limits"] if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("limits"), list):
        return [item for item in data["limits"] if isinstance(item, dict)]
    return []


# Stable reason codes surfaced for the dashboard / scheduler.
ZAI_REASON_AUTH = "not-authenticated"
ZAI_REASON_PLAN = "subscription-required"
ZAI_REASON_QUOTA = "quota-error"
ZAI_REASON_NETWORK = "network-error"


def _classify_zai_error(code: int | None, message: str) -> str:
    """Map a z.AI error code/message pair to a stable reason code.

    Order matters: the subscription-message check runs before the auth
    check because z.AI returns ``403 subscription required`` for users
    whose plan does not include the quota endpoint, which is a
    distinct state from a bad API key. Rate-limit matching covers both
    the explicit ``429`` and the canonical ``Too Many Requests`` /
    ``quota exceeded`` phrases so a hard quota hit does not collapse
    to the generic ``quota-error`` reason.
    """
    text = (message or "").lower()
    if "plan" in text or "subscribe" in text or "subscription" in text or "no active" in text:
        return ZAI_REASON_PLAN
    if code == 429 or "rate" in text or "throttle" in text or "too many" in text or "quota exceeded" in text:
        return "rate-limited"
    if code in (401, 403) or "auth" in text or "token" in text or "apikey" in text or "api key" in text:
        return ZAI_REASON_AUTH
    if (
        "timeout" in text
        or "network" in text
        or "connection" in text
        or "unreachable" in text
        or "econn" in text
    ):
        return ZAI_REASON_NETWORK
    return ZAI_REASON_QUOTA


def _extract_error_envelope(payload: Any) -> dict[str, Any] | None:
    """Return ``{"code", "message", "reason"}`` when the payload is a
    z.AI error envelope rather than a successful quota response.
    """
    if not isinstance(payload, dict):
        return None
    if "data" in payload and isinstance(payload["data"], dict) and "limits" in payload["data"]:
        return None
    code = _safe_int(payload.get("code"))
    msg = payload.get("msg")
    if code is None and not isinstance(msg, str):
        return None
    if code == 200 or (code is None and "limits" in payload):
        return None
    return {
        "code": code,
        "message": str(msg or ""),
        "reason": _classify_zai_error(code, str(msg or "")),
    }


# Sentinel returned by ``_fetch_zai_quota`` when the API call returned
# a parsed error envelope. Lets ``read_zai`` surface the reason even
# when an env-var fallback is also configured.
ZAI_ERROR_PAYLOAD = "__zai_error_payload__"


def _classify_limit_type(name: str) -> str | None:
    n = (name or "").strip().upper()
    if n in ZAI_TYPE_5H:
        return SCOPE_5H
    if n in ZAI_TYPE_WEEKLY:
        return SCOPE_WEEKLY
    return None


def _window_seconds(entry: dict[str, Any]) -> int | None:
    """Length of a limit's window in seconds, from ``number`` x ``unit``.

    Returns ``None`` when either field is missing or the unit code is not
    in :data:`_ZAI_UNIT_SECONDS`, so the caller can fall back to the
    label/horizon heuristic.
    """
    unit = _safe_int(entry.get("unit"))
    number = _safe_int(entry.get("number"))
    if unit is None or number is None or number <= 0:
        return None
    base = _ZAI_UNIT_SECONDS.get(unit)
    if base is None:
        return None
    return base * number


def _window_bucket(entry: dict[str, Any]) -> str | None:
    """Classify a limit by its window length: ``SCOPE_5H``, ``SCOPE_WEEKLY``,
    ``"long"`` (monthly+, surfaced by neither row), or ``None`` (unknown)."""
    secs = _window_seconds(entry)
    if secs is None:
        return None
    if secs <= _ZAI_5H_MAX_WINDOW:
        return SCOPE_5H
    if _ZAI_WEEKLY_MIN_WINDOW <= secs <= _ZAI_WEEKLY_MAX_WINDOW:
        return SCOPE_WEEKLY
    return "long"


def _parse_zai_limits(limits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pick the 5h and weekly rows out of a ``limits`` array.

    The primary signal is each limit's *window length* (``number`` x
    ``unit``): the shortest sub-day window is the 5h row, a ~one-week
    window is the weekly row, and longer (monthly) windows — e.g. z.AI's
    separate tool/search quota — are surfaced by neither. This is what
    keeps the 5-hour row from being captured by the monthly ``TIME_LIMIT``
    entry that resets weeks out.

    When ``unit``/``number`` are absent (older payload shape, tests) the
    reader falls back to the ``type`` label (``TIME_LIMIT`` -> 5h,
    ``WEEKLY_LIMIT`` -> weekly) and, for ambiguous duplicates, the reset
    horizon (shortest -> 5h, longest -> weekly).
    """
    by_name: dict[str, dict[str, Any]] = {}

    # Pass 1 — deterministic classification by window length.
    five_candidates: list[dict[str, Any]] = []
    weekly_candidates: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for entry in limits:
        bucket = _window_bucket(entry)
        if bucket == SCOPE_5H:
            five_candidates.append(entry)
        elif bucket == SCOPE_WEEKLY:
            weekly_candidates.append(entry)
        elif bucket == "long":
            continue  # monthly+ quota: not a row we surface
        else:
            remaining.append(entry)
    if five_candidates:
        five_candidates.sort(key=lambda e: _window_seconds(e) or 0)
        by_name[SCOPE_5H] = five_candidates[0]
    if weekly_candidates:
        weekly_candidates.sort(key=lambda e: abs((_window_seconds(e) or 0) - _ZAI_WEEKLY_TARGET))
        by_name[SCOPE_WEEKLY] = weekly_candidates[0]

    # Pass 2 — label + horizon fallback for entries with no usable window.
    claimable_5h = SCOPE_5H not in by_name
    claimable_weekly = SCOPE_WEEKLY not in by_name
    unclassified: list[dict[str, Any]] = []
    for entry in remaining:
        name = str(entry.get("type") or "")
        bucket = _classify_limit_type(name)
        if bucket == SCOPE_5H and claimable_5h:
            by_name[SCOPE_5H] = entry
            claimable_5h = False
        elif bucket == SCOPE_WEEKLY and claimable_weekly:
            by_name[SCOPE_WEEKLY] = entry
            claimable_weekly = False
        else:
            unclassified.append(entry)
    if unclassified:
        horizons: list[tuple[int, dict[str, Any]]] = []
        for entry in unclassified:
            reset = _epoch_seconds(_safe_int(entry.get("nextResetTime")))
            if reset is None:
                continue
            horizons.append((reset, entry))
        if horizons:
            horizons.sort(key=lambda item: item[0])
            if claimable_5h and horizons:
                by_name[SCOPE_5H] = horizons[0][1]
                claimable_5h = False
                horizons = horizons[1:]
            if claimable_weekly and horizons:
                by_name[SCOPE_WEEKLY] = horizons[-1][1]
                claimable_weekly = False
                horizons = horizons[:-1]
    return by_name


def _limit_to_scope(name: str, entry: dict[str, Any]) -> CapacityScope | None:
    """Translate a single z.AI ``limits`` row into a :class:`CapacityScope`."""
    # z.AI reports ``percentage`` as the *used* percent; flip to remaining.
    used = _safe_float(entry.get("percentage"))
    remaining = _safe_float(entry.get("remaining"))
    if remaining is None and used is not None:
        remaining = max(0.0, min(100.0, 100.0 - used))
    if remaining is None:
        return None
    remaining = max(0.0, min(100.0, remaining))
    reset_ms = _safe_int(entry.get("nextResetTime"))
    reset_epoch = _epoch_seconds(reset_ms)
    return CapacityScope(
        name=name,
        kind=CapacityKind.RESET_WINDOW,
        remaining_percent=remaining,
        reset_epoch=reset_epoch,
        resets_at=reset_epoch,
        source="z.ai api",
        extras={"type": str(entry.get("type") or ""), "remaining": entry.get("remaining")},
    )


def _fetch_zai_quota(env: dict[str, str]) -> dict[str, Any] | None:
    """Return ``{SCOPE_5H: scope, SCOPE_WEEKLY: scope}`` or ``None``.

    The sentinel :data:`ZAI_ERROR_PAYLOAD` is returned when the API
    returned a parsed error envelope so :func:`read_zai` can surface
    the classified reason without dropping the env-var fallback. When
    every reachable endpoint returned an HTTP/auth error the *last*
    failure is also surfaced as :data:`ZAI_ERROR_PAYLOAD` so a
    misconfigured key reads as ``not-authenticated`` instead of the
    generic ``inconclusive-usage``.
    """
    key = zai_api_key(env)
    if not key:
        return None
    timeout = zai_timeout(env)
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    parsed: Any = None
    last_error: dict[str, Any] | None = None
    for url in ZAI_QUOTA_ENDPOINTS:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # HTTP 401/403 is unambiguous; do not bother hitting the
            # fallback endpoint for the same auth failure.
            last_error = _classify_http_error(url, exc)
            if exc.code in (401, 403):
                break
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = _classify_transport_error(url, exc)
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = {
                "reason": ZAI_REASON_QUOTA,
                "message": f"invalid JSON: {exc}",
            }
            continue
        # Got a response; stop trying fallback endpoints.
        break
    if parsed is None:
        if last_error is not None:
            return {
                ZAI_ERROR_PAYLOAD: True,
                "reason": last_error["reason"],
                "message": last_error.get("message", ""),
            }
        return None
    envelope = _extract_error_envelope(parsed)
    if envelope is not None:
        return {
            ZAI_ERROR_PAYLOAD: True,
            "reason": envelope["reason"],
            "message": envelope["message"],
        }
    limits = _extract_limits(parsed)
    if not limits:
        return {
            ZAI_ERROR_PAYLOAD: True,
            "reason": ZAI_REASON_QUOTA,
            "message": "empty limits",
        }
    named = _parse_zai_limits(limits)
    scopes: dict[str, Any] = {}
    for name in (SCOPE_5H, SCOPE_WEEKLY):
        entry = named.get(name)
        if not entry:
            continue
        scope = _limit_to_scope(name, entry)
        if scope is not None:
            scopes[name] = scope
    if not scopes:
        return {
            ZAI_ERROR_PAYLOAD: True,
            "reason": ZAI_REASON_QUOTA,
            "message": "no parseable 5h/weekly limit",
        }
    return scopes


def _classify_http_error(url: str, exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Classify an :class:`urllib.error.HTTPError` into a stable reason.

    Auth errors short-circuit the second endpoint (no point retrying
    with the same bad token). Non-auth errors keep the fallback host
    in play.
    """
    code = getattr(exc, "code", None)
    reason = _classify_zai_error(code, str(getattr(exc, "reason", "")))
    message = f"HTTP {code} from {url}: {exc.reason}" if code else str(exc)
    return {"reason": reason, "message": message}


def _classify_transport_error(url: str, exc: BaseException) -> dict[str, Any]:
    """Classify a network/timeout error into a stable reason."""
    text = str(exc) if exc else ""
    reason = _classify_zai_error(None, text)
    if reason == ZAI_REASON_QUOTA and text:
        # Pure network failure: re-classify into the network bucket.
        reason = ZAI_REASON_NETWORK
    return {"reason": reason, "message": f"{type(exc).__name__}: {text} (url={url})"}


def _parse_injected_payload(env: dict[str, str]) -> dict[str, Any] | None:
    """Test path: parse ``LLM_USAGE_ZAI_QUOTA_LIMIT_JSON`` into scopes.

    Accepts both the raw API shape (``{data:{limits:[...]}}``) and the
    pre-decoded scopes shape (``{5h:{remaining,reset},weekly:{...}}``)
    so tests can drive either layer.
    """
    raw = env.get("LLM_USAGE_ZAI_QUOTA_LIMIT_JSON")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if "5h" in payload or "weekly" in payload:
        return payload
    limits = _extract_limits(payload)
    if not limits:
        envelope = _extract_error_envelope(payload)
        if envelope is not None:
            return {ZAI_ERROR_PAYLOAD: True, "reason": envelope["reason"], "message": envelope["message"]}
        # A successful envelope with no limits means the API responded but
        # the user has nothing to show. Surface that as a quota-error so the
        # caller knows "we measured, got nothing" rather than "we never asked".
        if "data" in payload or "limits" in payload or "code" in payload:
            return {ZAI_ERROR_PAYLOAD: True, "reason": ZAI_REASON_QUOTA, "message": "empty limits"}
        return None
    named = _parse_zai_limits(limits)
    out: dict[str, Any] = {}
    for name in (SCOPE_5H, SCOPE_WEEKLY):
        entry = named.get(name)
        if not entry:
            continue
        scope = _limit_to_scope(name, entry)
        if scope is not None:
            out[name] = scope
    return out or None


def _interval_from_env(env: dict[str, str]) -> tuple[float | None, int | None]:
    percent = _safe_float(env.get("LLM_USAGE_ZAI_5H_PERCENT"))
    reset_ms = _safe_int(env.get("LLM_USAGE_ZAI_5H_RESET_EPOCH"))
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    return percent, _epoch_seconds(reset_ms)


def _weekly_from_env(env: dict[str, str]) -> tuple[float | None, int | None]:
    percent = _safe_float(env.get("LLM_USAGE_ZAI_WEEKLY_PERCENT"))
    reset_ms = _safe_int(env.get("LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"))
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    return percent, _epoch_seconds(reset_ms)


def _env_fallback_present(env: dict[str, str]) -> bool:
    """Whether any env-var *fallback* scope signal is set.

    Mirrors ``minimax._env_fallback_present``. Deliberately excludes
    ``LLM_USAGE_ZAI_QUOTA_LIMIT_JSON`` -- that variable drives the
    injected-payload path (tagged ``z.ai injected``), not the env-var
    fallback (tagged ``env``), so it must not flip the fallback source on.
    """
    return any(
        env.get(k)
        for k in (
            "LLM_USAGE_ZAI_5H_PERCENT",
            "LLM_USAGE_ZAI_5H_RESET_EPOCH",
            "LLM_USAGE_ZAI_WEEKLY_PERCENT",
            "LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH",
        )
    )


def _build_scopes(
    five_percent: float | None,
    five_reset: int | None,
    weekly_percent: float | None,
    weekly_reset: int | None,
    source: str,
) -> list[CapacityScope]:
    scopes: list[CapacityScope] = []
    if five_percent is not None:
        scopes.append(
            CapacityScope(
                name=SCOPE_5H,
                kind=CapacityKind.RESET_WINDOW,
                remaining_percent=five_percent,
                reset_epoch=five_reset,
                resets_at=five_reset,
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


def read_zai(env: dict[str, str] | None = None) -> ProviderSnapshot:
    env = env or os.environ
    api_error_reason: str | None = None
    api_source_marker: str | None = None
    five_percent: float | None = None
    five_reset: int | None = None
    weekly_percent: float | None = None
    weekly_reset: int | None = None
    source_parts: list[str] = []

    # 1. Injected test payload wins over everything (hermetic tests).
    injected = _parse_injected_payload(env)
    if isinstance(injected, dict) and injected.get(ZAI_ERROR_PAYLOAD) is True:
        api_error_reason = str(injected.get("reason") or ZAI_REASON_QUOTA)
    elif isinstance(injected, dict) and injected:
        source_parts.append("z.ai injected")
        api_source_marker = "z.ai injected"
        five = injected.get(SCOPE_5H)
        weekly = injected.get(SCOPE_WEEKLY)
        if isinstance(five, dict):
            five_percent = _safe_float(five.get("remaining_percent"))
            five_reset = _epoch_seconds(_safe_int(five.get("reset_epoch")))
        if isinstance(weekly, dict):
            weekly_percent = _safe_float(weekly.get("remaining_percent"))
            weekly_reset = _epoch_seconds(_safe_int(weekly.get("reset_epoch")))

    # 2. Live API call (only when the injected payload did not produce
    #    an authoritative answer, to keep tests hermetic).
    if not source_parts and zai_api_key(env) and not api_error_reason:
        scopes = _fetch_zai_quota(env)
        if isinstance(scopes, dict) and scopes.get(ZAI_ERROR_PAYLOAD) is True:
            api_error_reason = str(scopes.get("reason") or ZAI_REASON_QUOTA)
        elif isinstance(scopes, dict) and scopes:
            source_parts.append("z.ai api")
            api_source_marker = "z.ai api"
            five_scope = scopes.get(SCOPE_5H)
            weekly_scope = scopes.get(SCOPE_WEEKLY)
            if isinstance(five_scope, CapacityScope):
                five_percent = five_scope.remaining_percent
                five_reset = five_scope.reset_epoch
            if isinstance(weekly_scope, CapacityScope):
                weekly_percent = weekly_scope.remaining_percent
                weekly_reset = weekly_scope.reset_epoch

    # 3. Env-var fallback.
    env_five_pct, env_five_reset = _interval_from_env(env)
    env_weekly_pct, env_weekly_reset = _weekly_from_env(env)
    if _env_fallback_present(env):
        source_parts.append("env")
    if five_percent is None and env_five_pct is not None:
        five_percent = env_five_pct
    if five_reset is None and env_five_reset is not None:
        five_reset = env_five_reset
    if weekly_percent is None and env_weekly_pct is not None:
        weekly_percent = env_weekly_pct
    if weekly_reset is None and env_weekly_reset is not None:
        weekly_reset = env_weekly_reset

    if not source_parts:
        source_parts.append("z.ai api" if zai_api_key(env) else "z.ai env")

    source = " + ".join(source_parts)
    scopes = _build_scopes(five_percent, five_reset, weekly_percent, weekly_reset, source)

    if not scopes:
        if api_error_reason is not None:
            return ProviderSnapshot(
                provider=PROVIDER_ZAI,
                available=False,
                reason=api_error_reason,
                source="z.ai api",
            )
        return ProviderSnapshot(
            provider=PROVIDER_ZAI,
            available=False,
            reason="inconclusive-usage",
            source=source,
        )

    return ProviderSnapshot(
        provider=PROVIDER_ZAI,
        available=True,
        source=source,
        selected_model=zai_model(env),
        scopes=scopes,
    )


__all__ = [
    "DEFAULT_ZAI_TIMEOUT",
    "ZAI_ERROR_PAYLOAD",
    "ZAI_QUOTA_ENDPOINTS",
    "ZAI_REASON_AUTH",
    "ZAI_REASON_NETWORK",
    "ZAI_REASON_PLAN",
    "ZAI_REASON_QUOTA",
    "read",
    "read_zai",
    "zai_api_key",
    "zai_model",
    "zai_timeout",
]


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    """Consistent with the other provider modules: ``read(env)`` returns a
    :class:`ProviderSnapshot`. The actual implementation lives in
    :func:`read_zai`; this is the public name used by
    :mod:`llm_tools.providers` callers.
    """
    return read_zai(env)
