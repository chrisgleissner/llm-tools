#!/usr/bin/env python3
"""Render the project-site step screenshots from demo-recorder captures.

Input is the JSON written by ``scripts/bridge_demo.py --frames-out``.  Record
at 84 columns so the text stays legible when the image is shown about 700 px
wide:

    python3 scripts/bridge_demo.py --theme dark  --size 84x30 --out /tmp/d.gif --frames-out /tmp/dark.json
    python3 scripts/bridge_demo.py --theme light --size 84x30 --out /tmp/l.gif --frames-out /tmp/light.json
    python3 scripts/site_stills.py /tmp/dark.json dark docs/site/assets
    python3 scripts/site_stills.py /tmp/light.json light docs/site/assets

Only demo-recorder frames (demo provider, ``~/acme-api``, ``/home/demo``) may be
used on the site.  Each still is the full-screen render with the unused middle
rows cut out, so it keeps the header and the bottom status lines.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_demo as bd  # noqa: E402

CELL_W = 16
CELL_H = 32

Pick = Callable[[str], bool]

SHOTS: dict[str, tuple[Pick, bool, int, bool]] = {
    # name: (predicate, pick last match, bottom rows kept, show cursor)
    "plan": (lambda t: "> 3  ○ QUEUED" in t and "Cover the header" in t, True, 3, False),
    "run": (lambda t: "Write tests/test_ratelimit.py" in t and "Running tests" in t and "✓" not in t, False, 1, False),
    "review": (lambda t: "› /approve" in t and "Needs you" in t, True, 3, True),
}


def pick_sample(samples: list[bd.Sample], pred: Pick, last: bool) -> bd.Sample:
    hits = [s for s in samples if pred(bd.strip_ansi(s.text))]
    if not hits:
        raise SystemExit("no capture matches; was the recording made at --size 84x30?")
    return hits[-1] if last else hits[0]


def stitch(img, rows: int, text: str, bottom: int, renderer: bd.Renderer):
    """Keep the used top rows and the last ``bottom`` rows; drop the empty middle."""
    from PIL import Image

    lines = text.split("\n")
    body = [i for i, line in enumerate(lines[: rows - bottom]) if line.strip()]
    top = (body[-1] + 2) if body else 2
    top = min(top, rows - bottom)
    pad_y = renderer.pad_y
    y_top = pad_y + top * CELL_H
    y_bot = pad_y + (rows - bottom) * CELL_H
    gap = CELL_H // 2
    width, height = img.size
    out = Image.new("RGB", (width, y_top + gap + (height - y_bot)), renderer.theme.bg)
    out.paste(img.crop((0, 0, width, y_top)), (0, 0))
    out.paste(img.crop((0, y_bot, width, height)), (0, y_top + gap))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    frames, theme_name, outdir = Path(argv[1]), argv[2], Path(argv[3])
    from PIL import Image

    samples, (cols, rows) = bd.load_frames(frames)
    if cols != 84:
        print(f"warning: recording is {cols} columns wide, expected 84", file=sys.stderr)
    theme = bd.THEMES[theme_name]
    for name, (pred, last, bottom, cursor) in SHOTS.items():
        sample = pick_sample(samples, pred, last)
        renderer = bd.Renderer(cols, rows, theme, cell_w=CELL_W, cell_h=CELL_H, chrome=False, show_cursor=cursor)
        img = renderer.render(bd.parse_ansi(sample.text, cols, rows), sample.cursor if cursor else None)
        img = stitch(img, rows, bd.strip_ansi(sample.text), bottom, renderer)
        for suffix, width in (("", img.size[0]), ("-1x", 700)):
            w, h = img.size
            out = img if width == w else img.resize((width, round(h * width / w)), Image.LANCZOS)
            out = out.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            path = outdir / f"step-{name}-{theme_name}{suffix}.png"
            out.save(path, optimize=True)
            print(path, out.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
