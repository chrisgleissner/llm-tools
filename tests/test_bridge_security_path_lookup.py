"""The provider command is looked up through absolute PATH entries only, never through the mission folder."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from llm_tools import common, scheduler


def hostile_path_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    folder = tmp_path / "repo"
    real_bin = tmp_path / "bin"
    folder.mkdir()
    real_bin.mkdir()
    for directory in (folder, real_bin):
        tool = directory / "codex"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(folder)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative/bin", str(real_bin)]))
    return folder, real_bin, real_bin / "codex"


def test_provider_command_is_resolved_through_absolute_path_entries_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, real_bin, real_codex = hostile_path_setup(tmp_path, monkeypatch)
    assert common.trusted_path() == str(real_bin)
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(folder))
    logs = common.setup_run_logs(tmp_path / "logs", "codex", "codex", None)
    common.load_prompt("do it", "", logs)
    argv = scheduler.command_argv(cfg, logs, "do it")
    assert argv[0] == str(real_codex)
    assert common.resolve_trusted_executable("not-installed-anywhere") == "not-installed-anywhere"
    assert common.resolve_trusted_executable("/bin/sh") == "/bin/sh"


def test_tmux_script_uses_a_path_without_relative_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, real_bin, real_codex = hostile_path_setup(tmp_path, monkeypatch)
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(folder), exec_mode="tmux", tmux_target="s:w")
    logs = common.setup_run_logs(tmp_path / "logs", "codex", "codex", None)
    status = tmp_path / "status"
    status.write_text("0", encoding="utf-8")
    monkeypatch.setattr(scheduler.common, "have_cmd", lambda name: True)
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    scheduler.run_tmux(cfg, logs, [str(real_codex), "exec", "-"], tmp_path / "out", status)
    script = (logs.run_dir / "tmux-command.sh").read_text(encoding="utf-8")
    assert f"export PATH={real_bin}\n" in script
    assert script.index("export PATH=") > script.index("cd ")  # still inside the folder, but with a trusted PATH for every lookup
