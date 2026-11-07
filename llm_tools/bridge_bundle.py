"""Redacted diagnostic bundle for bug reports (``llm-bridge doctor --bundle``).

The bundle is a ``.tar.gz`` written with mode 0600 that holds only what a
maintainer needs to diagnose a problem: versions, the ``doctor`` report, the
llm-tools configuration, worker and remote metadata, a summary of the mission
database, controller liveness, and the tail of recent controller and provider
logs.  Every text member passes through :func:`redact`, which removes tokens
and key-looking strings, values of secret-named keys, e-mail addresses,
private and LAN addresses, local host names and the home directory.

Mission names, prompts and task text are never read into the bundle.  They are
also removed from log tails by literal match: a provider that echoes the
prompt into its output does not leak it, as long as the echo is verbatim
(a prompt wrapped by a terminal at a different width is only removed line by
line for lines of at least 12 characters, and word by word for words of at least 16).  Redaction is a safety net, not a
proof: read the bundle before posting it publicly.

Standard library only.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import platform
import re
import sqlite3
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .bridge_store import BridgeStore, controller_running

REDACTED = "[REDACTED]"
REDACTED_PROMPT = "[PROMPT REDACTED]"
REDACTED_ADDRESS = "[ADDRESS REDACTED]"
REDACTED_HOST = "[HOST REDACTED]"
REDACTED_EMAIL = "[EMAIL REDACTED]"
BUNDLE_FORMAT = 1
NOTE = ("Removed: tokens and keys, secret-named values, e-mail addresses, private/LAN addresses, local host names, "
        "prompts, task text and mission names; the home folder is shown as ~. Read the bundle before sharing it.")

LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 64 * 1024
MAX_LOG_FILES = 24
RUN_LOGS_PER_MISSION = 4
OPERATION_LOGS = 4
TEXT_FILE_LIMIT = 256 * 1024
_MIN_LITERAL = 12  # shortest prompt line removed on its own
_MIN_WORD_LITERAL = 16  # shortest single word (identifier, path, URL, marker) removed on its own

_PLAIN_VALUE = re.compile(r"true|false|null|none|\d{1,6}", re.IGNORECASE)  # a flag or count under a secret-named key is not a secret
_SECRET_WORDS = r"(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential|private[_-]?key)"
_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL), REDACTED),
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), lambda m: f"{m.group(1)} {REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    # `name = "value"`, `"name": "value"`, `NAME=value`, `Authorization: Bearer value` for secret-named keys.
    (re.compile(
        rf"""(?P<key>["']?[\w.-]*{_SECRET_WORDS}[\w.-]*["']?)(?P<sep>\s*[=:]\s*)"""
        r"""(?P<val>"[^"\n]*"|'[^'\n]*'|(?:Bearer|Basic|Token)\s+(?:\[[A-Z ]+\]|[^\s,}\]]+)|\[[A-Z ]+\]|[^\s,}\]]+)""", re.IGNORECASE),
     lambda m: m.group(0) if _PLAIN_VALUE.fullmatch(m.group('val')) else f"{m.group('key')}{m.group('sep')}{REDACTED}"),
    # `--token value`, `--api-key=value`
    (re.compile(rf"(?P<flag>--[\w-]*{_SECRET_WORDS}[\w-]*)(?P<sep>=|\s+)(?P<val>\[[A-Z ]+\]|[^\s-][^\s]*)", re.IGNORECASE),
     lambda m: f"{m.group('flag')}{m.group('sep')}{REDACTED}"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), REDACTED_EMAIL),
)
_HOSTNAME = re.compile(r"(?<![\w.-])[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*\.(?:home\.arpa|local|lan|internal)(?![\w-]|\.\w)", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:.])(?=[0-9A-Fa-f:.]*:)[0-9A-Fa-f:.]{2,45}(?:%[0-9A-Za-z._-]+)?(?![0-9A-Za-z:])")
_HEX_SECRET = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{40,}(?![0-9A-Za-z])")  # not the 32-digit mission ids
_BASE64_SECRET = re.compile(r"(?<![\w/+=-])[A-Za-z0-9_+-]{32,}={0,2}(?![\w/+-])")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class BundleError(ValueError):
    """The bundle could not be written; nothing was left behind."""


def _private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_unspecified:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None and _private(mapped):
        return True
    return bool(address.is_private or address.is_loopback or address.is_link_local
                or (isinstance(address, ipaddress.IPv4Address) and address in _CGNAT))


def _ipv4(match: re.Match[str]) -> str:
    try:
        return REDACTED_ADDRESS if _private(ipaddress.ip_address(match.group(0))) else match.group(0)
    except ValueError:
        return match.group(0)


def _ipv6(match: re.Match[str]) -> str:
    text = match.group(0)
    core = text.split("%", 1)[0]  # a zone id such as %eth0 goes with a redacted address
    for attempt in (core, core.rstrip(".")):  # a sentence-ending full stop is not part of the address
        try:
            address = ipaddress.ip_address(attempt)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and _private(address):
            return REDACTED_ADDRESS + core[len(attempt):]
        return text
    return text


def _mixed_secret(match: re.Match[str]) -> str:
    text = match.group(0)
    kinds = (any(c.islower() for c in text), any(c.isupper() for c in text), any(c.isdigit() for c in text))
    return REDACTED if all(kinds) else text


def _home_text(home: str | os.PathLike[str] | None) -> str:
    try:
        text = str(home) if home is not None else str(Path.home())
    except (RuntimeError, KeyError):
        return ""
    return text.rstrip("/") if len(text.rstrip("/")) > 1 else ""


def literal_patterns(texts: Iterable[str]) -> list[str]:
    """Strings to remove verbatim: each whole text and each line of it that is long enough to be specific."""
    found: set[str] = set()
    for text in texts:
        text = text.strip()
        if not text:
            continue
        found.add(text)
        found.update(line.strip() for line in text.splitlines() if len(line.strip()) >= _MIN_LITERAL)
        found.update(word for word in text.split() if len(word) >= _MIN_WORD_LITERAL)
    return sorted(found, key=len, reverse=True)


def redact(text: str, *, home: str | os.PathLike[str] | None = None, literals: Sequence[str] = ()) -> str:
    """``text`` without secrets, personal data and private addresses.

    ``home`` is the home folder shown as ``~`` (default: the current user's);
    ``literals`` are strings (prompts, names) removed wherever they appear.
    """
    for literal in literals:
        if literal:
            text = text.replace(literal, REDACTED_PROMPT)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)  # type: ignore[arg-type]  # str or callable, as re.sub accepts
    home_text = _home_text(home)
    if home_text:
        text = re.sub(re.escape(home_text) + r"(?![\w-])", "~", text)
    text = _HOSTNAME.sub(REDACTED_HOST, text)
    text = _IPV6.sub(_ipv6, text)
    text = _IPV4.sub(_ipv4, text)
    text = _HEX_SECRET.sub(REDACTED, text)
    return _BASE64_SECRET.sub(_mixed_secret, text)


# ---- collecting members ---------------------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"


def _read_text(path: Path, limit: int = TEXT_FILE_LIMIT) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    text = data[:limit].decode("utf-8", errors="replace")
    return text + "\n[truncated]\n" if len(data) > limit else text


def tail_text(path: Path, *, lines: int = LOG_TAIL_LINES, max_bytes: int = LOG_TAIL_BYTES) -> str | None:
    """The last ``lines`` lines (at most ``max_bytes`` bytes) of a file, or ``None`` when it cannot be read."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]  # the first line is cut
    return "\n".join(text.splitlines()[-lines:]) + "\n"


def _config_members(state: Path) -> dict[str, str]:
    from . import config as llm_config

    members: dict[str, str] = {}
    path = llm_config.config_path()
    text = _read_text(path)
    members["config/llm-tools.toml"] = text if text is not None else "# no llm-tools configuration file\n"

    worker: dict[str, Any] = {"enrolled": False}
    worker_file = state / "worker" / "worker.json"
    try:
        raw = json.loads(worker_file.read_text(encoding="utf-8"))
        worker = {"enrolled": True, **{key: raw[key] for key in ("worker_id", "name", "coordinator_url", "allow_control", "allow_terminal_view", "max_generation") if key in raw},
                  "token_present": bool(raw.get("token")), "ca_present": (state / "worker" / "ca.pem").exists()}
    except (OSError, ValueError, TypeError):
        pass
    members["config/worker.json"] = _json_text(worker)

    remote: dict[str, Any] = {"configured": False}
    remote_file = state / "remote" / "remote.json"
    try:
        raw = json.loads(remote_file.read_text(encoding="utf-8"))
        remote = {"configured": True, "coordinator_url": raw.get("coordinator_url"), "token_present": bool(raw.get("token")),
                  "ca_present": (state / "remote" / "ca.pem").exists()}
    except (OSError, ValueError, TypeError):
        pass
    members["config/remote.json"] = _json_text(remote)
    return members


def _count(rows: Iterable[Any]) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in rows}


def store_summary(store: BridgeStore, *, running: Callable[[str], bool | None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Counts and statuses of the database (no names or text) and controller liveness per mission."""
    probe = running if running is not None else (lambda mission_id: controller_running(mission_id, on_error=None))
    db = store.db
    task_counts: dict[str, dict[str, int]] = {}
    for mission_id, status, count in db.execute("SELECT mission_id,status,COUNT(*) FROM tasks GROUP BY mission_id,status"):
        task_counts.setdefault(mission_id, {})[status] = int(count)
    event_counts = {mission_id: int(count) for mission_id, count in db.execute("SELECT mission_id,COUNT(*) FROM events GROUP BY mission_id")}
    missions = []
    health = []
    for mission_id, status, created, updated in db.execute("SELECT id,status,created_at,updated_at FROM missions ORDER BY created_at,id").fetchall():
        missions.append({"id": mission_id, "status": status, "created_at": created, "updated_at": updated,
                         "tasks": task_counts.get(mission_id, {}), "events": event_counts.get(mission_id, 0)})
        try:
            alive = probe(mission_id)
        except (OSError, ValueError):
            alive = None
        health.append({"mission": mission_id, "status": status, "controller_running": alive})
    summary = {
        "schema_version": int(db.execute("PRAGMA user_version").fetchone()[0]),
        "missions": len(missions),
        "tasks": int(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
        "events": int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        "mission_statuses": _count(db.execute("SELECT status,COUNT(*) FROM missions GROUP BY status")),
        "task_statuses": _count(db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")),
        "event_kinds": _count(db.execute("SELECT kind,COUNT(*) FROM events GROUP BY kind")),
        "operation_statuses": _count(db.execute("SELECT status,COUNT(*) FROM operations GROUP BY status")),
        "mission_list": missions,
    }
    return summary, {"controllers": health}


def _private_texts(store: BridgeStore) -> list[str]:
    """Names, objectives and task text that must not reach a log tail."""
    texts: list[str] = []
    for row in store.db.execute("SELECT name,objective FROM missions"):
        texts.extend(str(value) for value in row)
    for row in store.db.execute("SELECT title,original_prompt,prompt FROM tasks"):
        texts.extend(str(value) for value in row)
    return texts


def _log_files(store: BridgeStore, state: Path) -> list[tuple[str, Path]]:
    """Recent controller and provider logs as (member name, path); prompt copies are never included."""
    chosen: dict[Path, str] = {}
    for (mission_id,) in store.db.execute("SELECT id FROM missions ORDER BY updated_at DESC,id"):
        runtime = state / "runtime" / f"mission-{mission_id}"
        label = mission_id[:12]
        candidates: list[Path] = [runtime / "controller.log"]
        runs = [path for path in (runtime / "ralph-logs").rglob("*") if path.is_file() and path.suffix in {".out", ".log"} and "prompt" not in path.name.lower()] \
            if (runtime / "ralph-logs").is_dir() else []
        candidates += sorted(runs, key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)[:RUN_LOGS_PER_MISSION]
        for path in candidates:
            if path.is_file() and path not in chosen:
                relative = path.relative_to(runtime)
                chosen[path] = f"logs/{label}/{'-'.join(relative.parts)}"
    for (logs_path,) in store.db.execute("SELECT logs_path FROM operations WHERE logs_path IS NOT NULL ORDER BY updated_at DESC LIMIT ?", (OPERATION_LOGS,)):
        path = Path(str(logs_path))
        if path.is_file() and "prompt" not in path.name.lower() and path not in chosen:
            chosen[path] = f"logs/operations/{len(chosen):02d}-{path.name}"
    return list(sorted(((name, path) for path, name in chosen.items()))[:MAX_LOG_FILES])


def collect_members(report: dict[str, Any], *, version: str, state: Path, store_file: Path, home: str | None = None,
                    running: Callable[[str], bool | None] | None = None, now: float | None = None) -> dict[str, str]:
    """Every bundle member (name -> redacted text) for a doctor ``report``."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    safe_report = dict(report)
    mission = safe_report.get("mission")
    if isinstance(mission, dict):  # the mission's text and name stay out; its ID and status identify it
        safe_report["mission"] = {"id": mission.get("id"), "status": mission.get("status")}
    raw: dict[str, str] = {
        "versions.json": _json_text({
            "bundle_format": BUNDLE_FORMAT, "generated_at": stamp, "llm_tools": version,
            "python": sys.version.split()[0], "python_implementation": platform.python_implementation(),
            "os": platform.system(), "os_release": platform.release(), "platform": platform.platform(),
            "machine": platform.machine(), "note": NOTE}),
        "doctor.json": _json_text(safe_report),
    }
    raw.update(_config_members(state))
    literals: list[str] = []
    logs: list[tuple[str, Path]] = []
    try:
        with BridgeStore(store_file, readonly=True) as store:
            summary, health = store_summary(store, running=running)
            literals = literal_patterns(_private_texts(store))
            logs = _log_files(store, state)
    except (sqlite3.Error, OSError) as exc:
        summary, health = {"unavailable": type(exc).__name__}, {"controllers": []}
    raw["store-summary.json"] = _json_text(summary)
    raw["health.json"] = _json_text(health)
    members = {name: redact(text, home=home, literals=literals) for name, text in raw.items()}
    for name, path in logs:
        text = tail_text(path)
        if text is not None:
            members[name] = redact(text, home=home, literals=literals)
    return members


def default_name(now: float | None = None) -> str:
    return f"llm-bridge-bundle-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}.tar.gz"


def _target(path: str | os.PathLike[str] | None, now: float | None) -> Path:
    if not path:
        return Path.cwd() / default_name(now)
    target = Path(path).expanduser().absolute()
    return target / default_name(now) if target.is_dir() else target


def write_archive(target: Path, members: dict[str, str], *, now: float | None = None) -> None:
    """Write ``members`` to ``target`` (new file, mode 0600); an existing file is never overwritten."""
    if not target.parent.is_dir():
        raise BundleError(f"folder {target.parent} does not exist")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BundleError(f"{target} already exists; choose a new file name") from None
    except OSError as exc:
        raise BundleError(f"cannot write {target}: {(exc.strerror or type(exc).__name__).lower()}") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                for name, text in sorted(members.items()):
                    data = text.encode("utf-8")
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o600
                    info.mtime = int(now if now is not None else time.time())
                    archive.addfile(info, io.BytesIO(data))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def write_bundle(path: str | os.PathLike[str] | None, report: dict[str, Any], *, version: str, state: Path, store_file: Path,
                 home: str | None = None, running: Callable[[str], bool | None] | None = None, now: float | None = None) -> dict[str, Any]:
    """Collect, redact and write the bundle; returns its path, members and the note on what was removed."""
    target = _target(path, now)
    members = collect_members(report, version=version, state=state, store_file=store_file, home=home, running=running, now=now)
    write_archive(target, members, now=now)
    return {"path": str(target), "bytes": target.stat().st_size, "members": sorted(members), "note": NOTE}
