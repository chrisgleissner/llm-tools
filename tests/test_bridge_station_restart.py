"""Restart of a native mission after its station closed (real tmux, fake provider).

Regression for a live failure: after `stop` and a provider `/quit`, the next
`start` exited with "tmux server is not an LLM Bridge station". The socket
file was left on disk with no listener and no `.conf`.

tmux does not unlink its socket when a server exits, and `attach-session`
(unlike `has-session`, `display-message`, `show-option`, ...) starts a server
when the socket has no listener. An attach that runs after Bridge's teardown
therefore recreates a socket, the server exits at once ("no sessions"), and
the socket stays behind without the `.conf` that teardown already removed.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend
from tests.test_bridge_recovery_tmux import _mission_task_state, _pid_alive, call, controller_released, wait_for
from tests.conftest import scaled


def _late_attach(handle: TerminalHandle) -> None:
    """The unguarded `tmux attach-session` that used to follow a station teardown."""
    subprocess.run(
        ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", f"-t={handle.session_name}"],
        capture_output=True,
        timeout=scaled(10),
    )


def _stop_via_provider_quit(env, backend, mission_id, handle, database, task_id, state) -> None:
    ready = state / "runtime" / f"mission-{mission_id}" / "controller-ready.json"
    controller_pid = int(json.loads(ready.read_text(encoding="utf-8"))["pid"])
    call(env, "stop", "--mission", mission_id)
    backend.send_keys(handle, pane="provider", keys="/quit")
    backend.send_keys(handle, pane="provider", keys="Enter")
    wait_for(lambda: _mission_task_state(database, mission_id, task_id)[0] == "STOPPED")
    wait_for(lambda: controller_released(state, mission_id))
    wait_for(lambda: not _pid_alive(controller_pid))


def test_restart_after_stop_provider_quit_and_late_attach(tmp_path: Path, env: dict[str, str]) -> None:
    assert shutil.which("tmux"), "CI must install tmux for Bridge station integration tests"
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps(
        {"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}
    )
    ran = tmp_path / "provider-ran"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import sys\n"
        f"open({str(ran)!r}, 'a').write('x')\n"
        "print('fake provider ready', flush=True)\n"
        "for line in sys.stdin:\n"
        "    if line.strip() == '/quit':\n"
        "        break\n",
        encoding="utf-8",
    )
    created = json.loads(
        call(
            env, "mission", "create", "--prompt", "Native restart", "--cwd", tmp_path,
            "--providers", "codex", "--native", "--command-template",
            shlex.join([sys.executable, str(provider)]), "--json",
        )
    )
    mission_id = created["mission"]["id"]
    task_id = int(created["task"]["id"])
    database = state / "bridge.sqlite3"
    socket_path = mission_socket_path(mission_id, env=env)
    conf = socket_path.with_name(socket_path.name + ".conf")
    backend = TmuxTerminalBackend()
    session = f"mission-{mission_id}"
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: ran.exists())
        handle = backend.open_existing(socket_path, session)
        _stop_via_provider_quit(env, backend, mission_id, handle, database, task_id, state)
        assert not socket_path.exists() and not conf.exists(), "teardown must remove both station files"

        # What Bridge now runs to reattach a client must not recreate the socket.
        guarded = subprocess.run(["sh", "-c", backend._attach_if_present(handle)], capture_output=True, timeout=scaled(10))
        assert guarded.returncode != 0
        assert not socket_path.exists()

        # Control: the unguarded attach reproduces the leaked socket without a
        # .conf. `stop` on the stopped mission must clear it.
        _late_attach(handle)
        assert socket_path.exists() and not conf.exists()
        stopped = json.loads(call(env, "stop", "--mission", mission_id, "--json"))
        assert stopped["station_status"] == "ABSENT"
        assert not socket_path.exists()

        # The original failure: approve, then start with the leaked socket present.
        _late_attach(handle)
        assert socket_path.exists() and not conf.exists()
        call(env, "task", "add", "--mission", mission_id, "--prompt", "Second increment")
        call(env, "task", "verify", "--mission", mission_id, "--task", task_id, "--approve")
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: ran.read_text() == "xx")
        handle = backend.open_existing(socket_path, session)
        assert backend.pane_is_alive(handle, pane="provider")
        assert conf.exists()
        _stop_via_provider_quit(env, backend, mission_id, handle, database, task_id, state)
    finally:
        if socket_path.exists():
            subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], capture_output=True)
