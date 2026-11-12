"""Mission templates: create from a TOML file, export one, and the verifier guard on a template verifier."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_templates
from llm_tools.bridge import main
from llm_tools.bridge_runtime import run_configured_verifier
from llm_tools.bridge_store import BridgeStore, store_path

BASIC = """
template = 1
name = "Release train"
objective = \"\"\"
Ship the release.
Keep it small.
\"\"\"

[rotation]
providers = ["codex"]

[policy]
teams = "off"
token_spend = "low"
mode = "headless"
log_rotations = 5

[[tasks]]
id = "build"
title = "Build"
body = "Run the build."
acceptance = ["it builds"]

[[tasks]]
id = "test"
title = "Test"
body = "Run the tests."
depends_on = ["build"]
"""


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _create(capsys: pytest.CaptureFixture[str], repo: Path, *extra: str) -> dict[str, Any]:
    code, out, err = _run(capsys, "mission", "create", "--cwd", str(repo), "--json", "--include-config", *extra)
    assert code == 0, err + out
    return json.loads(out)


def _tasks(mission_id: str) -> list[dict[str, Any]]:
    with BridgeStore(store_path(), readonly=True) as store:
        return sorted(store.list_tasks(mission_id), key=lambda t: t["ordinal"])


def _name_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "xdg" / "llm-tools" / "templates"
    folder.mkdir(parents=True)
    return folder


def test_create_from_path_builds_objective_tasks_and_dependencies(env, tmp_path, capsys) -> None:
    path = tmp_path / "t.toml"
    path.write_text(BASIC, encoding="utf-8")
    result = _create(capsys, env, "--template", str(path))
    mission = result["mission"]
    assert mission["name"] == "Release train"
    assert mission["objective"] == "Ship the release.\nKeep it small.\n"
    assert mission["config"]["providers"] == "codex"
    assert mission["config"]["token_spend"] == "low" and mission["config"]["team_policy_enabled"] is False
    assert mission["config"]["headless"] is True and mission["config"]["log_rotations"] == 5
    tasks = _tasks(mission["id"])
    assert [t["title"] for t in tasks] == ["Build", "Test"]
    assert tasks[0]["prompt"] == "Run the build." and tasks[0]["acceptance"] == ["it builds"]
    assert tasks[1]["depends_on_ordinals"] == [1]
    assert result["task"]["id"] == tasks[0]["id"]


def test_create_from_name_uses_the_templates_folder(env, tmp_path, capsys) -> None:
    (_name_dir(tmp_path) / "train.toml").write_text(BASIC, encoding="utf-8")
    result = _create(capsys, env, "--template", "train")
    assert result["mission"]["name"] == "Release train"


def test_unknown_name_lists_available_templates_and_exits_2(env, tmp_path, capsys) -> None:
    (_name_dir(tmp_path) / "train.toml").write_text(BASIC, encoding="utf-8")
    code, out, err = _run(capsys, "mission", "create", "--cwd", str(env), "--template", "nope")
    assert code == 2
    assert "unknown template 'nope'" in out + err and "train" in out + err


def test_unknown_key_is_rejected_with_its_name(env, tmp_path, capsys) -> None:
    for text, key in ((BASIC.replace('name = "Release train"', 'name = "x"\nbogus = 1'), "bogus"),
                      (BASIC.replace("[policy]", "[policy]\nspeed = 1"), "speed"),
                      (BASIC.replace('id = "build"', 'id = "build"\nowner = "me"'), "owner")):
        path = tmp_path / "bad.toml"
        path.write_text(text, encoding="utf-8")
        code, out, err = _run(capsys, "mission", "create", "--cwd", str(env), "--template", str(path))
        assert code == 2 and f"unknown key '{key}'" in out + err
    assert not store_path().exists() or BridgeStore(store_path(), readonly=True).list_missions() == []


def test_unknown_dependency_and_cycle_are_rejected(env, tmp_path, capsys) -> None:
    unknown = tmp_path / "u.toml"
    unknown.write_text(BASIC.replace('["build"]', '["ghost"]'), encoding="utf-8")
    code, out, err = _run(capsys, "mission", "create", "--cwd", str(env), "--template", str(unknown))
    assert code == 2 and "depends on unknown task 'ghost'" in out + err
    cycle = tmp_path / "c.toml"
    cycle.write_text(BASIC.replace('title = "Build"', 'title = "Build"\ndepends_on = ["test"]'), encoding="utf-8")
    code, out, err = _run(capsys, "mission", "create", "--cwd", str(env), "--template", str(cycle))
    assert code == 2 and "would create a cycle" in out + err
    assert BridgeStore(store_path(), readonly=True).list_missions() == []  # nothing half-built is left behind


def test_missing_version_is_rejected(env, tmp_path, capsys) -> None:
    path = tmp_path / "v.toml"
    path.write_text(BASIC.replace("template = 1", ""), encoding="utf-8")
    code, out, err = _run(capsys, "mission", "create", "--cwd", str(env), "--template", str(path))
    assert code == 2 and "template = 1" in out + err


def test_command_line_options_override_the_template(env, tmp_path, capsys) -> None:
    path = tmp_path / "t.toml"
    path.write_text(BASIC, encoding="utf-8")
    result = _create(capsys, env, "--template", str(path), "--name", "Mine", "--token-spend", "high", "--teams", "on",
                     "--providers", "claude", "--mode", "background", "--log-rotations", "3", "--prompt", "Other objective")
    mission = result["mission"]
    assert mission["name"] == "Mine" and mission["objective"] == "Other objective"
    config = mission["config"]
    assert config["token_spend"] == "high" and config["team_policy_enabled"] is True
    assert config["providers"] == "claude" and not config["headless"] and config["log_rotations"] == 3
    assert [t["title"] for t in _tasks(mission["id"])] == ["Build", "Test"]


def test_create_requires_a_prompt_without_a_template(env, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mission", "create", "--cwd", str(env)])
    assert raised.value.code == 2
    assert "is required" in capsys.readouterr().err


def _verifier_template(repo: Path, tmp_path: Path) -> Path:
    (repo / "check.py").write_text("print('{\"passed\": true, \"summary\": \"ok\"}')\n", encoding="utf-8")
    path = tmp_path / "v.toml"
    path.write_text(f'template = 1\nobjective = "work"\n[verifier]\nargv = [{json.dumps(sys.executable)}, "check.py"]\ntimeout_seconds = 5\n',
                    encoding="utf-8")
    return path


def test_template_verifier_is_hashed_and_a_changed_file_needs_approval(env, tmp_path, capsys) -> None:
    template = _verifier_template(env, tmp_path)
    mission = _create(capsys, env, "--template", str(template))["mission"]
    assert list(mission["config"]["verifier"]["fingerprints"]) == ["check.py"]
    (env / "check.py").write_text("print('{\"passed\": true, \"summary\": \"rewritten\"}')\n", encoding="utf-8")
    with BridgeStore(store_path()) as store:
        store.set_mission_status(mission["id"], "RUNNING")
        task_id = store.list_tasks(mission["id"])[0]["id"]
        store.finish_operation(store.begin_operation(task_id, iteration=1, station="codex"), exit_code=0)
        outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")
    assert outcome["reason"] == "verifier-modified" and outcome["changed_files"] == ["check.py"]
    assert outcome["status"] == "VERIFYING" and outcome["passed"] is False


def test_verifier_command_form_files_and_cli_override(env, tmp_path, capsys) -> None:
    (env / "extra.txt").write_text("x\n", encoding="utf-8")
    path = tmp_path / "t.toml"
    path.write_text('template = 1\nobjective = "work"\n[verifier]\ncommand = "echo hi"\nfiles = ["extra.txt"]\n', encoding="utf-8")
    verifier = _create(capsys, env, "--template", str(path))["mission"]["config"]["verifier"]
    assert verifier["argv"] == ["echo", "hi"] and "extra.txt" in verifier["fingerprints"]
    overridden = _create(capsys, env, "--template", str(path), "--verifier", "true", "--new")["mission"]["config"]["verifier"]
    assert overridden["argv"] == ["true"] and "extra.txt" not in overridden.get("fingerprints", {})


def _config_without_runtime_noise(mission: dict[str, Any]) -> dict[str, Any]:
    return dict(mission["config"])


def _graph(mission_id: str) -> list[tuple[str, str, list[str], list[int]]]:
    return [(t["title"], t["prompt"], t["acceptance"], t["depends_on_ordinals"]) for t in _tasks(mission_id)]


def test_export_round_trips_config_and_task_graph(env, tmp_path, capsys) -> None:
    template = _verifier_template(env, tmp_path)
    text = template.read_text(encoding="utf-8") + (
        '[policy]\ntoken_spend = "low"\nteams = "on"\nmode = "headless"\ntrust_folder = true\n'
        '[[tasks]]\nid = "a"\ntitle = "A \\"quoted\\"\\\\"\nbody = "line1\\nline2 \\u0001 tab\\t"\nacceptance = ["x", "y"]\n'
        '[[tasks]]\nid = "b"\ntitle = "B"\ndepends_on = ["a"]\n')
    template.write_text(text, encoding="utf-8")
    first = _create(capsys, env, "--template", str(template), "--name", "Round trip")["mission"]
    exported = tmp_path / "out" / "exported.toml"
    code, out, err = _run(capsys, "mission", "export", first["id"], "--template", str(exported))
    assert code == 0, err
    assert str(env) not in exported.read_text(encoding="utf-8")
    second = _create(capsys, env, "--template", str(exported), "--new")["mission"]
    assert second["id"] != first["id"]
    assert second["name"] == first["name"] and second["objective"] == first["objective"]
    assert _config_without_runtime_noise(second) == _config_without_runtime_noise(first)
    assert _graph(second["id"]) == _graph(first["id"])
    assert _graph(first["id"])[-1][3] == [1]


def test_export_to_stdout_and_by_name(env, tmp_path, capsys) -> None:
    mission = _create(capsys, env, "--prompt", "Plain objective", "--providers", "codex")["mission"]
    code, out, _ = _run(capsys, "mission", "export", mission["id"], "--template")
    assert code == 0
    parsed = tomllib.loads(out)
    assert parsed["template"] == 1 and parsed["objective"] == "Plain objective" and len(parsed["tasks"]) == 1
    assert "frozen_tool_config" not in out
    code, _, _ = _run(capsys, "mission", "export", mission["id"], "--template", "plain")
    assert code == 0 and (tmp_path / "xdg" / "llm-tools" / "templates" / "plain.toml").is_file()
    assert _create(capsys, env, "--template", "plain", "--new")["mission"]["objective"] == "Plain objective"


def test_export_refuses_to_overwrite_without_force(env, tmp_path, capsys) -> None:
    mission = _create(capsys, env, "--prompt", "Objective", "--providers", "codex")["mission"]
    target = tmp_path / "keep.toml"
    target.write_text("precious\n", encoding="utf-8")
    code, out, err = _run(capsys, "mission", "export", mission["id"], "--template", str(target))
    assert code == 2 and "already exists" in out + err and "--force" in out + err
    assert target.read_text(encoding="utf-8") == "precious\n"
    code, _, _ = _run(capsys, "mission", "export", mission["id"], "--template", str(target), "--force")
    assert code == 0 and "Objective" in target.read_text(encoding="utf-8")


def test_toml_string_escaper_round_trips_awkward_text() -> None:
    for text in ['a "q" \\ b', "tab\t nl\n cr\r bell\x07 del\x7f", '"""', "unicode é 😀", "", "ends with quote\""]:
        for multiline in (False, True):
            assert tomllib.loads("v = " + bridge_templates.toml_string(text, multiline=multiline))["v"] == text
