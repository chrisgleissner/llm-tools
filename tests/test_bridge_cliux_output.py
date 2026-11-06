"""CLIUX output and glyph findings: 032, 037, 038, 065, 069, 070, 075."""

from __future__ import annotations

import pytest

from llm_tools import bridge_glyphs

REAL_USER_LOCALE_IS_C = bridge_glyphs._user_locale_is_c  # the autouse fixture replaces the module attribute


@pytest.fixture()
def locale_env(monkeypatch: pytest.MonkeyPatch):
    """Use the real locale detection with a controlled environment (CLIUX-065)."""
    monkeypatch.setattr(bridge_glyphs, "_user_locale_is_c", REAL_USER_LOCALE_IS_C)
    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: "UTF-8")  # what PEP 538 coercion reports for LANG=C
    for name in ("LC_ALL", "LC_CTYPE", "LANG", "LLM_BRIDGE_ASCII"):
        monkeypatch.delenv(name, raising=False)

    def apply(**values: str) -> bool:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        bridge_glyphs.ascii_mode.cache_clear()
        return bridge_glyphs.ascii_mode()

    yield apply
    bridge_glyphs.ascii_mode.cache_clear()


@pytest.mark.parametrize("env,expected", [
    ({"LANG": "C"}, True),
    ({"LANG": "POSIX"}, True),
    ({"LC_ALL": "C"}, True),
    ({"LANG": "C", "LC_CTYPE": "C.UTF-8"}, True),  # PEP 538 coercion exports LC_CTYPE=C.UTF-8 for LANG=C
    ({"LANG": "en_US.UTF-8"}, False),
    ({"LANG": "C.UTF-8"}, False),
    ({"LANG": "C", "LC_ALL": "en_US.UTF-8"}, False),
    ({}, False),
])
def test_ascii_glyphs_follow_the_locale_the_user_set(locale_env, env, expected) -> None:
    assert locale_env(**env) is expected


@pytest.mark.parametrize("value,expected", [("1", True), ("yes", True), ("0", False), ("false", False), ("", False)])
def test_llm_bridge_ascii_forces_the_ascii_glyph_set(locale_env, value, expected) -> None:
    assert locale_env(LANG="en_US.UTF-8", LLM_BRIDGE_ASCII=value) is expected


def test_ascii_state_glyphs_are_distinct_and_cancel_is_not_a_failure() -> None:
    ascii_forms = {name: GLYPH[1] for name, GLYPH in bridge_glyphs.GLYPHS.items() if name in {"running", "waiting", "review", "done", "failed", "cancelled", "paused", "square"}}
    assert len(set(ascii_forms.values())) == len(ascii_forms), ascii_forms
    assert ascii_forms["review"] == "?" and ascii_forms["done"] == "+" and ascii_forms["failed"] == "!"
    assert ascii_forms["cancelled"] == "-" and ascii_forms["square"] == "@"
    assert bridge_glyphs.state_look("CANCELLED") == ("cancelled", "state_paused")
    assert bridge_glyphs.state_look("FAILED")[1] == "state_failed"
    assert bridge_glyphs.state_text("CANCELLED", unicode=False) == "- CANCELLED"


# ---------------------------------------------------------------- CLIUX-069: the ASCII header has no `| |`


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: "UTF-8" if mode == "utf8" else "ANSI_X3.4-1968")
    bridge_glyphs.ascii_mode.cache_clear()


def test_ascii_header_leaves_out_the_identity_bar_so_it_cannot_read_as_two_separators(monkeypatch) -> None:
    from tests.test_bridge_tui_experience import full_snapshot, make_state
    from llm_tools import bridge_tui

    state = make_state(full_snapshot(), view="Overview")
    _set_mode(monkeypatch, "ascii")
    header = bridge_glyphs.localize(bridge_tui._header(state, 100))  # the draw code localizes every segment
    assert "| |" not in header and header.startswith("LLM BRIDGE | Fix the typo | ")
    _set_mode(monkeypatch, "utf8")
    assert bridge_tui._header(state, 100).startswith("LLM BRIDGE · ▌ Fix the typo · ")
    bridge_glyphs.ascii_mode.cache_clear()


# ---------------------------------------------------------------- CLIUX-032: cuts are marked, paths keep their end, pipes are not cut


def test_left_clip_keeps_the_last_folder_segment_whole() -> None:
    from llm_tools.bridge_switcher import left_clip

    assert left_clip("/home/chris/dev/llm-tools/repo2", 20) == "…/llm-tools/repo2"
    assert left_clip("/home/chris/dev/llm-tools/repo2", 20, unicode=False) == ".../llm-tools/repo2"
    assert left_clip("/a/b", 20) == "/a/b"
    clipped = left_clip("/a/b/a-very-long-final-segment-name", 14)
    assert clipped.startswith("…") and clipped.endswith("segment-name") and len(clipped) <= 14


def test_ascii_clip_marks_the_cut_with_three_dots_not_a_tilde() -> None:
    from llm_tools.bridge_switcher import clip

    assert clip("abcdefgh", 6, unicode=False) == "abc..."
    assert clip("abcdefgh", 6) == "abcde…"


def test_picker_folder_is_cut_from_the_left_in_ascii() -> None:
    from llm_tools import bridge_switcher as sw

    rows = [sw.MissionEntry(mission_id="a" * 32, name="Release 2.0 hardening", status="RUNNING", cwd="/home/chris/projects/release-engineering/porter")]
    picker = sw.MissionPicker(rows, unicode=False, home="/home/chris")
    text = "\n".join(line for line, _ in picker.lines(24, 60))
    assert "~/p~" not in text and "p~e" not in text and "~" not in text.replace("~/", "")  # no `~` standing for a cut
    narrow = "\n".join(line for line, _ in sw.MissionPicker(rows, unicode=False, home="/home/chris").lines(24, 40))
    assert ".../porter" in narrow


def test_missions_listing_is_never_cut_when_piped() -> None:
    from llm_tools import bridge_switcher as sw

    name = "Create a file named hello.txt containing the word hello, then stop."
    cwd = "/very/long/directory/path/that/goes/on/and/on/for/a/while/and/ends/here/repo2"
    rows = [sw.MissionEntry(mission_id="a" * 32, name=name, status="STOPPED", cwd=cwd, task_id=1, task_ordinal=1, task_title="Write tests for the parser and make sure edge cases are covered")]
    text = sw.format_missions(rows)  # stdout is captured, so it is not a terminal
    assert name in text and cwd in text and "…" not in text and "..." not in text


def test_mission_list_is_never_cut_when_piped() -> None:
    from llm_tools import bridge_format

    name = "Create a file named hello.txt containing the word hello, then stop."
    text = bridge_format.format_mission_list([{"id": "a" * 32, "status": "STOPPED", "updated_at": None, "name": name, "cwd": "/x"}])
    assert name in text


# ---------------------------------------------------------------- CLIUX-065: the theme report names the glyph set


def test_theme_report_names_the_glyph_set_and_how_to_change_it(monkeypatch, capsys) -> None:
    from llm_tools.bridge import main

    monkeypatch.setenv("LLM_BRIDGE_ASCII", "1")
    bridge_glyphs.ascii_mode.cache_clear()
    assert main(["theme", "--color", "never"]) == 0
    assert "Glyphs      ASCII (LLM_BRIDGE_ASCII is set)" in capsys.readouterr().out
    monkeypatch.delenv("LLM_BRIDGE_ASCII")
    bridge_glyphs.ascii_mode.cache_clear()
    assert main(["theme", "--color", "never"]) == 0
    assert "Glyphs      UTF-8 (set LLM_BRIDGE_ASCII=1" in capsys.readouterr().out


def test_help_keys_documents_the_ascii_switch() -> None:
    from llm_tools.bridge_help import topic_text

    assert "LLM_BRIDGE_ASCII=1" in topic_text("keys")
