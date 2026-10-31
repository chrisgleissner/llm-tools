"""A headless Codex or Claude run reads its prompt from stdin, so it is not visible in the process arguments."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from llm_tools import common, scheduler

from .conftest import run_cmd

AVAILABLE = '{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
SECRET = "SECRET-PROMPT-TEXT with ; $HOME and spaces\nsecond line"

# Records its arguments, its raw command line and everything on stdin.
FAKE_CLI = """#!{python}
import json, os, select, sys
raw = open('/proc/self/cmdline', 'rb').read().decode('utf-8', 'replace')
stdin = sys.stdin.read() if select.select([0], [], [], 3)[0] else ''
with open(os.environ['PROVIDER_CAPTURE'], 'w') as fh:
    json.dump({{'argv': sys.argv[1:], 'cmdline': raw, 'stdin': stdin}}, fh)
print('done')
"""


def install_fake(directory: Path, name: str) -> None:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(FAKE_CLI.format(python=os.sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize("provider, expected_tail", [("codex", ["-"]), ("claude", [])])
def test_headless_run_delivers_the_prompt_on_stdin(env: dict[str, str], tmp_path: Path, provider: str, expected_tail: list[str]) -> None:
    bin_dir = tmp_path / "bin"
    install_fake(bin_dir, provider)
    capture = tmp_path / "capture.json"
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "LLM_SCHEDULER_USAGE_JSON": AVAILABLE,
        "PROVIDER_CAPTURE": str(capture),
    })
    result = run_cmd(["./llm-scheduler", "--provider", provider, "--prompt", SECRET, "--headless", "--log-dir", str(tmp_path / "logs")], env)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = json.loads(capture.read_text(encoding="utf-8"))
    assert seen["stdin"] == SECRET
    assert "SECRET-PROMPT-TEXT" not in seen["cmdline"] and "SECRET-PROMPT-TEXT" not in " ".join(seen["argv"])
    if expected_tail:
        assert seen["argv"][-len(expected_tail):] == expected_tail


def make_cfg(tmp_path: Path, provider: str, **fields) -> tuple[scheduler.SchedulerConfig, common.RunLogs]:
    cfg = scheduler.SchedulerConfig(provider=provider, cwd=str(tmp_path), **fields)
    logs = common.setup_run_logs(tmp_path / "logs", provider, provider, None)
    common.load_prompt(SECRET, "", logs)
    return cfg, logs


def test_native_attached_template_and_other_providers_keep_the_prompt_argument(tmp_path: Path) -> None:
    for provider, fields in (
        ("codex", {"exec_mode": "tmux"}),
        ("claude", {"exec_mode": "tmux"}),
        ("codex", {"attached": True}),
        ("codex", {"command_template": "provider-mock {prompt}"}),
        ("kilo", {}),
        ("opencode", {}),
        ("copilot", {}),
    ):
        cfg, logs = make_cfg(tmp_path, provider, **fields)
        argv = scheduler.command_argv(cfg, logs, SECRET)
        assert SECRET in argv, (provider, fields)
        assert cfg.prompt_stdin == ""


def test_prompt_file_is_private_and_used_for_stdin(tmp_path: Path) -> None:
    cfg, logs = make_cfg(tmp_path, "codex")
    argv = scheduler.command_argv(cfg, logs, SECRET)
    assert [Path(argv[0]).name, argv[1]] == ["codex", "exec"] and argv[-1] == "-" and SECRET not in argv
    assert cfg.prompt_stdin == str(logs.run_dir / "prompt.txt")
    assert stat.S_IMODE(Path(cfg.prompt_stdin).stat().st_mode) == 0o600
