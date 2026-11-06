"""User-facing CLI errors name the problem in plain words: no Python values, errno strings or braces (CLIUX-021)."""

from __future__ import annotations

import ast
import io
import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_missions
from llm_tools.bridge import main

PACKAGE = Path(bridge.__file__).resolve().parent


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


@pytest.fixture()
def mission(tmp_path, state, capsys) -> str:
    assert main(["mission", "create", "--prompt", "Write the parser tests", "--cwd", str(tmp_path), "--providers", "claude", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["id"]


def _error(capsys, *argv: str) -> str:
    assert main(list(argv)) == 2, capsys.readouterr()
    return capsys.readouterr().err.strip()


def test_a_missing_prompt_file_says_what_could_not_be_read(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _error(capsys, "mission", "create", "--file", "nosuch.md", "--cwd", str(tmp_path)) == "llm-bridge: cannot read --file nosuch.md: no such file"


def test_a_folder_given_as_the_prompt_file_is_named_as_a_folder(tmp_path, state, capsys) -> None:
    assert _error(capsys, "mission", "create", "--file", str(tmp_path), "--cwd", str(tmp_path)) == f"llm-bridge: cannot read --file {tmp_path}: it is a folder"


def test_a_prompt_file_that_is_not_text_is_not_reported_as_a_codec_error(tmp_path, state, capsys) -> None:
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    message = _error(capsys, "mission", "create", "--file", str(binary), "--cwd", str(tmp_path))
    assert message == f"llm-bridge: cannot read --file {binary}: it is not UTF-8 text"


def test_an_empty_prompt_on_standard_input_is_named(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _error(capsys, "mission", "create", "--file", "-", "--cwd", str(tmp_path)) == "llm-bridge: the prompt is empty (read from standard input)"


def test_an_empty_prompt_file_and_an_empty_literal_prompt_are_named(tmp_path, state, capsys) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("\n")
    assert _error(capsys, "mission", "create", "--file", str(empty), "--cwd", str(tmp_path)) == f"llm-bridge: the prompt is empty (read from {empty})"
    assert _error(capsys, "mission", "create", "--prompt", "", "--cwd", str(tmp_path)) == "llm-bridge: the prompt is empty"


def test_task_add_and_amend_report_an_empty_prompt_the_same_way(tmp_path, mission, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _error(capsys, "task", "add", "--mission", mission, "--file", "-") == "llm-bridge: the prompt is empty (read from standard input)"
    assert _error(capsys, "task", "add", "--mission", mission, "--prompt", "  ") == "llm-bridge: the prompt is empty"


def test_handoff_without_a_route_lists_the_routes_to_choose_from(mission, capsys) -> None:
    assert _error(capsys, "steer", "--mission", mission, "--kind", "handoff") == "llm-bridge: --kind handoff needs --route NAME (one of: claude)"


def test_handoff_to_a_route_outside_the_rotation_says_so(mission, capsys) -> None:
    assert _error(capsys, "steer", "--mission", mission, "--kind", "handoff", "--route", "nosuch") == "llm-bridge: --route nosuch is not in this mission's rotation (one of: claude)"


def test_unreadable_token_and_ca_files_are_named(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    message = _error(capsys, "remote", "login", "--coordinator", "https://c.example:8443", "--token-file", "nosuch.token")
    assert message == "llm-bridge: cannot read --token-file nosuch.token: no such file"


@pytest.mark.parametrize("argv", [
    ["mission", "create", "--file", "nosuch.md"],
    ["mission", "create", "--prompt", ""],
    ["steer", "--kind", "handoff"],
    ["task", "show", "--task", "99"],
    ["task", "verify", "--task", "1", "--approve"],
    ["events", "--mission", "ffffffff"],
])
def test_no_cli_error_contains_python_values_errno_text_or_braces(tmp_path, mission, capsys, monkeypatch, argv: list[str]) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = list(argv)
    if arguments[0] == "mission":
        arguments += ["--cwd", str(tmp_path)]
    elif "--mission" not in arguments:
        arguments += ["--mission", mission]
    code = main(arguments)
    err = capsys.readouterr().err
    assert code == 2 and err.startswith("llm-bridge: ")
    assert not re.search(r"\bNone\b|Errno|[{}]|Traceback", err), err


def _raised_text(node: ast.Raise) -> str:
    call = node.exc
    if not isinstance(call, ast.Call) or not call.args:
        return ""
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    if isinstance(argument, ast.JoinedStr):
        return "".join(part.value for part in argument.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return ""


def test_no_raised_message_in_the_bridge_modules_spells_a_python_value_or_errno() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("bridge*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and re.search(r"\bNone\b|Errno|\{\}", _raised_text(node)):
                offenders.append(f"{path.name}:{node.lineno}: {_raised_text(node)[:80]}")
    assert not offenders, offenders
