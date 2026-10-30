"""Untrusted text (mission names, task titles, proposals) must not carry terminal control or bidi characters."""
from __future__ import annotations

import unicodedata

import pytest

from llm_tools import bridge_format as fmt
from llm_tools import bridge_tui

HOSTILE = "ok\x1b[2J\x1b]0;pwned\x07 ‮elbat⁦ x​z\ud800"
FORBIDDEN = {"Cc", "Cs", "Cf"}


def clean(text: str) -> bool:
    return not any(unicodedata.category(char) in FORBIDDEN and char != "\n" for char in text)


def test_clip_removes_escape_and_bidi_characters() -> None:
    clipped = fmt._clip(HOSTILE, 200)
    assert clean(clipped)
    assert "\x1b" not in clipped and "‮" not in clipped
    assert clipped.startswith("ok") and "elbat" in clipped


def test_cli_listings_strip_hostile_names_and_titles() -> None:
    mission = {"id": "a" * 32, "status": "RUNNING", "name": HOSTILE, "cwd": "/work", "updated_at": None}
    task = {"id": 1, "ordinal": 1, "status": "QUEUED", "priority": 0, "title": HOSTILE, "prompt": HOSTILE + "\n" + HOSTILE}
    proposal = {"id": 2, "ordinal": 2, "proposal_status": "PENDING", "title": HOSTILE}
    for rendered in (
        fmt.format_mission_list([mission]),
        fmt.format_task_list([task]),
        fmt.format_proposals([proposal]),
        fmt.format_task_detail(task),
    ):
        assert clean(rendered), repr(rendered)


@pytest.mark.parametrize("char", ["‮", "⁦", "​", "\x1b", "\x07", "\ud800"])
def test_tui_text_drops_control_surrogate_and_format_characters(char: str) -> None:
    assert bridge_tui._text(f"a{char}b") in {"ab", "a b"}
    assert clean(bridge_tui._text(HOSTILE))


def test_tui_put_never_draws_control_or_format_characters() -> None:
    drawn: list[str] = []

    class Window:
        def addnstr(self, y, x, text, width, attr=0):  # noqa: ANN001
            drawn.append(text)

    bridge_tui._put(Window(), 0, 0, HOSTILE, 80)  # type: ignore[arg-type]
    assert drawn and clean(drawn[0]), repr(drawn)
    assert "‮" not in drawn[0] and "\x1b" not in drawn[0]


def test_evidence_text_parses_dicts_and_survives_any_parser_error(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge_tui._evidence_text('{"source": "verifier"}') == "source=verifier"
    assert bridge_tui._evidence_text("{'source': 'verifier'}") == "source=verifier"
    assert bridge_tui._evidence_text("{not a dict}") == "{not a dict}"

    def explode(_text: str) -> object:
        raise TypeError("boom")  # literal_eval can also raise MemoryError, RecursionError, TypeError

    monkeypatch.setattr(bridge_tui.ast, "literal_eval", explode)
    assert bridge_tui._evidence_text("{'a': 1}") == "{'a': 1}"
