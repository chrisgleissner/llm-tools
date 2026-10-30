"""Mission names reach every operator-facing surface without escape or bidi characters."""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from llm_tools import bridge_launcher, bridge_switcher
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import detach_notice

HOSTILE = "evil\x1b]52;c;ZXZpbA==\x07\x1b]0;pwned\x07\x1b[2J ‮elbat⁦ x​z"
FORBIDDEN = {"Cc", "Cs", "Cf"}


def dirty(text: str) -> list[str]:
    return [repr(c) for c in text if unicodedata.category(c) in FORBIDDEN and c != "\n"]


@pytest.fixture()
def mission_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.delenv("LLM_BRIDGE_MANAGED", raising=False)
    with BridgeStore(state / "bridge.sqlite3") as store:
        created = store.create_mission(HOSTILE, "Objective", cwd=tmp_path)["id"]
        store.add_task(created, HOSTILE)
    return str(created)


@pytest.mark.parametrize("argv", [["missions"], ["mission", "list"], ["status"], ["task", "list"]])
def test_cli_output_has_no_control_or_format_characters(mission_id: str, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    command = argv if argv[0] == "missions" or argv == ["mission", "list"] else argv + ["--mission", mission_id]
    assert main(command) == 0
    out = capsys.readouterr().out
    assert "evil" in out
    assert not dirty(out), dirty(out)


def test_detach_notice_filters_the_mission_name(mission_id: str) -> None:
    notice = detach_notice(f"mission-{mission_id}")
    assert "evil" in notice, notice
    assert not dirty(notice), dirty(notice)


def test_home_and_picker_cells_have_no_control_or_format_characters() -> None:
    mission = {"id": "a" * 32, "name": HOSTILE, "status": "RUNNING", "cwd": "/work", "objective": HOSTILE}
    assert not dirty(bridge_launcher._flat(HOSTILE))
    assert not dirty(bridge_launcher.truncate(HOSTILE, 60))
    assert not dirty(bridge_launcher.mission_detail(mission, 80))
    assert not dirty(bridge_launcher.objective_summary(HOSTILE, 60))
    assert not dirty(bridge_switcher._flat(HOSTILE))
    assert not dirty(bridge_switcher._flat_keep(HOSTILE))
    assert not dirty(bridge_switcher.clip(HOSTILE, 60))
    assert not dirty(bridge_switcher.mission_label(HOSTILE, "/work"))
