"""A transient /proc read failure must not make a force stop refuse to signal its own process (FIND-012).

The interleaving is injected: the first reads of the process start token return nothing while the process is
alive, as when /proc cannot be read for a moment under load. The refusal for a genuinely different process,
and for an identity that stays unreadable, is unchanged.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_runtime import _process_start_token, _terminate_owned_process_group
from tests.conftest import wait_for
from tests.test_bridge_force_stop import _live_provider


def _flaky_token(monkeypatch: pytest.MonkeyPatch, failures: int | None) -> list[int]:
    """Make ``_process_start_token`` return "" for the first ``failures`` calls (None: always)."""
    calls: list[int] = []
    real = _process_start_token

    def token(pid: int) -> str:
        calls.append(pid)
        if failures is None or len(calls) <= failures:
            return ""
        return real(pid)

    monkeypatch.setattr(bridge_runtime, "_process_start_token", token)
    monkeypatch.setattr(bridge_runtime, "IDENTITY_READ_DELAY_SECONDS", 0.0)
    return calls


def _alive(provider: subprocess.Popen) -> bool:
    return provider.poll() is None


def test_transient_unreadable_identity_is_read_again_and_the_process_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, token, provider = _live_provider(tmp_path)
    try:
        calls = _flaky_token(monkeypatch, failures=2)
        _terminate_owned_process_group(pid, token)
        wait_for(lambda: not _alive(provider), 10, 0.05, "provider ended")
        assert len(calls) > 2, "the identity was read again after the unreadable results"
    finally:
        if _alive(provider):
            os.killpg(pid, signal.SIGKILL)
            provider.wait(timeout=5)


def test_identity_that_stays_unreadable_is_still_refused_and_nothing_is_signalled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, token, provider = _live_provider(tmp_path)
    try:
        calls = _flaky_token(monkeypatch, failures=None)
        with pytest.raises(PermissionError, match="persisted identity does not match"):
            _terminate_owned_process_group(pid, token)
        assert len(calls) == bridge_runtime.IDENTITY_READ_ATTEMPTS, "the wait is bounded"
        assert _alive(provider)
    finally:
        os.killpg(pid, signal.SIGKILL)
        provider.wait(timeout=5)


def test_a_different_identity_is_refused_at_once_without_rereading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, token, provider = _live_provider(tmp_path)
    try:
        monkeypatch.setattr(bridge_runtime, "IDENTITY_READ_DELAY_SECONDS", 0.0)
        with pytest.raises(PermissionError, match="persisted identity does not match"):
            _terminate_owned_process_group(pid, token + "-reused")
        assert _alive(provider)
    finally:
        os.killpg(pid, signal.SIGKILL)
        provider.wait(timeout=5)
