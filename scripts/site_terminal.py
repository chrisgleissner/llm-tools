#!/usr/bin/env python3
"""Convert captured terminal output (ANSI SGR) to HTML for docs/site.

The landing page shows real program output as text, not screenshots. Capture it
on a pty so the tools emit colour, then convert:

    python3 scripts/site_terminal.py capture.ansi [--drop REGEX] [--from REGEX]

The result is an HTML fragment (spans using the ``c-*`` classes in
``docs/site/style.css``) on stdout. ``--from`` drops every line before the first
match, ``--drop`` drops lines that match (for example a line holding a local
path). ``--keep`` trims a table to some visible columns (used for the
narrow mobile view). Colours are mapped by role so the page keeps AA contrast in both themes.
"""

from __future__ import annotations

import argparse
import html
import re
import sys

SGR = re.compile(r"\x1b\[([0-9;]*)m")
# (bold?, colour code) -> css class. Roles, not literal colours.
CLASS_BY_CODE = {
    "2": "c-dim",
    "38;5;39": "c-brand",
    "38;5;81": "c-claude",
    "38;5;77": "c-codex",
    "32": "c-ok",
    "33": "c-warn",
    "36": "c-info",
    "31": "c-bad",
}


def classify(params: str) -> str | None:
    parts = [p for p in params.split(";") if p != ""]
    if not parts or parts == ["0"]:
        return None
    # Drop leading "0" or "1" style/reset markers that precede a colour.
    while parts and parts[0] in {"0", "1", "2"} and len(parts) > 1:
        lead = parts.pop(0)
        if lead == "2":
            return "c-dim"
    key = ";".join(parts)
    return CLASS_BY_CODE.get(key) or CLASS_BY_CODE.get(parts[-1])


def convert(text: str) -> str:
    out: list[str] = []
    open_span = False
    pos = 0
    for m in SGR.finditer(text):
        out.append(html.escape(text[pos:m.start()], quote=False))
        pos = m.end()
        if open_span:
            out.append("</span>")
            open_span = False
        cls = classify(m.group(1))
        if cls:
            out.append(f'<span class="{cls}">')
            open_span = True
    out.append(html.escape(text[pos:], quote=False))
    if open_span:
        out.append("</span>")
    return "".join(out)


def keep_columns(line: str, ranges: list[tuple[int, int | None]]) -> str:
    """Keep only the given visible-column ranges of one ANSI line (SGR kept)."""
    cells: list[tuple[str, str]] = []  # (SGR sequence active before the cell, char)
    active = ""
    pos = 0
    for m in SGR.finditer(line):
        for ch in line[pos:m.start()]:
            cells.append((active, ch))
        active = "" if m.group(1) in ("", "0") else m.group(0)
        pos = m.end()
    for ch in line[pos:]:
        cells.append((active, ch))
    out: list[str] = []
    for lo, hi in ranges:
        prev = None
        for style, ch in cells[lo:hi]:
            if style != prev:
                out.append("\x1b[0m" + style if prev is not None else style)
                prev = style
            out.append(ch)
        if prev:
            out.append("\x1b[0m")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--from", dest="start", default=None, help="drop lines before the first match")
    ap.add_argument("--drop", action="append", default=[], help="drop lines matching this regex")
    ap.add_argument("--keep", default=None, help="visible column ranges to keep, e.g. 0:11,19:47,69:80")
    args = ap.parse_args()
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read().replace("\r\n", "\n").replace("\r", "")
    lines = text.split("\n")
    if args.start:
        for i, line in enumerate(lines):
            if re.search(args.start, SGR.sub("", line)):
                lines = lines[i:]
                break
    lines = [ln for ln in lines if not any(re.search(d, SGR.sub("", ln)) for d in args.drop)]
    if args.keep:
        ranges = []
        for part in args.keep.split(","):
            lo, _, hi = part.partition(":")
            ranges.append((int(lo or 0), int(hi) if hi else None))
        lines = [keep_columns(ln, ranges) for ln in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    sys.stdout.write(convert("\n".join(lines)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
