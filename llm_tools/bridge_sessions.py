"""Typed, provider-scoped native session identity and resume capabilities.

This module never guesses a provider's most recent session. Callers must supply
structured metadata from the exact operation and work directory; resume argv is
only produced when the installed CLI advertises its explicit session option.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import json
import stat
from datetime import datetime
from typing import Any, Mapping, cast
import uuid

from . import bridge_providers
from .fileio import open_regular_file, read_proc_text


@dataclass(frozen=True)
class NativeSessionIdentity:
    """A validated provider conversation id bound to its launch directory."""

    provider: str
    session_id: str
    cwd: str
    source: str
    parent_session_id: str | None = None


@dataclass(frozen=True)
class NativeSessionRecord:
    """One provider-owned metadata file tied to its exact conversation ID."""

    identity: NativeSessionIdentity
    path: Path
    modified_ns: int


@dataclass(frozen=True)
class _Capability:
    executable: str
    syntax: tuple[str, ...]
    help_pattern: re.Pattern[str]
    id_pattern: re.Pattern[str]
    new_session_syntax: tuple[str, ...] = ()
    new_session_pattern: re.Pattern[str] | None = None


_UUID = bridge_providers._UUID
_OPAQUE_ID = bridge_providers._OPAQUE_ID


class _SessionSources:
    """Read-through view of each provider's accepted identity sources in the registry."""

    def get(self, provider: str, default: frozenset[str] | None = None) -> frozenset[str] | None:
        spec = bridge_providers.get(provider)
        return spec.session_sources if spec is not None and spec.session_sources else default


_SESSION_SOURCES = _SessionSources()
_CODEX_NO_DAEMON = re.compile(r"(?<![A-Za-z0-9_-])--no-daemon(?:\s|[=<]|[\]\)]|$)")
_CODEX_ACTIVITY_LINE_BYTES = 16 * 1024
_CODEX_ACTIVITY_MAX_LINES = 100_000
_CODEX_TOOL_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


class _CapabilityView:
    """Read-through view of the registry's resume facts, so a registry change reaches every caller."""

    @staticmethod
    def _of(spec: bridge_providers.ProviderSpec) -> _Capability | None:
        if not spec.resume_syntax or spec.resume_help_pattern is None:
            return None
        return _Capability(spec.executable, spec.resume_syntax, spec.resume_help_pattern, spec.id_pattern,
                           spec.new_session_syntax, spec.new_session_pattern)

    def get(self, provider: str, default: _Capability | None = None) -> _Capability | None:
        spec = bridge_providers.get(provider)
        return (self._of(spec) if spec is not None else None) or default

    def __getitem__(self, provider: str) -> _Capability:
        capability = self.get(provider)
        if capability is None:
            raise KeyError(provider)
        return capability


_CAPABILITIES = _CapabilityView()


def _absolute_cwd(cwd: str | os.PathLike[str]) -> str:
    path = Path(cwd).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("native session cwd must be an existing directory")
    return str(path)


def _identity_is_valid(identity: NativeSessionIdentity) -> bool:
    capability = _CAPABILITIES.get(identity.provider)
    expected_source = _SESSION_SOURCES.get(identity.provider)
    if (
        capability is None
        or expected_source is None
        or identity.source not in expected_source
        or not capability.id_pattern.fullmatch(identity.session_id)
    ):
        return False
    try:
        return _absolute_cwd(identity.cwd) == identity.cwd
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def _provider_cwd(record: Mapping[str, Any], provider: str) -> object:
    if provider == "codex":
        return record.get("cwd")
    return record.get("cwd") or record.get("working_directory")


def _session_fields(provider: str, record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Extract explicit provider identity fields; never inspect message bodies."""
    if provider == "codex":
        if record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return None
        session_id = payload.get("id")
        cwd = payload.get("cwd")
        return (str(session_id or ""), str(cwd or ""), "codex:session_meta")

    if provider == "claude":
        sidechain = record.get("isSidechain")
        if sidechain is not None and sidechain is not False:
            return None
        if record.get("type") in ("user", "assistant"):
            return (str(record.get("sessionId") or ""), str(record.get("cwd") or ""), "claude:transcript")
        if record.get("type") != "system" or record.get("subtype") != "init":
            return None
        session_id = record.get("session_id") or record.get("sessionId")
        cwd = _provider_cwd(record, provider)
        return (str(session_id or ""), str(cwd or ""), "claude:system.init")

    if provider == "copilot":
        if record.get("type") not in ("session.start", "session_start"):
            return None
        data = cast("Mapping[str, Any]", record.get("data") if isinstance(record.get("data"), Mapping) else record)
        session_id = data.get("session_id") or data.get("sessionId")
        context = cast("Mapping[str, Any]", data.get("context") if isinstance(data.get("context"), Mapping) else {})
        cwd = context.get("cwd") or _provider_cwd(data, provider)
        return (str(session_id or ""), str(cwd or ""), "copilot:session.start")

    # Kilo and OpenCode session metadata formats are not yet runtime
    # evidenced by this project. Refusing to infer from filenames or a generic
    # `id` field keeps these providers UNAVAILABLE until a source is verified.
    return None


DB_SESSION_PROVIDERS = ("kilo", "opencode")
_DB_MAX_CANDIDATES = 50


def session_db_path(provider: str, *, home: str | os.PathLike[str] | None = None) -> Path | None:
    """Path of the SQLite session store of a Kilo or OpenCode install, or ``None`` for another provider."""
    spec = bridge_providers.get(provider)
    if spec is None or not spec.session_store:
        return None
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path(home or Path.home()) / ".local" / "share")
    return Path(data_home).expanduser() / spec.session_store


def _db_session_rows(provider: str, where: str, params: tuple[object, ...], *, home: str | os.PathLike[str] | None) -> list[sqlite3.Row] | None:
    """Read identity columns only (id, directory, parent, times) from the provider's session table."""
    path = session_db_path(provider, home=home)
    if path is None:
        return None
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return None
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except (OSError, sqlite3.Error):
        return None
    try:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT id, directory, parent_id, time_created, time_updated FROM session WHERE " + where + " LIMIT ?",
            (*params, _DB_MAX_CANDIDATES + 1),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _db_row_identity(provider: str, row: sqlite3.Row, cwd: str) -> NativeSessionIdentity | None:
    try:
        if _absolute_cwd(str(row["directory"])) != cwd:
            return None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    spec = bridge_providers.get(provider)
    if spec is None or not spec.id_pattern.fullmatch(str(row["id"])):
        return None
    return NativeSessionIdentity(provider, str(row["id"]), cwd, f"{provider}:session.db")


def discover_new_db_session(
    provider: str,
    *,
    cwd: str | os.PathLike[str],
    started_ns: int,
    home: str | os.PathLike[str] | None = None,
) -> NativeSessionIdentity | None:
    """The one session a Kilo/OpenCode run created after ``started_ns`` in ``cwd``, else ``None``.

    The store is searched for parentless, unarchived sessions created at or after the launch time
    in exactly this directory. Zero matches or more than one (another run in the same directory,
    a restored session) is ambiguous and yields ``None``: the next run then starts fresh. Never
    "the newest session".
    """
    if provider not in DB_SESSION_PROVIDERS or started_ns <= 0:
        return None
    try:
        expected_cwd = _absolute_cwd(cwd)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    rows = _db_session_rows(provider, "parent_id IS NULL AND time_archived IS NULL AND time_created >= ?", (started_ns // 1_000_000,), home=home)
    if rows is None or len(rows) > _DB_MAX_CANDIDATES:
        return None
    identities = [identity for row in rows if (identity := _db_row_identity(provider, row, expected_cwd)) is not None]
    return identities[0] if len(identities) == 1 else None


def _db_session_record(provider: str, session_id: str, cwd: str, *, home: str | os.PathLike[str] | None) -> NativeSessionRecord | None:
    rows = _db_session_rows(provider, "id = ?", (session_id,), home=home)
    if not rows or len(rows) != 1:
        return None
    identity = _db_row_identity(provider, rows[0], cwd)
    path = session_db_path(provider, home=home)
    if identity is None or path is None:
        return None
    return NativeSessionRecord(identity, path, int(rows[0]["time_updated"]) * 1_000_000)


def parse_session_metadata(
    provider: str,
    record: Mapping[str, Any],
    *,
    cwd: str | os.PathLike[str],
) -> NativeSessionIdentity | None:
    """Parse one explicit provider session-start metadata record.

    Returns ``None`` for unrelated/malformed metadata or a working-directory
    mismatch. The caller should consider native restoration unavailable unless
    exactly one identity can be tied to the operation being restored.
    """
    normalized_provider = provider.strip().lower()
    capability = _CAPABILITIES.get(normalized_provider)
    if capability is None:
        return None
    fields = _session_fields(normalized_provider, record)
    if fields is None:
        return None
    session_id, metadata_cwd, source = fields
    if not capability.id_pattern.fullmatch(session_id):
        return None
    try:
        expected_cwd = _absolute_cwd(cwd)
        observed_cwd = _absolute_cwd(metadata_cwd)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    if observed_cwd != expected_cwd:
        return None
    parent_id: str | None = None
    if normalized_provider == "codex":
        payload = record.get("payload")
        candidate = payload.get("parentThreadId") or payload.get("parent_thread_id") if isinstance(payload, Mapping) else None
        if isinstance(candidate, str) and candidate:
            parent_id = candidate
    return NativeSessionIdentity(normalized_provider, session_id, expected_cwd, source, parent_id)


def codex_session_records(
    sessions_dir: str | os.PathLike[str],
    *,
    cwd: str | os.PathLike[str],
    max_files: int = 4096,
    session_id: str | None = None,
    modified_after_ns: int | None = None,
    baseline: Mapping[str, int] | None = None,
) -> list[NativeSessionRecord]:
    """Read Codex's structured first-line metadata from a bounded file scan.

    Codex session transcripts are JSONL files whose first record is the
    structured ``session_meta`` object. Only that bounded first line is read;
    transcript text is never inspected. The scan skips symlinks and ignores
    files outside the expected directory tree.
    """
    root = Path(sessions_dir).expanduser()
    try:
        root_stat = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
            return []
        expected_cwd = _absolute_cwd(cwd)
    except (OSError, RuntimeError, ValueError, TypeError):
        return []
    paths: list[Path] = []
    visited = 0
    for directory, child_dirs, filenames in os.walk(root, followlinks=False):
        child_dirs.sort(reverse=True)
        filenames.sort(reverse=True)
        visited += 1
        if visited > max_files:
            break
        for filename in filenames:
            if not filename.endswith(".jsonl") or (session_id is not None and session_id not in filename):
                continue
            paths.append(Path(directory) / filename)
            if len(paths) >= max_files:
                break
        if len(paths) >= max_files:
            break
    records: list[NativeSessionRecord] = []
    for path in paths:
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            previous_mtime = baseline.get(str(path), -1) if baseline is not None else -1
            if modified_after_ns is not None and info.st_mtime_ns < modified_after_ns:
                continue
            if baseline is not None and info.st_mtime_ns <= previous_mtime:
                continue
            with open_regular_file(path, follow_symlinks=False) as stream:
                first_line = stream.readline(65537)
            if len(first_line) > 65536 or not first_line.strip():
                continue
            raw = json.loads(first_line)
            if not isinstance(raw, Mapping):
                continue
            identity = parse_session_metadata("codex", raw, cwd=expected_cwd)
            if identity is not None:
                records.append(NativeSessionRecord(identity, path, info.st_mtime_ns))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
    return records


def codex_session_records_by_id(
    sessions_dir: str | os.PathLike[str],
    session_ids: set[str],
    *,
    max_files: int = 4096,
) -> dict[str, NativeSessionRecord]:
    """Find exact Codex session headers without assuming child workdirs match the root.

    Only validated first-line metadata is read. A session ID with multiple matching files is
    omitted as ambiguous, and callers still need to verify its explicit parent relationship.
    """
    wanted = {value for value in session_ids if _UUID.fullmatch(value)}
    if not wanted:
        return {}
    root = Path(sessions_dir).expanduser()
    try:
        root_stat = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
            return {}
    except OSError:
        return {}
    found: dict[str, NativeSessionRecord | None] = {}
    visited = 0
    considered = 0
    for directory, child_dirs, filenames in os.walk(root, followlinks=False):
        child_dirs.sort(reverse=True)
        filenames.sort(reverse=True)
        visited += 1
        if visited > max_files:
            break
        for filename in filenames:
            session_id = next((candidate for candidate in wanted if candidate in filename), None)
            if session_id is None or not filename.endswith(".jsonl"):
                continue
            considered += 1
            if considered > max_files:
                break
            path = Path(directory) / filename
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                with open_regular_file(path, follow_symlinks=False) as stream:
                    first_line = stream.readline(65537)
                if len(first_line) > 65536 or not first_line.strip():
                    continue
                raw = json.loads(first_line)
                if not isinstance(raw, Mapping):
                    continue
                fields = _session_fields("codex", raw)
                if fields is None or fields[0] != session_id:
                    continue
                identity = parse_session_metadata("codex", raw, cwd=fields[1])
                if identity is None:
                    continue
                record = NativeSessionRecord(identity, path, info.st_mtime_ns)
                found[session_id] = record if session_id not in found else None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
        if considered > max_files:
            break
    return {key: record for key, record in found.items() if record is not None}


def codex_session_activity_tail(
    path: str | os.PathLike[str],
    *,
    session_id: str,
    parent_id: str,
    sessions_root: str | os.PathLike[str],
    max_bytes: int = 4 * 1024 * 1024,
) -> dict[str, str | None] | None:
    """Stream safe lifecycle and tool labels from one exact parent-bound Codex session.

    The session header is identity checked and the read has byte, line and per-record limits.
    Prompt text, tool arguments, tool results, messages and reasoning are never retained.
    """
    target = Path(path)
    try:
        root = Path(sessions_root).resolve(strict=True)
        if not target.resolve(strict=True).is_relative_to(root):
            return None
        info = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or target.is_symlink():
            return None
        with open_regular_file(target, follow_symlinks=False) as stream:
            opened = os.fstat(stream.fileno())
            if not os.path.samestat(info, opened):
                return None
            if opened.st_size > max_bytes:
                return None
            header = stream.readline(65537)
            if len(header) > 65536:
                return None
            raw_header = json.loads(header)
            payload = raw_header.get("payload") if isinstance(raw_header, dict) else None
            cwd = payload.get("cwd") if isinstance(payload, Mapping) else None
            identity = parse_session_metadata("codex", raw_header, cwd=cwd) if isinstance(raw_header, dict) and isinstance(cwd, str) else None
            if identity is None or identity.session_id != session_id or identity.parent_session_id != parent_id:
                return None
            latest_activity: tuple[tuple[float, int], dict[str, str | None]] | None = None
            latest_lifecycle: tuple[tuple[float, int], dict[str, str | None]] | None = None
            consumed = len(header)
            for sequence in range(_CODEX_ACTIVITY_MAX_LINES):
                remaining = opened.st_size - consumed
                if remaining <= 0:
                    break
                line = stream.readline(min(_CODEX_ACTIVITY_LINE_BYTES + 1, remaining))
                if not line:
                    break
                consumed += len(line)
                if len(line) > _CODEX_ACTIVITY_LINE_BYTES or not line.endswith(b"\n"):
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                timestamp = event.get("timestamp")
                timestamp_text = timestamp if isinstance(timestamp, str) else None
                timestamp_epoch: float | None = None
                if timestamp_text:
                    try:
                        timestamp_epoch = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00")).timestamp()
                    except (ValueError, OverflowError):
                        pass
                order_key = (timestamp_epoch if timestamp_epoch is not None else float("-inf"), sequence)
                activity: str | None = None
                lifecycle: str | None = None
                if event_type == "event_msg":
                    lifecycle_type = payload.get("type")
                    if lifecycle_type == "task_started":
                        lifecycle = "task_started"
                        activity = "last provider event: task started"
                    elif lifecycle_type == "task_complete":
                        lifecycle = "task_complete"
                        activity = "last provider event: task completed"
                    elif lifecycle_type == "turn_aborted":
                        lifecycle = "turn_aborted"
                        activity = "last provider event: turn aborted"
                elif event_type == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                    name = payload.get("name")
                    if isinstance(name, str) and _CODEX_TOOL_LABEL.fullmatch(name):
                        activity = f"last observed tool: {name}"
                if activity is not None:
                    value = {"activity": activity, "timestamp": timestamp_text}
                    if latest_activity is None or order_key > latest_activity[0]:
                        latest_activity = (order_key, value)
                if lifecycle is not None:
                    value = {"lifecycle": lifecycle, "lifecycle_timestamp": timestamp_text}
                    if latest_lifecycle is None or order_key > latest_lifecycle[0]:
                        latest_lifecycle = (order_key, value)
            else:
                return None
            if latest_activity is None and latest_lifecycle is None:
                return None
            result: dict[str, str | None] = {
                "activity": None,
                "timestamp": None,
                "lifecycle": None,
                "lifecycle_timestamp": None,
            }
            if latest_activity is not None:
                result.update(latest_activity[1])
            if latest_lifecycle is not None:
                result.update(latest_lifecycle[1])
            return result
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


_BOOT_ID = ""


def boot_id() -> str:
    """This boot's ID, read once: it cannot change while the machine is up."""
    global _BOOT_ID
    if not _BOOT_ID:
        _BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    return _BOOT_ID


def child_pids(process_pid: int) -> list[int] | None:
    """Direct children of a process from ``/proc/<pid>/task/*/children``, or ``None`` when unavailable.

    Following children touches only the processes in one tree, where scanning
    ``/proc`` reads every process on the machine. The files exist when the
    kernel is built with ``CONFIG_PROC_CHILDREN``; callers fall back to a scan
    when this returns ``None``.
    """
    children: list[int] = []
    task_dir = f"/proc/{int(process_pid)}/task"
    try:
        threads = os.listdir(task_dir)
    except FileNotFoundError:
        return []  # the process is gone: it has no children
    except OSError:
        return None
    for thread in threads:
        try:
            text = Path(f"{task_dir}/{thread}/children").read_text(encoding="ascii")
            children.extend(int(word) for word in text.split())
        except FileNotFoundError:
            if Path(f"{task_dir}/{thread}").exists():
                return None  # the thread is alive but the kernel has no child lists
        except (OSError, ValueError):
            return None
    return children


def _proc_stat(process_pid: int) -> tuple[str, int, int, str] | None:
    """Return state, parent pid, process group and boot-scoped start token."""
    try:
        raw = read_proc_text(f"/proc/{int(process_pid)}/stat")
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) <= 19:
            return None
        boot = boot_id()
        if not boot:
            return None
        return fields[0], int(fields[1]), int(fields[2]), f"{boot}:{fields[19]}"
    except (OSError, ValueError, IndexError):
        return None


def _owned_processes_by_children(process_pid: int, start_token: str) -> dict[int, str] | None:
    """The process and its descendants found by following child lists, or ``None`` to scan ``/proc`` instead.

    An empty result means the root is gone or no longer matches ``start_token``.
    """
    root = _proc_stat(process_pid)
    if root is None:
        return None
    if root[3] != start_token:
        return {}
    if child_pids(process_pid) is None:
        return None
    owned: dict[int, str] = {process_pid: start_token}
    pending = [process_pid]
    while pending:
        parent = pending.pop()
        children = child_pids(parent)
        if children is None:
            return None
        for child in children:
            if child in owned:
                continue
            parsed = _proc_stat(child)
            if parsed is not None:
                owned[child] = parsed[3]
                pending.append(child)
    return owned


def _owned_processes(process_pid: int, start_token: str) -> dict[int, str] | None:
    """Bound inspection to exact live PID ancestry, including new sessions."""
    tree = _owned_processes_by_children(process_pid, start_token)
    if tree is not None:
        return tree or None
    try:
        entries = os.scandir("/proc")
    except OSError:
        return None
    records: dict[int, tuple[str, int]] = {}
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            parsed = _proc_stat(int(entry.name))
            if parsed is not None:
                state, parent, _group, token = parsed
                records[int(entry.name)] = (token, parent)
    root = records.get(process_pid)
    if root is None or root[0] != start_token:
        return None
    children: dict[int, list[int]] = {}
    for pid, (_token, parent) in records.items():
        children.setdefault(parent, []).append(pid)
    owned: dict[int, str] = {process_pid: start_token}
    pending = [process_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in owned:
                owned[child] = records[child][0]
                pending.append(child)
    return owned


def _fd_is_writable(process_pid: int, fd_name: str) -> bool:
    try:
        info = read_proc_text(f"/proc/{process_pid}/fdinfo/{fd_name}")
    except OSError:
        return False
    for line in info.splitlines():
        if line.startswith("flags:"):
            try:
                flags = int(line.partition(":")[2].strip(), 8)
            except ValueError:
                return False
            return (flags & os.O_ACCMODE) != os.O_RDONLY
    return False


def discover_owned_codex_session(
    process_pid: int,
    start_token: str,
    *,
    cwd: str | os.PathLike[str],
) -> NativeSessionIdentity | None:
    """Discover one Codex session through writable FDs in the owned PID tree.

    This does not search for recent/global transcripts. The provider PID must
    still match its persisted boot-scoped start token, and exactly one unique
    `session_meta` ID+CWD must be open writable by that PID or a live child.
    """
    candidates = _owned_codex_session_candidates(process_pid, start_token, cwd=cwd)
    if len(candidates) != 1:
        return None
    return candidates[0][0]


def discover_owned_codex_root_session(
    process_pid: int,
    start_token: str,
    *,
    cwd: str | os.PathLike[str],
    started_after: float,
) -> NativeSessionIdentity | None:
    """Find the unique parentless Codex session opened by this exact live process after launch.

    Codex keeps writable JSONL handles for spawned children in the same process tree. The ordinary
    discovery helper remains conservative when several IDs are open; Bridge's map can identify its
    root by the provider's explicit parentThreadId metadata and the operation start boundary.
    """
    candidates = _owned_codex_session_candidates(process_pid, start_token, cwd=cwd)
    roots = [identity for identity, modified_at in candidates
             if identity.parent_session_id is None and modified_at >= float(started_after)]
    return roots[0] if len(roots) == 1 else None


def _owned_codex_session_candidates(
    process_pid: int,
    start_token: str,
    *,
    cwd: str | os.PathLike[str],
) -> list[tuple[NativeSessionIdentity, float]]:
    """Read bounded first-line metadata through writable FDs in a verified process tree."""
    if int(process_pid) < 2 or not start_token:
        return []
    try:
        expected_cwd = _absolute_cwd(cwd)
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve(strict=True)
        sessions_root = (codex_home / "sessions").resolve(strict=True)
        if not sessions_root.is_dir():
            return []
    except (OSError, RuntimeError, ValueError, TypeError):
        return []
    owned = _owned_processes(int(process_pid), start_token)
    if not owned:
        return []
    identities: dict[tuple[str, str, str], tuple[NativeSessionIdentity, float]] = {}
    for pid, token in owned.items():
        current = _proc_stat(pid)
        if current is None or current[0] == "Z" or current[3] != token:
            continue
        process_identities: dict[tuple[str, str, str], tuple[NativeSessionIdentity, float]] = {}
        try:
            fd_entries = os.scandir(f"/proc/{pid}/fd")
        except OSError:
            continue
        with fd_entries:
            for fd_entry in fd_entries:
                if not fd_entry.name.isdigit() or not _fd_is_writable(pid, fd_entry.name):
                    continue
                proc_fd = Path(fd_entry.path)
                try:
                    link = os.readlink(fd_entry.path)
                    if not link.startswith("/") or link.endswith(" (deleted)"):
                        continue
                    path = Path(link).resolve(strict=True)
                    if path.suffix != ".jsonl" or not path.is_relative_to(sessions_root):
                        continue
                    info = proc_fd.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    read_fd: int | None
                    try:
                        # Opening the proc descriptor binds reads to the exact
                        # live FD. For write-only provider FDs, fall back to
                        # the link target only after verifying device/inode.
                        read_fd = os.open(proc_fd, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
                    except OSError:
                        read_fd = os.open(link, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
                    opened = os.fstat(read_fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        os.close(read_fd)
                        continue
                    try:
                        prefix = os.pread(read_fd, 65537, 0)
                    finally:
                        os.close(read_fd)
                    # Later JSONL records may already fill this bounded read.
                    # Only the metadata header is parsed, irrespective of the
                    # transcript's current size or the provider's write pace.
                    first_line = prefix.partition(b"\n")[0]
                    if len(first_line) > 65536:
                        continue
                    record = json.loads(first_line)
                    if not isinstance(record, Mapping):
                        continue
                    identity = parse_session_metadata("codex", record, cwd=expected_cwd)
                    final_info = proc_fd.stat()
                    if (
                        identity is not None
                        and os.readlink(fd_entry.path) == link
                        and _fd_is_writable(pid, fd_entry.name)
                        and (final_info.st_dev, final_info.st_ino) == (info.st_dev, info.st_ino)
                    ):
                        process_identities[(identity.provider, identity.session_id, identity.cwd)] = (identity, info.st_mtime)
                except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
        after = _proc_stat(pid)
        if after is None or after[3] != token:
            # Discard this process's candidates rather than attributing a
            # reused or exited PID to the session it previously held.
            if pid == int(process_pid):
                return []
            continue
        identities.update(process_identities)
    root_after = _proc_stat(int(process_pid))
    if root_after is None or root_after[3] != start_token or root_after[0] == "Z":
        return []
    return list(identities.values())


def provider_session_metadata(
    provider: str,
    session_id: str,
    *,
    cwd: str | os.PathLike[str],
    home: str | os.PathLike[str] | None = None,
) -> NativeSessionRecord | None:
    """Read metadata from the provider-owned file for one explicit session ID.

    Claude and Copilot use ID-addressed structured transcript locations. No
    session listing or most-recent lookup is performed. Codex has date-sharded
    rollout names, so its bounded first-line metadata scanner is filtered to
    the caller's exact session ID and work directory.
    """
    normalized = provider.strip().lower()
    capability = _CAPABILITIES.get(normalized)
    if capability is None or not capability.id_pattern.fullmatch(session_id):
        return None
    try:
        canonical_cwd = _absolute_cwd(cwd)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    if normalized in DB_SESSION_PROVIDERS:
        return _db_session_record(normalized, session_id, canonical_cwd, home=home)
    if normalized == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path(home or Path.home()) / ".codex"))).expanduser()
        return next((record for record in codex_session_records(codex_home / "sessions", cwd=cwd, max_files=4096, session_id=session_id)
                     if record.identity.session_id == session_id), None)
    if normalized == "claude":
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path(home or Path.home()) / ".claude"))).expanduser()
        project = re.sub(r"[^a-zA-Z0-9]", "-", canonical_cwd)
        path = config_dir / "projects" / project / f"{session_id}.jsonl"
    elif normalized == "copilot":
        state_dir = Path(os.environ.get("COPILOT_CONFIG_DIR", str(Path(home or Path.home()) / ".copilot"))).expanduser()
        path = state_dir / "session-state" / session_id / "events.jsonl"
    else:
        return None
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return None
        with open_regular_file(path, follow_symlinks=False) as stream:
            for _ in range(256):
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    return None
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, Mapping):
                    identity = parse_session_metadata(normalized, record, cwd=cwd)
                    if identity is not None and identity.session_id == session_id:
                        return NativeSessionRecord(identity, path, info.st_mtime_ns)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    return None


def pending_operator_questions(
    provider: str,
    session_id: str,
    *,
    cwd: str | os.PathLike[str],
    max_bytes: int = 2 * 1024 * 1024,
) -> list[dict[str, object]]:
    """Return bounded unanswered structured questions from one exact provider session.

    Transcript identity is verified by ``provider_session_metadata`` first. Only
    the currently unanswered question input fields are returned; tool results,
    surrounding conversation and malformed/oversized records are discarded.
    """
    record = provider_session_metadata(provider, session_id, cwd=cwd)
    if record is None:
        return []
    path = record.path
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return []
        with open_regular_file(path, follow_symlinks=False) as stream:
            opened = os.fstat(stream.fileno())
            if not os.path.samestat(info, opened) or max_bytes <= 0:
                return []
            start = max(0, opened.st_size - max_bytes)
            if start:
                stream.seek(start)
                stream.readline()  # discard the possibly partial first record
            pending: dict[str, list[dict[str, object]]] = {}
            for _ in range(4096):
                remaining = opened.st_size - stream.tell()
                if remaining <= 0:
                    break
                line = stream.readline(min(65537, remaining))
                if not line:
                    break
                if len(line) > 65536 or not line.endswith(b"\n"):
                    continue
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                call_id: object = None
                question_input: object = None
                if provider == "codex":
                    payload = event.get("payload")
                    if not isinstance(payload, Mapping):
                        continue
                    if event.get("type") != "response_item":
                        continue
                    if payload.get("type") == "function_call" and payload.get("name") == "request_user_input":
                        call_id = payload.get("call_id")
                        args = payload.get("arguments")
                        if isinstance(args, str) and len(args.encode("utf-8", "replace")) <= 8192:
                            try:
                                question_input = json.loads(args)
                            except json.JSONDecodeError:
                                continue
                        else:
                            question_input = args
                    elif payload.get("type") == "function_call_output":
                        call_id = payload.get("call_id")
                        if isinstance(call_id, str):
                            pending.pop(call_id, None)
                        continue
                elif provider == "claude":
                    message = event.get("message")
                    if not isinstance(message, Mapping):
                        continue
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content[:32]:
                        if not isinstance(part, Mapping):
                            continue
                        if part.get("type") == "tool_use" and part.get("name") == "AskUserQuestion":
                            call_id = part.get("id")
                            question_input = part.get("input")
                            break
                        if part.get("type") == "tool_result" and isinstance(part.get("tool_use_id"), str):
                            pending.pop(str(part["tool_use_id"]), None)
                else:
                    return []
                if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
                    continue
                questions = _question_fields(question_input)
                if questions:
                    pending[call_id] = questions
            if not pending:
                return []
            return list(pending.values())[-1]
    except (OSError, RuntimeError, ValueError, TypeError):
        return []


def _question_fields(value: object) -> list[dict[str, object]]:
    """Allowlist provider question fields and impose per-value and aggregate bounds."""
    if not isinstance(value, Mapping) or not isinstance(value.get("questions"), list):
        return []
    result: list[dict[str, object]] = []
    total = 0
    if len(value["questions"]) > 5:
        return []
    for question in value["questions"]:
        if not isinstance(question, Mapping):
            return []
        prompt = question.get("question")
        options = question.get("options")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 800 or not isinstance(options, list) or len(options) > 8:
            return []
        cleaned_options: list[dict[str, str]] = []
        for option in options[:8]:
            if not isinstance(option, Mapping):
                return []
            label, description = option.get("label"), option.get("description", "")
            if not isinstance(label, str) or not label.strip() or len(label) > 160 or not isinstance(description, str) or len(description) > 240:
                return []
            cleaned_options.append({"label": label, "description": description})
            total += len(label) + len(description)
        header = question.get("header", "")
        if not isinstance(header, str) or len(header) > 80 or total + len(prompt) + len(header) > 4000:
            return []
        total += len(prompt) + len(header)
        result.append({"header": header, "question": prompt, "options": cleaned_options})
    return result if result else []


HELP_PROBE_TIMEOUT_SECONDS = 10
# (resolved path, mtime_ns, size) -> (return code, help text). A completed probe is remembered for
# the life of the process, so the CLI's --help runs at most once per controller and installed version.
_HELP_CACHE: dict[tuple[str, int, int], tuple[int, str]] = {}


def _probe_help(resolved: str) -> tuple[int, str] | None:
    try:
        info = os.stat(resolved)
        key: tuple[str, int, int] | None = (resolved, info.st_mtime_ns, info.st_size)
    except OSError:
        key = None
    if key is not None and key in _HELP_CACHE:
        return _HELP_CACHE[key]
    try:
        result = subprocess.run([resolved, "--help"], check=False, capture_output=True, text=True,
                                timeout=HELP_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None  # a timeout or launch failure is not cached: the next call may succeed
    probed = (result.returncode, (result.stdout + "\n" + result.stderr)[:128 * 1024])
    if key is not None:
        _HELP_CACHE[key] = probed
    return probed


def _installed_help(
    provider: str,
    executable: str | None,
    *,
    option: str = "resume",
) -> tuple[str, str] | None:
    capability = _CAPABILITIES.get(provider)
    if capability is None:
        return None
    binary = executable or capability.executable
    resolved = shutil.which(binary)
    if resolved is None:
        return None
    probed = _probe_help(resolved)
    if probed is None:
        return None
    returncode, help_text = probed
    pattern: re.Pattern[str] | None
    if option == "resume":
        pattern = capability.help_pattern
    elif option == "new-session":
        pattern = capability.new_session_pattern
    elif option == "no-daemon" and provider == "codex":
        pattern = _CODEX_NO_DAEMON
    else:
        pattern = None
    if returncode != 0 or pattern is None or not pattern.search(help_text):
        return None
    return resolved, help_text


def resume_argv(
    identity: NativeSessionIdentity,
    *,
    executable: str | None = None,
    help_text: str | None = None,
) -> list[str] | None:
    """Return explicit resume argv only for a validated ID and advertised CLI.

    The returned argv selects the session; the caller appends the current task
    prompt using its normal provider-specific launch builder. No shell parsing
    or global ``--last`` behavior is used.
    """
    capability = _CAPABILITIES.get(identity.provider)
    if capability is None or not _identity_is_valid(identity):
        return None
    binary = executable or capability.executable
    if help_text is None:
        detected = _installed_help(identity.provider, executable)
        if detected is None:
            return None
        binary, help_text = detected
    if not capability.help_pattern.search(help_text):
        return None
    return [binary, *capability.syntax, identity.session_id]


def new_session_argv(
    provider: str,
    argv: list[str],
    *,
    cwd: str | os.PathLike[str],
    executable: str | None = None,
    help_text: str | None = None,
    session_id: str | None = None,
) -> tuple[list[str], str] | None:
    """Bind a new Claude/Copilot invocation to an explicit session UUID.

    Only installed CLIs whose help advertises ``--session-id`` are modified.
    Codex creates its ID internally, so this helper does not try to capture or
    bind a new Codex session. A caller may confirm metadata for an exact,
    previously Bridge-owned ID; new-session discovery by cwd or recency is not
    a safe ownership signal. Claude/Copilot startup metadata is checked against
    the planned UUID and cwd before recording `NATIVE_CREATED`.
    """
    normalized_provider = provider.strip().lower()
    capability = _CAPABILITIES.get(normalized_provider)
    if capability is None or not capability.new_session_syntax or not argv:
        return None
    resolved = shutil.which(executable or capability.executable)
    if resolved is None or Path(argv[0]).name != capability.executable:
        return None
    if any(flag in argv for flag in ("--session-id", "--resume", "--session")):
        return None
    try:
        _absolute_cwd(cwd)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    if help_text is None:
        detected = _installed_help(normalized_provider, executable, option="new-session")
        if detected is None:
            return None
        resolved, help_text = detected
    if capability.new_session_pattern is None or not capability.new_session_pattern.search(help_text):
        return None
    exact_id = session_id or str(uuid.uuid4())
    if not capability.id_pattern.fullmatch(exact_id):
        return None
    return ([resolved, *capability.new_session_syntax, exact_id, *argv[1:]], exact_id)


def codex_owned_argv(
    argv: list[str],
    *,
    executable: str | None = None,
    help_text: str | None = None,
) -> list[str] | None:
    """Select Codex's directly owned terminal mode when explicitly supported."""
    if not argv or Path(argv[0]).name != "codex":
        return None
    resolved = shutil.which(executable or argv[0])
    if resolved is None:
        return None
    if help_text is None:
        detected = _installed_help("codex", resolved, option="no-daemon")
        if detected is None:
            return None
        resolved, help_text = detected
    if not _CODEX_NO_DAEMON.search(help_text):
        return None
    if "--no-daemon" in argv:
        return list(argv)
    return [resolved, "--no-daemon", *argv[1:]]


def confirm_restored(
    expected: NativeSessionIdentity,
    observed: NativeSessionIdentity | None,
) -> bool:
    """Confirm restoration only when provider, exact id and cwd all match."""
    if observed is None or not _identity_is_valid(expected) or not _identity_is_valid(observed):
        return False
    return (
        expected.provider == observed.provider
        and expected.session_id == observed.session_id
        and expected.cwd == observed.cwd
    )


__all__ = [
    "NativeSessionIdentity",
    "NativeSessionRecord",
    "codex_session_records",
    "codex_owned_argv",
    "confirm_restored",
    "discover_new_db_session",
    "discover_owned_codex_session",
    "session_db_path",
    "discover_owned_codex_root_session",
    "new_session_argv",
    "provider_session_metadata",
    "parse_session_metadata",
    "resume_argv",
]
