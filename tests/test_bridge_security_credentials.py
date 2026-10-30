"""A provider process sees its own credentials and none that belong to another provider."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge_runtime, common, scheduler
from llm_tools.terminal import TmuxTerminalBackend

SECRETS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "OPENAI_API_KEY": "openai-key",
    "CODEX_API_KEY": "codex-key",
    "GH_TOKEN": "gh-token",
    "GITHUB_TOKEN": "github-token",
    "COPILOT_GITHUB_TOKEN": "copilot-token",
    "ZAI_API_KEY": "zai-key",
    "MINIMAX_API_KEY": "minimax-key",
    "KILO_API_KEY": "kilo-key",
    "OPENCODE_API_KEY": "opencode-key",
}
# What each provider's process is allowed to see: exactly these of SECRETS.
EXPECTED = {
    "claude": {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"},
    "codex": {"OPENAI_API_KEY", "CODEX_API_KEY"},
    "copilot": {"GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"},
    "kilo": {"KILO_API_KEY", "ZAI_API_KEY", "MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    "opencode": {"OPENCODE_API_KEY", "ZAI_API_KEY", "MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    "minimax": {"MINIMAX_API_KEY"},
}


@pytest.fixture()
def secret_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name in common.credential_variables_to_remove(os.environ, None):
        monkeypatch.delenv(name, raising=False)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    return dict(SECRETS)


@pytest.mark.parametrize("provider", sorted(EXPECTED))
def test_provider_environment_keeps_own_credentials_and_drops_the_rest(provider: str, secret_env: dict[str, str]) -> None:
    env = scheduler.provider_env(scheduler.SchedulerConfig(provider=provider))
    assert env is not None
    present = {name for name in SECRETS if name in env}
    assert present == EXPECTED[provider]
    assert env["HTTPS_PROXY"] == "http://proxy.invalid:3128" and "PATH" in env


def test_managed_overrides_do_not_bring_credentials_back(secret_env: dict[str, str]) -> None:
    cfg = scheduler.SchedulerConfig(provider="codex", env_overrides={"LLM_BRIDGE_MANAGED": "1"}, ralph_robin_active=True)
    env = scheduler.provider_env(cfg)
    assert env is not None and env["LLM_BRIDGE_MANAGED"] == "1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "GH_TOKEN" not in env and "ZAI_API_KEY" not in env


def test_unknown_provider_is_left_alone(secret_env: dict[str, str]) -> None:
    assert common.credential_variables_to_remove(os.environ, "custom-cli") == []


def test_verifier_receives_no_provider_credentials(secret_env: dict[str, str], tmp_path: Path) -> None:
    probe = "import os,json; print(json.dumps(sorted(k for k in os.environ if k in %r)))" % (sorted(SECRETS),)
    result = bridge_runtime._run_bounded_verifier([sys.executable, "-c", probe], str(tmp_path), 30, tmp_path / "v.log")
    assert result["exit_code"] == 0 and result["output"].decode().strip() == "[]"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_native_tmux_provider_pane_does_not_inherit_other_providers_credentials(secret_env: dict[str, str], tmp_path: Path) -> None:
    # The tmux server starts with the full environment, as it does under a real controller.
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exit 0"], bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path, socket_path=tmp_path / "s.sock", session_name="creds", size=(100, 30),
    )
    try:
        logs = common.setup_run_logs(tmp_path / "logs", "codex")
        output, status = logs.run_dir / "attempt-1.out", logs.run_dir / "attempt-1.status"
        cfg = scheduler.SchedulerConfig(
            provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target=f"{handle.session_name}:station",
            tmux_socket=str(handle.socket_path), tmux_pane=handle.provider_pane, native_tui=True,
            env_overrides={"LLM_BRIDGE_MANAGED": "1"},
        )
        probe = "import os; print('seen=' + ','.join(sorted(k for k in os.environ if k in %r)))" % (sorted(SECRETS),)
        assert scheduler.run_tmux(cfg, logs, [sys.executable, "-c", probe], output, status) == 0
        time.sleep(0.2)
        seen = {line for line in output.read_text(encoding="utf-8").splitlines() if line.startswith("seen=")}
        assert seen == {"seen=" + ",".join(sorted(EXPECTED["codex"]))}
    finally:
        backend.stop(handle)
