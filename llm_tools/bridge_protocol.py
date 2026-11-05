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

# type -> (required payload fields, optional payload fields)
MESSAGE_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "hello": (
        frozenset({"worker_id", "name", "software_version", "protocol_versions", "platform", "capabilities", "missions", "event_cursor"}),
        frozenset(),
    ),
    "welcome": (frozenset({"generation", "protocol_version", "event_cursor"}), frozenset()),
    "command": (frozenset({"schema_version", "operation", "params", "mission_id", "target", "request_id"}), frozenset()),
    "command.ack": (frozenset({"request_id", "status"}), frozenset()),
    "command.result": (frozenset({"schema_version", "request_id", "operation", "outcome", "result", "error"}), frozenset()),
    "events": (frozenset({"after", "through", "events"}), frozenset()),
    "events.nack": (frozenset({"expected_after"}), frozenset()),
    "ping": (frozenset(), frozenset()),
    "pong": (frozenset(), frozenset()),
    "error": (frozenset({"code", "message"}), frozenset({"details", "in_reply_to"})),
}


class ProtocolError(CommandFailure):
    """A malformed or unsupported protocol message."""


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
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
