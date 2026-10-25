"""Bridge mission controller and the thin hooks into Ralph Robin."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import select
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import ralph_robin
from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir, utc_now
from .terminal import TerminalHandle, TmuxTerminalBackend


class BridgeMissionHooks:
    """Translate durable Bridge task boundaries into the existing Ralph loop."""

    def __init__(
        self,
        mission_id: str,
        *,
        handle: TerminalHandle | None,
        headless: bool,
        native_tui: bool = False,
        controller_logs: Path,
    ) -> None:
        self.mission_id = mission_id
        self.handle = handle
        self.headless = headless
        self.native_tui = native_tui
        self.controller_logs = controller_logs
        self._store: BridgeStore | None = None
        self._iteration = 0
        self._last_selected_station = ""

    def bind(self, store: BridgeStore) -> None:
        self._store = store

    @staticmethod
    def _selection_projection(selection: dict[str, Any]) -> dict[str, Any]:
        decisions: list[dict[str, Any]] = []
        raw_decisions = selection.get("decisions")
        if isinstance(raw_decisions, list):
            for decision in raw_decisions[:16]:
                if not isinstance(decision, dict):
                    continue
                item: dict[str, Any] = {
                    key: decision[key]
                    for key in ("provider", "route", "selected_model", "usable", "reason", "wait_until")
                    if key in decision and isinstance(decision[key], (str, int, float, bool, type(None)))
                }
                windows: list[dict[str, Any]] = []
                raw_windows = decision.get("windows")
                if isinstance(raw_windows, list):
                    for window in raw_windows[:8]:
                        if isinstance(window, dict):
                            windows.append({
                                key: window[key]
                                for key in ("name", "kind", "remaining_percent", "remaining_amount", "currency", "reset_epoch")
                                if key in window and isinstance(window[key], (str, int, float, bool, type(None)))
                            })
                item["windows"] = windows
                decisions.append(item)
        current = selection.get("decision") if isinstance(selection.get("decision"), dict) else {}
        return {
            "provider": str(selection.get("provider") or current.get("provider") or ""),
            "route": str(selection.get("route") or ""),
            "selected_model": str(selection.get("selected_model") or current.get("selected_model") or ""),
            "rotation_reason": str(selection.get("rotation_reason") or ""),
            "all_rate_limited": bool(selection.get("all_rate_limited")),
            "decision": {key: current[key] for key in ("usable", "reason", "wait_until") if key in current},
            "decisions": decisions,
        }

    def on_selection(self, selection: dict[str, Any]) -> None:
        projection = self._selection_projection(selection)
        station = projection["route"] or projection["provider"]
        if self._last_selected_station and station and station != self._last_selected_station:
            self.store.record_runtime_event(self.mission_id, "station-handoff", {
                "from": self._last_selected_station,
                "to": station,
                "provider": projection["provider"],
                "route": projection["route"],
                "reason": projection["rotation_reason"],
            })
        self.store.record_runtime_event(self.mission_id, "provider-selection", projection)
        if station:
            self._last_selected_station = station

    def on_wait(self, selection: dict[str, Any], target_epoch: int | None) -> None:
        projection = self._selection_projection(selection)
        self.store.record_runtime_event(self.mission_id, "provider-wait", {
            "target_epoch": target_epoch,
            "all_rate_limited": projection["all_rate_limited"],
            "reason": projection["decision"].get("reason", projection["rotation_reason"]),
            "decisions": projection["decisions"],
        })

    @property
    def store(self) -> BridgeStore:
        if self._store is None:
            raise RuntimeError("Bridge hooks are not bound to a store")
        return self._store

    def before_iteration(self) -> dict[str, Any]:
        while True:
            mission = self.store.get_mission(self.mission_id)
            status = mission["status"]
            if status in {"STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"}:
                if status == "STOPPING":
                    self.store.set_mission_status(self.mission_id, "STOPPED")
                return {"action": "stop", "reason": f"mission {status.lower()}", "exit_code": 0}
            if status == "PAUSED":
                time.sleep(0.5)
                continue

            tasks = self.store.ready_tasks(self.mission_id)
            if tasks:
                task = tasks[0]
                current = self.store.begin_iteration(int(task["id"]))
                self._iteration += 1
                history = self.store.db.execute(
                    "SELECT id,kind,data_json,created_at FROM events WHERE mission_id=? AND (task_id=? OR operation_id IN (SELECT id FROM operations WHERE task_id=?)) ORDER BY id DESC LIMIT 20",
                    (self.mission_id, current["id"], current["id"]),
                ).fetchall()
                operations = self.store.db.execute(
                    "SELECT id,station,route_id,model,status,exit_code,logs_path FROM operations WHERE task_id=? ORDER BY created_at DESC LIMIT 10",
                    (current["id"],),
                ).fetchall()
                checkpoint = {
                    "restoration": "RECONSTRUCTED",
                    "task_revision": int(current["revision"]),
                    "evidence": current["evidence"],
                    "operations": [dict(row) for row in operations],
                    "recent_events": [
                        {"id": int(row["id"]), "kind": row["kind"], "data": json.loads(row["data_json"]), "at": row["created_at"]}
                        for row in reversed(history)
                    ],
                }
                prompt = (
                    "LLM Bridge mission context. Provider session continuity is RECONSTRUCTED from durable state; "
                    "this is not an exact native conversation restore.\n\n"
                    f"Mission objective:\n{mission['objective']}\n\n"
                    f"Task {current['id']} ({current['title']}), revision {current['revision']}:\n{current['prompt']}\n\n"
                    f"Acceptance criteria:\n{json.dumps(current['acceptance'], ensure_ascii=False)}\n\n"
                    f"Durable checkpoint (latest bounded history):\n{json.dumps(checkpoint, ensure_ascii=False, default=str)[:12000]}\n\n"
                    "Continue from this state. Do not claim work from an earlier provider session unless supported by the evidence above."
                )
                return {
                    "action": "run",
                    "task_id": int(current["id"]),
                    "revision": int(current["revision"]),
                    "prompt": prompt,
                    "task_title": str(current["title"]),
                    "iteration": self._iteration,
                }

            counts = {row["status"]: int(row["n"]) for row in self.store.db.execute("SELECT status,COUNT(*) AS n FROM tasks WHERE mission_id=? GROUP BY status", (self.mission_id,))}
            total = sum(counts.values())
            if total and counts.get("COMPLETED", 0) == total:
                self.store.set_mission_status(self.mission_id, "COMPLETED")
                return {"action": "stop", "reason": "all tasks verified", "exit_code": 0}
            time.sleep(0.5)

    def before_launch(self, context: dict[str, Any], selection: dict[str, Any], scheduler_config: Any, iteration: int) -> bool:
        mission = self.store.get_mission(self.mission_id)
        if mission["status"] != "RUNNING":
            return False
        provider = str(selection.get("provider") or scheduler_config.provider)
        route_id = str(selection.get("route") or "")
        try:
            operation_id = self.store.begin_operation(int(context["task_id"]), iteration=iteration, station=route_id or provider)
        except TransitionError:
            # Mission pause/stop and operation intent serialize in SQLite. If
            # the command won that race, simply reject this launch boundary.
            if self.store.get_mission(self.mission_id)["status"] != "RUNNING":
                return False
            raise
        pane_id = ""
        if self.handle is not None and self.native_tui:
            scheduler_config.exec_mode = "tmux"
            scheduler_config.tmux_target = f"{self.handle.session_name}:station"
            scheduler_config.tmux_socket = str(self.handle.socket_path)
            scheduler_config.tmux_pane = self.handle.provider_pane
            scheduler_config.native_tui = True
            scheduler_config.headless = False
            pane_id = self.handle.provider_pane
        else:
            scheduler_config.exec_mode = "fresh"
            scheduler_config.headless = True
            scheduler_config.native_tui = False
        self.store.mark_operation_running(
            operation_id,
            pane_id=pane_id,
            route_id=route_id,
            model=str(scheduler_config.model or ""),
        )
        context["operation_id"] = operation_id
        context["selected_route"] = route_id
        context["selected_provider"] = provider
        scheduler_config.process_started = lambda process_pid: self._record_process(operation_id, process_pid)
        return True

    def _record_process(self, operation_id: str, process_pid: int) -> None:
        token = _process_start_token(process_pid)
        if not token:
            # A short provider may have exited before /proc can be read. The
            # operation will be resolved by after_iteration; a crash in this
            # small interval remains uncertain and cannot be force-signalled.
            return
        self.store.record_operation_process(operation_id, process_pid=process_pid, process_start=token)

    def after_iteration(
        self,
        context: dict[str, Any],
        selection: dict[str, Any],
        scheduler_config: Any,
        iteration: int,
        status: int,
        seconds: float,
    ) -> None:
        operation_id = context.get("operation_id")
        if not isinstance(operation_id, str):
            return
        try:
            operation = self.store.get_operation(operation_id)
        except KeyError:
            return
        if operation["status"] not in {"PREPARED", "RUNNING", "INTENT"}:
            return
        run_dir = scheduler_config.run_dir
        output_files = sorted(run_dir.glob("attempt-*.out")) if run_dir else []
        logs_path = str(output_files[-1]) if output_files else None
        try:
            task = self.store.finish_operation(operation_id, exit_code=int(status), logs_path=logs_path)
            if status == 0 and task["status"] == "VERIFYING":
                run_configured_verifier(self.store, self.mission_id, int(task["id"]), runtime=self.controller_logs.parent)
        except TransitionError:
            # A force stop may have durably reconciled this operation before
            # the scheduler noticed that its owned tmux server disappeared.
            return


def _private_verifier_dir(runtime: Path) -> Path:
    directory = runtime / "verifier"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"verifier directory must be private: {directory}")
    return directory


def _artifact_records(cwd: Path, artifacts: Any) -> list[dict[str, str]]:
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("verifier artifacts must be a list of at most 100 paths")
    result: list[dict[str, str]] = []
    for item in artifacts:
        if not isinstance(item, str) or not item:
            raise ValueError("verifier artifact paths must be non-empty strings")
        path = (cwd / item).resolve(strict=True) if not Path(item).is_absolute() else Path(item).resolve(strict=True)
        try:
            relative = path.relative_to(cwd.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"verifier artifact is outside the mission working directory: {item}") from exc
        if not path.is_file():
            raise ValueError(f"verifier artifact is not a regular file: {item}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise ValueError(f"verifier artifact exceeds 50 MiB: {item}")
                digest.update(chunk)
        result.append({"path": relative.as_posix(), "sha256": digest.hexdigest(), "size": str(size)})
    return result


def _run_bounded_verifier(argv: list[str], cwd: str, timeout: float, log_path: Path) -> dict[str, Any]:
    """Drain verifier output to a capped private log without unbounded RAM/disk."""
    log_limit = 10 * 1024 * 1024
    result_limit = 1024 * 1024
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert proc.stdout is not None
    digest = hashlib.sha256()
    captured = bytearray()
    output_size = 0
    written = 0
    timed_out = False
    with log_path.open("wb") as log:
        os.chmod(log_path, 0o600)
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            readable, _, _ = select.select([proc.stdout], [], [], min(0.05, max(0, remaining)))
            if readable:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if chunk:
                    digest.update(chunk)
                    output_size += len(chunk)
                    if len(captured) <= result_limit:
                        captured.extend(chunk[: max(0, result_limit + 1 - len(captured))])
                    if written < log_limit:
                        allowed = min(len(chunk), log_limit - written)
                        log.write(chunk[:allowed])
                        written += allowed
                    continue
                break
            if proc.poll() is not None and time.monotonic() - started >= timeout:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
        if output_size > written:
            log.write(b"\n[verifier output truncated at 10 MiB]\n")
        log.flush()
        os.fsync(log.fileno())
    return {
        "exit_code": proc.wait(),
        "timed_out": timed_out,
        "output": bytes(captured),
        "output_size": output_size,
        "output_sha256": digest.hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_truncated": output_size > result_limit,
    }


def run_configured_verifier(
    store: BridgeStore,
    mission_id: str,
    task_id: int,
    *,
    runtime: Path,
    timeout_override: float | None = None,
) -> dict[str, Any]:
    """Run the mission's explicit argv verifier and bind evidence to a revision."""
    mission = store.get_mission(mission_id)
    task = store.get_task(task_id)
    if task["mission_id"] != mission_id:
        raise TransitionError("configured verification task belongs to another mission")
    if task["status"] != "VERIFYING":
        return {"status": task["status"], "reason": "task-no-longer-verifying", "revision": int(task["revision"])}
    verifier = mission["config"].get("verifier")
    if not isinstance(verifier, dict) or not isinstance(verifier.get("argv"), list) or not verifier["argv"]:
        return {"status": "VERIFYING", "reason": "no-configured-verifier"}
    argv_template = verifier["argv"]
    if not all(isinstance(arg, str) and "\x00" not in arg for arg in argv_template):
        raise ValueError("verifier argv must contain only strings without NUL bytes")
    timeout = float(timeout_override if timeout_override is not None else verifier.get("timeout_seconds", 300))
    if timeout <= 0 or timeout > 3600:
        raise ValueError("verifier timeout must be between 0 and 3600 seconds")
    directory = _private_verifier_dir(runtime)
    context_path = directory / f"{task_id}-r{task['revision']}.json"
    context = {
        "mission_id": mission_id,
        "objective": mission["objective"],
        "cwd": mission["cwd"],
        "task_id": task_id,
        "title": task["title"],
        "revision": task["revision"],
        "prompt": task["prompt"],
        "acceptance": task["acceptance"],
    }
    with context_path.open("w", encoding="utf-8") as stream:
        os.chmod(context_path, 0o600)
        json.dump(context, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    substitutions = {
        "{context}": str(context_path),
        "{mission_id}": mission_id,
        "{task_id}": str(task_id),
        "{revision}": str(task["revision"]),
        "{cwd}": str(mission["cwd"]),
    }
    argv = [next((value + arg[len(key):] for key, value in substitutions.items() if arg.startswith(key)), arg) for arg in argv_template]
    log_path = directory / f"{task_id}.log"
    try:
        run_result = _run_bounded_verifier(argv, str(mission["cwd"]), timeout, log_path)
        output = run_result["output"].decode("utf-8", errors="replace")
        exit_code = int(run_result["exit_code"])
        reason = "timeout" if run_result["timed_out"] else "output-too-large" if run_result["output_truncated"] else "nonzero-exit" if exit_code else "invalid-result"
    except OSError as exc:
        log_path.touch(mode=0o600, exist_ok=True)
        os.chmod(log_path, 0o600)
        run_result = {"exit_code": None, "timed_out": False, "output": b"", "output_size": 0, "output_sha256": hashlib.sha256(b"").hexdigest(), "elapsed_seconds": 0.0, "output_truncated": False}
        output = ""
        exit_code = None
        reason = f"launch-failed: {exc}"
    outcome: dict[str, Any] = {
        "status": "VERIFYING",
        "passed": False,
        "reason": reason,
        "exit_code": exit_code,
        "elapsed_seconds": run_result["elapsed_seconds"],
        "logs_path": str(log_path),
        "revision": int(task["revision"]),
        "output_sha256": run_result["output_sha256"],
        "output_size": run_result["output_size"],
    }
    if exit_code == 0 and not run_result["output_truncated"]:
        try:
            try:
                payload = json.loads(output) if output.strip() else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if payload.get("passed") is False:
                raise ValueError("verifier reported passed: false")
            summary = str(payload.get("summary") or output.strip() or "verifier exited successfully")[:2000]
            artifacts = _artifact_records(Path(mission["cwd"]), payload.get("artifacts", []))
            evidence_payload = {
                "mission_id": mission_id,
                "task_id": task_id,
                "revision": int(task["revision"]),
                "summary": summary,
                "artifacts": artifacts,
                "output_sha256": run_result["output_sha256"],
            }
            digest = hashlib.sha256(json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            outcome.update(status="COMPLETED", passed=True, reason="passed", summary=summary, artifacts=artifacts, digest=digest)
            try:
                store.verify_task(
                    task_id,
                    evidence=f"Configured verifier passed: {summary}",
                    source="verifier",
                    metadata={"digest": digest, "artifacts": artifacts, "logs_path": str(log_path)},
                )
            except TransitionError:
                current = store.get_task(task_id)
                outcome.update(status=current["status"], passed=False, reason="task-changed-during-verification")
            return outcome
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            outcome["reason"] = f"invalid-result: {exc}"
    store.record_verification_attempt(task_id, outcome)
    return outcome


def _runtime_directory(mission_id: str) -> Path:
    directory = default_state_dir() / "runtime" / f"mission-{mission_id}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"Bridge runtime directory must be private: {directory}")
    return directory


def _session_name(mission_id: str) -> str:
    return f"mission-{mission_id}"


def _process_start_token(process_pid: int) -> str:
    try:
        raw = Path(f"/proc/{int(process_pid)}/stat").read_text(encoding="ascii")
    except (OSError, ValueError):
        return ""
    tail = raw[raw.rfind(")") + 2:].split()
    if len(tail) <= 19 or tail[0] == "Z":
        return ""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""
    return f"{boot_id}:{tail[19]}" if boot_id else ""


def _terminate_owned_process_group(process_pid: int, process_start: str, *, timeout: float = 2.0) -> None:
    if process_pid < 2 or not process_start or _process_start_token(process_pid) != process_start:
        raise PermissionError("refusing to signal a process whose persisted identity does not match")
    try:
        if os.getpgid(process_pid) != process_pid:
            raise PermissionError("owned provider process is not a process-group leader")
        os.killpg(process_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        if _process_start_token(process_pid) != process_start:
            return
        time.sleep(0.05)
    if _process_start_token(process_pid) != process_start:
        return
    os.killpg(process_pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _process_start_token(process_pid) != process_start:
            return
        time.sleep(0.05)
    raise TimeoutError(f"owned process group {process_pid} did not exit after SIGKILL")


def _ready_file(runtime: Path) -> Path:
    return runtime / "controller-ready.json"


def _write_controller_ready(ready: Path, startup_token: str, status: str, *, tmux: bool = False) -> None:
    payload = {
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()),
        "startup_token": startup_token,
        "status": status,
        "tmux": bool(tmux),
    }
    temporary = ready.with_name(f"{ready.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ready)


def _install_frozen_tool_config(mission: dict[str, Any], runtime: Path) -> None:
    path = runtime / "config.toml"
    content = str(mission["config"].get("frozen_tool_config", ""))
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.environ["LLM_TOOLS_CONFIG"] = str(path)


def _provider_station_argv(native_tui: bool, controller_log: Path) -> list[str]:
    if native_tui:
        # Native mode respawns this retained pane only after its prior CLI exits.
        return ["/bin/sh", "-c", "exit 0"]
    # Autonomous mode presents the real headless activity stream in the
    # provider-sized pane while the separate Bridge pane remains interactive.
    return ["tail", "-n", "50", "-F", str(controller_log)]


def _process_controller_lock(mission_id: str) -> MissionLock:
    return MissionLock(default_state_dir() / "locks", mission_id)


def _open_existing(backend: TmuxTerminalBackend, runtime: Path, mission_id: str) -> TerminalHandle | None:
    socket = runtime / "tmux.sock"
    if not socket.exists():
        return None
    return backend.open_existing(socket_path=socket, session_name=_session_name(mission_id), bridge_rows=6)


def run_controller(mission_id: str, *, headless: bool = False, native_tui: bool = False, providers: str = "claude,codex,opencode", routes: str = "", startup_token: str = "") -> int:
    """Own one mission until it is completed, stopped, or requires recovery."""
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return 2
    runtime = _runtime_directory(mission_id)
    ready = _ready_file(runtime)
    try:
        ready.unlink()
    except FileNotFoundError:
        pass

    with lock, BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        # Ownership is established before recovery. An unresolved operation is
        # uncertain; its task is blocked and is never replayed automatically.
        store.reconcile_interrupted_operations(mission_id)
        for command in store.pending_commands(mission_id):
            store.finish_command(command["id"], status="REJECTED", result={"error": "controller restarted; queued command was not replayed"})
        mission = store.get_mission(mission_id)
        if mission["status"] in {"CREATED", "PAUSED", "STOPPED", "FAILED"}:
            store.set_mission_status(mission_id, "RUNNING")
        elif mission["status"] == "STOPPING":
            store.set_mission_status(mission_id, "STOPPED")
            _write_controller_ready(ready, startup_token, "STOPPED")
            return 0
        elif mission["status"] in {"COMPLETED", "CANCELLED"}:
            _write_controller_ready(ready, startup_token, mission["status"])
            return 0

        headless = bool(headless or mission["config"].get("headless", False))
        native_tui = bool(native_tui or mission["config"].get("native_tui", False))
        if headless and native_tui:
            raise ValueError("--headless and --native cannot be combined")
        _install_frozen_tool_config(mission, runtime)
        providers = str(providers or mission["config"].get("providers", "claude,codex,opencode"))
        routes = str(routes or mission["config"].get("routes", ""))
        backend: TmuxTerminalBackend | None = None
        handle: TerminalHandle | None = None
        if not headless:
            if not shutil.which("tmux"):
                store.set_mission_status(mission_id, "FAILED")
                raise RuntimeError("tmux is required for native mode; retry with --headless")
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is None:
                controller_log = runtime / "controller.log"
                controller_log.touch(mode=0o600, exist_ok=True)
                os.chmod(controller_log, 0o600)
                bridge_argv = [sys.executable, "-m", "llm_tools.bridge", "console", "--mission", mission_id]
                handle = backend.start(
                    provider_argv=_provider_station_argv(native_tui, controller_log),
                    bridge_argv=bridge_argv,
                    cwd=mission["cwd"],
                    socket_path=runtime / "tmux.sock",
                    session_name=_session_name(mission_id),
                    size=(120, 40),
                )

        _write_controller_ready(ready, startup_token, "RUNNING", tmux=bool(handle))
        try:
            hooks = BridgeMissionHooks(mission_id, handle=handle, headless=headless, native_tui=native_tui, controller_logs=runtime / "controller.log")
            hooks.bind(store)
            argv = [
                "--prompt", mission["objective"],
                "--cwd", mission["cwd"],
                "--providers", providers,
                "--max-iterations", "0",
                "--max-duration", "0",
                "--min-iteration-seconds", "0",
                "--log-dir", str(runtime / "ralph-logs"),
                "--state-file", str(runtime / "ralph-state.json"),
            ]
            if routes:
                argv.extend(["--routes", routes])
            command_template = str(mission["config"].get("command_template", ""))
            if command_template:
                argv.extend(["--command-template", command_template])
            return ralph_robin.main(argv, bridge_hooks=hooks)
        except BaseException as exc:
            store.reconcile_interrupted_operations(mission_id)
            current = store.get_mission(mission_id)
            if current["status"] in {"RUNNING", "PAUSED", "STOPPING"}:
                store.set_mission_status(mission_id, "FAILED")
            with (runtime / "controller.log").open("a", encoding="utf-8") as log:
                log.write(f"controller failure: {type(exc).__name__}: {exc}\n")
            return int(exc.code) if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
        finally:
            current = store.get_mission(mission_id)
            if current["status"] == "STOPPING":
                store.set_mission_status(mission_id, "STOPPED")


def launch_controller(
    mission_id: str,
    *,
    headless: bool = False,
    providers: str | None = None,
    routes: str | None = None,
    native_tui: bool = False,
) -> int:
    """Start a detached mission owner, then attach the terminal if interactive."""
    if native_tui and headless:
        raise ValueError("--headless and --native cannot be combined")
    if not headless and not shutil.which("tmux"):
        raise RuntimeError("tmux is required for native mode; retry with --headless")
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        if not headless and sys.stdin.isatty() and sys.stdout.isatty():
            runtime = _runtime_directory(mission_id)
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is not None:
                return backend.attach(handle)
        print(f"mission {mission_id} already has a controller; use attach or status", file=sys.stderr)
        return 2
    lock.release()

    with BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        if providers:
            providers_value = providers
        else:
            providers_value = str(mission["config"].get("providers", "claude,codex,opencode"))
        if routes:
            routes_value = routes
        else:
            routes_value = str(mission["config"].get("routes", ""))
    runtime = _runtime_directory(mission_id)
    log_path = runtime / "controller.log"
    ready = _ready_file(runtime)
    try:
        ready.unlink()
    except FileNotFoundError:
        pass
    startup_token = secrets.token_hex(16)
    env = os.environ.copy()
    env["LLM_BRIDGE_STATE_DIR"] = str(default_state_dir())
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH", ""))))
    command = [sys.executable, "-m", "llm_tools.bridge", "_controller", "--mission", mission_id, "--providers", providers_value, "--startup-token", startup_token]
    if routes_value:
        command += ["--routes", routes_value]
    if headless:
        command.append("--headless")
    if native_tui:
        command.append("--native")
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        proc = subprocess.Popen(command, cwd=mission["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.is_file():
            try:
                status = json.loads(ready.read_text(encoding="utf-8"))
                if int(status.get("pid", -1)) == proc.pid and status.get("startup_token") == startup_token and status.get("status") in {"RUNNING", "COMPLETED", "STOPPED", "CANCELLED"}:
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"mission controller exited with status {proc.returncode}; inspect {log_path}")
        time.sleep(0.05)
    else:
        raise RuntimeError(f"mission controller did not become ready; inspect {log_path}")

    if headless or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"mission {mission_id} running; controller log: {log_path}")
        return 0
    backend = TmuxTerminalBackend()
    handle = _open_existing(backend, runtime, mission_id)
    if handle is None:
        raise RuntimeError(f"mission is running but its terminal station is unavailable; inspect {log_path}")
    return backend.attach(handle)
