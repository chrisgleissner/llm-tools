"""Folder-trust acknowledgement: explicit opt-in for Bridge missions, and only for a prompt that ends the output."""
from __future__ import annotations

import sys
from pathlib import Path

from llm_tools import bridge, bridge_format, common, ralph_robin
from llm_tools.bridge_runtime import ralph_argv

# The child prints its output, waits for input for a while and reports what it received.
CHILD = r"""
import os, select, sys, time
{body}
sys.stdout.flush()
ready, _, _ = select.select([0], [], [], {wait})
print('RECEIVED' if ready else 'NOTHING-RECEIVED', flush=True)
"""


def run(tmp_path: Path, body: str, *, auto_confirm: bool, wait: float = 2.0) -> tuple[int, str]:
    script = CHILD.format(body=body, wait=wait)
    return common.run_pty_capture(
        [sys.executable, "-c", script], tmp_path, 15, stream=False, auto_confirm=auto_confirm,
        idle_timeout=0, question_idle_timeout=0,
    )


def test_trust_phrase_followed_by_more_output_writes_no_byte(tmp_path: Path) -> None:
    body = (
        "print('Do you trust the files in this folder?', flush=True)\n"
        "time.sleep(0.1)\n"
        "for i in range(6): print('repository content line', i, flush=True); time.sleep(0.05)\n"
    )
    status, text = run(tmp_path, body, auto_confirm=True)
    assert "NOTHING-RECEIVED" in text and "RECEIVED\n" not in text.replace("NOTHING-RECEIVED", "")
    assert status == 0


def test_trust_phrase_in_earlier_output_is_not_a_prompt_even_when_output_goes_quiet(tmp_path: Path) -> None:
    body = "print('Confirm folder trust'); print('a'); print('b'); print('c'); print('d')\n"
    status, text = run(tmp_path, body, auto_confirm=True, wait=1.5)
    assert "NOTHING-RECEIVED" in text
    assert status == 0


def test_final_trust_prompt_receives_enter_when_confirmation_is_on(tmp_path: Path) -> None:
    status, text = run(tmp_path, "print('Do you trust the files in this folder?', flush=True)\n", auto_confirm=True)
    assert "NOTHING-RECEIVED" not in text and "RECEIVED" in text
    assert status == 0


def test_final_trust_prompt_without_confirmation_aborts_with_a_clear_reason(tmp_path: Path) -> None:
    body = "print('Do you trust the files in this folder?'); print('Enter to confirm \\u00b7 Esc to cancel', flush=True)\n"
    status, text = run(tmp_path, body, auto_confirm=False)
    assert status == common.AUTONOMY_ABORT_STATUS
    assert "RECEIVED" not in text
    assert "--trust-folder" in text


def test_blocking_phrase_in_earlier_output_does_not_abort(tmp_path: Path) -> None:
    body = (
        "print('Run out of tokens, not momentum', flush=True)\n"
        + "".join(f"print('later line {i}', flush=True)\n" for i in range(12))
    )
    status, text = run(tmp_path, body, auto_confirm=True, wait=0.3)
    assert status == 0 and "NOTHING-RECEIVED" in text


def test_controller_arguments_confirm_only_for_a_trust_folder_mission(tmp_path: Path) -> None:
    mission = {"objective": "x", "cwd": str(tmp_path), "config": {}}
    default = ralph_argv(mission, tmp_path, providers="codex", routes="")
    assert "--no-auto-confirm" in default and "--auto-confirm" not in default
    mission["config"] = {"trust_folder": True}
    opted_in = ralph_argv(mission, tmp_path, providers="codex", routes="")
    assert "--auto-confirm" in opted_in and "--no-auto-confirm" not in opted_in
    assert ralph_robin.parse_args(opted_in).auto_confirm is True
    assert ralph_robin.parse_args(default).auto_confirm is False


def test_create_trust_folder_is_stored_in_mission_config_and_shown_in_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    parser = bridge._build_parser()
    for flag, expected in (("--trust-folder", True), (None, False)):
        extra = [flag] if flag else []
        args = parser.parse_args(["mission", "create", "--prompt", "x", "--cwd", str(tmp_path), *extra])
        assert args.trust_folder is expected
    snapshot = {"mission": {"id": "a" * 32, "name": "n", "status": "CREATED", "cwd": str(tmp_path), "config": {"trust_folder": True}}}
    assert "background, trusts folder" in bridge_format.format_status(snapshot).splitlines()[0]
    snapshot["mission"]["config"] = {}
    assert "trusts folder" not in bridge_format.format_status(snapshot)


def test_legacy_create_stores_trust_folder_in_the_mission_config(tmp_path: Path, monkeypatch, capsys) -> None:
    import json

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    folder = tmp_path / "work"
    folder.mkdir()
    for flag, expected in (("--trust-folder", True), (None, False)):
        folder = tmp_path / f"w{expected}"
        folder.mkdir()
        argv = ["--prompt", "x", "--cwd", str(folder), "--no-start", "--json", "--include-config"] + ([flag] if flag else [])
        assert bridge.main(argv) == 0
        out = json.loads(capsys.readouterr().out)
        assert bool(out["mission"]["config"].get("trust_folder")) is expected
