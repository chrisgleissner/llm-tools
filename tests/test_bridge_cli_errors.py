"""Command-line error recovery: mistyped commands and options, and entry points that need a terminal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_help, bridge_tui, usage
from llm_tools.bridge_store import BridgeStore
from tests.conftest import without_help_reference


def _fail(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as raised:
        bridge.main(arguments)
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    return without_help_reference(captured.err)


def test_unknown_command_suggests_the_closest_public_command(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["stauts"], capsys)
    assert error == "llm-bridge: unknown command 'stauts' · did you mean status? · llm-bridge --help lists commands\n"


def test_unknown_command_never_lists_or_suggests_internal_commands(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["controler"], capsys)
    assert "_controller" not in error and "_station-gc" not in error and "choose from" not in error
    assert error.startswith("llm-bridge: unknown command 'controler'")
    assert "console" not in _fail(["consol"], capsys)


def test_unknown_command_without_a_close_match_points_at_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert _fail(["zzzzzz"], capsys) == "llm-bridge: unknown command 'zzzzzz' · llm-bridge --help lists commands\n"


def test_unknown_subcommand_names_its_group(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["task", "lst"], capsys)
    assert error == "llm-bridge: unknown task command 'lst' · did you mean list? · llm-bridge task --help lists commands\n"


def test_unknown_option_suggests_the_closest_option(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["status", "--misison", "14f5"], capsys)
    assert error == "llm-bridge: unknown option '--misison' · did you mean --mission? · llm-bridge status --help lists options\n"


def test_unknown_option_with_a_value_is_reported_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["task", "list", "--limt=3"], capsys)
    assert error == "llm-bridge: unknown option '--limt' · did you mean --limit? · llm-bridge task list --help lists options\n"


def test_hidden_options_are_not_suggested(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["missions", "--frm", "x"], capsys)
    assert error.startswith("llm-bridge: unknown option '--frm'") and "--from" not in error


def test_invalid_option_value_is_not_reported_as_an_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    error = _fail(["theme", "--color", "bogus"], capsys)
    assert "unknown command" not in error
    assert "invalid choice: 'bogus'" in error


def test_internal_commands_stay_callable() -> None:
    parser = bridge._build_parser()
    assert parser.parse_args(["_station-gc", "--mission", "abc"]).command == "_station-gc"
    assert parser.parse_args(["_controller", "--mission", "abc"]).command == "_controller"
    assert parser.parse_args(["console", "--mission", "abc"]).command == "console"


def test_missions_from_and_attach_client_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        bridge.main(["missions", "--help"])
    missions_help = capsys.readouterr().out
    assert "--from" not in missions_help and "mission whose station" not in missions_help
    assert bridge._build_parser().parse_args(["missions", "--from", "abc"]).from_mission == "abc"
    with pytest.raises(SystemExit):
        bridge.main(["attach", "--help"])
    attach_help = " ".join(capsys.readouterr().out.split())
    assert "terminal to use when several are attached (for example /dev/pts/3)" in attach_help
    assert "select a listed writable source client" not in attach_help


@pytest.fixture()
def mission_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        return str(store.create_mission("no terminal", "objective", cwd=tmp_path)["id"])


def _forbid_curses(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        raise AssertionError("a curses screen must not start without a terminal")

    monkeypatch.setattr(bridge_tui, "run_control", refuse)
    monkeypatch.setattr(bridge_tui, "run_console", refuse)


def test_control_without_a_terminal_explains_itself(mission_id: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _forbid_curses(monkeypatch)
    assert bridge.main(["control", "--mission", mission_id]) == 2
    captured = capsys.readouterr()
    assert without_help_reference(captured.err) == "llm-bridge: control needs an interactive terminal; use llm-bridge status\n"
    assert captured.out == "" and "Traceback" not in captured.err


def test_console_without_a_terminal_explains_itself(mission_id: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _forbid_curses(monkeypatch)
    assert bridge.main(["console", "--mission", mission_id]) == 2
    assert without_help_reference(capsys.readouterr().err) == "llm-bridge: console needs an interactive terminal; use llm-bridge status\n"


def test_usage_without_a_terminal_prints_the_usage_table(mission_id: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _forbid_curses(monkeypatch)
    monkeypatch.setattr(usage, "dashboard_snapshot", lambda cfg, **kw: {"text": "Provider   Remaining\nclaude     80%\n", "rows": []})
    assert bridge.main(["usage", "--mission", mission_id]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Provider   Remaining\nclaude     80%\n"
    assert captured.err == ""


def test_usage_without_a_terminal_needs_no_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    _forbid_curses(monkeypatch)
    monkeypatch.setattr(usage, "dashboard_snapshot", lambda cfg, **kw: {"text": "table\n", "rows": []})
    assert bridge.main(["usage"]) == 0
    assert capsys.readouterr().out == "table\n"


def test_usage_fallback_reports_a_failed_read_without_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))

    def broken(cfg: object, **kw: object) -> dict[str, object]:
        raise OSError("usage cache unreadable")

    monkeypatch.setattr(usage, "dashboard_snapshot", broken)
    assert bridge.main(["usage"]) == 2
    assert without_help_reference(capsys.readouterr().err) == "llm-bridge: usage cache unreadable\n"


def test_error_exits_end_with_the_troubleshooting_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert bridge.main(["status", "--mission", "zzzz"]) == 3
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("llm-bridge: ") and lines[-1] == f"see: {bridge_help.troubleshooting_reference('mission-not-found')}"
    assert lines[-1].endswith("/docs/llm-bridge/troubleshooting.md#mission-not-found")
    assert sum(line.startswith("see: ") for line in lines) == 1


def test_argparse_errors_carry_the_reference_after_the_standard_report(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        bridge.main(["theme", "--color", "bogus"])
    assert raised.value.code == 2
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("usage: llm-bridge theme") and "invalid choice: 'bogus'" in lines[-2]
    assert lines[-1] == f"see: {bridge_help.troubleshooting_reference('usage')}"


def test_json_errors_keep_the_hint_and_add_the_help_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert bridge.main(["status", "--mission", "zzzz", "--json"]) == 3
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"]
    assert captured.err == ""
    assert error["code"] == "mission-not-found"
    assert error["hint"] == "run llm-bridge mission list"
    assert error["help"] == bridge_help.troubleshooting_reference("mission-not-found")
    assert set(error) == {"code", "message", "hint", "help"}


def test_successful_commands_print_no_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert bridge.main(["mission", "list"]) == 0
    captured = capsys.readouterr()
    assert "see: " not in captured.out + captured.err
