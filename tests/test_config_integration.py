"""Tests for the config-file integration in ``llm-scheduler`` and ``ralph-robin``.

The CLI surface is largely tested through subprocess in other files. These
tests focus on the per-tool :func:`apply_config` helpers and the
``resolve_policies`` helper in ralph-robin, which are the smallest unit
that exercises the per-provider routing policy.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from llm_tools import ralph_robin, scheduler
from llm_tools.config import _cache


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    _cache.clear()


def _write_config(xdg: Path, body: str) -> None:
    (xdg / "llm-tools" / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (xdg / "llm-tools" / "config.toml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_ralph_apply_config_returns_empty_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    cfg = ralph_robin.RalphConfig()
    assert ralph_robin.apply_config(cfg) == {}


def test_ralph_apply_config_fills_defaults_and_ralph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [defaults]
        scope = "weekly"
        min_remaining = 5
        providers = ["kilo", "codex"]

        [ralph]
        even_burn = false
        max_iterations = 7
        max_duration = "2h"
        prefix = "time"
        prefix_usage_interval = 5
        """,
    )
    cfg = ralph_robin.RalphConfig()
    ralph_robin.apply_config(cfg)
    assert cfg.scope == "weekly"
    assert cfg.min_remaining == "5"
    assert cfg.providers_spec == "kilo,codex"
    assert cfg.even_burn is False
    assert cfg.max_iterations == "7"
    assert cfg.max_duration == "2h"
    assert cfg.prefix_spec == "time"
    assert cfg.prefix_usage_interval == "5"


def test_ralph_apply_config_respects_explicit_cli_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [defaults]
        scope = "weekly"

        [ralph]
        even_burn = false
        max_iterations = 7
        """,
    )
    cfg = ralph_robin.RalphConfig()
    cfg.explicit.add("scope")
    cfg.explicit.add("max_iterations")
    cfg.scope = "5h"
    cfg.max_iterations = "42"
    ralph_robin.apply_config(cfg)
    # explicit CLI values win over config file
    assert cfg.scope == "5h"
    assert cfg.max_iterations == "42"
    # but unset flags still come from the config
    assert cfg.even_burn is False


def test_ralph_resolve_policies_loads_per_provider_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [providers.claude]
        model = "sonnet"
        allow_fallback = true
        scope = "weekly"

        [providers.kilo]
        allow_fallback = false

        [providers.codex]
        model = "spark"
        """,
    )
    conf = ralph_robin.apply_config(ralph_robin.RalphConfig())
    cfg = ralph_robin.RalphConfig(providers=["claude", "kilo", "codex"])
    ralph_robin.resolve_policies(cfg, conf)
    assert cfg.policies["claude"].model == "sonnet"
    assert cfg.policies["claude"].allow_fallback is True
    assert cfg.policies["claude"].scope == "weekly"
    assert cfg.policies["kilo"].allow_fallback is False
    assert cfg.policies["kilo"].model is None
    assert cfg.policies["codex"].model == "spark"


def test_ralph_resolve_policies_warns_for_unsupported_model_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [providers.minimax]
        model = "ignored-minimax-model"
        """,
    )
    conf = ralph_robin.apply_config(ralph_robin.RalphConfig())
    cfg = ralph_robin.RalphConfig(providers=["minimax"])
    ralph_robin.resolve_policies(cfg, conf)
    captured = capsys.readouterr()
    assert "model pinning is not supported" in captured.err
    # The model is reset to None when the provider cannot take one.
    assert cfg.policies["minimax"].model is None
    # The rest of the policy is preserved.
    assert cfg.policies["minimax"].allow_fallback is False


def test_scheduler_apply_config_returns_early_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    cfg = scheduler.SchedulerConfig()
    scheduler.apply_config(cfg)  # no file => early return
    assert cfg.provider == ""
    assert cfg.scope == "auto"


def test_scheduler_apply_config_fills_tool_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [defaults]
        scope = "weekly"
        min_remaining = 5

        [scheduler]
        provider = "claude"
        scope = "5h"
        poll_interval = 30
        max_unavailable_wait = 120
        retry_delays = "5,10"
        """,
    )
    cfg = scheduler.SchedulerConfig()
    scheduler.apply_config(cfg)
    assert cfg.provider == "claude"
    assert cfg.scope == "5h"
    assert cfg.min_remaining == "5"
    assert cfg.poll_interval == "30"
    assert cfg.max_unavailable_wait == "120"
    assert cfg.retry_delays == "5,10"


def test_scheduler_apply_config_per_provider_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [providers.claude]
        model = "sonnet"
        allow_fallback = true
        scope = "weekly"
        min_remaining = 25
        """,
    )
    cfg = scheduler.SchedulerConfig()
    # Re-run apply_config with provider set so provider_policy is applied.
    cfg.provider = "claude"
    scheduler.apply_config(cfg)
    assert cfg.allow_fallback is True
    assert cfg.model == "sonnet"
    assert cfg.scope == "weekly"
    assert cfg.min_remaining == "25"


def test_scheduler_apply_config_respects_explicit_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [defaults]
        scope = "weekly"

        [scheduler]
        provider = "claude"
        scope = "5h"
        """,
    )
    cfg = scheduler.SchedulerConfig()
    cfg.explicit.add("scope")
    cfg.scope = "monthly"
    scheduler.apply_config(cfg)
    # explicit CLI scope wins
    assert cfg.scope == "monthly"


def test_scheduler_apply_config_warns_for_unsupported_model_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_config(
        tmp_path / "xdg",
        """
        [scheduler]
        provider = "minimax"

        [providers.minimax]
        model = "ignored-minimax-model"
        """,
    )
    cfg = scheduler.SchedulerConfig()
    scheduler.apply_config(cfg)
    captured = capsys.readouterr()
    assert "model pinning is not supported" in captured.err
    assert cfg.model == ""
