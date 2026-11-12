"""PROV-002, PROV-011, PROV-012, PROV-013 and DOCS-003: capability matrix, version drift, native refusals, prompt channel."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from llm_tools import bridge_capabilities as caps, bridge_doctor, bridge_format, bridge_providers as reg, scheduler


def _evidence(directory: Path, provider: str, version: str = "1.2.0", **capabilities: bool) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{provider}.json").write_text(json.dumps({
        "schema_version": 1, "provider": provider, "cli_version": version, "date": "2026-11-01", "capabilities": capabilities,
    }), encoding="utf-8")


def test_without_evidence_probe_capabilities_are_unverified_and_unimplemented_ones_unsupported(tmp_path: Path) -> None:
    report = caps.capabilities_report(tmp_path, installed=lambda _name: None)
    by_name = {entry["provider"]: entry for entry in report["providers"]}
    assert by_name["codex"]["capabilities"]["exact_resume"] == "unverified"
    assert by_name["codex"]["evidence"] is None
    assert by_name["kilo"]["capabilities"]["finish"] == "unsupported"
    assert set(by_name["minimax"]["capabilities"].values()) == {"unsupported"}


def test_evidence_turns_unverified_into_supported_or_unsupported(tmp_path: Path) -> None:
    _evidence(tmp_path, "codex", launch=True, exact_resume=False)
    entry = {e["provider"]: e for e in caps.capabilities_report(tmp_path, installed=lambda _n: None)["providers"]}["codex"]
    assert entry["capabilities"]["launch"] == "supported"
    assert entry["capabilities"]["exact_resume"] == "unsupported"
    assert entry["capabilities"]["reply"] == "unverified"
    assert entry["evidence"] == {"date": "2026-11-01", "cli_version": "1.2.0"}


def test_malformed_evidence_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "codex.json").write_text('{"schema_version": 2}', encoding="utf-8")
    assert caps.load_evidence("codex", tmp_path) is None
    (tmp_path / "codex.json").write_text("not json", encoding="utf-8")
    assert caps.load_evidence("codex", tmp_path) is None


def test_registry_change_changes_the_matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(reg.REGISTRY, "kilo", dataclasses.replace(reg.REGISTRY["kilo"], exit_command="/exit"))
    entry = {e["provider"]: e for e in caps.capabilities_report(tmp_path, installed=lambda _n: None)["providers"]}["kilo"]
    assert entry["capabilities"]["finish"] == "supported"


def test_text_and_json_output(tmp_path: Path) -> None:
    text = caps.format_capabilities(caps.capabilities_report(tmp_path, installed=lambda _n: "7.3.45"))
    assert "kilo · installed 7.3.45 · no evidence file" in text
    assert "  exact_resume        unverified" in text
    assert json.loads(caps.run_doctor_capabilities(True))["schema_version"] == 1


def test_generated_document_matches_the_registry() -> None:
    assert caps.DOC_PATH.read_text(encoding="utf-8") == caps.render_markdown()


def test_parser_accepts_capabilities_flag() -> None:
    from llm_tools import bridge_cli_parser

    args = bridge_cli_parser._build_parser().parse_args(["doctor", "--capabilities", "--json"])
    assert args.capabilities is True


@pytest.mark.parametrize(("installed", "drifts"), [("1.2.9", False), ("1.3.0", True), ("2.0.0", True), ("1.1.0", False), (None, False)])
def test_version_drift_warns_only_for_a_newer_minor(installed: str | None, drifts: bool) -> None:
    evidence = {"cli_version": "1.2.0", "date": "2026-11-01"}
    assert bool(caps.version_drift(installed, evidence)) is drifts


def test_doctor_provider_check_warns_on_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _evidence(tmp_path, "codex")
    monkeypatch.setattr(caps, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(caps, "installed_version", lambda _p, run=None: "1.9.0")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "codex").write_text("#!/bin/sh\n")
    (fake / "codex").chmod(0o755)
    check = bridge_doctor.provider_check("codex", env={"PATH": str(fake), "HOME": str(tmp_path)})
    assert check["installed"] is True
    assert "newer than the evidence" in check["version_drift"]
    assert "warning" in bridge_format.provider_check_text(check)


def test_provider_version_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caps, "installed_version", lambda _p, run=None: "3.4.5")
    assert caps.provider_version_record("kilo") == {"provider": "kilo", "cli_version": "3.4.5"}
    monkeypatch.setattr(caps, "installed_version", lambda _p, run=None: None)
    assert caps.provider_version_record("kilo")["cli_version"] == "unknown"


def test_installed_version_reads_version_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "kilo"
    fake.write_text("#!/bin/sh\necho 'kilo 7.3.45'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert caps.installed_version("kilo") == "7.3.45"
    assert caps.installed_version("nonesuch") is None


@pytest.mark.parametrize("provider", ["minimax", "zai"])
def test_native_start_on_unsupported_provider_names_the_route_alternative(provider: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        scheduler.provider_default_argv(scheduler.SchedulerConfig(provider=provider, attached=True), "p")
    assert exc.value.code == 2
    assert "route" in capsys.readouterr().err
    with pytest.raises(ValueError, match="route"):
        reg.check_native_start(provider)


def test_native_start_check_passes_for_native_providers() -> None:
    for provider in ("codex", "claude", "copilot", "kilo", "opencode"):
        reg.check_native_start(provider)


def test_prompt_channel_contract() -> None:
    for name, spec in reg.REGISTRY.items():
        templates = (spec.native_argv, spec.headless_argv, spec.headless_stream_argv)
        carries = any("{prompt}" in (t or ()) for t in templates)
        if spec.prompt_channel != reg.PROMPT_ARGV:
            assert not carries, f"{name} passes the prompt outside argv but its template carries it"
        assert reg.prompt_in_argv(name) == (spec.prompt_channel == reg.PROMPT_ARGV)
    for name in ("kilo", "opencode"):
        assert "no prompt-file or stdin option documented" in reg.REGISTRY[name].prompt_channel_note


def test_operations_doc_states_the_prompt_channel() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "llm-bridge" / "operations.md").read_text(encoding="utf-8")
    assert "Prompt visibility" in text and "Kilo" in text and "OpenCode" in text


def test_each_provider_selection_records_the_cli_version_read_once_per_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools.bridge_runtime import BridgeMissionHooks
    from llm_tools.bridge_store import BridgeStore

    reads: list[str] = []
    monkeypatch.setattr(caps, "installed_version", lambda provider, run=None: reads.append(provider) or "2.4.1")
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    for _ in range(2):
        hook.on_selection({"provider": "codex", "route": "", "rotation_reason": "usable", "all_rate_limited": False,
                           "decision": {"provider": "codex", "usable": True, "reason": "available"}, "decisions": []})
    selections = [event for event in store.events(mission["id"]) if event["kind"] == "provider-selection"]
    assert [event["data"]["cli_version"] for event in selections] == ["2.4.1", "2.4.1"]
    assert reads == ["codex"]


def test_doctor_capabilities_prints_the_matrix_without_a_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from llm_tools import bridge

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(caps, "run_doctor_capabilities", lambda as_json: "MATRIX json" if as_json else "MATRIX")
    assert bridge.main(["doctor", "--capabilities"]) == 0
    assert capsys.readouterr().out.strip() == "MATRIX"
    assert bridge.main(["doctor", "--capabilities", "--json"]) == 0
    assert capsys.readouterr().out.strip() == "MATRIX json"
