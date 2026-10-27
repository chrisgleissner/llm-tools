"""Hermetic oracles for the opt-in no-prompt native PTY probe."""

from __future__ import annotations

import shutil
import sys

import pytest

from scripts.bridge_native_input_probe import _DirectPTY, help_visible, marker_rows, mouse_effect, wheel_effect


def test_help_oracle_requires_rendered_local_help_not_just_echo() -> None:
    assert not help_visible("typed /help")
    assert help_visible("/help\nCommands\n/model changes models")
    assert not help_visible("/help\nAuthentication required")


def test_mouse_and_wheel_oracles_do_not_overstate_unobserved_effects() -> None:
    assert mouse_effect("Select Model\nmodel (current)", "Select Model\nSelect Reasoning Effort") == "effort-picker"
    assert mouse_effect("Select Model\nmodel (current)", "Select Model\nmodel") == "picker-dismissed-or-selection-made"
    assert mouse_effect("Select Model\nmodel (current)", "Select Model\nmodel (current)") == "no-visible-effect"
    assert mouse_effect("Select Model", "different menu") == "screen-changed"
    before = "transcript\nNATIVE_BRIDGE_OK\nprompt"
    after = "NATIVE_BRIDGE_OK\ntranscript\nprompt"
    assert marker_rows(before) == (1,) and marker_rows(after) == (0,)
    assert wheel_effect(before, after, pane_mode_before=0, pane_mode_after=0).startswith("PASS")
    assert wheel_effect(before, "no marker", pane_mode_before=0, pane_mode_after=0).startswith("NOT VERIFIED")
    assert wheel_effect(before, after, pane_mode_before=1, pane_mode_after=1).startswith("NOT VERIFIED")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_direct_cli_comparison_harness_delivers_keystrokes_to_a_native_terminal(tmp_path) -> None:
    process = _DirectPTY(["/bin/cat"], tmp_path, timeout=4, socket_path=tmp_path / "direct.sock")
    try:
        process.send(b"LOCAL_INPUT_ORACLE\r")
        assert "LOCAL_INPUT_ORACLE" in process.screen(0.2)
    finally:
        process.close()
