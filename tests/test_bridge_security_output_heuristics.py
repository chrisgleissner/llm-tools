"""Repository content echoed by a provider must not trip the blocking-prompt heuristics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from llm_tools import common, scheduler

TRAP = "Run out of tokens, not momentum"


def tool_result_event(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def assistant_text_event(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def stream_json_run(tmp_path: Path, events: list[dict], *, trailing_sleep: float = 0.0) -> tuple[int, str]:
    script = tmp_path / "emit.py"
    lines = "".join(json.dumps(event) + "\n" for event in events)
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write({lines!r})\nsys.stdout.flush()\n"
        f"time.sleep({trailing_sleep})\n",
        encoding="utf-8",
    )
    cfg = scheduler.SchedulerConfig()
    cfg.cwd = str(tmp_path)
    out, status = tmp_path / "out.txt", tmp_path / "status.txt"
    code = scheduler.run_fresh_claude_stream_json(cfg, [sys.executable, str(script)], out, status)
    return code, out.read_text(encoding="utf-8")


def test_tool_result_echoing_a_file_does_not_abort_a_stream_json_run(tmp_path: Path) -> None:
    code, text = stream_json_run(tmp_path, [tool_result_event(f"README.md says: {TRAP}"), assistant_text_event("done")])
    assert code == 0, text
    assert TRAP in text and "autonomous abort" not in text


def test_tool_result_as_the_last_event_does_not_abort_either(tmp_path: Path) -> None:
    code, text = stream_json_run(tmp_path, [tool_result_event(f"{TRAP}\nupgrade to max\nEnter to confirm")])
    assert code == 0, text


def test_assistant_text_prompt_at_the_end_still_aborts(tmp_path: Path) -> None:
    events = [assistant_text_event("You've hit your monthly spend limit\nWhat do you want to do?"), ]
    code, text = stream_json_run(tmp_path, events, trailing_sleep=3)
    assert code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in text


def test_scan_ignores_blocking_phrase_that_is_not_in_the_last_lines() -> None:
    guard = scheduler.ProgressGuard()
    earlier = f"{TRAP}\n" + "\n".join(f"line {i}" for i in range(20))
    assert guard.note_output(earlier) is False
    assert guard.note_output("line\n" * 3 + "What do you want to do?") is True
