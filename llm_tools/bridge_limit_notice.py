"""Recognize a native provider's usage-limit notice in the text of its station screen.

A native Claude Code or Codex session that runs out of quota keeps its pane open and only prints a
notice, so the process never exits and no exit status reaches the controller. The controller reads
the pane through the existing station activity read (``TmuxTerminalBackend.pane_activity_screen``)
and passes the text here.

The notice has a fixed shape: a line that *begins* with the provider's limit phrase (after any
decoration glyphs), optionally followed on the same or the next lines by a reset time. A sentence
that merely mentions a usage limit, such as source code or prose the provider is editing, does not
begin that way and is ignored. Standard library only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from . import bridge_providers

# Only the last lines of the screen are searched: the notice sits just above the input box.
SCREEN_TAIL_LINES = 40
# Lines after the notice that may continue a wrapped sentence.
CONTINUATION_LINES = 2
# A reset further away than this is a misparse, not a quota window.
MAX_RESET_SECONDS = 8 * 24 * 3600
NOTICE_CHARS = 240

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION = re.compile(
    r"\b(?:try again|retry|resets?)\s+in\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_DURATION_PART = re.compile(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_ABSOLUTE = re.compile(
    r"\b(?:resets?|try again)\s+(?:at\s+)?"
    r"(?:(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(?P<year>\d{4}))?,?\s*(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UsageLimitNotice:
    """A provider's usage-limit notice: the provider, the matched text and the reset time if it states one."""

    provider: str
    notice: str
    reset_text: str | None
    reset_epoch: int | None


def _duration_seconds(text: str) -> int:
    return sum(int(number) * _UNITS[unit[0].lower()] for number, unit in _DURATION_PART.findall(text))


def _zone(name: str | None, fallback: tzinfo) -> tzinfo:
    if not name:
        return fallback
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return fallback


def _absolute_epoch(match: re.Match[str], now: float, local: tzinfo) -> int | None:
    zone = _zone(match["zone"], local)
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    meridiem = (match["ampm"] or "").lower().replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif match["minute"] is None:
        return None  # a bare number is not a time of day
    if hour > 23 or minute > 59:
        return None
    current = datetime.fromtimestamp(now, zone)
    try:
        if match["month"]:
            month_name = match["month"][:3].lower()
            if month_name not in _MONTHS:
                return None
            month, day = _MONTHS.index(month_name) + 1, int(match["day"])
            year = int(match["year"]) if match["year"] else current.year
            target = datetime(year, month, day, hour, minute, tzinfo=zone)
            if target <= current and not match["year"]:
                target = target.replace(year=year + 1)
        else:
            target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= current:
                target = target + timedelta(days=1)
    except ValueError:
        return None
    return int(target.timestamp())


def parse_reset(text: str, now: float, tz: tzinfo | None = None) -> tuple[str | None, int | None]:
    """The reset time a notice states, as ``(text as written, epoch)``; ``(None, None)`` when it states none."""
    local = tz if tz is not None else datetime.fromtimestamp(now).astimezone().tzinfo or ZoneInfo("UTC")
    duration = _DURATION.search(text)
    if duration:
        seconds = _duration_seconds(duration.group(1))
        return duration.group(0).strip(), (int(now) + seconds if 0 < seconds <= MAX_RESET_SECONDS else None)
    absolute = _ABSOLUTE.search(text)
    if absolute is None:
        return None, None
    epoch = _absolute_epoch(absolute, now, local)
    if epoch is not None and not 0 < epoch - now <= MAX_RESET_SECONDS:
        epoch = None
    return absolute.group(0).strip(), epoch


def detect_usage_limit(provider: str, screen: str, *, now: float, tz: tzinfo | None = None) -> UsageLimitNotice | None:
    """The usage-limit notice at the bottom of ``screen`` for ``provider``, or ``None``.

    Only a line that begins with the provider's limit phrase counts; the most recent such line wins.
    """
    spec = bridge_providers.get(provider)
    start = spec.limit_notice_pattern if spec is not None else None
    if start is None or not screen:
        return None
    lines = [line.rstrip() for line in screen.splitlines() if line.strip()][-SCREEN_TAIL_LINES:]
    for index in range(len(lines) - 1, -1, -1):
        if not start.match(lines[index]):
            continue
        sentence = " ".join(re.sub(bridge_providers._DECORATION, "", line) for line in lines[index:index + 1 + CONTINUATION_LINES])
        reset_text, reset_epoch = parse_reset(sentence, now, tz)
        return UsageLimitNotice(provider, sentence[:NOTICE_CHARS], reset_text, reset_epoch)
    return None
