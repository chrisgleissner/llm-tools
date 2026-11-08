"""Usage-limit notices in a native provider's screen: wording, reset parsing and false positives."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from llm_tools.bridge_limit_notice import UsageLimitNotice, detect_usage_limit, parse_reset

FIXTURES = Path(__file__).parent / "fixtures" / "usage_limit"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 6, 21, 15, tzinfo=BERLIN).timestamp()  # Friday 21:15 local


def _at(*args: int) -> int:
    return int(datetime(*args, tzinfo=BERLIN).timestamp())


def screen(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("provider", "fixture", "reset_epoch"),
    [
        ("claude", "claude_usage_limit_reached.txt", _at(2026, 11, 6, 23, 40)),
        ("claude", "claude_five_hour_limit.txt", _at(2026, 11, 7, 15, 0)),  # 3pm has passed today: next day
        ("claude", "claude_legacy_limit_with_zone.txt", _at(2026, 11, 7, 16, 0)),
        ("codex", "codex_try_again_in.txt", int(NOW) + 2 * 3600 + 13 * 60),
        ("codex", "codex_try_again_at.txt", _at(2026, 11, 6, 23, 40)),
        ("codex", "codex_try_again_at_date.txt", _at(2026, 11, 8, 23, 40)),
    ],
)
def test_notice_wording_and_reset_time(provider: str, fixture: str, reset_epoch: int) -> None:
    found = detect_usage_limit(provider, screen(fixture), now=NOW, tz=BERLIN)
    assert isinstance(found, UsageLimitNotice)
    assert found.provider == provider and found.reset_epoch == reset_epoch and found.reset_text


def test_a_notice_without_a_reset_time_is_recognized_with_an_unknown_reset() -> None:
    found = detect_usage_limit("claude", screen("claude_limit_without_reset.txt"), now=NOW, tz=BERLIN)
    assert found is not None and found.reset_epoch is None and found.reset_text is None


def test_prose_and_code_that_mention_a_usage_limit_are_not_a_notice() -> None:
    text = screen("claude_editing_code_that_mentions_limits.txt")
    assert detect_usage_limit("claude", text, now=NOW, tz=BERLIN) is None
    assert detect_usage_limit("codex", text, now=NOW, tz=BERLIN) is None


def test_a_notice_for_another_provider_or_an_unknown_provider_is_ignored() -> None:
    assert detect_usage_limit("codex", screen("claude_usage_limit_reached.txt"), now=NOW, tz=BERLIN) is None
    assert detect_usage_limit("copilot", screen("claude_usage_limit_reached.txt"), now=NOW, tz=BERLIN) is None
    assert detect_usage_limit("claude", "", now=NOW, tz=BERLIN) is None


def test_only_the_bottom_of_the_screen_is_searched() -> None:
    old = "  ⎿  Usage limit reached · limit resets 11:40pm\n" + "output line\n" * 60
    assert detect_usage_limit("claude", old, now=NOW, tz=BERLIN) is None


def test_the_most_recent_notice_wins() -> None:
    text = "Usage limit reached · limit resets 9pm\nmore\nUsage limit reached · limit resets 11:40pm\n"
    found = detect_usage_limit("claude", text, now=NOW, tz=BERLIN)
    assert found is not None and found.reset_epoch == _at(2026, 11, 6, 23, 40)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("resets 12am", _at(2026, 11, 7, 0, 0)),
        ("resets 12pm", _at(2026, 11, 7, 12, 0)),
        ("resets Nov 9 at 3pm", _at(2026, 11, 9, 15, 0)),
        ("resets Dec 1 at 3pm", None),  # beyond any quota window
        ("try again in 45 minutes", int(NOW) + 45 * 60),
        ("resets in 1 day 3 hours", int(NOW) + 27 * 3600),
        ("resets in 400 days", None),  # not a quota window: a misparse
        ("resets 13pm", None),
        ("resets 25:00", None),
        ("resets 5", None),  # a bare number is not a time of day
        ("resets Foo 3 at 3pm", None),
        ("resets Feb 30 at 3pm", None),
    ],
)
def test_reset_parsing(text: str, expected: int | None) -> None:
    assert parse_reset(text, NOW, BERLIN)[1] == expected


def test_a_named_time_zone_overrides_the_local_zone_and_an_unknown_one_falls_back() -> None:
    utc = ZoneInfo("UTC")
    named = parse_reset("resets 3pm (America/New_York)", NOW, utc)[1]
    assert named == int(datetime(2026, 11, 7, 15, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert parse_reset("resets 3pm (Nowhere/Land)", NOW, BERLIN)[1] == _at(2026, 11, 7, 15, 0)


def test_default_zone_is_the_local_one() -> None:
    epoch = parse_reset("resets 11:40pm", NOW)[1]
    assert epoch is not None and 0 < epoch - NOW <= 24 * 3600
