"""Command-line conventions: machine output, exit codes, one list command, short and positional forms.

CLIUX-015, 016, 033, 041, 045, 049, 071, 072 and 080. Every test runs against an isolated state
directory; no provider or station is started.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_missions
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _run(capsys: pytest.CaptureFixture[str], *argv: str, code: int = 0) -> tuple[str, str]:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out, captured.err


def _usage_error(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[str, str]:
    with pytest.raises(SystemExit) as raised:
        main(list(argv))
    assert raised.value.code == 2
    captured = capsys.readouterr()
    return captured.out, captured.err


def _create(tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str = "work", *extra: str) -> dict[str, Any]:
    folder = tmp_path / name
    folder.mkdir()
    out, _ = _run(capsys, "mission", "create", "--prompt", f"{name} objective", "--name", name, "--cwd", str(folder), "--providers", "codex", *extra, "--json")
    return json.loads(out)  # type: ignore[no-any-return]


def _finish_first_task(state: Path, mission_id: str, task_id: int) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)


# ---------------------------------------------------------------- CLIUX-015: machine output


def test_json_errors_go_to_stdout_as_an_error_object(state, capsys) -> None:
    out, err = _run(capsys, "status", "--mission", "zzzz", "--json", code=3)
    assert err == ""
    error = json.loads(out)["error"]
    assert error["code"] == "mission-not-found"
    assert "no mission matches 'zzzz'" in error["message"]
    assert error["hint"] == "run llm-bridge mission list"


def test_json_usage_errors_are_error_objects_too(state, capsys) -> None:
    out, err = _usage_error(capsys, "status", "--misison", "abc", "--json")
    assert err == ""
    error = json.loads(out)["error"]
    assert error["code"] == "usage" and "--misison" in error["message"]


def test_plain_errors_stay_on_stderr(state, capsys) -> None:
    out, err = _run(capsys, "status", "--mission", "zzzz", code=3)
    assert out == "" and err.startswith("llm-bridge: no mission matches 'zzzz'")


def test_every_json_list_is_an_object(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    tasks = json.loads(_run(capsys, "task", "list", "--mission", mid, "--json")[0])
    assert isinstance(tasks, dict) and tasks["total"] == 1 and len(tasks["tasks"]) == 1
    assert tasks["tasks"][0]["ordinal"] == 1
    for command, key in ((("mission", "list"), "missions"), (("missions",), "missions"), (("events", "--mission", mid), "events"), (("proposal", "list", "--mission", mid), "proposals")):
        value = json.loads(_run(capsys, *command, "--json")[0])
        assert isinstance(value, dict) and isinstance(value[key], list), command


def test_task_json_carries_the_number_users_type(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    shown = json.loads(_run(capsys, "task", "show", "--mission", mid, "--task", "1", "--json")[0])
    task = shown.get("task", shown)
    assert task["number"] == task["ordinal"] == 1
    assert created["task"]["number"] == 1


# ---------------------------------------------------------------- CLIUX-016: exit codes


def test_exit_status_is_documented_in_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = capsys.readouterr().out
    assert "Exit status" in help_text
    for fragment in ("0  success or nothing to do", "1  runtime failure", "2  usage error", "3  not found or ambiguous", "4  wrong state"):
        assert fragment in help_text


def test_not_found_and_ambiguous_missions_exit_3(tmp_path, state, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.create_mission("first", "o", cwd=tmp_path, mission_id="abcd1111" + "0" * 24)
        other = tmp_path / "other"
        other.mkdir()
        store.create_mission("second", "o", cwd=other, mission_id="abcd2222" + "0" * 24)
    assert "ambiguous" in _run(capsys, "status", "--mission", "abcd", code=3)[1]
    out, _ = _run(capsys, "status", "--mission", "abcd", "--json", code=3)
    assert json.loads(out)["error"]["code"] == "mission-ambiguous"
    assert "no mission matches" in _run(capsys, "status", "--mission", "zzzz", code=3)[1]


def test_unknown_task_number_exits_3(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    err = _run(capsys, "task", "show", "--mission", mid, "--task", "9", code=3)[1]
    assert "there is no task 9" in err
    out, _ = _run(capsys, "task", "approve", "--mission", mid, "--task", "9", "--json", code=3)
    assert json.loads(out)["error"]["code"] == "task-not-found"


def test_wrong_state_exits_4(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    # The task is queued, not awaiting review.
    err = _run(capsys, "task", "approve", "--mission", mid, "--task", "1", code=4)[1]
    assert err.startswith("llm-bridge: ")
    out, _ = _run(capsys, "task", "approve", "--mission", mid, "--task", "1", "--json", code=4)
    assert json.loads(out)["error"]["code"] == "wrong-state"
    _run(capsys, "cancel", "--mission", mid)
    assert "cannot be started" in _run(capsys, "start", "--mission", mid, code=4)[1]


def test_finish_on_a_mission_that_is_not_native_exits_4(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys, "headless", "--mode", "headless")["mission"]["id"]
    assert _run(capsys, "finish", "--mission", mid, code=4)[1].startswith("llm-bridge: ")


def test_runtime_failure_exits_1(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]

    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("the controller could not be started")

    monkeypatch.setattr(bridge, "start_mission", broken)
    monkeypatch.setattr(bridge, "require_launchable_rotation", lambda *_a, **_k: None)
    assert "could not be started" in _run(capsys, "start", "--mission", mid, code=1)[1]


def test_no_op_outcomes_exit_0_and_say_already(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    _run(capsys, "pause", "--mission", mid)
    out, _ = _run(capsys, "pause", "--mission", mid)
    assert out.startswith("Already paused · mission " + mid[:8])
    stopped = _create(tmp_path, capsys, "second")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(stopped, "RUNNING")
        store.set_mission_status(stopped, "STOPPING")
        store.set_mission_status(stopped, "STOPPED")
    out, _ = _run(capsys, "stop", "--mission", stopped)
    assert out.startswith("Already stopped · mission " + stopped[:8])


def test_usage_errors_exit_2(state, capsys) -> None:
    assert "unknown option '--bogus'" in _usage_error(capsys, "status", "--bogus")[1]


# ---------------------------------------------------------------- CLIUX-033: one list command


def test_mission_list_is_the_same_listing_as_missions(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys, "alpha")
    _finish_first_task(state, created["mission"]["id"], created["task"]["id"])
    _create(tmp_path, capsys, "beta")
    listing = _run(capsys, "mission", "list")[0]
    assert listing == _run(capsys, "missions")[0]
    lines = listing.splitlines()
    header = lines[1].split()
    assert header[:2] == ["ID", "STATE"] and header[2:4] == ["NEEDS", "YOU"] and header[4:] == ["TASKS", "NAME", "FOLDER", "UPDATED"]
    alpha = next(line for line in lines if "alpha" in line)
    assert "review task 1" in alpha and "0/1" in alpha
    beta = next(line for line in lines if "beta" in line)
    assert "review" not in beta and "0/1" in beta


def test_mission_list_json_keeps_its_shape(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    listed = json.loads(_run(capsys, "mission", "list", "--json")[0])["missions"]
    assert [item["id"] for item in listed] == [mid]
    assert "config" not in listed[0] or "frozen_tool_config" not in listed[0]["config"]


# ---------------------------------------------------------------- CLIUX-049: short unique prefixes


def test_three_character_unique_prefix_is_accepted(tmp_path, state, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.create_mission("first", "o", cwd=tmp_path, mission_id="abc11111" + "0" * 24)
        other = tmp_path / "other"
        other.mkdir()
        store.create_mission("second", "o", cwd=other, mission_id="abd22222" + "0" * 24)
    assert json.loads(_run(capsys, "status", "--mission", "abc", "--json")[0])["mission"]["id"].startswith("abc11111")
    too_short = _run(capsys, "status", "--mission", "ab", code=2)[1]
    assert "too short" in too_short and "at least 3 characters" in too_short


# ---------------------------------------------------------------- CLIUX-071: no abbreviated options


def test_long_options_are_not_abbreviations(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert "unknown option '--f'" in _usage_error(capsys, "stop", "--f", "--mission", mid)[1]
    assert "unknown option '--mis'" in _usage_error(capsys, "status", "--mis", mid)[1]
    assert "unknown option '--auto'" in _usage_error(capsys, "mission", "create", "--prompt", "q", "--auto")[1]


def test_no_parser_allows_abbreviations() -> None:
    import argparse

    def walk(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
        found = [parser]
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    found.extend(walk(child))
        return found

    for built in (bridge._build_parser(), bridge._build_parser(parser_class=bridge._console_parser_class(), default_mission="abc")):
        assert all(parser.allow_abbrev is False for parser in walk(built))


def test_explicit_aliases_still_work() -> None:
    parser = bridge._build_parser()
    assert parser.parse_args(["task", "add", "--mission", "abc", "--prompt-file", "-"]).prompt_file == "-"
    assert parser.parse_args(["events", "-f", "--mission", "abc"]).follow is True


# ---------------------------------------------------------------- CLIUX-072: one pagination model


def test_list_commands_say_how_many_are_shown_of_how_many(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second")
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "third")
    out = _run(capsys, "task", "list", "--mission", mid, "--limit", "2")[0]
    assert "showing 2 of 3 tasks" in out
    # The same wording for missions, and a JSON total.
    _create(tmp_path, capsys, "other")
    assert "showing 1 of 2 missions" in _run(capsys, "missions", "--limit", "1")[0]
    assert "showing 1 of 2 missions" in _run(capsys, "mission", "list", "--limit", "1")[0]
    value = json.loads(_run(capsys, "missions", "--limit", "1", "--json")[0])
    assert len(value["missions"]) == 1 and value["total"] == 2
    page = json.loads(_run(capsys, "task", "list", "--mission", mid, "--limit", "2", "--json")[0])
    assert len(page["tasks"]) == 2 and page["total"] == 3


def test_proposal_list_reports_its_page(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        for index in range(3):
            store.propose_task(mid, f"idea {index}", source_operation_id=operation, title=f"idea {index}")
    out = _run(capsys, "proposal", "list", "--mission", mid, "--limit", "2")[0]
    assert "showing 2 of 3 proposals" in out and "--after" in out
    page = json.loads(_run(capsys, "proposal", "list", "--mission", mid, "--limit", "2", "--json")[0])
    assert len(page["proposals"]) == 2 and page["total"] == 3


# ---------------------------------------------------------------- CLIUX-080: positional task numbers, short options


def test_task_number_is_accepted_positionally(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    assert "task 1" in _run(capsys, "task", "show", "1", "--mission", mid)[0]
    assert "task 1" in _run(capsys, "task", "show", "--mission", mid, "1")[0]
    assert "task 1" in _run(capsys, "task", "show", "--task", "1", "--mission", mid)[0]
    out, _ = _run(capsys, "task", "approve", "1", "--mission", mid)
    assert "task 1" in out and "COMPLETED" in out


def test_short_options(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    shown = json.loads(_run(capsys, "task", "show", "-m", mid[:8], "-t", "1", "-j")[0])
    assert shown.get("task", shown)["ordinal"] == 1
    assert json.loads(_run(capsys, "status", "-m", mid[:8], "-j")[0])["mission"]["id"] == mid


def test_a_task_named_twice_is_a_usage_error(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert "not allowed with" in _usage_error(capsys, "task", "show", "1", "--task", "2", "--mission", mid)[1]
    assert "required" in _usage_error(capsys, "task", "show", "--mission", mid)[1]


def test_proposal_accept_takes_a_positional_number() -> None:
    parser = bridge._build_parser()
    assert parser.parse_args(["proposal", "accept", "2", "--mission", "abc"]).task == 2
    assert parser.parse_args(["proposal", "reject", "--task", "2", "--reason", "x", "--mission", "abc"]).task == 2


# ---------------------------------------------------------------- CLIUX-045: view PATH


def test_view_takes_a_positional_path(tmp_path, state, capsys, monkeypatch) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False, "fileno": lambda self: 0})())
    assert "# Notes" in _run(capsys, "view", str(notes))[0]
    assert "# Notes" in _run(capsys, "view", "--file", str(notes))[0]
    assert "required" in _usage_error(capsys, "view")[1]


# ---------------------------------------------------------------- CLIUX-041: coordinator URLs and --target


def test_coordinator_scheme_is_accepted_either_way(tmp_path, state, capsys, monkeypatch) -> None:
    from llm_tools import bridge_remote, bridge_worker

    token = tmp_path / "token"
    token.write_text("x" * 40, encoding="utf-8")
    seen: dict[str, str] = {}

    class Config:
        coordinator_url = "wss://bridge.example:8443"

        def public(self) -> dict[str, Any]:
            return {"name": "w", "worker_id": "0" * 32, "coordinator_url": seen["worker"], "allow_control": False}

    def enroll(name: str, url: str, token_text: str, **_kwargs: Any) -> Any:
        seen["worker"] = url
        return Config()

    def login(url: str, token_text: str, **_kwargs: Any) -> Any:
        seen["remote"] = url
        return type("Remote", (), {"coordinator_url": url})()

    monkeypatch.setattr(bridge_worker, "enroll", enroll)
    monkeypatch.setattr(bridge_remote, "login", login)
    _run(capsys, "worker", "enroll", "--name", "w", "--coordinator", "https://bridge.example:8443/", "--token-file", str(token))
    _run(capsys, "remote", "login", "--coordinator", "wss://bridge.example:8443", "--token-file", str(token))
    assert seen == {"worker": "wss://bridge.example:8443", "remote": "https://bridge.example:8443"}


def test_target_after_the_command_says_to_put_it_first(state, capsys) -> None:
    err = _usage_error(capsys, "mission", "list", "--target", "build-1")[1]
    assert "--target goes before the command" in err
    assert "llm-bridge --target build-1 mission list" in err


def test_worker_list_json_is_an_object(capsys) -> None:
    import argparse

    from llm_tools import bridge_remote

    class Client:
        def workers(self) -> list[dict[str, Any]]:
            return [{"name": "ws", "state": "online", "last_seen": 0}]

    args = argparse.Namespace(command="worker", worker_action="list", json=True)
    assert bridge_remote.run_cli("all", args, client=Client()) == 0  # type: ignore[arg-type]
    assert [worker["name"] for worker in json.loads(capsys.readouterr().out)["workers"]] == ["ws"]
