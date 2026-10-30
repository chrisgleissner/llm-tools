"""Credential hygiene, PYTHONPATH and PATH handling for providers, verifiers and the station's tmux server.

This is hygiene, not a boundary: processes of the same user can read each other's environment and the
credential files in the home directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from llm_tools import bridge_runtime, common, scheduler
from llm_tools.terminal import TmuxTerminalBackend

SECRETS = {
    "AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "AWS_SESSION_TOKEN": "c",
    "GOOGLE_APPLICATION_CREDENTIALS": "/x.json", "GEMINI_API_KEY": "d",
    "AZURE_CLIENT_SECRET": "e", "HF_TOKEN": "f", "NPM_TOKEN": "g", "NODE_AUTH_TOKEN": "g2",
    "OPENROUTER_API_KEY": "h",
}
EXPECTED_KEPT = {
    "claude": {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_API_KEY"},
    "codex": {"AZURE_CLIENT_SECRET"},
    "copilot": set(),
    "kilo": set(SECRETS) - {"NPM_TOKEN", "NODE_AUTH_TOKEN"},
    "opencode": set(SECRETS) - {"NPM_TOKEN", "NODE_AUTH_TOKEN"},
    "minimax": set(),
}


@pytest.fixture()
def cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in common.credential_variables_to_remove(os.environ, None):
        monkeypatch.delenv(name, raising=False)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")


@pytest.mark.parametrize("provider", sorted(EXPECTED_KEPT))
def test_cloud_and_registry_credentials_follow_the_provider_table(cloud_env: None, provider: str) -> None:
    env = scheduler.provider_env(scheduler.SchedulerConfig(provider=provider))
    assert env is not None
    assert {name for name in SECRETS if name in env} == EXPECTED_KEPT[provider]
    # Providers run git; the agent socket stays.
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"


def test_verifiers_receive_no_provider_or_cloud_credentials(cloud_env: None) -> None:
    env = common.scrubbed_environment(os.environ, None)
    assert not [name for name in SECRETS if name in env]
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"


def test_package_root_is_not_exported_to_providers_or_verifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    root = common.package_root()
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, "/opt/own"]))
    assert scheduler.provider_env(scheduler.SchedulerConfig(provider="codex"))["PYTHONPATH"] == "/opt/own"  # type: ignore[index]
    assert common.scrubbed_environment(os.environ, None)["PYTHONPATH"] == "/opt/own"
    monkeypatch.setenv("PYTHONPATH", root)
    assert "PYTHONPATH" not in scheduler.provider_env(scheduler.SchedulerConfig(provider="codex"))  # type: ignore[operator]
    assert "PYTHONPATH" not in common.scrubbed_environment(os.environ, None)


def test_station_server_environment_drops_unusable_credentials_and_the_package_root(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {**SECRETS, "GH_TOKEN": "g", "ZAI_API_KEY": "z", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "o"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PYTHONPATH", common.package_root())
    monkeypatch.delenv("LLM_USAGE_PROVIDERS", raising=False)
    only_claude_codex = {"frozen_tool_config": '[usage]\nproviders = ["claude", "codex"]\n'}
    env = bridge_runtime.station_server_environment(only_claude_codex)
    assert "PYTHONPATH" not in env and "PATH" in env
    for kept in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AZURE_CLIENT_SECRET"):
        assert kept in env, kept
    for gone in ("GH_TOKEN", "ZAI_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN", "NPM_TOKEN"):
        assert gone not in env, gone
    # No restriction configured: only credentials that no provider uses are removed.
    unrestricted = bridge_runtime.station_server_environment({})
    assert "GH_TOKEN" in unrestricted and "NPM_TOKEN" not in unrestricted


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_panes_do_not_inherit_what_the_server_environment_lacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("LLM_USAGE_PROVIDERS", raising=False)  # the environment would override the saved provider list
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-the-pane")
    monkeypatch.setenv("PYTHONPATH", common.package_root())
    dump = tmp_path / "pane-env.txt"
    server_env = bridge_runtime.station_server_environment({"frozen_tool_config": '[usage]\nproviders = ["claude"]\n'})
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", f"env > {dump}; exec sleep 30"],
        bridge_argv=["/bin/sh", "-c", "exec sleep 30"],
        cwd=tmp_path, socket_path=tmp_path / "s.sock", session_name="envcheck", size=(100, 30),
        server_env=server_env,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (dump.exists() and dump.stat().st_size):
            time.sleep(0.05)
        text = dump.read_text(encoding="utf-8")
        names = {line.partition("=")[0] for line in text.splitlines()}
        assert "GH_TOKEN" not in names and "must-not-reach-the-pane" not in text
        assert not any(line.startswith("PYTHONPATH=") and common.package_root() in line for line in text.splitlines())
        assert "\nPATH=" in "\n" + text
        global_env = subprocess.run(["tmux", "-S", str(handle.socket_path), "show-environment", "-g"], capture_output=True, text=True).stdout
        assert "GH_TOKEN" not in global_env
    finally:
        backend.stop(handle)


def test_tmux_commands_set_the_package_root_themselves() -> None:
    argv = common.rooted_module_argv("llm_tools.bridge", "console")
    assert argv[:2] == ["env", f"PYTHONPATH={common.package_root()}"]
    assert argv[2:5] == [os.sys.executable, "-P", "-m"]
