"""Helpers behind the first-run path: Claude folder trust, the signed-in default provider, the Start hint, --version."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_claude_folder_trust_lookup_reads_only_the_state_file(tmp_path: Path) -> None:
    from llm_tools import bridge_doctor

    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "work" / "repo"
    folder.mkdir(parents=True)
    env = {"HOME": str(home)}
    assert bridge_doctor.claude_folder_trusted(folder, env) is False  # a new install has no state file

    state = home / ".claude.json"
    state.write_text("{not json")
    assert bridge_doctor.claude_folder_trusted(folder, env) is None  # unreadable: unknown, not "untrusted"

    state.write_text(json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": False}}}))
    assert bridge_doctor.claude_folder_trusted(folder, env) is False

    state.write_text(json.dumps({"projects": {str(tmp_path / "work"): {"hasTrustDialogAccepted": True}}}))
    assert bridge_doctor.claude_folder_trusted(folder, env) is True  # trust given to a parent folder applies

    state.write_text(json.dumps({"projects": []}))
    assert bridge_doctor.claude_folder_trusted(folder, env) is False

    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": True}}}))
    assert bridge_doctor.claude_folder_trusted(folder, {**env, "CLAUDE_CONFIG_DIR": str(config_dir)}) is True


def test_default_provider_prefers_a_signed_in_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge_launcher

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # Nothing is signed in: the previous default, codex, is kept so validation can explain what is missing.
    assert bridge_launcher.default_providers(("codex", "claude")) == "codex"
    assert bridge_launcher.default_providers(("claude",)) == "claude"
    assert bridge_launcher.default_providers(()) == "codex"
    (home / ".codex" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "key"}))
    assert bridge_launcher.default_providers(("codex", "claude")) == "codex"  # only codex is signed in
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    assert bridge_launcher.default_providers(("codex", "claude")) == "claude"  # both ready: the built-in order wins
    configured = {"ralph": {"providers": ["codex"]}}
    assert bridge_launcher.default_providers(("codex", "claude"), configured) == "codex"  # a configured rotation still wins


def test_start_hint_mentions_the_trust_prompt_only_when_it_will_appear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge_doctor
    from llm_tools.bridge_launcher import Launcher, _field_hint

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    folder = tmp_path / "repo"
    folder.mkdir()
    state = Launcher(cwd=str(folder), directory=str(folder), providers="claude", focus=5)
    assert _field_hint(state, 200).endswith("Claude asks once to trust this folder: choose Yes")
    state.native = False
    assert "trust" not in _field_hint(state, 200)  # a headless run never shows the dialog
    state.native, state.providers = True, "codex"
    assert "trust" not in _field_hint(state, 200)
    state.providers = "claude"
    (home / ".claude.json").write_text(json.dumps({"projects": {str(folder): {"hasTrustDialogAccepted": True}}}))
    assert _field_hint(state, 200) == "Starts claude now · uses provider capacity"
    steps = bridge_doctor.next_steps(tmux_available=True, providers=[{"name": "claude", "installed": True, "logged_in": True}],
                                     folder=tmp_path, env={"HOME": str(home)})
    assert bridge_doctor.TRUST_NOTE in steps  # the parent folder of the trusted one is not trusted


def test_version_flag_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    from llm_tools.bridge import main

    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("llm-bridge ") and out.strip() != "llm-bridge"
