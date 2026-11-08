"""The three mission modes (Native, Headless, Background) are named the same everywhere, and create rejects --native with --headless."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge_launcher as launcher
from llm_tools.bridge import _build_parser, main
from llm_tools.bridge_help import TOPICS, topic_text
from llm_tools.bridge_launcher import Launcher
from llm_tools.bridge_store import BridgeStore

from .test_bridge_launcher_home import Grid


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path))
    return path


@pytest.mark.parametrize("form", ["subcommand", "legacy"])
def test_create_rejects_native_with_headless_and_saves_nothing(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], form: str) -> None:
    base = ["--prompt", "work", "--cwd", str(tmp_path), "--providers", "codex", "--native", "--headless"]
    argv = ["mission", "create", *base] if form == "subcommand" else base
    assert main(argv) != 0
    err = capsys.readouterr().err
    assert "--headless and --native cannot be combined" in err
    assert "--mode native, background or headless" in err
    assert not state.exists() or not (state / "bridge.sqlite3").exists() or BridgeStore(state / "bridge.sqlite3").list_missions() == []


@pytest.mark.parametrize("flags", [["--native"], ["--headless"], []])
def test_create_accepts_each_single_mode(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]) -> None:
    argv = ["mission", "create", "--prompt", "work", "--cwd", str(tmp_path), "--providers", "codex", *flags, "--json"]
    assert main(argv) == 0
    config = json.loads(capsys.readouterr().out)["mission"]["config"]
    assert (bool(config["native_tui"]), bool(config["headless"])) == ("--native" in flags, "--headless" in flags)


def test_status_and_snapshot_call_the_default_mode_background(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mission", "create", "--prompt", "work", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    assert main(["status", "--mission", mission_id]) == 0
    assert "(background)" in capsys.readouterr().out
    assert main(["status", "--mission", mission_id, "--json"]) == 0
    assert '"configured_mode": "background"' in capsys.readouterr().out


def help_texts() -> str:
    parser = _build_parser()
    out = [parser.format_help()]
    out += [topic_text(topic) for topic in TOPICS]
    for path in ("mission create", "start"):
        out.append(topic_text(path))
    return "\n".join(out)


def test_help_uses_native_headless_background_and_never_autonomous() -> None:
    text = help_texts()
    assert "autonomous" not in text.lower()
    create = " ".join(topic_text("mission create").split())
    assert "Native: the provider's own interactive terminal" in create and "Headless: no terminal" in create and "Background: the provider runs under a controller" in create
    start = " ".join(topic_text("start").split())
    assert "Native (the provider's own terminal" in start and "Background (a station shows the activity log)" in start and "Headless (no station)" in start
    quickstart = " ".join(topic_text("quickstart").split())
    assert "--mode native|background|headless" in quickstart and "Background is the default for the command line" in quickstart
    assert "Background (--mode" in " ".join(topic_text("providers").split())


def test_home_form_names_its_three_choices_native_background_and_headless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import curses
    from llm_tools import bridge_tui

    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)
    form = Launcher(str(tmp_path), creating=True, installed=("codex",), providers="codex", objective="x", focus=3)
    screens = []
    for _ in range(3):
        grid = Grid(size=(34, 120))
        launcher._draw(grid, form)
        screens.append(grid.text())
        form.key(" ")
    assert "Mode: Native — provider terminal in a station" in screens[0] and "Start and open provider" in screens[0]
    assert "Mode: Background — activity log in a station" in screens[1] and "Start and open activity log" in screens[1]
    assert "Mode: Headless — provider runs without a terminal" in screens[2] and "Start headless work" in screens[2]
    assert not any("utonomous" in screen for screen in screens)
    hint = launcher.field_hint(form)
    assert "Native shows the provider terminal" in hint and "Background" in hint and "Headless" in hint


def test_operations_doc_uses_the_same_three_names() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "llm-bridge" / "operations.md").read_text(encoding="utf-8")
    table = doc[doc.index("| Mode | `--mode` |"):]
    rows = [line for line in table.splitlines()[2:5]]
    assert [re.match(r"\| (\w+) \|", row).group(1) for row in rows] == ["Native", "Headless", "Background"]
    assert "(CLI default)" in rows[2]
    # The only remaining mention of the old name says that it is the old name.
    old = [line for line in doc.splitlines() if "utonomous" in line]
    assert len(old) <= 2 and all("call" in line or "Older" in line or "older" in line for line in old), old
    assert "`--native` with `--headless`,\nis refused" in doc
