"""Real tmux stations: mission switcher, station key binding and /finish.

Provider processes are fake scripts started with ``--command-template``; no
real provider CLI runs.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled, wait_for as _shared_wait_for

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

# Exits when it reads a line that is /quit or /exit. With FAKE_TWO_ENTER=1 the
# command only counts after a further empty line, like a composer whose first
# Enter accepts a slash-command popup (the popup entry is printed below it).
# It shows a composer prompt, which /finish requires before it types.
PROVIDER = """\
import os, sys
print('FAKE-PROVIDER ' + os.environ.get('FAKE_NAME', ''), flush=True)
sys.stdout.write('\u203a ')
sys.stdout.flush()
pending = False
for line in sys.stdin:
    text = line.strip()
    if os.environ.get('FAKE_TWO_ENTER') == '1':
        if pending and text == '':
            break
        pending = text in ('/quit', '/exit')
        if pending:
            print('  ' + text + '   Exit the provider', flush=True)
        continue
    if text in ('/quit', '/exit'):
        break
"""


def call(env: dict[str, str], *args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
        env=env, text=True, capture_output=True, timeout=scaled(40),
    )
    if check:
        assert result.returncode == 0, result.stderr + result.stdout
    return result


_SHORT_EXIT_TIMEOUT = (
    "import sys; from llm_tools import bridge, bridge_finish; "
    "bridge_finish.EXIT_TIMEOUT_SECONDS = float(sys.argv.pop(1)); raise SystemExit(bridge.main())"
)


def call_with_exit_timeout(env: dict[str, str], seconds: float, *args: object) -> subprocess.CompletedProcess[str]:
    """Run the CLI with ``bridge_finish.EXIT_TIMEOUT_SECONDS`` shortened so a stubborn provider fails quickly."""
    return subprocess.run(
        [sys.executable, "-c", _SHORT_EXIT_TIMEOUT, str(scaled(seconds)), *map(str, args)],
        env=env, text=True, capture_output=True, timeout=scaled(40),
    )


def wait_for(predicate, timeout: float = 20.0, message: str = "condition not reached") -> None:
    _shared_wait_for(predicate, timeout, 0.05, message=message)


class Client:
    """A terminal attached to a station through a real pseudo-terminal."""

    def __init__(self, socket: Path, config: Path, session: str, env: dict[str, str], size: tuple[int, int] = (100, 30),
                 after: str | None = None) -> None:
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
        self.tty = os.ttyname(slave)
        child_env = {k: v for k, v in env.items() if k not in {"TMUX", "TMUX_PANE"}}
        child_env["TERM"] = "xterm-256color"
        argv = ["tmux", "-S", str(socket), "-f", str(config), "attach-session", "-t", session]
        if after is not None:  # a launching shell: runs `after` once the attach command ends
            argv = ["sh", "-c", shlex.join(argv) + "; " + after]
        self.process = subprocess.Popen(
            argv,
            stdin=slave, stdout=slave, stderr=slave, env=child_env, start_new_session=True, close_fds=True,
        )
        os.close(slave)
        self.master = master
        self.output = bytearray()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.output.extend(data)
            del self.output[:-200000]

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.process.wait(timeout=scaled(5))
        os.close(self.master)


def clients_of(socket: Path) -> list[str]:
    result = subprocess.run(
        ["tmux", "-S", str(socket), "list-clients", "-F", "#{client_name}"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.split() if result.returncode == 0 else []


def prefix_pending(socket: Path, tty: str) -> bool:
    """Whether tmux has taken the client's prefix key and awaits the next key."""
    result = subprocess.run(
        ["tmux", "-S", str(socket), "list-clients", "-F", "#{client_name} #{client_prefix}"],
        capture_output=True, text=True, check=False,
    )
    return f"{tty} 1" in result.stdout.splitlines()


def wait_drawn(client: "Client") -> None:
    """Wait for the attached client to receive the station's first screen."""
    wait_for(lambda: len(client.output) > 0, message="the attached client drew nothing")


def press_prefix(client: "Client", socket: Path, key: bytes) -> None:
    """Send Ctrl-], wait until tmux holds the prefix, then send ``key``."""
    client.send(b"\x1d")
    wait_for(lambda: prefix_pending(socket, client.tty), message="tmux did not take the prefix key")
    client.send(key)


def wait_quiet(client: "Client", quiet: float = 0.2) -> None:
    """Wait until the client has stopped receiving output for ``quiet`` seconds."""
    last, since = len(client.output), time.monotonic()
    deadline = time.monotonic() + scaled(10)
    while time.monotonic() < deadline:
        time.sleep(0.02)
        size = len(client.output)
        if size != last:
            last, since = size, time.monotonic()
        elif time.monotonic() - since >= scaled(quiet):
            return


def type_into_picker(client: "Client", text: bytes) -> None:
    """Type into the popup picker and wait until the screen redraws with the text."""
    seen = bytes(client.output).count(text)
    client.send(text)
    wait_for(lambda: bytes(client.output).count(text) > seen, message="the picker did not echo the filter text")


def native_env(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    env["LLM_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps(
        {"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}
    )
    return env


def create_native(env: dict[str, str], folder: Path, name: str, provider: Path, extra_env: dict[str, str] | None = None) -> tuple[str, int]:
    folder.mkdir(parents=True, exist_ok=True)
    created = json.loads(call(
        env, "mission", "create", "--prompt", f"Work on {name}", "--name", name, "--cwd", folder,
        "--providers", "codex", "--native", "--command-template", shlex.join([sys.executable, str(provider)]), "--json",
    ).stdout)
    return created["mission"]["id"], int(created["task"]["id"])


def handle_of(mission_id: str, env: dict[str, str]):
    return TmuxTerminalBackend().open_existing(mission_socket_path(mission_id, env=env), f"mission-{mission_id}")


def task_status(env: dict[str, str], mission_id: str, task_id: int) -> str:
    with BridgeStore(Path(env["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
        return str(store.get_task(task_id)["status"])


def teardown(env: dict[str, str], mission_ids: list[str], clients: list[Client]) -> None:
    for client in clients:
        client.close()
    for mission_id in mission_ids:
        call(env, "stop", "--force", "--mission", mission_id, check=False)
        socket = mission_socket_path(mission_id, env=env)
        if socket.exists():
            try:
                TmuxTerminalBackend().stop(handle_of(mission_id, env))
            except (OSError, RuntimeError, PermissionError):
                pass


@pytest.fixture()
def two_stations(tmp_path: Path, env: dict[str, str]):
    env = native_env(tmp_path, env)
    provider = tmp_path / "provider.py"
    provider.write_text(PROVIDER, encoding="utf-8")
    alpha, alpha_task = create_native(env, tmp_path / "alpha-repo", "Alpha work", provider)
    beta, beta_task = create_native(env, tmp_path / "beta-repo", "Beta work", provider)
    clients: list[Client] = []
    try:
        for mission_id, task_id in ((alpha, alpha_task), (beta, beta_task)):
            call(env, "start", "--mission", mission_id)
            wait_for(lambda m=mission_id, t=task_id: task_status(env, m, t) == "RUNNING", message="task did not start")
        yield env, (alpha, alpha_task), (beta, beta_task), clients
    finally:
        teardown(env, [alpha, beta], clients)


def test_missions_command_switches_attached_client_to_other_station(two_stations) -> None:
    env, (alpha, _), (beta, _), clients = two_stations
    a_handle, b_handle = handle_of(alpha, env), handle_of(beta, env)
    client = Client(a_handle.socket_path, a_handle.config_path, a_handle.session_name, env)
    clients.append(client)
    wait_for(lambda: client.tty in clients_of(a_handle.socket_path), message="client did not attach")
    backend = TmuxTerminalBackend()
    wait_for(lambda: "type a command" in backend.capture(a_handle, pane="bridge"), message="console did not start")

    backend.send_keys(a_handle, pane="bridge", keys="/missions")
    backend.send_keys(a_handle, pane="bridge", keys="Enter")
    wait_for(lambda: "Filter:" in backend.capture(a_handle, pane="bridge"), message="picker did not open")
    wait_for(
        lambda: alpha[:8] in backend.capture(a_handle, pane="bridge") and beta[:8] in backend.capture(a_handle, pane="bridge"),
        message="picker did not list both missions",
    )
    backend.send_keys(a_handle, pane="bridge", keys="beta-repo")
    backend.send_keys(a_handle, pane="bridge", keys="Enter")

    wait_for(lambda: client.tty in clients_of(b_handle.socket_path), message="client was not switched to station B")
    assert client.tty not in clients_of(a_handle.socket_path)


def test_picker_switch_then_one_detach_returns_to_where_the_user_launched_from(two_stations) -> None:
    env, (alpha, _), (beta, _), clients = two_stations
    a_handle, b_handle = handle_of(alpha, env), handle_of(beta, env)
    client = Client(a_handle.socket_path, a_handle.config_path, a_handle.session_name, env, after="echo BACK-AT-LAUNCH-POINT")
    clients.append(client)
    wait_for(lambda: client.tty in clients_of(a_handle.socket_path), message="client did not attach to A")
    wait_drawn(client)
    press_prefix(client, a_handle.socket_path, b"m")  # Ctrl-] then m
    wait_for(lambda: b"Filter:" in bytes(client.output), message="popup picker did not draw")
    type_into_picker(client, b"beta-repo")
    client.send(b"\r")
    wait_for(lambda: client.tty in clients_of(b_handle.socket_path), message="popup pick did not switch the client")
    assert client.tty not in clients_of(a_handle.socket_path)
    assert client.process.poll() is None and b"BACK-AT-LAUNCH-POINT" not in bytes(client.output)
    client.send(b"\x1dd")  # one detach from the destination
    wait_for(lambda: b"BACK-AT-LAUNCH-POINT" in bytes(client.output), message="one detach did not return to the launching shell")
    wait_for(lambda: client.process.poll() is not None, message="the launching shell did not finish")
    assert clients_of(a_handle.socket_path) == [] and clients_of(b_handle.socket_path) == []
    # Neither station was stopped by leaving it.
    backend = TmuxTerminalBackend()
    assert backend.session_exists(a_handle) and backend.session_exists(b_handle)


def test_ctrl_bracket_m_binding_opens_picker_popup(two_stations) -> None:
    env, (alpha, _), (beta, _), clients = two_stations
    a_handle, b_handle = handle_of(alpha, env), handle_of(beta, env)
    keys = subprocess.run(
        ["tmux", "-S", str(a_handle.socket_path), "list-keys", "-T", "prefix"], capture_output=True, text=True, check=True,
    ).stdout
    bound = [line for line in keys.splitlines() if re.match(r"bind-key\s+-T prefix\s+m\s", line)]
    assert bound, keys
    assert "display-popup" in bound[0] and "missions --pick --from " + alpha in bound[0]

    client = Client(a_handle.socket_path, a_handle.config_path, a_handle.session_name, env, size=(100, 30))
    clients.append(client)
    wait_for(lambda: client.tty in clients_of(a_handle.socket_path))
    wait_drawn(client)
    press_prefix(client, a_handle.socket_path, b"m")  # Ctrl-] then m
    wait_for(lambda: b"Filter:" in bytes(client.output), message="popup picker did not draw")
    type_into_picker(client, b"beta-repo")
    client.send(b"\r")
    wait_for(lambda: client.tty in clients_of(b_handle.socket_path), message="popup pick did not switch the client")
    assert client.tty not in clients_of(a_handle.socket_path)


def test_station_config_sets_titles_and_help_mentions_switcher(two_stations) -> None:
    env, (alpha, _), _, _ = two_stations
    handle = handle_of(alpha, env)
    options = subprocess.run(
        ["tmux", "-S", str(handle.socket_path), "show-options", "-g"], capture_output=True, text=True, check=True,
    ).stdout
    assert "set-titles on" in options
    assert "@llm_bridge_context" in options.split("set-titles-string", 1)[1].splitlines()[0]
    keys = subprocess.run(
        ["tmux", "-S", str(handle.socket_path), "list-keys", "-T", "prefix"], capture_output=True, text=True, check=True,
    ).stdout
    assert "Ctrl-] then M" in keys


def test_status_context_carries_mission_label_and_title(two_stations) -> None:
    env, (alpha, _), _, clients = two_stations
    handle = handle_of(alpha, env)
    # An attached console publishes the context; attach a client so the Bridge UI refreshes.
    clients.append(Client(handle.socket_path, handle.config_path, handle.session_name, env))

    def option(name: str) -> str:
        return subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-gqv", name],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    wait_for(lambda: option("@llm_bridge_label") == "alpha-repo", message=f"label missing: {option('@llm_bridge_label')!r}")
    assert option("@llm_bridge_context") == "Task 1 RUNNING"

    def expand(fmt: str) -> str:
        return subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", handle.session_name + ":station", fmt],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    assert expand("#{T:set-titles-string}") == "Bridge · alpha-repo · Task 1 RUNNING"


def wait_for_composer(mission_id: str, env: dict[str, str]) -> None:
    handle = handle_of(mission_id, env)
    backend = TmuxTerminalBackend()
    wait_for(lambda: "\u203a" in backend.capture(handle, pane="provider"), message="fake provider never showed its prompt")


@pytest.mark.parametrize("two_enter", [False, True])
def test_finish_cli_exits_fake_provider_and_task_reaches_verifying(tmp_path: Path, env: dict[str, str], two_enter: bool) -> None:
    env = native_env(tmp_path, env)
    if two_enter:
        env["FAKE_TWO_ENTER"] = "1"
    provider = tmp_path / "provider.py"
    provider.write_text(PROVIDER, encoding="utf-8")
    mission_id, task_id = create_native(env, tmp_path / "repo", "Finish me", provider)
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: task_status(env, mission_id, task_id) == "RUNNING")
        wait_for_composer(mission_id, env)
        result = call(env, "finish", "--mission", mission_id, "--json")
        payload = json.loads(result.stdout)
        assert payload["outcome"] == "exited" and payload["provider"] == "codex"
        assert payload["exit_command"] == "/quit" and payload["task_id"] == task_id
        wait_for(lambda: task_status(env, mission_id, task_id) == "VERIFYING")
    finally:
        teardown(env, [mission_id], [])


def test_finish_console_command_confirms_and_exits_provider(tmp_path: Path, env: dict[str, str]) -> None:
    env = native_env(tmp_path, env)
    env["LLM_BRIDGE_FINISH_CODEX"] = "/exit"
    provider = tmp_path / "provider.py"
    provider.write_text(PROVIDER, encoding="utf-8")
    mission_id, task_id = create_native(env, tmp_path / "repo", "Console finish", provider)
    clients: list[Client] = []
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: task_status(env, mission_id, task_id) == "RUNNING")
        handle = handle_of(mission_id, env)
        backend = TmuxTerminalBackend()
        clients.append(Client(handle.socket_path, handle.config_path, handle.session_name, env))
        wait_for(lambda: bool(clients_of(handle.socket_path)))
        wait_for_composer(mission_id, env)
        backend.send_keys(handle, pane="bridge", keys="/finish")
        backend.send_keys(handle, pane="bridge", keys="Enter")
        wait_for(lambda: "exit now" in backend.capture(handle, pane="bridge"), message="no confirmation prompt")
        assert task_status(env, mission_id, task_id) == "RUNNING"
        backend.send_keys(handle, pane="bridge", keys="y")
        wait_for(lambda: task_status(env, mission_id, task_id) == "VERIFYING", message="task did not reach VERIFYING")
    finally:
        teardown(env, [mission_id], clients)


def test_finish_reports_when_provider_does_not_exit(tmp_path: Path, env: dict[str, str]) -> None:
    env = native_env(tmp_path, env)
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text("import sys\nsys.stdout.write('\\u203a ')\nsys.stdout.flush()\nfor line in sys.stdin:\n    pass\n", encoding="utf-8")
    mission_id, task_id = create_native(env, tmp_path / "repo", "Stubborn", stubborn)
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: task_status(env, mission_id, task_id) == "RUNNING")
        wait_for_composer(mission_id, env)
        result = call_with_exit_timeout(env, 1.0, "finish", "--mission", mission_id)
        assert result.returncode == 2
        assert "Provider did not exit · switch to it with Ctrl-] then P" in result.stderr
        assert task_status(env, mission_id, task_id) == "RUNNING"  # never killed
    finally:
        teardown(env, [mission_id], [])


def _selected_line(screen: str) -> str:
    return next((line for line in screen.splitlines() if line.startswith(("›", ">"))), "")


def test_picker_wheel_and_click_in_real_pty(tmp_path: Path, env: dict[str, str]) -> None:
    """SGR wheel/click sequences written to the picker's pseudo-terminal move and select rows."""
    env = native_env(tmp_path, env)
    names = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven"]
    for index, name in enumerate(names):
        folder = tmp_path / f"repo-{name.lower()}"
        folder.mkdir()
        call(env, "mission", "create", "--prompt", f"Work on {name}", "--name", name, "--cwd", folder, "--providers", "codex", "--json")
        time.sleep(0.01)
    socket = tmp_path / "picker.sock"
    command = f"env TERM=xterm-256color {shlex.quote(sys.executable)} -m llm_tools.bridge missions --pick"

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", *args], env=env, capture_output=True, text=True)

    try:
        tmux("new-session", "-d", "-s", "p", "-x", "80", "-y", "24", command)
        wait_for(lambda: "Filter:" in tmux("capture-pane", "-p", "-t", "p").stdout, message="picker did not start")
        initial = _selected_line(tmux("capture-pane", "-p", "-t", "p").stdout)
        order = [m["name"] for m in json.loads(call(env, "missions", "--json").stdout)["missions"]]
        assert len(order) == 8 and order[0] in initial

        def sgr(code: int, x: int, y: int, final: str = "M") -> None:
            tmux("send-keys", "-t", "p", "-l", f"\x1b[<{code};{x};{y}{final}")

        sgr(65, 10, 6)  # wheel down: three rows
        wait_for(lambda: order[3] in _selected_line(tmux("capture-pane", "-p", "-t", "p").stdout), message="wheel down did not move the selection")
        sgr(64, 10, 6)  # wheel up
        wait_for(lambda: order[0] in _selected_line(tmux("capture-pane", "-p", "-t", "p").stdout), message="wheel up did not move the selection")
        sgr(0, 12, 3 + 5 + 1)  # left button press and release on the sixth row (1-based y)
        sgr(0, 12, 3 + 5 + 1, "m")
        wait_for(lambda: order[5] in _selected_line(tmux("capture-pane", "-p", "-t", "p").stdout), message="click did not select the row")
        tmux("send-keys", "-t", "p", "Escape")
        wait_for(lambda: tmux("list-sessions").returncode != 0, message="Esc did not close the picker")
    finally:
        tmux("kill-server")


def test_wheel_reaches_popup_picker_through_station_client(two_stations) -> None:
    env, (alpha, _), (beta, _), clients = two_stations
    listed = [m["id"] for m in json.loads(call(env, "missions", "--json").stdout)["missions"]]
    first, second = listed[0], listed[1]
    first_handle, second_handle = handle_of(first, env), handle_of(second, env)
    client = Client(first_handle.socket_path, first_handle.config_path, first_handle.session_name, env, size=(100, 30))
    clients.append(client)
    wait_for(lambda: client.tty in clients_of(first_handle.socket_path))
    wait_drawn(client)
    press_prefix(client, first_handle.socket_path, b"m")
    wait_for(lambda: b"Filter:" in bytes(client.output), message="popup picker did not draw")
    # Without the wheel Enter would only refocus the source mission. The wheel
    # (SGR button 65) must reach the popup and move the selection to the second row.
    client.send(b"\x1b[<65;20;8M")
    wait_quiet(client)  # the popup redraws the moved selection, then goes quiet
    client.send(b"\r")
    wait_for(lambda: client.tty in clients_of(second_handle.socket_path), message="wheel did not reach the popup picker")
    assert client.tty not in clients_of(first_handle.socket_path)


def test_finish_refuses_to_type_into_a_real_provider_dialog(tmp_path: Path, env: dict[str, str]) -> None:
    env = native_env(tmp_path, env)
    asker = tmp_path / "asker.py"
    received = tmp_path / "received.txt"
    asker.write_text(
        "import sys\n"
        "print('Do you want to proceed?')\n"
        "print('\\u276f 1. Yes')\n"
        "print('  2. No (esc)', flush=True)\n"
        "line = sys.stdin.readline()\n"
        f"open({str(received)!r}, 'w').write(line)\n",
        encoding="utf-8",
    )
    mission_id, task_id = create_native(env, tmp_path / "repo", "Asking", asker)
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: task_status(env, mission_id, task_id) == "RUNNING")
        handle = handle_of(mission_id, env)
        wait_for(lambda: "Do you want to proceed" in TmuxTerminalBackend().capture(handle, pane="provider"))
        result = call(env, "finish", "--mission", mission_id, check=False)
        assert result.returncode == 2
        assert "Provider is asking something — switch to it with Ctrl-] then P" in result.stderr
        assert not received.exists(), "the provider must not have received any input"
        assert task_status(env, mission_id, task_id) == "RUNNING"
    finally:
        teardown(env, [mission_id], [])
