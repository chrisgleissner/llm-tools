"""A mission folder must never be able to supply code to Bridge's own processes."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge import dispatch_console_command
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled

ROOT = Path(__file__).resolve().parents[1]


def hostile_folder(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "hostile repo"
    package = folder / "llm_tools"
    package.mkdir(parents=True)
    marker = tmp_path / "PWNED"
    (package / "__init__.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    (folder / "sitecustomize.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('site')\n", encoding="utf-8")
    return folder, marker


def test_console_command_dispatch_ignores_package_in_mission_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile_folder(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission("hostile", "Check the folder", cwd=folder)["id"]
    monkeypatch.chdir(folder)

    response = dispatch_console_command(mission_id, "events")

    assert not marker.exists(), "code from the mission folder ran as the operator"
    assert "No module named" not in response, response


def test_station_hook_runs_without_current_directory_on_path(tmp_path: Path) -> None:
    folder, marker = hostile_folder(tmp_path)
    argv = [part.replace("{client}", "none") for part in bridge_runtime.station_detach_argv("0" * 32)]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    subprocess.run(argv, cwd=folder, env=env, capture_output=True, text=True, timeout=scaled(30), check=False)
    assert not marker.exists()


def test_controller_launch_is_isolated_from_mission_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile_folder(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission("hostile", "Check the folder", cwd=folder)["id"]
    captured: dict[str, object] = {}

    class Stop(Exception):
        pass

    def fake_popen(command, **kwargs):
        captured.update(command=command, env=kwargs["env"], cwd=kwargs["cwd"])
        raise Stop

    with monkeypatch.context() as patch:
        patch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)
        with pytest.raises(Stop):
            bridge_runtime.launch_controller(mission_id, headless=True)

    # Run the command the launcher built, from the folder it chose.
    command = list(captured["command"])  # type: ignore[arg-type]
    probe = command[:command.index("_controller")] + ["--help"]
    subprocess.run(probe, cwd=captured["cwd"], env=captured["env"], capture_output=True, text=True, timeout=scaled(30), check=False)
    assert not marker.exists()


def test_every_module_launch_uses_the_isolated_helper() -> None:
    """No launcher spells `python -m llm_tools...` by hand, where `-P` could be forgotten."""
    offenders = []
    for path in sorted((ROOT / "llm_tools").glob("*.py")):
        if path.name in {"usage_service.py", "common.py"}:
            continue  # llm-usage's own service/refresh helpers never run in a mission folder
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"-m",\s*"llm_tools|-m llm_tools', line) and "python -m llm_tools" not in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_python_module_argv_puts_p_before_module() -> None:
    from llm_tools.common import python_module_argv

    assert python_module_argv("llm_tools.bridge", "status") == [sys.executable, "-P", "-m", "llm_tools.bridge", "status"]
