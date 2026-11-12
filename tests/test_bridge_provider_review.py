"""Review fixes for PROV-001/002/011/012: version re-read interval, native refusal at real entry points, idle prompt, evidence location."""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge_capabilities as caps, bridge_doctor, bridge_providers as reg


def _evidence(directory: Path, provider: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{provider}.json").write_text(json.dumps({
        "schema_version": 1, "provider": provider, "cli_version": "1.2.0", "date": "2026-11-01", "capabilities": {},
    }), encoding="utf-8")


def test_cli_version_is_reread_at_most_once_an_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools.bridge_runtime import CLI_VERSION_TTL_SECONDS, BridgeMissionHooks
    from llm_tools.bridge_store import BridgeStore

    versions = iter(["1.0.0", "1.0.1", "1.0.2"])
    monkeypatch.setattr(caps, "installed_version", lambda provider, run=None, **_kw: next(versions))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    now = [1000.0]
    hook._clock = lambda: now[0]
    assert hook._cli_version("claude") == "1.0.0"
    now[0] += CLI_VERSION_TTL_SECONDS - 1
    assert hook._cli_version("claude") == "1.0.0"
    now[0] += 2
    assert hook._cli_version("claude") == "1.0.1"


def test_version_probe_uses_a_two_second_timeout(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)

        class Result:
            stdout, stderr = "kilo 1.2.3", ""

        return Result()

    (tmp_path / "kilo").write_text("")
    (tmp_path / "kilo").chmod(0o755)
    assert caps.installed_version("kilo", run, path=str(tmp_path)) == "1.2.3"
    assert seen["timeout"] == 2


def _bridge_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from llm_tools import bridge_missions

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("provider", ["minimax", "zai"])
def test_mission_create_in_native_mode_refuses_with_the_route_alternative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str) -> None:
    from llm_tools.bridge import main

    folder = _bridge_state(tmp_path, monkeypatch)
    code = main(["mission", "create", "--prompt", "x", "--cwd", str(folder), "--mode", "native", "--providers", provider])
    err = capsys.readouterr().err
    assert code != 0 and "route" in err and "native" in err
    capsys.readouterr()
    assert main(["mission", "list", "--json"]) == 0
    assert "x" not in [m.get("objective") for m in json.loads(capsys.readouterr().out).get("missions", [])]


def test_mission_create_minimax_in_headless_mode_is_still_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools.bridge import main

    folder = _bridge_state(tmp_path, monkeypatch)
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(folder), "--mode", "headless", "--providers", "minimax", "--json"]) == 0


def test_start_in_native_mode_refuses_minimax_before_launching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.bridge import main

    folder = _bridge_state(tmp_path, monkeypatch)
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(folder), "--mode", "headless", "--providers", "minimax", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    monkeypatch.setattr(bridge_runtime.shutil, "which", lambda name, **_kw: "/usr/bin/" + name)
    launched: list[object] = []
    monkeypatch.setattr(bridge_runtime.subprocess, "Popen", lambda *a, **k: launched.append(a))
    code = main(["start", "--mission", mission_id, "--mode", "native"])
    err = capsys.readouterr().err
    assert code != 0 and "route" in err and "native" in err
    assert launched == []


def test_home_form_does_not_offer_or_accept_minimax_in_native_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge_launcher
    from llm_tools.bridge_launcher import Launcher

    monkeypatch.setattr(bridge_launcher, "detect_installed", lambda: ("codex", "minimax"))
    monkeypatch.setattr(bridge_launcher.shutil, "which", lambda name: f"/bin/{name}")
    state = Launcher(str(tmp_path), objective="o", providers="minimax", directory=str(tmp_path))
    state.mode = "native"
    assert [name for name, _detail in state.choices()] == ["codex"]
    with pytest.raises(ValueError, match="Native mode.*route"):
        state.validate()
    state.mode = "headless"
    assert "minimax" in [name for name, _detail in state.choices()]
    state.validate()


def test_idle_prompt_detection_reads_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import terminal

    assert terminal._native_input_prompt("codex", "› Ask Codex to do anything\n? for shortcuts") is True
    assert terminal._native_input_prompt("claude", "❯") is True
    assert terminal._native_input_prompt("copilot", "> ") is False
    monkeypatch.setitem(reg.REGISTRY, "claude", dataclasses.replace(reg.REGISTRY["claude"], idle_prompt=None))
    assert terminal._native_input_prompt("claude", "❯") is False
    monkeypatch.setitem(reg.REGISTRY, "copilot", dataclasses.replace(reg.REGISTRY["copilot"], idle_prompt=reg.IdlePrompt(re.compile(r"^> $", re.M))))
    assert terminal._native_input_prompt("copilot", "> ") is True


def test_notice_delivery_is_read_when_used() -> None:
    from llm_tools import bridge_task_notice

    assert not hasattr(bridge_task_notice, "NOTICE_PROVIDERS")


def test_evidence_directory_is_inside_the_package() -> None:
    import llm_tools

    assert caps.EVIDENCE_DIR.parent == Path(llm_tools.__file__).resolve().parent
    assert "provider_evidence/*.json" in (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")


def test_only_doctor_reads_cli_versions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _evidence(tmp_path / "evidence", "codex")
    monkeypatch.setattr(caps, "EVIDENCE_DIR", tmp_path / "evidence")
    calls: list[str] = []
    monkeypatch.setattr(caps, "installed_version", lambda provider, run=None, **_kw: calls.append(provider) or "9.9.9")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "codex").write_text("")
    (fake / "codex").chmod(0o755)
    env = {"PATH": str(fake), "HOME": str(tmp_path)}
    assert "version_drift" not in bridge_doctor.provider_check("codex", env=env)
    assert bridge_doctor.provider_checks([("codex", None)], env) and calls == []
    assert "version_drift" in bridge_doctor.provider_checks([("codex", None)], env, check_versions=True)[0]
    assert calls == ["codex"]
