"""The Bridge colour guide: every element is drawn with the role the guide assigns it.

The fake window records ``(y, x, text, attr)`` for each draw call. The role
table is replaced by one unique attribute per role, so each recorded write can
be mapped back to the role it was drawn with.
"""

from __future__ import annotations

import curses
import hashlib
from collections import Counter

import pytest

from llm_tools import bridge_glyphs, bridge_draw, bridge_theme as theme, bridge_tui
from tests.test_bridge_theme import _FakeCurses
from tests.test_bridge_tui_experience import Screen, full_snapshot, make_state, orders_snapshot
from tests.test_bridge_tui_experience import _quiet_curses  # noqa: F401 - autouse fixture
from tests.test_bridge_tui_overhaul import running_snapshot


@pytest.fixture()
def roles(monkeypatch):
    """Give every role its own attribute value and return the attribute -> role map."""
    table = {role: (index + 1) << 24 for index, role in enumerate(theme.ROLES)}
    monkeypatch.setattr(bridge_draw, "_ROLE_ATTRS", table)
    inverse = {value: role for role, value in table.items()}
    return inverse


def elements(screen: Screen, inverse: dict[int, str], y: int) -> list[tuple[str, str]]:
    """(text, role) of each draw call on screen row ``y``, in order."""
    return [(text, inverse.get((attr >> 24) << 24, f"attr{attr}")) for row, _x, text, attr in screen.writes if row == y and text]


def row_of(screen: Screen, prefix: str) -> int:
    return next(index for index, line in enumerate(screen.lines) if line.lstrip().startswith(prefix))


def draw(state, inverse, *, height: int = 34, width: int = 110) -> Screen:
    screen = Screen(height, width)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    return screen


def role_of(parts: list[tuple[str, str]], text: str) -> str:
    return next(role for part, role in parts if part.strip() == text)


# --- section 5 of the guide, element by element ---------------------------------------------------------

def test_header_wordmark_chip_name_and_state(roles) -> None:
    state = make_state(full_snapshot())
    parts = elements(draw(state, roles), roles, 0)
    assert parts == [
        ("LLM BRIDGE", "brand"), (" · ", "dim"), ("▌", theme.mission_role("M1")), (" Fix the typo", "info"),
        (" · ", "dim"), ("● RUNNING", "state_running"),
    ]


def test_header_state_words_use_their_state_role(roles) -> None:
    expected = {"RUNNING": "state_running", "CREATED": "state_waiting", "PAUSED": "state_paused", "STOPPED": "state_paused",
                "COMPLETED": "state_done", "FAILED": "state_failed", "CANCELLED": "state_paused"}
    for status, role in expected.items():
        state = make_state({"mission": {"id": "M1", "name": "n", "status": status}})
        parts = elements(draw(state, roles), roles, 0)
        assert parts[-1] == (bridge_glyphs.state_text(status), role), status


def test_pinned_colour_wins_over_the_id_hash(roles) -> None:
    snapshot = full_snapshot()
    snapshot["mission"]["config"] = {"colour": 7}  # type: ignore[index]
    parts = elements(draw(make_state(snapshot), roles), roles, 0)
    assert role_of(parts, "▌") == "mission_7"


def test_navigation_has_one_accent_tab(roles) -> None:
    parts = elements(draw(make_state(full_snapshot()), roles), roles, 2)
    assert Counter(role for _text, role in parts)["accent"] == 1
    assert role_of(parts, "[1 Overview]") == "accent"
    assert parts[0][1] == "muted"


def test_overview_labels_ids_ages_commands_and_capacity(roles) -> None:
    screen = draw(make_state(full_snapshot()), roles)
    controller = elements(screen, roles, row_of(screen, "Controller"))
    assert role_of(controller, "Controller") == "heading"
    assert role_of(controller, "active") == "state_running"
    assert role_of(controller, "2s ago") == "muted"
    now = elements(screen, roles, row_of(screen, "Now"))
    assert [role for _text, role in now][:2] == ["heading", "info"]
    assert role_of(now, "2") == "muted" and role_of(now, "● RUNNING") == "state_running"
    assert role_of(now, "second task") == "info"
    nxt = elements(screen, roles, row_of(screen, "Next"))
    assert role_of(nxt, "○ QUEUED") == "state_waiting"
    tasks = elements(screen, roles, row_of(screen, "Tasks"))
    assert role_of(tasks, "running") == "state_running" and role_of(tasks, "queued") == "state_waiting"
    # The counts are information: only the Needs-you row draws the eye, so verifying/blocked keep the default colour here.
    assert "verifying" in "".join(text for text, role in tasks if role == "info") and not any(role == "state_failed" for _t, role in tasks)
    needs = elements(screen, roles, row_of(screen, "Needs you"))
    assert role_of(needs, "Needs you") == "state_review"  # the one attention label
    assert role_of(needs, "/approve") == "key" and role_of(needs, "F7") == "key"
    capacity = elements(screen, roles, row_of(screen, "Capacity"))
    assert role_of(capacity, "Capacity") == "heading"
    assert role_of(capacity, "■") == "provider_codex"
    assert role_of(capacity, "100%") == "capacity_ok" and role_of(capacity, "resets 4h 59m") == "muted"


def test_capacity_percentages_follow_the_thresholds(roles) -> None:
    assert [bridge_tui._percent_role(value) for value in (100, 26, 25, 12, 1, 0)] == [
        "capacity_ok", "capacity_ok", "capacity_low", "capacity_low", "capacity_low", "capacity_empty"]
    parts = bridge_tui._inline_segments("5h 25% left, week 0% left · rate-limited · lasts past reset", "info")
    assert [role for _text, role in parts if role.startswith("capacity")] == [
        "capacity_low", "capacity_empty", "capacity_empty", "capacity_ok"]


def test_inactivity_is_muted_and_warns_from_ten_minutes(roles) -> None:
    quiet = bridge_tui._inline_segments("no output for 2m", "info")
    long = bridge_tui._inline_segments("no output for 10m", "info")
    assert quiet == [("no output for 2m", "muted")] and long == [("no output for 10m", "warn")]
    assert bridge_tui._idle_role("no output 9m 59s") == "muted"


def test_recent_clock_is_muted_and_failures_are_errors(roles) -> None:
    snapshot = full_snapshot()
    snapshot["operator_events"] = [
        {"id": 1, "kind": "task-added", "created_at": "2033-05-18T03:00:00+00:00", "task_id": 1, "data": {"title": "A"}},
        {"id": 2, "kind": "operation-exited", "created_at": "2033-05-18T03:00:03+00:00", "task_id": 1, "data": {"exit_code": 1}},
    ]
    screen = draw(make_state(snapshot), roles, )
    recent = elements(screen, roles, row_of(screen, "Recent"))
    assert recent[0] == ("Recent", "heading") and recent[1][0].strip() == "" and recent[2][1] == "muted"
    failure = elements(screen, roles, row_of(screen, "Recent") + 1)
    assert failure[-1] == ("Provider exited (1) on task 1", "error")


def test_overview_recent_uses_operator_events_when_the_snapshot_has_them() -> None:
    snapshot = full_snapshot()
    snapshot["events"] = [{"id": 9, "kind": "task-added", "created_at": "2033-05-18T03:00:00+00:00", "task_id": 9, "data": {"title": "from events"}}]
    snapshot["operator_events"] = [{"id": 1, "kind": "task-added", "created_at": "2033-05-18T03:00:00+00:00", "task_id": 1, "data": {"title": "from operator"}}]
    screen = Screen(34, 110)
    bridge_tui._draw_full(screen, make_state(snapshot))  # type: ignore[arg-type]
    text = "\n".join(screen.lines)
    assert "from operator" in text and "from events" not in text


def test_tasks_list_colours_only_the_state_and_mutes_ids_and_finished_rows(roles) -> None:
    snapshot = orders_snapshot()
    snapshot["tasks"].append({"id": 11, "title": "Shipped", "status": "COMPLETED", "priority": 1})  # type: ignore[union-attr]
    snapshot["tasks"].append({"id": 12, "title": "Dropped", "status": "CANCELLED"})  # type: ignore[union-attr]
    state = make_state(snapshot, view="Tasks")
    screen = draw(state, roles)
    selected = elements(screen, roles, 3)
    assert [role for _text, role in selected] == ["selected"] and selected[0][0].startswith("> 2 ")
    queued_row = elements(screen, roles, row_of(screen, "10"))
    assert role_of(queued_row, "10") == "muted" and role_of(queued_row, "◆ VERIFYING") == "state_review"
    assert role_of(queued_row, "P12") == "muted" and role_of(queued_row, "Troubleshooting") == "info"
    done = elements(screen, roles, row_of(screen, "11"))
    assert role_of(done, "✓ COMPLETED") == "state_done" and role_of(done, "Shipped") == "muted"
    cancelled = elements(screen, roles, row_of(screen, "12"))
    assert role_of(cancelled, "✕ CANCELLED") == "state_paused" and role_of(cancelled, "Dropped") == "muted"


def test_preview_separator_state_matches_the_list(roles) -> None:
    screen = draw(make_state(orders_snapshot(), view="Tasks"), roles)
    separator = elements(screen, roles, row_of(screen, "── task 2"))
    assert role_of(separator, "○ QUEUED") == "state_waiting" and role_of(separator, "task 2") == "muted"
    assert all(role == "dim" for text, role in separator if set(text) <= {"─", " "})
    labels = elements(screen, roles, row_of(screen, "Revision"))
    assert labels[0][1] == "muted"


def test_providers_use_a_square_and_keep_the_name_in_default_text(roles) -> None:
    snapshot = full_snapshot()
    snapshot["stations"] = [{"id": "codex", "name": "codex", "model": "gpt-5"}, {"id": "claude", "name": "claude", "model": "sonnet"},
                            {"id": "kilo", "name": "kilo", "model": "m"}]
    snapshot["station_context"]["viewed_station"] = "claude"  # type: ignore[index]
    state = make_state(snapshot, view="Providers")
    state.handle_key("KEY_END")  # the last block (kilo) holds the selection bar
    screen = draw(state, roles)
    codex = next(elements(screen, roles, i) for i, line in enumerate(screen.lines) if i >= 3 and "■ codex" in line)
    assert codex[0] == ("■", "provider_codex") and role_of(codex, "codex") == "info"
    assert any(role == "state_running" for text, role in codex if "running" in text)
    claude = next(elements(screen, roles, i) for i, line in enumerate(screen.lines) if i >= 3 and "■ claude" in line)
    assert claude[0] == ("■", "provider_claude") and role_of(claude, "[viewed]") == "accent"
    assert role_of(claude, "capacity not observed") == "muted"
    selected = next(elements(screen, roles, i) for i, line in enumerate(screen.lines) if line.startswith("> ■ kilo"))
    assert [role for _text, role in selected] == ["selected"]
    state.handle_key("KEY_HOME")
    screen = draw(state, roles)
    kilo = next(elements(screen, roles, i) for i, line in enumerate(screen.lines) if i >= 3 and "■ kilo" in line)
    assert kilo[0] == ("■", "provider_other")


def test_activity_rows_plumbing_dim_operator_default_failures_error(roles) -> None:
    from tests.test_bridge_tui_overhaul import activity_events

    screen = draw(make_state({"events": activity_events()}, view="Activity"), roles)
    rows = [elements(screen, roles, y) for y in range(3, 9) if screen.lines[y].strip()]
    by_text = {text: role for parts in rows for text, role in parts if text[:1].isalpha()}
    assert by_text["Provider exited (1) on task 1"] == "error"
    assert by_text["Provider process started"] == "dim"
    assert by_text["Task 1 added · A"] == "info"
    assert all(parts[0][1] in {"muted", "dim"} for parts in rows)  # clocks


def test_actions_rows_key_label_and_muted_effect(roles) -> None:
    state = make_state(full_snapshot(), view="Actions")
    screen = draw(state, roles)
    row = elements(screen, roles, 4)  # first unselected action row
    assert [role for _text, role in row if _text.strip()] == ["info", "key", "muted"]


def test_hints_name_keys_in_key_role_and_verbs_muted(roles) -> None:
    screen = draw(make_state(full_snapshot()), roles)
    hints = elements(screen, roles, 33)
    assert ("Ctrl-E", "key") in hints and (" add task", "muted") in hints and (" · ", "dim") in hints
    assert hints[-1] == ("?", "key") or hints[-1][0].strip().endswith("help")
    tasks = elements(draw(make_state(full_snapshot(), view="Tasks"), roles), roles, 33)
    assert ("Enter", "key") in tasks and (bridge_glyphs.glyph("updown"), "key") in tasks


def test_hint_splitter_handles_every_item_the_views_offer() -> None:
    for item, key in [("Ctrl-] P provider", "Ctrl-] P"), ("PgUp/PgDn page", "PgUp/PgDn"), ("Tab/F1-8 views", "Tab/F1-8"),
                      ("Ctrl-P/Ctrl-N history", "Ctrl-P/Ctrl-N"), ("? help", "?"), ("/ filter", "/"), ("/help commands", "/help"),
                      ("o objective", "o"), ("Shift-←/→ pan", "Shift-←/→"), ("Esc back", "Esc")]:
        assert bridge_tui._split_hint(item)[0] == key, item
    assert bridge_tui._split_hint("type to filter actions") == ("", "type to filter actions")


def test_command_prompt_is_accent_while_typing_and_muted_when_idle(roles) -> None:
    state = make_state(full_snapshot())
    idle = elements(draw(state, roles), roles, 32)
    assert idle[0] == ("› ", "muted")
    state.input_focused = True
    typing = elements(draw(state, roles), roles, 32)
    assert typing[0] == ("› ", "accent")


def test_status_strip_roles(roles) -> None:
    state = make_state(running_snapshot())
    state.snapshot["output_idle_seconds"] = 95
    strip = elements(draw(state, roles), roles, 1)
    assert strip[0] == ("■", "provider_codex") and role_of(strip, "95%") == "capacity_ok"
    assert role_of(strip, "21%") == "capacity_low" and role_of(strip, "no output 1m") == "muted"
    state.snapshot["output_idle_seconds"] = 700
    assert role_of(elements(draw(state, roles), roles, 1), "no output 11m") == "warn"


def test_strip_never_repeats_route_and_model() -> None:
    assert bridge_tui._owner_label("claude-sonnet", "claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert bridge_tui._owner_label("codex", "gpt-5") == "codex (gpt-5)"
    assert bridge_tui._owner_label("codex", "codex") == "codex" and bridge_tui._owner_label("codex", "") == "codex"
    assert bridge_tui._owner_label("kilo-minimax-m3", "minimax-m3") == "kilo-minimax-m3"


# --- snapshot fields from the queued-findings work -------------------------------------------------------

def waiting_snapshot() -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot["current_task"] = {}
    snapshot["tasks"] = [{"id": 3, "title": "t", "status": "QUEUED"}]
    snapshot["waiting"] = {
        "reason": "capacity", "providers": ["copilot"], "until_epoch": 2_000_000_000 + 2 * 86400,
        "entries": [{"name": "copilot", "window": "monthly", "remaining_percent": 0.0, "reset_epoch": 2_000_000_000 + 2 * 86400}],
        "detail": "copilot monthly 0%",
    }
    snapshot["station_context"] = {}
    return snapshot


def test_waiting_appears_in_the_strip_and_the_needs_you_row(roles) -> None:
    state = make_state(waiting_snapshot())
    screen = draw(state, roles)
    assert "waiting for capacity · copilot monthly 0% · resumes in 2d" in screen.lines[1]
    strip = elements(screen, roles, 1)
    assert role_of(strip, "waiting for capacity") == "warn" and role_of(strip, "0%") == "capacity_empty"
    needs = elements(screen, roles, row_of(screen, "Needs you"))
    assert role_of(needs, "Needs you") == "state_review"
    assert "Waiting for capacity: copilot monthly quota resets in 2d" in screen.lines[row_of(screen, "Needs you")]


def failed_snapshot() -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot["mission"]["status"] = "FAILED"  # type: ignore[index]
    snapshot["lifecycle"] = {"status": "FAILED", "failure_reason": "claude exited with status 9"}
    snapshot["current_task"] = {}
    return snapshot


def test_failure_reason_reaches_the_controller_row_and_the_strip(roles) -> None:
    screen = draw(make_state(failed_snapshot()), roles)
    assert screen.lines[1].startswith("mission failed — claude exited with status 9")
    controller = elements(screen, roles, row_of(screen, "Controller"))
    assert controller[-1] == ("failed — claude exited with status 9", "error")
    assert elements(screen, roles, 0)[-1] == ("✕ FAILED", "state_failed")
    plain = full_snapshot()
    plain["mission"]["status"] = "FAILED"  # type: ignore[index]
    assert make_state(plain).rows()[0] == "Controller  mission failed — controller finished"


# --- mission identity ----------------------------------------------------------------------------------

def test_mission_hash_is_stable_distributed_and_never_changes() -> None:
    ids = [hashlib.sha256(str(value).encode()).hexdigest()[:32] for value in range(4000)]  # random-looking ids like uuid4 hex
    numbers = [theme.mission_number(item) for item in ids]
    assert numbers == [theme.mission_number(item) for item in ids]  # stable
    counts = Counter(numbers)
    assert set(counts) == set(range(1, 9)) and min(counts.values()) > 400 and max(counts.values()) < 600  # even spread
    assert theme.mission_number("a" * 32) == int("a" * 8, 16) % 8 + 1
    assert theme.mission_role("not hex at all") == theme.mission_role("not hex at all")  # non-hex ids hash too
    assert theme.mission_number("") in range(1, 9)


def test_two_missions_in_the_same_folder_differ_by_id() -> None:
    assert len({theme.mission_role("0" * 7 + f"{value:x}" + "0" * 24, {}) for value in range(8)}) == 8
    assert theme.mission_role("11111111" + "a" * 24) != theme.mission_role("11111112" + "a" * 24)


@pytest.mark.parametrize("pin,expected", [(3, 3), ("5", 5), (" 8 ", 8), (None, None), (0, None), (9, None), (True, None), ("x", None)])
def test_pinned_colour_rules(pin, expected) -> None:
    automatic = theme.mission_number("0000000a" + "0" * 24)
    assert theme.mission_number("0000000a" + "0" * 24, {"colour": pin}) == (expected or automatic)


@pytest.mark.parametrize(("name", "polarity", "backgrounds", "minimum"), [
    ("dark", "dark", theme.DARK_BACKGROUNDS, 4.5), ("light", "light", theme.LIGHT_BACKGROUNDS, 4.5),
    ("high-contrast", "dark", theme.DARK_BACKGROUNDS, 6.0), ("high-contrast", "light", theme.LIGHT_BACKGROUNDS, 6.0),
])
def test_mission_hues_are_text_grade_and_chips_are_contrast_checked(name, polarity, backgrounds, minimum) -> None:
    resolved = theme.resolve({"LLM_BRIDGE_THEME": name, "LLM_BRIDGE_BACKGROUND": polarity})
    hues = set()
    for number in range(1, 9):
        style = resolved.style(f"mission_{number}")
        assert style.fg is not None and style.bg is None
        hues.add(style.fg)
        for label, background in backgrounds.items():
            ratio = theme.contrast_ratio(theme.xterm_rgb(style.fg), background)
            assert ratio >= minimum, f"{name}/mission_{number} on {label}: {ratio:.2f}"
        chip = theme.mission_chip_style(number, resolved)
        assert chip.bg == style.fg and chip.bold
        assert theme.contrast_ratio(theme.xterm_rgb(chip.fg), theme.xterm_rgb(chip.bg)) >= theme.TEXT_CONTRAST
    assert len(hues) == 8  # eight distinct hues


def test_mission_hues_avoid_the_state_hue_families() -> None:
    import colorsys

    for name, polarity in (("dark", "dark"), ("light", "light"), ("high-contrast", "dark"), ("high-contrast", "light")):
        resolved = theme.resolve({"LLM_BRIDGE_THEME": name, "LLM_BRIDGE_BACKGROUND": polarity})
        for number in range(1, 9):
            red, green, blue = (c / 255 for c in theme.xterm_rgb(resolved.style(f"mission_{number}").fg))
            hue, saturation, _value = colorsys.rgb_to_hsv(red, green, blue)
            if saturation > 0.25:
                assert not (0.04 <= hue <= 0.20 or 0.22 <= hue <= 0.45), (name, number, hue)  # amber/orange and green
                assert not (hue >= 0.97 or hue <= 0.02), (name, number, hue)  # red


def test_neutral_theme_keeps_every_hue_readable_on_white_and_black() -> None:
    resolved = theme.resolve({"LLM_BRIDGE_THEME": "neutral", "LLM_BRIDGE_BACKGROUND": "dark"})
    backgrounds = {**theme.DARK_BACKGROUNDS, **theme.LIGHT_BACKGROUNDS}
    for role in theme.ROLES:
        style = resolved.style(role)
        if style.fg is None:
            continue
        floor = theme.DECORATIVE_CONTRAST if role in theme.DECORATIVE_ROLES else 3.1
        for label, background in backgrounds.items():
            assert theme.contrast_ratio(theme.xterm_rgb(style.fg), background) >= floor, (role, style.fg, label)
    assert resolved.style("selected").reverse and resolved.style("muted").dim and resolved.style("selected").bg is None


def test_undetected_background_selects_the_neutral_palette_and_says_so() -> None:
    guessed = theme.resolve({"TERM": "xterm-256color"})
    assert guessed.guessed and guessed.polarity == "dark" and guessed.style("muted").dim
    known = theme.resolve({"TERM": "xterm-256color", "COLORFGBG": "0;15"})
    assert not known.guessed and known.polarity == "light" and not known.style("muted").dim
    assert not theme.resolve({"LLM_BRIDGE_THEME": "dark"}).guessed
    assert theme.describe(guessed, colors=256)["background"]["guessed"] is True


def test_mission_hue_overrides_apply_to_the_chip() -> None:
    resolved = theme.resolve({"LLM_BRIDGE_THEME": "dark", "LLM_BRIDGE_COLOR_MISSION_2": "#ff00ff"})
    assert resolved.style("mission_2").fg == theme.nearest_256((255, 0, 255))
    assert theme.mission_chip_style(2, resolved).bg == resolved.style("mission_2").fg
    assert theme.mission_chip_sgr(1, "dark").startswith("1;38;5;16;48;5;43")
    mono = theme.mission_chip_style(1, theme.resolve({"NO_COLOR": "1"}))
    assert mono.reverse and mono.fg is None and mono.bg is None
    with pytest.raises(ValueError):
        theme.mission_chip_style(9, theme.resolve({}))
    assert "colour43" in theme.tmux_mission_chip_markup(1, env={"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark"})
    assert theme.tmux_mission_chip_markup(1, env={"TERM": "dumb"}) == "#[default]"


# --- curses colour setup per theme -----------------------------------------------------------------------

@pytest.mark.parametrize("env,colors", [
    ({"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "dark"}, 256),
    ({"TERM": "xterm-256color", "LLM_BRIDGE_THEME": "light"}, 256),
    ({"TERM": "xterm", "LLM_BRIDGE_THEME": "dark"}, 8),
    ({"TERM": "xterm", "LLM_BRIDGE_THEME": "light"}, 8),
])
def test_every_mission_and_state_role_gets_a_visible_pair(monkeypatch, env, colors) -> None:
    fake = _FakeCurses(monkeypatch, colors=colors)
    assert bridge_tui.initialize_colors(env, is_tty=True)
    background = (0, 0, 0) if env["LLM_BRIDGE_THEME"] == "dark" else (255, 255, 255)
    for role in (*theme.MISSION_ROLES, "state_running", "state_review", "state_failed", "provider_codex", "capacity_low"):
        foreground, pair_background = fake.pairs[theme.ROLES.index(role) + 1]
        assert pair_background == -1  # the terminal's own background
        assert 0 <= foreground < colors
        if colors == 8:  # the nearest ANSI colour that still contrasts with the background
            assert theme.contrast_ratio(theme.xterm_rgb(foreground), background) >= theme.LOW_COLOR_CONTRAST, role


def test_mono_uses_attributes_only_and_default_colours(monkeypatch) -> None:
    calls: list[str] = []
    fake = _FakeCurses(monkeypatch)
    monkeypatch.setattr(curses, "use_default_colors", lambda: calls.append("default"))
    assert not bridge_tui.initialize_colors({"TERM": "xterm-256color", "NO_COLOR": "1"}, is_tty=True)
    assert fake.pairs == {} and calls == ["default"]  # no colour pair; pair 0 follows the terminal's colours
    assert bridge_tui.role_attr("state_failed") & curses.A_BOLD and not bridge_tui.role_attr("state_failed") & curses.A_REVERSE
    assert bridge_tui.role_attr("accent") & curses.A_UNDERLINE and bridge_tui.role_attr("brand") & curses.A_BOLD
    assert bridge_tui.role_attr("mission_3") == 0 and bridge_tui.role_attr("selected") & curses.A_REVERSE


def test_rate_limited_provider_colours_only_the_word_and_failed_operations_only_the_failure() -> None:
    snapshot = full_snapshot()
    now = 2_000_000_000
    snapshot["stations"] = [{"id": "kilo", "name": "kilo", "model": "m"}]
    snapshot["capacity"] = {"selection": {"route": "kilo", "decisions": [
        {"route": "kilo", "usable": False, "windows": [{"name": "5h", "kind": "reset_window", "remaining_percent": 0.0, "reset_epoch": now + 3600}]}]}}
    snapshot["operations"] = [{"id": "op-1", "task_id": 4, "station": "kilo", "status": "FAILED", "exit_code": 2, "created_at": now - 120}]
    snapshot["station_context"] = {}
    segments, _starts, _roles = make_state(snapshot, view="Providers")._provider_segments()
    header = next(row for row in segments if row[0][0] == "■")
    assert ("rate-limited", "capacity_empty") in header and (", resets in 1h", "muted") in header
    recent = next(row for row in segments if row[0][0].startswith("  recent: task 4"))
    assert [(text, role) for text, role in recent if role == "error.quiet"] == [("failed", "error.quiet"), ("exit 2", "error.quiet")]
    assert recent[0][1] == "dim" and recent[-1][1] == "dim"


def test_tmux_status_segments_name_the_state_and_localize_in_ascii(monkeypatch) -> None:
    snapshot = bridge_tui._bounded_snapshot(running_snapshot())
    quiet = bridge_tui._station_status_segments(snapshot)
    assert quiet[0] == ("Task 6 ", "info") and quiet[1] == ("● RUNNING", "state_running")
    silent = bridge_tui._station_status_segments(snapshot | {"output_idle_seconds": 700})
    assert silent[1] == ("no output for 11m", "warn")
    paused = {"mission": {"status": "PAUSED"}, "current_task": {"id": 2, "status": "VERIFYING"}}
    assert bridge_tui._station_status_segments(paused)[-2:] == [(" · ", "dim"), ("‖ PAUSED", "state_paused")]
    assert bridge_tui._station_status_segments({"mission": {"status": "CREATED"}})[0] == ("No task", "muted")
    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: "ANSI_X3.4-1968")
    bridge_glyphs.ascii_mode.cache_clear()
    assert bridge_glyphs.localize("Task 6 ● RUNNING · …") == "Task 6 * RUNNING | ..."
