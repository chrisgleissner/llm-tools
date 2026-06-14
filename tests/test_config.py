"""Tests for the shared ``llm_tools.config`` module.

These tests cover the full config-file lifecycle (load + validate), the
per-provider routing policy, and the precedence helpers used by
``llm-scheduler`` and ``ralph-robin``. They use ``monkeypatch`` to point
``XDG_CONFIG_HOME`` and ``LLM_TOOLS_CONFIG`` at temporary TOML files so
no real user config is touched.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from llm_tools import config
from llm_tools.config import (
    ProviderPolicy,
    config_path,
    load_config,
    merged_tool_config,
    provider_policy,
)


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    config._cache.clear()


def _write_toml(env: dict[str, str], path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_config_path_uses_explicit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    explicit = tmp_path / "my-config.toml"
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(explicit))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == explicit


def test_config_path_uses_xdg_when_no_explicit_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    expected = xdg / "llm-tools" / "config.toml"
    assert config_path() == expected


def test_config_path_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = config_path()
    assert path.name == "config.toml"
    assert path.parent.name == "llm-tools"


def test_load_config_returns_empty_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    assert load_config() == {}


def test_load_config_parses_full_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [defaults]
        providers = ["claude", "codex"]
        scope = "weekly"
        min_remaining = 5

        [providers.claude]
        model = "sonnet"
        allow_fallback = true
        scope = "weekly"
        min_remaining = 10

        [providers.kilo]
        allow_fallback = false

        [ralph]
        even_burn = false
        max_iterations = 3
        max_duration = "2h"

        [scheduler]
        poll_interval = 30
        """,
    )
    loaded = load_config()
    assert loaded["defaults"]["providers"] == ["claude", "codex"]
    assert loaded["defaults"]["scope"] == "weekly"
    assert loaded["providers"]["claude"]["model"] == "sonnet"
    assert loaded["providers"]["kilo"]["allow_fallback"] is False
    assert loaded["ralph"]["max_duration"] == "2h"
    assert loaded["scheduler"]["poll_interval"] == 30


def test_load_config_caches_by_mtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    cfg_path = _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[defaults]\nscope = \"auto\"\n",
    )
    first = load_config()
    second = load_config()
    assert first is second  # cached
    # Touch with a new mtime so cache is invalidated and a re-parse is needed.
    import time

    time.sleep(0.05)
    os.utime(cfg_path, None)
    third = load_config()
    assert third is not first  # cache miss
    assert third["defaults"]["scope"] == "auto"


def test_load_config_invalid_toml_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "this is = not = valid = toml = [\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_load_config_rejects_top_level_table_via_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        '"not a table"\n',
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_unknown_top_level_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[bogus]\nfoo = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_unknown_defaults_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[defaults]\nfoo = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_defaults_must_be_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "defaults = 42\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_unknown_ralph_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[ralph]\nunknown = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_ralph_must_be_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "ralph = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_unknown_scheduler_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[scheduler]\nweird = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_providers_must_be_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "providers = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_unknown_provider_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[providers.acme]\nallow_fallback = true\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_provider_policy_must_be_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    # Override the table type so ``providers.claude`` becomes a string.
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "providers.claude = \"not a table\"\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_provider_unknown_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[providers.claude]\nweird = 1\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_validate_allow_fallback_must_be_bool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        "[providers.claude]\nallow_fallback = \"yes\"\n",
    )
    with pytest.raises(SystemExit):
        load_config()


def test_provider_policy_defaults_when_block_missing() -> None:
    cfg: dict = {}
    p = provider_policy(cfg, "claude")
    assert p == ProviderPolicy(model=None, allow_fallback=False, scope=None, min_remaining=None)


def test_provider_policy_returns_set_values() -> None:
    cfg = {
        "providers": {
            "claude": {
                "model": "sonnet",
                "allow_fallback": True,
                "scope": "weekly",
                "min_remaining": 5,
            }
        }
    }
    p = provider_policy(cfg, "claude")
    assert p.model == "sonnet"
    assert p.allow_fallback is True
    assert p.scope == "weekly"
    assert p.min_remaining == "5"


def test_provider_policy_uses_false_default_for_allow_fallback() -> None:
    cfg = {"providers": {"claude": {"model": "sonnet"}}}
    p = provider_policy(cfg, "claude")
    assert p.allow_fallback is False
    assert p.model == "sonnet"


def test_provider_policy_missing_block_returns_defaults() -> None:
    cfg = {"providers": {"codex": {"model": "spark"}}}
    p = provider_policy(cfg, "kilo")
    assert p == ProviderPolicy()


def test_merged_tool_config_defaults_only() -> None:
    cfg = {"defaults": {"scope": "weekly", "min_remaining": 5}}
    assert merged_tool_config(cfg, "ralph") == {"scope": "weekly", "min_remaining": 5}


def test_merged_tool_config_tool_overrides_defaults() -> None:
    cfg = {
        "defaults": {"scope": "weekly", "min_remaining": 5},
        "ralph": {"scope": "5h"},
    }
    assert merged_tool_config(cfg, "ralph") == {"scope": "5h", "min_remaining": 5}


def test_merged_tool_config_empty() -> None:
    assert merged_tool_config({}, "ralph") == {}


def test_merged_tool_config_missing_tool_block() -> None:
    cfg = {"defaults": {"scope": "weekly"}}
    assert merged_tool_config(cfg, "scheduler") == {"scope": "weekly"}


def test_as_str_helper() -> None:
    assert config._as_str(None) is None
    assert config._as_str(True) == "true"
    assert config._as_str(False) == "false"
    assert config._as_str(7) == "7"
    assert config._as_str(2.5) == "2.5"
