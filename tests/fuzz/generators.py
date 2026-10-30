"""Seeded generators of chaotic input. Every function takes a ``random.Random``."""

from __future__ import annotations

import curses
import random
import string
from typing import Any

# --------------------------------------------------------------------------- text

_COMBINING = [chr(c) for c in range(0x0300, 0x0370)] + [chr(c) for c in range(0x1AB0, 0x1AC0)] + ["⃐", "⃝", "️", "︎"]
_CJK = [chr(c) for c in list(range(0x4E00, 0x4E40)) + list(range(0x3041, 0x3060)) + list(range(0xAC00, 0xAC20)) + list(range(0xFF01, 0xFF40))]
_EMOJI = ["😀", "🚀", "🧑", "🏽", "🇩", "🇪", "❤", "☕", "🔥", "🧠", "🫠", "\U0001F9D1‍\U0001F4BB", "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466", "🏳️‍🌈", "1️⃣"]
_BIDI = ["‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎", "‏", "؜"]
_ZERO_WIDTH = ["​", "‌", "‍", "⁠", "﻿", "­", " ", " ", "\u0085", "�", "￾", "￿", "\U0010ffff"]
_C0 = [chr(c) for c in range(0, 32)] + ["\x7f"]
_C1 = [chr(c) for c in range(0x80, 0xA0)]
_SURROGATES = [chr(c) for c in (0xD800, 0xD83D, 0xDBFF, 0xDC00, 0xDE00, 0xDFFF)]
_ASCII_WORDS = ["status", "task", "add", "--prompt", "pause", "the", "mission", "Fix", "tests", "README.md", "a", "b", "0", "1", "42", "-1", "--", "-", "/", "\\", "'", '"', "`", "$", "&&", "|", ";", "{", "}", "[", "]", "#", "*", "_", "~", "<", ">", "=", "%", "@", "!", "?", ":", ","]
_NEWLINES = ["\n", "\r", "\r\n", "\n\n", "\r\r", "\n\r", "\r\n\r\n"]


def _pick_fragment(rng: random.Random, *, controls: bool, surrogates: bool, nul: bool) -> str:
    kind = rng.choice(["ascii", "ascii", "ascii", "word", "word", "combining", "cjk", "emoji", "bidi", "zw", "newline", "space", "tab", "latin", "ctrl", "c1", "surrogate", "nul", "mixed"])
    if kind == "ascii":
        return rng.choice(string.printable[:94])
    if kind == "word":
        return rng.choice(_ASCII_WORDS)
    if kind == "combining":
        return rng.choice("aeoxN") + "".join(rng.choice(_COMBINING) for _ in range(rng.randint(1, 4)))
    if kind == "cjk":
        return rng.choice(_CJK)
    if kind == "emoji":
        return rng.choice(_EMOJI)
    if kind == "bidi":
        return rng.choice(_BIDI) + rng.choice(["abc", "אבג", "مرحبا", "x"])
    if kind == "zw":
        return rng.choice(_ZERO_WIDTH)
    if kind == "newline":
        return rng.choice(_NEWLINES)
    if kind == "space":
        return " " * rng.randint(1, 6)
    if kind == "tab":
        return "\t" * rng.randint(1, 3)
    if kind == "latin":
        return rng.choice("éüñßçøåÆŁ")
    if kind == "ctrl" and controls:
        return rng.choice(_C0[1:])
    if kind == "c1" and controls:
        return rng.choice(_C1)
    if kind == "surrogate" and surrogates:
        return rng.choice(_SURROGATES)
    if kind == "nul" and nul:
        return "\x00"
    if kind == "mixed":
        return rng.choice(_COMBINING) + rng.choice(_CJK) + rng.choice(_EMOJI)
    return rng.choice(string.ascii_letters)


def chaotic_text(rng: random.Random, max_len: int = 120, *, controls: bool = True, surrogates: bool = True, nul: bool = True, bounded: bool = False) -> str:
    """Random Unicode: combining marks, wide CJK, emoji/ZWJ, bidi, C0/C1, lone surrogates, NUL, newlines.

    Occasionally returns a very long line or a long run of newlines.
    """
    roll = rng.random()
    if bounded:
        roll = max(roll, 0.05)  # no very long lines or newline floods
    if roll < 0.03:
        unit = _pick_fragment(rng, controls=controls, surrogates=surrogates, nul=nul) or "x"
        return unit * rng.randint(2000, 20000)
    if roll < 0.05:
        return "".join(rng.choice(_NEWLINES) for _ in range(rng.randint(100, 800)))
    if roll < 0.20:
        return ""
    return "".join(_pick_fragment(rng, controls=controls, surrogates=surrogates, nul=nul) for _ in range(rng.randint(0, max_len)))


def plain_text(rng: random.Random, max_len: int = 80, *, bounded: bool = False) -> str:
    """Printable, single-line, valid-UTF-8 text including wide and combining characters."""
    text = chaotic_text(rng, max_len, controls=False, surrogates=False, nul=False, bounded=bounded)
    for newline in ("\n", "\r", " ", " ", "\x85"):
        text = text.replace(newline, " ")
    return text


def valid_text(rng: random.Random, max_len: int = 120, *, bounded: bool = False) -> str:
    """Text that encodes to UTF-8 (no lone surrogates)."""
    return chaotic_text(rng, max_len, surrogates=False, bounded=bounded)


def markdownish(rng: random.Random, max_lines: int = 40) -> str:
    """Markdown-flavoured chaos: headings, fences, tables, lists, links, bad nesting."""
    lines = []
    for _ in range(rng.randint(0, max_lines)):
        piece = valid_text(rng, 40).replace("\r", " ")
        kind = rng.choice(["h", "h", "list", "olist", "fence", "table", "quote", "link", "plain", "plain", "code", "html", "hr", "nest", "blank"])
        if kind == "h":
            lines.append("#" * rng.randint(1, 8) + rng.choice([" ", "", "  "]) + piece)
        elif kind == "list":
            lines.append(rng.choice(["- ", "* ", "+ ", "  - ", "    - "]) + piece)
        elif kind == "olist":
            lines.append(f"{rng.randint(0, 999999999999)}. {piece}")
        elif kind == "fence":
            lines.append(rng.choice(["```", "~~~", "```python", "````", "```" + piece]))
        elif kind == "table":
            lines.append("| " + " | ".join(piece[: rng.randint(0, 9)] for _ in range(rng.randint(1, 8))) + " |")
            if rng.random() < 0.5:
                lines.append("|" + "|".join(rng.choice(["---", ":-:", "--:", ""]) for _ in range(rng.randint(1, 8))) + "|")
        elif kind == "quote":
            lines.append("> " * rng.randint(1, 6) + piece)
        elif kind == "link":
            lines.append(f"[{piece}]({rng.choice(['http://x', '', '#a', 'javascript:alert(1)', piece])}) ![{piece}](x) <{piece}>")
        elif kind == "code":
            lines.append("`" * rng.randint(1, 5) + piece + "`" * rng.randint(0, 5))
        elif kind == "html":
            lines.append(rng.choice(["<div>", "</p>", "<!--", "-->", "<script>", "&amp;&#x41;&bogus;"]) + piece)
        elif kind == "hr":
            lines.append(rng.choice(["---", "***", "___", "- - -"]))
        elif kind == "nest":
            lines.append("  " * rng.randint(0, 40) + "- " + "> " * rng.randint(0, 10) + piece)
        elif kind == "blank":
            lines.append("")
        else:
            lines.append(piece)
    return rng.choice(["\n", "\n", "\r\n", "\r"]).join(lines)


def invalid_utf8_bytes(rng: random.Random, max_len: int = 80) -> bytes:
    """Bytes mixing valid UTF-8, truncated multibyte sequences, overlongs, lone continuation bytes."""
    out = bytearray()
    for _ in range(rng.randint(0, max_len)):
        kind = rng.choice(["ascii", "valid", "trunc", "cont", "overlong", "high", "random", "nul"])
        if kind == "ascii":
            out += rng.choice(string.printable).encode()
        elif kind == "valid":
            out += valid_text(rng, 3, bounded=True).encode("utf-8")
        elif kind == "trunc":
            out += "€𝄞".encode()[: rng.randint(1, 6)]
        elif kind == "cont":
            out.append(rng.randint(0x80, 0xBF))
        elif kind == "overlong":
            out += rng.choice([b"\xc0\xaf", b"\xe0\x80\xaf", b"\xf0\x80\x80\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"])
        elif kind == "high":
            out.append(rng.randint(0xF5, 0xFF))
        elif kind == "nul":
            out.append(0)
        else:
            out.append(rng.randint(0, 255))
    return bytes(out)


# ------------------------------------------------------------------- key streams

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class PastedStr(str):
    """A string known to be bracketed-paste content (so tests can tell paste from typing)."""

_NAMED = ["KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END", "KEY_PPAGE", "KEY_NPAGE", "KEY_BACKSPACE", "KEY_DC", "KEY_ENTER", "KEY_BTAB", "KEY_SLEFT", "KEY_SRIGHT", "KEY_RESIZE", "KEY_MOUSE", "KEY_ESCAPE"] + [f"KEY_F{n}" for n in range(0, 14)] + ["KEY_Fx", "KEY_F", "KEY_F99999999999999999999"]
_CTRL = [chr(c) for c in range(1, 32)] + ["\x7f"]
_CURSES_INTS = [getattr(curses, name) for name in (
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_HOME", "KEY_END", "KEY_PPAGE", "KEY_NPAGE", "KEY_BACKSPACE",
    "KEY_DC", "KEY_ENTER", "KEY_BTAB", "KEY_SLEFT", "KEY_SRIGHT", "KEY_RESIZE", "KEY_MOUSE", "KEY_IC", "KEY_F1", "KEY_F2", "KEY_F12",
)] + [0, -1, 1 << 30, 99999]


def semantic_key(rng: random.Random) -> object:
    """One key as ``_ConsoleState.handle_key`` receives it (str or KEY_ name)."""
    roll = rng.random()
    if roll < 0.40:
        return rng.choice(string.printable[:94] + " ")
    if roll < 0.55:
        return rng.choice(_NAMED)
    if roll < 0.75:
        return rng.choice(_CTRL)
    if roll < 0.80:
        return rng.choice(["\n", "\r", "KEY_ENTER"])
    if roll < 0.85:
        return rng.choice(["y", "Y", "n", "N", "\x1b", "q", "j", "k", "?", "/", "v", "o", "l", "e", "m", "w", "h", "g", "G", "s", "c"])
    if roll < 0.95:
        return plain_text(rng, 3, bounded=True)[:3] or "é"
    return rng.choice(["[", "[A", "\x1b[", "\x1bOP", "[200~", "[201~"])


def read_chunks(rng: random.Random, data: bytes) -> list[bytes]:
    """Split ``data`` at random boundaries, as a terminal read might."""
    chunks = []
    index = 0
    while index < len(data):
        size = rng.choice([1, 1, 2, 3, 5, 8, 64, 4096])
        chunks.append(data[index:index + size])
        index += size
    return chunks or [b""]


def mouse_report(rng: random.Random, *, buttons: tuple[int, ...] | None = None) -> bytes:
    """An SGR mouse report with absurd coordinates and buttons."""
    button = rng.choice(buttons or (0, 1, 2, 3, 32, 35, 64, 65, 66, 67, 68, 96, 127, 255, 99999, -1))
    x = rng.choice([0, 1, 5, 80, 120, 2000, 65535, 99999999999, -5])
    y = rng.choice([0, 1, 5, 24, 45, 2000, 65535, 99999999999, -5])
    return b"\x1b[<%d;%d;%d%s" % (button, x, y, rng.choice([b"M", b"m", b"M", b"X", b""]))


def escape_fragment(rng: random.Random, *, mouse_buttons: tuple[int, ...] | None = None) -> bytes:
    """A (possibly broken) escape sequence."""
    kind = rng.choice(["csi", "csi", "ss3", "trunc", "mouse", "paste_start", "paste_end", "fkey", "alt", "osc", "dcs", "lone", "private"])
    if kind == "csi":
        params = ";".join(str(rng.choice([0, 1, 2, 5, 99, 65536, 10 ** 12])) for _ in range(rng.randint(0, 4)))
        return b"\x1b[" + params.encode() + bytes([rng.choice(b"ABCDHFPQRSZmJKtu~@`")])
    if kind == "ss3":
        return b"\x1bO" + bytes([rng.choice(b"ABCDHFPQRS")])
    if kind == "trunc":
        return rng.choice([b"\x1b", b"\x1b[", b"\x1b[1", b"\x1b[1;", b"\x1b[<", b"\x1b[<0;", b"\x1b[200", b"\x1b[201", b"\x1bO"])
    if kind == "mouse":
        return mouse_report(rng, buttons=mouse_buttons)
    if kind == "paste_start":
        return PASTE_START
    if kind == "paste_end":
        return PASTE_END
    if kind == "fkey":
        return b"\x1b[" + str(rng.choice([11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24, 25, 99])).encode() + b"~"
    if kind == "alt":
        return b"\x1b" + bytes([rng.randint(0x20, 0x7E)])
    if kind == "osc":
        return b"\x1b]" + valid_text(rng, 10, bounded=True).encode("utf-8", "replace") + rng.choice([b"\x07", b"\x1b\\", b""])
    if kind == "dcs":
        return b"\x1bP" + valid_text(rng, 10, bounded=True).encode("utf-8", "replace") + rng.choice([b"\x1b\\", b""])
    if kind == "private":
        return b"\x1b[?" + rng.choice([b"2004h", b"2004l", b"1000;1006h", b"1u"])
    return b"\x1b"


def paste_block(rng: random.Random, *, terminate: bool | None = None) -> bytes:
    """A bracketed paste (sometimes nested, unterminated or carrying binary junk)."""
    terminated = rng.random() < 0.8 if terminate is None else terminate
    body = valid_text(rng, 80, bounded=True).encode("utf-8") if rng.random() < 0.8 else invalid_utf8_bytes(rng, 60)
    if rng.random() < 0.15:
        body += PASTE_START + valid_text(rng, 10, bounded=True).encode("utf-8")  # nested start marker inside the payload
    return PASTE_START + body + (PASTE_END if terminated else b"")


def raw_byte_stream(rng: random.Random, max_bytes: int = 400, *, avoid: frozenset[int] = frozenset(), mouse_buttons: tuple[int, ...] | None = None) -> bytes:
    """Terminal input: text, escape fragments, pastes, mouse reports, function keys, Ctrl chars."""
    out = bytearray()
    while len(out) < max_bytes and rng.random() < 0.97:
        kind = rng.choice(["text", "text", "esc", "esc", "paste", "ctrl", "junk", "mouse"])
        if kind == "text":
            out += valid_text(rng, 20, bounded=True).encode("utf-8")
        elif kind == "esc":
            out += escape_fragment(rng, mouse_buttons=mouse_buttons)
        elif kind == "paste":
            out += paste_block(rng)
        elif kind == "ctrl":
            out.append(rng.randint(0, 31))
        elif kind == "mouse":
            out += mouse_report(rng, buttons=mouse_buttons)
        else:
            out += invalid_utf8_bytes(rng, 12)
    data = bytes(out)
    if avoid:
        data = bytes(byte for byte in data if byte not in avoid)
    return data


def wch_stream(rng: random.Random, count: int = 80) -> list[Any]:
    """Values a curses ``get_wch`` loop could see, plus harness pseudo-events.

    Items are single characters, occasional multi-character strings (terminals that
    deliver a whole sequence at once), curses integer key codes, and the tuples
    ``("mouse", x, y, buttons)``, ``("resize", h, w)``, ``("pause", seconds)``, ``("invalid",)``.
    """
    items: list[Any] = []
    while len(items) < count:
        kind = rng.choice(["char", "char", "char", "ctrl", "esc", "paste", "int", "mouse", "resize", "pause", "named", "text", "enter", "yn", "invalid"])
        if kind == "invalid":
            items.append(("invalid",))  # an input sequence curses cannot decode: get_wch raises ValueError
        elif kind == "char":
            items.append(rng.choice(string.printable[:94] + " "))
        elif kind == "ctrl":
            items.append(rng.choice(_CTRL))
        elif kind == "esc":
            for byte in escape_fragment(rng).decode("latin-1"):
                items.append(byte if rng.random() < 0.85 else byte + rng.choice("[~M"))
        elif kind == "paste":
            terminated = rng.random() < 0.75
            block = paste_block(rng, terminate=terminated).decode("utf-8", "replace")
            # Only a framed paste is known to be paste content: after an unterminated one the operator may have taken over.
            wrap = PastedStr if terminated else str
            if rng.random() < 0.3:
                items.append(wrap(block))  # whole transaction as one value
            else:
                items.extend(wrap(char) for char in block)
        elif kind == "int":
            items.append(rng.choice(_CURSES_INTS))
        elif kind == "mouse":
            items.append(("mouse", rng.choice([-1, 0, 3, 30, 79, 500, 70000]), rng.choice([-1, 0, 1, 2, 5, 23, 24, 500, 70000]),
                          rng.choice([0, 1, 2, 4, 8, 0x10000, 0x200000, 0x4000000 | 0x10000, 0x200000 | 0x4000000, getattr(curses, "BUTTON1_CLICKED", 4), getattr(curses, "BUTTON1_DOUBLE_CLICKED", 8), 1 << 30, -1])))
            items.append(curses.KEY_MOUSE)
        elif kind == "resize":
            items.append(("resize", *terminal_size(rng)))
            items.append(curses.KEY_RESIZE)
        elif kind == "pause":
            items.append(("pause", rng.choice([0.01, 0.07, 0.2, 0.2, 2.5, 5.0])))
        elif kind == "named":
            items.append(rng.choice(_CURSES_INTS))
        elif kind == "text":
            items.extend(plain_text(rng, 12, bounded=True))
        elif kind == "enter":
            items.append(rng.choice(["\n", "\r", curses.KEY_ENTER]))
        else:
            items.append(rng.choice("yYnN"))
    return items


# ------------------------------------------------------------------------ sizes

def terminal_size(rng: random.Random) -> tuple[int, int]:
    """(rows, columns) from 1x1 up to 400x120, biased to the edges and common sizes."""
    roll = rng.random()
    if roll < 0.25:
        return rng.randint(1, 4), rng.randint(1, 12)
    if roll < 0.50:
        return rng.randint(1, 30), rng.randint(1, 50)
    if roll < 0.75:
        return rng.choice([(24, 80), (24, 100), (40, 120), (8, 40), (6, 60), (9, 50), (30, 200), (60, 160)])
    return rng.randint(1, 120), rng.randint(1, 400)


def resize_storm(rng: random.Random, count: int = 12) -> list[tuple[int, int]]:
    return [terminal_size(rng) for _ in range(rng.randint(1, count))]


# ------------------------------------------------------------------ command lines

_COMMANDS = ["status", "events", "task", "tasks", "orders", "mission", "missions", "add", "amend", "priority", "reorder", "dependencies",
             "verify", "approve", "reject", "retry", "pause", "resume", "cancel", "continue", "stop", "start", "attach", "detach", "steer",
             "proposal", "proposals", "propose", "rotation", "station", "theme", "doctor", "help", "?", "control", "usage", "finish",
             "quit", "exit", "home", "console", "_controller", "gc", "view"]
_SUBS = ["add", "list", "show", "amend", "priority", "reorder", "dependencies", "pause", "resume", "cancel", "retry", "verify", "reject",
         "create", "set", "colour", "accept", "--help", "-h"]
_FLAGS = ["--json", "--task", "--prompt", "--prompt=", "--file", "--title", "--priority", "--order", "--depends-on", "--acceptance", "--evidence",
          "--approve", "--reason", "--value", "--add", "--limit", "--offset", "--after", "--all", "--mission", "--kind", "--route", "--status",
          "--force", "--native", "--headless", "--providers", "--routes", "--set", "--cwd", "--client", "--help", "-h", "-x", "--", "-", "--unknown",
          "--theme", "--colors", "--color", "--pick", "--from"]
_NUMBERS = ["0", "1", "2", "3", "42", "-1", "-0", "007", "1e9", "1.5", "99999999999999999999", "-99999999999999999999", "2147483648", "9223372036854775808",
            "0x10", "١٢٣", "²", "nan", "inf", "", " "]


def command_line(rng: random.Random, *, words: int = 8) -> str:
    """A console command line: real verbs and flags mixed with quotes, slashes, numbers and Unicode noise."""
    parts: list[str] = []
    if rng.random() < 0.3:
        parts.append("/")
    for index in range(rng.randint(0, words)):
        kind = rng.choice(["cmd", "sub", "flag", "num", "word", "quote", "text", "path", "eq"])
        if index == 0 and rng.random() < 0.7:
            kind = "cmd"
        if kind == "cmd":
            parts.append(rng.choice(_COMMANDS))
        elif kind == "sub":
            parts.append(rng.choice(_SUBS))
        elif kind == "flag":
            parts.append(rng.choice(_FLAGS))
        elif kind == "num":
            parts.append(rng.choice(_NUMBERS))
        elif kind == "word":
            parts.append(rng.choice(_ASCII_WORDS))
        elif kind == "quote":
            parts.append(rng.choice(["'", '"', "'don't", '"unbalanced', "it's", "'a b'", '"a b"', "\\", "a\\ b", "'\"'", "$(x)", "`x`"]))
        elif kind == "path":
            parts.append(rng.choice(["/", "//", "/etc/passwd", "../..", "~", "~/x", ".", "-", "/dev/null", "nonexistent/path", "a/b/c", "\x00x", "x" * 300]))
        elif kind == "eq":
            parts.append(rng.choice(_FLAGS) + "=" + plain_text(rng, 6, bounded=True))
        else:
            parts.append(plain_text(rng, 8, bounded=True))
    line = rng.choice([" ", " ", " ", "  ", "\t"]).join(parts)
    if rng.random() < 0.1:
        line += " " + valid_text(rng, 40, bounded=True)
    if rng.random() < 0.03:
        line += " " + "x" * rng.randint(1000, 6000)
    return line


def cli_args(rng: random.Random, verbs: list[str] | None = None) -> list[str]:
    """argv for ``bridge.main``: a plausible verb followed by chaos."""
    args: list[str] = []
    verb_pool = verbs or ["status", "events", "task", "mission", "missions", "proposal", "rotation", "doctor", "theme", "help", "steer", "station", "gc"]
    if rng.random() < 0.9:
        args.append(rng.choice(verb_pool))
    for _ in range(rng.randint(0, 7)):
        kind = rng.choice(["sub", "flag", "num", "text", "flagnum", "flagtext"])
        if kind == "sub":
            args.append(rng.choice(_SUBS))
        elif kind == "flag":
            args.append(rng.choice(_FLAGS))
        elif kind == "num":
            args.append(rng.choice(_NUMBERS))
        elif kind == "text":
            args.append(valid_text(rng, 10, bounded=True))
        elif kind == "flagnum":
            args += [rng.choice(["--task", "--value", "--limit", "--offset", "--after", "--add", "--priority", "--order"]), rng.choice(_NUMBERS)]
        else:
            args += [rng.choice(["--prompt", "--title", "--reason", "--evidence", "--acceptance", "--kind", "--route", "--status", "--set", "--providers", "--routes", "--mission"]), valid_text(rng, 12, bounded=True)]
    return [arg for arg in args if "\x00" not in arg]


# ----------------------------------------------------------------- JSON-like data

def json_value(rng: random.Random, depth: int = 0, *, weird: bool = True) -> Any:
    """Arbitrary nested JSON-like data: wrong types, None, huge numbers, big containers."""
    roll = rng.random()
    if depth > 4:
        roll = min(roll, 0.69)
    if roll < 0.12:
        return None
    if roll < 0.20:
        return rng.choice([True, False])
    if roll < 0.32:
        return rng.choice([0, 1, -1, 7, 2 ** 31, 2 ** 63, 10 ** 30, -10 ** 30, 1700000000, 1700000000000])
    if roll < 0.40:
        return rng.choice([0.0, -0.5, 99.9, 1e308, float("inf"), float("-inf"), float("nan"), 1e-320]) if weird else 0.5
    if roll < 0.62:
        return chaotic_text(rng, 30) if weird else plain_text(rng, 30)
    if roll < 0.70:
        return rng.choice(["", " ", "unknown", "none", "-", "RUNNING", "READY", "VERIFYING", "5h", "weekly", "42", "3.5"])
    if roll < 0.85:
        return [json_value(rng, depth + 1, weird=weird) for _ in range(rng.choice([0, 1, 2, 3, 5, 12, 40]))]
    if roll < 0.99:
        keys = ["id", "ordinal", "title", "status", "name", "kind", "data", "reason", "from", "to", "text", "summary", "priority", "remaining_percent",
                "reset_epoch", "window", "provider", "route", "model", "operation_id", "task_id", "created_at", "revision", "exit_code"]
        return {rng.choice(keys) if rng.random() < 0.8 else chaotic_text(rng, 6): json_value(rng, depth + 1, weird=weird) for _ in range(rng.choice([0, 1, 3, 6, 15]))}
    nested: Any = "leaf"
    for _ in range(rng.randint(20, 60)):
        nested = rng.choice([[nested], {"data": nested}])
    return nested


_TASK_STATUSES = ["QUEUED", "READY", "RUNNING", "PAUSED", "BLOCKED", "VERIFYING", "COMPLETED", "FAILED", "CANCELLED"]
_MISSION_STATUSES = ["CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"]


def _mutate(rng: random.Random, value: Any, depth: int = 0) -> Any:
    """Randomly damage a (copy of a) valid snapshot subtree."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            roll = rng.random()
            if roll < 0.05:
                continue  # missing key
            if roll < 0.10:
                out[key] = None
            elif roll < 0.15:
                out[key] = json_value(rng, 3)
            elif roll < 0.17:
                out[key] = chaotic_text(rng, 400)
            else:
                out[key] = _mutate(rng, item, depth + 1)
        return out
    if isinstance(value, list):
        items = [_mutate(rng, item, depth + 1) for item in value]
        roll = rng.random()
        if roll < 0.04 and items:
            items = items * rng.randint(2, 60)
        elif roll < 0.06:
            items.append(json_value(rng, 2))
        return items
    if isinstance(value, str) and rng.random() < 0.2:
        return chaotic_text(rng, 60)
    if isinstance(value, (int, float)) and rng.random() < 0.15:
        return rng.choice([None, "x", -1, 10 ** 12, 1e300, float("nan"), True, [], {}])
    return value


def task_row(rng: random.Random, number: int) -> dict[str, Any]:
    return {
        "id": number + rng.choice([0, 100]), "mission_id": "m" * 32, "ordinal": number, "title": plain_text(rng, 40),
        "original_prompt": valid_text(rng, 80), "prompt": valid_text(rng, 80), "priority": rng.choice([0, 1, -3, 99]), "ordering": number * 10,
        "status": rng.choice(_TASK_STATUSES), "revision": rng.randint(1, 4), "origin": rng.choice(["operator", "agent-proposal"]),
        "proposal_status": rng.choice(["NONE", "PENDING", "ACCEPTED", "REJECTED"]), "created_at": "2026-10-30T06:55:35+00:00",
        "updated_at": "2026-10-30T06:55:35+00:00", "acceptance": [plain_text(rng, 20) for _ in range(rng.randint(0, 3))],
        "evidence": [{"text": valid_text(rng, 30), "at": "2026-10-30T06:55:35+00:00", "source": rng.choice(["operator", "verifier", "approval"]), "revision": 1}
                     for _ in range(rng.randint(0, 3))],
        "pending": rng.choice([{}, {"prompt": valid_text(rng, 20)}]), "depends_on": [], "depends_on_ordinals": [rng.randint(1, 5) for _ in range(rng.randint(0, 2))],
        "amendments": [],
    }


def base_snapshot(rng: random.Random) -> dict[str, Any]:
    """A snapshot shaped like ``bridge.console_snapshot`` with plausible content."""
    mission_id = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    tasks = [task_row(rng, number) for number in range(1, rng.choice([0, 1, 2, 5, 30, 250]) + 1)]
    events = []
    kinds = ["mission-created", "task-added", "task-status", "mission-status", "operation-prepared", "operation-running", "operation-exited",
             "provider-selection", "capacity-refresh", "command-applied", "task-rejected", "operation-uncertain", "native-session-restoration",
             "repository-checkpoint", "review-hold", "weird-kind", "controller-heartbeat", "task-verified", "dependency-added"]
    for number in range(1, rng.choice([0, 3, 25, 400]) + 1):
        events.append({"id": number, "mission_id": mission_id, "task_id": rng.choice([None, 1, 2]), "operation_id": rng.choice([None, "abcdef0123456789"]),
                       "kind": rng.choice(kinds), "created_at": "2026-10-30T06:55:35+00:00", "data": json_value(rng, 1) if rng.random() < 0.5 else
                       {"from": rng.choice(_TASK_STATUSES), "to": rng.choice(_TASK_STATUSES), "reason": plain_text(rng, 20), "title": plain_text(rng, 20)}})
    windows = [{"name": rng.choice(["5h", "weekly"]), "remaining_percent": rng.choice([0, 12.5, 100, None, "x"]), "reset_epoch": rng.choice([None, 1893456000, 10 ** 15])}
               for _ in range(rng.randint(0, 3))]
    return {
        "schema_version": 1,
        "mission": {"id": mission_id, "name": plain_text(rng, 40), "objective": valid_text(rng, 120), "cwd": "/tmp/" + plain_text(rng, 20),
                    "status": rng.choice(_MISSION_STATUSES), "created_at": "2026-10-30T06:55:35+00:00", "updated_at": "2026-10-30T06:55:35+00:00",
                    "config": rng.choice([{}, {"colour": rng.randint(1, 8)}, {"colour": "x"}, {"providers": "codex,claude", "native_tui": True}])},
        "tasks": tasks,
        "task_counts": {status: rng.randint(0, 5) for status in rng.sample(_TASK_STATUSES, rng.randint(0, 5))},
        "proposal_counts": {},
        "task_total": len(tasks) + rng.choice([0, 0, 1000]),
        "task_page": {"limit": 200, "offset": 0, "returned": len(tasks), "search": ""},
        "criteria": {"verified": 0, "total": 0, "complete": None},
        "current_task": rng.choice([None, {"id": 1, "ordinal": 1, "title": plain_text(rng, 20), "status": rng.choice(_TASK_STATUSES), "revision": 1}]),
        "pending_commands": [], "operations": [
            {"id": "abcdef0123456789", "task_id": 1, "task_ordinal": 1, "provider": rng.choice(["codex", "claude", "kilo", None]), "station": "codex", "route_id": "",
             "model": plain_text(rng, 10), "phase": "running", "status": rng.choice(["RUNNING", "EXITED", "PREPARED", "UNCERTAIN"]), "exit_code": rng.choice([None, 0, 1, 75]),
             "created_at": "2026-10-30T06:55:35+00:00"} for _ in range(rng.randint(0, 4))],
        "events": events, "operator_events": events[:20],
        "event_page": {"offset": 0, "limit": 20, "returned": len(events)},
        "stations": [{"name": rng.choice(["codex", "claude", "kilo-zai", plain_text(rng, 10)]), "provider": "codex", "model": plain_text(rng, 10), "state": rng.choice(["running", "idle", "unavailable"]),
                      "selected": rng.random() < 0.3, "viewed": rng.random() < 0.3} for _ in range(rng.randint(0, 5))],
        "station_config": {"providers": ["codex"], "routes": [], "models": {}, "efforts": {}, "configured_mode": rng.choice(["background", "native"])},
        "station_mode": {"configured_mode": "background", "actual_mode": "unknown", "source": "unavailable"},
        "station_observation": {"station": "unknown", "model": "unknown", "effort": "unknown"},
        "station_context": {"executing_operation": None, "operation_status": "unknown", "provider_liveness": "unknown", "last_launched_station": "unknown", "viewed_station": "unknown"},
        "execution": {"iteration": None, "operation_elapsed_seconds": rng.choice([None, 12, 99999]), "seconds_since_verified_progress": None, "progress_source": "none"},
        "capacity": {"selection": plain_text(rng, 10), "wait": None, "next_eligible_epoch": None, "source": "unavailable", "live": False,
                     "decisions": [{"provider": "codex", "windows": windows, "ready": rng.random() < 0.5} for _ in range(rng.randint(0, 3))]},
        "waiting": rng.choice([None, {"reason": "rate-limited", "providers": ["codex"], "entries": windows, "until_epoch": 1893456000, "detail": plain_text(rng, 40)}]),
        "lifecycle": {"status": "RUNNING", "failure_reason": None, "controller": rng.choice(["running", "unknown", "stale"]), "controller_running": rng.random() < 0.5,
                      "heartbeat": {"reported_state": "unknown", "age_seconds": rng.choice([None, 3, 99999]), "source": "x"}},
        "suspend": {"status": "unknown"}, "health": {"status": rng.choice(["unknown", "ok", "stalled"]), "stall_warning": rng.choice([None, plain_text(rng, 30)])},
        "persistence": {"database": "/x", "schema_version": 10},
        "last_meaningful_progress": None, "hold": None, "needs_you": None,
        "usage": rng.choice([None, {"text": "Provider  Scope  Remaining\ncodex  5h  72%", "rows": []}, {"rows": json_value(rng, 2)}]),
    }


def random_snapshot(rng: random.Random) -> dict[str, Any]:
    """Valid-looking snapshot with damage, or pure nonsense."""
    roll = rng.random()
    if roll < 0.15:
        return {chaotic_text(rng, 8): json_value(rng) for _ in range(rng.randint(0, 8))}
    if roll < 0.25:
        return {}
    if roll < 0.35:
        # a dict full of the real keys with arbitrary values
        keys = list(base_snapshot(rng))
        return {key: json_value(rng) for key in keys if rng.random() < 0.8}
    snapshot = base_snapshot(rng)
    if rng.random() < 0.7:
        snapshot = _mutate(rng, snapshot)
    return snapshot
