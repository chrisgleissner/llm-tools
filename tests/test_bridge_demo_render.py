"""Unit tests for the Bridge demo recorder's ANSI parser, frame handling and renderer.

These tests need neither tmux nor ffmpeg. The renderer tests need Pillow and a
monospace TrueType font and are skipped when either is missing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge_demo.py"
_spec = importlib.util.spec_from_file_location("bridge_demo", SCRIPT)
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["bridge_demo"] = demo
_spec.loader.exec_module(demo)

ESC = "\x1b"


def parse(text: str, cols: int = 20, rows: int = 3):
    return demo.parse_ansi(text, cols, rows)


# -- SGR parsing --------------------------------------------------------------


def test_plain_text_fills_the_grid_and_pads_with_blanks() -> None:
    screen = parse("hello\nworld")
    assert screen.cols == 20 and screen.rows == 3
    assert screen.line_text(0) == "hello"
    assert screen.line_text(1) == "world"
    assert screen.line_text(2) == ""
    assert all(cell == demo.BLANK for cell in screen.cells[2])


def test_reset_and_default_colours() -> None:
    screen = parse(f"{ESC}[31mA{ESC}[0mB{ESC}[32mC{ESC}[39mD")
    assert screen.cells[0][0].style.fg == 1
    assert screen.cells[0][1].style == demo.DEFAULT_STYLE
    assert screen.cells[0][2].style.fg == 2
    assert screen.cells[0][3].style.fg is None
    # An empty parameter string is a full reset.
    assert parse(f"{ESC}[1mA{ESC}[mB").cells[0][1].style == demo.DEFAULT_STYLE


def test_bold_dim_italic_underline_and_their_resets() -> None:
    screen = parse(f"{ESC}[1;2;3;4mA{ESC}[22;23;24mB")
    a, b = screen.cells[0][0].style, screen.cells[0][1].style
    assert (a.bold, a.dim, a.italic, a.underline) == (True, True, True, True)
    assert (b.bold, b.dim, b.italic, b.underline) == (False, False, False, False)


def test_256_colour_foreground_and_background() -> None:
    screen = parse(f"{ESC}[38;5;39;48;5;25mX")
    style = screen.cells[0][0].style
    assert style.fg == 39 and style.bg == 25
    # The same colour when it arrives as colon-separated sub-parameters.
    assert parse(f"{ESC}[38:5:39mX").cells[0][0].style.fg == 39
    assert demo.palette_color(39, demo.DARK) == (0, 175, 255)
    assert demo.palette_color(25, demo.DARK) == (0, 95, 175)
    assert demo.palette_color(245, demo.DARK) == (138, 138, 138)
    assert demo.palette_color(231, demo.DARK) == (255, 255, 255)


def test_truecolour_and_bright_colours() -> None:
    screen = parse(f"{ESC}[38;2;10;20;30;48;2;1;2;3mX{ESC}[91;102mY")
    x, y = screen.cells[0][0].style, screen.cells[0][1].style
    assert x.fg == (10, 20, 30) and x.bg == (1, 2, 3)
    assert y.fg == 9 and y.bg == 10
    # Colon form with an empty colour-space field.
    assert parse(f"{ESC}[38:2::7:8:9mZ").cells[0][0].style.fg == (7, 8, 9)


def test_reverse_video_swaps_foreground_and_background() -> None:
    screen = parse(f"{ESC}[7mR{ESC}[27mN")
    reverse, normal = screen.cells[0][0].style, screen.cells[0][1].style
    assert reverse.reverse and not normal.reverse
    theme = demo.DARK
    assert demo.resolve_colors(normal, theme) == (theme.fg, theme.bg)
    assert demo.resolve_colors(reverse, theme) == (theme.bg, theme.fg)
    coloured = demo.Style(fg=1, bg=4, reverse=True)
    assert demo.resolve_colors(coloured, theme) == (theme.ansi[4], theme.ansi[1])
    # Cursor inversion applied on top of reverse video restores the original.
    assert demo.resolve_colors(coloured, theme, invert=True) == (theme.ansi[1], theme.ansi[4])


def test_dim_blends_the_foreground_toward_the_background() -> None:
    fg, bg = demo.resolve_colors(demo.Style(dim=True), demo.DARK)
    assert fg != demo.DARK.fg and bg == demo.DARK.bg
    assert all(bg_c <= f <= fg_c for f, fg_c, bg_c in zip(fg, demo.DARK.fg, demo.DARK.bg))


def test_style_persists_across_newlines_but_blank_cells_stay_default() -> None:
    screen = parse(f"{ESC}[31mred\nstill red{ESC}[0m")
    assert screen.cells[1][0].style.fg == 1
    assert screen.cells[0][10].style == demo.DEFAULT_STYLE


def test_non_sgr_escape_sequences_are_ignored() -> None:
    text = f"{ESC}[2J{ESC}[H{ESC}]0;title\x07{ESC}[?25lA{ESC}(BB{ESC}[1;5HC"
    assert parse(text).line_text(0) == "ABC"
    assert demo.strip_ansi(f"{ESC}[1mbold{ESC}[0m plain") == "bold plain"


def test_input_beyond_the_grid_is_dropped() -> None:
    screen = parse("abcdefghij\nsecond\nthird\nfourth\nfifth", cols=5, rows=3)
    assert screen.line_text(0) == "abcde"
    assert screen.line_text(1) == "secon"
    assert screen.line_text(2) == "third"


# -- wide characters ----------------------------------------------------------


@pytest.mark.parametrize(
    ("ch", "width"),
    [("a", 1), ("│", 1), ("▌", 1), ("●", 1), ("◆", 1), ("✓", 1), ("é", 1), ("中", 2), ("あ", 2), ("Ａ", 2), ("́", 0), ("​", 0)],
)
def test_char_width(ch: str, width: int) -> None:
    assert demo.char_width(ch) == width


def test_wide_character_occupies_two_cells() -> None:
    screen = parse(f"a中b{ESC}[1mあ{ESC}[0mc", cols=10)
    row = screen.cells[0]
    assert [c.ch for c in row[:7]] == ["a", "中", "", "b", "あ", "", "c"]
    assert row[4].style.bold and row[5].style.bold  # the placeholder shares the style
    assert screen.line_text(0) == "a中bあc"


def test_wide_character_that_does_not_fit_is_dropped() -> None:
    screen = parse("abc中", cols=4)
    assert screen.line_text(0) == "abc"


def test_combining_marks_attach_to_the_previous_cell() -> None:
    screen = parse("éx中́y", cols=10)
    row = screen.cells[0]
    assert row[0].ch == "é"
    assert row[1].ch == "x"
    assert row[2].ch == "中́"
    assert row[3].ch == ""
    assert row[4].ch == "y"


def test_box_drawing_and_symbols_keep_single_cell_columns() -> None:
    screen = parse("│▌●◆✓│", cols=10)
    assert [c.ch for c in screen.cells[0][:6]] == list("│▌●◆✓│")
    assert screen.cells[0][6] == demo.BLANK


# -- frame de-duplication and timing ------------------------------------------


def test_dedupe_merges_identical_consecutive_frames() -> None:
    samples = [
        demo.Sample(0.0, "a"),
        demo.Sample(0.1, "a"),
        demo.Sample(0.2, "b"),
        demo.Sample(0.3, "b"),
        demo.Sample(0.4, "b"),
        demo.Sample(0.5, "a"),
    ]
    held = demo.dedupe_frames(samples, tail_hold=1.0)
    assert [h.text for h in held] == ["a", "b", "a"]
    assert [round(h.duration, 3) for h in held] == [0.2, 0.3, 1.0]
    assert sum(h.duration for h in held) == pytest.approx(1.5 + 0.0)


def test_dedupe_treats_a_cursor_move_as_a_change() -> None:
    samples = [demo.Sample(0.0, "x", (1, 1)), demo.Sample(0.1, "x", (2, 1)), demo.Sample(0.2, "x", (2, 1))]
    held = demo.dedupe_frames(samples, tail_hold=0.5)
    assert [h.cursor for h in held] == [(1, 1), (2, 1)]
    assert [round(h.duration, 3) for h in held] == [0.1, 0.6]


def test_dedupe_of_nothing_and_of_one_sample() -> None:
    assert demo.dedupe_frames([]) == []
    only = demo.dedupe_frames([demo.Sample(5.0, "x")], tail_hold=2.0)
    assert len(only) == 1 and only[0].duration == 2.0


def test_speed_and_max_hold() -> None:
    frames = [demo.Held("a", None, 8.0), demo.Held("b", None, 1.0)]
    scaled = demo.scale_durations(frames, speed=2.0, max_hold=4.0)
    assert [f.duration for f in scaled] == [2.0, 0.5]
    assert [f.duration for f in demo.scale_durations(frames, speed=1.0)] == [8.0, 1.0]


def test_quantised_durations_do_not_drift() -> None:
    durations = [0.104, 0.104, 0.104, 0.104, 0.104, 0.004, 0.2]
    quantised = demo.quantise_durations(durations)
    assert all(abs(round(q * 100) - q * 100) < 1e-6 for q in quantised)
    assert all(q >= 0.02 for q in quantised)
    assert sum(quantised) == pytest.approx(sum(durations), abs=0.02)


def test_saved_frames_round_trip(tmp_path: Path) -> None:
    samples = [demo.Sample(0.0, "a\n", None), demo.Sample(1.25, "b", (3, 4))]
    path = tmp_path / "frames.json"
    demo.save_frames(path, samples, (110, 30))
    loaded, size = demo.load_frames(path)
    assert size == (110, 30)
    assert loaded == samples
    assert json.loads(path.read_text())["size"] == [110, 30]


def test_redact_replaces_private_prefixes_and_keeps_columns() -> None:
    text = "Folder: /tmp/bd-ab12/home/acme-api  next\nplain line"
    result = demo.redact(text, {"/tmp/bd-ab12/home": "/home/demo", "/tmp/bd-ab12": "/tmp/demo"})
    assert result.splitlines()[0] == "Folder: " + "/home/demo/acme-api".ljust(len("/tmp/bd-ab12/home/acme-api")) + "  next"
    assert len(result.splitlines()[0]) == len(text.splitlines()[0])
    assert result.splitlines()[1] == "plain line"


def test_parse_size() -> None:
    assert demo.parse_size("110x30") == (110, 30)
    with pytest.raises(Exception):
        demo.parse_size("110")


# -- highlight storyboard helpers ---------------------------------------------


def test_transient_screens_are_recognised() -> None:
    assert demo.is_transient("   \n\n")
    assert demo.is_transient("controller-active" + " " * 40 + "[0/0]\n\n\nBridge: Ctrl-] then B | acme-api")
    assert demo.is_transient("[detached (from session mission-abc)]\n[detached (from session mission-def)]")
    assert demo.is_transient("LLM BRIDGE \u00b7 abc \u00b7 LOADING\nloading\u2026")
    assert not demo.is_transient("LLM BRIDGE \u00b7 Fix flaky checkout \u00b7 RUNNING\ntask 1 awaits approval")
    # A real screen that merely mentions one marker is kept.
    assert not demo.is_transient("controller-active\nsecond\nthird\nfourth line")


def test_drop_transient_samples_keeps_order_and_the_rest() -> None:
    samples = [demo.Sample(0.0, "home"), demo.Sample(0.1, "  "), demo.Sample(0.2, "station"), demo.Sample(0.3, "[detached (from session x)]")]
    assert [s.text for s in demo.drop_transient_samples(samples)] == ["home", "station"]


def test_drop_blips_removes_single_captures_between_identical_ones() -> None:
    texts = ["a", "b", "a", "a", "c", "d", "c", "e"]
    samples = [demo.Sample(i * 0.1, text) for i, text in enumerate(texts)]
    # "b" sits between two "a"; "d" between two "c".  "c" (4) is followed by "d", so it stays.
    assert [s.text for s in demo.drop_blips(samples, tolerance=0)] == ["a", "a", "a", "c", "c", "e"]
    assert [s.text for s in demo.drop_blips(samples[:2], tolerance=0)] == ["a", "b"]
    assert demo.drop_blips([]) == []
    steady = [demo.Sample(0.0, "x"), demo.Sample(0.1, "y"), demo.Sample(0.2, "z")]
    assert demo.drop_blips(steady) == steady


def test_drop_blips_tolerates_a_ticking_timer_in_the_neighbours() -> None:
    screen = "\n".join("abcdefghij"[n] * 30 for n in range(5))
    scrolled = "\n".join("abcdefghij"[n + 1] * 30 for n in range(5))
    before = demo.Sample(0.0, screen + "\nRunning tests 7.0s")
    glitch = demo.Sample(0.1, scrolled + "\nRunning tests 7.0s")
    after = demo.Sample(0.2, screen + "\nRunning tests 7.1s")
    assert demo.drop_blips([before, glitch, after]) == [before, after]
    # A typed character is a real change, not a blip.
    typed = [demo.Sample(0.0, "abc"), demo.Sample(0.1, "abcd"), demo.Sample(0.2, "abc")]
    assert demo.drop_blips(typed) == typed


def test_text_distance_counts_cells_and_ignores_colour() -> None:
    assert demo.text_distance("abc", "abc") == 0
    assert demo.text_distance(f"{ESC}[31mabc{ESC}[0m", "abc") == 0
    assert demo.text_distance("abc", "abd") == 1
    assert demo.text_distance("ab\ncd", "ab") == 2  # a missing row differs in every cell
    assert demo.text_distance("a", "abc") == 2


def _held(texts: list[str], duration: float = 1.0) -> list:
    return [demo.Held(text, None, duration) for text in texts]


def test_loop_seam_picks_the_frame_closest_to_the_first() -> None:
    frames = _held(["home", "station", "overview", "hxme", "home!", "odd"])
    # Candidates are the frames starting in the last 3 seconds: "hxme", "home!", "odd".
    assert demo.loop_seam_end(frames, window=3.0) == 4  # "home!" differs by 1 cell, "hxme" also by 1; the later wins
    assert demo.loop_seam_end(_held(["home", "x", "home"]), window=3.0) == 2


def test_loop_seam_ignores_frames_before_the_window_and_the_first_frame() -> None:
    frames = _held(["home", "home", "zzzz", "zzzz"])
    # The second frame is identical to the first but starts too early to be chosen.
    assert demo.loop_seam_end(frames, window=2.0) in (2, 3)
    assert demo.loop_seam_end(_held(["only"]), window=5.0) == 0
    with pytest.raises(ValueError):
        demo.loop_seam_end([], window=1.0)


def test_loop_seam_tie_prefers_the_later_frame() -> None:
    assert demo.loop_seam_end(_held(["a", "b", "b", "b"]), window=3.0) == 3


def test_render_samples_cuts_the_recording_at_the_seam(pil) -> None:
    samples = [demo.Sample(0.0, "home"), demo.Sample(1.0, "other"), demo.Sample(2.0, "home"), demo.Sample(3.0, "zzzz"), demo.Sample(4.0, "zzzz!")]
    images, durations = demo.render_samples(samples, (10, 2), demo.DARK, seam_window=4.0, tail_hold=2.0, chrome=False)
    assert len(images) == 3  # home, other, home
    assert durations[-1] == 2.0


def test_sample_at_and_poster_index() -> None:
    samples = [demo.Sample(0.0, "a"), demo.Sample(1.0, "b"), demo.Sample(2.0, "c")]
    assert demo.sample_at(samples, 0.5) == 0
    assert demo.sample_at(samples, 1.0) == 1
    assert demo.sample_at(samples, 9.0) == 2
    assert demo.poster_index(samples, 1.4) == 1
    with pytest.raises(ValueError):
        demo.poster_index([], None)


def test_poster_without_a_mark_is_the_densest_screen() -> None:
    samples = [demo.Sample(0.0, "a"), demo.Sample(1.0, f"{ESC}[1mabc def{ESC}[0m\nxyz"), demo.Sample(2.0, "ab")]
    assert demo.information_density(samples[1].text) == 9
    assert demo.poster_index(samples, None) == 1
    tied = [demo.Sample(0.0, "ab"), demo.Sample(1.0, "cd")]
    assert demo.poster_index(tied, None) == 0  # first of equally dense screens


def test_marks_and_storyboard_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "frames.json"
    demo.save_frames(path, [demo.Sample(0.0, "a")], (100, 28), {"poster": 12.34567}, "highlight")
    assert demo.load_marks(path) == {"poster": 12.3457}
    assert demo.load_storyboard(path) == "highlight"
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"size": [110, 30], "samples": [{"t": 0, "text": "a", "cursor": None}]}))
    assert demo.load_marks(old) == {}
    assert demo.load_storyboard(old) == "full"


def test_storyboard_defaults_and_registry() -> None:
    import argparse

    assert set(demo.STORYBOARDS) == {"full", "highlight", "limits"}
    args = argparse.Namespace(storyboard="highlight", size=None, cps=None, tail_hold=None, max_hold=None, seam_window=None, cell_width=None)
    demo.resolve_defaults(args)
    assert (args.size, args.cps, args.cell_width) == ((90, 24), 60.0, 10)
    full = argparse.Namespace(storyboard="full", size=None, cps=None, tail_hold=None, max_hold=None, seam_window=None, cell_width=None)
    demo.resolve_defaults(full)
    assert (full.size, full.cps, full.seam_window) == ((110, 30), 25.0, 0.0)
    explicit = argparse.Namespace(storyboard="highlight", size=(80, 24), cps=10.0, tail_hold=None, max_hold=None, seam_window=None, cell_width=None)
    demo.resolve_defaults(explicit)
    assert explicit.size == (80, 24) and explicit.cps == 10.0


def test_highlight_seed_missions_cover_four_states_with_distinct_colours() -> None:
    missions = demo.HIGHLIGHT_MISSIONS
    assert sorted(m.state for m in missions) == ["completed", "created", "review", "running"]
    assert len({m.colour for m in missions}) == 4
    assert len({m.repo for m in missions}) == 4
    assert missions[0].state == "review"  # task 1, so Bridge's /approve numbering matches
    assert all(len(m.name) <= 14 for m in missions)  # fits the picker's name column at 90 cells


# -- rendering ----------------------------------------------------------------


@pytest.fixture()
def pil():
    return pytest.importorskip("PIL.Image")


@pytest.fixture()
def renderer(pil):
    try:
        return demo.Renderer(12, 3, demo.DARK, chrome=False)
    except RuntimeError as exc:  # no monospace font installed
        pytest.skip(str(exc))


def pixels(image) -> list[tuple[int, int, int]]:
    data = image.convert("RGB").tobytes()
    return [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3)]


def region(image, renderer, col: int, row: int, width: int = 1):
    x0 = renderer.pad_x + col * renderer.cell_w
    y0 = renderer.bar_h + renderer.pad_y + row * renderer.cell_h
    return image.crop((x0, y0, x0 + width * renderer.cell_w, y0 + renderer.cell_h))


def test_render_size_is_even_and_background_is_the_theme_background(renderer) -> None:
    image = renderer.render(parse("", 12, 3))
    assert image.size[0] % 2 == 0 and image.size[1] % 2 == 0
    assert image.getpixel((0, 0)) == demo.DARK.bg
    assert set(pixels(region(image, renderer, 0, 0, 12))) == {demo.DARK.bg}


def test_render_draws_background_colour_of_a_cell(renderer) -> None:
    image = renderer.render(parse(f"{ESC}[48;5;25m  {ESC}[0m", 12, 3))
    expected = demo.palette_color(25, demo.DARK)
    assert set(pixels(region(image, renderer, 0, 0, 2))) == {expected}
    assert set(pixels(region(image, renderer, 2, 0))) == {demo.DARK.bg}


def test_render_reverse_video_uses_the_foreground_as_background(renderer) -> None:
    image = renderer.render(parse(f"{ESC}[7m {ESC}[0m", 12, 3))
    assert set(pixels(region(image, renderer, 0, 0))) == {demo.DARK.fg}


def test_wide_character_background_spans_both_cells(renderer) -> None:
    image = renderer.render(parse(f"{ESC}[44m中{ESC}[0mx", 12, 3))
    blue = demo.DARK.ansi[4]
    assert set(pixels(region(image, renderer, 0, 0, 2))) >= {blue}
    assert set(pixels(region(image, renderer, 1, 0))) >= {blue}
    assert blue not in set(pixels(region(image, renderer, 2, 0)))
    assert blue not in set(pixels(region(image, renderer, 3, 0)))


def test_text_is_drawn_in_the_foreground_colour(renderer) -> None:
    image = renderer.render(parse(f"{ESC}[38;5;78mM", 12, 3))
    colours = set(pixels(region(image, renderer, 0, 0)))
    assert demo.palette_color(78, demo.DARK) in colours


def test_box_drawing_and_blocks_fill_the_cell_edges_without_gaps(renderer) -> None:
    image = renderer.render(parse("─│█▌", 12, 3))
    fg = demo.DARK.fg
    horizontal = region(image, renderer, 0, 0)
    mid = renderer.cell_h // 2
    assert all(horizontal.getpixel((x, mid)) == fg for x in range(renderer.cell_w))
    vertical = region(image, renderer, 1, 0)
    cx = renderer.cell_w // 2
    assert all(vertical.getpixel((cx, y)) == fg for y in range(renderer.cell_h))
    assert set(pixels(region(image, renderer, 2, 0))) == {fg}
    half = region(image, renderer, 3, 0)
    assert half.getpixel((0, 0)) == fg and half.getpixel((renderer.cell_w - 1, 0)) == demo.DARK.bg


def test_cursor_inverts_its_cell_and_can_be_disabled(pil) -> None:
    on = demo.Renderer(12, 3, demo.DARK, chrome=False, show_cursor=True)
    off = demo.Renderer(12, 3, demo.DARK, chrome=False, show_cursor=False)
    screen = parse("", 12, 3)
    assert set(pixels(region(on.render(screen, (2, 1)), on, 2, 1))) == {demo.DARK.fg}
    assert set(pixels(region(off.render(screen, (2, 1)), off, 2, 1))) == {demo.DARK.bg}
    assert set(pixels(region(on.render(screen, None), on, 2, 1))) == {demo.DARK.bg}


def test_light_theme_uses_its_own_background(pil) -> None:
    light = demo.Renderer(12, 3, demo.LIGHT, chrome=False)
    image = light.render(parse("", 12, 3))
    assert image.getpixel((0, 0)) == demo.LIGHT.bg == (250, 250, 250)


def test_rendering_is_deterministic_and_row_cache_is_transparent(renderer) -> None:
    screen = parse(f"{ESC}[1mBridge{ESC}[0m ok\nline │ two", 12, 3)
    first = renderer.render(screen)
    second = renderer.render(screen)
    assert first.tobytes() == second.tobytes()
    fresh = demo.Renderer(12, 3, demo.DARK, chrome=False)
    assert fresh.render(screen).tobytes() == first.tobytes()


def test_chrome_adds_a_title_bar(pil) -> None:
    with_bar = demo.Renderer(12, 3, demo.DARK, chrome=True)
    without = demo.Renderer(12, 3, demo.DARK, chrome=False)
    assert with_bar.size[1] > without.size[1]
    image = with_bar.render(parse("", 12, 3))
    assert image.getpixel((140, 4)) == demo.DARK.chrome_bg


def test_render_samples_dedupes_and_returns_matching_durations(pil) -> None:
    samples = [demo.Sample(0.0, "one"), demo.Sample(0.1, "one"), demo.Sample(0.2, "two")]
    images, durations = demo.render_samples(samples, (12, 3), demo.DARK, tail_hold=1.0, chrome=False)
    assert len(images) == 2 and len(durations) == 2
    assert durations == pytest.approx([0.2, 1.0])


def test_pillow_gif_fallback_writes_a_looping_gif(pil, tmp_path: Path) -> None:
    from PIL import Image

    samples = [demo.Sample(0.0, "one"), demo.Sample(0.5, f"{ESC}[31mtwo"), demo.Sample(1.0, "three")]
    images, durations = demo.render_samples(samples, (12, 3), demo.DARK, tail_hold=0.5, chrome=False)
    out = tmp_path / "demo.gif"
    demo.encode_gif_pillow(images, demo.quantise_durations(durations), out)
    with Image.open(out) as gif:
        assert gif.n_frames == 3
        assert gif.info.get("loop") == 0
        assert gif.info["duration"] == 500


def test_find_font_returns_an_existing_file() -> None:
    try:
        path = demo.find_font()
    except RuntimeError:
        pytest.skip("no monospace font installed")
    assert Path(path).is_file()
