from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMMANDS = {"./llm-usage", "./llm-scheduler", "./ralph-robin"}
try:
    import coverage as _coverage

    COVERAGE_SITE = str(Path(_coverage.__file__).resolve().parents[1])
except Exception:
    COVERAGE_SITE = ""


def local_command_args(args: list[str]) -> list[str]:
    if not args or args[0] not in LOCAL_COMMANDS:
        return args
    return [sys.executable, *args]


@pytest.fixture(autouse=True)
def _hermetic_power_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch real machine power state from the test suite.

    ``LLM_TOOLS_NO_INHIBIT`` stops any in-process ``ralph_robin.main`` test from
    spawning a real ``systemd-inhibit`` helper. Tests that specifically exercise
    inhibitor/suspend behaviour override these explicitly.
    """
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    out = os.environ.copy()
    # Keep the fixture hermetic when the suite itself runs inside a ralph-robin
    # session: ralph-robin exports LLM_TOOLS_RALPH_ROBIN_* guard vars into the
    # ambient environment, and copying them here would leak into every CLI
    # subprocess — tripping the "--suspend-until-ready is disabled inside an
    # active ralph-robin provider run" guard and forcing providers down the
    # unavailable/sleep path. Tests that exercise those vars set them explicitly.
    for key in [k for k in out if k.startswith("LLM_TOOLS_RALPH_ROBIN_")]:
        del out[key]
    extras: dict[str, str] = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{ROOT}:{out.get('PATH', '')}",
        "LLM_USAGE_COPILOT_CACHE_TTL": "0",
        # Keep Codex hermetic: never spawn the real `codex app-server` (which
        # would hit the live account). Tests that exercise the active-refresh
        # path inject a payload via LLM_USAGE_CODEX_RATE_LIMITS_JSON, which
        # takes precedence over this switch.
        "LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1",
        # Active-refresh reads are single-shot under test: retries only matter
        # against a live network and would add real sleeps to failure-path
        # tests. Cases that assert retry behaviour set this explicitly.
        "LLM_USAGE_LIVE_FETCH_RETRIES": "0",
        # Never spawn a real systemd-inhibit helper from in-process or
        # subprocess test runs; inhibitor behaviour is covered explicitly.
        "LLM_TOOLS_NO_INHIBIT": "1",
        "LLM_SCHEDULER_HEADLESS": "1",
    }
    # Only enable coverage instrumentation on CLI subprocesses when the
    # current process is already being measured (``coverage run -m
    # pytest`` sets ``COVERAGE_PROCESS_START`` and the parent coverage
    # instance is active). Without this guard every subprocess pays
    # ~150ms of coverage.startup() that produces a .coverage.pid file
    # the parent will then drop on the floor, slowing the suite by
    # tens of seconds for no measurement benefit. We also leave
    # ``COVERAGE_SITE`` on PYTHONPATH so the subprocess can import
    # coverage if the parent is measuring.
    if out.get("COVERAGE_PROCESS_START"):
        extras["COVERAGE_PROCESS_START"] = out["COVERAGE_PROCESS_START"]
    out.update(extras)
    if COVERAGE_SITE and COVERAGE_SITE not in out.get("PYTHONPATH", ""):
        out["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), COVERAGE_SITE) if p)
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
    args = local_command_args(args)
    return subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)


def run_cmd_bytes(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    args = local_command_args(args)
    return subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)
