"""Provider transcript shapes observed through metadata keys, without messages."""

from __future__ import annotations

import json
from pathlib import Path
import re

from llm_tools.bridge_sessions import provider_session_metadata


SESSION_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def test_exact_claude_native_transcript_confirms_planned_session(tmp_path: Path, monkeypatch) -> None:
    """Native JSONL has top-level sessionId/cwd, not stream-json system.init."""
    cwd = tmp_path / "work_with.dots"
    cwd.mkdir()
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    project = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
    project.mkdir(parents=True)
    transcript = project / f"{SESSION_ID}.jsonl"
    transcript.write_text("\n".join(json.dumps(record) for record in (
        {"type": "queue-operation", "operation": "enqueue", "sessionId": SESSION_ID},
        {"type": "user", "sessionId": SESSION_ID, "cwd": str(cwd.resolve()),
         "isSidechain": False, "parentUuid": None},
        {"type": "assistant", "sessionId": SESSION_ID, "cwd": str(cwd.resolve()),
         "isSidechain": False},
    )) + "\n", encoding="utf-8")

    record = provider_session_metadata("claude", SESSION_ID, cwd=cwd)

    assert record is not None, "real native transcript metadata must confirm the exact planned identity"
    assert record.identity.session_id == SESSION_ID
    assert record.identity.cwd == str(cwd.resolve())
    assert record.path == transcript

    # An ID-addressed filename alone must never bind unrelated transcript data.
    transcript.write_text(json.dumps({
        "type": "user", "sessionId": OTHER_ID, "cwd": str(cwd.resolve()),
    }) + "\n", encoding="utf-8")
    assert provider_session_metadata("claude", SESSION_ID, cwd=cwd) is None
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    transcript.write_text(json.dumps({
        "type": "user", "sessionId": SESSION_ID, "cwd": str(other_cwd.resolve()),
    }) + "\n", encoding="utf-8")
    assert provider_session_metadata("claude", SESSION_ID, cwd=cwd) is None
