"""CLI receipts that must not claim success: start, create validation, duplicate folders (CLIUX-001, -002, -010)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_missions, bridge_runtime
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


def _create(tmp_path: Path, capsys, name: str = "work", *extra: str, folder: Path | None = None) -> dict:
    cwd = folder or _folder(tmp_path, name)
    assert main(["mission", "create", "--prompt", f"{name} objective", "--name", name, "--cwd", str(cwd), "--headless", *extra, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def _mission_count(state: Path) -> int:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return int(store.db.execute("SELECT COUNT(*) FROM missions").fetchone()[0])


# ---- CLIUX-001: start must not report success when nothing runs -----------------------------------


def test_start_refuses_a_cancelled_mission(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys, "gone", "--providers", "codex")["mission"]["id"]
    assert main(["cancel", "--mission", mid, "--yes"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: pytest.fail("a cancelled mission must not launch a controller"))

    assert main(["start", "--mission", mid]) == 4
    captured = capsys.readouterr()
    assert f"mission {mid[:8]} was cancelled and cannot be started" in captured.err
    assert "mission create --prompt" in captured.err
    assert "running" not in captured.out


def test_start_refuses_a_completed_mission(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys, "done", "--providers", "codex")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (mid,))
        store.db.commit()
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: pytest.fail("a completed mission must not launch a controller"))
    assert main(["start", "--mission", mid]) == 4
    assert "finished (completed)" in capsys.readouterr().err


def test_launch_reports_a_controller_that_started_nothing(tmp_path, state, capsys, monkeypatch) -> None:
    """A controller that reports ready for a cancelled mission started no provider; the launch says so."""
    mid = _create(tmp_path, capsys, "late", "--providers", "codex")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "CANCELLED")
    with pytest.raises(RuntimeError, match=f"mission {mid[:8]} is cancelled, so no provider was started"):
        bridge_runtime.launch_controller(mid, headless=True)
    assert "running" not in capsys.readouterr().out


def test_launch_prints_the_failure_reason_with_a_fix_for_a_rotation_the_controller_rejects(tmp_path, state, capsys) -> None:
    """The controller fails before it reports ready; the launch raises with the recorded reason and the fix command."""
    mid = _create(tmp_path, capsys, "typo", "--providers", "codex")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_config_value(mid, "providers", "claud", event_kind="test-config")
    with pytest.raises(RuntimeError) as raised:
        bridge_runtime.launch_controller(mid, headless=True)
    message = str(raised.value)
    assert message.startswith(f"mission {mid[:8]} could not start: unknown rotation entry 'claud' · did you mean claude?")
    assert f"fix it with: llm-bridge rotation --mission {mid[:8]} --set LIST" in message
    assert "running" not in capsys.readouterr().out


def test_start_refuses_a_saved_rotation_with_an_unknown_provider(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys, "typo2", "--providers", "codex")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_config_value(mid, "providers", "claud", event_kind="test-config")
    assert main(["start", "--mission", mid]) == 2
    captured = capsys.readouterr()
    assert "unknown rotation entry 'claud' · did you mean claude?" in captured.err
    assert "running" not in captured.out


# ---- CLIUX-002: validate rotation entries at creation and start ---------------------------------


def test_create_rejects_an_unknown_provider_with_a_suggestion(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "p")
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(folder), "--providers", "claud"]) == 2
    captured = capsys.readouterr()
    assert "unknown rotation entry 'claud' · did you mean claude?" in captured.err
    assert captured.out == ""
    assert not (state / "bridge.sqlite3").exists() or _mission_count(state) == 0


def test_create_rejects_an_unknown_route_and_a_mixed_list(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "p")
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(folder), "--routes", "nosuch"]) == 2
    assert "unknown rotation entry 'nosuch'" in capsys.readouterr().err
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(folder), "--providers", "claude,codx"]) == 2
    assert "did you mean codex" in capsys.readouterr().err


def test_legacy_prompt_mode_validates_the_rotation_too(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "p")
    assert main(["--prompt", "z", "--cwd", str(folder), "--providers", "claud", "--no-start"]) == 2
    assert "did you mean claude?" in capsys.readouterr().err


def test_start_with_an_unknown_provider_changes_nothing(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys, "keep", "--providers", "codex")["mission"]["id"]
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: pytest.fail("an unknown provider must not launch a controller"))
    assert main(["start", "--mission", mid, "--providers", "claud"]) == 2
    assert "did you mean claude?" in capsys.readouterr().err
    assert main(["start", "--mission", mid, "--routes", "nosuch"]) == 2
    assert "unknown rotation entry 'nosuch'" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["config"]["providers"] == "codex"


def test_create_warns_about_an_installed_provider_that_is_not_signed_in(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.delenv("LLM_BRIDGE_NO_SIGNIN_CHECK")
    claude_auth = Path.home() / ".claude" / ".credentials.json"
    claude_auth.parent.mkdir(parents=True)
    claude_auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token"}}), encoding="utf-8")
    folder = _folder(tmp_path, "p")
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(folder), "--providers", "claude,codex", "--json"]) == 0
    captured = capsys.readouterr()
    assert "warning: codex is not signed in (run: codex login); it will be skipped until it is" in captured.err
    assert "claude is not signed in" not in captured.err
    assert json.loads(captured.out)["mission"]["id"]  # a warning never fails the command
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(_folder(tmp_path, "q")), "--providers", "codex", "--json"]) == 0
    monkeypatch.setenv("LLM_BRIDGE_NO_SIGNIN_CHECK", "1")
    capsys.readouterr()
    assert main(["mission", "create", "--prompt", "z", "--cwd", str(_folder(tmp_path, "r")), "--providers", "codex", "--json"]) == 0
    assert "not signed in" not in capsys.readouterr().err


def test_start_warns_about_an_explicit_provider_that_is_not_signed_in(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys, "w", "--providers", "claude")["mission"]["id"]
    monkeypatch.delenv("LLM_BRIDGE_NO_SIGNIN_CHECK")
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: 0)
    assert main(["start", "--mission", mid, "--providers", "codex"]) == 0
    assert "warning: codex is not signed in" in capsys.readouterr().err


# ---- CLIUX-010: duplicate missions in a folder ------------------------------------------------------


def test_create_warns_when_the_folder_already_has_an_unfinished_mission(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "repo")
    first = _create(tmp_path, capsys, "parser", "--providers", "codex", folder=folder)["mission"]["id"]
    assert main(["mission", "create", "--prompt", "again", "--name", "parser", "--cwd", str(folder), "--headless", "--providers", "codex"]) == 0
    captured = capsys.readouterr()
    assert f'warning: this folder already has unfinished mission {first[:8]} "parser" (CREATED);' in captured.err
    assert f"llm-bridge task add --mission {first[:8]}" in captured.err
    assert "--new" in captured.err
    assert captured.out.startswith("Created mission ")  # the second mission is still created
    assert _mission_count(state) == 2


def test_create_new_flag_silences_the_duplicate_warning(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "repo")
    _create(tmp_path, capsys, "parser", "--providers", "codex", folder=folder)
    assert main(["mission", "create", "--prompt", "again", "--cwd", str(folder), "--headless", "--providers", "codex", "--new"]) == 0
    assert capsys.readouterr().err == ""


def test_create_does_not_warn_about_a_finished_mission(tmp_path, state, capsys) -> None:
    folder = _folder(tmp_path, "repo")
    first = _create(tmp_path, capsys, "parser", "--providers", "codex", folder=folder)["mission"]["id"]
    assert main(["cancel", "--mission", first, "--yes"]) == 0
    capsys.readouterr()
    assert main(["mission", "create", "--prompt", "again", "--cwd", str(folder), "--headless", "--providers", "codex"]) == 0
    assert capsys.readouterr().err == ""


def test_ambiguity_listing_shows_when_each_mission_was_updated(tmp_path, state, capsys, monkeypatch) -> None:
    folder = _folder(tmp_path, "repo")
    first = _create(tmp_path, capsys, "same name", "--providers", "codex", folder=folder)["mission"]["id"]
    second = _create(tmp_path, capsys, "same name", "--providers", "codex", folder=folder)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE missions SET updated_at=? WHERE id=?", ("2026-03-04T05:06:07+00:00", first))
        store.db.commit()
    monkeypatch.chdir(folder)
    assert main(["status"]) == 3
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("  ")]
    assert len(lines) == 2 and all(" updated " in line for line in lines)
    row = next(line for line in lines if line.startswith(f"  {first[:8]}"))
    assert "updated 2026-03-0" in row  # local time; the date can shift by a day near midnight
    assert any(line.startswith(f"  {second[:8]}") for line in lines)


def test_suggested_commands_name_the_mission(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys, "n", "--providers", "codex")
    mid = created["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(created["task"]["id"], iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    assert main(["status", "--mission", mid]) == 0
    status = capsys.readouterr().out
    assert f"llm-bridge task approve --mission {mid[:8]} --task 1" in status
    assert f"llm-bridge task reject --mission {mid[:8]} --task 1" in status
    assert main(["task", "verify", "--mission", mid, "--task", "1", "--evidence", "x"]) == 0
    assert f"llm-bridge task approve --mission {mid[:8]} --task 1" in capsys.readouterr().out
