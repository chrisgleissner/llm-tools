"""Colour roles, state words and inline styling of console text."""

from __future__ import annotations

import functools
import re
from .bridge_vocab import MISSION_NEEDS_YOU_LABEL
from .bridge_glyphs import FINISHED_STATES, glyph, provider_role, state_look, state_text
from .bridge_draw import QUIET, Segment, _merge_segments
from .bridge_console_commands import _TASK_ACK


_DURATION_PART = re.compile(r"(\d+)\s*([dhms])\b")


_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _duration_seconds(value: object) -> int | None:
    """Seconds in text like `2d 10h 35m`, or None when it is not a plain duration."""
    text = str(value).strip().lstrip("~").strip()
    if len(text) > 80:  # far longer than any duration; the pattern is quadratic on a long run of digits
        return None
    parts = _DURATION_PART.findall(text)
    if not parts or _DURATION_PART.sub("", text).strip():
        return None
    return sum(int(number) * _DURATION_UNITS[unit] for number, unit in parts)


_FINISHED_STATES = FINISHED_STATES


_state_look = state_look


_state_text = state_text


def _state_segment(status: object) -> Segment:
    look = _state_look(status)
    return (_state_text(status), look[1] if look else "info")


_provider_role = provider_role


def _percent_role(percent: float) -> str:
    """Capacity role for a share of quota remaining: 0 is empty, up to 25% is low."""
    if percent <= 0:
        return "capacity_empty"
    return "capacity_low" if percent <= 25 else "capacity_ok"


_IDLE_WARN_SECONDS = 600


_DURATION = r"\d+[dhms](?: \d+[dhms])*"


_STATE_WORDS = "NEEDS YOU|RUNNING|READY|QUEUED|VERIFYING|REVIEW|COMPLETED|FAILED|BLOCKED|CANCELLED|PAUSED|PAUSING|STOPPED|STOPPING|CREATED|ACTIVE"


@functools.lru_cache(maxsize=4)
def _token_pattern(square: str, glyphs: tuple[str, ...]) -> re.Pattern[str]:
    state_glyphs = "|".join(re.escape(item) for item in glyphs)
    return re.compile(
        "|".join([
            rf"(?P<square>{re.escape(square)} )(?P<owner>[^\s/,;()]+)",
            r"(?P<cmd>(?<![\w/])/[a-z][a-z-]*(?![\w/.]))",
            r"(?P<key>\bCtrl-\S|\bF\d{1,2}\b)",
            rf"(?P<idle>\bno output(?: for)? {_DURATION})",
            rf"(?P<time>\b{_DURATION} ago\b|\bin {_DURATION}|\bresets (?:now|{_DURATION})|empty in ~{_DURATION} at this pace)",
            r"(?P<pct>\b\d+(?:\.\d+)?%)",
            r"(?P<past>\blasts past reset\b)",
            r"(?P<limited>\brate-limited\b)",
            rf"(?P<state>(?:(?:{state_glyphs}) )?\b(?:{_STATE_WORDS})\b)",
            r"(?P<taskid>(?<=\btask )\d+\b)",
            r"(?P<hexid>\b[0-9a-f]{8}\b)",
        ])
    )


_LOWER_STATES = r"(?<!not )\b(?:running|queued|ready|verifying|blocked|failed|completed|cancelled|paused|stopped|stopping)\b"


_CONTROLLER_WARN = r"silent for|not running|not started|no heartbeat recorded yet|pausing after the current provider invocation"


_PLAIN_PATTERNS = {
    "tasks": re.compile(_LOWER_STATES),
    "controller": re.compile(rf"{_LOWER_STATES}|\bactive\b|{_CONTROLLER_WARN}"),
    "strip": re.compile(rf"{_LOWER_STATES}|awaits approval|awaits your review|waiting for capacity|stopping after the current step"),
}


def _idle_role(text: str) -> str:
    """`muted`, or `warn` once the provider has been silent for ten minutes."""
    tail = text.split("no output", 1)[1].replace("for ", "", 1).strip()
    seconds = _duration_seconds(tail)
    return "warn" if seconds is not None and seconds >= _IDLE_WARN_SECONDS else "muted"


def _plain_words(text: str, base: str, context: str) -> list[Segment]:
    """Colour the lowercase state words and phrases that only some contexts use."""
    pattern = _PLAIN_PATTERNS.get(context)
    if pattern is None or not text:
        return [(text, base)]
    out: list[Segment] = []
    position = 0
    for match in pattern.finditer(text):
        out.append((text[position:match.start()], base))
        word = match.group(0)
        if word == "active":
            role = "state_running"
        elif word in {"awaits approval", "awaits your review"}:
            role = "state_review"
        elif word in {"waiting for capacity", "stopping after the current step"} or re.fullmatch(_CONTROLLER_WARN, word):
            role = "warn"
        else:
            look = _state_look(word)
            role = look[1] if look else base
            if context == "tasks" and role in {"state_review", "state_failed"}:
                role = base  # the Needs-you row is the one place that draws the eye
        out.append((word, role))
        position = match.end()
    out.append((text[position:], base))
    return out


def _inline_segments(text: str, base: str = "info", *, context: str = "generic") -> list[Segment]:
    """Colour the machine-generated parts of an informational line.

    Only system-written phrasing is tokenised (percentages, ages, state words,
    `/commands`, key names, ids and the provider square); user-written text such
    as task titles never goes through here.
    """
    glyphs = tuple(dict.fromkeys(glyph(name) for name in ("running", "waiting", "review", "done", "failed", "paused")))
    pattern = _token_pattern(glyph("square"), glyphs)
    out: list[Segment] = []
    position = 0
    for match in pattern.finditer(text):
        out.extend(_plain_words(text[position:match.start()], base, context))
        kind = match.lastgroup
        token = match.group(0)
        if match.group("square"):
            out.append((match.group("square").rstrip(), _provider_role(match.group("owner"))))
            out.append((" " + match.group("owner"), base))
        elif kind in {"cmd", "key"}:
            out.append((token, "key"))
        elif kind == "idle":
            out.append((token, _idle_role(token)))
        elif kind in {"time", "taskid", "hexid"}:
            out.append((token, "muted"))
        elif kind == "pct":
            out.append((token, _percent_role(float(token.rstrip("%")))))
        elif kind == "past":
            out.append((token, "capacity_ok"))
        elif kind == "limited":
            out.append((token, "capacity_empty"))
        else:  # state word, optionally behind its glyph
            look = _state_look(MISSION_NEEDS_YOU_LABEL if token.endswith(MISSION_NEEDS_YOU_LABEL) else token.split()[-1])
            role = look[1] if look else base
            if context == "needs" and role in {"state_review", "state_failed"}:
                role += QUIET
            out.append((token, role))
        position = match.end()
    out.extend(_plain_words(text[position:], base, context))
    return _merge_segments(out)


def _status_role(value: str) -> str:
    """Pick a semantic role for short status and response lines."""
    lowered = value.lower()
    if any(token in lowered for token in ("none ready", "no tasks ready")):
        return "dim"
    if any(token in lowered for token in ("not ready", "ready=false")):
        return "warn"
    if any(token in lowered for token in ("error", "failed", "rejected", "could not", "unavailable")):
        return "error"
    if any(token in lowered for token in ("warning", "blocked", "waiting", "rate limit", "paused", "retry")):
        return "warn"
    if _TASK_ACK.match(lowered) or any(token in lowered for token in ("applied:", "completed", "success", "usable", "ready", "passed")):
        return "ok"
    return "dim"
