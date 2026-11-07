"""Windows supports the remote client only; every other command fails with a structured error."""
from __future__ import annotations

import json

import pytest

from llm_tools import bridge, bridge_remote  # noqa: F401  (imported before sys.platform is patched; ssl branches on it)


@pytest.fixture()
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge.sys, "platform", "win32")


@pytest.mark.parametrize("arguments", [
    ["status"],
    ["mission", "list"],
    ["--target", "local", "status"],
    ["worker", "run"],
    ["worker", "enroll", "--name", "x"],
    ["console"],
])
def test_local_commands_are_refused_without_a_traceback(windows: None, arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert bridge.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("llm-bridge: ") and "not supported on Windows" in lines[0]
    assert "llm-bridge --target NAME COMMAND" in lines[0] and "Traceback" not in captured.err


def test_json_refusal_is_an_error_envelope(windows: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert bridge.main(["status", "--json"]) == 2
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.err == ""
    assert envelope["schema_version"] == 1 and envelope["outcome"] == "failed"
    assert envelope["error"]["code"] == "unsupported-platform"
    assert envelope["error"]["details"] == {"platform": "windows", "command": "status"}


def test_no_arguments_prints_help_instead_of_starting_the_curses_launcher(windows: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert bridge.main([]) == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["--version"], ["help"], ["status", "--help"], ["remote", "status"], ["worker", "list"]])
def test_the_remote_client_and_help_stay_available(windows: None, arguments: list[str]) -> None:
    assert bridge.unsupported_on_windows(arguments) is None


def test_the_named_target_is_not_refused_locally(windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(bridge_remote, "run_cli", lambda target, args: seen.append(target) or 0)
    assert bridge.main(["--target", "pi", "status"]) == 0
    assert seen == ["pi"]


def test_other_platforms_are_never_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(bridge.sys, "platform", platform)
        assert bridge.unsupported_on_windows(["status"]) is None


def test_the_windows_modules_import_without_curses() -> None:
    """Windows has no curses; every module the remote client and the test harness load must import without it."""
    import subprocess
    import sys
    from pathlib import Path

    program = (
        "import sys, importlib\n"
        "sys.modules['curses'] = None\n"
        "sys.modules['_curses'] = None\n"
        "for name in ('bridge', 'bridge_remote', 'bridge_runtime', 'terminal', 'bridge_commands', 'bridge_protocol', 'bridge_ws', 'bridge_input'):\n"
        "    importlib.import_module('llm_tools.' + name)\n"
        "if sys.platform != 'win32':\n"
        "    from llm_tools import bridge_missions\n"
        "    bridge_missions.station_live('probe')  # mission list orders by liveness; it must not load the console\n"
    )
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", program], cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
