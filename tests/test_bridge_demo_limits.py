"""The ``limits`` storyboard of scripts/bridge_demo.py: capacity figures, usage history and the stand-in provider."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("bridge_demo_limits_under_test", SCRIPTS / "bridge_demo.py")
assert _spec and _spec.loader
demo = importlib.util.module_from_spec(_spec)
sys.modules["bridge_demo_limits_under_test"] = demo
_spec.loader.exec_module(demo)


def test_limits_defaults_and_filtering() -> None:
    import argparse

    args = argparse.Namespace(storyboard="limits", size=None, cps=None, tail_hold=None, max_hold=None, seam_window=None, cell_width=None, gif_fps=None, gif_width=None, max_kb=None)
    demo.resolve_defaults(args)
    assert args.size == (90, 24) and args.cell_width == 12 and args.seam_window == 0.0
    assert "limits" in demo.FILTERED_STORYBOARDS and "limits" in demo.STORYBOARDS


def test_capacity_steps_fall_for_claude_and_stay_healthy_for_codex() -> None:
    assert list(demo.LIMITS_CLAUDE_STEPS) == sorted(demo.LIMITS_CLAUDE_STEPS, reverse=True)
    assert demo.LIMITS_CLAUDE_STEPS[-1] == 0
    assert len(set(demo.LIMITS_CLAUDE_STEPS)) == len(demo.LIMITS_CLAUDE_STEPS)  # every step changes the figure on screen
    assert len(demo.LIMITS_CODEX_STEPS) == len(demo.LIMITS_CLAUDE_STEPS)
    assert min(demo.LIMITS_CODEX_STEPS) > 50


def test_usage_payload_has_the_shape_of_the_injected_usage_json() -> None:
    payload = demo.limits_usage_payload(6, 88, 1_000_000)
    assert payload["claude"]["five_hour"] == {"remaining": 6, "resets_at": 1_000_000 + int(demo.LIMITS_CLAUDE_RESET_HOURS * 3600)}
    assert payload["codex"]["five_hour"]["remaining"] == 88
    assert payload["claude"]["available"] is True and "week" in payload["codex"]


def test_write_limits_usage_replaces_the_file_whole(tmp_path: Path) -> None:
    target = tmp_path / "usage.json"
    demo.write_limits_usage(target, 0)
    assert json.loads(target.read_text())["claude"]["five_hour"]["remaining"] == demo.LIMITS_CLAUDE_STEPS[0]
    demo.write_limits_usage(target, 3)
    assert json.loads(target.read_text())["claude"]["five_hour"]["remaining"] == 0
    assert not target.with_suffix(".tmp").exists()


def test_usage_history_gives_bridge_a_steady_burn_to_forecast_from() -> None:
    rows = demo.limits_usage_history(10_000_000)
    claude = [row for row in rows if row["provider"] == "Claude"]
    assert [row["ts"] for row in claude] == sorted(row["ts"] for row in claude)
    assert claude[0]["remaining"] > 90 and claude[-1]["remaining"] > demo.LIMITS_CLAUDE_STEPS[0]
    assert all(row["window"] == "5h" for row in rows)
    assert max(row["ts"] for row in rows) < 10_000_000
    values = [row["remaining"] for row in claude]
    assert values == sorted(values, reverse=True)


def test_usage_history_yields_a_forecast_from_the_estimator(tmp_path: Path) -> None:
    from llm_tools import common

    now = 1_800_000_000
    cache = tmp_path / "cache"
    log = cache / "llm-tools" / "llm-usage" / "llm-usage.log"
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps(row) + "\n" for row in demo.limits_usage_history(now)), encoding="utf-8")
    env = {"XDG_CACHE_HOME": str(cache), "HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": str(now)}
    estimate = common.estimate_remaining_time_from_log("Claude", "5h", demo.LIMITS_CLAUDE_STEPS[2], env)
    assert estimate != "-" and estimate.endswith("m")
    assert common.estimate_remaining_time_from_log("Codex", "5h", demo.LIMITS_CODEX_STEPS[2], env) != "-"


def test_limits_config_is_valid_tool_config() -> None:
    assert tomllib.loads(demo.LIMITS_CONFIG_TOML) == {"ralph": {"even_burn": False, "retry_delays": ""}}


def test_empty_provider_pane_with_only_the_station_status_line_is_transient() -> None:
    assert demo.is_transient("\n\n\nBridge: Ctrl-] then B │  acme-api  Task 1 ● RUNNING  │ ▶ Provider: Ctrl-] then P\n")
    assert demo.is_transient("\n▶ Bridge: Ctrl-] then B │ acme-api")
    assert not demo.is_transient("╭─╮\nBridge: Ctrl-] then B │ acme-api")


def test_wait_for_screen_accepts_any_of_several_needles() -> None:
    class Fake:
        def capture(self):
            return "\x1b[1m[Overview]\x1b[0m  Tasks", None

    demo.wait_for_screen(Fake(), demo.OVERVIEW_TAB, timeout=1)
    with pytest.raises(TimeoutError):
        demo.wait_for_screen(Fake(), "[9 Nothing]", timeout=0.2)


def test_stand_in_claude_reports_the_limit_and_exits_with_status_1(tmp_path: Path) -> None:
    limit = tmp_path / "limit"
    limit.write_text("go")
    env = {
        **os.environ,
        "DEMO_PROVIDER_SCENARIO": "limits", "DEMO_PROVIDER_NAME": "claude",
        "DEMO_PROVIDER_LIMIT_FILE": str(limit), "DEMO_PROVIDER_SPEED": "100", "DEMO_PROVIDER_CADENCE": "0",
    }
    result = subprocess.run([sys.executable, str(SCRIPTS / "bridge_demo_provider.py"), "Add a thing"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "5-hour limit reached" in result.stdout and "claude" in result.stdout
    assert result.stdout.startswith("\x1b[H\x1b[J")  # home and erase; no blank row above the banner


def _stand_in(tmp_path: Path, name: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ, "DEMO_PROVIDER_SCENARIO": "limits", "DEMO_PROVIDER_NAME": name,
        "DEMO_PROVIDER_SPEED": "100", "DEMO_PROVIDER_CADENCE": "0", **extra,
    }
    return subprocess.run([sys.executable, str(SCRIPTS / "bridge_demo_provider.py"), "Add a thing"], env=env, capture_output=True, text=True, timeout=60)


def test_stand_in_codex_passes_its_tests_and_exits_cleanly(tmp_path: Path) -> None:
    result = _stand_in(tmp_path, "codex")
    assert result.returncode == 0
    assert "6 passed" in result.stdout and "Task complete" in result.stdout


def test_stand_in_waits_for_its_go_file(tmp_path: Path) -> None:
    go = tmp_path / "go"
    go.mkdir()
    started = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "bridge_demo_provider.py"), "Add a thing"],
        env={**os.environ, "DEMO_PROVIDER_SCENARIO": "limits", "DEMO_PROVIDER_NAME": "codex", "DEMO_PROVIDER_SPEED": "100",
             "DEMO_PROVIDER_CADENCE": "0", "DEMO_PROVIDER_GO_DIR": str(go)},
        stdout=subprocess.PIPE, text=True,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            started.wait(timeout=1.0)  # banner printed, streaming held back
        (go / "codex.go").write_text("go")
        output, _ = started.communicate(timeout=30)
    finally:
        started.kill()
    assert started.returncode == 0 and "6 passed" in output


def test_overlay_places_captions_and_walks_by_marks() -> None:
    marks = {"caption2": 4.0, "caption3": 6.0, "walk1": 6.2, "caption4": 12.0, "walk2": 14.0}
    overlay = demo.Overlay.from_marks(marks)
    assert overlay is not None and demo.Overlay.from_marks({}) is None
    assert overlay.tag(0.0) == (0, demo.WIPE_STEPS, -1, 0)
    assert overlay.tag(4.0)[:2] == (1, 0)  # a new caption starts its wipe
    assert overlay.tag(4.0 + demo.WIPE_STEPS * demo.WIPE_STEP_SECONDS + 0.01)[:2] == (1, demo.WIPE_STEPS)
    assert overlay.tag(6.2 + 1.1)[2:] == (0, 2)  # third step of the first walk
    assert overlay.tag(9.0)[2:] == (0, demo.WALKS[0].steps - 1)  # the last step stays until the next walk
    assert overlay.tag(15.0)[2] == 1 and overlay.tag(20.0)[0] == 3


def test_walk_rows_follow_the_screen_and_not_its_wording() -> None:
    screen = "\n".join(
        ["header", "Now   task 1", "Next  nothing", "Tasks 1 verifying", "Needs you  approve", "", "Recent  a", "        b", "        c", "        d", "        e", "", "footer"]
    )
    assert demo.block_rows(screen, "Recent") == [6, 7, 8, 9, 10]
    assert demo.walk_row_numbers(screen, demo.WALKS[0]) == [7, 8, 9, 10]  # the newest four events
    assert demo.walk_row_numbers(screen, demo.WALKS[1]) == [1, 3, 4]
    assert demo.walk_row(screen, demo.WALKS[0], 99) == 10  # a step past the end stays on the last row
    assert demo.walk_row("no overview here", demo.WALKS[0], 0) is None


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [748, 1108])
def test_caption_band_is_legible_and_fits(theme: str, width: int) -> None:
    band = demo.CaptionBand(width, demo.THEMES[theme])
    assert band.height % 2 == 0
    for beat, text in enumerate(demo.CAPTIONS):
        assert band.font.getlength(text) <= width - 2 * band.PAD
        assert demo.contrast_ratio(band.text_color, band.background(beat)) >= 7
    assert band.font.size >= (34 if width < 900 else 30)  # about 17 px or more on a 390 px wide phone for the narrow clip
    base = band._Image.new("RGB", (width, 100), demo.THEMES[theme].bg)
    out = band.compose(base, (2, demo.WIPE_STEPS, -1, 0))
    assert out.size == (width, 100 + band.height)
    wiping = band.compose(base, (2, 0, -1, 0))
    assert wiping.getpixel((5, 100 + band.height - 3)) == band.background(2)
    assert wiping.getpixel((width - 5, 100 + band.height - 3)) == band.background(1)


def test_apply_cuts_removes_time_and_moves_later_marks() -> None:
    samples = [demo.Sample(float(t), str(t)) for t in range(10)]
    kept, marks = demo.apply_cuts(samples, {"cut_start": 3.0, "cut_end": 6.0, "caption3": 8.0, "inside": 4.0, "before": 1.0})
    assert [x.text for x in kept] == ["0", "1", "2", "6", "7", "8", "9"]
    assert [x.t for x in kept] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert marks == {"caption3": 5.0, "inside": 3.0, "before": 1.0}
    unchanged, same = demo.apply_cuts(samples, {"x": 1.0})
    assert unchanged == samples and same == {"x": 1.0}


def test_tags_split_frames_and_survive_scaling() -> None:
    samples = [demo.Sample(0.0, "a", None, (0,)), demo.Sample(0.1, "a", None, (0,)), demo.Sample(0.2, "a", None, (1,))]
    frames = demo.scale_durations(demo.dedupe_frames(samples, tail_hold=0.3), speed=2.0)
    assert [f.tag for f in frames] == [(0,), (1,)]
    assert [round(f.duration, 2) for f in frames] == [0.1, 0.15]
    assert demo.tag_samples(samples, None) == samples


def test_longest_static_ignores_the_final_hold() -> None:
    assert demo.longest_static([0.4, 0.7, 0.3, 1.5]) == 0.7
    assert demo.longest_static([1.5]) == 0.0


def test_limits_defaults_cap_the_encodes() -> None:
    assert demo.LIMITS_DEFAULTS["max_kb"] == 600 and demo.LIMITS_DEFAULTS["gif_width"] <= 960
    assert 10 <= demo.LIMITS_DEFAULTS["gif_fps"] <= 12 and demo.LIMITS_DEFAULTS["tail_hold"] <= 1.5


def test_encode_to_size_tries_the_next_attempt_until_the_file_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "clip.mp4"
    sizes = {30: 900, 32: 700, 34: 500}
    ran: list[int] = []

    def fake_ffmpeg(args: list[str]) -> None:
        crf = int(args[args.index("-crf") + 1])
        ran.append(crf)
        out.write_bytes(b"x" * sizes[crf])

    monkeypatch.setattr(demo, "_run_ffmpeg", fake_ffmpeg)
    build = lambda crf, scale: ["-crf", str(crf)]  # noqa: E731
    assert demo._encode_to_size(build, [(30, 1.0), (32, 1.0), (34, 1.0)], out, 600) == (34, 1.0)
    assert ran == [30, 32, 34]
    ran.clear()
    assert demo._encode_to_size(build, [(30, 1.0), (32, 1.0)], out, None) == (30, 1.0)  # no limit: one attempt
    assert ran == [30]
    with pytest.raises(RuntimeError, match="over the 100 byte limit"):
        demo._encode_to_size(build, [(30, 1.0), (32, 1.0)], out, 100)


def test_scale_filter_keeps_even_sizes() -> None:
    assert demo._scale_filter(1.0) == ""
    assert demo._scale_filter(0.8) == "scale=trunc(iw*0.8/2)*2:trunc(ih*0.8/2)*2:flags=lanczos,"
