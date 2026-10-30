"""``LLM_SCHEDULER_USAGE_JSON_FILE``: injected usage that is read again on every call."""

from __future__ import annotations

import json
from pathlib import Path

from llm_tools import common


def write(path: Path, remaining: int) -> None:
    path.write_text(json.dumps({"claude": {"available": True, "five_hour": {"remaining": remaining}}}), encoding="utf-8")


def test_file_is_read_again_on_every_call(tmp_path: Path) -> None:
    usage = tmp_path / "usage.json"
    env = {"LLM_SCHEDULER_USAGE_JSON_FILE": str(usage)}
    write(usage, 40)
    assert common.usage_snapshot_for_provider("claude", env)["five_hour"]["remaining"] == 40
    write(usage, 3)
    assert common.usage_snapshot_for_provider("claude", env)["five_hour"]["remaining"] == 3


def test_file_wins_over_the_inline_value(tmp_path: Path) -> None:
    usage = tmp_path / "usage.json"
    write(usage, 7)
    inline = json.dumps({"claude": {"available": True, "five_hour": {"remaining": 90}}})
    env = {"LLM_SCHEDULER_USAGE_JSON": inline, "LLM_SCHEDULER_USAGE_JSON_FILE": str(usage)}
    assert common.usage_snapshot_for_provider("claude", env)["five_hour"]["remaining"] == 7


def test_unreadable_or_empty_file_falls_back_to_the_inline_value(tmp_path: Path) -> None:
    inline = json.dumps({"claude": {"available": True, "five_hour": {"remaining": 90}}})
    missing = {"LLM_SCHEDULER_USAGE_JSON": inline, "LLM_SCHEDULER_USAGE_JSON_FILE": str(tmp_path / "absent.json")}
    assert common.usage_snapshot_for_provider("claude", missing)["five_hour"]["remaining"] == 90
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert common.usage_snapshot_for_provider("claude", {**missing, "LLM_SCHEDULER_USAGE_JSON_FILE": str(empty)})["five_hour"]["remaining"] == 90


def test_provider_missing_from_the_file_reports_no_data(tmp_path: Path) -> None:
    usage = tmp_path / "usage.json"
    write(usage, 50)
    snapshot = common.usage_snapshot_for_provider("codex", {"LLM_SCHEDULER_USAGE_JSON_FILE": str(usage)})
    assert snapshot["available"] is False and snapshot["reason"] == "no-data"
