"""Remote CLI client: configuration, request mapping and failure reporting without a coordinator."""

from __future__ import annotations

import io
import json
import os
import threading
import stat
import urllib.error

import pytest

from llm_tools import bridge_remote as remote
from llm_tools.bridge import _build_parser, main

TOKEN = "a" * 40


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def opener(routes):
    """Answer requests from ``routes[(method, path)]`` = (status, body) or an exception."""
    calls = []

    def open_(request, context=None, timeout=None):
        path = request.full_url.split("8443", 1)[1]
        key = (request.get_method(), path.split("?")[0])
        calls.append((key, json.loads(request.data) if request.data else None, dict(request.header_items()), request.full_url))
        answer = routes[key]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode() if body is not None else b"{"))
        return Response(status, body)

    open_.calls = calls
    return open_


def client(routes):
    return remote.Client(remote.RemoteConfig("https://c:8443", TOKEN), opener=opener(routes))


def args(*argv):
    return _build_parser().parse_args(list(argv))


def test_login_status_logout(state, tmp_path, capsys):
    with pytest.raises(ValueError, match="https://"):
        remote.login("http://c", TOKEN, ca_pem=None)
    with pytest.raises(ValueError, match="API token"):
        remote.login("https://c", "x", ca_pem=None)
    with pytest.raises(ValueError, match="PEM"):
        remote.login("https://c", TOKEN, ca_pem="x")
    assert main(["remote", "status"]) == 2 and "no coordinator" in capsys.readouterr().err
    (tmp_path / "token").write_text(TOKEN)
    (tmp_path / "ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    assert main(["remote", "login", "--coordinator", "https://c:8443/", "--token-file", str(tmp_path / "token"), "--ca-file", str(tmp_path / "ca.pem")]) == 0
    if os.name != "nt":  # Windows has no permission bits; the profile ACL protects the file (DECISIONS.md D8)
        assert stat.S_IMODE((state / "remote" / remote.REMOTE_FILE).stat().st_mode) == 0o600
    capsys.readouterr()
    assert main(["remote", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"coordinator_url": "https://c:8443"} and TOKEN not in out
    (state / "remote" / remote.REMOTE_FILE).write_text("[")
    with pytest.raises(remote.RemoteError, match="unreadable"):
        remote.load()
    assert main(["remote", "logout"]) == 0 and "removed" in capsys.readouterr().out
    assert main(["remote", "logout"]) == 0 and "No coordinator" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "operation", "params"),
    [
        (["mission", "create", "--prompt", "new", "--cwd", ".", "--providers", "codex"], "mission.create",
         {"prompt": "new", "cwd": ".", "name": "", "acceptance": [], "providers": "codex", "routes": None,
          "headless": False, "native_tui": False, "teams": None, "token_spend": None, "trust_folder": False, "unsupported_options": []}),
        (["mission", "settings", "--mission", "m1"], "mission.settings.get", {}),
        (["mission", "set", "--mission", "m1", "--auto-continue", "on", "--teams", "off"], "mission.settings.patch",
         {"auto_continue": True, "teams": False, "token_spend": None, "suspend_when_rate_limited": None, "colour": None}),
        (["mission", "colour", "--mission", "m1", "4"], "mission.settings.patch",
         {"auto_continue": None, "teams": None, "token_spend": None, "suspend_when_rate_limited": None, "colour": "4"}),
        (["rotation", "--mission", "m1"], "mission.rotation.get", {"set": None, "routes": None, "providers": None}),
        (["rotation", "--mission", "m1", "--set", "codex,claude"], "mission.rotation.put", {"set": "codex,claude", "routes": None, "providers": None}),
        (["agents", "--mission", "m1"], "mission.agents", {"mission": "m1"}),
        (["proposal", "create", "--mission", "m1", "--operation", "op1", "--prompt", "new"], "proposal.create",
         {"source_operation_id": "op1", "prompt": "new", "title": "", "priority": 0, "depends_on": [], "acceptance": []}),
        (["doctor"], "worker.doctor", {"mission": None}),
        (["finish", "--mission", "m1"], "mission.finish", {}),
        (["steer", "--mission", "m1", "--kind", "replace", "--task", "2", "--prompt", "new"], "mission.steer",
         {"kind": "replace", "task": 2, "task_id": None, "route": None, "prompt": "new"}),
        (["worker", "audit"], "worker.audit", {"limit": 50}),
        (["gc"], "worker.gc", {}),
        (["status", "--mission", "m1"], "mission.status", {}),
        (["events", "--mission", "m1", "--after", "3"], "mission.events", {"after": 3, "limit": 100}),
        (["stop", "--mission", "m1"], "mission.stop", {"force": False}),
        (["stop", "--mission", "m1", "--force"], "mission.stop", {"force": True}),
        (["start", "--mission", "m1", "--headless", "--providers", "codex"], "mission.start", {"headless": True, "native_tui": None, "providers": "codex", "routes": None}),
        (["suspend-until-credits", "--mission", "m1"], "mission.suspend_until_credits", {}),
        (["restore", "--input", "/tmp/backup.db"], "worker.restore", {"input": "/tmp/backup.db"}),
        (["resume", "--mission", "m1"], "mission.resume", {}),
        (["task", "list", "--mission", "m1"], "task.list", {"limit": 200, "offset": 0}),
        (["task", "list", "--mission", "m1", "--all"], "task.list", {"limit": 200, "offset": 0, "all": True}),
        (["task", "show", "--mission", "m1", "--task-id", "4"], "task.show", {"task_id": 4}),
        (["task", "verify", "--mission", "m1", "--task", "2", "--approve"], "task.verify", {"task_number": 2, "evidence": "", "approved": True}),
        (["task", "reject", "--mission", "m1", "--task", "2", "--reason", "no"], "task.reject", {"task_number": 2, "reason": "no"}),
        (["task", "reorder", "--mission", "m1", "--task", "2", "--value", "5"], "task.reorder", {"task_number": 2, "order": 5}),
        (["task", "retry", "--mission", "m1", "--task", "2"], "task.retry", {"task_number": 2}),
        (["task", "add", "--mission", "m1", "--prompt", "x"], "task.add", {"prompt": "x", "title": "", "priority": 0, "order": None, "acceptance": []}),
        (["task", "add", "--mission", "m1", "--prompt", "x", "--depends-on", "1,3"], "task.add",
         {"prompt": "x", "title": "", "priority": 0, "order": None, "depends_on_numbers": [1, 3], "acceptance": []}),
        (["task", "dependencies", "--mission", "m1", "--task", "4", "--add", "2"], "task.dependency",
         {"task_number": 4, "depends_on_number": 2}),
        (["proposal", "list", "--mission", "m1"], "proposal.list", {"status": "PENDING", "limit": 100, "after": 0}),
        (["proposal", "accept", "--mission", "m1", "--task", "3"], "proposal.accept", {"task_number": 3, "reason": ""}),
        (["proposal", "reject", "--mission", "m1", "--task", "3", "--reason", "no"], "proposal.reject", {"task_number": 3, "reason": "no"}),
    ],
)
def test_request_mapping(argv, operation, params):
    request, _ = remote.request_for(args(*argv), "ws")
    assert (request.operation, dict(request.params), request.target) == (operation, params, "ws")


def test_create_maps_trust_folder_and_names_unsupported_local_options():
    request, _ = remote.request_for(args("mission", "create", "--prompt", "new", "--cwd", ".", "--trust-folder", "--include-config", "--verifier-arg", "true"), "ws")
    assert request.params["trust_folder"] is True
    assert request.params["unsupported_options"] == ["--include-config", "--verifier-arg"]


def test_create_suspend_and_restore_use_resource_endpoints():
    routes = {
        ("POST", "/api/v1/missions"): (200, {"state": "done", "result": {"mission": {}, "task": {}}}),
        ("POST", "/api/v1/missions/m1/suspend-until-credits"): (200, {"state": "done", "result": {"ok": True}}),
        ("POST", "/api/v1/workers/ws/restore"): (200, {"state": "done", "result": {
            "missions": 1, "schema_version": 10, "last_event_id": 42, "event_sequence": 43,
        }}),
    }
    api = client(routes)
    create, _ = remote.request_for(args("mission", "create", "--prompt", "new", "--cwd", "."), "ws")
    suspend, _ = remote.request_for(args("suspend-until-credits", "--mission", "m1"), "ws")
    restore, restore_human = remote.request_for(args("restore", "--input", "/root/backup.db"), "ws")
    assert api.submit_resource("ws", create)["mission"] == {}
    assert api.submit_resource("ws", suspend)["ok"] is True
    restore_result = api.submit_resource("ws", restore)
    assert restore_result["missions"] == 1
    restore_text = restore_human(restore_result)
    assert restore_text == "Restored 1 missions (schema 10, last event 42)."
    assert "/root/backup.db" not in restore_text
    assert api.opener.calls[0][1]["worker"] == "ws"
    assert [call[0][1] for call in api.opener.calls] == [
        "/api/v1/missions", "/api/v1/missions/m1/suspend-until-credits", "/api/v1/workers/ws/restore",
    ]


def test_api016_surface_operations_use_resource_routes():
    routes = {
        ("GET", "/api/v1/missions/m1/settings"): (200, {"state": "done", "result": {"auto_continue": False}}),
        ("PATCH", "/api/v1/missions/m1/settings"): (200, {"state": "done", "result": {"detail": "updated"}}),
        ("POST", "/api/v1/missions/m1/settings"): (404, None),
        ("POST", "/api/v1/missions/m1/steer"): (200, {"state": "done", "result": {"status": "APPLIED"}}),
        ("POST", "/api/v1/missions/m1/finish"): (200, {"state": "done", "result": {"detail": "finished"}}),
        ("GET", "/api/v1/missions/m1/rotation"): (200, {"state": "done", "result": {"mode": "providers"}}),
        ("PUT", "/api/v1/missions/m1/rotation"): (200, {"state": "done", "result": {"updated": True}}),
        ("GET", "/api/v1/missions/m1/agents"): (200, {"state": "done", "result": {"agents": []}}),
        ("GET", "/api/v1/workers/ws/agents"): (200, {"state": "done", "result": {"agents": []}}),
        ("GET", "/api/v1/missions/m1/proposals"): (200, {"state": "done", "result": {"proposals": []}}),
        ("POST", "/api/v1/missions/m1/proposals"): (200, {"state": "done", "result": {"id": "t"}}),
        ("POST", "/api/v1/missions/m1/proposals/3/accept"): (200, {"state": "done", "result": {"accepted": True}}),
        ("POST", "/api/v1/missions/m1/proposals/3/reject"): (200, {"state": "done", "result": {"accepted": False}}),
        ("GET", "/api/v1/workers/ws/doctor"): (200, {"state": "done", "result": {"sqlite_integrity": "ok"}}),
        ("GET", "/api/v1/workers/ws/audit"): (200, {"state": "done", "result": {"entries": []}}),
        ("POST", "/api/v1/workers/ws/gc"): (200, {"state": "done", "result": {"stations": []}}),
    }
    api = client(routes)
    requests = [
        remote.request_for(args("mission", "settings", "--mission", "m1"), "ws")[0],
        remote.request_for(args("mission", "set", "--mission", "m1", "--auto-continue", "on"), "ws")[0],
        remote.request_for(args("mission", "color", "--mission", "m1", "auto"), "ws")[0],
        remote.request_for(args("steer", "--mission", "m1", "--kind", "pause-after-iteration"), "ws")[0],
        remote.request_for(args("finish", "--mission", "m1"), "ws")[0],
        remote.request_for(args("rotation", "--mission", "m1"), "ws")[0],
        remote.request_for(args("rotation", "--mission", "m1", "--set", "codex"), "ws")[0],
        remote.request_for(args("agents", "--mission", "m1"), "ws")[0],
        remote.request_for(args("proposal", "list", "--mission", "m1"), "ws")[0],
        remote.request_for(args("proposal", "create", "--mission", "m1", "--operation", "op1", "--prompt", "new"), "ws")[0],
        remote.request_for(args("proposal", "accept", "--mission", "m1", "--task", "3"), "ws")[0],
        remote.request_for(args("proposal", "reject", "--mission", "m1", "--task", "3"), "ws")[0],
        remote.request_for(args("doctor"), "ws")[0], remote.request_for(args("worker", "audit"), "ws")[0],
        remote.request_for(args("gc"), "ws")[0],
    ]
    for request in requests:
        assert api.submit_resource("ws", request) is not None, request.operation
    colour_patch = next(call for call in api.opener.calls if call[0] == ("PATCH", "/api/v1/missions/m1/settings") and call[1].get("colour") == "auto")
    assert colour_patch[1]["colour"] == "auto"
    proposal_paths = [call[3].split("8443", 1)[1] for call in api.opener.calls if "/proposals" in call[3]]
    assert any(path.startswith("/api/v1/missions/m1/proposals?") and "worker=ws" in path for path in proposal_paths)
    assert any(path.endswith("/proposals/3/accept?worker=ws") for path in proposal_paths)
    assert any(path.endswith("/proposals/3/reject?worker=ws") for path in proposal_paths)
    all_agents = remote.request_for(args("agents", "--all-missions"), "ws")[0]
    assert api.submit_resource("ws", all_agents) is not None
    assert api.opener.calls[-1][0][1] == "/api/v1/workers/ws/agents"


def test_task_dependency_cli_options_use_task_resource_routes():
    routes = {
        ("POST", "/api/v1/missions/m1/tasks"): (200, {"state": "done", "result": {"id": "task-5"}}),
        ("PATCH", "/api/v1/missions/m1/tasks/4"): (200, {"state": "done", "result": {"id": "task-4"}}),
    }
    api = client(routes)
    add, _ = remote.request_for(args("task", "add", "--mission", "m1", "--prompt", "new", "--depends-on", "1,3"), "ws")
    dependency, _ = remote.request_for(args("task", "dependencies", "--mission", "m1", "--task", "4", "--add", "2"), "ws")
    assert api.submit_resource("ws", add)["id"] == "task-5"
    assert api.submit_resource("ws", dependency)["id"] == "task-4"
    assert [call[0] for call in api.opener.calls] == [
        ("POST", "/api/v1/missions/m1/tasks"), ("PATCH", "/api/v1/missions/m1/tasks/4"),
    ]
    assert api.opener.calls[0][1]["depends_on"] == [1, 3]
    assert api.opener.calls[1][1]["depends_on"] == 2


def test_task_list_all_collects_bounded_resource_pages():
    routes = {
        ("GET", "/api/v1/missions/m1/tasks"): [
            (200, {"state": "done", "result": {"tasks": [{"ordinal": 1}, {"ordinal": 2}], "total": 3, "offset": 0}}),
            (200, {"state": "done", "result": {"tasks": [{"ordinal": 3}], "total": 3, "offset": 2}}),
        ],
    }
    api = client(routes)
    request, _ = remote.request_for(args("task", "list", "--mission", "m1", "--all"), "ws")
    result = api.submit_resource("ws", request)
    assert result == {"tasks": [{"ordinal": 1}, {"ordinal": 2}, {"ordinal": 3}], "total": 3, "offset": 0}
    assert ["offset=0" in call[3] for call in api.opener.calls] == [True, False]
    assert "offset=2" in api.opener.calls[1][3]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["task", "add", "--mission", "m1", "--prompt", "x", "--depends-on", "oops"], "comma-separated task numbers"),
    ],
)
def test_unmappable_commands_fail_explicitly(argv, message):
    with pytest.raises(ValueError, match=message):
        remote.request_for(args(*argv), "ws")


def test_backup_target_is_explicitly_excluded_due_to_artifact_streaming_policy():
    with pytest.raises(ValueError, match="streaming artifact route"):
        remote.request_for(args("backup", "--output", "/tmp/bridge.db"), "ws")


def test_run_cli_outcomes(capsys):
    mission_page = {"missions": [], "next_cursor": None}
    assert remote.run_cli("ws", args("mission", "list"), client=client({("GET", "/api/v1/missions"): (200, mission_page)})) == 0
    assert capsys.readouterr().out.strip()
    pending = {"state": "pending", "result": None}
    assert remote.run_cli("ws", args("status", "--mission", "m"), client=client({("GET", "/api/v1/missions/m"): (202, pending)})) == remote.EXIT_UNAVAILABLE
    assert "stays queued" in capsys.readouterr().err
    uncertain = {"state": "done", "result": {"outcome": "uncertain", "error": {"message": "disk"}}}
    assert remote.run_cli("ws", args("cancel", "--mission", "m"), client=client({("POST", "/api/v1/missions/m/cancel"): (200, uncertain)})) == 2
    assert "uncertain" in capsys.readouterr().err
    refused = {"error": {"code": "unknown-target", "message": "no worker"}}
    assert remote.run_cli("x", args("status", "--mission", "m"), client=client({("GET", "/api/v1/missions/m"): (404, refused)})) == 2
    assert "unknown-target" in capsys.readouterr().err
    assert remote.run_cli("x", args("status", "--mission", "m"), client=client({("GET", "/api/v1/missions/m"): (401, None)})) == 2
    assert "API token" in capsys.readouterr().err
    assert remote.run_cli("x", args("status", "--mission", "m"), client=client({("GET", "/api/v1/missions/m"): (500, {})})) == 2
    assert "HTTP 500" in capsys.readouterr().err
    unreachable = client({("GET", "/api/v1/missions/m"): urllib.error.URLError("refused")})
    assert remote.run_cli("x", args("status", "--mission", "m"), client=unreachable) == 2
    assert "cannot reach" in capsys.readouterr().err
    assert remote.run_cli("ws", args("status"), client=client({})) == 2
    assert "--mission is required" in capsys.readouterr().err


def test_resource_submission_uses_resource_path_and_idempotency_key():
    done = {"state": "done", "request_id": "req-add", "result": {"outcome": "applied", "result": {"id": 4}, "error": None}}
    fake = client({("POST", "/api/v1/missions/m1/tasks"): (200, done)})
    request = remote.commands.CommandRequest(
        "task.add", {"title": "A task", "prompt": "Do work"}, mission_id="m1", target="ws", request_id="req-add"
    )
    assert fake.submit_resource("ws", request) == done["result"]
    sent = fake.opener.calls[0]
    assert sent[0] == ("POST", "/api/v1/missions/m1/tasks")
    assert sent[1] == {"title": "A task", "prompt": "Do work", "wait_ms": 15000}
    assert sent[2]["Idempotency-key"] == "req-add"

    shown = {"state": "done", "result": {"outcome": "ok", "result": {"ordinal": 2}, "error": None}}
    viewer = client({("GET", "/api/v1/missions/m1/tasks/2"): (200, shown)})
    show_request = remote.commands.CommandRequest("task.show", {"task_number": 2}, mission_id="m1", target="ws", request_id="req-show")
    assert viewer.submit_resource("ws", show_request) == shown["result"]

    stopped = client({("POST", "/api/v1/missions/m1/stop"): (200, done)})
    stop_request = remote.commands.CommandRequest("mission.stop", {"force": True}, mission_id="m1", target="ws", request_id="req-stop")
    assert stopped.submit_resource("ws", stop_request) == done["result"]
    sent = stopped.opener.calls[0]
    assert sent[0] == ("POST", "/api/v1/missions/m1/stop")
    assert sent[1] == {"force": True, "wait_ms": 15000}
    assert sent[2]["Idempotency-key"] == "req-stop"


def test_target_all_aggregates_reads_and_reports_unavailable_workers(capsys):
    workers = {"workers": [{"name": "a", "state": "online"}, {"name": "b", "state": "offline"}, {"name": "c", "state": "online"}]}
    mission_page = {"missions": [], "next_cursor": None}
    routes = {("GET", "/api/v1/workers"): (200, workers), ("GET", "/api/v1/missions"): [(200, mission_page), (202, {"state": "pending"})]}
    fake = client(routes)
    assert remote.run_cli("all", args("mission", "list", "--json"), client=fake) == remote.EXIT_UNAVAILABLE
    targets = json.loads(capsys.readouterr().out)["targets"]
    assert targets["a"]["outcome"] == "ok" and targets["b"]["error"]["code"] == "offline" and targets["c"]["error"]["code"] == "unreachable"
    assert all(call[0][0] == "GET" for call in fake.opener.calls)
    assert len([call for call in fake.opener.calls if call[0] == ("GET", "/api/v1/missions")]) == 2
    assert remote.run_cli("all", args("mission", "list"), client=client({("GET", "/api/v1/workers"): (200, {"workers": []})})) == 0
    assert "No workers" in capsys.readouterr().out
    all_missions = client({("GET", "/api/v1/workers"): (200, workers), ("GET", "/api/v1/missions"): (200, mission_page)})
    assert remote.run_cli("all", args("mission", "list"), client=all_missions) == remote.EXIT_UNAVAILABLE
    assert "== a" in capsys.readouterr().out
    assert all(call[0][0] == "GET" for call in all_missions.opener.calls)
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, workers)})) == 0
    assert "offline" in capsys.readouterr().out
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, {"workers": []})})) == 0
    assert "No workers" in capsys.readouterr().out
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, [])})) == 2
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (403, {})})) == 2
    capsys.readouterr()


def test_mission_list_uses_worker_scoped_resource_route_and_preserves_cli_json(capsys):
    mission = {
        "id": "m-1", "name": "Example", "status": "RUNNING",
        "created_at": "2026-11-07T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z",
        "controller_running": True, "folder": "example", "task_counts": {"QUEUED": 1}, "model": "codex",
    }
    api = client({
        ("GET", "/api/v1/missions"): (200, {
            "missions": [{"worker": "worker-a", "stale": False, "mission": mission}], "next_cursor": None
        })
    })
    assert remote.run_cli("worker-a", args("mission", "list", "--json"), client=api) == 0
    public_cli_row = {field: mission[field] for field in ("id", "name", "status", "created_at", "updated_at", "controller_running")}
    assert json.loads(capsys.readouterr().out) == {"missions": [public_cli_row]}
    call = api.opener.calls[0]
    assert call[0] == ("GET", "/api/v1/missions")
    assert "limit=500" in call[3]
    assert "worker=worker-a" in call[3]
    assert all(call[0][0] == "GET" for call in api.opener.calls)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"worker": "worker-a", "stale": False, "mission": {"id": "m-1", "name": "Example", "status": "RUNNING", "created_at": "now", "updated_at": "now"}}, "incomplete mission summary"),
        ({"worker": "worker-a", "stale": False, "mission": {"id": "m-1", "name": "Example", "status": "RUNNING", "created_at": "now", "updated_at": "now", "controller_running": "yes"}}, "invalid mission summary"),
        ({"worker": "worker-a", "stale": False, "mission": None}, "unexpected mission row"),
        ("invalid row", "unexpected mission row"),
        ({"worker": "worker-a", "stale": False, "mission": {"id": None, "name": "Example", "status": "RUNNING", "created_at": "now", "updated_at": "now", "controller_running": True}}, "invalid mission summary"),
    ],
)
def test_remote_mission_list_rejects_incomplete_or_mistyped_cli_fields(row, expected, capsys):
    api = client({
        ("GET", "/api/v1/missions"): (200, {
            "missions": [row],
            "next_cursor": None,
        })
    })

    assert remote.run_cli("worker-a", args("mission", "list", "--json"), client=api) == 2
    error = capsys.readouterr().err
    assert expected in error


def test_main_routes_targets_and_worker_list_to_the_remote_client(capsys):
    assert main(["--target", "ws", "status", "--mission", "m"]) == 2
    assert "no coordinator is configured" in capsys.readouterr().err
    assert main(["worker", "list"]) == 2
    assert "no coordinator is configured" in capsys.readouterr().err


def test_worker_list_shows_age_not_epoch():
    workers = [{"name": "ws", "host_name": "builder", "remote_address": "203.0.113.7", "state": "online", "last_seen": 1000}, {"name": "old", "state": "offline", "last_seen": None},
               {"name": "x", "state": "offline", "last_seen": "garbage"}]
    text = remote._format_workers(workers, now=1000 + 30)
    assert "last seen just now" in text and "last seen never" in text and "1000" not in text
    assert "builder (203.0.113.7)" in text and "unknown host (offline)" in text
    assert "last seen 1h 2m ago" in remote._format_workers(workers[:1], now=1000 + 3720)


def test_redirects_are_refused_so_the_token_is_never_resent(tmp_path):
    import http.server

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = remote.RemoteConfig(f"http://127.0.0.1:{server.server_address[1]}", TOKEN)
        status, _ = remote.Client(config, opener=remote._open_without_redirects).call("GET", "/api/v1/workers")
        assert status == 302 and [path for path, _ in hits] == ["/api/v1/workers"]
    finally:
        server.shutdown()
        server.server_close()
