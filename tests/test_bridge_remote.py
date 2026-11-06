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
        calls.append((key, json.loads(request.data) if request.data else None, dict(request.header_items())))
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
        (["status", "--mission", "m1"], "mission.status", {}),
        (["events", "--mission", "m1", "--after", "3"], "mission.events", {"after": 3, "limit": 100}),
        (["stop", "--mission", "m1"], "mission.stop", {}),
        (["start", "--mission", "m1", "--headless", "--providers", "codex"], "mission.start", {"headless": True, "native_tui": None, "providers": "codex", "routes": None}),
        (["resume", "--mission", "m1"], "mission.resume", {}),
        (["task", "list", "--mission", "m1"], "task.list", {"limit": 200, "offset": 0}),
        (["task", "show", "--mission", "m1", "--task-id", "4"], "task.show", {"task_id": 4}),
        (["task", "verify", "--mission", "m1", "--task", "2", "--approve"], "task.verify", {"task_number": 2, "evidence": "", "approved": True}),
        (["task", "reject", "--mission", "m1", "--task", "2", "--reason", "no"], "task.reject", {"task_number": 2, "reason": "no"}),
        (["task", "reorder", "--mission", "m1", "--task", "2", "--value", "5"], "task.reorder", {"task_number": 2, "order": 5}),
        (["task", "retry", "--mission", "m1", "--task", "2"], "task.retry", {"task_number": 2}),
        (["task", "add", "--mission", "m1", "--prompt", "x"], "task.add", {"prompt": "x", "title": "", "priority": 0, "order": None, "acceptance": []}),
        (["proposal", "list", "--mission", "m1"], "proposal.list", {"status": "PENDING", "limit": 100, "after": 0}),
        (["proposal", "accept", "--mission", "m1", "--task", "3"], "proposal.decide", {"task_number": 3, "accept": True, "reason": ""}),
        (["proposal", "reject", "--mission", "m1", "--task", "3", "--reason", "no"], "proposal.decide", {"task_number": 3, "accept": False, "reason": "no"}),
    ],
)
def test_request_mapping(argv, operation, params):
    request, _ = remote.request_for(args(*argv), "ws")
    assert (request.operation, dict(request.params), request.target) == (operation, params, "ws")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["stop", "--mission", "m1", "--force"], "not available"),
        (["doctor"], "not available"),
        (["task", "list", "--mission", "m1", "--all"], "--limit"),
        (["task", "add", "--mission", "m1", "--prompt", "x", "--depends-on", "1"], "depends-on"),
        (["task", "dependencies", "--mission", "m1", "--task", "1", "--add", "2"], "dependencies"),
    ],
)
def test_unmappable_commands_fail_explicitly(argv, message):
    with pytest.raises(ValueError, match=message):
        remote.request_for(args(*argv), "ws")


def test_run_cli_outcomes(capsys):
    done = {"state": "done", "result": {"outcome": "ok", "result": {"missions": []}, "error": None}}
    assert remote.run_cli("ws", args("mission", "list"), client=client({("POST", "/api/v1/commands"): (200, done)})) == 0
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


def test_target_all_aggregates_reads_and_reports_unavailable_workers(capsys):
    workers = {"workers": [{"name": "a", "state": "online"}, {"name": "b", "state": "offline"}, {"name": "c", "state": "online"}]}
    done = {"state": "done", "result": {"outcome": "ok", "result": {"missions": []}, "error": None}}
    routes = {("GET", "/api/v1/workers"): (200, workers), ("POST", "/api/v1/commands"): [(200, done), (202, {"state": "pending"})]}
    fake = client(routes)
    assert remote.run_cli("all", args("mission", "list", "--json"), client=fake) == remote.EXIT_UNAVAILABLE
    targets = json.loads(capsys.readouterr().out)["targets"]
    assert targets["a"]["outcome"] == "ok" and targets["b"]["error"]["code"] == "offline" and targets["c"]["error"]["code"] == "unreachable"
    request_ids = {call[1]["request"]["request_id"] for call in fake.opener.calls if call[1]}
    assert len(request_ids) == 2  # one independent request per worker
    assert remote.run_cli("all", args("mission", "list"), client=client({("GET", "/api/v1/workers"): (200, {"workers": []})})) == 0
    assert "No workers" in capsys.readouterr().out
    assert remote.run_cli("all", args("mission", "list"), client=client({("GET", "/api/v1/workers"): (200, workers), ("POST", "/api/v1/commands"): [(200, done), (200, done)]})) == remote.EXIT_UNAVAILABLE
    assert "== a" in capsys.readouterr().out
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, workers)})) == 0
    assert "offline" in capsys.readouterr().out
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, {"workers": []})})) == 0
    assert "No workers" in capsys.readouterr().out
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (200, [])})) == 2
    assert remote.run_cli("all", args("worker", "list"), client=client({("GET", "/api/v1/workers"): (403, {})})) == 2
    capsys.readouterr()


def test_main_routes_targets_and_worker_list_to_the_remote_client(capsys):
    assert main(["--target", "ws", "status", "--mission", "m"]) == 2
    assert "no coordinator is configured" in capsys.readouterr().err
    assert main(["worker", "list"]) == 2
    assert "no coordinator is configured" in capsys.readouterr().err


def test_worker_list_shows_age_not_epoch():
    workers = [{"name": "ws", "state": "online", "last_seen": 1000}, {"name": "old", "state": "offline", "last_seen": None},
               {"name": "x", "state": "offline", "last_seen": "garbage"}]
    text = remote._format_workers(workers, now=1000 + 30)
    assert "last seen just now" in text and "last seen never" in text and "1000" not in text
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
