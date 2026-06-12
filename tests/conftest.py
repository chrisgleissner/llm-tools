from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    out = os.environ.copy()
    out.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{ROOT}:{out.get('PATH', '')}",
            "PYTHONPATH": str(ROOT),
            "COVERAGE_PROCESS_START": str(ROOT / "pyproject.toml"),
            "LLM_USAGE_COPILOT_CACHE_TTL": "0",
            "LLM_SCHEDULER_HEADLESS": "1",
        }
    )
    return out


@pytest.fixture()
def fake_bin(env: dict[str, str]) -> Path:
    return Path(env["PATH"].split(":", 1)[0])


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def fake_provider(fake_bin: Path) -> Path:
    return write_exe(
        fake_bin / "provider-mock",
        """#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get("PROVIDER_MODE", "plain")
if os.environ.get("PROVIDER_CAPTURE"):
    with open(os.environ["PROVIDER_CAPTURE"], "ab") as fh:
        fh.write((" ".join(sys.argv[1:])).encode() + b"\\n")
if mode == "plain":
    sys.stdout.write("chat ok\\n")
elif mode == "multiline":
    sys.stdout.write("line one\\nline two\\n")
elif mode == "nonewline":
    sys.stdout.write("no final newline")
elif mode == "ansi":
    sys.stdout.write("\\x1b[31mred\\x1b[0m\\n")
elif mode == "utf8":
    sys.stdout.write("cafe \\u2615\\n")
elif mode == "stderr":
    sys.stderr.write("progress on stderr\\n")
    sys.stdout.write("answer only\\n")
elif mode == "partial_fail":
    sys.stdout.write("partial")
    sys.stdout.flush()
    sys.exit(42)
elif mode == "rate_limit":
    sys.stdout.write("HTTP 429 Too Many Requests\\n")
elif mode == "blocking":
    sys.stdout.write("What do you want to do?\\nEnter to confirm - Esc to cancel\\n")
    sys.stdout.flush()
    time.sleep(20)
elif mode == "idle_no_prompt":
    sys.stdout.write("working...\\n")
    sys.stdout.flush()
    time.sleep(20)
elif mode == "credit_question":
    sys.stdout.write("You've hit your monthly spend limit. Wait for limit to reset or upgrade to Max?\\n")
    sys.stdout.flush()
    time.sleep(20)
sys.stdout.flush()
""",
    )


def run_cmd(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)


def run_cmd_bytes(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)
