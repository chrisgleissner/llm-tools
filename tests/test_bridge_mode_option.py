"""CLIUX-008: one --mode option names the three run modes; CLIUX-009: help states what the code does."""

from __future__ import annotations

import curses
import json
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_launcher as launcher, bridge_runtime, bridge_tui
from llm_tools.bridge import _build_parser, main
from llm_tools.bridge_help import TOPICS, topic_text
from llm_tools.bridge_store import BridgeStore

from .test_bridge_launcher_home import Grid, prepare, run_with, set_status, start_keys
from .test_bridge_tui_experience import full_snapshot, make_state


Launcher = launcher.Launcher


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    return path


def _create(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> dict[str, Any]:
    argv = ["mission", "create", "--prompt", "work", "--cwd", str(tmp_path), "--providers", "codex", *extra, "--json"]
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)["mission"]


# --- CLIUX-008 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,headless,native", [("native", False, True), ("background", False, False), ("headless", True, False)])
def test_create_mode_option_sets_the_flags_and_json_mode(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, headless: bool, native: bool) -> None:
    mission = _create(tmp_path, capsys, "--mode", mode)
    config = mission["config"]
    assert (config["headless"], config["native_tui"], config["mode"]) == (headless, native, mode)


@pytest.mark.parametrize("flag,mode", [("--native", "native"), ("--headless", "headless"), (None, "background")])
def test_json_mode_follows_the_legacy_flags_and_the_default(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str | None, mode: str) -> None:
    mission = _create(tmp_path, capsys, *([flag] if flag else []))
    assert mission["config"]["mode"] == mode
    assert main(["status", "--mission", mission["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mission"]["config"]["mode"] == mode


@pytest.mark.parametrize("extra", [
    ["--mode", "native", "--headless"], ["--mode", "background", "--native"], ["--mode", "headless", "--native"],
    ["--native", "--headless"],
])
def test_conflicting_mode_options_are_rejected_briefly_and_save_nothing(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]) -> None:
    assert main(["mission", "create", "--prompt", "work", "--cwd", str(tmp_path), "--providers", "codex", *extra]) == 2
    err = capsys.readouterr().err
    assert "cannot be combined" in err and len(err.strip().splitlines()) == 1
    assert main(["mission", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["missions"] == []


def test_mode_may_repeat_a_matching_legacy_flag(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _create(tmp_path, capsys, "--mode", "native", "--native")["config"]["mode"] == "native"


def test_mode_option_is_documented_and_the_old_flags_are_hidden() -> None:
    for path in ("mission create", "start"):
        text = topic_text(path)
        assert "--mode {native,background,headless}" in " ".join(text.split())
        assert "--native" not in text and "--headless" not in text
        assert "Native" in text and "Background" in text and "Headless" in text
    create = " ".join(topic_text("mission create").split())
    assert "Background is the default" in create and "needs no terminal" in create
    assert "remain compatible" not in "\n".join(topic_text(topic) for topic in TOPICS)


def test_start_mode_background_leaves_a_native_mission_in_background(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mission = _create(tmp_path, capsys, "--mode", "native")
    monkeypatch.setattr(bridge_runtime.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    launched: dict[str, list[str]] = {}

    class FakeController:
        pid = 43210
        returncode = None

        def poll(self) -> None:
            return None

    def fake_popen(argv: list[str], **_kwargs: Any) -> FakeController:
        launched["argv"] = list(argv)
        token = argv[argv.index("--startup-token") + 1]
        bridge_runtime._ready_file(bridge_runtime._runtime_directory(mission["id"])).write_text(
            json.dumps({"pid": 43210, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return FakeController()

    monkeypatch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)
    assert main(["start", "--mission", mission["id"], "--mode", "background"]) == 0
    command = launched["argv"]
    assert "--native" not in command and "--headless" not in command and "--background" in command


def test_controller_applies_an_explicit_background_over_a_saved_native_mode(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with bridge.open_store() as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path, config={"native_tui": True, "headless": False})
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_set(self: BridgeStore, mission_id: str, values: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        seen.update(values)
        raise Stop

    monkeypatch.setattr(BridgeStore, "set_mission_config_values", fake_set)
    monkeypatch.setattr(bridge_runtime.shutil, "which", lambda name: None)
    with pytest.raises(Stop):
        bridge_runtime.run_controller(mission["id"], background=True)
    assert seen == {"headless": False, "native_tui": False}


def test_start_rejects_conflicting_mode_options(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mission = _create(tmp_path, capsys)
    assert main(["start", "--mission", mission["id"], "--mode", "native", "--headless"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("mode,expected", [("native", "Native"), ("background", "Background"), ("headless", "Headless")])
def test_home_form_reaches_all_three_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str) -> None:
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)
    form = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex", objective="x", focus=3)
    seen = []
    for _ in range(3):
        grid = Grid(size=(34, 120))
        launcher._draw(grid, form)
        seen.append((form.mode, grid.text()))
        form.key(" ")
    modes = [item[0] for item in seen]
    assert modes == ["native", "background", "headless"] and form.mode == "native"
    screen = dict(seen)[mode]
    assert f"Mode: {expected}" in screen
    form.key(curses.KEY_LEFT)
    assert form.mode == "headless"


def test_home_form_creates_and_starts_with_the_selected_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = ["n", *"Go", "\t", "\t", "\t", " ", "\t", "\t", "\n"]  # Space once on Mode selects Background
    seen: list[list[str]] = []

    def start(args: list[str]) -> int:
        seen.append(args)
        set_status(args[2], "RUNNING")
        return 0

    status, calls, _ = run_with(start, tmp_path, monkeypatch, keys)
    assert status == 0
    create = next(call for call in calls if call[0] == "mission")
    assert create[create.index("--mode") + 1] == "background"
    assert seen[0][3:] == ["--mode", "background"]
    with bridge.open_store() as store:
        assert store.list_missions()[0]["config"]["mode"] == "background"


# --- CLIUX-009 ---------------------------------------------------------------------------------

def _help(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit):
        main([*argv, "--help"])
    return " ".join(capsys.readouterr().out.split())


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))


def test_help_default_rotation_is_the_rotation_a_new_mission_gets(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    providers, routes = bridge.creation_rotation(None, None)
    assert routes == ""
    text = _help(capsys, "mission", "create")
    assert f"(default: {providers})" in text and f"providers {providers}" in text
    assert "(default:" not in text.split("--routes LIST comma-separated")[1].split("--acceptance")[0]
    assert providers in " ".join(topic_text("quickstart").split())
    assert "built-in claude,codex,opencode" not in topic_text("quickstart") or providers == bridge.BUILTIN_PROVIDERS


def test_help_default_rotation_drops_providers_the_config_disables(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path, monkeypatch, '[usage]\nproviders = ["claude", "codex"]\n')
    providers, _routes = bridge.creation_rotation(None, None)
    assert providers == "claude,codex"
    text = _help(capsys, "mission", "create")
    assert "opencode" not in text and "(default: claude,codex)" in text
    assert "opencode" not in topic_text("quickstart").split("Three modes")[1].split("New missions")[0]


def test_help_routes_default_names_routes_not_providers(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path, monkeypatch, '[ralph]\nroutes = ["codex", "claude"]\n')
    providers, routes = bridge.creation_rotation(None, None)
    assert routes == "codex,claude"
    text = _help(capsys, "mission", "create")
    routes_help = text.split("--routes LIST comma-separated")[1].split("--acceptance")[0]
    providers_help = text.split("--providers LIST comma-separated")[1].split("--routes LIST comma-separated")[0]
    assert "(default: codex,claude)" in routes_help
    assert "(default:" not in providers_help  # the configured routes apply, so providers have no default


def test_task_add_reopens_a_completed_mission_the_same_way_with_or_without_mission(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from .test_bridge_first_run_fixes import _completed_mission

    mid, _ = _completed_mission(tmp_path, state, capsys, name="done")
    assert main(["task", "add", "--mission", mid, "--prompt", "one"]) == 0
    with_flag = capsys.readouterr().out
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "STOPPED"
    mid2, _ = _completed_mission(tmp_path, state, capsys, name="done2")
    monkeypatch.chdir(tmp_path / "done2")
    assert main(["task", "add", "--prompt", "two"]) == 0
    without_flag = capsys.readouterr().out
    short = mid2[:8]
    assert f"Mission {short} reopened" in without_flag and "start it with: llm-bridge start" in without_flag
    assert f"Mission {mid[:8]} reopened" in with_flag
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid2)["status"] == "STOPPED"


def test_help_states_says_a_completed_mission_is_reopened_by_a_new_task() -> None:
    text = " ".join(topic_text("states").split())
    assert "cannot accept another task" not in text
    assert "reopens" in text and "task add" in text and "start" in text and "Cancelled" in text


def test_help_keys_names_all_nine_views_and_the_typed_names() -> None:
    text = " ".join(topic_text("keys").split())
    for name in ("Overview", "Tasks", "Providers", "Activity", "Sessions", "Health", "Actions", "Usage", "Agents"):
        assert name in text.split("F1-F9 select")[1].split("Line 1")[0]
    typed = text.split("Typing a view name (")[1].split(")")[0]
    assert set(typed.replace(" ", "").split(",")) == {name.lower() for name in bridge_tui.VIEWS}


@pytest.mark.parametrize("word,view", [("tasks", "Tasks"), ("/tasks", "Tasks"), ("agents", "Agents"), ("orders", "Tasks")])
def test_typed_view_names_open_the_view(word: str, view: str) -> None:
    state = make_state(full_snapshot(), view="Overview")
    state.command = word
    state.command_cursor = len(word)
    state.input_focused = True
    state.handle_key("\n")
    assert state.view == view


def test_a_command_refused_before_dispatch_clears_the_line_and_stays_in_history() -> None:
    state = make_state(full_snapshot(), view="Overview")
    state.command = "/reject 1"
    state.command_cursor = len(state.command)
    state.input_focused = True
    state.handle_key("\n")
    assert state.command == "" and state.command_cursor == 0
    assert state.output[-1].startswith("error: usage: reject") and "↑ recalls the command" in state.output[-1]
    state.handle_key("\x10")  # Ctrl-P
    assert state.command == "/reject 1"
