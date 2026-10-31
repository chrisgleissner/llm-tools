from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import process_hygiene

ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMMANDS = {"./llm-usage", "./llm-scheduler", "./ralph-robin"}
try:
    import coverage as _coverage

    COVERAGE_SITE = str(Path(_coverage.__file__).resolve().parents[1])
except Exception:
    COVERAGE_SITE = ""


def _parent_under_coverage() -> bool:
    """True when this test process is itself being measured by ``coverage``.

    ``coverage run -m pytest`` (how CI invokes the suite) does *not* export
    ``COVERAGE_PROCESS_START`` into the environment, so sniffing env vars can't
    tell us whether measurement is active. The reliable signal is a live
    Coverage instance, which ``Coverage.current()`` returns (and which is
    ``None`` under a plain ``pytest`` run).
    """
    try:
        import coverage

        return coverage.Coverage.current() is not None
    except Exception:
        return False


def timeout_scale() -> float:
    """Multiplier for every wall-clock allowance in the suite (``TEST_TIMEOUT_SCALE``, default 1.0).

    Raise it on a loaded or slow host (for example ``TEST_TIMEOUT_SCALE=3``) so
    subprocess limits, polling deadlines and fake-CLI delays stretch together.
    Invalid or non-positive values fall back to 1.0.
    """
    try:
        value = float(os.environ.get("TEST_TIMEOUT_SCALE", "1"))
    except ValueError:
        return 1.0
    return value if value > 0 else 1.0


def scaled(seconds: float) -> float:
    """``seconds`` multiplied by :func:`timeout_scale`; for timeouts and fixed delays that race under load."""
    return seconds * timeout_scale()


def recorded_exit_status(backend: Any, handle: Any, timeout: float = 3.0) -> str:
    """The exit status tmux recorded for the dead provider pane of ``handle``, or "" when it recorded none.

    tmux 3.4 sometimes marks a pane dead without recording a status (reproduced with a plain
    ``new-session 'sleep 0.5; exit 0'`` on 3.4 in an Ubuntu 24.04 container, 5 to 10 runs in 30;
    not seen on 3.6). It then draws no remain-on-exit footer at all, so a test cannot assert the
    footer text for that pane.
    """
    def status() -> str:
        result = backend._run(handle.socket_path, "display-message", "-p", "-t", handle.provider_pane,
                              "#{pane_dead}\t#{pane_dead_status}", check=False, capture=True)
        dead, _, code = result.stdout.rstrip("\n").partition("\t")
        return code if dead == "1" else ""

    try:
        wait_for(lambda: status() != "", timeout=timeout)
    except AssertionError:
        return ""
    return status()


def wait_for(
    predicate: Callable[[], Any],
    timeout: float = 5.0,
    interval: float = 0.05,
    message: str | None = None,
) -> Any:
    """Poll ``predicate`` until it is truthy or ``timeout`` (scaled) elapses.

    Returns the last predicate value, so ``assert wait_for(...)`` and
    ``value = wait_for(...)`` both work. With ``message`` a timeout raises
    ``AssertionError(message)`` instead of returning the falsy value. The
    predicate is evaluated one final time after the deadline so a condition met
    during the last sleep is not missed.
    """
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    value = predicate()
    if not value and message is not None:
        raise AssertionError(message)
    return value


def local_command_args(args: list[str]) -> list[str]:
    if not args or args[0] not in LOCAL_COMMANDS:
        return args
    return [sys.executable, *args]


@pytest.fixture(autouse=True)
def _measure_terminal_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measure real PTY children that inherit os.environ rather than env()."""
    if _parent_under_coverage():
        monkeypatch.setenv("COVERAGE_PROCESS_START", str(ROOT / "pyproject.toml"))
        paths = [str(ROOT), COVERAGE_SITE, os.environ.get("PYTHONPATH", "")]
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(path for path in paths if path))


@pytest.fixture(autouse=True)
def _no_launcher_burst_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process launcher tests feed keys instantly; real-time guard tests opt back in."""
    from llm_tools import bridge_launcher

    monkeypatch.setattr(bridge_launcher, "_BURST_SECONDS", 0.0)
    # Scripted keys arrive at once: wait for the mission list so the first frames are deterministic.
    monkeypatch.setattr(bridge_launcher, "_FIRST_LOAD_WAIT", 10.0)


@pytest.fixture(autouse=True)
def _utf8_launcher_glyphs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Draw tests assert UTF-8 glyphs whatever locale the test host uses; ASCII tests opt in."""
    from llm_tools import bridge_glyphs

    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: "UTF-8")
    bridge_glyphs.ascii_mode.cache_clear()
    yield
    bridge_glyphs.ascii_mode.cache_clear()


@pytest.fixture(autouse=True)
def _no_live_provider_launch(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Unexpected provider execution must fail locally, never consume capacity.

    Tests can prepend their own deterministic fake binaries. Keeping the guard
    ahead of ambient PATH also protects UI tests that accidentally change which
    action their simulated key stream selects.
    """
    guard = tmp_path_factory.mktemp("provider-launch-guard")
    isolated_home = guard / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    for name in ("codex", "claude", "copilot", "kilo", "mmx", "opencode"):
        path = guard / name
        path.write_text("#!/bin/sh\necho 'Unexpected live provider launch blocked by test harness' >&2\nexit 123\n")
        path.chmod(0o700)
    monkeypatch.setenv("PATH", str(guard) + os.pathsep + os.environ.get("PATH", ""))
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN",
                 "GH_TOKEN", "GITHUB_TOKEN", "ZAI_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _hermetic_power_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch real machine power state from the test suite.

    ``LLM_TOOLS_NO_INHIBIT`` stops any in-process ``ralph_robin.main`` test from
    spawning a real ``systemd-inhibit`` helper. Tests that specifically exercise
    inhibitor/suspend behaviour override these explicitly.
    """
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")


@pytest.fixture(autouse=True)
def _hermetic_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read the developer's real ``~/.config/llm-tools/config.toml``.

    Points the config lookup at an empty directory. Tests that need a config
    file set ``XDG_CONFIG_HOME`` or ``LLM_TOOLS_CONFIG`` themselves.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-xdg-config"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    monkeypatch.delenv("LLM_USAGE_PROVIDERS", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unit readers/samplers must never append to the developer's usage history."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "isolated-xdg-cache"))


# --- Resource markers ---------------------------------------------------------
#
# ``tmux``, ``pty``, ``subprocess`` and ``slow`` are applied from ``tests/markers.json`` so the fast
# inner loop (``-m "not tmux and not pty and not subprocess and not slow and not fuzz"``) runs only in-process tests.
# The manifest is measured, not guessed: ``python -m pytest --record-markers`` runs the whole suite
# serially, notes which tests really start a child process, allocate a pseudo-terminal or run tmux,
# and rewrites the manifest. Keys are test ids without parametrisation. A test missing from the
# manifest (for example a new one) is unmarked, so it runs in the fast loop until the manifest is
# regenerated; nothing is ever skipped by the markers themselves.

MARKERS_FILE = Path(__file__).with_name("markers.json")
SLOW_SECONDS = 3.0
RESOURCE_MARKERS = ("tmux", "pty", "subprocess", "slow")
_RECORD: dict[str, set[str]] = {}
_SECONDS: dict[str, float] = {}
_RECORDING: dict[str, Any] = {"enabled": False, "node": None}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--record-markers", action="store_true", default=False,
        help="run serially and rewrite tests/markers.json from the processes, PTYs and tmux servers the tests really start",
    )


def _marker_key(nodeid: str) -> str:
    return nodeid.split("[", 1)[0]


def _load_markers() -> dict[str, list[str]]:
    try:
        import json

        raw = json.loads(MARKERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {f"{path}::{name}": list(kinds) for path, names in raw.items() for name, kinds in names.items()}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    manifest = _load_markers()
    for item in items:
        for kind in manifest.get(_marker_key(item.nodeid), ()):
            if kind in RESOURCE_MARKERS:
                item.add_marker(getattr(pytest.mark, kind))


def _install_recorder() -> None:
    real_init = subprocess.Popen.__init__
    real_openpty, real_forkpty = os.openpty, os.forkpty

    def note(kind: str) -> None:
        node = _RECORDING["node"]
        if node is not None:
            _RECORD.setdefault(_marker_key(node), set()).add(kind)

    def popen_init(self: subprocess.Popen, args: Any, *a: Any, **kw: Any) -> None:
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        first = os.path.basename(str(argv[0])) if argv else ""
        if first.startswith("tmux") or (first in {"sh", "bash"} and "tmux" in " ".join(map(str, argv))):
            note("tmux")
        note("subprocess")
        real_init(self, args, *a, **kw)

    def openpty() -> tuple[int, int]:
        note("pty")
        return real_openpty()

    def forkpty() -> tuple[int, int]:
        note("pty")
        return real_forkpty()

    subprocess.Popen.__init__ = popen_init  # type: ignore[method-assign]
    os.openpty = openpty  # type: ignore[assignment]
    os.forkpty = forkpty  # type: ignore[assignment]
    import pty as _pty

    _pty.openpty = openpty  # type: ignore[assignment]


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--record-markers") and not hasattr(config, "workerinput"):
        _RECORDING["enabled"] = True
        _install_recorder()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    if _RECORDING["enabled"]:
        _RECORDING["node"] = item.nodeid
    yield
    _RECORDING["node"] = None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _RECORDING["enabled"]:
        _SECONDS[report.nodeid] = _SECONDS.get(report.nodeid, 0.0) + report.duration


def _write_markers() -> None:
    import json

    seconds: dict[str, float] = {}
    for nodeid, spent in _SECONDS.items():
        key = _marker_key(nodeid)
        seconds[key] = max(seconds.get(key, 0.0), spent)  # slowest parametrisation, not their sum
    grouped: dict[str, dict[str, list[str]]] = {}
    for key in sorted(set(_RECORD) | set(seconds)):
        kinds = set(_RECORD.get(key, set()))
        if seconds.get(key, 0.0) >= SLOW_SECONDS:
            kinds.add("slow")
        if kinds:
            path, _, name = key.partition("::")
            grouped.setdefault(path, {})[name] = [kind for kind in RESOURCE_MARKERS if kind in kinds]
    MARKERS_FILE.write_text(json.dumps(grouped, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# --- Bridge runtime-directory hygiene -------------------------------------
#
# Bridge tmux sockets live in ``$XDG_RUNTIME_DIR/llm-bridge`` (or the shared
# ``/tmp/lb-<uid>`` fallback) and ``mission_socket_path`` skips any directory
# that would push the socket path past the ~96 byte sockaddr_un budget. pytest
# tmp paths are long, so a per-test ``tmp_path / "runtime"`` was skipped and
# sockets landed in the real runtime directory. Every test therefore gets a
# short private runtime directory below /tmp instead.

# Resolved once at import: tests replace PATH, and teardown must still find them.
_RM = shutil.which("rm") or "/bin/rm"
_TMUX = shutil.which("tmux")
_POSIX_SPAWN = os.posix_spawn
_WAITPID = os.waitpid
_REAL_RUNTIME_ENV = os.environ.get("XDG_RUNTIME_DIR", "")
_SESSION_RUNTIME_DIRS: set[str] = set()
_SESSION_START = time.time()
_BASELINE: dict[str, object] = {}
_ESCAPED_SOCKETS: dict[str, str] = {}  # socket path -> test id that asked for it


def _real_bridge_dirs() -> list[Path]:
    dirs = [Path("/tmp") / f"lb-{os.getuid()}"]
    if _REAL_RUNTIME_ENV:
        dirs.insert(0, Path(_REAL_RUNTIME_ENV) / "llm-bridge")
    return dirs


def _list_entries(directories: list[Path]) -> set[str]:
    found: set[str] = set()
    for directory in directories:
        try:
            found.update(str(directory / name) for name in os.listdir(directory))
        except OSError:
            pass
    return found


def _socket_is_live(path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _controller_pids() -> dict[int, str]:
    """PIDs of ``llm_tools.bridge _controller`` processes, keyed to their start token.

    Identified from /proc cmdline only; never by pattern-killing.
    """
    found: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
            if b"llm_tools.bridge" in argv and b"_controller" in argv:
                stat_raw = Path(f"/proc/{entry}/stat").read_text(encoding="ascii")
                found[int(entry)] = stat_raw[stat_raw.rfind(")") + 2:].split()[19]
        except (OSError, ValueError, IndexError):
            continue
    return found


def _process_belongs_to_session(pid: int, markers: list[str]) -> bool:
    try:
        blob = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
        blob += os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return False
    return any(marker and marker in blob for marker in markers)


def pytest_sessionstart(session: pytest.Session) -> None:
    if not hasattr(session.config, "workerinput"):
        for directory in process_hygiene.sweep_stale_runtime_dirs():
            sys.stderr.write(f"stopped processes left by a dead test session in {directory}\n")
        for directory in process_hygiene.stale_runtime_dirs():
            _quiet_run([_RM, "-rf", "--", directory])
    _BASELINE["entries"] = _list_entries(_real_bridge_dirs())
    _BASELINE["controllers"] = _controller_pids()


def _quiet_run(argv: list[str]) -> None:
    """Run a short cleanup command without touching patchable Python machinery.

    Tests patch ``subprocess``, ``time.sleep`` and even ``os.read`` globally, and
    teardown can run while those patches are active. ``posix_spawn`` plus a
    blocking ``waitpid`` (both captured at import) avoids all of them; ``rm``
    and ``tmux kill-server`` do not block.
    """
    actions = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in (0, 1, 2)]
    try:
        pid = _POSIX_SPAWN(argv[0], argv, os.environ.copy(), file_actions=actions)
        _WAITPID(pid, 0)
    except OSError:
        pass


def _kill_tmux_servers_in(directory: Path) -> None:
    """Stop tmux servers still listening on sockets inside one test's runtime dir only."""
    # Plain os.listdir: some tests replace os.scandir/Path globbing with fakes,
    # and those patches are still active while this fixture tears down.
    if not _TMUX:
        return
    bridge_dir = os.path.join(str(directory), "llm-bridge")
    try:
        names = [name for name in os.listdir(bridge_dir) if name.endswith(".sock")]
    except OSError:
        return
    for name in names:
        _quiet_run([_TMUX, "-S", os.path.join(bridge_dir, name), "kill-server"])


@pytest.fixture(autouse=True)
def _record_socket_escapes(request: pytest.FixtureRequest, _private_runtime_dir: Path):
    """Remember any in-process Bridge socket path that resolves outside the test's runtime dir."""
    from llm_tools import bridge_runtime

    original = bridge_runtime.mission_socket_path
    sandbox = str(_private_runtime_dir)

    def recording(mission_id: str, *, env: dict[str, str] | None = None) -> Path:
        path = original(mission_id, env=env)
        if not str(path).startswith(sandbox + os.sep):
            _ESCAPED_SOCKETS.setdefault(str(path), request.node.nodeid)
        return path

    bridge_runtime.mission_socket_path = recording  # type: ignore[assignment]
    try:
        yield
    finally:
        if bridge_runtime.mission_socket_path is recording:
            bridge_runtime.mission_socket_path = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _private_runtime_dir(tmp_path: Path):
    """Short, private, per-test XDG_RUNTIME_DIR so Bridge sockets never leave the test.

    Teardown can run while the test's own patches (such as a fake ``os.scandir``)
    are still active, so it avoids Python-level directory walking and uses
    ``rm -rf`` and ``os.listdir`` only.
    """
    runtime = Path(tempfile.mkdtemp(prefix="lbt-", dir="/tmp"))
    runtime.chmod(0o700)
    process_hygiene.write_owner(str(runtime))
    _SESSION_RUNTIME_DIRS.add(str(runtime))
    before = process_hygiene.live_pids()
    patch = pytest.MonkeyPatch()
    patch.setenv("XDG_RUNTIME_DIR", str(runtime))
    try:
        yield runtime
    finally:
        patch.undo()
        _kill_tmux_servers_in(runtime)
        leaked = process_hygiene.reap_leaks(before, [str(runtime), str(tmp_path)])
        _quiet_run([_RM, "-rf", "--", str(runtime)])
        if leaked:
            pytest.fail(
                "test left live processes behind (stopped by the harness):\n  " + "\n  ".join(leaked),
                pytrace=False,
            )


@pytest.fixture()
def env(tmp_path: Path, _private_runtime_dir: Path) -> dict[str, str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    out = os.environ.copy()
    # Keep the fixture hermetic when the suite itself runs inside a ralph-robin
    # session: ralph-robin exports LLM_TOOLS_RALPH_ROBIN_* guard vars into the
    # ambient environment, and copying them here would leak into every CLI
    # subprocess — tripping the "--suspend-until-ready is disabled inside an
    # active ralph-robin provider run" guard and forcing providers down the
    # unavailable/sleep path. Tests that exercise those vars set them explicitly.
    for key in [k for k in out if k.startswith("LLM_TOOLS_RALPH_ROBIN_")]:
        del out[key]
    extras: dict[str, str] = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{ROOT}:{out.get('PATH', '')}",
        # Most tests assert direct reader/CLI behaviour. Service behaviour is
        # covered explicitly and opts back in so the suite does not spawn a
        # background sampler for every llm-usage subprocess.
        "LLM_USAGE_NO_SERVICE": "1",
        "LLM_USAGE_COPILOT_CACHE_TTL": "0",
        # Keep the Copilot add-on (GitHub billing) reader hermetic: never read an
        # ambient GH_TOKEN or shell out to `gh auth token`. Tests that exercise
        # the add-on inject a usage payload via LLM_USAGE_COPILOT_ADDON_USAGE_JSON.
        "LLM_USAGE_DISABLE_COPILOT_ADDON": "1",
        # Same for the monthly premium-request fallback. It is a separate reader
        # from the add-on spend path, so tests opt into it explicitly.
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "1",
        # Keep Codex hermetic: never spawn the real `codex app-server` (which
        # would hit the live account). Tests that exercise the active-refresh
        # path inject a payload via LLM_USAGE_CODEX_RATE_LIMITS_JSON, which
        # takes precedence over this switch.
        "LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1",
        # Active-refresh reads are single-shot under test: retries only matter
        # against a live network and would add real sleeps to failure-path
        # tests. Cases that assert retry behaviour set this explicitly.
        "LLM_USAGE_LIVE_FETCH_RETRIES": "0",
        # Never spawn a real systemd-inhibit helper from in-process or
        # subprocess test runs; inhibitor behaviour is covered explicitly.
        "LLM_TOOLS_NO_INHIBIT": "1",
        "LLM_SCHEDULER_HEADLESS": "1",
    }
    # Only enable coverage instrumentation on CLI subprocesses when the parent
    # test process is itself being measured (``coverage run -m pytest``).
    # Without this guard every subprocess pays ~150ms of coverage.startup()
    # that produces a .coverage.pid file the parent will then drop on the
    # floor, slowing the suite by tens of seconds for no measurement benefit.
    # ``coverage run`` does not export ``COVERAGE_PROCESS_START`` itself, so we
    # detect the live parent Coverage instance and point subprocesses at the
    # project config; their ``coverage.process_startup()`` hook (via the
    # installed .pth / sitecustomize) only starts measuring when that var is
    # set. ``COVERAGE_SITE`` stays on PYTHONPATH so the subprocess can import
    # coverage.
    if _parent_under_coverage():
        extras["COVERAGE_PROCESS_START"] = str(ROOT / "pyproject.toml")
    out.update(extras)
    if COVERAGE_SITE and COVERAGE_SITE not in out.get("PYTHONPATH", ""):
        out["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), COVERAGE_SITE) if p)
    return out


@pytest.fixture()
def fake_bin(env: dict[str, str]) -> Path:
    return Path(env["PATH"].split(":", 1)[0])


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def fake_provider(fake_bin: Path) -> Path:
    return write_exe(
        fake_bin / "provider-mock",
        """#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get("PROVIDER_MODE", "plain")
if os.environ.get("PROVIDER_CAPTURE"):
    with open(os.environ["PROVIDER_CAPTURE"], "ab") as fh:
        fh.write((" ".join(sys.argv[1:])).encode() + b"\\n")
if mode == "plain":
    sys.stdout.write("chat ok\\n")
elif mode == "multiline":
    sys.stdout.write("line one\\nline two\\n")
elif mode == "nonewline":
    sys.stdout.write("no final newline")
elif mode == "ansi":
    sys.stdout.write("\\x1b[31mred\\x1b[0m\\n")
elif mode == "utf8":
    sys.stdout.write("cafe \\u2615\\n")
elif mode == "stderr":
    sys.stderr.write("progress on stderr\\n")
    sys.stdout.write("answer only\\n")
elif mode == "partial_fail":
    sys.stdout.write("partial")
    sys.stdout.flush()
    sys.exit(42)
elif mode == "rate_limit":
    sys.stdout.write("HTTP 429 Too Many Requests\\n")
elif mode == "blocking":
    sys.stdout.write("What do you want to do?\\nEnter to confirm - Esc to cancel\\n")
    sys.stdout.flush()
    time.sleep(20)
elif mode == "idle_no_prompt":
    sys.stdout.write("working...\\n")
    sys.stdout.flush()
    time.sleep(20)
elif mode == "credit_question":
    sys.stdout.write("You've hit your monthly spend limit. Wait for limit to reset or upgrade to Max?\\n")
    sys.stdout.flush()
    time.sleep(20)
sys.stdout.flush()
""",
    )


def run_cmd(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    args = local_command_args(args)
    return subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)


def run_cmd_bytes(args: list[str], env: dict[str, str], **kwargs) -> subprocess.CompletedProcess:
    args = local_command_args(args)
    return subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)


_WORKER_PROBLEMS: list[str] = []


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """pytest-xdist controller: collect the hygiene problems each worker reported when it finished."""
    _WORKER_PROBLEMS.extend(getattr(node, "workeroutput", {}).get("hygiene_problems", []))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail loudly if the suite leaked Bridge sockets or controllers outside its sandbox.

    Other test suites and real Bridge missions may run on this host at the same
    time and write to the same real runtime directory, so a plain before/after
    directory diff cannot prove which process created an entry. The hard checks
    are therefore attributable:

    * a socket path that a test obtained from ``mission_socket_path`` outside its
      private runtime directory, and whose socket or config file now exists;
    * a ``llm_tools.bridge _controller`` started during this session whose
      environment or working directory points into this session's temp dirs and
      that is still alive.

    Unattributable new, non-listening entries in the real directories are
    reported as a warning; ``LLM_TEST_STRICT_RUNTIME_GUARD=1`` makes them fatal
    (use it on a host where no other Bridge activity runs).
    """
    if _RECORDING["enabled"]:
        _write_markers()
    problems: list[str] = []
    baseline_entries: set[str] = _BASELINE.get("entries", set())  # type: ignore[assignment]
    escaped = [f"{path}  (requested by {node})" for path, node in sorted(_ESCAPED_SOCKETS.items())
               if (os.path.exists(path) or os.path.exists(path + ".conf"))
               and path not in baseline_entries and path + ".conf" not in baseline_entries]
    if escaped:
        problems.append("Bridge sockets created outside the per-test runtime dir:\n  " + "\n  ".join(escaped))

    stale = []
    for path in sorted(_list_entries(_real_bridge_dirs()) - baseline_entries):
        sock = path[:-5] if path.endswith(".conf") else path
        # A live listener belongs to a concurrent real Bridge session.
        if not _socket_is_live(sock):
            stale.append(path)
    if stale:
        text = ("new non-listening Bridge runtime entries appeared in the real runtime dir "
                "(may come from a concurrent suite):\n  " + "\n  ".join(stale))
        if os.environ.get("LLM_TEST_STRICT_RUNTIME_GUARD") == "1":
            problems.append(text)
        else:
            sys.stderr.write("\nWARNING: " + text + "\n")

    factory = getattr(session.config, "_tmp_path_factory", None)
    basetemp = str(factory.getbasetemp()) if factory is not None else ""
    markers = [basetemp, *sorted(_SESSION_RUNTIME_DIRS)]
    baseline_controllers: dict[int, str] = _BASELINE.get("controllers", {})  # type: ignore[assignment]
    leaked = [pid for pid, token in _controller_pids().items()
              if baseline_controllers.get(pid) != token and _process_belongs_to_session(pid, markers)]
    if leaked:
        problems.append("Bridge _controller processes started by this session are still alive: "
                        + ", ".join(map(str, leaked)))
    # Under pytest-xdist every worker is its own session: it checks only what it created (its own
    # temp dirs and sockets) and hands the result to the controller, which sets the exit status
    # because a worker's exit status does not fail the run.
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["hygiene_problems"] = problems
        return
    problems = list(dict.fromkeys([*problems, *_WORKER_PROBLEMS]))
    if problems:
        sys.stderr.write("\nTEST HYGIENE FAILURE\n" + "\n".join(problems) + "\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
