from __future__ import annotations

import json
import io
import os
import socket
import subprocess
import sys
import threading
from typing import Any
from urllib.error import HTTPError
from pathlib import Path

import pytest

from llm_tools import common, copilot_refresh, ralph_robin, scheduler, usage, usage_service
from llm_tools.capacity import CapacityKind, CapacityScope, ProviderSnapshot

from .conftest import ROOT, run_cmd, write_exe


def test_usage_option_branches_and_unavailable(env: dict[str, str], tmp_path: Path) -> None:
    base = env | {"LLM_USAGE_DISABLE_COPILOT": "1"}
    assert run_cmd(["./llm-usage", "--no-header", "--hide-remaining-time", "--hide-source"], base).returncode == 0
    assert usage.parse_args(["--no-service"]).use_service is False
    assert usage.parse_args(["--service-run"]).service_action == "run"
    assert usage.parse_args(["--service-foreground"]).service_action == "run"
    assert usage.parse_args(["--service-install"]).service_action == "install"
    assert usage.parse_args(["--service-uninstall"]).service_action == "uninstall"
    assert usage.parse_args(["--service-start"]).service_action == "start"
    assert usage.parse_args(["--service-stop"]).service_action == "stop"
    assert usage.parse_args(["--service-status"]).service_action == "status"
    assert usage.parse_args(["--service-interval", "15"]).service_interval == "15"
    with pytest.raises(SystemExit):
        usage.parse_args(["--service-interval"])
    with pytest.raises(SystemExit):
        usage.parse_args(["--service-interval", "4"])
    bad_offset = run_cmd(["./llm-usage", "--copilot-monthly-reset-offset-days", "x"], env)
    assert bad_offset.returncode == 2
    assert "expects an integer" in bad_offset.stderr
    missing_value = run_cmd(["./llm-usage", "--copilot-monthly-reset-offset-days"], env)
    assert missing_value.returncode == 2
    unknown = run_cmd(["./llm-usage", "--bad"], env)
    assert unknown.returncode == 2
    no_footer = run_cmd(["./llm-usage", "--json"], env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "No footer here"})
    assert json.loads(no_footer.stdout)["copilot"]["reason"] == "format-changed"
    timeout = common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_CMD": "sleep 2", "LLM_USAGE_COPILOT_TIMEOUT": "1"})
    assert timeout["available"] is False


def test_usage_main_inprocess_and_render_helpers(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert usage.main(["--statusline"]) == 0
    assert capsys.readouterr().out.strip() == "Claude"
    cfg = usage.Config()
    cfg.no_header = True
    cfg.show_source = True
    cfg.show_remaining_time = False
    usage.print_unavailable_rows(cfg, "Missing")
    out = capsys.readouterr().out
    assert "Missing" in out


def test_usage_watch_interrupt_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(_cfg: usage.Config) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(usage, "render_watch_frame", interrupt)
    monkeypatch.setattr(usage.sys.stdout, "isatty", lambda: False, raising=False)
    assert usage.main(["--watch", "1", "--no-service"]) == 130


def test_usage_service_snapshot_roundtrip(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = usage.Config()
    provider_data = {
        "codex": {
            "provider": "codex",
            "available": True,
            "source": "codex cache",
            "rows": [
                {
                    "name": "Codex",
                    "five_hour": {"used": 20.0},
                    "week": {"used": 40.0},
                }
            ],
        },
        "claude": ProviderSnapshot(
            provider="claude",
            available=True,
            source="claude api",
            scopes=[
                CapacityScope(name="5h", kind=CapacityKind.RESET_WINDOW, remaining_percent=60.0),
                CapacityScope(name="weekly", kind=CapacityKind.RESET_WINDOW, remaining_percent=70.0),
            ],
            model_scopes=[
                CapacityScope(
                    name="weekly",
                    kind=CapacityKind.RESET_WINDOW,
                    remaining_percent=42.0,
                    extras={"model": "Sonnet"},
                )
            ],
        ),
        "copilot": ProviderSnapshot(
            provider="copilot",
            available=True,
            source="copilot cli",
            scopes=[
                CapacityScope(
                    name="monthly",
                    kind=CapacityKind.RESET_WINDOW,
                    remaining_percent=75.0,
                    reset_epoch=1800000000,
                )
            ],
            model_scopes=[
                CapacityScope(
                    name="balance",
                    kind=CapacityKind.BALANCE,
                    remaining_amount=3.25,
                    currency="$",
                    source="github billing",
                )
            ],
        ),
        "kilo": ProviderSnapshot(provider="kilo", available=False, reason="missing-cli", source="kilo cli"),
        "opencode": ProviderSnapshot(provider="opencode", available=False, reason="missing-cli", source="opencode cli"),
        "minimax": ProviderSnapshot(
            provider="minimax",
            available=True,
            source="mmx cli",
            scopes=[
                CapacityScope(
                    name="5h",
                    kind=CapacityKind.RESET_WINDOW,
                    remaining_percent=55.0,
                )
            ],
        ),
    }
    payload = usage.service_payload_from_provider_data(cfg, provider_data, env)
    assert payload["environment_fingerprint"] == usage.service_environment_fingerprint(env)
    assert usage.service_payload_matches_environment(payload, env) is True
    assert usage.service_payload_matches_environment({"providers": {}}, env) is False
    restored = usage.provider_data_from_service_payload(payload)
    assert restored is not None
    assert restored["copilot"].available is True
    assert restored["copilot"].scopes[0].remaining_percent == 75.0
    assert restored["copilot"].model_scopes[0].remaining_amount == 3.25
    calls: list[tuple[str, str, float | None]] = []
    monkeypatch.setattr(common, "log_usage_sample", lambda provider, window, remaining: calls.append((provider, window, remaining)))
    usage.log_samples_from_provider_data(provider_data)
    usage.log_samples_from_provider_data(provider_data | {"codex": {"provider": "codex", "available": True, "five_hour": {"used": 10.0}, "week": {"used": 30.0}}})
    assert ("Claude", "5h", 60.0) in calls
    assert ("Claude Sonnet", "weekly", 42.0) in calls
    assert ("Codex", "5h", 80.0) in calls
    assert ("Codex", "weekly", 70.0) in calls
    assert ("copilot", "monthly", 75.0) in calls
    assert ("MiniMax", "5h", 55.0) in calls


def test_watch_frame_uses_service_snapshot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("LLM_USAGE_NO_SERVICE", raising=False)
    cfg = usage.Config()
    cfg.json_output = True
    provider_data = {
        "codex": {
            "provider": "codex",
            "available": True,
            "source": "service",
            "five_hour": {"used": 25.0},
            "week": {"used": 50.0},
        },
        "claude": usage.unavailable_snapshot("claude", "service"),
        "copilot": usage.unavailable_snapshot("copilot", "service"),
        "kilo": usage.unavailable_snapshot("kilo", "service"),
        "opencode": usage.unavailable_snapshot("opencode", "service"),
        "minimax": usage.unavailable_snapshot("minimax", "service"),
    }
    payload = usage.service_payload_from_provider_data(cfg, provider_data)
    payload["generated_at"] = "2026-06-16T22:37:00+01:00"
    monkeypatch.setattr(usage_service, "request_snapshot", lambda **_kwargs: payload)
    monkeypatch.setattr(usage, "_fetch_provider_data", lambda *_args, **_kwargs: pytest.fail("watch bypassed service"))

    assert usage.render_once_via_service(cfg) is True
    once = json.loads(capsys.readouterr().out)
    usage.render_watch_frame(cfg)
    watch = json.loads(capsys.readouterr().out)
    assert watch == once
    assert watch["generated_at"] == "2026-06-16T22:37:00+01:00"
    assert watch["codex"]["available"] is True


def test_service_snapshot_rejected_when_environment_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_USAGE_NO_SERVICE", raising=False)
    cfg = usage.Config()
    provider_data = {
        "codex": {"provider": "codex", "available": False, "reason": "missing-cli", "source": "service"},
        "claude": usage.unavailable_snapshot("claude", "service"),
        "copilot": usage.unavailable_snapshot("copilot", "service"),
        "kilo": usage.unavailable_snapshot("kilo", "service"),
        "opencode": usage.unavailable_snapshot("opencode", "service"),
        "minimax": usage.unavailable_snapshot("minimax", "service"),
    }
    service_env = dict(os.environ)
    service_env["PATH"] = "/old-service-path"
    payload = usage.service_payload_from_provider_data(cfg, provider_data, service_env)
    monkeypatch.setattr(usage_service, "request_snapshot", lambda **_kwargs: payload)
    monkeypatch.setenv("PATH", "/current-client-path")

    assert usage.render_once_via_service(cfg) is False


def test_usage_service_ephemeral_client(env: dict[str, str]) -> None:
    live_env = env | {
        "LLM_USAGE_DISABLE_COPILOT": "1",
        "LLM_USAGE_PROVIDER_PARALLELISM": "1",
        "LLM_USAGE_SERVICE_INTERVAL": "60",
        "LLM_USAGE_NO_PROGRESS": "1",
        "PATH": f"{env['PATH'].split(':', 1)[0]}:{Path(sys.executable).parent}:{ROOT}",
    }
    live_env.pop("LLM_USAGE_NO_SERVICE", None)
    result = run_cmd(["./llm-usage", "--json"], live_env, timeout=20)
    assert result.returncode == 0, result.stderr
    obj = json.loads(result.stdout)
    assert obj["codex"]["available"] is False
    svc_dir = common.usage_cache_dir(live_env) / "service"
    assert (svc_dir / "latest.json").is_file()
    assert (svc_dir / "history.jsonl").is_file()

    status = run_cmd(["./llm-usage", "--service-status", "--json"], live_env, timeout=10)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["running"] is False


def test_usage_service_unit_templates(env: dict[str, str]) -> None:
    unit = usage_service.systemd_unit_text(30, env)
    plist = usage_service.launchd_plist_text(30, env)
    assert "llm_tools.usage_service" in unit
    assert "--interval 30" in unit
    assert "llm_tools.usage_service" in plist
    assert "<string>30</string>" in plist


def test_service_path_expands_home_from_env_not_os_environ(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``_service_path`` bakes ``~/.local/bin`` into the unit PATH by expanding
    # ``~``. The expansion must come from ``env["HOME"]`` (hermetic), never the
    # ambient ``os.environ["HOME"]`` -- otherwise the unit leaks the real home.
    env_local_bin = Path(env["HOME"]) / ".local" / "bin"
    env_local_bin.mkdir(parents=True)
    # Point the *ambient* HOME at a different dir that has no ``.local/bin`` so a
    # regression (expanding against os.environ) would silently drop the entry.
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    monkeypatch.setenv("HOME", str(other_home))

    entries = usage_service._service_path(env).split(os.pathsep)
    assert str(env_local_bin) in entries
    assert str(other_home / ".local" / "bin") not in entries


def test_service_path_drops_missing_dirs_and_keeps_present(
    env: dict[str, str], tmp_path: Path
) -> None:
    # Only directories that actually exist on disk are baked in: a
    # fallback like /usr/local/bin is skipped when absent, and the
    # ``~`` (bare, no slash) expansion resolves to HOME too.
    present = tmp_path / "present-bin"
    present.mkdir()
    env = {**env, "HOME": str(tmp_path), "PATH": str(present)}
    entries = usage_service._service_path(env).split(os.pathsep)
    assert str(present) in entries
    # A non-existent conventional fallback is never emitted.
    assert "/this/does/not/exist" not in entries
    assert entries  # at least the present dir survived


def test_usage_service_paths_io_and_cached_snapshot(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    no_runtime = dict(env)
    no_runtime.pop("XDG_RUNTIME_DIR", None)
    assert str(usage_service.runtime_dir(no_runtime)).endswith(f"llm-tools-{os.getuid()}")
    assert usage_service.systemd_unit_path(env).name == "llm-usage.service"
    assert usage_service.launchd_plist_path(env).name == "com.llm-tools.llm-usage.plist"
    assert usage_service._parse_interval("2") == 5
    assert usage_service._parse_interval("bad") == usage_service.DEFAULT_INTERVAL_SECONDS
    assert usage_service._parse_interval("15") == 15

    target = tmp_path / "nested" / "latest.json"
    usage_service._atomic_json_write(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    hist = tmp_path / "nested" / "history.jsonl"
    usage_service._append_history(hist, {"n": 1})
    usage_service._append_history(hist, {"n": 2})
    assert [json.loads(line)["n"] for line in hist.read_text(encoding="utf-8").splitlines()] == [1, 2]

    monkeypatch.setenv("LLM_USAGE_SERVICE_TEST_OUTER", "outer")
    with usage_service._temporary_environ({"LLM_USAGE_SERVICE_TEST_INNER": "inner"}):
        assert os.environ.get("LLM_USAGE_SERVICE_TEST_INNER") == "inner"
        assert os.environ.get("LLM_USAGE_SERVICE_TEST_OUTER") is None
    assert os.environ.get("LLM_USAGE_SERVICE_TEST_OUTER") == "outer"

    sampler = usage_service.UsageSampler(env, interval=60)
    payload = {"generated_at_epoch": common.now_epoch(env), "providers": {}}
    sampler.paths.latest.parent.mkdir(parents=True, exist_ok=True)
    sampler.paths.latest.write_text(json.dumps(payload), encoding="utf-8")
    assert sampler.snapshot()["generated_at_epoch"] == payload["generated_at_epoch"]

    stale = {"generated_at_epoch": 1, "providers": {}}
    sampler.latest = stale
    monkeypatch.setattr(sampler, "sample_once", lambda: {"generated_at_epoch": 2, "providers": {}})
    assert sampler.snapshot(max_age=1)["generated_at_epoch"] == 2
    assert usage_service._request(tmp_path / "missing.sock", {"op": "snapshot"}, timeout=0.01) is None


def test_usage_service_json_protocol(env: dict[str, str], tmp_path: Path) -> None:
    class DummySampler:
        def snapshot(self, max_age=None):  # type: ignore[no-untyped-def]
            return {"generated_at_epoch": 123, "max_age": max_age}

    sock = tmp_path / "svc.sock"
    stop = threading.Event()
    server = usage_service.ThreadingUnixServer(str(sock), DummySampler(), stop)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        snap = usage_service._request(sock, {"op": "snapshot", "max_age": 7})
        assert snap is not None and snap["snapshot"]["max_age"] == 7
        status = usage_service._request(sock, {"op": "status"})
        assert status is not None and status["generated_at_epoch"] == 123
        unknown = usage_service._request(sock, {"op": "bogus"})
        assert unknown == {"ok": False, "error": "unknown-op"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock))
            client.sendall(b"not json\n")
            assert json.loads(client.recv(1024).decode("utf-8"))["error"] == "bad-request"
        shutdown = usage_service._request(sock, {"op": "shutdown"})
        assert shutdown == {"ok": True}
        assert stop.is_set()
    finally:
        server.shutdown()
        server.server_close()


def test_usage_service_manager_fallbacks(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(usage_service.shutil, "which", lambda _name, path=None: None)

    monkeypatch.setattr(usage_service.platform, "system", lambda: "Linux")
    assert usage_service.install_service(30, env) == 0
    assert usage_service.systemd_unit_path(env).is_file()
    assert usage_service.uninstall_service(env) == 0
    assert not usage_service.systemd_unit_path(env).exists()
    assert usage_service.start_service(env) == 2
    assert "no supported service manager" in capsys.readouterr().err
    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: {"ok": True})
    assert usage_service.stop_service(env) == 0

    monkeypatch.setattr(usage_service.platform, "system", lambda: "Darwin")
    assert usage_service.install_service(30, env) == 0
    assert usage_service.launchd_plist_path(env).is_file()
    assert usage_service.uninstall_service(env) == 0
    assert not usage_service.launchd_plist_path(env).exists()

    monkeypatch.setattr(usage_service.platform, "system", lambda: "Other")
    assert usage_service.install_service(30, env) == 2
    assert usage_service.uninstall_service(env) == 2

    run_calls: list[list[str]] = []
    monkeypatch.setattr(usage_service, "_run", lambda cmd: run_calls.append(cmd) or 0)
    monkeypatch.setattr(usage_service.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(usage_service.platform, "system", lambda: "Linux")
    assert usage_service.install_service(45, env) == 0
    assert any(cmd[:3] == ["systemctl", "--user", "enable"] for cmd in run_calls)
    assert usage_service.start_service(env) == 0
    assert usage_service.stop_service(env) == 0
    assert usage_service.uninstall_service(env) == 0

    run_calls.clear()
    monkeypatch.setattr(usage_service.platform, "system", lambda: "Darwin")
    assert usage_service.install_service(45, env) == 0
    assert any("bootstrap" in cmd for cmd in run_calls)
    assert usage_service.start_service(env) == 0
    assert usage_service.stop_service(env) == 0
    assert usage_service.uninstall_service(env) == 0


def test_usage_service_cli_and_status_helpers(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        usage_service,
        "run_service",
        lambda *, interval, ephemeral, env=None: calls.append((interval, ephemeral)) or 0,
    )
    assert usage_service.service_cli(["--ephemeral", "--interval", "bad"]) == 0
    assert calls == [(usage_service.DEFAULT_INTERVAL_SECONDS, True)]

    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: {"ok": True, "pid": 123})
    assert usage_service.running_status(env)["running"] is True
    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: None)
    assert usage_service.running_status(env)["running"] is False


def test_usage_service_action_dispatch(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, int | None]] = []
    monkeypatch.setattr(usage_service, "run_service", lambda *, interval, ephemeral, env=None: calls.append(("run", interval)) or 0)
    monkeypatch.setattr(usage_service, "install_service", lambda interval, env=None: calls.append(("install", interval)) or 0)
    monkeypatch.setattr(usage_service, "uninstall_service", lambda env=None: calls.append(("uninstall", None)) or 0)
    monkeypatch.setattr(usage_service, "start_service", lambda env=None: calls.append(("start", None)) or 0)
    monkeypatch.setattr(usage_service, "stop_service", lambda env=None: calls.append(("stop", None)) or 0)
    monkeypatch.setattr(usage_service, "socket_path", lambda env=None: Path("/tmp/llm-usage.sock"))

    for action, expected in [
        ("run", ("run", 9)),
        ("install", ("install", 9)),
        ("uninstall", ("uninstall", None)),
        ("start", ("start", None)),
        ("stop", ("stop", None)),
    ]:
        cfg = usage.Config()
        cfg.service_action = action
        cfg.service_interval = "9"
        assert usage.handle_service_action(cfg) == 0
        assert calls[-1] == expected
    out = capsys.readouterr().out
    assert "service installed" in out
    assert "service uninstalled" in out

    monkeypatch.setattr(usage_service, "running_status", lambda env=None: {"running": True, "pid": 123, "socket": "sock", "generated_at_epoch": 1800000000})
    cfg = usage.Config()
    cfg.service_action = "status"
    assert usage.handle_service_action(cfg) == 0
    assert "service running" in capsys.readouterr().out
    cfg.json_output = True
    assert usage.handle_service_action(cfg) == 0
    assert json.loads(capsys.readouterr().out)["running"] is True
    cfg.service_action = "unknown"
    assert usage.handle_service_action(cfg) == 2


def test_usage_service_request_snapshot_branches(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: {"ok": True, "snapshot": {"providers": {}}})
    assert usage_service.request_snapshot(env=env) == {"providers": {}}

    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: None)
    assert usage_service.request_snapshot(env=env, start_ephemeral=False) is None
    monkeypatch.setattr(usage_service, "start_ephemeral_service", lambda *_args, **_kwargs: None)
    assert usage_service.request_snapshot(env=env) is None

    class FakeProc:
        def __init__(self) -> None:
            self.terminated = False

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            raise TimeoutError

        def terminate(self) -> None:
            self.terminated = True

    proc = FakeProc()
    monkeypatch.setattr(usage_service, "start_ephemeral_service", lambda *_args, **_kwargs: proc)
    assert usage_service.request_snapshot(env=env) is None
    assert proc.terminated is True


def test_usage_service_request_snapshot_reuses_recent_disk_snapshot(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000", "LLM_USAGE_SERVICE_INTERVAL": "60"}
    provider_data = {
        "codex": {"provider": "codex", "available": True, "source": "cache", "five_hour": {"used": 11}},
        "claude": usage.unavailable_snapshot("claude", "claude api"),
        "copilot": usage.unavailable_snapshot("copilot", "copilot cli"),
        "kilo": usage.unavailable_snapshot("kilo", "kilo cli"),
        "opencode": usage.unavailable_snapshot("opencode", "opencode cli"),
        "minimax": usage.unavailable_snapshot("minimax", "mmx cli"),
    }
    payload = usage.service_payload_from_provider_data(usage.Config(), provider_data, live_env)
    payload["generated_at_epoch"] = 980
    usage_service._atomic_json_write(usage_service.latest_path(live_env), payload)
    monkeypatch.setattr(usage_service, "_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        usage_service,
        "start_ephemeral_service",
        lambda *_args, **_kwargs: pytest.fail("recent disk snapshot should be reused"),
    )

    assert usage_service.request_snapshot(env=live_env) == payload

    payload["generated_at_epoch"] = 900
    usage_service._atomic_json_write(usage_service.latest_path(live_env), payload)
    monkeypatch.setattr(usage_service, "start_ephemeral_service", lambda *_args, **_kwargs: None)
    assert usage_service.request_snapshot(env=live_env) is None


def test_read_claude_api_refreshes_oauth_token(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-token",
                    "refreshToken": "refresh-token",
                    "expiresAt": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    requests: list[tuple[str, bytes | None, str | None]] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        url = req.full_url
        data = req.data
        auth = req.headers.get("Authorization")
        requests.append((url, data, auth))
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer expired-token":
            raise HTTPError(url, 401, "unauthorized", hdrs=None, fp=None)
        if url == common.CLAUDE_OAUTH_TOKEN_URL:
            # Anthropic's refresh endpoint requires a JSON body. The historical
            # ``application/x-www-form-urlencoded`` body (and the legacy
            # URL-form client_id) returns HTTP 400 and silently breaks Claude
            # usage after every access-token expiry, which is what made the
            # dashboard report ``unavailable`` for hours at a time.
            assert req.get_header("Content-type") == "application/json"
            payload = json.loads((data or b"").decode("utf-8"))
            assert payload == {
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token",
                "client_id": common.CLAUDE_OAUTH_CLIENT_ID,
            }
            return FakeResponse('{"access_token":"fresh-token","refresh_token":"fresh-refresh","expires_in":3600}')
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer fresh-token":
            return FakeResponse(
                '{"rate_limits":{"five_hour":{"used_percentage":12,"resets_at":"2026-06-14T18:00:00Z"},'
                '"seven_day":{"used_percentage":34,"resets_at":"2026-06-20T18:00:00Z"}}}'
            )
        raise AssertionError(f"unexpected request: {url} auth={auth!r}")

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude_api(env)
    assert data is not None
    assert data["five_hour"]["used"] == 12
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["claudeAiOauth"]["accessToken"] == "fresh-token"
    assert saved["claudeAiOauth"]["refreshToken"] == "fresh-refresh"
    assert [item[0] for item in requests] == [
        common.CLAUDE_OAUTH_USAGE_URL,
        common.CLAUDE_OAUTH_TOKEN_URL,
        common.CLAUDE_OAUTH_USAGE_URL,
    ]


def test_read_claude_api_refresh_targets_anthropic_json_endpoint(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: Anthropic's ``/v1/oauth/token`` endpoint speaks JSON, not
    form-encoded, and lives on ``api.anthropic.com`` (not the
    ``platform.claude.com`` URL used for the initial code exchange). The
    historical code posted form-encoded data to the platform host, which the
    endpoint rejected with HTTP 400 ``Invalid request format``; every refresh
    silently returned ``None`` so the dashboard degraded to ``unavailable``
    after the access token's 8h lifetime — a long-standing gap that this test
    pins so a future refactor cannot regress it back to the broken shape."""
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-token",
                    "refreshToken": "refresh-token",
                    "expiresAt": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        url = req.full_url
        auth = req.get_header("Authorization")
        # Only the token-endpoint request is relevant to the regression; we
        # capture it explicitly so the later usage-call does not clobber the
        # captured fields.
        if url == common.CLAUDE_OAUTH_TOKEN_URL:
            captured["url"] = url
            captured["content_type"] = req.get_header("Content-type")
            captured["body"] = (req.data or b"").decode("utf-8")
            return FakeResponse(
                '{"access_token":"fresh-token","refresh_token":"fresh-refresh","expires_in":3600}'
            )
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer expired-token":
            raise HTTPError(url, 401, "unauthorized", hdrs=None, fp=None)
        if url == common.CLAUDE_OAUTH_USAGE_URL and auth == "Bearer fresh-token":
            return FakeResponse(
                '{"rate_limits":{"five_hour":{"used_percentage":12,"resets_at":"2026-06-14T18:00:00Z"},'
                '"seven_day":{"used_percentage":34,"resets_at":"2026-06-20T18:00:00Z"}}}'
            )
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    assert common.read_claude_api(env) is not None

    assert captured["url"] == "https://api.anthropic.com/v1/oauth/token"
    assert captured["content_type"] in {"application/json", "application/json; charset=utf-8"}
    assert json.loads(captured["body"]) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    }
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["claudeAiOauth"]["accessToken"] == "fresh-token"
    assert saved["claudeAiOauth"]["refreshToken"] == "fresh-refresh"


def test_read_claude_api_uses_claude_code_oauth_token_env_var(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``CLAUDE_CODE_OAUTH_TOKEN`` (set by ``claude setup-token``)
    is a 1-year token that ships without a refresh token, so the OAuth refresh
    path is a dead end for it. The dashboard must honour the env var directly
    — otherwise users whose credentials file lost the refresh token (e.g. after
    a stale ``claude auth logout`` or a CLI upgrade) cannot get fresh Claude
    usage at all, even when they have a perfectly valid long-lived token in
    scope. The env var wins over the credentials file's access token."""
    cred = Path(env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    # Expired + no refresh token so the OAuth path is dead.
                    "accessToken": "stale-file-token",
                    "refreshToken": "",
                    "expiresAt": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    token_env = env | {"CLAUDE_CODE_OAUTH_TOKEN": "long-lived-env-token"}
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        # Only the env-var token must reach the server; the credentials file's
        # stale token must not.
        if req.get_header("Authorization") != "Bearer long-lived-env-token":
            raise AssertionError(
                f"unexpected Authorization header: {req.get_header('Authorization')!r}"
            )
        return FakeResponse(
            '{"rate_limits":{"five_hour":{"used_percentage":42,"resets_at":"2026-06-18T12:00:00Z"},'
            '"seven_day":{"used_percentage":21,"resets_at":"2026-06-20T12:00:00Z"}}}'
        )

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude_api(token_env)
    assert data is not None
    assert data["five_hour"]["used"] == 42
    assert data["week"]["used"] == 21
    assert captured["url"] == common.CLAUDE_OAUTH_USAGE_URL
    # The refresh endpoint must NOT be touched when the env var is set.
    assert "token_url" not in captured


def test_read_claude_api_degrades_to_cache_when_refresh_token_missing(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the access token is rejected *and* there is no refresh token
    to renew it, the OAuth refresh path is a dead end. The reader must
    never drive an interactive ``claude auth login`` — the dashboard
    must never block on a prompt. Instead it degrades to the most recent
    cached usage so the numbers still render. Claude Code rewrites
    ``~/.claude/.credentials.json`` on its own whenever it is used, so a
    later read recovers with no action from the user."""
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000"}
    cred = Path(live_env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "expired-token", "refreshToken": "", "expiresAt": 0}}
        ),
        encoding="utf-8",
    )
    cache = common.usage_cache_dir(live_env) / "claude-usage-api.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        '{"rate_limits":{"five_hour":{"used_percentage":20,"resets_at":"2026-06-18T00:00:00Z"}}}',
        encoding="utf-8",
    )
    os.utime(cache, (1000, 1000))

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        if req.full_url == common.CLAUDE_OAUTH_USAGE_URL:
            raise HTTPError(req.full_url, 401, "unauthorized", hdrs=None, fp=None)
        raise AssertionError(f"unexpected request: {req.full_url}")

    def no_spawn(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("read_claude_api must never spawn a subprocess")

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.subprocess, "Popen", no_spawn)
    data = common.read_claude_api(live_env)
    assert data is not None
    assert data.get("reason") != "needs-reauth"
    assert data["five_hour"]["used"] == 20


def test_read_claude_falls_through_to_local_when_oauth_dead(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected access token, no refresh token, and no cached API usage
    must fall through to the local ``~/.claude/projects`` snapshot rather
    than degrade to ``unavailable`` — usage still shows."""
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000"}
    cred = Path(live_env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "expired-token", "refreshToken": "", "expiresAt": 0}}
        ),
        encoding="utf-8",
    )
    project = Path(live_env["HOME"]) / ".claude" / "projects" / "r.jsonl"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        '{"message":{"rateLimits":{"fiveHour":{"usedPercent":6,"resetsAt":"2026-06-18T00:00:00Z"}}}}\n',
        encoding="utf-8",
    )
    os.utime(project, (1000, 1000))

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        raise HTTPError(req.full_url, 401, "unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude(live_env)
    assert data is not None
    assert data.get("available") is not False
    assert data["five_hour"]["used"] == 6


def test_read_claude_never_prompts_when_oauth_dead_and_no_data(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dead credentials with no cache and no local data degrade quietly to
    ``None`` (the provider adapter renders ``no-local-data``) — the reader
    must never read stdin or spawn ``claude auth login``."""
    live_env = env | {"LLM_USAGE_NOW_EPOCH": "1000"}
    cred = Path(live_env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "expired-token", "refreshToken": "", "expiresAt": 0}}
        ),
        encoding="utf-8",
    )

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        raise HTTPError(req.full_url, 401, "unauthorized", hdrs=None, fp=None)

    def no_spawn(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("the usage reader must never spawn a subprocess")

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    monkeypatch.setattr(common.subprocess, "Popen", no_spawn)
    data = common.read_claude(live_env)
    assert data is None


def _make_http_error(url: str, code: int, headers: dict[str, str] | None = None) -> HTTPError:
    hdrs = None
    if headers is not None:
        from email.message import Message

        hdrs = Message()
        for key, value in headers.items():
            hdrs[key] = value
    return HTTPError(url, code, "err", hdrs, None)  # type: ignore[arg-type]


def test_claude_oauth_usage_retries_rate_limit(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # A 429 is transient and carries a Retry-After; honour it on the same read so
    # a brief rate-limit recovers to fresh data instead of degrading to the
    # ~/.claude/projects fallback (which reports "unavailable"). The Retry-After
    # is capped so a pathological value cannot hang the tool.
    retry_env = env | {
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY": "0",
    }
    calls = {"n": 0}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"rate_limits":{"five_hour":{"used_percentage":7,"resets_at":"2026-06-18T00:00:00Z"}}}'

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"})
        return FakeResponse()

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    text, unauthorized = common._fetch_claude_oauth_usage_text("tok", retry_env)
    assert calls["n"] == 2
    assert unauthorized is False
    assert text is not None and "five_hour" in text

    # Retry-After is read from the header, defaulted, and capped.
    assert common._retry_after_seconds(
        _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"}), retry_env, 0.5
    ) == 0.0
    assert common._retry_after_seconds(
        _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"}),
        env | {"LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY": "3"},
        0.5,
    ) == 3.0
    assert common._retry_after_seconds(
        _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, None),
        env | {"LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY": "5"},
        0.5,
    ) == 0.5


def test_claude_oauth_usage_retries_5xx_and_network(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # A 5xx / network blip is transient and must be retried within the bounded
    # budget instead of degrading on the first failure (previously any HTTPError
    # returned immediately).
    retry_env = env | {"LLM_USAGE_LIVE_FETCH_RETRIES": "2", "LLM_USAGE_LIVE_FETCH_RETRY_DELAY": "0"}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"rate_limits":{"five_hour":{"used_percentage":3,"resets_at":"2026-06-18T00:00:00Z"}}}'

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    seq = [lambda: (_ for _ in ()).throw(_make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 500)),
           lambda: (_ for _ in ()).throw(OSError("temporary")),
           lambda: FakeResponse()]
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        idx = calls["n"]
        calls["n"] += 1
        return seq[idx]()

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    text, unauthorized = common._fetch_claude_oauth_usage_text("tok", retry_env)
    assert calls["n"] == 3
    assert unauthorized is False
    assert text is not None and "five_hour" in text


def test_claude_oauth_usage_rate_limit_single_shot_when_retries_disabled(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With retries globally disabled (the suite's hermetic default) a 429 must not
    # retry or sleep -- the path stays single-shot.
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"})

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    text, unauthorized = common._fetch_claude_oauth_usage_text("tok", env)
    assert calls["n"] == 1
    assert text is None
    assert unauthorized is False


def test_claude_oauth_rate_limit_serves_bounded_api_cache(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_env = env | {
        "LLM_USAGE_NOW_EPOCH": "1000",
        "LLM_USAGE_CLAUDE_RATE_LIMIT_CACHE_MAX_AGE": "300",
    }
    cred = Path(live_env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8")
    cache = common.usage_cache_dir(live_env) / "claude-usage-api.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        '{"rate_limits":{"five_hour":{"used_percentage":20,"resets_at":"2026-06-18T00:00:00Z"}}}',
        encoding="utf-8",
    )
    os.utime(cache, (880, 880))
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"})

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude_api(live_env)
    assert data is not None
    assert data["five_hour"]["used"] == 20
    assert calls["n"] == 1

    data = common.read_claude_api(live_env)
    assert data is not None
    assert data["five_hour"]["used"] == 20
    assert calls["n"] == 1


def test_claude_oauth_rate_limit_does_not_fall_back_to_stale_project_data(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_env = env | {
        "LLM_USAGE_NOW_EPOCH": "1000",
        "LLM_USAGE_LIVE_FETCH_RETRIES": "2",
        "LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY": "0",
        "LLM_USAGE_STALE_RECOVERY_DELAY": "0",
    }
    cred = Path(live_env["HOME"]) / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8")
    project = Path(live_env["HOME"]) / ".claude" / "projects" / "r.jsonl"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        '{"message":{"rateLimits":{"fiveHour":{"usedPercent":6,"resetsAt":"2026-06-18T00:00:00Z"}}}}\n',
        encoding="utf-8",
    )
    os.utime(project, (800, 800))
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise _make_http_error(common.CLAUDE_OAUTH_USAGE_URL, 429, {"Retry-After": "8"})

    monkeypatch.setattr(common, "urlopen", fake_urlopen)
    data = common.read_claude(live_env)
    assert data == {
        "provider": "claude",
        "source": common.CLAUDE_OAUTH_USAGE_URL,
        "available": False,
        "reason": "rate-limited",
    }
    assert calls["n"] == 2


def test_usage_dashboard_ready_guidance_and_reset(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    cfg = usage.Config()
    cfg.color_enabled = False

    assert usage.render_ready(10, cfg) == "yes"
    assert usage.render_ready(0, cfg) == "no"
    assert usage.classify_budget_guidance("weekly", 60, 1000 + int(3.5 * 86400)).text == "↑ headroom"
    assert usage.classify_budget_guidance("weekly", 50, 1000 + int(3.5 * 86400)).text == "= on pace"
    assert usage.classify_budget_guidance("weekly", 40, 1000 + int(3.5 * 86400)).text == "↓ conserve"

    assert usage.format_reset(1000 + 36 * 60, cfg) == "36m"
    assert usage.format_reset(1000 + 4 * 3600 + 34 * 60, cfg) == "4h 34m"
    assert usage.format_reset(1000 + 5 * 86400 + 2 * 3600, cfg) == "5d 2h"

    rows = [
        usage.UsageRow("Codex", "5h", 70, "70%", 1000 + 9000, "fixture"),
        usage.UsageRow("Codex", "weekly", 40, "40%", 1000 + int(3.5 * 86400), "fixture"),
        usage.UsageRow("Claude", "5h", 0, "0%", 1000 + 1800, "fixture"),
        usage.UsageRow("Claude", "weekly", 91, "91%", 1000 + int(5 * 86400), "fixture"),
    ]
    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    out = capsys.readouterr().out
    assert "Ready" in out
    assert "Guidance" in out
    assert "yes" in out
    assert "no" in out
    assert "↑ headroom" in out
    assert "↓ conserve" in out
    assert "× empty" in out
    assert "open" not in out
    assert "closed" not in out
    assert "Pace / Gate" not in out
    assert "╞" not in out
    assert "◆" not in out
    assert "Use" not in out.splitlines()[0]


@pytest.mark.parametrize("window", ["weekly", "monthly"])
def test_usage_budget_guidance_compares_remaining_to_time_left(monkeypatch: pytest.MonkeyPatch, window: str) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    duration = usage.window_seconds(window)
    assert duration is not None
    reset = 1000 + int(duration / 2)

    assert usage.classify_budget_guidance(window, 56, reset).text == "↑ headroom"
    assert usage.classify_budget_guidance(window, 54, reset).text == "= on pace"
    assert usage.classify_budget_guidance(window, 44, reset).text == "↓ conserve"
    assert usage.classify_budget_guidance(window, 50, None).text == "· no rate data"


def test_usage_session_guidance_forecasts_runout(env: dict[str, str]) -> None:
    env = env | {
        "LLM_USAGE_NOW_EPOCH": "1600",
        "LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "9999",
        "XDG_CACHE_HOME": str(Path(env["HOME"]) / ".cache"),
    }
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    log = cache / "llm-usage.log"
    log.write_text("", encoding="utf-8")

    assert usage.classify_session_guidance("Codex", "5h", 0, 3600, env).text == "× empty"
    assert usage.classify_session_guidance("Codex", "5h", 20, 3600, env).text == "· no rate data"
    # A missing measurement (unavailable provider) is "no rate data", not an
    # exhausted window. "× empty" must stay reserved for a genuinely spent quota.
    assert usage.classify_session_guidance("MiniMax", "5h", None, None, env).text == "· no rate data"

    log.write_text(
        '{"ts":1000,"provider":"Codex","window":"5h","remaining":20}\n'
        '{"ts":1600,"provider":"Codex","window":"5h","remaining":10}\n',
        encoding="utf-8",
    )
    assert usage.classify_session_guidance("Codex", "5h", 10, 3600, env).text == "! empty in 10m"

    log.write_text(
        '{"ts":1000,"provider":"Codex","window":"5h","remaining":90}\n'
        '{"ts":1600,"provider":"Codex","window":"5h","remaining":80}\n',
        encoding="utf-8",
    )
    assert usage.classify_session_guidance("Codex", "5h", 80, 2600, env).text == "✓ lasts until reset"


def test_usage_table_snapshot_has_guidance_and_no_old_dial(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = usage.Config()
    cfg.color_enabled = False
    rows = [
        usage.UsageRow("Codex", "5h", 84, "84%", 1000 + 4 * 3600, "fixture"),
        usage.UsageRow("Codex", "weekly", 33, "33%", 1000 + 5 * 86400 + 3600, "fixture"),
        usage.UsageRow("Claude", "5h", 0, "0%", 1000 + 120, "fixture"),
        usage.UsageRow("Claude", "weekly", 91, "91%", 1000 + 4 * 86400 + 23 * 3600, "fixture"),
        usage.UsageRow("Copilot", "monthly", 36, "36%", 1000 + 17 * 86400 + 10 * 3600, "fixture"),
    ]

    usage.print_dashboard_header(cfg)
    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    out = capsys.readouterr().out

    assert out.startswith("LLM Usage · ")
    assert "\n\nBars: quota rows █ available · ░ spent" in out
    assert "$ rows █ spent · ░ budget left" in out
    assert "Guidance:" in out
    assert "Provider   Ready   Scope     Remaining" in out
    assert "Codex      yes     5h         84% ████████░░" in out
    assert "                   weekly     33% ███░░░░░░░   ↓ conserve" in out
    assert "Claude     no      5h          0% ░░░░░░░░░░   × empty" in out
    assert "╞" not in out
    assert "◆" not in out
    assert "──╞═══╡──◆" not in out


def test_usage_unicode_column_alignment_is_stable(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = usage.Config()
    cfg.color_enabled = False
    rows = [
        usage.UsageRow("Codex", "5h", 84, "84%", 1000 + 4 * 3600, "fixture"),
        usage.UsageRow("Codex", "weekly", 33, "33%", 1000 + 5 * 86400, "fixture"),
    ]

    usage.print_table_header(cfg)
    usage.print_usage_rows(cfg, rows)
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    width = usage.table_fixed_width(cfg)
    assert all(usage.visible_len(line) == width for line in lines)
    assert width <= 120


def test_scheduler_argument_branches(env: dict[str, str], tmp_path: Path) -> None:
    cases = [
        ["./llm-scheduler", "--provider"],
        ["./llm-scheduler", "--prompt"],
        ["./llm-scheduler", "--prompt-file"],
        ["./llm-scheduler", "--cwd"],
        ["./llm-scheduler", "--tmux"],
        ["./llm-scheduler", "--command-template"],
        ["./llm-scheduler", "--headless-idle-timeout"],
        ["./llm-scheduler", "--run-dir"],
        ["./llm-scheduler", "--unknown"],
    ]
    for args in cases:
        assert run_cmd(args, env).returncode == 2
    bad_at = run_cmd(["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--at", "not-a-date", "--log-dir", str(tmp_path / "logs")], env)
    assert bad_at.returncode == 2
    assert not (tmp_path / "logs").exists()
    bad_env = run_cmd(["./llm-scheduler", "--provider", "codex", "--prompt", "x"], env | {"LLM_SCHEDULER_IDLE_TIMEOUT": "bad"})
    assert bad_env.returncode == 2
    wake = run_cmd(["./llm-scheduler", "--wake-test"], env)
    assert wake.returncode == 0
    assert json.loads(wake.stdout)["note"].startswith("wake is best effort")
    guarded = run_cmd(
        ["./llm-scheduler", "--provider", "claude", "--prompt", "x", "--suspend-until-ready"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1"},
    )
    assert guarded.returncode == common.AUTONOMY_ABORT_STATUS
    assert "disabled inside an active ralph-robin" in guarded.stderr
    allowed = run_cmd(
        ["./llm-scheduler", "--provider", "claude", "--prompt", "x", "--suspend-until-ready", "--dry-run", "--command-template", "true"],
        env | {"LLM_TOOLS_RALPH_ROBIN_ACTIVE": "1", "LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND": "1", "LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert allowed.returncode == 0


def test_scheduler_unavailable_suspend_and_no_stream(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    unavailable = '{"available":false,"reason":"missing-cli"}'
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "claude",
            "--prompt",
            "x",
            "--command-template",
            "provider-mock",
            "--max-unavailable-wait",
            "1",
            "--poll-interval",
            "1",
            "--no-retry",
            "--log-dir",
            str(tmp_path / "unavail"),
        ],
        env | {"LLM_SCHEDULER_USAGE_JSON": unavailable},
    )
    assert result.returncode == 0
    assert "chat ok" in result.stdout
    quiet = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "provider-mock", "--log-dir", str(tmp_path / "quiet")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}', "LLM_SCHEDULER_NO_STREAM": "1"},
    )
    assert quiet.returncode == 0
    assert "chat ok" not in quiet.stdout


def test_scheduler_suspend_dry_run_and_failures(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    write_exe(fake_bin / "systemd-run", "#!/usr/bin/env python3\nprint('Running timer as unit: mocked.timer')\n")
    write_exe(fake_bin / "systemctl", "#!/usr/bin/env python3\nimport sys\nprint('running' if sys.argv[1:3] == ['--user','is-system-running'] else '')\n")
    exhausted = '{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}}'
    dry = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "dry")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": exhausted},
    )
    assert dry.returncode == 0
    assert "would schedule" in dry.stdout
    near = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "true", "--suspend-until-ready", "--dry-run", "--log-dir", str(tmp_path / "near")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":0,"resets_at":9999999999},"week":{"remaining":50}}', "LLM_USAGE_NOW_EPOCH": "9999999970"},
    )
    assert "suspend scheduling failed" in near.stderr


def test_scheduler_tmux_missing_and_template_error(env: dict[str, str], tmp_path: Path) -> None:
    result = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--command-template", "unterminated '", "--log-dir", str(tmp_path / "bad-template")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert result.returncode == 1
    tmux = run_cmd(
        ["./llm-scheduler", "--provider", "codex", "--prompt", "x", "--tmux", ":", "--no-retry", "--log-dir", str(tmp_path / "tmux")],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}'},
    )
    assert tmux.returncode == 1


def test_ralph_and_scheduler_highlight_helpers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert scheduler.provider_env(scheduler.SchedulerConfig()) is None
    env = scheduler.provider_env(scheduler.SchedulerConfig(provider="codex", ralph_robin_active=True, ralph_robin_providers="claude,codex"))
    assert env is not None
    assert env["LLM_TOOLS_RALPH_ROBIN_ACTIVE"] == "1"
    assert env["LLM_TOOLS_RALPH_ROBIN_SELECTED_PROVIDER"] == "codex"
    assert env["LLM_TOOLS_RALPH_ROBIN_PROVIDERS"] == "claude,codex"

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_TOOLS_COLOR_DIFF_ADD", raising=False)
    monkeypatch.delenv("LLM_TOOLS_SYMBOL_COMMAND", raising=False)
    monkeypatch.delenv("LLM_TOOLS_NO_SYMBOLS", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert common.ANSI_COLOR_ROLES["info"] == "39"
    assert common.ANSI_COLOR_ROLES["heading"] == "1;39"
    assert common.ANSI_COLOR_ROLES["ok"].endswith(";77")
    assert common.ANSI_COLOR_ROLES["error"].endswith(";81")
    assert not {
        "1;38;5;203",
        "38;5;203",
        "1;38;5;219",
        "1;38;5;222",
        "1;38;5;183",
    } & set(common.ANSI_COLOR_ROLES.values())
    assert scheduler.stream_color_enabled(Tty()) is True
    assert f"\x1b[{common.color_code('diff_add')}m+added\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"+added\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('diff_remove')}m-removed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"-removed\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('diff_hunk')}m@@ hunk\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"@@ hunk\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('error')}merror failed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"error failed\n", stream_name="stdout", enabled=True)
    assert f"\x1b[{common.color_code('warn')}mwarning: check this\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"warning: check this\n", stream_name="stdout", enabled=True)
    assert scheduler.highlight_provider_text(b"progress\n", stream_name="stderr", enabled=True) == b"progress\n"
    assert f"\x1b[{common.color_code('error')}merror failed\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"error failed\n", stream_name="stderr", enabled=True)
    assert scheduler.highlight_provider_text(b"\x1b[31mred\x1b[0m\n", stream_name="stdout", enabled=True) == b"\x1b[31mred\x1b[0m\n"
    monkeypatch.setenv("LLM_TOOLS_COLOR_DIFF_ADD", "1;34")
    assert b"\x1b[1;34m+added\x1b[0m\n" == scheduler.highlight_provider_text(b"+added\n", stream_name="stdout", enabled=True)
    monkeypatch.setenv("LLM_TOOLS_SYMBOL_COMMAND", "$")
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    monkeypatch.setenv("LLM_TOOLS_NO_SYMBOLS", "1")
    assert f"\x1b[{common.color_code('command')}mgit status\x1b[0m\n".encode() == scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=True)
    assert scheduler.highlight_provider_text(b"git status\n", stream_name="stdout", enabled=False) == b"git status\n"

    decision = {"provider": "claude", "usable": False, "reason": "rate-limited", "wait_until": 2000, "windows": [{"name": "5h", "remaining": 0}]}
    assert "rate-limited" in ralph_robin.decision_summary(decision)
    ralph_robin.print_usage_summary({"decisions": [decision, {"provider": "codex", "usable": True, "reason": "usable", "windows": [{"name": "5h", "remaining": 61.5}]}]})
    assert "claude" in capsys.readouterr().err
    monkeypatch.setattr(ralph_robin, "color_enabled", lambda: True)
    ralph_robin.status_line("plain body", level="error")
    body = capsys.readouterr().err.split(": ", 1)[1]
    assert body == "plain body\n"


def test_ralph_validation_dry_run_rotation_and_autonomy(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    assert run_cmd(["./ralph-robin"], env).returncode == 2
    assert run_cmd(["./ralph-robin", "--providers", "bad", "--prompt", "x"], env).returncode == 2
    usage_json = '{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}'
    dry = run_cmd(
        ["./ralph-robin", "--providers", "claude,codex", "--prompt", "x", "--command-template", "provider-mock {provider}", "--dry-run", "--state-file", str(tmp_path / "s.json"), "--log-dir", str(tmp_path / "logs")],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert dry.returncode == 0
    assert "dry-run" in dry.stderr
    run = run_cmd(
        ["./ralph-robin", "--providers", "claude,codex", "--prompt", "x", "--command-template", "provider-mock {provider}", "--state-file", str(tmp_path / "s2.json"), "--log-dir", str(tmp_path / "logs2"), "--no-retry", "--max-iterations", "1"],
        env | {"LLM_USAGE_NOW_EPOCH": "1780430000", "LLM_SCHEDULER_USAGE_JSON": usage_json},
    )
    assert run.returncode == 0
    assert json.loads((tmp_path / "s2.json").read_text())["current_provider"] == "codex"
    blocked = run_cmd(
        ["./ralph-robin", "--providers", "claude,codex", "--prompt", "x", "--command-template", "provider-mock {provider}", "--state-file", str(tmp_path / "s3.json"), "--log-dir", str(tmp_path / "logs3"), "--no-retry", "--max-duration", "3"],
        env | {"LLM_SCHEDULER_USAGE_JSON": '{"claude":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":50},"week":{"remaining":50}}}', "PROVIDER_MODE": "blocking"},
    )
    assert blocked.returncode == common.AUTONOMY_ABORT_STATUS


def test_ralph_injects_selected_provider_context(env: dict[str, str], fake_provider: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture.txt"
    usage_json = '{"claude":{"available":true,"five_hour":{"remaining":0,"resets_at":1780441200},"week":{"remaining":50}},"codex":{"available":true,"five_hour":{"remaining":60},"week":{"remaining":68}}}'
    prompt = "When continuation is required, run exactly: llm-scheduler --provider claude --prompt-file task.md --suspend-until-ready"
    result = run_cmd(
        [
            "./ralph-robin",
            "--prompt",
            prompt,
            "--command-template",
            "provider-mock {provider} {prompt}",
            "--state-file",
            str(tmp_path / "state.json"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--no-retry",
            "--max-iterations",
            "1",
        ],
        env
        | {
            "LLM_USAGE_NOW_EPOCH": "1780430000",
            "LLM_SCHEDULER_USAGE_JSON": usage_json,
            "PROVIDER_CAPTURE": str(capture),
        },
    )
    assert result.returncode == 0
    captured = capture.read_text(encoding="utf-8")
    assert "codex RALPH ROBIN RUNTIME CONTEXT" in captured
    assert "Current selected provider: codex" in captured
    assert "claude: rate-limited" in captured
    assert "codex: usable" in captured
    assert "Do not run provider-specific llm-scheduler --suspend-until-ready commands" in captured
    assert prompt in captured


def test_ensure_copilot_footer_settings(env: dict[str, str], tmp_path: Path) -> None:
    home = tmp_path / "copilot-home"
    cenv = env | {"COPILOT_HOME": str(home)}
    settings = home / "settings.json"
    # Fresh install: file is created with the footer items we scrape enabled.
    common.ensure_copilot_footer_settings(cenv)
    assert json.loads(settings.read_text())["footer"] == {"showQuota": True, "showAiUsed": True}
    # Idempotent: an already-enabled file is left byte-for-byte untouched.
    before = settings.stat().st_mtime_ns
    common.ensure_copilot_footer_settings(cenv)
    assert settings.stat().st_mtime_ns == before
    # Existing user settings are preserved while the required flags are flipped on.
    settings.write_text(json.dumps({"footer": {"showQuota": False, "showSandbox": True}, "beep": True}))
    common.ensure_copilot_footer_settings(cenv)
    data = json.loads(settings.read_text())
    assert data["footer"] == {"showQuota": True, "showSandbox": True, "showAiUsed": True}
    assert data["beep"] is True
    # Opt-out env disables the write entirely.
    home2 = tmp_path / "copilot-home-2"
    common.ensure_copilot_footer_settings(cenv | {"COPILOT_HOME": str(home2), "LLM_USAGE_COPILOT_NO_SETTINGS_WRITE": "1"})
    assert not (home2 / "settings.json").exists()
    # Unparseable settings are left untouched rather than clobbered.
    home3 = tmp_path / "copilot-home-3"
    home3.mkdir()
    (home3 / "settings.json").write_text("{not json")
    common.ensure_copilot_footer_settings(cenv | {"COPILOT_HOME": str(home3)})
    assert (home3 / "settings.json").read_text() == "{not json"


def test_freshen_stale_windows() -> None:
    now = 1000
    # Reset already passed: window rolled over -> full quota, reset cleared.
    assert common.freshen_window({"used": 90.0, "resets_at": 500, "window_minutes": 300}, now) == {
        "used": 0.0,
        "resets_at": None,
        "window_minutes": 300,
    }
    # Future reset is left untouched (same object returned).
    future = {"used": 90.0, "resets_at": 2000, "window_minutes": 300}
    assert common.freshen_window(future, now) is future
    # No reset and non-dict inputs pass through unchanged.
    no_reset = {"used": 90.0, "resets_at": None}
    assert common.freshen_window(no_reset, now) is no_reset
    assert common.freshen_window(None, now) is None
    # Provider-level walk freshens top-level and per-row windows using NOW_EPOCH.
    obj = {
        "five_hour": {"used": 50.0, "resets_at": 500},
        "week": {"used": 10.0, "resets_at": 5000},
        "rows": [{"five_hour": {"used": 70.0, "resets_at": 400}, "week": {"used": 5.0, "resets_at": 6000}}],
    }
    out = common.freshen_provider_windows(obj, {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert out["five_hour"] == {"used": 0.0, "resets_at": None}
    assert out["week"]["used"] == 10.0
    assert out["rows"][0]["five_hour"]["used"] == 0.0
    assert out["rows"][0]["week"]["used"] == 5.0


def test_read_codex_freshens_elapsed_window(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    home = Path(env["HOME"])
    (home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    # 5h reset 900 is in the past relative to NOW_EPOCH 1000; weekly 9999 is future.
    (home / ".codex" / "sessions" / "r.jsonl").write_text(
        '{"rate_limits":{"primary":{"used_percent":98,"resets_at":900},"secondary":{"used_percent":65,"resets_at":9999}}}\n',
        encoding="utf-8",
    )
    codex = common.read_codex(env | {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert codex is not None
    assert codex["five_hour"] == {"used": 0.0, "resets_at": None, "window_minutes": 300}
    assert codex["week"]["used"] == 65  # unexpired weekly is preserved


def test_common_extra_branches(env: dict[str, str], fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert common.fmt_duration("bad") == "-"
    assert common.time_until("bad") == "-"
    assert common.parse_copilot_ai_credits("AI Credits: 17") == 17
    assert common.parse_copilot_monthly_used("Monthly: 42% used") == 42
    assert common.parse_copilot_monthly_used("Plan: 62% used · Session: 0 AIC used") == 62
    assert common.json_for_copilot(None)["reason"] == "unavailable"
    assert common.json_for_copilot({"provider": "copilot", "monthly": {"remaining": 1}, "ai_credits": {"used": 2}}, False).get("ai_credits") is None
    assert common.output_is_retryable(0, "chapter 429") is False
    assert common.output_is_retryable(0, "claude: rate-limited, codex: usable") is False
    assert common.output_is_retryable(0, "rate limit reached") is True
    assert common.output_is_retryable(0, "HTTP 429 Too Many Requests") is True
    assert common.output_is_retryable(42, "") is True
    # A model describing the SYSTEM UNDER TEST is not a provider rate limit: these
    # bare words must NOT trip a retry (the bug that re-ran/killed the loop).
    assert common.output_is_retryable(0, "the c64u device was overloaded and dropped out") is False
    assert common.output_is_retryable(0, "the REST endpoint was temporarily unavailable; try again later") is False
    assert common.output_is_retryable(0, "no rate-limit issues were observed this loop") is False
    # Genuine provider/transport signatures still retry.
    assert common.output_is_retryable(0, 'API Error: {"type":"overloaded_error"}') is True
    assert common.output_is_retryable(0, "HTTP 503 Service Unavailable") is True
    # trust_clean_exit (ralph-robin owns rate-limit handling) trusts exit 0 even
    # when the transcript pastes a device log that looks like a rate limit.
    assert common.output_is_retryable(0, "device log: HTTP 429 Too Many Requests", trust_clean_exit=True) is False
    assert common.output_is_retryable(1, "boom", trust_clean_exit=True) is True
    assert common.argv_to_command_line(["a b", "$x"]) == "'a b' '$x'"
    assert common.template_argv("cmd {provider} {prompt_file} {cwd}", provider="codex", prompt="p", prompt_file=tmp_path / "p.txt", cwd="/tmp") == ["cmd", "codex", str(tmp_path / "p.txt"), "/tmp"]
    assert common.read_copilot_live(env | {"LLM_USAGE_DISABLE_COPILOT": "1"})["reason"] == "disabled"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "auth required"})["reason"] == "not-authenticated"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "Monthly: 5% used AI Credits: 9"})["monthly"]["remaining"] == 95
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    assert common.parse_epoch("bad-date") is None
    monkeypatch.setenv("HOME", env["HOME"])
    cache = common.usage_cache_dir()
    cache.mkdir(parents=True)
    (cache / "claude-usage-api.json").write_text('{"rate_limits":{"five_hour":{"used_percentage":20}}}', encoding="utf-8")
    assert common.read_claude_api()["five_hour"]["used"] == 20


def test_parser_option_coverage(tmp_path: Path) -> None:
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    scfg = scheduler.parse_args([
        "--provider", "claude", "--prompt-file", str(prompt), "--at", "@100",
        "--window", "5h", "--min-remaining", "2", "--poll-interval", "3",
        "--max-unavailable-wait", "4", "--retry-delays", "5", "--cwd", str(tmp_path),
        "--fresh", "--headless", "--tmux", "s:w", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "logs"), "--run-dir", str(tmp_path / "run"),
        "--wake", "--suspend-until-ready",
    ])
    assert scfg.provider == "claude"
    assert scfg.tmux_target == "s:w"
    assert scfg.suspend_until_ready is True
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="codex", cwd="/c", attached=True), "p") == ["codex", "-C", "/c", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude", attached=True), "p") == ["claude", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude"), "p") == ["claude", "--print", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="claude", claude_stream_json=True), "p") == ["claude", "--print", "--output-format", "stream-json", "--verbose", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="copilot", cwd="/c", attached=True), "p") == ["copilot", "-C", "/c", "-i", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="kilo", cwd="/c", attached=True), "p") == ["kilo", "run", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="kilo", cwd="/c"), "p") == ["kilo", "run", "--dir", "/c", "p"]
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="opencode", cwd="/c", attached=True), "p") == ["opencode"]
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="codex")).startswith("Codex")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="claude")).startswith("Claude")
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="copilot")).startswith("GitHub")

    rcfg = ralph_robin.parse_args([
        "--providers", " claude, codex ,,", "--prompt-file", str(prompt), "--window", "weekly",
        "--min-remaining", "2", "--poll-interval", "3", "--max-unavailable-wait", "4",
        "--retry-delays", "5", "--cwd", str(tmp_path), "--fresh", "--headless",
        "--tmux", "s:w", "--command-template", "true", "--auto-confirm", "--no-auto-confirm",
        "--headless-idle-timeout", "7", "--headless-question-timeout", "8",
        "--log-dir", str(tmp_path / "rlogs"), "--state-file", str(tmp_path / "state.json"),
        "--wake", "--suspend-until-ready",
    ])
    ralph_robin.validate_args(rcfg)
    assert rcfg.providers == ["claude", "codex"]
    assert ralph_robin.safe_args_json(rcfg)["providers"] == ["claude", "codex"]
    with pytest.raises(SystemExit):
        ralph_robin.parse_providers(" , ")


def test_scheduler_system_and_tmux_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "t", "codex")
    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setattr(common, "have_cmd", lambda name: name in {"systemd-run", "systemctl", "rtcwake", "tmux"})

    class P:
        def __init__(self, code: int = 0, out: str = "ok") -> None:
            self.returncode = code
            self.stdout = out

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return P(0, "")
        return P(0, "ok")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(common.subprocess, "run", fake_run)
    monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate-limited") is True
    assert "scheduled: logs written to" in capsys.readouterr().out
    assert any(c and c[0] == "systemd-run" for c in calls)

    cfg.wake = True
    scheduler.log_wake_plan(cfg, logs, 3000)
    assert cfg.wake_armed_target == 3000
    scheduler.log_wake_plan(cfg, logs, 3000)

    cfg.exec_mode = "tmux"
    cfg.tmux_target = "sess:win"
    status = tmp_path / "status"

    def fake_tmux(args, **kwargs):
        if args[:2] == ["tmux", "has-session"]:
            return P(0, "")
        if args[:2] == ["tmux", "list-windows"]:
            return P(0, "other\n")
        if args[:2] == ["tmux", "capture-pane"]:
            status.write_text("0", encoding="utf-8")
            return P(0, "pane")
        return P(0, "")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    out = tmp_path / "tmux.out"
    assert scheduler.run_tmux(cfg, logs, ["true"], out, status) == 0
    assert "pane" in out.read_text()


def test_common_process_helpers_and_estimators(env: dict[str, str], tmp_path: Path) -> None:
    status, text = common.run_pty_capture(
        [sys.executable, "-c", "print('Confirm folder trust'); input(); print('trusted')"],
        tmp_path,
        5,
        stream=False,
        auto_confirm=True,
        idle_timeout=0,
        question_idle_timeout=0,
    )
    assert status == 0
    assert "trusted" in text
    status2, text2 = common.run_pty_capture(
        [sys.executable, "-c", "print('What do you want to do?'); print('Enter to confirm - Esc to cancel'); import time; time.sleep(5)"],
        tmp_path,
        5,
        stream=False,
        auto_confirm=True,
        idle_timeout=0,
        question_idle_timeout=0,
    )
    assert status2 == common.AUTONOMY_ABORT_STATUS
    assert "autonomous abort" in text2

    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "llm-usage.log").write_text(
        '{"ts":1000,"provider":"p","window":"w","remaining":100}\n'
        '{"ts":1060,"provider":"p","window":"w","remaining":90}\n'
        '{"ts":1120,"provider":"p","window":"w","remaining":80}\n',
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("p", "w", 40, env | {"LLM_USAGE_NOW_EPOCH": "1120", "LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "9999"}) == "4m"
    assert common.estimate_remaining_time_from_log("p", "w", 0, env) == "-"


def test_estimate_remaining_time_survives_resets_and_gaps(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    # A genuine, SUSTAINED reset: remaining declines gradually, jumps back to 100,
    # then stays high before declining again. (Realistic -- a real window reset
    # restores full quota and you do not burn 10% in one sample interval right
    # after it. A lone 90->100->90 blip is noise and is despiked, see the
    # dedicated despike test.)
    rows = [
        (base, 100),
        (base + 1800, 95),
        (base + 3600, 90),       # old window burn (dropped once we anchor to the reset)
        (base + 7200, 100),      # RESET
        (base + 8000, 98),       # stays high -> confirms a real reset, not a spike
        (base + 10800, 90),      # post-reset burn: 100 -> 90 over 3600s
        (base + 18000, 80),      # a further 10% after a 2h gap
    ]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"p","window":"w","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(base + 18000)}
    # The jump at +7200 (90->100) anchors to the current window, dropping the
    # earlier burn. The trailing 2h gap exceeds max_gap, so only the post-reset
    # 10% over 3600s counts -> 50% lasts 5h.
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env) == "5h"
    stale_env = env | {"LLM_USAGE_NOW_EPOCH": str(base + 18601)}
    assert common.estimate_remaining_time_from_log("p", "w", 50, stale_env) == "-"
    assert common.estimate_remaining_time_from_log("p", "w", 50, stale_env | {"LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "0"}) == "5h"
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_LOOKBACK_SECONDS": "60"}) == "-"
    # Disabling the gap filter also counts the post-reset trailing 2h decrease:
    # 20% over 10800s within the anchored window -> 50% lasts 7h 30m.
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_MAX_GAP_SECONDS": "0"}) == "7h 30m"
    assert common.estimate_remaining_time_from_log("p", "w", 50, now_env | {"LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS": "bad"}) == "5h"


def test_estimate_remaining_time_despikes_transient_outliers(env: dict[str, str]) -> None:
    """A lone bad reading (a momentary stale/alternate value) must not blow away
    the burn-rate history. Its recovery looks like a window reset, which would
    anchor to the last ~minute and report a spurious 'no rate data'."""
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    # Smooth 5h decline 100 -> 84 over ~16 min, with two isolated 72 dips that
    # immediately recover to ~85 -- exactly the real-log pattern.
    rows: list[tuple[int, float]] = []
    rem = 100.0
    for i in range(32):
        ts = base + i * 30
        if i in (20, 26):
            rows.append((ts, 72.0))  # transient outlier
        else:
            rows.append((ts, rem))
            rem -= 0.5
    log = cache / "llm-usage.log"
    log.write_text(
        "".join(f'{{"ts":{ts},"provider":"Claude","window":"5h","remaining":{r}}}\n' for ts, r in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(rows[-1][0])}
    # Despiked -> the full ~16 min of decline drives a real forecast, not "-".
    assert common.estimate_remaining_time_from_log("Claude", "5h", rows[-1][1], now_env) not in ("-", "1m")


def test_estimate_remaining_time_requires_minimum_span_for_real_windows(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    # A lone coarse step (a stale 80% reading jumping to the steady 49%) followed
    # by a few flat seconds. Read literally this looks like 31% burned in 33s, which
    # the old estimator extrapolated to "weekly gone in ~5m". With only ~3.5min of
    # history there is not enough evidence to estimate a weekly/5h ETA.
    rows = [(base, 80)] + [(base + 33 + i * 30, 49) for i in range(7)]
    log = cache / "llm-usage.log"
    now = rows[-1][0]
    for window in ("weekly", "5h", "monthly"):
        log.write_text(
            "".join(f'{{"ts":{ts},"provider":"Codex","window":"{window}","remaining":{rem}}}\n' for ts, rem in rows),
            encoding="utf-8",
        )
        now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
        assert common.estimate_remaining_time_from_log("Codex", window, 49, now_env) == "-"

    # Once the same flat reading has been observed across enough wall-clock history,
    # the lone step is diluted and a (large, sane) estimate appears instead of "-".
    long_rows = [(base, 80)] + [(base + 33 + i * 3600, 49) for i in range(8)]
    long_now = long_rows[-1][0]
    log.write_text(
        "".join(f'{{"ts":{ts},"provider":"Codex","window":"weekly","remaining":{rem}}}\n' for ts, rem in long_rows),
        encoding="utf-8",
    )
    est = common.estimate_remaining_time_from_log("Codex", "weekly", 49, env | {"LLM_USAGE_NOW_EPOCH": str(long_now)})
    assert est not in ("-", "1m")

    # The gate is tunable: dropping the fraction to 0 restores the raw estimate.
    short_env = env | {"LLM_USAGE_NOW_EPOCH": str(now), "LLM_USAGE_REMAINING_TIME_MIN_SPAN_FRACTION": "0"}
    log.write_text(
        "".join(f'{{"ts":{ts},"provider":"Codex","window":"weekly","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("Codex", "weekly", 49, short_env) != "-"


def test_estimate_anchors_to_current_window(env: dict[str, str]) -> None:
    # The reported bug: burn from PRIOR 5h windows leaked into the current
    # window's velocity, pinning "empty in Xm" to a wildly short, stuck value.
    # Anchoring to the most recent reset must confine the estimate to the burn
    # actually observed in the current window.
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    rows = [(base, 100), (base + 600, 20)]  # prior window: heavy burn, then exhausted
    # Reset back to ~full, then a gentle ~1pp/min burn (99 -> 82 over 17 min).
    rows += [(base + 4000 + i * 60, 99 - i) for i in range(18)]
    now = rows[-1][0]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"Claude","window":"5h","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
    secs = common.estimate_remaining_seconds_from_log("Claude", "5h", 82, now_env)
    # Current window only: 17pp over 1020s -> 82pp lasts ~1h22m. Counting the
    # leaked prior burn would collapse this to ~22m.
    assert secs is not None and 3600 < secs < 18000


def test_estimate_ignores_impossible_drain_spikes(env: dict[str, str]) -> None:
    # A single reading that "drains" most of the window in seconds is physically
    # impossible (a transient bad sample). It must not dominate the velocity even
    # when it is inside the current window and never recovers above threshold.
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    base = 1_000_000
    rows = [(base, 99), (base + 30, 20)]  # 79pp "drained" in 30s -> impossible
    rows += [(base + 30 + i * 300, 20 - i) for i in range(1, 8)]  # gentle 20 -> 13
    now = rows[-1][0]
    (cache / "llm-usage.log").write_text(
        "".join(f'{{"ts":{ts},"provider":"Claude","window":"5h","remaining":{rem}}}\n' for ts, rem in rows),
        encoding="utf-8",
    )
    now_env = env | {"LLM_USAGE_NOW_EPOCH": str(now)}
    secs = common.estimate_remaining_seconds_from_log("Claude", "5h", 13, now_env)
    # Only the gentle 7pp/2100s burn should count (~1h+); the spike would force ~5m.
    assert secs is not None and secs > 3600
    # Lowering the guard below the spike's drain time lets it back in -> tiny ETA.
    leaky = now_env | {"LLM_USAGE_REMAINING_TIME_MIN_DRAIN_SECONDS": "1"}
    leaked = common.estimate_remaining_seconds_from_log("Claude", "5h", 13, leaky)
    assert leaked is not None and leaked < secs


def test_prune_usage_log(env: dict[str, str]) -> None:
    cache = common.usage_cache_dir(env)
    cache.mkdir(parents=True, exist_ok=True)
    log = cache / "llm-usage.log"
    common.prune_usage_log(env)
    lines = [f'{{"ts":{i},"provider":"p","window":"w","remaining":50}}' for i in range(100)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "0"})
    assert len(log.read_text(encoding="utf-8").splitlines()) == 100
    common.prune_usage_log(env)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 100
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "10", "LLM_USAGE_LOG_TAIL_LINES": "5"})
    kept = log.read_text(encoding="utf-8").splitlines()
    assert len(kept) == 5
    assert kept[-1] == lines[-1]
    common.prune_usage_log(env | {"LLM_USAGE_LOG_MAX_BYTES": "bad"})
    assert len(log.read_text(encoding="utf-8").splitlines()) == 5


def test_common_filesystem_provider_paths(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", env["HOME"])
    # This path exercises the local-filesystem fallback readers, so keep Codex
    # from spawning the real app-server (it reads os.environ, not the fixture).
    monkeypatch.setenv("LLM_USAGE_DISABLE_CODEX_APP_SERVER", "1")
    home = Path(env["HOME"])
    assert common.latest_matching_line(tmp_path / "missing", lambda _o: True, env) is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{bad\n{}\n", encoding="utf-8")
    assert common.latest_matching_line(tmp_path, lambda o: o == {}, env) == "{}"
    assert common.window_from("x", 1) is None
    assert common.normalize_codex_obj({}, "s") is None
    assert common.normalize_claude_obj("x", "s") is None
    (home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "sessions" / "r.jsonl").write_text('{"rateLimits":{"primary":{"usedPercent":5}}}\n', encoding="utf-8")
    assert common.read_codex()["five_hour"]["used"] == 5
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "projects" / "r.jsonl").write_text('{"message":{"rateLimits":{"fiveHour":{"usedPercent":6}}}}\n', encoding="utf-8")
    assert common.read_claude()["five_hour"]["used"] == 6
    ccache = common.usage_cache_dir(env) / "copilot-usage.json"
    ccache.parent.mkdir(parents=True, exist_ok=True)
    ccache.write_text('{"provider":"copilot","monthly":{"remaining":1}}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999"})["monthly"]["remaining"] == 1


def test_copilot_refresh_module(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "copilot-usage.json"
    lock = tmp_path / "copilot-refresh.lock"
    lock.mkdir()
    monkeypatch.setenv("LLM_USAGE_COPILOT_CAPTURE_TEXT", "Monthly: 10% used AI Credits: 3")
    assert copilot_refresh.main([str(cache)]) == 0
    data = json.loads(cache.read_text())
    assert data["monthly"]["remaining"] == 90
    assert not lock.exists()
    assert copilot_refresh.main([]) == 2


def test_copilot_refresh_wait_budget_cold_start_is_long(env: dict[str, str]) -> None:
    # Stale-or-missing cache both wait long enough for the capture to land so we
    # never serve a value past its TTL (the warm-cache "serve stale quickly" path
    # is gone -- it was the source of partially-stale usage). The budget covers
    # the PTY capture timeout *plus* the GitHub premium_request fallback that runs
    # after it, so the cold run does not give up before the refresh writes real
    # data (capture timeout 10 + billing fallback 8 = 18).
    assert common.copilot_refresh_wait_budget(env, cache_present=True) == 18.0
    assert common.copilot_refresh_wait_budget(env, cache_present=False) == 18.0
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_TIMEOUT": "4"}, cache_present=False) == 12.0
    # The billing-fallback headroom is tunable on its own.
    assert (
        common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_BILLING_FALLBACK_BUDGET": "3"}, cache_present=False)
        == 13.0
    )
    # Explicit override always wins, including the 0 used by other tests.
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_REFRESH_WAIT": "0"}, cache_present=False) == 0.0
    assert common.copilot_refresh_wait_budget(env | {"LLM_USAGE_COPILOT_REFRESH_WAIT": "bad"}, cache_present=True) == 18.0


def test_copilot_cold_start_returns_refreshed_data(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = env | {"XDG_CACHE_HOME": str(tmp_path / "cold-xdg"), "LLM_USAGE_COPILOT_CACHE_TTL": "999"}
    cache = common.usage_cache_dir(fresh) / "copilot-usage.json"
    lock = common.usage_cache_dir(fresh) / "copilot-refresh.lock"

    class PopenStub:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            # Stand in for the background refresh: land real data and release the lock.
            cache.write_text('{"provider":"copilot","monthly":{"remaining":77}}', encoding="utf-8")
            try:
                lock.rmdir()
            except OSError:
                pass

    monkeypatch.setattr(common.subprocess, "Popen", PopenStub)
    # No cache exists yet, so without the cold-start wait this would be "refresh-pending".
    assert common.read_copilot(fresh)["monthly"]["remaining"] == 77


def test_validation_and_selection_edge_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        common.require_cmd("definitely-not-installed-llm-tools-test")
    for args in [
        ("", "", "", "1", ""),
        (str(tmp_path), "bad", "1", "1", ""),
        (str(tmp_path), "1", "bad", "1", ""),
        (str(tmp_path), "1", "0", "1", ""),
        (str(tmp_path), "1", "1", "bad", ""),
        (str(tmp_path), "1", "1", "1", "x,no"),
    ]:
        with pytest.raises(SystemExit):
            common.validate_gate_args(*args)
    with pytest.raises(SystemExit):
        common.validate_prompt_args("x", "y")
    with pytest.raises(SystemExit):
        common.validate_prompt_args("", "")
    with pytest.raises(SystemExit):
        common.validate_prompt_args("", str(tmp_path / "missing"))
    with pytest.raises(SystemExit):
        common.validate_provider_window("codex", "monthly")
    with pytest.raises(SystemExit):
        common.validate_provider_window("codex", "bad")

    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    cfg.state_file.write_text("{bad", encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.state_file.write_text('{"providers_spec":"other","current_index":9}', encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 0
    cfg.state_file.write_text('{"rotation_spec":"claude,codex","current_index":1}', encoding="utf-8")
    assert ralph_robin.current_index_from_state(cfg) == 1

    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 0, "codex": 0}
    cfg.state_file.write_text("{bad", encoding="utf-8")
    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 0, "codex": 0}
    cfg.state_file.write_text('{"rotation_spec":"other","completed_counts":{"claude":9}}', encoding="utf-8")
    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 0, "codex": 0}
    cfg.state_file.write_text('{"providers_spec":"claude,codex"}', encoding="utf-8")
    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 0, "codex": 0}
    cfg.state_file.write_text(
        '{"rotation_spec":"claude,codex","completed_counts":{"claude":"bad","codex":-4}}',
        encoding="utf-8",
    )
    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 0, "codex": 0}

    cfg.dry_run = False
    ralph_robin.save_state(cfg, 1, "codex", {"claude": 2, "codex": 3})
    assert ralph_robin.completed_counts_from_state(cfg) == {"claude": 2, "codex": 3}
    saved_state = json.loads(cfg.state_file.read_text() or "{}")
    assert saved_state["rotation_spec"] == "claude,codex"
    assert saved_state["completed_counts"] == {"claude": 2, "codex": 3}
    ralph_robin.save_state(cfg, 0, "claude")
    assert "completed_counts" not in json.loads(cfg.state_file.read_text() or "{}")

    route_cfg = ralph_robin.RalphConfig(
        routes_spec="",
        routes=["mini", "large"],
        state_file=tmp_path / "route-state.json",
        dry_run=False,
    )
    assert ralph_robin.rotation_state_spec(route_cfg) == "mini,large"
    ralph_robin.save_state(route_cfg, 1, "kilo", {"mini": 4, "large": 5})
    assert ralph_robin.completed_counts_from_state(route_cfg) == {"mini": 4, "large": 5}

    flaky_cfg = ralph_robin.RalphConfig(
        providers_spec="claude,codex",
        providers=["claude", "codex"],
        state_file=tmp_path / "flaky-state.json",
        dry_run=False,
    )
    real_chmod = Path.chmod

    def raise_for_flaky_state(path: Path, mode: int) -> None:
        if path in {flaky_cfg.state_file.parent, flaky_cfg.state_file}:
            raise OSError("chmod denied")
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", raise_for_flaky_state)
    ralph_robin.save_state(flaky_cfg, 0, "claude", {"claude": 1})
    assert ralph_robin.completed_counts_from_state(flaky_cfg) == {"claude": 1, "codex": 0}

    cfg.state_file.write_text('{"providers_spec":"claude,codex","current_index":9}', encoding="utf-8")
    cfg.dry_run = True
    ralph_robin.save_state(cfg, 1, "codex")
    assert json.loads(cfg.state_file.read_text() or "{}").get("current_index") == 9

    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: {"available": False, "reason": "missing-cli"})
    sel = ralph_robin.select_provider(cfg, logs, 0, {"claude", "codex"})
    assert sel["rotation_reason"] == "all-skipped"
    sel2 = ralph_robin.select_provider(cfg, logs, 0, set())
    assert sel2["rotation_reason"] == "advanced-to-undetermined"
    assert sel2["all_rate_limited"] is False

    snapshots = {
        "claude": {"available": True, "five_hour": {"remaining": 0, "resets_at": 2000}, "week": {"remaining": 50}},
        "codex": {"available": True},
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    sel3 = ralph_robin.select_provider(cfg, logs, 0, set())
    assert sel3["provider"] == "codex"
    assert sel3["rotation_reason"] == "advanced-to-undetermined"
    assert sel3["all_rate_limited"] is False


def test_ralph_even_burn_prefers_highest_remaining_daily_capacity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    # Even-burn ranks by pace deviation (remaining − expected). Codex resets in
    # 2 days with 50% left: expected = 2/7×100 ≈ 28.6%, delta ≈ +21.4% (headroom).
    # Claude resets in 5 days with 80% left: expected = 5/7×100 ≈ 71.4%, delta ≈ +8.6%.
    # Codex has more headroom → wins.
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 80},
            "week": {"remaining": 80, "resets_at": 1000 + (5 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 50},
            "week": {"remaining": 50, "resets_at": 1000 + (2 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    selected = ralph_robin.select_provider(cfg, logs, 0, set())
    assert selected["provider"] == "codex"
    assert selected["rotation_reason"] == "even-burn"

    cfg.even_burn = False
    old_rotation = ralph_robin.select_provider(cfg, logs, 0, set())
    assert old_rotation["provider"] == "claude"
    assert old_rotation["rotation_reason"] == "current-usable"


def test_ralph_even_burn_prefers_higher_remaining_when_resets_align(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # When weekly resets are (near) simultaneous, daily capacity reduces to
    # remaining, so the provider with more weekly headroom wins regardless of
    # which one is current. This mirrors the real Claude(81%)/Codex(47%) case.
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 81, "resets_at": 1000 + (6 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 85},
            "week": {"remaining": 47, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    # current_index points at codex; even-burn must still advance to claude.
    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "claude"
    assert selected["rotation_reason"] == "even-burn"


def test_ralph_even_burn_handles_unknown_weekly_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Claude reports weekly remaining but no reset time. Even-burn must fall back
    # to a full weekly window and still rank Claude rather than silently bailing
    # to the current provider (codex).
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 81},  # no resets_at -> reset_epoch is None
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 85},
            "week": {"remaining": 47, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    # Claude has no reset_epoch → fallback score = 81/7 ≈ 11.6.
    # Codex: 47% with 6 days left, expected = 6/7×100 ≈ 85.7%, delta ≈ −38.7 (conserve).
    # 11.6 > −38.7 → Claude wins.
    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "claude"
    assert selected["rotation_reason"] == "even-burn"


def test_ralph_even_burn_hands_over_when_incumbent_weekly_drains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Regression for "ralph-robin never hands over from codex": the incumbent's
    # 5h session window stays full and fast-resets, while its weekly plan is
    # nearly drained. Under the old max-across-scopes ranking both providers
    # tied on their healthy 5h pace and the incumbent kept winning the tie-break
    # forever, burning its weekly to the floor. With binding (weekly) ranking
    # the selector must advance to the peer that still has weekly headroom.
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    now = 1000
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 80, "resets_at": now + 5 * 86400},
        },
        "codex": {
            # 5h looks great (just reset), but weekly is almost gone.
            "available": True,
            "five_hour": {"remaining": 96},
            "week": {"remaining": 6, "resets_at": now + 5 * 86400},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    # current_index points at codex (the incumbent that just ran). Even-burn
    # must hand over to claude rather than keep draining codex's weekly.
    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "claude"
    assert selected["rotation_reason"] == "even-burn"

    # Codex is still perfectly usable (weekly 6% > 1% floor) — the hand-over is
    # driven by relative weekly surplus, not by codex being exhausted.
    codex_decision = next(d for d in selected["decisions"] if d["provider"] == "codex")
    assert codex_decision["usable"] is True


def _usable_selection(provider: str = "claude") -> dict:
    return {
        "index": 0,
        "provider": provider,
        "rotation_reason": "even-burn",
        "all_rate_limited": False,
        "decision": {"provider": provider, "usable": True, "wait_until": None},
        "decisions": [{"provider": provider, "usable": True, "wait_until": None}],
    }


def _ralph_main_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--prompt", "x",
        "--providers", "claude,codex",
        "--log-dir", str(tmp_path / "logs"),
        "--state-file", str(tmp_path / "state.json"),
        *extra,
    ]


def test_ralph_loops_until_max_iterations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    calls: list[str] = []
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: (calls.append(scfg.provider), 0)[1])

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "3", "--max-duration", "0", "--min-iteration-seconds", "0"))
    assert rc == 0
    assert len(calls) == 3  # looped instead of exiting after the first success


def test_ralph_aborts_on_instant_success_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A provider that returns success instantly (misconfig / no-op) must not let
    # the orchestrator spin forever; it aborts after a sustained fast streak.
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: 1000.0)  # no time ever elapses per iteration
    monkeypatch.setattr(ralph_robin, "sleep_seconds", lambda s: None)
    calls: list[str] = []
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: (calls.append(scfg.provider), 0)[1])

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "0", "--max-duration", "0", "--min-iteration-seconds", "5"))
    assert rc == common.AUTONOMY_ABORT_STATUS
    assert len(calls) == ralph_robin.FAST_SUCCESS_ABORT_STREAK


def test_ralph_loops_until_max_duration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: _usable_selection())
    clock = {"t": 0.0}
    monkeypatch.setattr(ralph_robin, "monotonic", lambda: clock["t"])
    calls: list[str] = []

    def fake_run(scfg: scheduler.SchedulerConfig) -> int:
        clock["t"] += 2000.0  # each increment burns ~33 minutes
        calls.append(scfg.provider)
        return 0

    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", fake_run)
    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "0", "--max-duration", "1h"))
    assert rc == 0
    assert len(calls) == 2  # 0s -> 2000s -> 4000s exceeds 3600s budget


def test_ralph_suspends_when_all_blocked_then_continues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "100")
    selections = [
        {  # everything blocked: soonest reset at epoch 1000
            "index": -1,
            "provider": "",
            "rotation_reason": "all-skipped",
            "decisions": [
                {"provider": "claude", "wait_until": 1000},
                {"provider": "codex", "wait_until": 2000},
            ],
        },
        _usable_selection(),  # provider free again after the wait
    ]
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: selections.pop(0))
    # One chunk covers the whole wait here; the stub advances the wall clock so
    # wait_until_epoch's poll loop terminates (in production time.time() does).
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "100000")
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(100 + int(sum(slept))))

    monkeypatch.setattr(ralph_robin, "sleep_seconds", fake_sleep)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: 0)

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "1", "--max-duration", "0"))
    assert rc == 0
    assert slept == [900]  # waited until the soonest reset (epoch 1000 - now 100) instead of exiting


def test_ralph_suspends_machine_until_earliest_renewal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # When every provider is rate-limited, Ralph suspends until the EARLIEST
    # window renewal across the rotation (epoch 1000 here), then resumes its own
    # loop and re-selects. Suspend infra is disabled so it uses the in-process
    # fallback we can observe.
    monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "100")
    monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
    selections = [
        {
            "index": 0,
            "provider": "claude",
            "rotation_reason": "all-unusable",
            "all_rate_limited": True,
            "decision": {"provider": "claude", "wait_until": 1000},
            "decisions": [
                {"provider": "claude", "wait_until": 2000},
                {"provider": "codex", "wait_until": 1000},
            ],
        },
        _usable_selection(),  # rotation recovers after the wake
    ]
    monkeypatch.setattr(ralph_robin, "select_provider", lambda cfg, logs, ci, sk: selections.pop(0))
    # One chunk covers the whole wait here; the stub advances the wall clock so
    # wait_until_epoch's poll loop terminates (in production time.time() does).
    monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "100000")
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(100 + int(sum(slept))))

    monkeypatch.setattr(ralph_robin, "sleep_seconds", fake_sleep)
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", lambda scfg: 0)

    rc = ralph_robin.main(_ralph_main_argv(tmp_path, "--max-iterations", "1", "--max-duration", "0", "--min-iteration-seconds", "0"))
    assert rc == 0
    assert slept == [900]  # epoch 1000 (earliest of 2000/1000) minus now 100


def test_ralph_even_burn_prefers_ready_provider_over_blocked_higher_weekly_headroom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = ralph_robin.RalphConfig(providers_spec="claude,codex", providers=["claude", "codex"], state_file=tmp_path / "state.json")
    logs = common.setup_run_logs(tmp_path / "logs", "r")
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    snapshots = {
        "claude": {
            "available": True,
            "five_hour": {"remaining": 0, "resets_at": 1100},
            "week": {"remaining": 81, "resets_at": 1000 + (6 * 86400)},
        },
        "codex": {
            "available": True,
            "five_hour": {"remaining": 100, "resets_at": 2000},
            "week": {"remaining": 50, "resets_at": 1000 + (6 * 86400)},
        },
    }
    monkeypatch.setattr(common, "usage_snapshot_for_provider", lambda provider, env=None: snapshots[provider])

    selected = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected["provider"] == "codex"
    assert selected["rotation_reason"] == "current-usable"
    assert selected["decision"]["reason"] == "usable"

    snapshots["claude"] = {
        "available": True,
        "five_hour": {"remaining": 100, "resets_at": 2000},
        "week": {"remaining": 0, "resets_at": 1000 + (6 * 86400)},
    }
    selected_weekly_exhausted = ralph_robin.select_provider(cfg, logs, 1, set())
    assert selected_weekly_exhausted["provider"] == "codex"
    assert selected_weekly_exhausted["rotation_reason"] == "current-usable"


def test_scheduler_more_system_edges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs = common.setup_run_logs(tmp_path / "logs", "s")
    cfg = scheduler.SchedulerConfig(provider="claude", prompt_text="p", cwd=str(tmp_path), log_dir=tmp_path / "logs", run_dir=logs.run_dir)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-run")
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    class P:
        def __init__(self, code: int = 0, out: str = "") -> None:
            self.returncode = code
            self.stdout = out

    monkeypatch.setattr(common, "have_cmd", lambda name: name in {"systemd-run", "systemctl"})
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: P(1, "fail"))
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    def inactive(args, **kwargs):
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return P(1, "")
        return P(0, "ok")

    monkeypatch.setattr(scheduler.subprocess, "run", inactive)
    assert scheduler.schedule_resume_and_suspend(cfg, logs, 2000, "rate") is False

    cfg.pre_suspend_confirmation_seconds = 0
    scheduler.print_pre_suspend_confirmation(cfg, logs, 2000, "unit", "why")
    assert "suspend-until-ready armed" in capsys.readouterr().out

    cfg2 = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), exec_mode="tmux", tmux_target="session")
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    out = tmp_path / "out"
    status = tmp_path / "status"
    assert scheduler.run_tmux(cfg2, logs, ["true"], out, status) == 127
    assert "tmux not installed" in out.read_text()


def test_error_fallback_branches(env: dict[str, str], fake_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PATH", env["PATH"])
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nprint('1234' if '-d' in sys.argv else '')\n")
    assert common.parse_epoch("next friday") == 1234
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nprint('not-an-int')\n")
    assert common.parse_epoch("next friday") is None
    write_exe(fake_bin / "date", "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    assert common.fmt_reset(None) == ""
    assert common.fmt_reset(0).startswith("1970-01-01")
    assert common.format_local_epoch(0).startswith("1970-01-01")
    assert common.now_epoch({"LLM_USAGE_NOW_EPOCH": "bad"}) > 0
    assert common.copilot_monthly_reset_epoch({"LLM_USAGE_NOW_EPOCH": "1798761600", "LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS": "bad"}) is not None
    assert common.num(True) is None
    assert common.num(None) is None
    assert common.num("nope") is None
    assert common.fmt_number("1.25") == "1.2"
    assert common.remaining_from_used(-5) == 100
    assert common.remaining_from_used(125) == 0
    assert common.window_from({"resets_at": 99}, 300) == {"used": None, "resets_at": 99, "window_minutes": 300}

    assert common.normalize_codex_obj({"msg": {"rateLimits": {"spark-model": {"primary": {"used_percent": 3}}}}}, "src")["rows"][0]["key"] == "codex-spark"
    assert common.normalize_claude_obj({"five_hour": {"utilization": 7}, "seven_day": {"used_percent": 8}}, "src")["week"]["used"] == 8
    assert common.json_for_provider(None, "codex") == {"provider": "codex", "available": False}
    assert common.decorate_window(None) is None
    assert common.json_for_copilot({"provider": "copilot", "monthly": None}, True)["available"] is False
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "trust_prompt_seen"})["reason"] == "trust-prompt"
    assert common.read_copilot_live(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "AI Credits: 4"})["ai_credits"]["used"] == 4

    cache_dir = common.usage_cache_dir(env)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "copilot-usage.json").write_text("{bad", encoding="utf-8")
    fake_proc: list[list[str]] = []
    original_popen = subprocess.Popen

    class PopenStub:
        def __init__(self, args, **kwargs) -> None:
            fake_proc.append(list(args))

    monkeypatch.setattr(common.subprocess, "Popen", PopenStub)
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","available":false,"reason":"format-changed"}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","available":false,"reason":"timeout"}', encoding="utf-8")
    assert common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})["reason"] == "refresh-pending"
    fresh_env = env | {"XDG_CACHE_HOME": str(tmp_path / "fresh-xdg"), "LLM_USAGE_COPILOT_CACHE_TTL": "999", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"}
    assert common.read_copilot(fresh_env)["reason"] == "refresh-pending"
    assert fake_proc
    (cache_dir / "copilot-refresh.lock").mkdir(exist_ok=True)
    os.utime(cache_dir / "copilot-refresh.lock", (1, 1))
    (cache_dir / "copilot-usage.json").write_text('{"provider":"copilot","monthly":{"remaining":2}}', encoding="utf-8")
    os.utime(cache_dir / "copilot-usage.json", (1, 1))
    # An ancient lock (dead refresh) plus a stale-but-real snapshot: the refresh
    # cannot land within a zero wait budget, so we serve the most recent monthly
    # figure ("usage on start") instead of dropping to unavailable. The
    # background refresh (stubbed here) keeps it current for the next run.
    served = common.read_copilot(env | {"LLM_USAGE_COPILOT_CACHE_TTL": "1", "LLM_USAGE_COPILOT_REFRESH_WAIT": "0"})
    assert served["monthly"]["remaining"] == 2
    monkeypatch.setattr(common.subprocess, "Popen", original_popen)

    log = cache_dir / "llm-usage.log"
    log.write_text(
        '{"ts":1000,"provider":"p","window":"w","remaining":80}\n'
        '{"ts":1060,"provider":"p","window":"w","remaining":90}\n'
        '{"ts":1120,"provider":"p","window":"w","remaining":89}\n'
        'not-json\n',
        encoding="utf-8",
    )
    assert common.estimate_remaining_time_from_log("p", "w", 1, env | {"LLM_USAGE_NOW_EPOCH": "1120", "LLM_USAGE_LOG_TAIL_LINES": "bad"}) == "1m"
    assert common.estimate_remaining_time_from_log("p", "missing", 1, env) == "-"
    assert common.estimate_remaining_time_from_log("p", "w", "bad", env) == "-"
    common.log_usage_sample("p", "w", "-", env)

    assert common.usage_decision_for_provider("copilot", "weekly", "1", "60", {}, env)["reason"] == "unsupported-scope"
    assert common.usage_decision_for_provider("codex", "monthly", "1", "60", {"available": True}, env)["reason"] == "unsupported-scope"
    assert common.usage_decision_for_provider("codex", "5h", "1", "60", {"available": True, "five_hour": {"resets_at": 2000}}, env)["reason"] == "inconclusive-usage"
    assert common.usage_snapshot_for_provider("unknown", env)["reason"] == "unsupported-provider"
    assert common.output_is_retryable(130, "", attached=True) is False
    assert common.output_is_retryable(1, "", attached=True) is True

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("same", encoding="utf-8")
    logs = common.setup_run_logs(tmp_path / "logs-same", "same", run_dir=tmp_path)
    assert common.load_prompt("", str(prompt), logs)[0] == "same"

    with pytest.raises(SystemExit):
        scheduler.parse_args(["--help"])
    monkeypatch.setenv("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS", "bad")
    with pytest.raises(SystemExit):
        scheduler.parse_args([])
    monkeypatch.delenv("LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS", raising=False)
    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path))
    monkeypatch.setenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "bad")
    with pytest.raises(SystemExit):
        scheduler.validate_args(cfg)
    monkeypatch.delenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", raising=False)
    assert scheduler.parse_date_d("not-a-date") is None
    assert scheduler.scheduler_model_description(scheduler.SchedulerConfig(provider="codex", command_template="true")) == "from command template"
    assert scheduler.highlight_provider_text(b"Tool call: shell\nTitle:\nplain\n", stream_name="stdout", enabled=True).count(b"\x1b[") >= 2
    assert scheduler.highlight_provider_text(b"plain\n", stream_name="stdout", enabled=False) == b"plain\n"

    cfg = scheduler.SchedulerConfig(provider="codex", prompt_text="p", cwd=str(tmp_path), attached=True)
    logs2 = common.setup_run_logs(tmp_path / "submit-logs", "s")
    monkeypatch.setattr(scheduler, "run_fresh_attached", lambda _cfg, _argv, _out, _status: 0)
    assert scheduler.submit_once(cfg, logs2, 1, ["true"]) == 1
    (logs2.run_dir / "attempt-2.status").write_text("bad", encoding="utf-8")
    (logs2.run_dir / "attempt-2.out").write_text("", encoding="utf-8")
    monkeypatch.setattr(scheduler, "run_fresh_attached", lambda _cfg, _argv, _out, _status: 0)
    assert scheduler.submit_once(cfg, logs2, 2, ["true"]) == 1

    ucfg = usage.Config()
    ucfg.color_enabled = True
    assert usage.colorize_percent("9%", ucfg).startswith("\x1b[0;31m")
    assert usage.colorize_percent("29%", ucfg).startswith("\x1b[0;33m")
    assert usage.colorize_percent("30%", ucfg).startswith("\x1b[0;32m")
    assert usage.colorize_percent("bad%", ucfg) == "bad%"
    plain = usage.Config()
    plain.color_enabled = False
    assert usage.progress_bar(100) == "█" * 10
    assert usage.progress_bar(0) == "░" * 10
    assert usage.progress_bar(35) == "█" * 4 + "░" * 6
    assert usage.render_remaining("100%", plain) == "100% ██████████"
    assert usage.render_remaining("35%", plain) == " 35% ████░░░░░░"
    assert usage.render_remaining("unavailable", plain) == "unavailable"
    assert usage.render_remaining("-", plain) == "-"
    assert usage.render_remaining("9%", ucfg).startswith("\x1b[0;31m")
    # Daily budget helper remains available to scheduler/Ralph paths.
    at1000 = {"LLM_USAGE_NOW_EPOCH": "1000"}
    assert common.daily_budget_percent(50, 1000 + 86400, at1000) == 50.0  # 1 day out -> 50%
    assert common.daily_budget_percent(20, 1000 + 7200, at1000) == 20.0  # 2h out -> all remaining
    assert round(common.daily_budget_percent(35, 1000 + (5 * 86400), at1000), 1) == 7.0
    assert common.daily_budget_percent(None, 2000, at1000) is None
    assert common.daily_budget_percent(50, None, at1000) is None
    assert common.daily_budget_percent(50, 900, at1000) is None  # reset already passed
    assert usage.render_daily_budget(None, plain) == ""  # calm: no forecast -> blank
    assert usage.render_daily_budget(60, plain, 50) == "↑ headroom"
    assert usage.render_daily_budget(50, plain, 50) == "= on pace"
    assert usage.render_daily_budget(40, plain, 50) == "↓ conserve"
    assert usage.render_daily_budget(50, ucfg, 50).startswith("\x1b[0;32m")
    assert usage.render_daily_budget(60, ucfg, 50).startswith("\x1b[0;36m")
    assert usage.render_daily_budget(40, ucfg, 50).startswith("\x1b[0;33m")
    assert usage.render_guidance_info(usage.GuidanceInfo("× empty", "empty"), ucfg).startswith("\x1b[0;31m")
    assert usage.render_gate(1, plain) == "yes"
    assert usage.render_gate(0, plain) == "no"
    assert usage.render_gate(1, ucfg) == "yes"
    assert usage.render_gate(0, ucfg).startswith("\x1b[1;31m")
    assert usage.render_pace_or_gate("5h", 94, plain) == ""  # calm: no reset -> blank
    assert usage.is_short_window("5h") is True
    assert usage.is_budget_window("weekly") is True
    usage.print_codex_rows(ucfg, {"source": "src", "five_hour": {"used": 10}, "week": {"used": 20}})
    usage.print_copilot_rows(ucfg, None)
    out = capsys.readouterr().out
    assert "Codex" in out and "Copilot" in out


def test_usage_snapshot_and_decision_no_delegation_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_snapshot(provider: str, env=None):
        calls["snapshot_provider"] = provider
        return {"provider": provider, "available": True}

    def fake_decision(provider, window, min_remaining, poll_interval, snapshot, env=None, *, model=None, allow_fallback=True):
        calls["decision_provider"] = provider
        calls["model"] = model
        calls["allow_fallback"] = allow_fallback
        return {"provider": provider, "usable": True, "reason": "usable"}

    monkeypatch.setattr(common, "usage_snapshot_for_provider", fake_snapshot)
    monkeypatch.setattr(common, "usage_decision_for_provider", fake_decision)

    snap, dec = common.usage_snapshot_and_decision("claude", None, "auto", "1", "60", model="sonnet", allow_fallback=False)
    assert calls["snapshot_provider"] == "claude"
    assert calls["decision_provider"] == "claude"
    # No delegation: the pinned model and allow_fallback flow straight through.
    assert calls["model"] == "sonnet"
    assert calls["allow_fallback"] is False
    assert dec["provider"] == "claude"
    assert "capacity_provider" not in dec


def test_usage_snapshot_and_decision_delegates_to_capacity_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_snapshot(provider: str, env=None):
        calls["snapshot_provider"] = provider
        return {"provider": provider, "available": True}

    def fake_decision(provider, window, min_remaining, poll_interval, snapshot, env=None, *, model=None, allow_fallback=True):
        calls["decision_provider"] = provider
        calls["model"] = model
        calls["allow_fallback"] = allow_fallback
        return {"provider": provider, "usable": True, "reason": "usable", "windows": [{"name": "5h"}]}

    monkeypatch.setattr(common, "usage_snapshot_for_provider", fake_snapshot)
    monkeypatch.setattr(common, "usage_decision_for_provider", fake_decision)

    snap, dec = common.usage_snapshot_and_decision("opencode", "minimax", "auto", "1", "60", model="ignored", allow_fallback=False)
    # Capacity is read from minimax...
    assert calls["snapshot_provider"] == "minimax"
    assert calls["decision_provider"] == "minimax"
    # ...gated purely on minimax's aggregate windows (requesting model ignored).
    assert calls["model"] is None
    assert calls["allow_fallback"] is True
    # ...but relabelled back to the provider whose CLI actually runs.
    assert dec["provider"] == "opencode"
    assert dec["capacity_provider"] == "minimax"
    assert dec["windows"] == [{"name": "5h"}]


# ---------------------------------------------------------------------------
# New footer parsers + GitHub premium_request/usage fallback for Copilot
# ---------------------------------------------------------------------------


def test_parse_copilot_remaining_pct_footer_1_0_57_plus() -> None:
    """The Copilot CLI >= 1.0.57 swapped the old ``Plan: N% used`` line for a
    ``Remaining reqs.: N%`` line (it's the *remaining* percentage now, not
    used). The parser has to recognise the new shape and convert it to a
    *used* percentage so the dashboard's quota bar still draws correctly.
    """
    assert common.parse_copilot_monthly_used("Remaining reqs.: 41%") == 59.0
    assert common.parse_copilot_monthly_used("Remaining requests: 80%") == 20.0
    # The legacy shapes must keep working so users on older CLIs do not
    # silently lose their monthly figure.
    assert common.parse_copilot_monthly_used("Plan: 62% used") == 62.0
    assert common.parse_copilot_monthly_used("Monthly: 42% used") == 42.0


def test_parse_copilot_ai_credits_new_footer() -> None:
    """The 1.0.57+ footer prints ``AI Credits N (Ys)`` without a colon; the
    1.0.40 line was ``AI Credits: N``. The parser has to accept both so a
    Copilot upgrade does not flip the row to ``format-changed``.
    """
    assert common.parse_copilot_ai_credits("AI Credits 7.41 (8s)") == 7.41
    assert common.parse_copilot_ai_credits("AI Credits 2.07 (3m)") == 2.07
    assert common.parse_copilot_ai_credits("AI Credits 2.07") == 2.07
    # The legacy colon-separated form must keep working.
    assert common.parse_copilot_ai_credits("AI Credits: 9") == 9.0


def test_copilot_live_falls_back_to_github_premium_request_usage(env: dict[str, str]) -> None:
    """The new Copilot CLI no longer prints the ``Plan: N% used`` line. When
    the GitHub ``premium_request/usage`` endpoint is reachable, the live
    reader must compute the monthly used percent from the per-model request
    counts rather than reporting ``format-changed``.
    """
    payload = {
        "usageItems": [
            {
                "product": "Copilot",
                "sku": "Copilot Premium Request",
                "model": "GPT-5.4",
                "unitType": "requests",
                "grossQuantity": 200,
            },
            {
                "product": "Copilot",
                "sku": "Copilot Premium Request",
                "model": "Claude Sonnet 4.6",
                "unitType": "requests",
                "grossQuantity": 50,
            },
        ]
    }
    # Capture text is the new footer (no Plan:, AI Credits with no colon).
    e = env | {
        "LLM_USAGE_COPILOT_CAPTURE_TEXT": "Changes    +0 -0\nAI Credits 2.07 (3s)\nTokens     ↑ 14.0k (6.7k cached) • ↓ 44 (35 reasoning)\n",
        "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(payload),
        "LLM_USAGE_COPILOT_PLAN": "pro",
        # The conftest disables the GitHub billing addon by default to keep
        # the suite hermetic; this test explicitly opts back in to inject a
        # payload via the new env var.
        "LLM_USAGE_DISABLE_COPILOT_ADDON": "0",
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
    }
    out = common.read_copilot_live(e)
    # ``read_copilot_live`` signals "we have data" by returning a dict
    # without an ``available: False`` key (the failure shape is the only
    # place that key appears).
    assert out.get("available") is not False
    # 200 + 50 = 250 of 300 (Pro allowance) = 83.333...% used.
    assert abs(out["monthly"]["used"] - (250 / 300 * 100.0)) < 1e-6
    assert abs(out["monthly"]["remaining"] - (50 / 300 * 100.0)) < 1e-6


def test_copilot_live_github_fallback_respects_plan_allowance(env: dict[str, str]) -> None:
    """Different Copilot plans ship with different monthly premium-request
    allowances. The math has to follow the resolved plan so a Pro+ user
    (1500 requests) does not get a quota bar that looks like they used
    16% of a 300-allowance plan.
    """
    payload = {
        "usageItems": [
            {
                "product": "Copilot",
                "sku": "Copilot Premium Request",
                "model": "GPT-5.4",
                "unitType": "requests",
                "grossQuantity": 240,
            }
        ]
    }
    e = env | {
        "LLM_USAGE_COPILOT_CAPTURE_TEXT": "AI Credits 1 (1s)\n",
        "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(payload),
        "LLM_USAGE_COPILOT_PLAN": "pro_plus",
        "LLM_USAGE_DISABLE_COPILOT_ADDON": "0",
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
    }
    out = common.read_copilot_live(e)
    # 240 of 1500 = 16%.
    assert abs(out["monthly"]["used"] - (240 / 1500 * 100.0)) < 1e-6
    # The source label flips to "github billing" so the user can tell
    # where the number came from.
    assert out["source"] == "github billing"


def test_copilot_live_prefers_legacy_footer_when_present(env: dict[str, str]) -> None:
    """The legacy ``Plan: 62% used`` line is the source of truth when it
    appears; the GitHub fallback is only consulted when the footer is
    missing the monthly figure. This test pins the precedence so a
    future refactor does not silently downgrade a working CLI footer to
    the (slower, rate-limited) REST path.
    """
    e = env | {
        "LLM_USAGE_COPILOT_CAPTURE_TEXT": "Plan: 30% used\nAI Credits 4 (2s)\n",
    }
    out = common.read_copilot_live(e)
    assert out["source"] == "copilot cli"
    assert out["monthly"]["used"] == 30.0


def test_copilot_live_no_token_falls_back_to_format_changed(env: dict[str, str]) -> None:
    """When the live footer has no monthly figure AND the GitHub billing
    API is unreachable, the dashboard must still report a clear reason
    (``format-changed``) so the user knows the live CLI footer is the
    reason -- the alternative (``inconclusive-usage``) would be a
    misleading "we don't know" when the truth is "we do know, the CLI
    changed".
    """
    # A footer that matches neither the legacy ``Plan: N% used`` nor the
    # new ``AI Credits N (Ys)`` shape — so no monthly figure is detected
    # and the GitHub addon is disabled by the conftest, mirroring a
    # machine with no ``gh`` CLI / GitHub token.
    e = env | {
        "LLM_USAGE_COPILOT_CAPTURE_TEXT": "Welcome to Copilot CLI 1.0.63\n",
    }
    out = common.read_copilot_live(e)
    # Failure shape: the live reader sets ``available: False`` only when
    # the snapshot represents a "we cannot measure" state.
    assert out.get("available") is False
    assert out["reason"] == "format-changed"


def test_copilot_billing_fallback_does_not_override_disabled(env: dict[str, str]) -> None:
    out = common.read_copilot_live(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT": "1",
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
                {"usageItems": [{"product": "Copilot", "grossQuantity": 30}]}
            ),
        }
    )
    assert out.get("available") is False
    assert out["reason"] == "disabled"


def test_copilot_billing_fallback_does_not_override_auth_prompt(env: dict[str, str]) -> None:
    out = common.read_copilot_live(
        env
        | {
            "LLM_USAGE_COPILOT_CAPTURE_TEXT": "auth required",
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
                {"usageItems": [{"product": "Copilot", "grossQuantity": 30}]}
            ),
        }
    )
    assert out.get("available") is False
    assert out["reason"] == "not-authenticated"


def test_copilot_billing_fallback_does_not_override_missing_cli(env: dict[str, str]) -> None:
    out = common.read_copilot_live(
        env
        | {
            "PATH": env["PATH"].split(":", 1)[0],
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
                {"usageItems": [{"product": "Copilot", "grossQuantity": 30}]}
            ),
        }
    )
    assert out.get("available") is False
    assert out["reason"] == "missing-cli"


def test_copilot_monthly_allowance_override(env: dict[str, str]) -> None:
    """Users with custom / enterprise contracts can pin their plan's
    monthly allowance explicitly so the quota bar uses the right
    denominator. Pinning a 5000-request allowance for a 100-request
    payload gives 2% used, regardless of the plan name.
    """
    e = env | {
        "LLM_USAGE_COPILOT_CAPTURE_TEXT": "AI Credits 1 (1s)\n",
        "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
            {"usageItems": [{"product": "Copilot", "grossQuantity": 100, "unitType": "requests"}]}
        ),
        "LLM_USAGE_COPILOT_MONTHLY_ALLOWANCE": "5000",
        "LLM_USAGE_DISABLE_COPILOT_ADDON": "0",
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
    }
    out = common.read_copilot_live(e)
    assert abs(out["monthly"]["used"] - 2.0) < 1e-6
    # The same override is reflected by the underlying helper so the
    # ``read_copilot_monthly_used`` JSON shape also carries the right
    # denominator (and the GUI / dashboard can show it on hover).
    direct = common.read_copilot_monthly_used(e)
    assert direct.get("allowance") == 5000
    assert abs(direct["used"] - 2.0) < 1e-6


def test_copilot_monthly_fallback_is_not_disabled_by_addon_flag(env: dict[str, str]) -> None:
    """Disabling add-on spend must not disable the monthly quota fallback.

    The two readers use different GitHub endpoints and different display rows.
    A user may want to hide the spend row while still relying on
    ``premium_request/usage`` for the monthly quota figure.
    """
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_ADDON": "1",
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
                {"usageItems": [{"product": "Copilot", "grossQuantity": 30}]}
            ),
            "LLM_USAGE_COPILOT_PLAN": "pro",
        }
    )
    assert out is not None
    assert out["requests"] == 30
    assert abs(out["used"] - 10.0) < 1e-6


def test_copilot_monthly_disable_flag_returns_none(env: dict[str, str]) -> None:
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "1",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON": json.dumps(
                {"usageItems": [{"product": "Copilot", "grossQuantity": 30}]}
            ),
        }
    )
    assert out is None


def test_copilot_monthly_cache_and_failure_paths(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    cache = common.usage_cache_dir(env) / "copilot-monthly.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"used": 12.5, "remaining": 87.5, "ts": 1}), encoding="utf-8")
    cached = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_MONTHLY_TTL": "100000",
        }
    )
    assert cached is not None and cached["used"] == 12.5

    # Invalid TTL falls back to the default, and a malformed cache is ignored.
    cache.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(common, "_github_token", lambda env=None: None)
    assert common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_MONTHLY_TTL": "bad",
        }
    ) is None

    # A token without a resolvable login degrades to the last usable cache.
    cache.write_text(json.dumps({"used": 7.0, "remaining": 93.0, "ts": 1}), encoding="utf-8")
    os.utime(cache, (1, 1))
    monkeypatch.setattr(common, "_github_token", lambda env=None: "token")
    monkeypatch.setattr(common, "_github_api_get", lambda path, token, env=None: {})
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_MONTHLY_TTL": "0",
        }
    )
    assert out == {"used": 7.0, "remaining": 93.0}


def test_copilot_premium_request_payload_malformed_paths() -> None:
    assert common._copilot_monthly_used_from_premium_request_usage("not-json") is None
    assert common._copilot_monthly_used_from_premium_request_usage([]) is None
    assert common._copilot_monthly_used_from_premium_request_usage({}) is None
    assert common._copilot_monthly_used_from_premium_request_usage({"usageItems": "bad"}) is None
    assert common._copilot_monthly_used_from_premium_request_usage(
        {"usageItems": ["bad", {"product": "actions", "grossQuantity": 10}, {"product": "Copilot", "grossQuantity": 0}]}
    ) == 0.0


def test_github_api_get_error_paths(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    class FakeResp:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            return None

    def http_403(_req, timeout=0):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError("https://example.invalid", 403, "forbidden", {}, None)

    monkeypatch.setattr(common, "urlopen", http_403)
    assert common._github_api_get("/x", "token", env) is None

    attempts = {"n": 0}

    def transient_then_ok(_req, timeout=0):  # type: ignore[no-untyped-def]
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("temporary")
        return FakeResp(b'{"ok": true}')

    monkeypatch.setattr(common, "urlopen", transient_then_ok)
    out = common._github_api_get(
        "/x",
        "token",
        env | {"LLM_USAGE_LIVE_FETCH_RETRIES": "1", "LLM_USAGE_LIVE_FETCH_RETRY_DELAY": "0"},
    )
    assert out == {"ok": True}


def test_github_token_gh_failure_returns_none(env: dict[str, str], fake_bin: Path) -> None:
    write_exe(fake_bin / "gh", "#!/usr/bin/env bash\nexit 2\n")
    base = {k: v for k, v in env.items() if k not in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")}
    assert common._github_token(base) is None


def test_copilot_ai_credits_survive_provider_snapshot(env: dict[str, str]) -> None:
    """The provider snapshot path powers ``llm-usage`` table/JSON output.

    It must preserve the optional AI credits figure, while the missing monthly
    quota remains an unknown monthly scope that does not make Copilot ready.
    """
    from llm_tools.providers import copilot as copilot_provider

    snap = copilot_provider.read(env | {"LLM_USAGE_COPILOT_CAPTURE_TEXT": "AI Credits 4.5 (2s)\n"})
    monthly = next(scope for scope in snap.scopes if scope.name == "monthly")
    assert monthly.kind == "unknown"
    legacy = usage._legacy_copilot(snap, show_credits=True)
    assert legacy["ai_credits"]["used"] == 4.5
    cfg = usage.Config()
    cfg.show_copilot_credits = True
    rows = usage.copilot_rows(cfg, legacy)
    assert any(row.scope == "ai-credits" and row.left_text == "4.5" for row in rows)
    assert usage.provider_ready(rows, "Copilot") is False


# ---------------------------------------------------------------------------
# MiniMax error-envelope detection
# ---------------------------------------------------------------------------


def test_minimax_error_envelope_maps_to_clear_reason(env: dict[str, str], tmp_path: Path, fake_bin: Path) -> None:
    """The mmx CLI returns ``{"error": {"code": 1, "message": "API error:
    no active token plan subscription (HTTP 200)"}}`` when the user has no
    active plan. The reader must classify that into a stable, descriptive
    reason so the dashboard can tell the user "your account has no
    subscription" instead of the generic ``inconclusive-usage``.
    """
    # A fake mmx binary that prints the error envelope, exactly the way
    # the real CLI does.
    write_exe(
        fake_bin / "mmx",
        "#!/usr/bin/env python3\nimport json, sys\nsys.stdout.write(json.dumps({\"error\": {\"code\": 1, \"message\": \"API error: no active token plan subscription (HTTP 200)\"}}))\n",
    )
    from llm_tools.providers import minimax
    snap = minimax.read_minimax(env)
    assert snap.available is False
    assert snap.reason == "subscription-required"


def test_minimax_error_envelope_classifier_branches() -> None:
    """A small lookup table drives the message -> reason mapping; the
    branches below cover each classification so a future message-string
    change does not silently fall through to the wrong bucket.
    """
    from llm_tools.providers.minimax import _classify_minimax_error as cls
    assert cls("no active token plan subscription") == "subscription-required"
    assert cls("no active plan attached") == "subscription-required"
    assert cls("authentication failed: bad token") == "not-authenticated"
    assert cls("please login first") == "not-authenticated"
    assert cls("rate limit exceeded; try again in 30s") == "rate-limited"
    assert cls("HTTP 429 throttled") == "rate-limited"
    assert cls("connection refused") == "network-error"
    assert cls("upstream service unavailable") == "quota-error"


# ---------------------------------------------------------------------------
# Day-by-day fallback for the Copilot monthly figure
# ---------------------------------------------------------------------------


def test_copilot_monthly_sums_per_day_for_current_month(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The GitHub ``premium_request/usage`` endpoint silently returns an
    empty ``usageItems`` for the *current* month when queried with a bare
    ``?year=...&month=...`` — even when individual days report plenty of
    usage. The reader has to ask for the month one day at a time
    otherwise the dashboard draws a full-green "headroom" bar on a day
    the user has actually exhausted their allowance. This test pins
    that the day-by-day fallback is the path the reader takes for the
    current month and that the per-day totals are summed.
    """
    from llm_tools import common

    captured: list[str] = []

    def fake_api_get(path: str, token: str, env=None):
        captured.append(path)
        # Day 1-2: empty; day 3: 200 requests; day 4: 150; rest empty.
        if "day=3" in path:
            return {
                "usageItems": [
                    {"product": "Copilot", "grossQuantity": 200, "unitType": "requests"},
                ]
            }
        if "day=4" in path:
            return {
                "usageItems": [
                    {"product": "Copilot", "grossQuantity": 150, "unitType": "requests"},
                ]
            }
        return {"usageItems": []}

    monkeypatch.setattr(common, "_github_token", lambda env=None: "test-token")
    monkeypatch.setattr(common, "_github_api_get", fake_api_get)
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_ADDON": "0",
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PLAN": "pro",
            "LLM_USAGE_COPILOT_ADDON_LOGIN": "alice",
            # Force a deterministic "current month" by fixing the
            # epoch to 2026-06-16 12:00:00 UTC.
            "LLM_USAGE_NOW_EPOCH": "1781630400",
        }
    )
    # At least one day-level probe was issued...
    assert any("day=" in path for path in captured), f"expected day-level probes, got {captured!r}"
    # ...and no bare month probe.
    assert not any("day=" not in path and "month=" in path for path in captured), (
        f"expected NO bare month probe, got {captured!r}"
    )
    # 200 + 150 = 350 of 300 (Pro allowance) -> capped at 100% used.
    assert out is not None
    assert abs(out["used"] - 100.0) < 1e-6
    assert out["remaining"] == 0.0


def test_copilot_monthly_api_failure_does_not_cache_zero_usage(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitHub billing API failure is not the same as "0 requests used"."""
    from llm_tools import common

    captured: list[str] = []

    def fake_api_get(path: str, token: str, env=None):
        captured.append(path)
        return None

    monkeypatch.setattr(common, "_github_token", lambda env=None: "test-token")
    monkeypatch.setattr(common, "_github_api_get", fake_api_get)
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PLAN": "pro",
            "LLM_USAGE_COPILOT_ADDON_LOGIN": "alice",
            "LLM_USAGE_NOW_EPOCH": "1781630400",
        }
    )
    assert out is None
    assert any("day=" in path for path in captured)


def test_copilot_monthly_uses_month_probe_for_past_month(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Past (complete) months can be queried with a single
    ``?year=...&month=...`` request because the API quirk that hides
    the current month's data does not apply. The reader detects "past
    month" via the test seam (``LLM_USAGE_COPILOT_PREMIUM_REQUEST_MONTH_OVERRIDE``)
    and issues exactly one month-level request rather than 30 day-level
    ones.
    """
    from llm_tools import common

    captured: list[str] = []

    def fake_api_get(path: str, token: str, env=None):
        captured.append(path)
        return {
            "usageItems": [
                {"product": "Copilot", "grossQuantity": 90, "unitType": "requests"},
            ]
        }

    monkeypatch.setattr(common, "_github_token", lambda env=None: "test-token")
    monkeypatch.setattr(common, "_github_api_get", fake_api_get)
    out = common.read_copilot_monthly_used(
        env
        | {
            "LLM_USAGE_DISABLE_COPILOT_ADDON": "0",
            "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
            "LLM_USAGE_COPILOT_PLAN": "pro",
            "LLM_USAGE_COPILOT_ADDON_LOGIN": "alice",
            "LLM_USAGE_COPILOT_PREMIUM_REQUEST_MONTH_OVERRIDE": "2026-05",
        }
    )
    # Exactly one probe — the month-level one — and no day-by-day
    # for-each loop. The override is the test seam that pins "May
    # 2026" so the reader takes the cheap path.
    assert len(captured) == 1
    assert "month=5" in captured[0]
    assert "day=" not in captured[0]
    assert out is not None
    assert abs(out["used"] - (90 / 300 * 100)) < 1e-6
