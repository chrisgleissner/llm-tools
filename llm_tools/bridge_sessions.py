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
import subprocess
import json
import stat
from typing import Any, Mapping
import uuid


@dataclass(frozen=True)
class NativeSessionIdentity:
    """A validated provider conversation id bound to its launch directory."""

    provider: str
    session_id: str
    cwd: str
    source: str


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


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_SESSION_SOURCES = {
    "codex": "codex:session_meta",
    "claude": "claude:system.init",
    "copilot": "copilot:session.start",
}
_CODEX_NO_DAEMON = re.compile(r"(?<![A-Za-z0-9_-])--no-daemon(?:\s|[=<]|[\]\)]|$)")
_CAPABILITIES: dict[str, _Capability] = {
    "codex": _Capability("codex", ("resume",), re.compile(r"(?im)^\s*resume(?:\s|$)"), _UUID),
    "claude": _Capability(
        "claude", ("--resume",), re.compile(r"(?<!\S)--resume(?:\s|[=<]|$)"), _UUID,
        ("--session-id",), re.compile(r"(?<!\S)--session-id(?:\s|[=<]|$)"),
    ),
    "copilot": _Capability(
        "copilot", ("--resume",), re.compile(r"(?<!\S)--resume(?:\s|[=<]|$)"), _UUID,
        ("--session-id",), re.compile(r"(?<!\S)--session-id(?:\s|[=<]|$)"),
    ),
    "kilo": _Capability("kilo", ("--session",), re.compile(r"(?<!\S)--session(?:\s|[=<]|$)"), _OPAQUE_ID),
    "opencode": _Capability("opencode", ("--session",), re.compile(r"(?<!\S)--session(?:\s|[=<]|$)"), _OPAQUE_ID),
}


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
        or identity.source != expected_source
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
    """Extract only known structured start records; ignore ordinary messages."""
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
        if record.get("type") != "system" or record.get("subtype") != "init":
            return None
        session_id = record.get("session_id") or record.get("sessionId")
        cwd = _provider_cwd(record, provider)
        return (str(session_id or ""), str(cwd or ""), "claude:system.init")

    if provider == "copilot":
        if record.get("type") not in {"session.start", "session_start"}:
            return None
        data = record.get("data") if isinstance(record.get("data"), Mapping) else record
        session_id = data.get("session_id") or data.get("sessionId")
        context = data.get("context") if isinstance(data.get("context"), Mapping) else {}
        cwd = context.get("cwd") or _provider_cwd(data, provider)
        return (str(session_id or ""), str(cwd or ""), "copilot:session.start")

    # Kilo and OpenCode session metadata formats are not yet runtime
    # evidenced by this project. Refusing to infer from filenames or a generic
    # `id` field keeps these providers UNAVAILABLE until a source is verified.
    return None


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
    return NativeSessionIdentity(normalized_provider, session_id, expected_cwd, source)


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
            with path.open("rb") as stream:
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


def _proc_stat(process_pid: int) -> tuple[str, int, int, str] | None:
    """Return state, parent pid, process group and boot-scoped start token."""
    try:
        raw = Path(f"/proc/{int(process_pid)}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) <= 19:
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if not boot_id:
            return None
        return fields[0], int(fields[1]), int(fields[2]), f"{boot_id}:{fields[19]}"
    except (OSError, ValueError, IndexError):
        return None


def _owned_processes(process_pid: int, start_token: str) -> dict[int, str] | None:
    """Bound inspection to exact live PID ancestry, including new sessions."""
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
        info = Path(f"/proc/{process_pid}/fdinfo/{fd_name}").read_text(encoding="ascii")
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
    if int(process_pid) < 2 or not start_token:
        return None
    try:
        expected_cwd = _absolute_cwd(cwd)
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve(strict=True)
        sessions_root = (codex_home / "sessions").resolve(strict=True)
        if not sessions_root.is_dir():
            return None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    owned = _owned_processes(int(process_pid), start_token)
    if not owned:
        return None
    identities: dict[tuple[str, str, str], NativeSessionIdentity] = {}
    for pid, token in owned.items():
        current = _proc_stat(pid)
        if current is None or current[0] == "Z" or current[3] != token:
            continue
        process_identities: dict[tuple[str, str, str], NativeSessionIdentity] = {}
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
                        read_fd = os.open(proc_fd, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                    except OSError:
                        read_fd = os.open(link, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                    opened = os.fstat(read_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        os.close(read_fd)
                        continue
                    try:
                        first_line = os.pread(read_fd, 65537, 0)
                    finally:
                        os.close(read_fd)
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
                        process_identities[(identity.provider, identity.session_id, identity.cwd)] = identity
                except (OSError, RuntimeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
        after = _proc_stat(pid)
        if after is None or after[3] != token:
            # Discard this process's candidates rather than attributing a
            # reused or exited PID to the session it previously held.
            if pid == int(process_pid):
                return None
            continue
        identities.update(process_identities)
    if len(identities) != 1:
        return None
    root_after = _proc_stat(int(process_pid))
    if root_after is None or root_after[3] != start_token or root_after[0] == "Z":
        return None
    return next(iter(identities.values()))


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
    if normalized == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path(home or Path.home()) / ".codex"))).expanduser()
        return next((record for record in codex_session_records(codex_home / "sessions", cwd=cwd, max_files=32, session_id=session_id)
                     if record.identity.session_id == session_id), None)
    if normalized == "claude":
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path(home or Path.home()) / ".claude"))).expanduser()
        project = str(Path(cwd).expanduser().resolve(strict=True)).replace("/", "-")
        path = config_dir / "projects" / project / f"{session_id}.jsonl"
    elif normalized == "copilot":
        state_dir = Path(os.environ.get("COPILOT_CONFIG_DIR", str(Path(home or Path.home()) / ".copilot"))).expanduser()
        path = state_dir / "session-state" / session_id / "events.jsonl"
    else:
        return None
    capability = _CAPABILITIES.get(normalized)
    if capability is None or not capability.id_pattern.fullmatch(session_id):
        return None
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return None
        with path.open("rb") as stream:
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
    try:
        result = subprocess.run(
            [resolved, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    help_text = (result.stdout + "\n" + result.stderr)[:128 * 1024]
    if option == "resume":
        pattern = capability.help_pattern
    elif option == "new-session":
        pattern = capability.new_session_pattern
    elif option == "no-daemon" and provider == "codex":
        pattern = _CODEX_NO_DAEMON
    else:
        pattern = None
    if result.returncode != 0 or pattern is None or not pattern.search(help_text):
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
    "discover_owned_codex_session",
    "new_session_argv",
    "provider_session_metadata",
    "parse_session_metadata",
    "resume_argv",
]
