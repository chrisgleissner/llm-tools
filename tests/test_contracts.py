from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from llm_tools import common, scheduler

from .conftest import ROOT, run_cmd, run_cmd_bytes, write_exe


AVAILABLE = '{"available":true,"five_hour":{"remaining":50,"resets_at":"2026-06-02T23:00:00Z"},"week":{"remaining":50,"resets_at":"2026-06-07T23:00:00Z"}}'
COPILOT_AVAILABLE = '{"available":true,"monthly":{"remaining":25}}'


def seed_provider_data(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    (home / ".codex" / "sessions" / "s.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":53,"window_minutes":300,"resets_at":"2026-06-02T13:49:00Z"},"secondary":{"used_percent":59,"window_minutes":10080,"resets_at":"2026-06-07T16:25:00Z"},"spark":{"primary":{"used_percent":99,"resets_at":"2026-06-02T22:26:00Z"},"secondary":{"used_percent":96,"resets_at":"2026-06-08T17:49:00Z"}}}}\n',
        encoding="utf-8",
    )
    (home / ".claude" / "projects" / "p.jsonl").write_text(
        '{"rate_limits":{"five_hour":{"used_percentage":0,"resets_at":"2026-06-02T13:20:00Z"},"seven_day":{"used_percentage":25,"resets_at":"2026-06-04T13:00:00Z"}}}\n',
        encoding="utf-8",
    )


def test_usage_help_and_validation(env: dict[str, str]) -> None:
    help_result = run_cmd(["./llm-usage", "--help"], env)
    assert help_result.returncode == 0
    assert "Usage: llm-usage" in help_result.stdout
    bad = run_cmd(["./llm-usage", "--watch", "abc"], env)
    assert bad.returncode == 2
    assert "watch requires numeric seconds" in bad.stderr


def test_usage_json_table_statusline_and_cache(env: dict[str, str]) -> None:
    seed_provider_data(env)
    env["LLM_USAGE_COPILOT_CAPTURE_TEXT"] = "Plan: 62% used · Session: 0 AIC used"
    js = run_cmd(["./llm-usage", "--json", "--show-copilot-credits"], env)
    assert js.returncode == 0, js.stderr
    data = json.loads(js.stdout)
    assert set(data) == {"generated_at", "codex", "claude", "copilot"}
    assert data["codex"]["rows"][1]["key"] == "codex-spark"
    assert data["copilot"]["monthly"]["used"] == 62
    assert data["copilot"]["monthly"]["remaining"] == 38
    assert data["copilot"].get("ai_credits") is None
    table = run_cmd(["./llm-usage", "--show-source"], env)
    assert table.returncode == 0
    assert "Codex" in table.stdout
    assert "GPT-5.3 Spark" in table.stdout
    assert "Copilot" in table.stdout
    assert "38%" in table.stdout
    assert "copilot cli" in table.stdout
    hidden = run_cmd(["./llm-usage", "--hide-codex-spark"], env)
    assert "GPT-5.3 Spark" not in hidden.stdout
    status = subprocess.run(
        ["./llm-usage", "--statusline"],
        cwd=ROOT,
        env=env,
        input='{"rate_limits":{"five_hour":{"used_percentage":10},"seven_day":{"used_percentage":25}}}',
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert status.stdout.strip() == "Claude 5h 90% left weekly 75% left"
    assert (Path(env["HOME"]) / ".cache" / "llm-tools" / "llm-usage" / "claude-status.json").is_file()


def test_usage_log_only(env: dict[str, str]) -> None:
    seed_provider_data(env)
    env["LLM_USAGE_DISABLE_COPILOT"] = "1"
    result = run_cmd(["./llm-usage", "--log-only"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    log = Path(env["HOME"]) / ".cache" / "llm-tools" / "llm-usage" / "llm-usage.log"
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {(e["provider"], e["window"]) for e in entries} >= {("Codex", "5h"), ("Codex", "weekly"), ("Claude", "5h"), ("Claude", "weekly")}
    assert all(e["remaining"] is not None for e in entries)


def test_scheduler_validation_and_dry_run(env: dict[str, str]) -> None:
    assert "one of --prompt" in run_cmd(["./llm-scheduler", "--tool", "codex"], env).stderr
    assert "invalid --tool" in run_cmd(["./llm-scheduler", "--tool", "bad", "--prompt", "x"], env).stderr
    assert "not valid for copilot" in run_cmd(["./llm-scheduler", "--tool", "copilot", "--window", "weekly", "--prompt", "x"], env).stderr
    env["LLM_USAGE_NOW_EPOCH"] = "1780430000"
    env["LLM_SCHEDULER_USAGE_JSON"] = '{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}}'
    result = run_cmd(["./llm-scheduler", "--tool", "codex", "--prompt", "x", "--command-template", "true", "--dry-run", "--log-dir", str(Path(env["HOME"]) / "logs")], env)
    assert result.returncode == 0
    assert "dry-run: logs written to" in result.stdout
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "usage_decision" and e["data"]["reason"] == "rate-limited" for e in events)


def test_scheduler_submission_prompt_files_retry_and_logs(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.txt"
    attempts = tmp_path / "attempts.txt"
    env.update(
        {
            "LLM_SCHEDULER_USAGE_JSON": AVAILABLE,
            "PROVIDER_CAPTURE": str(capture),
            "PROVIDER_MODE": "plain",
        }
    )
    prompt = tmp_path / "special prompt.txt"
    prompt.write_text("line one\nline two with ; $HOME and spaces\n", encoding="utf-8")
    result = run_cmd(
        [
            "./llm-scheduler",
            "--tool",
            "codex",
            "--prompt-file",
            str(prompt),
            "--command-template",
            "provider-mock {prompt}",
            "--log-dir",
            str(tmp_path / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "chat ok" in result.stdout
    run_dir = Path(result.stdout.strip().split()[-1])
    assert (run_dir / "prompt.txt").read_text() == prompt.read_text()
    assert "line two with ; $HOME and spaces" in capture.read_text()

    write_exe(
        fake_provider.parent / "flaky",
        """#!/usr/bin/env python3
import os, pathlib, sys
p = pathlib.Path(os.environ["ATTEMPTS"])
count = int(p.read_text() or "0") if p.exists() else 0
p.write_text(str(count + 1))
print("HTTP 429 Too Many Requests" if count == 0 else "ok")
sys.exit(0)
""",
    )
    env["ATTEMPTS"] = str(attempts)
    retry = run_cmd(["./llm-scheduler", "--tool", "copilot", "--prompt", "x", "--command-template", "flaky", "--retry-delays", "0,0", "--log-dir", str(tmp_path / "retry")], env | {"LLM_SCHEDULER_USAGE_JSON": COPILOT_AVAILABLE})
    assert retry.returncode == 0
    assert attempts.read_text() == "2"


def test_scheduler_autonomy_abort_no_retry(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    env.update({"LLM_SCHEDULER_USAGE_JSON": AVAILABLE, "PROVIDER_MODE": "blocking"})
    result = run_cmd(
        [
            "./llm-scheduler",
            "--tool",
            "claude",
            "--prompt",
            "x",
            "--command-template",
            "provider-mock",
            "--retry-delays",
            "0,0",
            "--log-dir",
            str(tmp_path / "logs"),
        ],
        env,
    )
    assert result.returncode == common.AUTONOMY_ABORT_STATUS
    run_dir = Path(result.stderr.strip().split()[-1])
    assert "autonomous abort: interactive prompt detected" in (run_dir / "attempt-1.out").read_text()
    assert not (run_dir / "attempt-2.out").exists()


def test_scheduler_aborts_on_no_output_progress(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    # exact-stdout path (e.g. codex): a provider that prints then stalls without
    # any recognizable prompt must be detected as "no progress" and killed.
    env.update({"LLM_SCHEDULER_USAGE_JSON": AVAILABLE, "PROVIDER_MODE": "idle_no_prompt"})
    result = run_cmd(
        [
            "./llm-scheduler", "--tool", "codex", "--prompt", "x",
            "--command-template", "provider-mock",
            "--no-retry", "--log-dir", str(tmp_path / "logs"),
            "--headless-idle-timeout", "1", "--headless-question-timeout", "0",
        ],
        env,
    )
    assert result.returncode == common.AUTONOMY_ABORT_STATUS
    run_dir = Path(result.stderr.strip().split()[-1])
    assert "no output progress" in (run_dir / "attempt-1.out").read_text()


def test_scheduler_aborts_on_credit_question(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    # A "out of credit / wait or upgrade?" style prompt is a blocking prompt.
    env.update({"LLM_SCHEDULER_USAGE_JSON": AVAILABLE, "PROVIDER_MODE": "credit_question"})
    result = run_cmd(
        [
            "./llm-scheduler", "--tool", "codex", "--prompt", "x",
            "--command-template", "provider-mock",
            "--no-retry", "--log-dir", str(tmp_path / "logs"),
        ],
        env,
    )
    assert result.returncode == common.AUTONOMY_ABORT_STATUS
    assert "autonomous abort" in (Path(result.stderr.strip().split()[-1]) / "attempt-1.out").read_text()


def ralph_stdout(env: dict[str, str], mode: str, tmp_path: Path) -> subprocess.CompletedProcess:
    renv = env.copy()
    renv.update(
        {
            "LLM_SCHEDULER_USAGE_JSON": '{"claude":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}',
            "PROVIDER_MODE": mode,
        }
    )
    return run_cmd_bytes(
        [
            "./ralph-robin",
            "--prompt",
            "rr",
            "--command-template",
            "provider-mock {tool} {prompt}",
            "--state-file",
            str(tmp_path / f"{mode}.json"),
            "--log-dir",
            str(tmp_path / f"{mode}-logs"),
            "--no-retry",
            "--max-iterations",
            "1",
        ],
        renv,
    )


def test_ralph_robin_exact_stdout_passthrough(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    cases = {
        "plain": b"chat ok\n",
        "multiline": b"line one\nline two\n",
        "nonewline": b"no final newline",
        "ansi": b"\x1b[31mred\x1b[0m\n",
        "utf8": "cafe \u2615\n".encode(),
        "stderr": b"answer only\n",
    }
    for mode, expected in cases.items():
        result = ralph_stdout(env, mode, tmp_path)
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout == expected
        if mode == "stderr":
            assert b"progress on stderr" in result.stderr


def test_ralph_robin_claude_stream_json_passthrough(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    write_exe(
        fake_bin / "claude",
        """#!/usr/bin/env python3
import json, sys, time
assert "--output-format" in sys.argv
assert "stream-json" in sys.argv
events = [
    {"type":"assistant","message":{"content":[{"type":"text","text":"I will inspect it.\\n"}]}},
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"pytest -q"}}]}},
    {"type":"user","message":{"content":[{"type":"tool_result","content":"1 passed\\n"}]}},
    {"type":"assistant","message":{"content":[{"type":"text","text":"Done."}]}},
]
for event in events:
    print(json.dumps(event), flush=True)
    time.sleep(0.01)
""",
    )
    result = run_cmd_bytes(
        [
            "./ralph-robin",
            "--prompt",
            "rr",
            "--state-file",
            str(tmp_path / "claude-stream.json"),
            "--log-dir",
            str(tmp_path / "claude-stream-logs"),
            "--no-retry",
            "--max-iterations",
            "1",
        ],
        env
        | {
            "LLM_SCHEDULER_USAGE_JSON": '{"claude":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}',
        },
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"I will inspect it.\n" in result.stdout
    assert b"Tool call: Bash\n" in result.stdout
    assert b'"command": "pytest -q"' in result.stdout
    assert b"Tool result:\n1 passed\n" in result.stdout
    assert b"Done.\n" in result.stdout
    assert b'"type":"assistant"' not in result.stdout


def test_claude_stream_result_fallback_after_tool_only_event() -> None:
    renderer = scheduler.ClaudeStreamRenderer()
    tool_event = b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"true"}}]}}\n'
    result_event = b'{"type":"result","result":"final answer"}\n'
    assert b"Tool call: Bash\n" in renderer.render_line(tool_event)
    assert renderer.render_line(result_event) == b"final answer\n"


def test_ralph_robin_partial_stdout_on_provider_failure(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    result = ralph_stdout(env, "partial_fail", tmp_path)
    assert result.returncode == 1
    assert result.stdout == b"partial"


def test_common_normalization_usage_decisions_and_time(env: dict[str, str]) -> None:
    codex = common.normalize_codex_obj({"rate_limits": {"primary": {"used_percent": 10}, "spark": {"primary": {"used_percent": 99}}}}, "test")
    assert codex and codex["five_hour"]["used"] == 10
    assert codex["rows"][1]["key"] == "codex-spark"
    claude = common.normalize_claude_obj({"rate_limits": {"five_hour": {"used_percentage": 10}}}, "test")
    assert claude and claude["five_hour"]["used"] == 10
    full = common.json_for_provider(claude, "claude")
    assert full["five_hour"]["remaining"] == 90
    assert common.fmt_duration(90061) == "1d 1h 1m"
    env["LLM_USAGE_NOW_EPOCH"] = "1000"
    stale = common.usage_decision_for_tool("codex", "auto", "1", "60", {"available": True, "five_hour": {"remaining": 0, "resets_at": 900}, "week": {"remaining": 50}}, env)
    assert stale["usable"] is True
    limited = common.usage_decision_for_tool("codex", "auto", "1", "60", {"available": True, "five_hour": {"remaining": 0, "resets_at": 2000}, "week": {"remaining": 50}}, env)
    assert limited["reason"] == "rate-limited"
    unavailable = common.usage_decision_for_tool("claude", "auto", "1", "60", {"available": False, "reason": "missing-cli"}, env)
    assert unavailable["wait_until"] == 1060


def test_legacy_cache_migration(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    legacy = home / ".cache" / "llm-usage"
    legacy.mkdir(parents=True)
    (legacy / "claude-status.json").write_text("{}", encoding="utf-8")
    result = run_cmd(["./llm-usage", "--json"], env | {"LLM_USAGE_DISABLE_COPILOT": "1"})
    assert result.returncode == 0
    assert (home / ".cache" / "llm-tools" / "llm-usage" / "claude-status.json").is_file()
    assert not legacy.exists()
