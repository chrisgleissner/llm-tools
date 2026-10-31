"""Background sampler for ``llm-usage``.

The service is deliberately small and local-only:

* Unix-domain socket under the user's runtime directory.
* JSON line request/response protocol.
* Latest snapshot plus append-only history JSONL on disk.
* No database, HTTP listener, telemetry, or external dependencies.

``llm-usage`` uses this in two modes. By default it starts an ephemeral service
when no continuous service is already running, reads one snapshot, then asks it
to exit. Users who want instant client startup and continuous burn-down history
can install/start the same foreground service under systemd user services
(Linux) or launchd LaunchAgents (macOS).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from . import common


SERVICE_NAME = "llm-usage"
SERVICE_LABEL = "com.llm-tools.llm-usage"
DEFAULT_INTERVAL_SECONDS = 60
SOCKET_TIMEOUT_SECONDS = 5.0


def service_dir(env: dict[str, str] | None = None) -> Path:
    return common.usage_cache_dir(env) / "service"


def runtime_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    base = env.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "llm-tools"
    return Path(tempfile.gettempdir()) / f"llm-tools-{os.getuid()}"


def socket_path(env: dict[str, str] | None = None) -> Path:
    return runtime_dir(env) / "llm-usage.sock"


def pid_path(env: dict[str, str] | None = None) -> Path:
    return runtime_dir(env) / "llm-usage.pid"


def latest_path(env: dict[str, str] | None = None) -> Path:
    return service_dir(env) / "latest.json"


def history_path(env: dict[str, str] | None = None) -> Path:
    return service_dir(env) / "history.jsonl"


def systemd_unit_path(env: dict[str, str] | None = None) -> Path:
    home = common.home_dir(env)
    return home / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def launchd_plist_path(env: dict[str, str] | None = None) -> Path:
    home = common.home_dir(env)
    return home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _private_owned_directory(path: Path, uid: int) -> bool:
    """A real directory (never a symlink) owned by ``uid`` that no one else can enter."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077


def trusted_directory(path: Path) -> bool:
    """Create ``path`` (mode 0700) if absent and accept it only if it is ours and private.

    The socket directory can be ``/tmp/llm-tools-$UID`` where another user may
    have created the name first; serving or reading snapshots there would let
    that user spoof capacity data. A directory that is a symlink or owned by
    someone else is refused. A loose mode on our own directory (left by an
    older release) is tightened to 0700.
    """
    uid = os.getuid()
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return False
    if _private_owned_directory(path, uid):
        return True
    try:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == uid:
            os.chmod(path, 0o700)
    except OSError:
        return False
    return _private_owned_directory(path, uid)


def trusted_socket_path(env: dict[str, str] | None = None) -> Path | None:
    """The service socket path, or ``None`` when its directory cannot be trusted."""
    sock = socket_path(env)
    return sock if trusted_directory(sock.parent) else None


def peer_uid(sock: socket.socket) -> int | None:
    """UID of the process at the other end of a Unix socket (Linux ``SO_PEERCRED``); ``None`` elsewhere."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        return int(struct.unpack("3i", creds)[1])
    except (OSError, struct.error):
        return None


def _peer_is_us(sock: socket.socket) -> bool:
    uid = peer_uid(sock)
    return uid is None or uid == os.getuid()


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_json_write(path: Path, obj: dict[str, Any]) -> None:
    _mkdir_private(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def _append_history(path: Path, obj: dict[str, Any]) -> None:
    _mkdir_private(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _parse_interval(raw: str | None) -> int:
    try:
        value = int(float(raw or str(DEFAULT_INTERVAL_SECONDS)))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return max(5, value)


@contextmanager
def _temporary_environ(env: dict[str, str]) -> Any:
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


@dataclass
class ServicePaths:
    service_dir: Path
    socket: Path
    pid: Path
    latest: Path
    history: Path


def paths(env: dict[str, str] | None = None) -> ServicePaths:
    root = service_dir(env)
    return ServicePaths(
        service_dir=root,
        socket=socket_path(env),
        pid=pid_path(env),
        latest=latest_path(env),
        history=history_path(env),
    )


class UsageSampler:
    def __init__(self, env: dict[str, str], interval: int) -> None:
        self.env = dict(env)
        self.interval = interval
        self.paths = paths(env)
        self.lock = threading.Lock()
        self.latest: dict[str, Any] | None = None
        self.latest_error: str = ""

    def sample_once(self) -> dict[str, Any]:
        from . import usage

        with self.lock:
            with _temporary_environ(self.env):
                cfg = usage.Config()
                cfg.progress_enabled = False
                cfg.show_remaining_time = False
                cfg.watch_interval = "0"
                cfg.json_output = False
                provider_data = usage.read_all_provider_data(cfg, progress=None)
                usage.log_samples_from_provider_data(provider_data)
                payload = usage.service_payload_from_provider_data(cfg, provider_data, self.env)
            _atomic_json_write(self.paths.latest, payload)
            _append_history(self.paths.history, payload)
            self.latest = payload
            self.latest_error = ""
        return payload

    def snapshot(self, max_age: int | None = None) -> dict[str, Any]:
        with self.lock:
            current = self.latest
        if current is None and self.paths.latest.is_file():
            try:
                loaded = json.loads(self.paths.latest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                current = loaded
                with self.lock:
                    self.latest = loaded
        if current is None:
            return self.sample_once()
        if max_age is not None:
            generated = common.num(current.get("generated_at_epoch"))
            if generated is None or common.now_epoch(self.env) - int(generated) > max_age:
                return self.sample_once()
        return current


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sock: str, sampler: UsageSampler, stop_event: threading.Event) -> None:
        self.sampler = sampler
        self.stop_event = stop_event
        # Bind inside a fresh 0700 directory next to the final path and rename the socket into
        # place after chmod 0600, so it is never reachable by other users. The process umask is
        # shared by every thread, so it must not be changed for this.
        final = Path(sock)
        staging = Path(tempfile.mkdtemp(prefix=".b", dir=str(final.parent)))
        try:
            super().__init__(str(staging / "s"), UsageRequestHandler)
            try:
                os.chmod(staging / "s", 0o600)
            except OSError:
                pass
            os.replace(staging / "s", final)
            self.server_address = str(final)
        except BaseException:
            try:
                (staging / "s").unlink()
            except OSError:
                pass
            raise
        finally:
            try:
                staging.rmdir()
            except OSError:
                pass


class UsageRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _peer_is_us(self.request):
            return  # another user's process connected: answer nothing
        raw = self.rfile.readline(1024 * 1024)
        try:
            req = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write({"ok": False, "error": "bad-request"})
            return
        if not isinstance(req, dict):
            self._write({"ok": False, "error": "bad-request"})
            return
        op = str(req.get("op") or "snapshot")
        server = self.server
        assert isinstance(server, ThreadingUnixServer)
        try:
            if op == "snapshot":
                max_age = req.get("max_age")
                age = int(max_age) if isinstance(max_age, int) and max_age >= 0 else None
                self._write({"ok": True, "snapshot": server.sampler.snapshot(age)})
            elif op == "status":
                snap = server.sampler.snapshot(None)
                self._write(
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "socket": str(server.server_address),
                        "generated_at_epoch": snap.get("generated_at_epoch"),
                    }
                )
            elif op == "shutdown":
                server.stop_event.set()
                self._write({"ok": True})
            else:
                self._write({"ok": False, "error": "unknown-op"})
        except Exception as exc:
            self._write({"ok": False, "error": str(exc)})

    def _write(self, obj: dict[str, Any]) -> None:
        try:
            self.wfile.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        except OSError:
            pass


def _stale_socket(sock: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(sock))
        return False
    except OSError:
        return True


def run_service(*, interval: int, ephemeral: bool = False, env: dict[str, str] | None = None) -> int:
    env = dict(env or os.environ)
    p = paths(env)
    _mkdir_private(p.service_dir)
    if not trusted_directory(p.socket.parent):
        print(f"llm-usage service: refusing untrusted socket directory {p.socket.parent} (must be a private directory you own)", file=sys.stderr)
        return 1
    if p.socket.exists() and _stale_socket(p.socket):
        try:
            p.socket.unlink()
        except OSError:
            pass
    sampler = UsageSampler(env, interval)
    stop_event = threading.Event()
    try:
        server = ThreadingUnixServer(str(p.socket), sampler, stop_event)
    except OSError as exc:
        print(f"llm-usage service: could not bind {p.socket}: {exc}", file=sys.stderr)
        return 1
    try:
        p.pid.write_text(str(os.getpid()) + "\n", encoding="utf-8")
        p.pid.chmod(0o600)
    except OSError:
        pass
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    def _stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    old_int = signal.signal(signal.SIGINT, _stop)
    old_term = signal.signal(signal.SIGTERM, _stop)
    try:
        sampler.sample_once()
        if ephemeral:
            while not stop_event.wait(0.1):
                pass
            return 0
        while not stop_event.is_set():
            if stop_event.wait(interval):
                break
            try:
                sampler.sample_once()
            except Exception as exc:
                with sampler.lock:
                    sampler.latest_error = str(exc)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        server.shutdown()
        server.server_close()
        try:
            p.socket.unlink()
        except OSError:
            pass
        try:
            p.pid.unlink()
        except OSError:
            pass
    return 0


def _request(sock: Path, request: dict[str, Any], timeout: float = SOCKET_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(sock))
            if not _peer_is_us(client):
                return None  # the socket is served by another user: never trust its data
            client.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except OSError:
        return None
    try:
        data = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def _cached_latest_snapshot(env: dict[str, str], max_age: int) -> dict[str, Any] | None:
    path = latest_path(env)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    generated = common.num(payload.get("generated_at_epoch"))
    if generated is None or common.now_epoch(env) - int(generated) > max_age:
        return None
    try:
        from . import usage

        if not usage.service_payload_matches_environment(payload, env):
            return None
        if usage.provider_data_from_service_payload(payload) is None:
            return None
    except Exception:
        return None
    return payload


def request_snapshot(
    *,
    env: dict[str, str] | None = None,
    start_ephemeral: bool = True,
    timeout: float = SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    env = dict(env or os.environ)
    sock = trusted_socket_path(env)
    if sock is None:
        return None  # untrusted socket directory: the caller reads providers directly
    resp = _request(sock, {"op": "snapshot"}, timeout)
    if isinstance(resp, dict) and resp.get("ok") is True and isinstance(resp.get("snapshot"), dict):
        return resp["snapshot"]
    if not start_ephemeral:
        return None
    cached = _cached_latest_snapshot(env, _parse_interval(env.get("LLM_USAGE_SERVICE_INTERVAL")))
    if cached is not None:
        return cached
    proc = start_ephemeral_service(env, timeout=timeout)
    if proc is None:
        return None
    try:
        resp = _request(sock, {"op": "snapshot"}, timeout)
        if isinstance(resp, dict) and resp.get("ok") is True and isinstance(resp.get("snapshot"), dict):
            _request(sock, {"op": "shutdown"}, timeout=1.0)
            return resp["snapshot"]
        return None
    finally:
        try:
            _request(sock, {"op": "shutdown"}, timeout=1.0)
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
            except OSError:
                pass


def start_ephemeral_service(env: dict[str, str], timeout: float = SOCKET_TIMEOUT_SECONDS) -> subprocess.Popen[str] | None:
    p = paths(env)
    _mkdir_private(p.service_dir)
    if not trusted_directory(p.socket.parent):
        return None
    if p.socket.exists() and _stale_socket(p.socket):
        try:
            p.socket.unlink()
        except OSError:
            pass
    cmd = common.python_module_argv(
        "llm_tools.usage_service",
        "--ephemeral",
        "--interval",
        str(_parse_interval(env.get("LLM_USAGE_SERVICE_INTERVAL"))),
    )
    run_env = common.with_package_root(env)
    try:
        proc = subprocess.Popen(
            cmd,
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if p.socket.exists() and not _stale_socket(p.socket):
            return proc
        time.sleep(0.05)
    return proc if proc.poll() is None else None


def running_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(env or os.environ)
    trusted = trusted_socket_path(env)
    resp = _request(trusted, {"op": "status"}, timeout=1.0) if trusted is not None else None
    if isinstance(resp, dict) and resp.get("ok") is True:
        return {"running": True, **resp}
    return {"running": False, "socket": str(socket_path(env))}


def _service_path(env: dict[str, str] | None = None) -> str:
    """PATH to bake into the background-sampler unit.

    systemd (and launchd) start user services with a minimal PATH that
    omits version-manager bin dirs (nvm, volta, asdf, npm-global). The
    sampler then cannot find Node-based CLIs such as ``copilot`` and
    reports them as ``missing-cli`` even though the user has them
    installed -- and that bad snapshot poisons the shared cache the
    foreground ``llm-usage`` reads. We bake an explicit PATH so the
    service sees exactly what the user sees: the directories that hold
    the CLIs we sample, the install-time PATH, then conventional
    fallbacks.
    """
    env = env or os.environ
    home = str(common.home_dir(env))
    parts: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        # Expand ``~`` from ``env`` (not ``os.environ``) so tests that set a
        # synthetic HOME stay hermetic; ``os.path.expanduser`` would leak the
        # real ``$HOME`` here.
        if raw == "~" or raw.startswith("~/"):
            raw = home + raw[1:]
        path = os.path.expanduser(raw)
        if any(ch in path for ch in "\n\r\x00%"):
            return  # cannot be written into a unit file; skip it rather than fail
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            parts.append(path)

    for tool in ("copilot", "node", "codex", "mmx", "kilo", "opencode"):
        found = shutil.which(tool, path=env.get("PATH"))
        if found:
            add(os.path.dirname(found))
    for entry in (env.get("PATH") or "").split(os.pathsep):
        add(entry)
    for fallback in ("~/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        add(fallback)
    return os.pathsep.join(parts)


def _unit_value(value: object, what: str) -> str:
    """A path or value for a unit file; refuses text that systemd or launchd would interpret."""
    text = str(value)
    if any(ch in text for ch in "\n\r\x00%"):
        raise ValueError(f"cannot write {what} into a service definition: it contains a newline, NUL or '%' ({text!r})")
    return text


def _systemd_word(value: object, what: str) -> str:
    """One ExecStart argument, quoted so spaces, quotes and `$` stay literal."""
    text = _unit_value(value, what)
    if re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def _systemd_assignment(name: str, value: str) -> str:
    """An `Environment=` line; quoted when the value contains spaces or quotes."""
    text = f"{name}={value}"
    if re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", text):
        return f"Environment={text}"
    return 'Environment="' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _service_home(env: dict[str, str] | None) -> str:
    """Working directory of the service: the home directory, never the install-time cwd.

    The folder `--install-service` is run from may be a repository checkout
    that someone else controls; a service must not run with it as its cwd.
    """
    return _unit_value(common.home_dir(env or os.environ), "the home directory")


def systemd_unit_text(interval: int, env: dict[str, str] | None = None) -> str:
    exec_start = " ".join(
        _systemd_word(part, "the service command")
        for part in common.python_module_argv("llm_tools.usage_service", "--foreground", "--interval", str(interval))
    )
    cache = _unit_value(service_dir(env), "the service directory")
    path = _unit_value(_service_path(env), "PATH")
    root = _unit_value(common.package_root(), "the package directory")
    home = _service_home(env)
    return f"""[Unit]
Description=llm-usage background sampler
After=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
{_systemd_assignment("PATH", path)}
{_systemd_assignment("PYTHONPATH", root)}
WorkingDirectory={home}
StandardOutput=append:{cache}/service.log
StandardError=append:{cache}/service.err

[Install]
WantedBy=default.target
"""


def launchd_plist_text(interval: int, env: dict[str, str] | None = None) -> str:
    def xml(value: object, what: str) -> str:
        return xml_escape(_unit_value(value, what))

    program = "\n".join(
        f"    <string>{xml(part, 'the service command')}</string>"
        for part in common.python_module_argv("llm_tools.usage_service", "--foreground", "--interval", str(interval))
    )
    root = xml(service_dir(env), "the service directory")
    path = xml(_service_path(env), "PATH")
    package = xml(common.package_root(), "the package directory")
    home = xml(_service_home(env), "the home directory")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{program}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONPATH</key><string>{package}</string>
  </dict>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>StandardOutPath</key><string>{root}/service.log</string>
  <key>StandardErrorPath</key><string>{root}/service.err</string>
</dict>
</plist>
"""


def _run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


def install_service(interval: int, env: dict[str, str] | None = None) -> int:
    env = dict(env or os.environ)
    _mkdir_private(service_dir(env))
    system = platform.system().lower()
    if system == "linux":
        unit = systemd_unit_path(env)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit_text(interval, env), encoding="utf-8")
        if not shutil.which("systemctl"):
            print(f"wrote {unit}; systemctl is not available, start it manually", file=sys.stderr)
            return 0
        rc = _run(["systemctl", "--user", "daemon-reload"])
        if rc == 0:
            rc = _run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"])
        return rc
    if system == "darwin":
        plist = launchd_plist_path(env)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist_text(interval, env), encoding="utf-8")
        uid = os.getuid()
        if not shutil.which("launchctl"):
            print(f"wrote {plist}; launchctl is not available, start it manually", file=sys.stderr)
            return 0
        _run(["launchctl", "bootout", f"gui/{uid}", str(plist)])
        rc = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        if rc == 0:
            rc = _run(["launchctl", "enable", f"gui/{uid}/{SERVICE_LABEL}"])
        if rc == 0:
            rc = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{SERVICE_LABEL}"])
        return rc
    print("llm-usage service install is supported on Linux/systemd and macOS/launchd", file=sys.stderr)
    return 2


def uninstall_service(env: dict[str, str] | None = None) -> int:
    env = dict(env or os.environ)
    system = platform.system().lower()
    if system == "linux":
        rc = 0
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
            _run(["systemctl", "--user", "daemon-reload"])
        try:
            systemd_unit_path(env).unlink()
        except OSError:
            pass
        return rc
    if system == "darwin":
        uid = os.getuid()
        plist = launchd_plist_path(env)
        if shutil.which("launchctl"):
            _run(["launchctl", "bootout", f"gui/{uid}", str(plist)])
            _run(["launchctl", "disable", f"gui/{uid}/{SERVICE_LABEL}"])
        try:
            plist.unlink()
        except OSError:
            pass
        return 0
    return 2


def start_service(env: dict[str, str] | None = None) -> int:
    system = platform.system().lower()
    if system == "linux" and shutil.which("systemctl"):
        return _run(["systemctl", "--user", "start", f"{SERVICE_NAME}.service"])
    if system == "darwin" and shutil.which("launchctl"):
        return _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"])
    print("no supported service manager found; use --service-run in a supervisor", file=sys.stderr)
    return 2


def stop_service(env: dict[str, str] | None = None) -> int:
    trusted = trusted_socket_path(env)
    resp = _request(trusted, {"op": "shutdown"}, timeout=1.0) if trusted is not None else None
    if isinstance(resp, dict) and resp.get("ok") is True:
        return 0
    system = platform.system().lower()
    if system == "linux" and shutil.which("systemctl"):
        return _run(["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"])
    if system == "darwin" and shutil.which("launchctl"):
        return _run(["launchctl", "kill", "TERM", f"gui/{os.getuid()}/{SERVICE_LABEL}"])
    return 2


def service_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm_tools.usage_service")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--interval", default=os.environ.get("LLM_USAGE_SERVICE_INTERVAL", str(DEFAULT_INTERVAL_SECONDS)))
    ns = parser.parse_args(argv)
    return run_service(interval=_parse_interval(ns.interval), ephemeral=bool(ns.ephemeral), env=os.environ)


def main() -> int:
    return service_cli()


if __name__ == "__main__":
    raise SystemExit(main())
