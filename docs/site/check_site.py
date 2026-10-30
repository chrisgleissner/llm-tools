#!/usr/bin/env python3
"""Checks run by the Pages workflow before the site is published.

1. Every relative reference in index.html, 404.html, style.css and site.js resolves to a
   file in this directory (src, href, poster, srcset candidates, url(...)).
2. Every file in assets/ is referenced (assets/src/ holds sources and is exempt).
3. No file contains a home path other than /home/demo, a private address, a
   token, or one of your own private patterns, in its raw bytes or, for images
   and for frames sampled from videos and GIFs, in OCR text.

Screenshots on the site must come from the demo recorder
(scripts/bridge_demo.py): demo provider, ``~/acme-api`` and ``/home/demo``.

Personal strings (your user name, host names, e-mail address) must not be
written in this file, because the file sits next to the published site. Put them
outside the published tree instead:

* ``LLM_TOOLS_SITE_PRIVATE_PATTERNS``: regular expressions, one per line.
* ``LLM_TOOLS_SITE_PRIVATE_FILE``: path of a file with one regular expression
  per line (``#`` starts a comment). Default: ``.site-private-patterns`` in the
  repository root, which is not tracked.

OCR needs the ``tesseract`` binary and video frames need ``ffmpeg``. Without
them those steps are skipped locally with a notice and fail when ``CI`` is set,
so a missing tool cannot hide a leak in CI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
ASSETS = ROOT / "assets"
ASSETS_SRC = ASSETS / "src"
# Repository tooling and sources; .github/workflows/pages.yml leaves them out of the artifact.
UNPUBLISHED = {(ROOT / "check_site.py").resolve(), (ROOT / "README.md").resolve()}
TEXT_SOURCES = ("index.html", "style.css", "site.js")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".gif"}
VIDEO_FRAMES = 6
PRIVATE_ENV = "LLM_TOOLS_SITE_PRIVATE_PATTERNS"
PRIVATE_FILE_ENV = "LLM_TOOLS_SITE_PRIVATE_FILE"
DEFAULT_PRIVATE_FILE = REPO_ROOT / ".site-private-patterns"

# Forbidden in any file, raw or OCR'd. /home/ is allowed only as /home/demo.
HOME_PATH = re.compile(r"/home/(?!demo\b)[A-Za-z0-9_.-]+")
GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    HOME_PATH,
    re.compile(r"/Users/[A-Za-z0-9_.-]+"),
    re.compile(r"(?<![A-Za-z0-9_:.-])/root/"),
    re.compile(r"/tmp/claude-"),
    re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"bypass permissions", re.I),
)


def load_private_patterns(env: Mapping[str, str] | None = None) -> list[re.Pattern[str]]:
    """Personal patterns from the environment or an untracked local file; never from this file."""
    env = os.environ if env is None else env
    lines: list[str] = list((env.get(PRIVATE_ENV) or "").splitlines())
    path = Path(env.get(PRIVATE_FILE_ENV) or DEFAULT_PRIVATE_FILE)
    if path.is_file():
        lines += path.read_text(encoding="utf-8").splitlines()
    patterns: list[re.Pattern[str]] = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.I))
    return patterns


def find_leak(text: str, private: list[re.Pattern[str]] | None = None) -> str | None:
    """The first forbidden string in ``text``, or ``None``."""
    for pattern in (*GENERIC_PATTERNS, *(private or [])):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def refs_in_html(html: str) -> list[str]:
    out = re.findall(r'(?:src|href|poster|data-poster-mobile)="([^"]+)"', html)
    for srcset in re.findall(r'srcset="([^"]+)"', html):
        out += [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
    return out


def refs_in_css(css: str) -> list[str]:
    return [m.strip("'\" ") for m in re.findall(r"url\(([^)]+)\)", css)]


def is_external(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "data:", "#", "mailto:", "//"))


def ocr(tesseract: str, image: Path) -> str | None:
    """OCR text of an image, or ``None`` when tesseract timed out."""
    try:
        result = subprocess.run(
            [tesseract, str(image), "stdout"], capture_output=True, text=True, timeout=300,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except subprocess.TimeoutExpired:
        return None
    return result.stdout


def video_duration(ffprobe: str | None, video: Path) -> float | None:
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=60,
        )
        value = float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    return value if value > 0 else None


def extract_frames(ffmpeg: str, ffprobe: str | None, video: Path, out_dir: Path, count: int = VIDEO_FRAMES) -> list[Path]:
    """About ``count`` frames spread evenly over the video (the first frame included)."""
    duration = video_duration(ffprobe, video)
    rate = f"{count}/{duration:.3f}" if duration else "1/2"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(video), "-vf", f"fps={rate}", "-frames:v", str(count), str(out_dir / "frame-%02d.png")],
        capture_output=True, timeout=300, check=False,
    )
    return sorted(out_dir.glob("frame-*.png"))


def main() -> int:
    errors: list[str] = []
    referenced: set[Path] = set()
    private = load_private_patterns()

    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "site.js").read_text(encoding="utf-8")
    refs = refs_in_html(html) + refs_in_css(css)
    refs += re.findall(r"""["'](assets/[^"']+)["']""", js)
    # Absolute URLs of this site (Open Graph and Twitter images) point at local files.
    refs += re.findall(r'content="https://[^"/]+/llm-tools/(assets/[^"]+)"', html)
    # 404.html is served from any missing path, so it uses absolute /llm-tools/ URLs.
    not_found = (ROOT / "404.html").read_text(encoding="utf-8") if (ROOT / "404.html").exists() else ""
    refs += [r[len("/llm-tools/"):] if r.startswith("/llm-tools/") else r for r in refs_in_html(not_found)]
    for ref in refs:
        if is_external(ref):
            continue
        if ref == "":
            continue
        target = ROOT / ref.split("#")[0].split("?")[0]
        if not target.exists():
            errors.append(f"missing site file: {ref}")
        elif target.resolve() in UNPUBLISHED or ASSETS_SRC in target.resolve().parents:
            errors.append(f"the page references a file the Pages workflow does not publish: {ref}")
        else:
            referenced.add(target.resolve())
    for attr in re.findall(r'data-(?:dark|light)="([^"]+)"', html):
        if not (ROOT / attr).exists():
            errors.append(f"missing site file: {attr}")
        referenced.add((ROOT / attr).resolve())

    for path in sorted(ASSETS.rglob("*")):
        if path.is_file() and "src" not in path.relative_to(ASSETS).parts[:1]:
            if path.resolve() not in referenced:
                errors.append(f"unreferenced asset (delete it or reference it): assets/{path.relative_to(ASSETS)}")

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.name == Path(__file__).name or "__pycache__" in path.parts:
            continue
        leak = find_leak(path.read_bytes().decode("utf-8", errors="ignore"), private)
        if leak:
            errors.append(f"{path.relative_to(ROOT)}: contains {leak!r}")

    in_ci = bool(os.environ.get("CI"))
    tesseract = shutil.which("tesseract")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    images = [p for p in sorted(ASSETS.rglob("*")) if p.suffix.lower() in IMAGE_SUFFIXES]
    videos = [p for p in sorted(ASSETS.rglob("*")) if p.suffix.lower() in VIDEO_SUFFIXES]
    scanned_frames = 0
    if not tesseract:
        msg = "tesseract not installed; image and video text was not checked"
        if in_ci:
            errors.append(msg)
        else:
            print("warning: " + msg)
    else:
        for image in images:
            text = ocr(tesseract, image)
            if text is None:
                errors.append(f"{image.relative_to(ROOT)}: OCR timed out")
                continue
            leak = find_leak(text, private)
            if leak:
                errors.append(f"{image.relative_to(ROOT)}: image text contains {leak!r}")
        if videos and not ffmpeg:
            msg = "ffmpeg not installed; video and GIF frames were not checked"
            if in_ci:
                errors.append(msg)
            else:
                print("warning: " + msg)
        elif ffmpeg:
            for video in videos:
                with tempfile.TemporaryDirectory(prefix="site-frames-") as scratch:
                    try:
                        frames = extract_frames(ffmpeg, ffprobe, video, Path(scratch))
                    except (subprocess.TimeoutExpired, OSError):
                        frames = []
                    if not frames:
                        errors.append(f"{video.relative_to(ROOT)}: no frames could be extracted for the text check")
                        continue
                    for frame in frames:
                        text = ocr(tesseract, frame)
                        if text is None:
                            errors.append(f"{video.relative_to(ROOT)}: OCR timed out")
                            break
                        scanned_frames += 1
                        leak = find_leak(text, private)
                        if leak:
                            errors.append(f"{video.relative_to(ROOT)}: video frame text contains {leak!r}")
                            break

    if errors:
        print("site check failed:")
        for e in errors:
            print("  " + e)
        return 1
    print(
        f"site check passed: {len(referenced)} referenced files, {len(images)} images and "
        f"{scanned_frames} video frames scanned, {len(private)} private patterns"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
