"""Repository content echoed by a provider must not trip the blocking-prompt heuristics."""
from __future__ import annotations

import json
import io
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from llm_tools import common, scheduler

TRAP = "Run out of tokens, not momentum"


def tool_result_event(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def assistant_text_event(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def stream_json_run(tmp_path: Path, events: list[dict], *, trailing_sleep: float = 0.0) -> tuple[int, str]:
    script = tmp_path / "emit.py"
    lines = "".join(json.dumps(event) + "\n" for event in events)
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write({lines!r})\nsys.stdout.flush()\n"
        f"time.sleep({trailing_sleep})\n",
        encoding="utf-8",
    )
    cfg = scheduler.SchedulerConfig()
    cfg.cwd = str(tmp_path)
    out, status = tmp_path / "out.txt", tmp_path / "status.txt"
    code = scheduler.run_fresh_claude_stream_json(cfg, [sys.executable, str(script)], out, status)
    return code, out.read_text(encoding="utf-8")


def exact_stdout_run(tmp_path: Path, text: str, *, trailing_sleep: float = 0.0, exit_code: int = 0) -> tuple[int, str]:
    script = tmp_path / "emit-plain.py"
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write({text!r})\nsys.stdout.flush()\n"
        f"time.sleep({trailing_sleep})\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    cfg = scheduler.SchedulerConfig()
    cfg.cwd = str(tmp_path)
    out, status = tmp_path / "plain-out.txt", tmp_path / "plain-status.txt"
    code = scheduler.run_fresh_exact_stdout(cfg, [sys.executable, str(script)], out, status)
    return code, out.read_text(encoding="utf-8")


def exact_stdout_capture(tmp_path: Path, text: str, monkeypatch, *, trailing_sleep: float = 0.0) -> tuple[int, str]:
    script = tmp_path / "emit-prompt.py"
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write({text!r})\nsys.stdout.flush()\n"
        f"time.sleep({trailing_sleep})\n",
        encoding="utf-8",
    )
    cfg = scheduler.SchedulerConfig()
    cfg.cwd = str(tmp_path)
    out, status = tmp_path / "visible-out.txt", tmp_path / "visible-status.txt"
    captured = io.BytesIO()
    monkeypatch.setattr(scheduler, "sys", SimpleNamespace(
        stdout=SimpleNamespace(buffer=captured, isatty=lambda: False),
        stderr=SimpleNamespace(buffer=io.BytesIO(), isatty=lambda: False),
    ))
    code = scheduler.run_fresh_exact_stdout(cfg, [sys.executable, str(script)], out, status)
    return code, captured.getvalue().decode("utf-8", "replace")


def codex_json_run(tmp_path: Path, events: list[dict], monkeypatch, *, trailing_sleep: float = 0.0, exit_code: int = 0) -> tuple[int, str, str]:
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"events = {events!r}\n"
        "for event in events:\n"
        "    print(json.dumps(event))\n"
        f"time.sleep({trailing_sleep})\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    cfg = scheduler.SchedulerConfig()
    cfg.cwd = str(tmp_path)
    cfg.exact_stdout = True
    stdout_capture = io.BytesIO()
    fake_stdout = SimpleNamespace(buffer=stdout_capture, isatty=lambda: False)
    monkeypatch.setattr(scheduler, "sys", SimpleNamespace(stdout=fake_stdout, stderr=sys.stderr))
    argv = [
        str(codex), "-c", "model_reasoning_effort=medium", "exec", "--model", "gpt-6.1-sol", "-C", str(tmp_path), "-",
    ]
    out, status = tmp_path / "codex-out.txt", tmp_path / "codex-status.txt"
    code = scheduler.run_fresh_headless(cfg, argv, out, status)
    return code, out.read_text(encoding="utf-8"), stdout_capture.getvalue().decode("utf-8", "replace")


def test_tool_result_echoing_a_file_does_not_abort_a_stream_json_run(tmp_path: Path) -> None:
    code, text = stream_json_run(tmp_path, [tool_result_event(f"README.md says: {TRAP}"), assistant_text_event("done")])
    assert code == 0, text
    assert TRAP in text and "autonomous abort" not in text


def test_tool_result_as_the_last_event_does_not_abort_either(tmp_path: Path) -> None:
    code, text = stream_json_run(tmp_path, [tool_result_event(f"{TRAP}\nupgrade to max\nEnter to confirm")])
    assert code == 0, text


def test_assistant_text_prompt_at_the_end_still_aborts(tmp_path: Path) -> None:
    events = [assistant_text_event("You've hit your monthly spend limit\nWhat do you want to do?"), ]
    code, text = stream_json_run(tmp_path, events, trailing_sleep=3)
    assert code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in text


def test_historical_tool_diagnostic_does_not_abort_or_leak_to_managed_stdout(tmp_path: Path, monkeypatch) -> None:
    diagnostic = (
        "hook: PostToolUse\n"
        "hook: PostToolUse Completed\n"
        "ERROR: You’ve reached your usage limit. Open https://provider.invalid/usage to review.\n"
    )
    events = [
        {"type": "item.completed", "item": {"id": "item-1", "type": "command_execution", "aggregated_output": diagnostic, "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item-2", "type": "agent_message", "text": "I reviewed the saved report."}},
        {"type": "turn.completed"},
    ]
    code, text, managed_stdout = codex_json_run(tmp_path, events, monkeypatch)
    assert code == 0, text
    assert diagnostic in text and "autonomous abort" not in text
    assert diagnostic not in managed_stdout and "I reviewed the saved report." in managed_stdout


def test_actionable_limit_prompt_still_aborts_plain_stream(tmp_path: Path) -> None:
    prompt = "You’ve hit your monthly spend limit.\nWhat do you want to do?\n"
    code, text = exact_stdout_run(tmp_path, prompt, trailing_sleep=3)
    assert code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in text


def test_limit_menu_choices_still_abort_plain_and_structured_streams(tmp_path: Path, monkeypatch) -> None:
    menu = (
        "ERROR: You’ve reached your usage limit.\n"
        "Adjust monthly spend limit\nUpgrade to Max\n"
    )
    plain_code, plain_text = exact_stdout_run(tmp_path, menu, trailing_sleep=3)
    assert plain_code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in plain_text

    event = {"type": "error", "message": menu}
    structured_code, structured_text, _managed_stdout = codex_json_run(
        tmp_path, [event], monkeypatch, trailing_sleep=3,
    )
    assert structured_code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in structured_text


def test_raw_wrapper_prompt_is_visible_before_guard_abort(tmp_path: Path, monkeypatch) -> None:
    prompt = "Proceed? [y/N]\n"
    code, visible = exact_stdout_capture(tmp_path, prompt, monkeypatch, trailing_sleep=3)
    assert code == common.AUTONOMY_ABORT_STATUS
    assert visible == prompt


def test_structured_provider_quota_diagnostic_keeps_native_exit_status(tmp_path: Path, monkeypatch) -> None:
    event = {"type": "error", "message": "You’ve reached your usage limit. Please wait for limit to reset."}
    code, text, managed_stdout = codex_json_run(tmp_path, [event], monkeypatch, exit_code=42)
    assert code == 42
    assert "autonomous abort" not in text
    assert "usage limit" in managed_stdout


def test_explicit_prompt_cues_in_structured_provider_error_still_abort(tmp_path: Path, monkeypatch) -> None:
    event = {"type": "error", "message": "You’ve reached your usage limit. What do you want to do?"}
    code, text, _managed_stdout = codex_json_run(tmp_path, [event], monkeypatch, trailing_sleep=3)
    assert code == common.AUTONOMY_ABORT_STATUS
    assert "interactive prompt detected" in text


def test_plain_provider_quota_diagnostic_keeps_native_exit_status(tmp_path: Path) -> None:
    diagnostic = "ERROR: You’ve reached your usage limit. Try again later.\n"
    code, text = exact_stdout_run(tmp_path, diagnostic, exit_code=42)
    assert code == 42
    assert "autonomous abort" not in text


def test_provider_native_75_with_quota_diagnostic_is_not_confused_with_guard_abort(tmp_path: Path) -> None:
    diagnostic = "ERROR: You’ve reached your usage limit. Try again later.\n"
    code, _text = exact_stdout_run(tmp_path, diagnostic, exit_code=common.AUTONOMY_ABORT_STATUS)
    assert code == 1
    sidecar = json.loads((tmp_path / "plain-status.diagnostic.json").read_text(encoding="utf-8"))
    assert sidecar["provider_exit_status"] == common.AUTONOMY_ABORT_STATUS
    assert sidecar["scheduler_status"] == 1
    assert sidecar["class"] == "capacity"
    assert sidecar["cause"] == "provider-quota"
    assert diagnostic.strip() not in str(sidecar)
    validated = common.read_provider_diagnostic(tmp_path / "plain-status.diagnostic.json")
    assert validated is not None
    assert validated["provider_exit_status"] == common.AUTONOMY_ABORT_STATUS


def test_successful_codex_turn_ignores_historical_tool_credit_error_and_keeps_event_private(tmp_path: Path, monkeypatch) -> None:
    logs = common.setup_run_logs(tmp_path, "historical-credit")
    cfg = scheduler.SchedulerConfig(
        provider="codex", cwd=str(tmp_path), prompt_text="x", exact_stdout=True,
    )
    historical = 'tool output: {"error_type":"usage_limit_exceeded","api_key":"sentinelsecret"}'

    def fake_run(_cfg, _argv, output_file, status_file):
        output_file.write_text(historical, encoding="utf-8")
        status_file.write_text("0", encoding="utf-8")
        status_file.with_suffix(".diagnostic.json").write_text(
            json.dumps({"class": "success", "cause": "completed", "provider_credit_diagnostic": False}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(scheduler, "run_fresh_headless", fake_run)
    assert scheduler.submit_once(cfg, logs, 1, ["codex", "exec", "--json", "x"]) == 0
    event_text = logs.event_log.read_text(encoding="utf-8")
    assert "credit_exhausted" not in event_text
    assert "sentinelsecret" not in event_text
    assert historical not in event_text


@pytest.mark.parametrize("sidecar_kind", ["malformed", "wrong-types", "oversized"])
def test_invalid_codex_diagnostic_sidecar_falls_back_without_raw_output(tmp_path: Path, monkeypatch, sidecar_kind: str) -> None:
    logs = common.setup_run_logs(tmp_path, "bad-sidecar")
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), prompt_text="x", exact_stdout=True)
    secret_output = 'ERROR: quota was discussed in old tool output {"api_key":"sentinelsecret"}'
    if sidecar_kind == "malformed":
        sidecar_text = "{broken"
    else:
        sidecar = common.provider_failure_diagnostic(
            "ERROR: usage limit reached", common.AUTONOMY_ABORT_STATUS,
            provider_quota_diagnostic=True,
        )
        sidecar.update({
            "provider_exit_status": common.AUTONOMY_ABORT_STATUS,
            "scheduler_status": 1,
            "provider_quota_diagnostic": True,
            "provider_credit_diagnostic": False,
            "blocking_prompt": False,
        })
        sidecar_text = json.dumps(sidecar)
        if sidecar_kind == "wrong-types":
            sidecar = {**sidecar, "provider_exit_status": "bad"}
            sidecar_text = json.dumps(sidecar)
        else:
            sidecar_text += " " * 17000

    def fake_run(_cfg, _argv, output_file, status_file):
        output_file.write_text(secret_output, encoding="utf-8")
        status_file.write_text("0", encoding="utf-8")
        status_file.with_suffix(".diagnostic.json").write_text(sidecar_text, encoding="utf-8")
        return 0

    monkeypatch.setattr(scheduler, "run_fresh_headless", fake_run)
    assert scheduler.submit_once(cfg, logs, 1, ["codex", "exec", "--json", "x"]) == 0
    event_text = logs.event_log.read_text(encoding="utf-8")
    assert "sentinelsecret" not in event_text
    assert secret_output not in event_text


def test_codex_activity_events_render_and_clear_stale_question_scan() -> None:
    events = [
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "README.md"}]}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "docs", "tool": "search", "result": "2 results"}},
        {"type": "item.completed", "item": {"type": "web_search", "query": "official API reference"}},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Checking the stored record."}},
    ]
    renderer = scheduler.CodexStreamRenderer()
    rendered = []
    scannable = []
    for event in events:
        rendered.append(renderer.render_line((json.dumps(event) + "\n").encode()).decode())
        scannable.append(renderer.scannable.decode())

    assert "README.md" in rendered[0]
    assert "docs.search" in rendered[1] and "2 results" not in rendered[1]
    assert "Web search activity" in rendered[2] and "official API reference" not in rendered[2]
    assert "Reasoning updated" in rendered[3] and "Checking the stored record." not in rendered[3]
    assert scannable == [scheduler.TOOL_ACTIVITY_SCAN_MARKER] * len(events)


def test_codex_json_adapter_preserves_direct_command_flags_and_leaves_wrappers_alone() -> None:
    command = [
        "/opt/codex/bin/codex", "-c", "model_reasoning_effort=medium", "exec",
        "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-6.1-sol", "prompt with --json text",
    ]
    adapted = scheduler.codex_exec_json_argv(command)

    assert adapted == [*command[:4], "--json", *command[4:]]
    assert scheduler.codex_exec_json_argv(adapted) == adapted
    wrapper = ["env", "codex", "exec", "prompt"]
    assert scheduler.codex_exec_json_argv(wrapper) == wrapper


def test_codex_json_adapter_handles_global_and_post_prompt_options() -> None:
    image_command = ["codex", "-i", "image.png", "exec", "prompt"]
    assert scheduler.codex_exec_json_argv(image_command) == [
        "codex", "-i", "image.png", "exec", "--json", "prompt",
    ]
    remote_command = ["codex", "--remote", "ws://localhost", "exec", "prompt"]
    assert scheduler.codex_exec_json_argv(remote_command) == [
        "codex", "--remote", "ws://localhost", "exec", "--json", "prompt",
    ]
    color_command = ["codex", "exec", "--color", "never", "prompt"]
    assert scheduler.codex_exec_json_argv(color_command) == [
        "codex", "exec", "--json", "--color", "never", "prompt",
    ]
    alias_command = ["codex", "e", "prompt"]
    assert scheduler.codex_exec_json_argv(alias_command) == ["codex", "e", "--json", "prompt"]
    already_json = ["codex", "exec", "prompt", "--json"]
    assert scheduler.codex_exec_json_argv(already_json) == already_json
    assert scheduler.is_codex_exec_json(already_json)
    delimiter_prompt = ["codex", "exec", "--", "--json"]
    adapted = scheduler.codex_exec_json_argv(delimiter_prompt)
    assert adapted == ["codex", "exec", "--json", "--", "--json"]
    assert scheduler.is_codex_exec_json(adapted)


def test_explicit_codex_command_template_reaches_structured_transport(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    captured = tmp_path / "argv.json"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(captured)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    cfg = scheduler.SchedulerConfig(
        provider="codex", cwd=str(tmp_path), command_template="codex exec {prompt}",
        prompt_text="managed prompt", exact_stdout=True,
    )
    logs = common.setup_run_logs(tmp_path / "logs", "codex-template")
    argv = scheduler.command_argv(cfg, logs, cfg.prompt_text)

    code = scheduler.run_fresh_headless(cfg, argv, tmp_path / "out.txt", tmp_path / "status.txt")

    assert code == 0
    assert json.loads(captured.read_text(encoding="utf-8")) == ["exec", "--json", "managed prompt"]


def test_scan_ignores_blocking_phrase_that_is_not_in_the_last_lines() -> None:
    guard = scheduler.ProgressGuard()
    earlier = f"{TRAP}\n" + "\n".join(f"line {i}" for i in range(20))
    assert guard.note_output(earlier) is False
    assert guard.note_output("line\n" * 3 + "What do you want to do?") is True
