"""/finish must submit the exit command, also to a provider that treats a quick Enter as pasted text (DOG-027)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from llm_tools import bridge_finish
from llm_tools.bridge_finish import composer_holds_command, finish_mission, second_enter_needed
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import wait_for
from tests.test_bridge_switcher import running_native, state  # noqa: F401  (state is a fixture)

# Like Codex, an Enter that arrives within a short burst after typed characters inserts a newline
# instead of submitting. Only a deliberate Enter, after a pause, runs "/quit".
FAKE_PASTE_BURST_PROVIDER = '''\
import os, sys, time, tty
tty.setraw(0)
BURST = 0.15
buffer, last_char = "", 0.0
def draw():
    text = "\\x1b[2J\\x1b[H" + "work output\\r\\n\\r\\n\\u203a " + buffer.replace("\\n", "\\r\\n  ") + "\\r\\n\\r\\n  gpt-5 \\u00b7 100% context left"
    os.write(1, text.encode())
draw()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    for ch in data.decode(errors="replace"):
        if ch == "\\r":
            if time.monotonic() - last_char < BURST:
                buffer += "\\n"
            elif buffer.strip() == "/quit":
                sys.exit(0)
            else:
                buffer = ""
        else:
            buffer += ch
            last_char = time.monotonic()
    draw()
'''


def test_composer_holds_command_only_when_typed_and_unsubmitted() -> None:
    assert composer_holds_command("out\n› /quit\n  gpt-5\n", "/quit")
    assert composer_holds_command("out\n│ > /exit │\n", "/exit")
    assert not composer_holds_command("out\n› \n  gpt-5\n", "/quit")
    assert not composer_holds_command("out\n› /quit now please\n", "/quit")
    assert not composer_holds_command("/quit\n", "/quit")


def test_second_enter_rules() -> None:
    # popup open, exit command first: Enter accepts it
    assert second_enter_needed("› /quit\n  /quit   Exit Codex\n  /quote  Other\n", "/quit")
    # popup open with a different first entry while the composer shows the command: no Enter
    assert not second_enter_needed("› /quit\n  /quote  Other\n  /quit   Exit Codex\n", "/quit")
    # no popup, the current input line still holds the command: Enter
    assert second_enter_needed("work\n› /quit\n  gpt-5\n", "/quit")
    # Claude echoes the earlier input above an empty composer: no Enter
    assert not second_enter_needed("> /exit\n\nbye soon\n\n╭──────╮\n│ > │\n╰──────╯\n", "/exit")
    assert not second_enter_needed("work\n› \n  gpt-5\n", "/quit")


@pytest.mark.tmux
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_finish_submits_exit_command_to_provider_that_treats_quick_enter_as_paste(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(bridge_finish, "EXIT_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(bridge_finish, "TASK_SETTLE_SECONDS", 0.0)
    fake = tmp_path / "codex_fake.py"
    fake.write_text(FAKE_PASTE_BURST_PROVIDER, encoding="utf-8")
    store, mission_id, _task, _operation = running_native(state, tmp_path, "codex")
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, str(fake)],
        bridge_argv=["sleep", "60"],
        cwd=tmp_path,
        socket_path=tmp_path / "t.sock",
        session_name="finish-paste",
    )
    try:
        wait_for(lambda: "gpt-5" in backend.capture(handle, pane="provider"), 5, 0.05, "fake provider drew its prompt")
        result = finish_mission(mission_id, backend=backend, handle=handle, store=store)
        assert result.command == "/quit"
        assert not backend.pane_is_alive(handle, pane="provider")
    finally:
        backend.stop(handle)
        store.close()
