"""Python module launches and service definitions must not trust the current directory."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import common, usage_service

ROOT = Path(__file__).resolve().parents[1]


def test_no_launcher_builds_a_python_dash_m_command_by_hand() -> None:
    """Only `python_module_argv` spells `sys.executable ... -m`, and it adds `-P`."""
    offenders = []
    for path in sorted((ROOT / "llm_tools").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'sys\.executable,\s*(?:"-P",\s*)?"-m"', text):
            if path.name == "common.py" and '"-P", "-m", module' in text[match.start():match.end() + 20]:
                continue
            offenders.append(f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}")
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"\{python\} -m|<string>-m</string>", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def hostile_folder(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "hostile"
    (folder / "llm_tools").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    (folder / "llm_tools" / "__init__.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pkg')\n", encoding="utf-8")
    (folder / "sitecustomize.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('site')\n", encoding="utf-8")
    return folder, marker


def test_copilot_refresh_child_ignores_a_hostile_working_directory(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile_folder(tmp_path)
    launched: dict[str, object] = {}

    class Captured(Exception):
        pass

    def fake_popen(argv, **kwargs):
        launched.update(argv=list(argv), env=dict(kwargs["env"]))
        raise Captured

    live_env = {**env, "LLM_USAGE_COPILOT_CACHE_TTL": "300"}
    with monkeypatch.context() as patch:
        patch.setattr(common.subprocess, "Popen", fake_popen)
        with pytest.raises(Captured):
            common.read_copilot(live_env)
    argv = launched["argv"]
    assert argv[:3] == [sys.executable, "-P", "-m"] and argv[3] == "llm_tools.copilot_refresh"

    child_env = {**launched["env"], "LLM_USAGE_DISABLE_COPILOT": "1"}  # type: ignore[arg-type]
    result = subprocess.run(argv, cwd=folder, env=child_env, capture_output=True, text=True, timeout=60, check=False)  # type: ignore[arg-type]
    assert not marker.exists(), "code from the working directory ran in the Copilot refresh child"
    assert result.returncode == 0, result.stderr


def test_ephemeral_service_command_is_isolated() -> None:
    cmd = common.python_module_argv("llm_tools.usage_service", "--ephemeral")
    assert cmd[:3] == [sys.executable, "-P", "-m"]
    env = common.with_package_root({"PYTHONPATH": "/x"})
    assert env["PYTHONPATH"].split(os.pathsep) == [common.package_root(), "/x"]
    assert common.without_package_root(env)["PYTHONPATH"] == "/x"
    assert "PYTHONPATH" not in common.without_package_root({"PYTHONPATH": common.package_root()})


def test_systemd_unit_uses_isolated_python_and_the_home_directory(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hostile = tmp_path / "checkout"
    hostile.mkdir()
    monkeypatch.chdir(hostile)
    unit = usage_service.systemd_unit_text(30, env)
    assert f"ExecStart={sys.executable} -P -m llm_tools.usage_service --foreground --interval 30" in unit
    assert f"WorkingDirectory={env['HOME']}" in unit
    assert str(hostile) not in unit
    assert f"Environment=PYTHONPATH={common.package_root()}" in unit
    plist = usage_service.launchd_plist_text(30, env)
    assert "<string>-P</string>\n    <string>-m</string>" in plist
    assert f"<key>WorkingDirectory</key><string>{env['HOME']}</string>" in plist
    assert str(hostile) not in plist


def test_service_definitions_reject_newline_and_percent_in_paths(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("/home/a\nExecStartPre=/bin/evil", "/home/50%h"):
        with pytest.raises(ValueError):
            usage_service.systemd_unit_text(30, {**env, "HOME": bad})
        with pytest.raises(ValueError):
            usage_service.launchd_plist_text(30, {**env, "HOME": bad})
    monkeypatch.setattr(sys, "executable", "/opt/py thon/bin/python")
    unit = usage_service.systemd_unit_text(30, env)
    assert 'ExecStart="/opt/py thon/bin/python" -P -m' in unit
    monkeypatch.setattr(sys, "executable", "/opt/p$y/python")
    assert '"/opt/p$$y/python"' in usage_service.systemd_unit_text(30, env)
    monkeypatch.setattr(sys, "executable", "/opt/a&b/<python>")
    assert "/opt/a&amp;b/&lt;python&gt;" in usage_service.launchd_plist_text(30, env)
