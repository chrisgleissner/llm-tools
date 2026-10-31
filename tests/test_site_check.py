"""The site checker keeps personal strings out of its own source and scans video frames."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "docs" / "site" / "check_site.py"
spec = importlib.util.spec_from_file_location("check_site", CHECKER)
assert spec and spec.loader
check_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_site)


def test_checker_source_contains_no_personal_strings() -> None:
    source = CHECKER.read_text(encoding="utf-8").lower()
    for word in ("mic" + "key", "chr" + "is@", "gleiss" + "ner"):
        assert word not in source


@pytest.mark.parametrize("text", [
    "see /home/alice/project", "/Users/bob/dev", "cd /root/x", "/tmp/claude-1000/x", "host 192.168.1.13", "10.0.0.7",
    "172.16.5.4", "ghp_" + "a" * 24, "github_pat_" + "B" * 30, "sk-" + "c" * 30, "AKIA" + "A" * 16, "bypass permissions on",
])
def test_generic_patterns_flag_private_material(text: str) -> None:
    assert check_site.find_leak(text) is not None


@pytest.mark.parametrize("text", [
    "/home/demo/acme-api", "~/acme-api", "task-list sk-short", "172.32.0.1", "11.0.0.1", ":root/ {", "version 1.2.3.4", "plain text",
])
def test_generic_patterns_allow_the_demo_material(text: str) -> None:
    assert check_site.find_leak(text) is None


def test_personal_patterns_come_from_the_environment_or_an_untracked_file(tmp_path: Path) -> None:
    assert check_site.load_private_patterns({}) == [] or check_site.DEFAULT_PRIVATE_FILE.is_file()
    env = {"LLM_TOOLS_SITE_PRIVATE_PATTERNS": "myhost\n\n  me@example\\.test  ", "LLM_TOOLS_SITE_PRIVATE_FILE": str(tmp_path / "none")}
    patterns = check_site.load_private_patterns(env)
    assert check_site.find_leak("login on MYHOST", patterns) == "MYHOST"
    assert check_site.find_leak("mail me@example.test", patterns) == "me@example.test"
    assert check_site.find_leak("nothing here", patterns) is None
    file = tmp_path / "patterns"
    file.write_text("# comment\nsecretword\n", encoding="utf-8")
    from_file = check_site.load_private_patterns({"LLM_TOOLS_SITE_PRIVATE_FILE": str(file)})
    assert check_site.find_leak("a SecretWord b", from_file) == "SecretWord"


def _text_video(path: Path, text: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=900x160:d=2:r=5",
         "-vf", f"drawtext=text='{text}':fontcolor=black:fontsize=56:x=20:y=50", str(path)],
        capture_output=True, timeout=120, check=False,
    )
    return result.returncode == 0 and path.is_file()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("tesseract")), reason="needs ffmpeg and tesseract")
@pytest.mark.parametrize("suffix", [".mp4", ".gif"])
def test_text_in_video_frames_is_found(tmp_path: Path, suffix: str) -> None:
    video = tmp_path / f"leak{suffix}"
    if not _text_video(video, "/home/alice/secret"):
        pytest.skip("this ffmpeg build cannot draw text")
    frames = check_site.extract_frames(shutil.which("ffmpeg"), shutil.which("ffprobe"), video, tmp_path)
    assert 1 <= len(frames) <= check_site.VIDEO_FRAMES
    text = check_site.ocr(shutil.which("tesseract"), frames[0])
    assert text is not None and check_site.find_leak(text) is not None, text


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_frames_are_sampled_across_the_whole_video(tmp_path: Path) -> None:
    video = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x120:d=12:r=10", str(video)], check=True, timeout=120)
    frames = check_site.extract_frames(shutil.which("ffmpeg"), shutil.which("ffprobe"), video, tmp_path)
    assert len(frames) == check_site.VIDEO_FRAMES
    assert check_site.video_duration(None, video) is None
    assert 11 < (check_site.video_duration(shutil.which("ffprobe"), video) or 0) < 13


def test_published_site_files_pass_the_raw_text_scan() -> None:
    """The fast half of the Pages check: no file under docs/site holds a generic leak pattern."""
    for path in sorted((ROOT / "docs" / "site").rglob("*")):
        if path.is_file() and path.name != "check_site.py" and "__pycache__" not in path.parts:
            leak = check_site.find_leak(path.read_bytes().decode("utf-8", errors="ignore"))
            assert leak is None, f"{path}: {leak!r}"
