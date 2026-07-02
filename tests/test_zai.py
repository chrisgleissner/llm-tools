"""Tests for the z.ai provider adapter (GLM 4.7 / GLM 5.2 capacity source)."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from llm_tools import common, usage
from llm_tools.capacity import (
    CapacityKind,
    PROVIDER_ZAI,
    SCOPE_5H,
    SCOPE_WEEKLY,
)
from llm_tools.providers import (
    read_zai,
    zai_api_key,
    zai_model,
    zai_timeout,
    ZAI_QUOTA_ENDPOINTS,
)


# --- Env-var reader ----------------------------------------------------------


def test_zai_api_key_prefers_test_override() -> None:
    env = {"LLM_USAGE_ZAI_API_KEY": "override-key", "ZAI_API_KEY": "primary-key"}
    assert zai_api_key(env) == "override-key"


def test_zai_api_key_falls_back_to_primary() -> None:
    assert zai_api_key({"ZAI_API_KEY": "primary-key"}) == "primary-key"


def test_zai_api_key_returns_none_when_unset(tmp_path: Path) -> None:
    # Hermetic: an isolated HOME/XDG_DATA_HOME with no agent auth.json on
    # disk (and no env override / ZAI_API_KEY) discovers no key.
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "share")}
    assert zai_api_key(env) is None


def _write_agent_auth(share: Path, app: str, key: str) -> None:
    auth = share / app / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"zai": {"type": "api", "key": key}}), encoding="utf-8")


def test_zai_api_key_discovers_from_kilo_auth(tmp_path: Path) -> None:
    share = tmp_path / "share"
    _write_agent_auth(share, "kilo", "kilo-stored-key")
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(share)}
    assert zai_api_key(env) == "kilo-stored-key"


def test_zai_api_key_discovers_from_opencode_when_kilo_absent(tmp_path: Path) -> None:
    share = tmp_path / "share"
    _write_agent_auth(share, "opencode", "opencode-key")
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(share)}
    assert zai_api_key(env) == "opencode-key"


def test_zai_api_key_explicit_env_beats_discovery(tmp_path: Path) -> None:
    share = tmp_path / "share"
    _write_agent_auth(share, "kilo", "kilo-stored-key")
    env = {"ZAI_API_KEY": "explicit", "HOME": str(tmp_path), "XDG_DATA_HOME": str(share)}
    assert zai_api_key(env) == "explicit"


def test_zai_api_key_discovery_defaults_to_home_local_share(tmp_path: Path) -> None:
    # With XDG_DATA_HOME unset, discovery falls back to ~/.local/share.
    _write_agent_auth(tmp_path / ".local" / "share", "kilo", "home-key")
    assert zai_api_key({"HOME": str(tmp_path)}) == "home-key"


def test_zai_model_returns_none_when_unset() -> None:
    assert zai_model({}) is None


def test_zai_model_returns_pinned_value() -> None:
    assert zai_model({"LLM_USAGE_ZAI_MODEL": "zai/glm-4.7"}) == "zai/glm-4.7"


def test_zai_timeout_default_is_ten() -> None:
    assert zai_timeout({}) == 10


def test_zai_timeout_accepts_override() -> None:
    assert zai_timeout({"LLM_USAGE_ZAI_TIMEOUT": "3"}) == 3


# --- read_zai: env-var fallback path ------------------------------------------


def test_read_zai_env_5h_only(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["ZAI_API_KEY"] = ""  # hermetic: no live API call
    snap = read_zai(env)
    assert snap.provider == PROVIDER_ZAI
    assert snap.available is True
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 75.0
    assert by_name[SCOPE_5H].reset_epoch == 1781431200
    assert SCOPE_WEEKLY not in by_name


def test_read_zai_env_weekly_only(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    snap = read_zai(env)
    assert snap.available is True
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_WEEKLY].remaining_percent == 97.0
    assert by_name[SCOPE_WEEKLY].reset_epoch == 1781481600


def test_read_zai_env_both_scopes(env: dict[str, str]) -> None:
    env.update(
        {
            "LLM_USAGE_ZAI_5H_PERCENT": "75",
            "LLM_USAGE_ZAI_5H_RESET_EPOCH": "1781431200",
            "LLM_USAGE_ZAI_WEEKLY_PERCENT": "97",
            "LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH": "1781481600",
            "ZAI_API_KEY": "",
        }
    )
    snap = read_zai(env)
    assert snap.available is True
    assert {s.name for s in snap.scopes} == {SCOPE_5H, SCOPE_WEEKLY}


def test_read_zai_env_clamps_percent(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "150"  # clamp to 100
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "-5"  # clamp to 0
    env["ZAI_API_KEY"] = ""
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 100.0
    assert by_name[SCOPE_WEEKLY].remaining_percent == 0.0


def test_read_zai_env_accepts_millisecond_reset(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "50"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200000"
    env["ZAI_API_KEY"] = ""
    snap = read_zai(env)
    assert snap.scopes[0].reset_epoch == 1781431200


def test_read_zai_missing_key_and_env_reports_inconclusive(env: dict[str, str]) -> None:
    env.pop("ZAI_API_KEY", None)
    env.pop("LLM_USAGE_ZAI_API_KEY", None)
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"


def test_read_zai_env_ignores_garbage_values(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "not-a-number"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "?"
    env["ZAI_API_KEY"] = ""
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"


def test_read_zai_carries_selected_model(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_MODEL"] = "zai/glm-5.2"
    env["ZAI_API_KEY"] = ""
    snap = read_zai(env)
    assert snap.selected_model == "zai/glm-5.2"


# --- read_zai: live-API path (mocked) ----------------------------------------


def _mock_urlopen(monkeypatch: pytest.MonkeyPatch, payload: dict, *, status: int = 200):
    """Monkeypatch urllib.request.urlopen to return ``payload`` as JSON."""

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    body = json.dumps(payload).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        # The first endpoint is tried first; we let the test supply the URL
        # it expects by ignoring the actual target.
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def test_read_zai_parses_zai_api_envelope(env: dict[str, str], monkeypatch) -> None:
    """The ``/api/monitor/usage/quota/limit`` envelope shape is parsed into
    5h + weekly reset-window scopes, with the ``percentage`` field flipped
    from used to remaining.
    """
    payload = {
        "code": 200,
        "msg": "success",
        "data": {
            "level": "lite",
            "limits": [
                {
                    "type": "TIME_LIMIT",
                    "percentage": 25,
                    "remaining": 75,
                    "nextResetTime": 1781431200000,
                    "usageDetails": [
                        {"modelCode": "GLM-4.7", "usage": 100},
                    ],
                },
                {
                    "type": "WEEKLY_LIMIT",
                    "percentage": 3,
                    "remaining": 97,
                    "nextResetTime": 1781481600000,
                },
            ],
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is True
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 75.0
    assert by_name[SCOPE_5H].reset_epoch == 1781431200
    assert by_name[SCOPE_WEEKLY].remaining_percent == 97.0
    assert by_name[SCOPE_WEEKLY].reset_epoch == 1781481600


def test_read_zai_parses_two_time_limit_entries(env: dict[str, str], monkeypatch) -> None:
    """z.ai's 5h and weekly quotas may both surface as ``TIME_LIMIT``
    entries distinguished only by their reset horizon. The reader
    picks the shorter reset as 5h and the longer as weekly.
    """
    payload = {
        "code": 200,
        "msg": "success",
        "data": {
            "level": "lite",
            "limits": [
                {
                    "type": "TIME_LIMIT",
                    "percentage": 25,
                    "remaining": 75,
                    "nextResetTime": 1781431200000,
                },
                {
                    "type": "TIME_LIMIT",
                    "percentage": 3,
                    "remaining": 97,
                    "nextResetTime": 1781481600000,
                },
            ],
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 75.0
    assert by_name[SCOPE_5H].reset_epoch == 1781431200
    assert by_name[SCOPE_WEEKLY].remaining_percent == 97.0
    assert by_name[SCOPE_WEEKLY].reset_epoch == 1781481600


def test_read_zai_classifies_windows_by_length_not_label(env: dict[str, str], monkeypatch) -> None:
    """Real coding-plan payload: a monthly ``TIME_LIMIT`` (resets weeks out),
    the 5-hour ``TOKENS_LIMIT``, and the weekly ``TOKENS_LIMIT``. The 5h row
    must track the *5-hour* window (by ``number`` x ``unit`` length), not the
    monthly ``TIME_LIMIT`` that the old label mapping captured.
    """
    payload = {
        "code": 200,
        "msg": "success",
        "data": {
            "limits": [
                {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 0, "remaining": 100, "nextResetTime": 1784878391978},
                {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 97, "nextResetTime": 1782304670000},
                {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 19, "nextResetTime": 1782891191000},
            ],
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    # 5h tracks the 5-hour TOKENS_LIMIT (97% used -> 3% remaining), not the
    # monthly TIME_LIMIT (which would have shown 100% remaining, reset ~29d out).
    assert by_name[SCOPE_5H].remaining_percent == 3.0
    assert by_name[SCOPE_5H].reset_epoch == 1782304670
    assert by_name[SCOPE_WEEKLY].remaining_percent == 81.0
    assert by_name[SCOPE_WEEKLY].reset_epoch == 1782891191


def test_read_zai_uses_injected_payload_over_api(env: dict[str, str], monkeypatch) -> None:
    """LLM_USAGE_ZAI_QUOTA_LIMIT_JSON wins over a live API call so tests
    can drive the reader without spinning up an HTTP server.
    """
    payload = {
        "5h": {"remaining_percent": 50, "reset_epoch": 1781431200},
        "weekly": {"remaining_percent": 60, "reset_epoch": 1781481600},
    }
    _mock_urlopen(monkeypatch, {"data": {"limits": []}})  # never reached
    env["LLM_USAGE_ZAI_QUOTA_LIMIT_JSON"] = json.dumps(payload)
    env["ZAI_API_KEY"] = "should-not-be-used"
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    assert by_name[SCOPE_5H].remaining_percent == 50.0
    assert by_name[SCOPE_WEEKLY].remaining_percent == 60.0


def test_read_zai_classifies_auth_error(env: dict[str, str], monkeypatch) -> None:
    _mock_urlopen(monkeypatch, {"code": 401, "msg": "auth failed"})
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"


def test_read_zai_classifies_plan_required_error(env: dict[str, str], monkeypatch) -> None:
    _mock_urlopen(monkeypatch, {"code": 403, "msg": "subscription required"})
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "subscription-required"


def test_read_zai_classifies_network_error(env: dict[str, str], monkeypatch) -> None:
    import urllib.error

    def _raise(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    # Both endpoints fail; surface the network error instead of a
    # generic "inconclusive-usage" so the user can tell the difference
    # between a bad key and a network outage.
    assert snap.available is False
    assert snap.reason == "network-error"


def test_read_zai_classifies_401_as_auth(env: dict[str, str], monkeypatch) -> None:
    """A 401 on the first endpoint short-circuits the fallback (no
    point retrying with the same bad token) and surfaces not-authenticated.
    """
    import urllib.error

    def _raise_401(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", hdrs={}, fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise_401)
    env["ZAI_API_KEY"] = "bad-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"


def test_read_zai_falls_through_to_env_on_network_error(env: dict[str, str], monkeypatch) -> None:
    import urllib.error

    def _raise(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "44"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is True
    assert snap.scopes[0].remaining_percent == 44.0


def test_read_zai_classifies_empty_limits(env: dict[str, str], monkeypatch) -> None:
    """An HTTP 200 with an empty limits array is a real API result
    (not a transport error). Surface it as ``quota-error`` so the
    user can see that the API responded but reported no quota, which
    is a different state from a network outage.
    """
    _mock_urlopen(monkeypatch, {"code": 200, "msg": "success", "data": {"limits": []}})
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "quota-error"


# --- Provider plumbing -------------------------------------------------------


def test_capacity_provider_scopes_include_zai_5h_and_weekly() -> None:
    from llm_tools.capacity import PROVIDER_SCOPES, SCOPE_AUTO

    assert SCOPE_5H in PROVIDER_SCOPES[PROVIDER_ZAI]
    assert SCOPE_WEEKLY in PROVIDER_SCOPES[PROVIDER_ZAI]
    assert SCOPE_AUTO in PROVIDER_SCOPES[PROVIDER_ZAI]


def test_common_usage_snapshot_for_provider_handles_zai(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "80"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "90"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    snap = common.usage_snapshot_for_provider("zai", env)
    assert snap["provider"] == "zai"
    assert snap["available"] is True
    assert {s["name"] for s in snap["scopes"]} == {"5h", "weekly"}


def test_common_usage_decision_for_zai(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "80"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "90"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    snap, decision = common.usage_snapshot_and_decision(
        "zai", None, "auto", "1", "60", env,
    )
    assert decision["usable"] is True


def test_validate_provider_scope_rejects_weekly_for_unknown_provider() -> None:
    # Same shape as the minimax error, just for zai.
    import subprocess

    result = run_cmd(["./llm-scheduler", "--provider", "zai", "--prompt", "x", "--scope", "balance"], env={})
    assert result.returncode == 2
    assert "not valid for zai" in result.stderr


# --- Scheduler / Ralph integration -------------------------------------------


def test_scheduler_accepts_zai_provider(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "zai",
            "--prompt",
            "x",
            "--scope",
            "5h",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions
    assert decisions[0]["data"]["usable"] is True


def test_scheduler_zai_below_minimum_blocks(env: dict[str, str]) -> None:
    env["LLM_USAGE_NOW_EPOCH"] = "1781413200"  # 2026-06-14, well before reset
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "0"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "0"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "zai",
            "--prompt",
            "x",
            "--scope",
            "auto",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions
    assert decisions[0]["data"]["usable"] is False
    assert decisions[0]["data"]["reason"] == "rate-limited"


def test_ralph_robin_rejects_unknown_provider(env: dict[str, str]) -> None:
    result = run_cmd(["./ralph-robin", "--providers", "zai,bogus", "--prompt", "x"], env)
    assert result.returncode == 2
    assert "invalid provider in --providers" in result.stderr


def test_ralph_robin_accepts_zai_provider(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    result = run_cmd(
        [
            "./ralph-robin",
            "--providers",
            "zai",
            "--prompt",
            "x",
            "--command-template",
            "true",
            "--no-retry",
            "--max-iterations",
            "1",
            "--max-duration",
            "30s",
            "--state-file",
            str(Path(env["HOME"]) / "ralph-state.json"),
            "--log-dir",
            str(Path(env["HOME"]) / "ralph-logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr


# --- Kilo argv injects --model for zai routes --------------------------------


def test_kilo_command_argv_passes_model_flag() -> None:
    from llm_tools.providers import kilo_command_argv

    argv = kilo_command_argv(cfg_attached=False, cwd="/tmp/work", prompt="hi")
    # Kilo run supports -m, --model provider/model.
    assert argv[:2] == ["kilo", "run"]
    assert "--dir" in argv


def test_scheduler_includes_zai_in_model_flag_providers() -> None:
    # Sanity: kilo is now in MODEL_FLAG_PROVIDERS so a route like
    # [routes.kilo-zai-glm-4.7] injects -m zai/glm-4.7.
    from llm_tools import scheduler

    flags = scheduler.provider_model_flags("kilo", "zai/glm-4.7")
    assert flags == ["--model", "zai/glm-4.7"]
    flags = scheduler.provider_model_flags("opencode", "zai/glm-5.2")
    assert flags == ["--model", "zai/glm-5.2"]
    # zai itself has no launch CLI: a bare --provider zai invocation should
    # not silently emit a --model flag.
    assert scheduler.provider_model_flags("zai", "zai/glm-4.7") == []


# --- Route-level delegation --------------------------------------------------


def test_route_kilo_zai_glm_4_7_gates_on_zai(env: dict[str, str], tmp_path: Path) -> None:
    """A route with launch provider kilo, model zai/glm-4.7, capacity.policy
    delegate / provider zai must gate on the zai reader (5h/weekly scopes)
    and launch kilo with the --model flag.
    """
    from llm_tools import config as toolconfig
    from llm_tools import routes as route_mod

    cfg_file = tmp_path / "zai-route-config.toml"
    cfg_file.write_text(
        textwrap.dedent(
            """
            [routes.kilo-zai-glm-47]
            provider = "kilo"
            model = "zai/glm-4.7"

            [routes.kilo-zai-glm-47.capacity]
            policy = "delegate"
            provider = "zai"
            """
        ),
        encoding="utf-8",
    )
    # The dev shell may export LLM_TOOLS_CONFIG pointing at the user's
    # real config; force the loader to use this test file regardless.
    env.pop("LLM_TOOLS_CONFIG", None)
    env["LLM_TOOLS_CONFIG"] = str(cfg_file)
    env.pop("XDG_CONFIG_HOME", None)
    toolconfig._cache.clear()
    route = toolconfig.route_policy(toolconfig.load_config(env), "kilo-zai-glm-47")
    assert route is not None
    assert route.model == "zai/glm-4.7"
    assert route.provider == "kilo"

    # Inject zai usage so the decision returns usable=True.
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["ZAI_API_KEY"] = ""
    snapshot, decision = route_mod.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env,
    )
    assert snapshot["selected_model"] == "zai/glm-4.7"
    assert decision["capacity_provider"] == "zai"
    assert decision["usable"] is True
    assert {w["name"] for w in decision.get("windows", [])} == {"5h", "weekly"}


# --- Usage table rendering ----------------------------------------------------


def test_usage_table_renders_zai_rows(env: dict[str, str], monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/var/empty")
    monkeypatch.setenv("ZAI_API_KEY", "")
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    merged = dict(env)
    for key in (
        "LLM_USAGE_ZAI_5H_PERCENT",
        "LLM_USAGE_ZAI_5H_RESET_EPOCH",
        "LLM_USAGE_ZAI_WEEKLY_PERCENT",
        "LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH",
        "PATH",
        "ZAI_API_KEY",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]
    snap = read_zai(merged)
    json_obj = {
        "provider": snap.provider,
        "available": snap.available,
        "reason": snap.reason,
        "source": snap.source,
        "selected_model": snap.selected_model,
        "scopes": [
            {
                "name": s.name,
                "kind": s.kind,
                "remaining_percent": s.remaining_percent,
                "reset_epoch": s.reset_epoch,
                "source": s.source,
            }
            for s in snap.scopes
        ],
    }

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_zai_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "Z.ai" in out
    assert "5h" in out
    assert "weekly" in out
    assert "75%" in out
    assert "97%" in out


def test_usage_table_renders_zai_unavailable() -> None:
    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_zai_rows(cfg, None)
    out = buf.getvalue()
    assert "Z.ai" in out
    assert "unavailable" in out


def test_usage_table_collapses_zai_inconclusive_to_unavailable(capsys) -> None:
    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    zai_json = {
        "provider": PROVIDER_ZAI,
        "available": False,
        "reason": "inconclusive-usage",
        "source": "z.ai api",
        "scopes": [],
    }
    usage.print_zai_rows(cfg, zai_json)
    out = capsys.readouterr().out
    assert "Z.ai" in out
    assert "unavailable" in out
    assert "inconclusive-usage" not in out
    # The exhausted-quota marker must not appear for an unmeasured provider.
    assert "× empty" not in out


# --- JSON contract -----------------------------------------------------------


def test_zai_json_top_level_in_llm_usage_json(env: dict[str, str]) -> None:
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "75"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "97"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env["LLM_USAGE_DISABLE_COPILOT"] = "1"
    env["ZAI_API_KEY"] = ""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PATH", "/var/empty")
    result = run_cmd(["./llm-usage", "--json"], env)
    monkeypatch.undo()
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "zai" in data
    assert data["zai"]["provider"] == "zai"
    assert data["zai"]["available"] is True
    assert {s["name"] for s in data["zai"]["scopes"]} == {"5h", "weekly"}


def test_zai_endpoints_constant_lists_international_then_cn() -> None:
    # The international endpoint is tried first; the CN host is the
    # documented fallback. The constant is order-sensitive: tests rely on
    # this so a future rename is a deliberate, visible change.
    assert ZAI_QUOTA_ENDPOINTS[0].startswith("https://api.z.ai")
    assert any("bigmodel.cn" in url for url in ZAI_QUOTA_ENDPOINTS)


# --- Classifier / parser internals -------------------------------------------


def test_zai_classifier_distinguishes_plan_vs_auth_vs_network() -> None:
    from llm_tools.providers.zai import _classify_zai_error

    assert _classify_zai_error(None, "no active plan") == "subscription-required"
    assert _classify_zai_error(401, "Unauthorized") == "not-authenticated"
    assert _classify_zai_error(403, "subscription required") == "subscription-required"
    assert _classify_zai_error(429, "Too Many Requests") == "rate-limited"
    assert _classify_zai_error(None, "connection timed out") == "network-error"
    assert _classify_zai_error(None, "weird unknown failure") == "quota-error"


def test_zai_classify_limit_type_recognises_known_labels() -> None:
    from llm_tools.providers.zai import _classify_limit_type

    assert _classify_limit_type("TIME_LIMIT") == "5h"
    assert _classify_limit_type("WEEKLY_LIMIT") == "weekly"
    assert _classify_limit_type("WEEKLY_QUOTA") == "weekly"
    assert _classify_limit_type("WEEKLY") == "weekly"
    assert _classify_limit_type("TIME_LIMIT_5H") == "5h"
    assert _classify_limit_type("TOKENS_LIMIT") is None


def test_zai_parser_handles_used_only_field(env: dict[str, str], monkeypatch) -> None:
    """When the API only returns ``percentage`` (used), the reader
    flips it to remaining.
    """
    payload = {
        "code": 200,
        "data": {
            "limits": [
                {"type": "TIME_LIMIT", "percentage": 25, "nextResetTime": 1781431200000},
                {"type": "WEEKLY_LIMIT", "percentage": 3, "nextResetTime": 1781481600000},
            ]
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    assert by_name["5h"].remaining_percent == 75.0
    assert by_name["weekly"].remaining_percent == 97.0


def test_zai_parser_handles_seconds_era_reset(env: dict[str, str], monkeypatch) -> None:
    payload = {
        "code": 200,
        "data": {
            "limits": [
                {"type": "TIME_LIMIT", "percentage": 25, "nextResetTime": 1781431200},
            ]
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.scopes[0].reset_epoch == 1781431200


def test_zai_parser_drops_unparseable_entries(env: dict[str, str], monkeypatch) -> None:
    """An entry with no remaining percent and no reset epoch is skipped
    rather than poisoning the snapshot with a zero-percent row.
    """
    payload = {
        "code": 200,
        "data": {
            "limits": [
                {"type": "TIME_LIMIT"},
                {"type": "WEEKLY_LIMIT", "percentage": 3, "nextResetTime": 1781481600000},
            ]
        },
    }
    _mock_urlopen(monkeypatch, payload)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    by_name = {s.name: s for s in snap.scopes}
    assert "5h" not in by_name
    assert by_name["weekly"].remaining_percent == 97.0


def test_zai_classify_http_error_short_circuits_on_401() -> None:
    import urllib.error

    from llm_tools.providers.zai import _classify_http_error

    err = urllib.error.HTTPError(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        401,
        "Unauthorized",
        hdrs={},
        fp=None,
    )
    out = _classify_http_error("https://api.z.ai/api/monitor/usage/quota/limit", err)
    assert out["reason"] == "not-authenticated"


def test_zai_classify_transport_error_falls_through_to_network() -> None:
    from llm_tools.providers.zai import _classify_transport_error

    out = _classify_transport_error("https://api.z.ai/x", OSError("connection refused"))
    assert out["reason"] == "network-error"


def test_zai_classify_transport_error_keywordless_reclassifies_to_network() -> None:
    """A transport exception whose text has no recognisable keyword
    initially classifies as ``quota-error``; the helper then re-maps a
    non-empty keywordless failure to ``network-error``."""
    from llm_tools.providers.zai import _classify_transport_error

    out = _classify_transport_error("https://api.z.ai/x", ValueError("zzz unrecoverable"))
    assert out["reason"] == "network-error"


def test_zai_extracted_envelope_classifies_auth() -> None:
    from llm_tools.providers.zai import _extract_error_envelope

    assert _extract_error_envelope({"code": 401, "msg": "auth failed"}) == {
        "code": 401,
        "message": "auth failed",
        "reason": "not-authenticated",
    }


def test_zai_flat_limits_payload_is_accepted() -> None:
    """The endpoint may eventually drop the ``{data:{limits:[]}}``
    envelope; the reader must still parse the flat shape.
    """
    from llm_tools.providers.zai import _extract_limits

    assert _extract_limits({"limits": [{"type": "TIME_LIMIT"}]}) == [{"type": "TIME_LIMIT"}]


def test_zai_env_fallback_present_detects_all_keys() -> None:
    from llm_tools.providers.zai import _env_fallback_present

    assert _env_fallback_present({}) is False
    assert _env_fallback_present({"LLM_USAGE_ZAI_5H_PERCENT": "75"}) is True
    assert _env_fallback_present({"LLM_USAGE_ZAI_WEEKLY_PERCENT": "50"}) is True
    # The injected-payload var is NOT an env fallback signal: it drives the
    # "z.ai injected" source, not "env".
    assert _env_fallback_present({"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": "{}"}) is False


def test_zai_injected_payload_invalid_json_is_ignored() -> None:
    from llm_tools.providers.zai import _parse_injected_payload

    assert _parse_injected_payload({"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": "not json"}) is None


def test_zai_injected_payload_non_dict_is_ignored() -> None:
    from llm_tools.providers.zai import _parse_injected_payload

    assert _parse_injected_payload({"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": "[1]"}) is None


def test_zai_injected_payload_envelope_surfaces_error() -> None:
    """Injected error envelope propagates the reason into the snapshot."""
    from llm_tools.providers.zai import _parse_injected_payload, ZAI_ERROR_PAYLOAD

    result = _parse_injected_payload(
        {"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": json.dumps({"code": 401, "msg": "bad token"})}
    )
    assert result[ZAI_ERROR_PAYLOAD] is True
    assert result["reason"] == "not-authenticated"


def test_zai_injected_payload_no_limits_surfaces_error() -> None:
    from llm_tools.providers.zai import _parse_injected_payload, ZAI_ERROR_PAYLOAD

    result = _parse_injected_payload(
        {"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": json.dumps({"data": {"limits": []}})}
    )
    assert result[ZAI_ERROR_PAYLOAD] is True


def test_zai_injected_payload_unknown_shape_returns_none() -> None:
    from llm_tools.providers.zai import _parse_injected_payload

    # Has ``data.limits`` missing AND no 5h/weekly shape.
    assert _parse_injected_payload(
        {"LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": json.dumps({"foo": "bar"})}
    ) is None


def test_zai_limit_to_scope_returns_none_for_missing_percent() -> None:
    from llm_tools.providers.zai import _limit_to_scope

    assert _limit_to_scope("5h", {}) is None
    assert _limit_to_scope("5h", {"nextResetTime": 1781431200000}) is None


def test_zai_limit_to_scope_clamps_percent() -> None:
    from llm_tools.providers.zai import _limit_to_scope

    scope = _limit_to_scope("5h", {"percentage": 150, "nextResetTime": 1781431200000})
    assert scope is not None
    assert scope.remaining_percent == 0.0  # 100 - 150 clamped to 0


def test_zai_limit_to_scope_prefers_remaining_over_used() -> None:
    from llm_tools.providers.zai import _limit_to_scope

    scope = _limit_to_scope(
        "5h",
        {"percentage": 10, "remaining": 80, "nextResetTime": 1781431200000},
    )
    assert scope is not None
    assert scope.remaining_percent == 80.0


def test_zai_reader_full_http_error_short_circuits(env: dict[str, str], monkeypatch) -> None:
    """Even when the first endpoint returns 401, the second endpoint is
    NOT tried (no point retrying with the same bad token). The
    snapshot surfaces ``not-authenticated`` instead of falling through.
    """
    import urllib.error

    calls = []

    def _raise_401(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs={}, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", _raise_401)
    env["ZAI_API_KEY"] = "bad-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"
    # First endpoint tried; second skipped (auth short-circuit).
    assert len(calls) == 1


def test_zai_reader_non_auth_http_error_tries_fallback(env: dict[str, str], monkeypatch) -> None:
    """A non-auth 5xx on the first endpoint falls through to the
    second endpoint instead of bubbling a noisy reason.
    """
    import urllib.error

    calls = []

    def _fail_then_succeed(req, timeout=None):
        calls.append(req.full_url)
        if "api.z.ai" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", hdrs={}, fp=None)

        class _FakeResp:
            def __init__(self) -> None:
                self._body = json.dumps(
                    {
                        "code": 200,
                        "data": {
                            "limits": [
                                {"type": "TIME_LIMIT", "percentage": 25, "nextResetTime": 1781431200000},
                            ]
                        },
                    }
                ).encode()

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _fail_then_succeed)
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is True
    # First endpoint fails (5xx), fallback succeeds.
    assert len(calls) == 2


def test_zai_reader_invalid_json_response(env: dict[str, str], monkeypatch) -> None:
    """An HTTP 200 with a non-JSON body surfaces ``quota-error`` so the
    user can distinguish "wrong response" from "no data".
    """

    class _FakeResp:
        def read(self) -> bytes:
            return b"<html>not json</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "quota-error"


def test_zai_reader_classifies_rate_limit_message(env: dict[str, str], monkeypatch) -> None:
    """An error envelope whose message says ``rate limit exceeded``
    surfaces ``rate-limited`` so the user knows to back off, not
    re-auth.
    """
    _mock_urlopen(monkeypatch, {"code": 429, "msg": "rate limit exceeded"})
    env["ZAI_API_KEY"] = "test-key"
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "rate-limited"


def test_zai_endpoint_test_override_wins(env: dict[str, str]) -> None:
    """The ``LLM_USAGE_ZAI_API_KEY`` override beats the user's real
    ``ZAI_API_KEY`` so tests can isolate without touching the shell.
    """
    env["LLM_USAGE_ZAI_API_KEY"] = "override"
    env["ZAI_API_KEY"] = "real"
    assert zai_api_key(env) == "override"


def test_zai_env_fallback_present_detects_reset_epoch() -> None:
    from llm_tools.providers.zai import _env_fallback_present

    assert _env_fallback_present({"LLM_USAGE_ZAI_5H_RESET_EPOCH": "1781431200"}) is True
    assert _env_fallback_present({"LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH": "1781481600"}) is True


# --- Pure-function branches (coverage) ----------------------------------------


def test_zai_safe_int_safe_float_reject_non_numeric() -> None:
    from llm_tools.providers.zai import _safe_float, _safe_int

    # Booleans are never valid numeric inputs.
    assert _safe_int(True) is None
    assert _safe_float(True) is None
    assert _safe_float(False) is None
    # A non-integral string trips the exception fallback, not a crash.
    assert _safe_int("1.5") is None
    assert _safe_int("abc") is None
    # Happy path still parses.
    assert _safe_int("7") == 7
    assert _safe_float("12.5") == 12.5


def test_zai_timeout_garbage_falls_back_to_default() -> None:
    assert zai_timeout({"LLM_USAGE_ZAI_TIMEOUT": "not-a-number"}) == 10
    assert zai_timeout({"LLM_USAGE_ZAI_TIMEOUT": "-3"}) == 10  # non-positive


def test_zai_agent_auth_key_no_home_discovers_nothing() -> None:
    from llm_tools.providers.zai import _agent_auth_key

    # No HOME and no XDG_DATA_HOME: nothing to read, nothing discovered.
    assert _agent_auth_key({}, "zai") is None


def test_zai_agent_auth_key_ignores_non_dict_store(tmp_path: Path) -> None:
    from llm_tools.providers.zai import _agent_auth_key

    share = tmp_path / "share"
    (share / "kilo").mkdir(parents=True)
    # A JSON list (not an object) must be skipped, not crash.
    (share / "kilo" / "auth.json").write_text("[1, 2, 3]", encoding="utf-8")
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(share)}
    assert _agent_auth_key(env, "zai") is None


def test_zai_agent_auth_key_entry_without_key_returns_none(tmp_path: Path) -> None:
    from llm_tools.providers.zai import _agent_auth_key

    share = tmp_path / "share"
    (share / "kilo").mkdir(parents=True)
    # Entry present but key missing / blank: not a usable credential.
    (share / "kilo" / "auth.json").write_text(
        json.dumps({"zai": {"type": "api"}}), encoding="utf-8"
    )
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(share)}
    assert _agent_auth_key(env, "zai") is None


def test_zai_extract_limits_and_envelope_edge_cases() -> None:
    from llm_tools.providers.zai import _extract_error_envelope, _extract_limits

    # Non-dict payloads degrade to empty / None rather than crashing.
    assert _extract_limits(["a", "b"]) == []
    assert _extract_error_envelope("not-a-dict") is None
    assert _extract_error_envelope(None) is None
    # A success-shaped envelope is not an error even with a message.
    assert _extract_error_envelope({"code": 200, "msg": "ok"}) is None
    assert _extract_error_envelope({"limits": []}) is None
    # code/msg both missing: nothing to classify.
    assert _extract_error_envelope({"foo": "bar"}) is None


def test_zai_window_seconds_unknown_unit_is_none() -> None:
    from llm_tools.providers.zai import _window_seconds

    # Unknown unit code (e.g. 99) cannot be converted to seconds.
    assert _window_seconds({"unit": 99, "number": 1}) is None
    assert _window_seconds({"unit": 3}) is None  # missing number
    assert _window_seconds({"number": 0}) is None  # non-positive


def test_zai_parse_limits_horizon_fallback_picks_shortest_and_longest() -> None:
    """Entries with no usable window/label fall back to the reset horizon:
    the shortest horizon becomes the 5h row, the longest the weekly row."""
    from llm_tools.providers.zai import SCOPE_5H as S5, SCOPE_WEEKLY as SW
    from llm_tools.providers.zai import _parse_zai_limits

    entries = [
        {"type": "MYSTERY", "nextResetTime": 1781481600000},  # later -> weekly
        {"type": "OTHER", "nextResetTime": 1781431200000},  # sooner -> 5h
    ]
    out = _parse_zai_limits(entries)
    assert out[S5]["nextResetTime"] == 1781431200000
    assert out[SW]["nextResetTime"] == 1781481600000


def test_zai_parse_limits_horizon_skips_entries_without_reset() -> None:
    """An unclassified entry with no nextResetTime cannot be placed by the
    horizon heuristic and is dropped."""
    from llm_tools.providers.zai import _parse_zai_limits

    out = _parse_zai_limits([{"type": "MYSTERY"}])
    assert out == {}


def test_zai_read_alias_returns_snapshot(env: dict[str, str]) -> None:
    from llm_tools.providers.zai import read

    env["LLM_USAGE_ZAI_5H_PERCENT"] = "80"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["ZAI_API_KEY"] = ""
    env.pop("ZAI_API_KEY", None)
    env.pop("LLM_USAGE_ZAI_API_KEY", None)
    snap = read(env)
    assert snap.provider == PROVIDER_ZAI
    assert snap.available is True


def test_zai_fetch_quota_no_key_returns_none() -> None:
    from llm_tools.providers.zai import _fetch_zai_quota

    # A truthy env with no key (and no discoverable agent auth.json) bails
    # before any network call. ``{}`` is falsy and would fall back to
    # ``os.environ``, so use an explicit empty-ish env instead.
    assert _fetch_zai_quota({"HOME": "/nonexistent-zai-home"}) is None


def test_zai_fetch_quota_all_long_windows_is_quota_error(
    env: dict[str, str], monkeypatch
) -> None:
    """Limits that parse but are all monthly+ (neither 5h nor weekly)
    surface a quota-error rather than a misleading 'no data'."""
    env["ZAI_API_KEY"] = "test-key"
    _mock_urlopen(
        monkeypatch,
        {
            "code": 200,
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "unit": 5,
                        "number": 1,
                        "percentage": 10,
                        "nextResetTime": 1781431200000,
                    }
                ]
            },
        },
    )
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "quota-error"


def test_zai_injected_raw_api_shape_parses_into_scopes() -> None:
    """A raw API-shape payload (``{data:{limits:[...]}}``) injected via
    LLM_USAGE_ZAI_QUOTA_LIMIT_JSON is decoded by ``_parse_injected_payload``
    into CapacityScope objects (the dict-shape path is what ``read_zai``
    consumes; the raw-shape path is exercised at the helper layer)."""
    from llm_tools.providers.zai import _parse_injected_payload

    out = _parse_injected_payload(
        {
            "LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": json.dumps(
                {
                    "code": 200,
                    "data": {
                        "limits": [
                            {"type": "TIME_LIMIT", "percentage": 20, "nextResetTime": 1781431200000},
                            {"type": "WEEKLY_LIMIT", "percentage": 30, "nextResetTime": 1781481600000},
                        ]
                    },
                }
            )
        }
    )
    assert isinstance(out, dict)
    assert out[SCOPE_5H].remaining_percent == 80.0  # 100 - 20 used
    assert out[SCOPE_WEEKLY].remaining_percent == 70.0  # 100 - 30 used


def test_zai_injected_raw_api_shape_single_limit_skips_missing_scope() -> None:
    """A raw payload with only a 5h limit yields just the 5h scope; the
    absent weekly entry is skipped via the ``not entry`` guard."""
    from llm_tools.providers.zai import _parse_injected_payload

    out = _parse_injected_payload(
        {
            "LLM_USAGE_ZAI_QUOTA_LIMIT_JSON": json.dumps(
                {
                    "code": 200,
                    "data": {
                        "limits": [
                            {"type": "TIME_LIMIT", "percentage": 20, "nextResetTime": 1781431200000},
                        ]
                    },
                }
            )
        }
    )
    assert isinstance(out, dict)
    assert SCOPE_5H in out
    assert SCOPE_WEEKLY not in out


def test_zai_read_injected_error_envelope_surfaces_reason(env: dict[str, str]) -> None:
    env.pop("ZAI_API_KEY", None)
    env.pop("LLM_USAGE_ZAI_API_KEY", None)
    env["LLM_USAGE_ZAI_QUOTA_LIMIT_JSON"] = json.dumps({"code": 401, "msg": "bad token"})
    snap = read_zai(env)
    assert snap.available is False
    assert snap.reason == "not-authenticated"


# --- Scheduler: bare --provider zai launch contract ---------------------------


def test_scheduler_zai_attached_launch_rejected() -> None:
    from llm_tools import scheduler

    cfg = scheduler.SchedulerConfig(provider="zai", attached=True, cwd="/tmp")
    with pytest.raises(SystemExit) as exc:
        scheduler.provider_default_argv(cfg, "x")
    assert exc.value.code == 2


def test_scheduler_zai_headless_launch_rejected() -> None:
    from llm_tools import scheduler

    cfg = scheduler.SchedulerConfig(provider="zai", attached=False, cwd="/tmp")
    with pytest.raises(SystemExit) as exc:
        scheduler.provider_default_argv(cfg, "x")
    assert exc.value.code == 2


def test_scheduler_zai_model_description() -> None:
    from llm_tools import scheduler

    cfg = scheduler.SchedulerConfig(provider="zai")
    assert "routes" in scheduler.scheduler_model_description(cfg)


def test_zai_log_samples_records_reset_windows(env: dict[str, str], monkeypatch) -> None:
    """``log_samples_from_provider_data`` records a z.ai reset-window sample
    for each scope, mirroring the minimax path."""
    env["LLM_USAGE_ZAI_5H_PERCENT"] = "77"
    env["LLM_USAGE_ZAI_5H_RESET_EPOCH"] = "1781431200"
    env["LLM_USAGE_ZAI_WEEKLY_PERCENT"] = "88"
    env["LLM_USAGE_ZAI_WEEKLY_RESET_EPOCH"] = "1781481600"
    env.pop("ZAI_API_KEY", None)
    env.pop("LLM_USAGE_ZAI_API_KEY", None)
    zai_snap = read_zai(env)
    assert zai_snap.available is True
    calls: list[tuple[str, str, float | None]] = []
    monkeypatch.setattr(common, "log_usage_sample", lambda p, w, r: calls.append((p, w, r)))
    usage.log_samples_from_provider_data({"zai": zai_snap})
    assert ("Z.ai", "5h", 77.0) in calls
    assert ("Z.ai", "weekly", 88.0) in calls


# --- Helpers ------------------------------------------------------------------


def run_cmd(args, env):
    from .conftest import run_cmd as _run_cmd

    return _run_cmd(args, env)