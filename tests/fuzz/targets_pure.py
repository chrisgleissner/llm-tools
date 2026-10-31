"""Fuzz targets for pure (curses-free) Bridge code: input primitives, documents, themes, events."""

from __future__ import annotations

import curses
import itertools
import random
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

from llm_tools import bridge_documents, bridge_format, bridge_theme, bridge_tui
from llm_tools.bridge_input import (
    BracketedPaste,
    TextBuffer,
    cell_slice,
    cell_width,
    input_view,
    normalize_paste_newlines,
    offset_for_cells,
    wrapped_row_for_column,
)

from . import generators as gen
from .harness import FuzzFailure, Sandbox, display_width, note

# ------------------------------------------------------------------- bridge_input


def target_input_primitives(rng: random.Random, sb: Sandbox) -> None:
    """cell_width / cell_slice / input_view / offsets never raise and respect the requested width."""
    text = gen.chaotic_text(rng, 60)
    note("text", text)
    total = cell_width(text)
    if total < 0:
        raise FuzzFailure(f"negative cell width {total}")
    start = rng.choice([0, 0, 1, 2, 7, 40, 10 ** 6])
    width = rng.choice([0, 1, 2, 3, 5, 8, 20, 80, 10 ** 6])
    clipped = cell_slice(text, start, width)
    if text.isprintable() or "\t" not in text:
        measured = display_width(clipped.expandtabs(8))
        if measured > width:
            raise FuzzFailure(f"cell_slice(start={start}, width={width}) is {measured} cells wide: {clipped!r}")
    cell_slice(text, rng.randint(-5, 50), rng.randint(-5, 50))  # odd arguments: no exception
    cursor = rng.choice([-5, 0, 1, len(text) // 2, len(text), len(text) + 10, 10 ** 9])
    view_width = rng.choice([-1, 0, 1, 2, 3, 10, 80])
    view, column = input_view(text, cursor, view_width)
    limit = max(1, view_width)
    if not 0 <= column <= limit - 1:
        raise FuzzFailure(f"input_view caret column {column} outside 0..{limit - 1}")
    if display_width(view.expandtabs(8)) > limit - 1 and "\t" not in text:
        raise FuzzFailure(f"input_view text is {display_width(view)} cells wide for width {view_width}: {view!r}")
    offset = offset_for_cells(text, rng.randint(-3, 200))
    if not 0 <= offset <= len(text):
        raise FuzzFailure(f"offset_for_cells returned {offset} for {len(text)} characters")
    row, cells = wrapped_row_for_column(text, rng.choice([-1, 0, len(text), 10 ** 9]), rng.choice([-2, 0, 1, 5, 80]))
    if row < 0 or cells < 0:
        raise FuzzFailure(f"wrapped_row_for_column returned {(row, cells)}")
    normalized = normalize_paste_newlines(text)
    if "\r" in normalized.replace("\r\n", ""):
        raise FuzzFailure("a lone carriage return survived newline normalisation")


def _feed_all(decoder: BracketedPaste, chunks: list[bytes]) -> list[str]:
    results = []
    for chunk in chunks:
        result = decoder.feed(chunk)
        if result is not None:
            results.append(result)
            decoder.reset()
        elif decoder.done and decoder.error:
            decoder.reset()
    return results


def target_bracketed_paste(rng: random.Random, sb: Sandbox) -> None:
    """A valid paste split across any reads round-trips exactly; garbage never raises or grows without bound."""
    # 1. valid transactions
    payload = gen.valid_text(rng, 100)
    if gen.PASTE_END.decode() in payload:
        payload = payload.replace("\x1b[201~", "")
    note("payload", payload)
    data = gen.PASTE_START + payload.encode("utf-8") + gen.PASTE_END
    chunks = gen.read_chunks(rng, data)
    decoder = BracketedPaste()
    results = _feed_all(decoder, chunks)
    expected = normalize_paste_newlines(payload)
    if results != [expected]:
        raise FuzzFailure(f"valid paste did not round-trip: got {results!r}, expected {[expected]!r}")
    # 2. size limit: oversize payload is refused, memory stays bounded
    limit = rng.choice([8, 64, 500])
    small = BracketedPaste(limit)
    big = bytes(rng.choice(b"abcdefgh \n") for _ in range(rng.randint(limit + 1, limit * 40)))
    stream = gen.PASTE_START + big + (gen.PASTE_END if rng.random() < 0.6 else b"")
    peak = 0
    outputs = []
    for chunk in gen.read_chunks(rng, stream):
        result = small.feed(chunk)
        peak = max(peak, len(small.pending))
        if result is not None:
            outputs.append(result)
    if outputs:
        raise FuzzFailure(f"oversize paste ({len(big)} > {limit} bytes) was accepted")
    if peak > limit + len(gen.PASTE_END) + 4096:
        raise FuzzFailure(f"pending buffer grew to {peak} bytes for a {limit}-byte limit")
    # 3. arbitrary bytes in arbitrary chunks never raise and can always be reset
    wild = BracketedPaste(rng.choice([16, 4096]))
    raw = gen.raw_byte_stream(rng, 600)
    note("raw", raw)
    for chunk in gen.read_chunks(rng, raw):
        wild.feed(chunk)
        if rng.random() < 0.05:
            wild.interrupt()
        if rng.random() < 0.05:
            wild.reset()
    wild.reset()
    if wild.waiting or wild.active:
        raise FuzzFailure("reset left the decoder waiting")
    follow = wild.feed(gen.PASTE_START + b"ok" + gen.PASTE_END)
    if follow != "ok":
        raise FuzzFailure(f"decoder unusable after reset: {follow!r}")


def target_text_buffer(rng: random.Random, sb: Sandbox) -> None:
    """TextBuffer edits keep the caret inside the text and the text inside its byte limit."""
    limit = rng.choice([4, 32, 300, 1_048_576])
    initial = gen.valid_text(rng, 10).encode("utf-8")[:limit].decode("utf-8", "ignore")
    buffer = TextBuffer(initial, rng.randint(-5, 50), limit)
    for _ in range(rng.randint(1, 40)):
        action = rng.choice(["insert", "paste", "move", "backspace", "delete", "clear"])
        before = (buffer.text, buffer.cursor)
        if action == "insert":
            buffer.insert(gen.valid_text(rng, 5))
        elif action == "paste":
            value = gen.valid_text(rng, 40)
            accepted, received = buffer.paste(value)
            if received != len(value.encode("utf-8")):
                raise FuzzFailure("paste reported the wrong byte count")
            if accepted and buffer.text != before[0][: before[1]] + value + before[0][before[1]:]:
                raise FuzzFailure("accepted paste did not insert verbatim at the caret")
            if not accepted and (buffer.text, buffer.cursor) != before:
                raise FuzzFailure("rejected paste changed the buffer")
        elif action == "move":
            buffer.move(rng.randint(-10, len(buffer.text) + 10))
        elif action == "backspace":
            buffer.backspace()
        elif action == "delete":
            buffer.delete()
        else:
            buffer.clear()
        if not 0 <= buffer.cursor <= len(buffer.text):
            raise FuzzFailure(f"caret {buffer.cursor} outside text of {len(buffer.text)} characters after {action}")
        if buffer.size_bytes > limit:
            raise FuzzFailure(f"buffer holds {buffer.size_bytes} bytes, limit {limit}")


# ---------------------------------------------------------------------- documents


def _unavoidable(row: str, width: int) -> bool:
    """A single character wider than the whole viewport cannot be wrapped to fit."""
    return width < 2 and len([char for char in row if display_width(char)]) <= 1


def target_documents(rng: random.Random, sb: Sandbox) -> None:
    """DocumentViewer navigation, search and layout at random sizes; wrapped rows fit the width."""
    source = (gen.markdownish(rng) if rng.random() < 0.5 else gen.valid_text(rng, 150))[:80000]
    note("source", source)
    heavy = len(source) > 8000 or source.count("\n") > 150 or any(len(line) > 1500 for line in source.split("\n"))
    viewer = bridge_documents.DocumentViewer("title " + gen.plain_text(rng, 10), source)
    for _ in range(rng.randint(1, 25)):
        height, width = gen.terminal_size(rng)
        action = rng.choice(["visible", "visible", "toggle_rendered", "toggle_wrap", "resize", "search", "scroll", "page", "home", "end", "headings", "lines"])
        if action == "visible":
            rows = viewer.visible(height, width)
            if len(rows) > max(1, height):
                raise FuzzFailure(f"visible() returned {len(rows)} rows for height {height}")
            if viewer.wrap and not viewer.rendered:
                for row in rows:
                    if display_width(row) > max(1, width) and not _unavoidable(row, width):
                        raise FuzzFailure(f"wrapped row is {display_width(row)} cells wide at width {width}: {row!r}")
        elif action == "toggle_rendered":
            if heavy and rng.random() < 0.85:
                continue  # Rich needs seconds for these and competes for the interpreter lock; the background path is exercised by 15%
            viewer.toggle_rendered()
        elif action == "toggle_wrap":
            viewer.toggle_wrap()
        elif action == "resize":
            viewer.resize(width)
        elif action == "search":
            query = gen.plain_text(rng, 4) if rng.random() < 0.6 else rng.choice(source.split() or ["x"])
            viewer.search(query, reverse=rng.random() < 0.5)
        elif action == "scroll":
            viewer.scroll(rng.randint(-30, 30), height)
        elif action == "page":
            viewer.page(rng.choice([-1, 1]), height)
        elif action == "home":
            viewer.home()
        elif action == "end":
            viewer.end(height)
        elif action == "headings":
            viewer.headings()
        else:
            viewer.lines
        if viewer.top < 0:
            raise FuzzFailure(f"viewer scrolled to a negative row {viewer.top}")
    for thread in threading.enumerate():  # sources over 32 KB render in a background thread; do not let them pile up across cases
        if thread.name == "llm-bridge-markdown":
            thread.join(3)
    width = rng.choice([1, 2, 5, 7, 20, 80])
    for line in source.split("\n")[:20]:
        for row in bridge_documents._wrap_cells(line, width):
            if display_width(row) > width and not _unavoidable(row, width):
                raise FuzzFailure(f"_wrap_cells row {display_width(row)} > {width}: {row!r}")


def target_markdown(rng: random.Random, sb: Sandbox) -> None:
    """render_markdown and _reflow_markdown on markdown-ish chaos: no exception, rows fit the width."""
    text = "\n".join(line[:1500] for line in gen.markdownish(rng)[:20000].split("\n")[:150])  # Rich needs seconds for one very long line; the viewer renders such a source in a thread
    note("markdown", text)
    rendered = bridge_documents.render_markdown(text)
    if not isinstance(rendered, str):
        raise FuzzFailure("render_markdown did not return text")
    width = rng.choice([1, 5, 8, 12, 20, 40, 80, 200])
    limit = rng.choice([0, 1, 5, 50, 500])
    rows = bridge_tui._reflow_markdown(text, width, limit)
    effective = max(8, width)
    if len(rows) > max(limit, 0) and limit > 0:
        raise FuzzFailure(f"_reflow_markdown returned {len(rows)} rows for limit {limit}")
    for line, role in rows:
        if "\n" in line:
            raise FuzzFailure(f"reflow row contains a newline: {line!r}")
        if display_width(line.expandtabs(8)) > effective:
            raise FuzzFailure(f"reflow row is {display_width(line)} cells wide at width {width}: {line!r}")
    segments = bridge_documents.render_markdown_segments(text, width, rng.random() < 0.5)
    if not segments:
        raise FuzzFailure("render_markdown_segments returned no rows")


# ------------------------------------------------------------------------- theme

_COUNTER = itertools.count()
_ROLES = list(bridge_theme.ROLES)
_VALID_STYLES = ["1;38;5;203", "#ff8800", "red", "bright-cyan", "fg:12", "bg:#112233", "bold underline", "dim", "reverse", "38;5;40", "fg:#00ff00 bold", "normal"]
_INVALID_STYLES = ["zzz", "#gggggg", "fg:", "not a colour at all", "12345678", "bg:purplish", "\x00", "1;38;5;9999", "fg:#12"]


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def target_theme_config(rng: random.Random, sb: Sandbox) -> None:
    """Random [bridge.theme] TOML and environment overrides: never raises; invalid values warn, valid ones do not."""
    lines = ["[bridge.theme]"]
    expectations: dict[str, bool] = {}  # role -> should warn
    if rng.random() < 0.6:
        name_value = rng.choice(['"dark"', '"light"', '"mono"', '"nonsense"', '5', '"hc"', '[1]', _toml_string(gen.plain_text(rng, 10))])
        lines.append(f"name = {name_value}")
    if rng.random() < 0.4:
        background_value = rng.choice(['"dark"', '"light"', '"purple"', '3', 'true'])
        lines.append(f"background = {background_value}")
    if rng.random() < 0.8:
        lines.append("[bridge.theme.roles]")
        for role in rng.sample(_ROLES, rng.randint(0, min(6, len(_ROLES)))):
            kind = rng.choice(["valid", "valid", "invalid", "table_ok", "table_bad", "wrong_type"])
            if kind == "valid":
                lines.append(f"{role} = {_toml_string(rng.choice(_VALID_STYLES))}")
                expectations[role] = False
            elif kind == "invalid":
                lines.append(f"{role} = {_toml_string(rng.choice(_INVALID_STYLES))}")
                expectations[role] = True
            elif kind == "table_ok":
                lines.append(f'{role} = {{ color = "#aabbcc", bold = true }}')
                expectations[role] = False
            elif kind == "table_bad":
                bad_table = rng.choice(['{ colour = "red" }', "{ color = 5 }", '{ bold = "yes" }', "{ bg = 3 }"])
                lines.append(f"{role} = {bad_table}")
                expectations[role] = True
            else:
                lines.append(f"{role} = {rng.choice(['42', 'true', '[1, 2]', '1.5'])}")
                expectations[role] = True
        if rng.random() < 0.3:
            lines.append(f"{_toml_string(gen.plain_text(rng, 8))} = \"red\"")
    config_text = "\n".join(lines) + "\n"
    broken = rng.random() < 0.15
    if broken:
        config_text = config_text[: rng.randint(0, len(config_text))] + rng.choice(["", "[[", "= =", "\x00", "'''"])
        expectations = {}  # the damaged file no longer says what was generated
    note("config", config_text)
    path = sb.root / f"theme-{next(_COUNTER)}.toml"  # a new path each time: the config loader caches by (path, mtime)
    path.write_text(config_text, encoding="utf-8")
    env = {"LLM_TOOLS_CONFIG": str(path), "TERM": rng.choice(["xterm-256color", "xterm", "dumb", "linux", ""]), "HOME": str(sb.home)}
    env_expect: dict[tuple[str, str], bool] = {}
    for _ in range(rng.randint(0, 4)):
        role = rng.choice(_ROLES)
        prefix = rng.choice(["LLM_BRIDGE_COLOR_", "LLM_TOOLS_COLOR_"])
        good = rng.random() < 0.5
        env[prefix + role.upper()] = rng.choice(_VALID_STYLES) if good else rng.choice(_INVALID_STYLES)
        env_expect[(prefix, role)] = not good
    for key in rng.sample(["LLM_BRIDGE_THEME", "LLM_BRIDGE_BACKGROUND", "COLORFGBG", "NO_COLOR", "LLM_USAGE_NO_COLOR", "LLM_BRIDGE_COLOR_NOPE"], rng.randint(0, 4)):
        env[key] = rng.choice(["dark", "light", "mono", "15;0", "0;15", "garbage", "", gen.plain_text(rng, 12), "\ud800"])
    note("env", env)
    try:
        tomllib.loads(config_text)
        parses = True
    except (tomllib.TOMLDecodeError, ValueError):
        parses = False
    bridge_theme.reset_cache()
    resolved = bridge_theme.resolve(env, probe=False)
    if set(resolved.styles) != set(_ROLES):
        raise FuzzFailure("resolved theme lost roles")
    for role in _ROLES:
        resolved.sgr(role)  # rendering a style never raises
    text = bridge_theme.format_theme_report(resolved, colors=rng.choice([8, 16, 256]), color=rng.random() < 0.5)
    if not isinstance(text, str):
        raise FuzzFailure("theme report is not text")
    bridge_theme.describe(resolved, colors=256)
    warnings = list(resolved.warnings)
    unreadable = any("could not be read" in warning for warning in warnings)
    if parses and not unreadable:
        for role, should_warn in expectations.items():
            mentioned = any(warning.startswith(f"[bridge.theme.roles] {role}:") for warning in warnings)
            if should_warn and not mentioned:
                raise FuzzFailure(f"invalid config value for role {role!r} produced no warning: {warnings}")
            if not should_warn and mentioned:
                raise FuzzFailure(f"valid config value for role {role!r} produced a warning: {warnings}")
    for (prefix, role), should_warn in env_expect.items():
        mentioned = any(warning.startswith(f"{prefix}{role.upper()}:") for warning in warnings)
        if should_warn and not mentioned:
            raise FuzzFailure(f"invalid environment value for {prefix}{role.upper()} produced no warning: {warnings}")
        if not should_warn and mentioned:
            raise FuzzFailure(f"valid environment value for {prefix}{role.upper()} produced a warning: {warnings}")
    if not parses and not unreadable:
        raise FuzzFailure(f"unparseable config produced no warning: {warnings}")


def target_theme_curses(rng: random.Random, sb: Sandbox) -> None:
    """initialize_colors against a stubbed curses with random terminal capabilities."""
    from .harness import Patcher, fake_curses

    with Patcher() as patch:
        with fake_curses(patch):
            patch.setattr(curses, "has_colors", lambda: rng.random() < 0.8)
            patch.setattr(curses, "COLORS", rng.choice([0, 1, 8, 16, 88, 256, 65536]))
            patch.setattr(curses, "COLOR_PAIRS", rng.choice([0, 1, 8, 16, 64, 256, 65536]))
            patch.setattr(curses, "color_pair", lambda n: (n & 0xFF) << 8)

            def init_pair(pair: int, fg: int, bg: int) -> None:
                if not (1 <= pair < max(2, curses.COLOR_PAIRS)) or not (-1 <= fg < curses.COLORS) or not (-1 <= bg < curses.COLORS):
                    raise curses.error("init_pair() returned ERR")

            patch.setattr(curses, "init_pair", init_pair)
            env = {"TERM": rng.choice(["xterm-256color", "xterm", "dumb", "linux"]), "LLM_BRIDGE_THEME": rng.choice(["dark", "light", "mono", "high-contrast", "neutral", "auto"]),
                   "COLORFGBG": rng.choice(["", "0;15", "15;0"]), "HOME": str(sb.home)}
            if rng.random() < 0.3:
                env["NO_COLOR"] = "1"
            bridge_theme.reset_cache()
            bridge_tui.initialize_colors(env, is_tty=rng.random() < 0.8)
            for role in _ROLES + ["selected", "secondary", "error.quiet", "nonexistent", ""]:
                if not isinstance(bridge_tui.role_attr(role), int):
                    raise FuzzFailure(f"role_attr({role!r}) is not an int")
    bridge_tui.initialize_colors({"TERM": "dumb"}, is_tty=False)


# ------------------------------------------------------------------------- events

_EVENT_KINDS = ["mission-created", "task-added", "task-ready", "task-status", "mission-status", "operation-prepared", "operation-running", "operation-exited",
                "provider-selection", "capacity-refresh", "command-applied", "command-rejected", "native-session-restoration", "operation-uncertain", "repository-checkpoint",
                "task-rejected", "review-hold", "review-hold-released", "mission-colour", "controller-heartbeat", "steering-applied", "weird", "", "x" * 300]


def _random_events(rng: random.Random, count: int) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for number in range(count):
        event: dict[str, object] = {"id": number + 1, "kind": rng.choice(_EVENT_KINDS) if rng.random() < 0.85 else gen.json_value(rng, 3)}
        for key in ("task_id", "operation_id", "created_at", "task_ordinal", "summary"):
            if rng.random() < 0.6:
                event[key] = gen.json_value(rng, 3)
        if rng.random() < 0.9:
            event["data"] = {key: gen.json_value(rng, 1) for key in rng.sample(
                ["from", "to", "reason", "title", "name", "route", "provider", "station", "model", "selected_model", "exit_code", "task_status", "session_id", "restoration_state",
                 "process_pid", "route_id", "rotation_reason", "command", "summary", "value", "decisions", "revision", "task_id", "operation_id", "evidence_count"], rng.randint(0, 8))}
            if rng.random() < 0.2:
                event["data"] = gen.json_value(rng, 1)
        events.append(event)
    return events


def target_event_summaries(rng: random.Random, sb: Sandbox) -> None:
    """Event humanisation yields single, bounded lines for any kind and payload."""
    events = _random_events(rng, rng.choice([1, 3, 12, 60]))
    note("events", events)
    projected = bridge_tui._project_events(events, 200)
    numbers = bridge_tui._task_numbers({"tasks": gen.json_value(rng, 2) if rng.random() < 0.3 else [{"id": 1, "ordinal": 7}]})
    for event in projected:
        summary = bridge_tui._event_summary(event)
        text, plumbing = bridge_tui._humanize_event(event, numbers)
        for label, line in (("_event_summary", summary), ("_humanize_event", text)):
            if not isinstance(line, str) or "\n" in line or "\r" in line or any(ord(ch) < 32 for ch in line):
                raise FuzzFailure(f"{label} is not a clean single line: {line!r}")
            if len(line) > 900:
                raise FuzzFailure(f"{label} returned {len(line)} characters")
    for entry in bridge_tui._activity_entries(projected, numbers):
        if "\n" in entry.text or len(entry.text) > 1000:
            raise FuzzFailure(f"activity row is not a bounded single line: {entry.text[:200]!r}")
    # the CLI formatter works on raw store events whose payload is JSON but unconstrained
    cli_events = [{**event, "data": event.get("data") if isinstance(event.get("data"), dict) else {}} for event in events]
    rendered = bridge_format.format_events(cli_events)
    if not isinstance(rendered, str):
        raise FuzzFailure("format_events did not return text")
    for event in cli_events:
        line = bridge_format._event_summary(event.get("data"))
        if "\n" in line or len(line) > 140:
            raise FuzzFailure(f"bridge_format._event_summary line not bounded/single: {line[:200]!r}")


# ----------------------------------------------------------------------- regular expressions

def _bridge_patterns() -> list[tuple[str, Any]]:
    import importlib
    import re

    found = []
    for module_name in ("bridge", "bridge_tui", "bridge_documents", "bridge_launcher", "bridge_format", "bridge_monitor", "bridge_store", "bridge_help", "bridge_switcher",
                        "bridge_glyphs", "bridge_input", "bridge_theme", "bridge_finish", "bridge_progress", "bridge_sessions", "bridge_runtime", "bridge_doctor", "bridge_rotation",
                        "bridge_logs", "bridge_usage", "terminal", "bridge_actions", "bridge_viewer", "bridge_refresh"):
        try:
            module = importlib.import_module(f"llm_tools.{module_name}")
        except Exception:  # noqa: BLE001 - an optional module that cannot import is not this target's concern
            continue
        for name, value in vars(module).items():
            if isinstance(value, re.Pattern):
                found.append((f"{module_name}.{name}", value))
    return found


_STARTERS = ["", "# a", "## ", "- ", "* ", "1. ", "12) ", "| ", "```", "~~~", "> ", "task 1  ", "12:34:56  ", "Provider  ", "  ", "\t", "[", "(", "a ", "abc: ", "x=", "--"]
_ALPHABET = [" ", "\t", "#", "-", "*", "|", "`", ".", ")", "a", "1", ":", "·", "=", "/", "\\", "_", "~", "<", ">", "[", "]", "(", "\n", "x y", "  ", "12:34:56"]


def target_regex_time(rng: random.Random, sb: Sandbox) -> None:
    """Every compiled pattern in the Bridge modules matches adversarial text in at most quadratic time."""
    import time as clock

    patterns = _bridge_patterns()
    if not patterns:
        raise FuzzFailure("found no compiled patterns to test")
    name, pattern = rng.choice(patterns)
    prefix = rng.choice(_STARTERS) + "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 3)))
    unit = rng.choice(_ALPHABET) if rng.random() < 0.7 else "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 3)))
    tail = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 2))) + rng.choice(["", "x", "!", "\x00", "·"])
    note("pattern", (name, pattern.pattern[:120], prefix, unit, tail))
    # 300 repeats expose cubic backtracking at once; 1500 repeats expose quadratic work that a long input would turn into a freeze.
    for repeats, budget in ((300, 0.2), (1500, 2.5)):
        text = prefix + unit * repeats + tail
        if isinstance(pattern.pattern, bytes):
            text = text.encode("utf-8")
        for method in (pattern.match, pattern.search, pattern.fullmatch):
            started = clock.perf_counter()
            method(text)
            elapsed = clock.perf_counter() - started
            if elapsed > budget:
                raise FuzzFailure(f"{name} {method.__name__} took {elapsed:.2f}s on {len(text)} characters ({repeats} x {unit!r}): pattern {pattern.pattern[:100]!r}")


# ------------------------------------------------------------------------ text helpers

def target_text_helpers(rng: random.Random, sb: Sandbox) -> None:
    """Clipping, padding, age and duration helpers never raise and never return more cells than asked."""
    from llm_tools import bridge_glyphs, bridge_launcher, bridge_switcher

    text = gen.chaotic_text(rng, 80)
    note("text", text)
    width = rng.choice([-1, 0, 1, 2, 3, 5, 10, 24, 80, 300])
    for label, function in (
        ("launcher.truncate", bridge_launcher.truncate), ("launcher.middle_ellipsis", bridge_launcher.middle_ellipsis),
        ("switcher.clip", bridge_switcher.clip), ("switcher.middle_clip", bridge_switcher.middle_clip),
    ):
        if label == "switcher.middle_clip":
            text_in = text.replace("\x00", " ")
        else:
            text_in = text
        result = function(text_in, width)
        if not isinstance(result, str):
            raise FuzzFailure(f"{label} returned {type(result).__name__}")
        if display_width(result) > max(0, width) and not (width == 1 and display_width(result) == 2):
            raise FuzzFailure(f"{label}({width}) is {display_width(result)} cells: {result!r}")
        if label.startswith("switcher.clip") and ("\n" in result or "\r" in result):
            raise FuzzFailure(f"{label} kept a line break: {result!r}")
    bridge_launcher.short_path(text, width, home=rng.choice([None, "/home/x", "", "/"]))
    bridge_switcher.short_home(text, rng.choice([None, "/home/x", ""]))
    bridge_switcher.mission_label(text, gen.chaotic_text(rng, 30))
    bridge_switcher.pad(text, width)
    localized = bridge_glyphs.localize(text)
    if not isinstance(localized, str):
        raise FuzzFailure("localize did not return text")
    for stamp in (gen.json_value(rng, 3), text, rng.choice([0, -1, 10 ** 30, "2026-10-30T07:00:00Z", "garbage", None, float("nan")])):
        for function in (bridge_launcher.relative_age, bridge_format.age, bridge_tui._age, bridge_tui._relative_epoch, bridge_tui._local_clock):
            result = function(stamp)
            if result is not None and (not isinstance(result, str) or "\n" in result):
                raise FuzzFailure(f"{function.__name__}({stamp!r}) returned {result!r}")
    for seconds in (rng.choice([0, 1, 59, 60, 3599, 86400, 10 ** 12, -5, 0.5]), gen.json_value(rng, 3)):
        for function in (bridge_tui._span, bridge_tui._duration_seconds):
            function(seconds)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            for function in (bridge_format.duration, bridge_format.countdown):
                if seconds == seconds and abs(seconds) != float("inf"):
                    function(seconds)
