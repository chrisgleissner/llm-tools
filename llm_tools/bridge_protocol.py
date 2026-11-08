"""Worker–coordinator protocol version 1: envelopes and payload validation.

``docs/distributed/protocol.md`` is the normative description. This module is
pure: it encodes, decodes and validates messages and never performs I/O, so the
worker service and tests share one parser (PROTO-001, PROTO-002, TEST-008).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .bridge_commands import CommandFailure, CommandRequest

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = (1,)
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_EVENTS_PER_BATCH = 500
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_ENVELOPE_FIELDS = {"v", "type", "id", "worker_id", "generation", "payload"}

# Mission tombstones (PRD-038) in one `events` message: mission IDs of 1-128 characters of [A-Za-z0-9_.-].
MAX_DELETED_MISSIONS_PER_MESSAGE = 100
_DELETED_MISSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")

# type -> (required payload fields, optional payload fields)
MESSAGE_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "hello": (
        frozenset({"worker_id", "name", "software_version", "protocol_versions", "platform", "capabilities", "missions", "event_cursor"}),
        frozenset({"host_name", "worker_policy"}),
    ),
    "welcome": (frozenset({"generation", "protocol_version", "event_cursor"}), frozenset()),
    "command": (frozenset({"schema_version", "operation", "params", "mission_id", "target", "request_id"}), frozenset()),
    "command.ack": (frozenset({"request_id", "status"}), frozenset()),
    "command.result": (frozenset({"schema_version", "request_id", "operation", "outcome", "result", "error"}), frozenset()),
    "events": (frozenset({"after", "through", "events"}), frozenset({"mission_cursor", "missions", "deleted_missions"})),
    "events.nack": (frozenset({"expected_after"}), frozenset()),
    "terminal.open": (frozenset({"session_id", "mission_id", "mode", "cols", "rows"}), frozenset()),
    "terminal.close": (frozenset({"session_id"}), frozenset()),
    "terminal.status": (frozenset({"session_id", "state", "mode", "reason"}), frozenset()),
    "ping": (frozenset(), frozenset()),
    "pong": (frozenset(), frozenset()),
    "error": (frozenset({"code", "message"}), frozenset({"details", "in_reply_to"})),
}


class ProtocolError(CommandFailure):
    """A malformed or unsupported protocol message."""


def negotiate_version(offered: Any) -> int:
    """The highest protocol version both a peer's ``protocol_versions`` list and ``SUPPORTED_VERSIONS`` contain.

    Versions are whole numbers; there is no minor version. A peer that also speaks an older or
    newer version than this build is accepted as long as the lists overlap, so a worker and a
    coordinator from adjacent releases keep working. With no overlap the error names both lists.
    """
    if not isinstance(offered, list) or not offered or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in offered):
        raise ProtocolError("invalid-message", "hello: protocol_versions must be a non-empty list of whole numbers")
    common = [version for version in SUPPORTED_VERSIONS if version in offered]
    if not common:
        raise ProtocolError(
            "unsupported-version",
            f"hello: no common protocol version (peer offers {sorted(set(offered))}, this build supports {sorted(SUPPORTED_VERSIONS)})",
            details={"supported": list(SUPPORTED_VERSIONS), "offered": sorted(set(offered))},
        )
    return max(common)


@dataclass(frozen=True)
class Message:
    type: str
    worker_id: str
    generation: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    v: int = PROTOCOL_VERSION

    def encode(self) -> str:
        text = json.dumps(
            {"v": self.v, "type": self.type, "id": self.id, "worker_id": self.worker_id, "generation": self.generation, "payload": dict(self.payload)},
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message-too-large", f"{self.type} message exceeds {MAX_MESSAGE_BYTES} bytes")
        return text


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProtocolError("invalid-message", f"{name} must be an integer >= {minimum}")
    return value


def _validate_payload(kind: str, payload: Mapping[str, Any]) -> None:
    required, optional = MESSAGE_FIELDS[kind]
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing or unknown:
        raise ProtocolError("invalid-message", f"{kind}: missing {sorted(missing)} unknown {sorted(unknown)}")
    if kind == "command":
        try:
            CommandRequest.from_json(dict(payload))
        except CommandFailure as exc:
            raise ProtocolError(exc.code, str(exc), details=exc.details) from None
    elif kind == "command.ack":
        if payload["status"] not in {"received", "duplicate"} or not isinstance(payload["request_id"], str):
            raise ProtocolError("invalid-message", "command.ack: bad status or request_id")
    elif kind == "events":
        after, through = _int(payload["after"], "after"), _int(payload["through"], "through")
        events = payload["events"]
        if not isinstance(events, list) or len(events) > MAX_EVENTS_PER_BATCH:
            raise ProtocolError("invalid-message", f"events must be a list of at most {MAX_EVENTS_PER_BATCH}")
        previous = after
        for event in events:
            if not isinstance(event, dict):
                raise ProtocolError("invalid-message", "each event must be an object")
            event_id = _int(event.get("id"), "event id", minimum=1)
            if event_id <= previous or event_id > through:
                raise ProtocolError("invalid-message", "event ids must ascend within (after, through]")
            previous = event_id
        if "missions" in payload:
            missions = payload["missions"]
            if not isinstance(missions, list) or len(missions) > 10_000 or any(not isinstance(m, dict) for m in missions):
                raise ProtocolError("invalid-message", "missions must be a list of at most 10000 objects")
        if "deleted_missions" in payload:
            deleted = payload["deleted_missions"]
            if (not isinstance(deleted, list) or len(deleted) > MAX_DELETED_MISSIONS_PER_MESSAGE
                    or any(not isinstance(item, str) or not _DELETED_MISSION_ID.fullmatch(item) for item in deleted)):
                raise ProtocolError("invalid-message", f"deleted_missions must be a list of at most {MAX_DELETED_MISSIONS_PER_MESSAGE} mission IDs")
        if "mission_cursor" in payload and _int(payload["mission_cursor"], "mission_cursor") > through:
            raise ProtocolError("invalid-message", "mission_cursor must not be greater than through")
        if through < after:
            raise ProtocolError("invalid-message", "through must not be below after")
    elif kind == "events.nack":
        _int(payload["expected_after"], "expected_after")
    elif kind == "welcome":
        _int(payload["generation"], "generation", minimum=1)
        _int(payload["event_cursor"], "event_cursor")
        if payload["protocol_version"] not in SUPPORTED_VERSIONS:
            raise ProtocolError("unsupported-version", f"protocol version {payload['protocol_version']!r} is not supported")
    elif kind == "hello":
        _int(payload["event_cursor"], "event_cursor")
        if not isinstance(payload["capabilities"], list) or not isinstance(payload["missions"], list):
            raise ProtocolError("invalid-message", "hello: capabilities and missions must be lists")
        negotiate_version(payload["protocol_versions"])
        host_name = payload.get("host_name")
        if host_name is not None and (not isinstance(host_name, str) or not host_name or len(host_name.encode("utf-8")) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in host_name)):
            raise ProtocolError("invalid-message", "hello: host_name must be 1-255 bytes without control characters")
        policy = payload.get("worker_policy")
        if policy is not None:
            flags = ("allow_control", "allow_terminal_view", "allow_create", "allow_power", "allow_restore")
            if not isinstance(policy, dict) or any(not isinstance(policy.get(key), bool) for key in flags):
                raise ProtocolError("invalid-message", "hello: worker_policy must contain boolean opt-in fields")
            roots = policy.get("allow_root")
            if not isinstance(roots, list) or len(roots) > 32 or any(not isinstance(root, str) or not root or len(root.encode("utf-8")) > 4096 for root in roots):
                raise ProtocolError("invalid-message", "hello: worker_policy.allow_root must contain at most 32 paths of 1-4096 bytes")
    elif kind.startswith("terminal."):
        if not isinstance(payload["session_id"], str) or not _ID.fullmatch(payload["session_id"]):
            raise ProtocolError("invalid-message", "session_id must be 1-128 characters of [A-Za-z0-9_.-]")
        if kind == "terminal.open":
            if not isinstance(payload["mission_id"], str) or payload["mode"] not in {"view", "write"}:
                raise ProtocolError("invalid-message", "terminal.open: bad mission_id or mode")
            for name in ("cols", "rows"):
                if _int(payload[name], name, minimum=1) > 1000:
                    raise ProtocolError("invalid-message", f"{name} must be at most 1000")
        elif kind == "terminal.status":
            if payload["state"] not in {"opened", "refused", "closed"} or payload["mode"] not in {"view", "write"} or not isinstance(payload["reason"], str):
                raise ProtocolError("invalid-message", "terminal.status: bad state, mode or reason")
    elif kind == "error":
        if not isinstance(payload["code"], str) or not isinstance(payload["message"], str):
            raise ProtocolError("invalid-message", "error: code and message must be strings")


def decode(text: str | bytes) -> Message:
    """Parse and fully validate one message; raise ``ProtocolError`` otherwise."""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message-too-large", f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors; so is an over-long integer
        raise ProtocolError("invalid-message", "message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        raise ProtocolError("invalid-message", f"envelope must have exactly the fields {sorted(_ENVELOPE_FIELDS)}")
    if value["v"] not in SUPPORTED_VERSIONS:
        raise ProtocolError("unsupported-version", f"protocol version {value['v']!r} is not supported", details={"supported": list(SUPPORTED_VERSIONS)})
    kind = value["type"]
    if kind not in MESSAGE_FIELDS:
        raise ProtocolError("unsupported-message", f"unsupported message type {kind!r}")
    for name in ("id", "worker_id"):
        if not isinstance(value[name], str) or not _ID.fullmatch(value[name]):
            raise ProtocolError("invalid-message", f"{name} must be 1-128 characters of [A-Za-z0-9_.-]")
    generation = _int(value["generation"], "generation")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError("invalid-message", "payload must be an object")
    _validate_payload(kind, payload)
    return Message(type=kind, worker_id=value["worker_id"], generation=generation, payload=payload, id=value["id"], v=value["v"])
