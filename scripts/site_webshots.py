#!/usr/bin/env python3
"""Turn the web-console screenshots captured by web/e2e/site-shots.spec.ts into site assets.

    SITE_SHOTS_DIR=/tmp/shots npx playwright test e2e/site-shots.spec.ts --project=chromium-desktop --project=chromium-mobile
    python3 scripts/site_webshots.py /tmp/shots docs/site/assets

Writes web-{overview,approval,terminal,phone}-{dark,light}-{1x,2x}.{webp,png}. The data is the deterministic mock
of web/e2e/mock.ts, so the images contain sample names only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

WIDTHS = {"web-phone": (353, 706), "web-overview": (1000, 2000), "web-approval": (1000, 2000), "web-terminal": (1000, 2000)}


def main(src: Path, out: Path) -> None:
    for name, (w1, w2) in WIDTHS.items():
        for theme in ("dark", "light"):
            image = Image.open(src / f"{name}-{theme}.png").convert("RGB")
            for label, width in (("1x", w1), ("2x", w2)):
                height = round(image.height * width / image.width)
                scaled = image.resize((width, height), Image.LANCZOS)
                base = out / f"{name}-{theme}-{label}"
                scaled.save(base.with_suffix(".webp"), "WEBP", quality=82, method=6)
                scaled.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(base.with_suffix(".png"), optimize=True)
                print(base.name, width, height)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
