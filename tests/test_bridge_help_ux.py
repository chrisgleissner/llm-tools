"""Help pages are grouped, specific about side effects and generated from one registry (CLIUX-017/018/019/028/030/035/039/040/042/074)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_help, bridge_missions
from llm_tools.bridge import _build_parser, main
from llm_tools.bridge_help import COMMANDS, TOPICS, topic_text

_INTERNAL = {"console", "_controller", "_station-gc"}


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _public_paths() -> list[str]:
    """Every public command path of the parser, such as ``task approve``."""
    paths: list[str] = []

    def visit(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen: list[object] = []
                for name, child in action.choices.items():
                    if name in _INTERNAL or child in seen:
                        continue
                    seen.append(child)
                    path = (prefix + " " + name).strip()
                    paths.append(path)
                    visit(child, path)

    visit(_build_parser(), "")
    return paths


def _page(path: str) -> str:
    """The command page of ``path``; ``help remote`` is a topic, so command pages come from the parser."""
    parser = bridge_help._command_path_parser(path)
    assert parser is not None, path
    return parser.format_help()


def _example_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip().startswith("llm-bridge ")]


# --- CLIUX-017 ---------------------------------------------------------------------------------

def test_effects_line_is_specific_per_command() -> None:
    assert "Effects: starts provider work (uses paid capacity)" in topic_text("start")
    assert "Effects: starts provider work" in topic_text("continue")
    assert "Effects: suspends this computer" in topic_text("suspend-until-credits")
    assert "does not suspend this computer" in " ".join(topic_text("pause").split())


@pytest.mark.parametrize("path", ["status", "task list", "task show", "events", "doctor", "mission list", "task diff"])
def test_read_only_commands_have_no_effects_line(path: str) -> None:
    assert "Effects:" not in topic_text(path)


def test_command_pages_drop_boilerplate_and_carry_one_example() -> None:
    for path in _public_paths():
        text = _page(path)
        for banned in ("Does not directly start", "input files retain source text", "More: llm-bridge help keys", "Can enable provider work"):
            assert banned not in text, (path, banned)
        if text.count("\n  ") and path not in {"mission", "task", "proposal", "worker", "remote"}:
            assert text.count("Example:") == 1, path
            assert len(_example_lines(text.split("Example:", 1)[1])) == 1, path


# --- CLIUX-018 ---------------------------------------------------------------------------------

def test_top_level_help_is_grouped_and_lists_global_options() -> None:
    text = _build_parser().format_help()
    assert "positional arguments" not in text and "COMMAND ..." not in text
    headings = ["Start here", "Missions", "Tasks", "Run control", "Station", "Capacity and power", "Data", "Distributed", "Appearance", "Global options"]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "--version" in text and "--target WORKER" in text
    for command in ("suspend-until-credits", "continue", "agents", "gc", "steer", "rotation", "theme", "backup", "restore", "worker", "remote"):
        assert re.search(rf"^\s+{re.escape(command)}\s+\S", text, re.M), command


def test_every_public_top_level_command_is_in_the_help_groups() -> None:
    listed = {entry[0].split()[0] for _, entries in bridge_help.COMMAND_GROUPS for entry in entries}
    public = {path for path in _public_paths() if " " not in path}
    assert public <= listed, public - listed


def test_root_help_keeps_a_separate_trailer_for_the_exit_status_section(monkeypatch) -> None:
    assert bridge_help.root_help().rstrip().endswith(bridge_help.EXIT_STATUS_TEXT.rstrip())  # CLIUX-016 fills the slot
    monkeypatch.setattr(bridge_help, "ROOT_HELP_TRAILER", "Exit status\n  0  success")
    assert _build_parser().format_help().rstrip().endswith("Exit status\n  0  success")


# --- CLIUX-019 ---------------------------------------------------------------------------------

def _rows(path: str) -> list[str]:
    """The subcommand names in the Commands section of a page, in printed order."""
    return re.findall(r"^  ([\w-]+)\s{2,}\S", _page(path).split("Commands:")[1].split("\n\n")[0], re.M)


def test_subcommands_are_listed_common_first_without_duplicate_rows() -> None:
    assert _rows("mission") == ["create", "list", "set", "settings", "delete", "colour", "export"]
    assert _rows("task")[:6] == ["add", "list", "show", "approve", "reject", "amend"]
    for path in ("task", "proposal", "worker", "remote"):
        rows = _rows(path)
        assert rows and len(rows) == len(set(rows)), path
    assert "color" not in _page("mission").replace("colour", "")


def test_every_public_command_has_a_description_and_one_example() -> None:
    for path in _public_paths():
        assert path in COMMANDS, path
        description, example = COMMANDS[path]
        assert description and example, path
        text = _page(path)
        assert "Example:" in text, path
        _build_parser().parse_args(__import__("shlex").split(example))


def test_listing_descriptions_use_one_case_style() -> None:
    for path in ("mission", "task", "proposal", "worker", "remote"):
        text = _page(path).split("Commands:")[1].split("\n\n")[0]
        for line in text.splitlines()[1:]:
            match = re.match(r"^  [\w-]+\s{2,}(\S)(\S?)", line)
            if match:
                assert match.group(1).islower() or not match.group(2).islower(), line


# --- CLIUX-028 / 074 ---------------------------------------------------------------------------

def test_console_help_is_generated_grouped_and_states_the_slash_mapping() -> None:
    text = topic_text("console")
    for heading in ("Start here", "Missions", "Tasks", "Run control", "Station", "Capacity and power", "Data", "Appearance"):
        assert heading in text
    for command in ("/sessions", "/agents", "/finish", "/cancel", "/steer", "/rotation", "/continue", "/suspend-until-credits"):
        assert command in text, command
    assert len(re.findall(r"^\s+/tasks\b", text, re.M)) == 1
    assert "/reject [N] WHY" in text and "llm-bridge task reject --task N --reason WHY" in text
    assert "/approve" in text and "llm-bridge task approve --task N" in text
    assert "are the same commands without the slash" not in text


def test_console_help_covers_every_public_top_level_command() -> None:
    text = topic_text("console")
    console_names = {"detach": "leave"}  # CLIUX-059: in the console, /leave leaves your own terminal
    for path in _public_paths():
        if " " not in path:
            assert "/" + console_names.get(path, path) in text, path


def test_one_topic_registry_feeds_every_list_of_topics(capsys) -> None:
    names = bridge_help.topic_names()
    assert set(names) == set(TOPICS) and "terms" in names and "console" in names and "distributed" in names
    assert list(names)[0] == "quickstart"
    for name in names:
        assert bridge_help.topic_summary(name)
    error = topic_text("nosuch")
    assert all(name in error for name in names)
    with pytest.raises(SystemExit):
        main(["help", "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert all(name in shown for name in names)
    assert all(name in topic_text("console") for name in names)
    assert main(["help"]) == 0
    index = capsys.readouterr().out
    assert all(re.search(rf"^\s+{name}\s{{2,}}\S", index, re.M) for name in names)
    assert all(name in " ".join(topic_text("quickstart").split()) for name in names if name not in {"quickstart", "terms", "console"})


# --- CLIUX-030 ---------------------------------------------------------------------------------

def test_help_prose_uses_task_language() -> None:
    banned = ("owned controller", "typed intent", "typed steering", "authoritative transition", "invocation boundary", "safe execution boundary", "scripted CLI semantics")
    for path in _public_paths():
        text = " ".join(_page(path).split())
        for phrase in banned:
            assert phrase not in text, (path, phrase)
    for topic in TOPICS:
        text = " ".join(topic_text(topic).split())
        for phrase in banned:
            assert phrase not in text, (topic, phrase)
    assert "first provider that has capacity" in " ".join(topic_text("start").split())
    steer = " ".join(topic_text("steer").split())
    assert "next safe point" in steer


def test_terms_topic_defines_the_vocabulary() -> None:
    text = topic_text("terms")
    for term in ("Mission", "Task", "Station", "Controller", "Provider", "Rotation", "Verify"):
        assert re.search(rf"^{term}\s", text, re.M), term


# --- CLIUX-035 ---------------------------------------------------------------------------------

def _create(capsys, tmp_path, *extra: str) -> tuple[int, str, str]:
    code = main(["mission", "create", "--prompt", "Write tests", "--cwd", str(tmp_path), "--providers", "claude", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _verifier(capsys, tmp_path, *extra: str) -> list[str]:
    code, out, err = _create(capsys, tmp_path, "--json", *extra)
    assert code == 0, err
    return json.loads(out)["mission"]["config"]["verifier"]["argv"]


def test_verifier_option_is_split_like_a_shell_and_echoed_as_argv(state, tmp_path, capsys) -> None:
    assert _verifier(capsys, tmp_path, "--verifier", "python -m pytest -q") == ["python", "-m", "pytest", "-q"]
    code, out, _ = _create(capsys, tmp_path, "--verifier", "python -m pytest -q 'a b'")
    assert code == 0 and '· verifier ["python", "-m", "pytest", "-q", "a b"]' in out


def test_verifier_after_double_dash_and_verifier_arg_still_work(state, tmp_path, capsys) -> None:
    assert _verifier(capsys, tmp_path, "--", "python", "-m", "pytest", "-q") == ["python", "-m", "pytest", "-q"]
    assert _verifier(capsys, tmp_path, "--verifier-arg", "python", "--verifier-arg=-m", "--verifier-arg", "pytest") == ["python", "-m", "pytest"]


def test_two_verifier_forms_are_refused_and_bad_quoting_is_explained(state, tmp_path, capsys) -> None:
    code, _, err = _create(capsys, tmp_path, "--verifier", "pytest", "--", "pytest")
    assert code == 2 and "one verifier form" in err
    code, _, err = _create(capsys, tmp_path, "--verifier", "pytest 'unclosed")
    assert code == 2 and "--verifier" in err and "quot" in err


def test_help_shows_the_verifier_forms() -> None:
    create = " ".join(topic_text("mission create").split())
    assert "--verifier COMMAND" in create and "-- python -m pytest -q" in create
    verification = " ".join(topic_text("verification").split())
    assert "--verifier 'python -m pytest -q'" in verification
    assert "--verifier-arg python" not in verification


# --- CLIUX-039 ---------------------------------------------------------------------------------

def test_theme_output_ends_with_how_to_set_the_theme(state, capsys) -> None:
    assert main(["theme"]) == 0
    lines = capsys.readouterr().out.rstrip().splitlines()
    assert "LLM_BRIDGE_THEME=light" in lines[-1] and '[bridge.theme] name = "light"' in lines[-1]


def test_theme_flags_use_one_spelling_with_hidden_aliases(state, capsys) -> None:
    text = " ".join(topic_text("theme").split())
    assert "--palette-size" in text and "--colour {auto,always,never}" in text
    assert "--colors" not in text and "--color " not in text and "--color," not in text
    for flags in (["--palette-size", "16"], ["--colors", "16"], ["--colour", "never"], ["--color", "never"]):
        assert main(["theme", "--json", *flags]) == 0
        json.loads(capsys.readouterr().out)
    assert main(["theme", "--colors", "16", "--palette-size", "256", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["colors"] == 256


# --- CLIUX-040 ---------------------------------------------------------------------------------

def test_steer_help_keeps_handoff_and_review_and_names_the_provider_flag() -> None:
    text = " ".join(topic_text("steer").split())
    assert "--kind {handoff,review}" in text and "--to PROVIDER" in text
    assert "--route" not in text and "pause-after-iteration" not in text.split("Other kinds")[0]
    assert "task amend" in text and "task verify" in text and "pause" in text


def test_steer_to_and_the_old_route_flag_both_work(state, tmp_path, capsys) -> None:
    code, out, _ = _create(capsys, tmp_path, "--json")
    mission = json.loads(out)["mission"]["id"]
    assert main(["steer", "--mission", mission, "--kind", "handoff", "--to", "claude"]) == 0
    assert "handoff" in capsys.readouterr().out
    assert main(["steer", "--mission", mission, "--kind", "handoff", "--route", "claude"]) == 0
    capsys.readouterr()


def test_steer_errors_name_the_missing_piece_with_an_example(state, tmp_path, capsys) -> None:
    code, out, _ = _create(capsys, tmp_path, "--json")
    mission = json.loads(out)["mission"]["id"]
    assert main(["steer", "--mission", mission, "--kind", "review"]) == 2
    err = capsys.readouterr().err
    assert '--kind review needs --task N' in err and 'e.g. llm-bridge steer --kind review --task 1 --prompt "check error paths"' in err
    assert main(["steer", "--mission", mission, "--kind", "handoff"]) == 2
    err = capsys.readouterr().err
    assert "--kind handoff needs --to PROVIDER (one of: claude)" in err and "e.g. llm-bridge steer --kind handoff --to claude" in err


# --- CLIUX-042 ---------------------------------------------------------------------------------

def test_rotation_help_shows_only_set_and_the_saved_config_wording() -> None:
    text = " ".join(topic_text("rotation").split())
    assert "--set LIST" in text and "--routes" not in text and "--providers" not in text
    assert "frozen" not in text and "saved with the mission" in text


def test_rotation_old_setters_remain_hidden_aliases(state, tmp_path, capsys) -> None:
    code, out, _ = _create(capsys, tmp_path, "--json")
    mission = json.loads(out)["mission"]["id"]
    for flag in ("--providers", "--routes", "--set"):
        assert main(["rotation", "--mission", mission, flag, "claude", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["providers"] == ["claude"]
