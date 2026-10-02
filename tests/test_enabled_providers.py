"""Tests for ``[usage].providers`` / ``LLM_USAGE_PROVIDERS``.

The setting limits which providers every tool uses: ``llm-usage`` reads and
shows only the enabled providers, ``ralph-robin`` drops rotation entries whose
launch CLI is disabled, and ``llm-scheduler`` refuses a disabled provider.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from llm_tools import config as toolconfig
from llm_tools import ralph_robin, scheduler, usage
from llm_tools.capacity import ProviderSnapshot

ROUTES_CONFIG = """
[routes.kilo-minimax-m3]
provider = "kilo"
model    = "kilo/minimax/minimax-m3"
[routes.kilo-minimax-m3.capacity]
policy = "opaque"
scope  = "subscription"

[routes.kilo-zai-glm-52]
provider = "kilo"
model    = "zai/glm-5.2"
[routes.kilo-zai-glm-52.capacity]
policy   = "delegate"
provider = "zai"
"""


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    toolconfig._cache.clear()


def _write_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))
    return path


# --- config resolution ---------------------------------------------------------


def test_enabled_providers_is_none_without_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, "[budget]\nmonthly = 10\n")
    assert toolconfig.enabled_providers() is None
    assert toolconfig.usage_providers("not a dict") is None  # type: ignore[arg-type]


def test_enabled_providers_reads_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, '[usage]\nproviders = ["claude", "codex"]\n')
    assert toolconfig.enabled_providers() == frozenset({"claude", "codex"})
    # A blank env value does not override the file.
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "  ")
    assert toolconfig.enabled_providers() == frozenset({"claude", "codex"})


def test_enabled_providers_env_overrides_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, '[usage]\nproviders = ["claude"]\n')
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", " Codex, kilo ,")
    assert toolconfig.enabled_providers() == frozenset({"codex", "kilo"})


@pytest.mark.parametrize(
    ("value", "message"),
    [("claud", "unknown provider(s) claud"), (",", "no provider names")],
)
def test_enabled_providers_env_rejects_bad_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str, message: str
) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", value)
    with pytest.raises(SystemExit) as exc:
        toolconfig.enabled_providers()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[usage]\nproviders = "claude"\n', "must be a list of provider names"),
        ("[usage]\nproviders = [1]\n", "must be a list of provider names"),
        ("[usage]\nproviders = []\n", "must name at least one provider"),
        ('[usage]\nproviders = ["acme"]\n', "unknown provider 'acme'"),
        ('[usage]\nshow = ["claude"]\n', "usage: unknown key(s): show"),
    ],
)
def test_usage_config_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, message: str
) -> None:
    _write_config(monkeypatch, tmp_path, body)
    with pytest.raises(SystemExit):
        toolconfig.load_config()
    assert message in capsys.readouterr().err


def test_usage_table_without_providers_key_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, "[usage]\n")
    assert toolconfig.enabled_providers() is None


# --- llm-usage -----------------------------------------------------------------


def _stub_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_tools.providers as providers

    def must_not_read() -> object:
        raise AssertionError("hidden provider was read")

    for name in ("read_copilot_snapshot", "read_kilo", "read_opencode", "read_minimax", "read_zai"):
        monkeypatch.setattr(providers, name, must_not_read)
    monkeypatch.setattr(usage.common, "read_codex", lambda: {"provider": "codex", "available": False, "reason": "fixture"})
    monkeypatch.setattr(
        providers, "read_claude_snapshot", lambda: ProviderSnapshot(provider="claude", available=False, reason="fixture")
    )


@pytest.mark.parametrize("parallelism", [1, 4])
def test_hidden_providers_are_not_read_or_shown(monkeypatch: pytest.MonkeyPatch, parallelism: int) -> None:
    _stub_readers(monkeypatch)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,codex")
    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.provider_parallelism = parallelism
    assert cfg.visible_providers == frozenset({"claude", "codex"})
    data = usage.read_all_provider_data(cfg)
    assert set(data) == {"codex", "claude", "copilot", "kilo", "opencode", "minimax", "zai"}
    assert data["kilo"].reason == "hidden"
    assert data["codex"]["reason"] == "fixture"
    rows, _show_model = usage._build_usage_rows(cfg, data)
    assert {row.provider_label or row.provider for row in rows} == {"Claude", "Codex"}
    obj = usage.json_object_from_provider_data(cfg, data)
    for name in ("copilot", "kilo", "opencode", "minimax", "zai"):
        assert obj[name] == {"provider": name, "available": False, "reason": "hidden"}
    assert obj["codex"]["reason"] == "fixture"


def test_hidden_codex_keeps_dict_shape() -> None:
    data = usage._hidden_provider_data("codex", lambda: {"provider": "codex", "available": False, "reason": "reader-error"})
    assert data == {"provider": "codex", "available": False, "reason": "hidden"}
    snap = usage._hidden_provider_data("kilo", lambda: usage.unavailable_snapshot("kilo", "kilo cli"))
    assert (snap.reason, snap.source) == ("hidden", "kilo cli")


def _record_route_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import llm_tools.routes as routes

    seen: list[str] = []

    def fake(route: Any, *_args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        seen.append(route.route_id)
        snapshot = {"provider": route.provider, "available": True, "scopes": [{"name": "subscription", "kind": "opaque"}]}
        return snapshot, {"usable": True}

    monkeypatch.setattr(routes, "usage_snapshot_and_decision_for_route", fake)
    return seen


def test_routes_on_hidden_launch_cli_are_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, ROUTES_CONFIG)
    seen = _record_route_reads(monkeypatch)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,zai")
    cfg = usage.Config()
    # Delegated capacity (zai) does not keep a kilo route: routes match on the launch CLI.
    assert usage.route_rows(cfg) == []
    assert usage.route_decision_summary(cfg) == []
    assert seen == []
    # Without a cfg the summary keeps its previous behaviour and lists every route.
    assert len(usage.route_decision_summary()) == 2


def test_routes_on_visible_launch_cli_are_shown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, ROUTES_CONFIG)
    seen = _record_route_reads(monkeypatch)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "kilo")
    cfg = usage.Config()
    assert len(usage.route_rows(cfg)) == 2
    assert len(usage.route_decision_summary(cfg)) == 2
    assert seen == ["kilo-minimax-m3", "kilo-zai-glm-52"] * 2


def test_service_snapshot_with_hidden_visible_provider_is_bypassed(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import usage_service

    hidden = {"provider": "kilo", "available": False, "reason": "hidden"}
    payload = {"providers": {"codex": {"provider": "codex", "available": False, "reason": "hidden"}, "kilo": hidden}}
    monkeypatch.setattr(usage_service, "request_snapshot", lambda **_kwargs: payload)
    monkeypatch.setattr(usage, "service_payload_matches_environment", lambda *_args: True)
    cfg = usage.Config()
    cfg.use_service = True
    # All providers visible: the service skipped kilo and codex, so read directly.
    assert usage._provider_data_via_service(cfg) is None
    # Only claude visible: the hidden entries are not needed, so the snapshot is used.
    cfg.visible_providers = frozenset({"claude"})
    assert usage._provider_data_via_service(cfg) is not None


# --- ralph-robin ---------------------------------------------------------------


def _ralph(argv: list[str]) -> ralph_robin.RalphConfig:
    cfg = ralph_robin.parse_args([*argv, "-p", "x"])
    ralph_robin.apply_config(cfg)
    ralph_robin.validate_args(cfg)
    return cfg


def test_ralph_keeps_rotation_without_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, "[budget]\nmonthly = 10\n")
    cfg = _ralph(["-P", "claude,codex,copilot"])
    assert cfg.providers == ["claude", "codex", "copilot"]


def test_ralph_keeps_rotation_when_every_provider_is_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,codex,copilot")
    cfg = _ralph(["-P", "claude,codex"])
    assert (cfg.providers, cfg.providers_spec) == (["claude", "codex"], "claude,codex")
    assert "skipping" not in capsys.readouterr().err


def test_ralph_drops_disabled_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(monkeypatch, tmp_path, '[usage]\nproviders = ["claude", "codex"]\n')
    cfg = _ralph(["-P", "claude,copilot,codex"])
    assert cfg.providers == ["claude", "codex"]
    assert cfg.providers_spec == "claude,codex"
    assert "warning: skipping copilot" in capsys.readouterr().err


def test_ralph_drops_routes_by_launch_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(monkeypatch, tmp_path, ROUTES_CONFIG)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,codex,zai")
    cfg = _ralph(["--routes", "claude,kilo-minimax-m3,codex,kilo-zai-glm-52"])
    assert cfg.routes == ["claude", "codex"]
    assert cfg.routes_spec == "claude,codex"
    assert "warning: skipping kilo-minimax-m3, kilo-zai-glm-52" in capsys.readouterr().err


def test_ralph_keeps_routes_on_enabled_launch_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_config(monkeypatch, tmp_path, ROUTES_CONFIG)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "kilo")
    cfg = _ralph(["--routes", "kilo-minimax-m3,kilo-zai-glm-52"])
    assert cfg.routes == ["kilo-minimax-m3", "kilo-zai-glm-52"]


def test_ralph_fails_when_every_entry_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude")
    with pytest.raises(SystemExit) as exc:
        _ralph(["-P", "codex,copilot"])
    assert exc.value.code == 2
    assert "no rotation entries left" in capsys.readouterr().err


# --- llm-scheduler -------------------------------------------------------------


def test_scheduler_refuses_disabled_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,codex")
    cfg = scheduler.SchedulerConfig(provider="copilot", prompt_text="hi", cwd=str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        scheduler.validate_args(cfg)
    assert exc.value.code == 2
    assert "provider copilot is disabled" in capsys.readouterr().err


def test_scheduler_accepts_enabled_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "claude,codex")
    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="hi", cwd=str(tmp_path))
    scheduler.validate_args(cfg)
