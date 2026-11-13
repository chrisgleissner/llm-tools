"""PROV-001: the provider registry is the one source of each provider fact; consumers read it at call time."""
from __future__ import annotations

import dataclasses
import re

import pytest

from llm_tools import bridge_finish, bridge_limit_notice, bridge_providers as reg, bridge_sessions, bridge_task_notice, scheduler


def _patch(monkeypatch: pytest.MonkeyPatch, name: str, **changes: object) -> None:
    monkeypatch.setitem(reg.REGISTRY, name, dataclasses.replace(reg.REGISTRY[name], **changes))


def test_every_provider_has_a_record_and_templates_only_use_known_placeholders() -> None:
    assert {"codex", "claude", "copilot", "kilo", "opencode", "minimax", "zai"} <= set(reg.REGISTRY)
    for spec in reg.REGISTRY.values():
        for template in (spec.native_argv, spec.headless_argv, spec.headless_stream_argv):
            for token in template or ():
                assert not (token.startswith("{") and token not in {"{model}", "{cwd}", "{prompt}"})


def test_scheduler_reads_argv_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = scheduler.SchedulerConfig(provider="kilo", cwd="/c", attached=True)
    assert scheduler.provider_default_argv(cfg, "p") == ["kilo", "--prompt", "p"]
    _patch(monkeypatch, "kilo", native_argv=("kilo", "--changed", "{prompt}"))
    assert scheduler.provider_default_argv(cfg, "p") == ["kilo", "--changed", "p"]


def test_sessions_read_resume_form_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge_sessions._CAPABILITIES["codex"].syntax == ("resume",)
    _patch(monkeypatch, "codex", resume_syntax=("continue",))
    assert bridge_sessions._CAPABILITIES["codex"].syntax == ("continue",)
    assert bridge_sessions._CAPABILITIES.get("minimax") is None


def test_finish_reads_exit_command_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge_finish.exit_command("codex", {}) == "/quit"
    assert bridge_finish.exit_command("codex", {"LLM_BRIDGE_FINISH_CODEX": "/exit"}) == "/exit"
    _patch(monkeypatch, "codex", exit_command="/bye")
    assert bridge_finish.exit_command("codex", {}) == "/bye"
    assert bridge_finish.exit_command("kilo", {}) is None


def test_limit_notice_reads_pattern_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = "Usage limit reached. Try again in 2 hours."
    assert bridge_limit_notice.detect_usage_limit("claude", screen, now=0.0) is not None
    _patch(monkeypatch, "claude", limit_notice_pattern=None)
    assert bridge_limit_notice.detect_usage_limit("claude", screen, now=0.0) is None
    _patch(monkeypatch, "kilo", limit_notice_pattern=re.compile(r"^quota gone", re.I))
    assert bridge_limit_notice.detect_usage_limit("kilo", "quota gone", now=0.0) is not None


def test_task_notice_reads_delivery_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge_task_notice.notice_providers() == {"claude", "codex", "copilot"}
    _patch(monkeypatch, "kilo", notice_delivery=True)
    assert "kilo" in bridge_task_notice.notice_providers()


def test_terminal_question_detector_comes_from_the_registry() -> None:
    assert reg.REGISTRY["codex"].question_detector == reg.QUESTION_CODEX_QUEUED
    assert reg.REGISTRY["claude"].question_detector == reg.QUESTION_CLAUDE_IDLE
    assert reg.REGISTRY["kilo"].question_detector == reg.QUESTION_NONE
