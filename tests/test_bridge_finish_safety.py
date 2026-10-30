"""/finish reads the provider pane before typing and never answers a provider dialog."""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_tools import bridge_finish
from llm_tools.bridge_finish import ASKING_MESSAGE, FinishError, composer_is_idle, exit_popup_open, finish_mission
from tests.test_bridge_switcher import HANDLE, FakeBackend, running_native, state  # noqa: F401  (state is a fixture)

CLAUDE_PERMISSION = """\
 Bash command

   rm -rf build
   Remove the build directory

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /work
   3. No, and tell Claude what to do differently (esc)
"""
CODEX_APPROVAL = """\
  Would you like to run the following command?

  $ git push origin main

› 1. Yes, proceed (y)
  2. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""
COPILOT_TRUST = """\
 Do you trust the files in this folder?

 /home/demo/work

 > 1. Yes, proceed
   2. No, exit (Esc)
"""
YES_NO = "Overwrite config.toml? (y/n) "
ALLOW = "Allow Claude to edit src/app.py?\n  Yes  No\n"

IDLE_SCREENS = {
    "codex": "work output\n\n› Ask Codex to do something\n\n  gpt-5 · 100% context left\n",
    "claude": "work output\n\n╭──────────────────╮\n│ > \n╰──────────────────╯\n  ? for shortcuts\n",
    "claude-new": "work output\n─────────────\n❯ \n─────────────\n  ⏵⏵ bypass permissions on\n",
    "copilot": "work output\n\n> Type @ to mention files\n",
}


@pytest.mark.parametrize("screen", [CLAUDE_PERMISSION, CODEX_APPROVAL, COPILOT_TRUST, YES_NO, ALLOW])
def test_finish_refuses_when_provider_shows_a_dialog_and_types_nothing(state: Path, tmp_path: Path, screen: str) -> None:
    store, mission_id, task_id, _op = running_native(state, tmp_path, "codex")
    backend = FakeBackend(screen=screen)
    with pytest.raises(FinishError) as caught:
        finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None)
    assert str(caught.value) == "Provider is asking something — switch to it with Ctrl-] then P" == ASKING_MESSAGE
    assert backend.sent == [], "no key may be sent into a provider dialog"
    assert store.get_task(task_id)["status"] == "RUNNING"


@pytest.mark.parametrize("name", sorted(IDLE_SCREENS))
def test_idle_composers_are_recognised(name: str) -> None:
    assert composer_is_idle(IDLE_SCREENS[name])


@pytest.mark.parametrize("screen", ["", "   \n\n", "still working...\n", "compiling\n  /quit  not a prompt"])
def test_screen_without_a_composer_is_not_idle(screen: str) -> None:
    assert not composer_is_idle(screen)


def test_unreadable_pane_is_refused(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, _op = running_native(state, tmp_path, "codex")
    backend = FakeBackend()

    def broken(handle, *, pane):
        raise RuntimeError("capture failed")

    backend.capture = broken  # type: ignore[method-assign]
    with pytest.raises(FinishError, match="asking something"):
        finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None)
    assert backend.sent == []


def test_second_enter_only_while_the_exit_popup_is_open(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, operation = running_native(state, tmp_path, "codex")
    backend = FakeBackend(exit_after_enters=None, popup=False)
    backend.on_exit = lambda: None
    ticks = iter(range(1000))
    with pytest.raises(FinishError, match="did not exit"):
        finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, timeout=3, sleep=lambda _s: None, clock=lambda: float(next(ticks)))
    assert backend.sent == ["/quit", "Enter"], "no popup on screen, so the second Enter would be typed blind"

    store2, mission2, _t, op2 = running_native(state, tmp_path, "claude")
    backend2 = FakeBackend(exit_after_enters=2)
    backend2.on_exit = lambda: store2.finish_operation(op2, exit_code=0)
    finish_mission(mission2, backend=backend2, handle=HANDLE, store=store2, sleep=lambda _s: None)
    assert backend2.sent == ["/exit", "Enter", "Enter"]


def test_popup_must_list_the_exit_command_first() -> None:
    assert exit_popup_open("› /quit\n  /quit   Exit Codex\n  /quote  Other\n", "/quit")
    assert not exit_popup_open("› /quit\n  /quote  Other\n  /quit   Exit Codex\n", "/quit")
    assert not exit_popup_open("› /quit\n", "/quit")  # only the typed command, no popup
    assert not exit_popup_open("Do you want to proceed?\n❯ 1. Yes\n", "/quit")


def test_skip_check_override_types_without_reading_the_pane(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, operation = running_native(state, tmp_path, "codex")
    backend = FakeBackend(exit_after_enters=1, screen="custom ui with no prompt glyph")
    backend.on_exit = lambda: store.finish_operation(operation, exit_code=0)
    finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None,
                   env={bridge_finish.SKIP_CHECK_ENV: "1"})
    assert backend.sent[:2] == ["/quit", "Enter"]
