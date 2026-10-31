"""Session identity parsing and advertised native-resume capabilities."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from llm_tools.bridge_sessions import (
    NativeSessionIdentity,
    codex_session_records,
    codex_owned_argv,
    confirm_restored,
    discover_owned_codex_session,
    new_session_argv,
    parse_session_metadata,
    resume_argv,
)
from llm_tools.bridge_runtime import _process_start_token
from tests.conftest import scaled


CODEX_ID = "12345678-1234-4234-8234-123456789abc"
CLAUDE_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def _fake_cli(path: Path, help_text: str) -> Path:
    path.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write({help_text!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_codex_session_identity_requires_exact_session_meta_and_workdir(tmp_path: Path) -> None:
    expected_cwd = tmp_path.resolve()
    record = {
        "type": "session_meta",
        "payload": {"id": CODEX_ID, "cwd": str(expected_cwd)},
    }

    identity = parse_session_metadata("codex", record, cwd=expected_cwd)

    assert identity == NativeSessionIdentity("codex", CODEX_ID, str(expected_cwd), "codex:session_meta")
    assert parse_session_metadata("codex", {**record, "type": "message"}, cwd=expected_cwd) is None
    assert parse_session_metadata(
        "codex",
        {"type": "session_meta", "payload": {"id": "--last", "cwd": str(expected_cwd)}},
        cwd=expected_cwd,
    ) is None
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    assert parse_session_metadata("codex", record, cwd=other_cwd) is None


def test_codex_owned_terminal_argv_requires_advertised_no_daemon(tmp_path: Path) -> None:
    executable = _fake_cli(tmp_path / "codex", "usage: codex [--no-daemon] [PROMPT]")
    argv = [str(executable), "--model", "gpt-test", "objective"]

    assert codex_owned_argv(argv, executable=str(executable), help_text="codex [--no-daemon] prompt") == [
        str(executable), "--no-daemon", "--model", "gpt-test", "objective",
    ]
    assert codex_owned_argv(argv, executable=str(executable), help_text="codex [PROMPT]") is None


def test_claude_session_identity_requires_structured_init_record(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    record = {
        "type": "system",
        "subtype": "init",
        "session_id": CLAUDE_ID,
        "cwd": cwd,
    }

    identity = parse_session_metadata("claude", record, cwd=tmp_path)

    assert identity == NativeSessionIdentity("claude", CLAUDE_ID, cwd, "claude:system.init")
    assert parse_session_metadata("claude", {**record, "session_id": CODEX_ID}, cwd=tmp_path) is not None
    assert parse_session_metadata("claude", {**record, "subtype": "message"}, cwd=tmp_path) is None


def test_resume_argv_uses_exact_id_only_when_installed_help_advertises_support(tmp_path: Path, monkeypatch) -> None:
    cwd = str(tmp_path.resolve())
    identity = NativeSessionIdentity("codex", CODEX_ID, cwd, "codex:session_meta")
    binary = _fake_cli(tmp_path / "codex", "codex commands:\n  resume SESSION_ID [PROMPT]\n")
    monkeypatch.setenv("PATH", str(tmp_path))

    argv = resume_argv(identity)

    assert argv == [str(binary), "resume", CODEX_ID]
    assert "--last" not in argv


def test_capability_drift_and_uninstalled_cli_are_unavailable(tmp_path: Path, monkeypatch) -> None:
    cwd = str(tmp_path.resolve())
    identity = NativeSessionIdentity("claude", CLAUDE_ID, cwd, "claude:system.init")
    _fake_cli(tmp_path / "claude", "usage: claude [prompt] [--continue]\n")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert resume_argv(identity) is None
    assert resume_argv(
        NativeSessionIdentity("codex", CODEX_ID, cwd, "codex:session_meta"),
        executable="missing-provider-cli",
    ) is None


def test_claude_and_copilot_start_with_owned_uuid_then_confirm_structured_startup(tmp_path: Path, monkeypatch) -> None:
    cwd = str(tmp_path.resolve())
    help_text = "options: --session-id UUID --resume SESSION_ID\n"
    claude = _fake_cli(tmp_path / "claude", help_text)
    copilot = _fake_cli(tmp_path / "copilot", help_text)
    monkeypatch.setenv("PATH", str(tmp_path))

    claude_launch = new_session_argv("claude", ["claude", "--effort", "high", "prompt"], cwd=cwd)
    copilot_launch = new_session_argv("copilot", ["copilot", "-i", "prompt"], cwd=cwd)

    assert claude_launch is not None and copilot_launch is not None
    claude_argv, claude_id = claude_launch
    copilot_argv, copilot_id = copilot_launch
    assert claude_argv == [str(claude), "--session-id", claude_id, "--effort", "high", "prompt"]
    assert copilot_argv == [str(copilot), "--session-id", copilot_id, "-i", "prompt"]
    assert claude_id != copilot_id

    claude_identity = parse_session_metadata(
        "claude",
        {"type": "system", "subtype": "init", "session_id": claude_id, "cwd": cwd},
        cwd=cwd,
    )
    copilot_identity = parse_session_metadata(
        "copilot",
        {"type": "session.start", "data": {"sessionId": copilot_id, "context": {"cwd": cwd}}},
        cwd=cwd,
    )
    assert claude_identity is not None and claude_identity.session_id == claude_id
    assert copilot_identity is not None and copilot_identity.session_id == copilot_id
    assert confirm_restored(claude_identity, claude_identity)
    assert confirm_restored(copilot_identity, copilot_identity)


def test_new_session_id_is_not_injected_after_cli_capability_drift(tmp_path: Path, monkeypatch) -> None:
    binary = _fake_cli(tmp_path / "claude", "options: --resume SESSION_ID\n")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert new_session_argv("claude", ["claude", "prompt"], cwd=tmp_path) is None
    assert new_session_argv("claude", [str(binary), "prompt"], cwd=tmp_path, session_id="--last") is None


def test_resume_identity_confirmation_rejects_session_provider_and_cwd_mismatch(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    expected = NativeSessionIdentity("codex", CODEX_ID, cwd, "codex:session_meta")

    assert confirm_restored(expected, expected)
    assert not confirm_restored(expected, NativeSessionIdentity("codex", CLAUDE_ID, cwd, "codex:session_meta"))
    assert not confirm_restored(expected, NativeSessionIdentity("claude", CODEX_ID, cwd, "claude:system.init"))
    assert not confirm_restored(expected, NativeSessionIdentity("codex", CODEX_ID, str(tmp_path.parent), "codex:session_meta"))
    assert not confirm_restored(expected, None)


def test_unverified_provider_metadata_stays_unavailable(tmp_path: Path) -> None:
    session = {"type": "session_start", "session_id": "session_12345678", "cwd": str(tmp_path)}

    assert parse_session_metadata("copilot", session, cwd=tmp_path) is None
    assert parse_session_metadata("kilo", session, cwd=tmp_path) is None
    assert parse_session_metadata("opencode", session, cwd=tmp_path) is None


def test_codex_metadata_file_scan_reads_only_exact_structured_first_line(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    sessions = tmp_path / "codex" / "sessions" / "2026" / "10" / "25"
    sessions.mkdir(parents=True)
    transcript = sessions / "session.jsonl"
    transcript.write_text(
        '{"type":"session_meta","payload":{"id":"' + CODEX_ID + '","cwd":"' + str(cwd) + '"}}\n'
        '{"type":"message","text":"must not be read"}\n',
        encoding="utf-8",
    )
    wrong_cwd = sessions / "wrong.jsonl"
    wrong_cwd.write_text(
        '{"type":"session_meta","payload":{"id":"' + CLAUDE_ID + '","cwd":"' + str(tmp_path) + '"}}\n',
        encoding="utf-8",
    )
    (sessions / "ignored.txt").write_text("not metadata", encoding="utf-8")

    records = codex_session_records(tmp_path / "codex" / "sessions", cwd=cwd)

    assert len(records) == 1
    assert records[0].identity.session_id == CODEX_ID
    assert records[0].path == transcript
    assert records[0].modified_ns == transcript.stat().st_mtime_ns


def _codex_transcript(path: Path, session_id: str, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd)}}) + "\n",
        encoding="utf-8",
    )


def _process_holding_fds(paths: list[Path], *, readonly: bool = False, child: bool = False) -> subprocess.Popen[str]:
    open_expr = "\n".join(
        f"held_{index}=open({str(path)!r},{'r' if readonly else 'a'!r})"
        for index, path in enumerate(paths)
    )
    opener = f"import time\n{open_expr}\nprint('READY',flush=True)\ntime.sleep(30)"
    if child:
        script = (
            "import subprocess,sys,time\n"
            f"p=subprocess.Popen([sys.executable,'-c',{opener!r}],stdout=subprocess.PIPE,text=True,start_new_session=True)\n"
            "print(p.pid,flush=True)\n"
            "print(p.stdout.readline().strip(),flush=True)\n"
            "time.sleep(30)\n"
        )
    else:
        script = opener
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert process.stdout is not None
    if child:
        process.owned_child_pid = int(process.stdout.readline().strip())
    assert process.stdout.readline().strip() == "READY"
    return process


def test_owned_codex_session_requires_exact_live_writable_process_tree_fd(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    transcript = tmp_path / "codex" / "sessions" / "owned.jsonl"
    _codex_transcript(transcript, CODEX_ID, cwd)
    process = _process_holding_fds([transcript], child=True)
    token = _process_start_token(process.pid)
    assert token
    try:
        identity = discover_owned_codex_session(process.pid, token, cwd=str(cwd))
        assert identity == NativeSessionIdentity("codex", CODEX_ID, str(cwd.resolve()), "codex:session_meta")
        assert discover_owned_codex_session(process.pid, token + ":reused", cwd=str(cwd)) is None
    finally:
        child_pid = getattr(process, "owned_child_pid", 0)
        if child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=scaled(3))


def test_codex_session_discovery_rejects_unrelated_readonly_and_multiple_fds(tmp_path: Path, monkeypatch) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    first = codex_home / "sessions" / "first.jsonl"
    second = codex_home / "sessions" / "second.jsonl"
    unrelated = tmp_path / "outside.jsonl"
    _codex_transcript(first, CODEX_ID, cwd)
    _codex_transcript(second, CLAUDE_ID, cwd)
    _codex_transcript(unrelated, CODEX_ID, cwd)

    for paths, readonly in (([unrelated], False), ([first], True), ([first, second], False)):
        process = _process_holding_fds(paths, readonly=readonly)
        token = _process_start_token(process.pid)
        try:
            assert discover_owned_codex_session(process.pid, token, cwd=str(cwd)) is None
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=scaled(3))


@pytest.mark.parametrize("shape", ["second-record", "large-transcript", "oversized-header", "malformed-header"])
def test_owned_codex_discovery_reads_only_bounded_metadata_header(tmp_path: Path, monkeypatch, shape: str) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    transcript = tmp_path / "codex" / "sessions" / "owned.jsonl"
    _codex_transcript(transcript, CODEX_ID, cwd)
    if shape == "oversized-header":
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": CODEX_ID, "cwd": str(cwd), "padding": "x" * 65536,
        }}) + "\n")
    elif shape == "malformed-header":
        transcript.write_text('{"type":"session_meta",\n' + transcript.read_text())
    else:
        with transcript.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "message": "x" * (70000 if shape == "large-transcript" else 8),
            }}) + "\n")
    process = _process_holding_fds([transcript])
    try:
        identity = discover_owned_codex_session(process.pid, _process_start_token(process.pid), cwd=cwd)
        if shape in {"second-record", "large-transcript"}:
            assert identity == NativeSessionIdentity("codex", CODEX_ID, str(cwd.resolve()), "codex:session_meta")
        else:
            assert identity is None
    finally:
        process.kill()
        process.wait(timeout=scaled(3))
