"""Protocol v1 envelope parsing: strict validation and seeded malformed-input fuzz."""

from __future__ import annotations

import json
import random

import pytest

from llm_tools import bridge_protocol as proto
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_protocol import Message, ProtocolError, decode


def _message(kind: str, payload: dict, **overrides) -> dict:
    value = {"v": 1, "type": kind, "id": "m-1", "worker_id": "w-1", "generation": 3, "payload": payload}
    value.update(overrides)
    return value


VALID = [
    Message("hello", "w-1", 0, {"worker_id": "w-1", "name": "ws", "software_version": "1.4.0", "protocol_versions": [1], "platform": {}, "capabilities": [], "missions": [], "event_cursor": 0}),
    Message("welcome", "w-1", 0, {"generation": 1, "protocol_version": 1, "event_cursor": 0}),
    Message("command", "w-1", 2, CommandRequest("mission.list", request_id="r1").to_json()),
    Message("command.ack", "w-1", 2, {"request_id": "r1", "status": "duplicate"}),
    Message("command.result", "w-1", 2, {"schema_version": 1, "request_id": "r1", "operation": "mission.list", "outcome": "ok", "result": {}, "error": None}),
    Message("events", "w-1", 2, {"after": 4, "through": 9, "events": [{"id": 5}, {"id": 9}]}),
    Message("events", "w-1", 2, {"after": 4, "through": 9, "events": [{"id": 9}], "missions": [{"id": "m1"}]}),
    Message("events.nack", "w-1", 2, {"expected_after": 4}),
    Message("ping", "w-1", 2, {}),
    Message("error", "w-1", 2, {"code": "x", "message": "y", "in_reply_to": "m-0"}),
]


@pytest.mark.parametrize("message", VALID, ids=lambda message: message.type)
def test_valid_messages_round_trip(message):
    assert decode(message.encode()) == message
    assert decode(message.encode().encode()) == message


def test_hello_without_optional_host_name_remains_compatible():
    hello = next(message for message in VALID if message.type == "hello")
    decoded = decode(hello.encode())
    assert "host_name" not in decoded.payload


def test_hello_worker_policy_is_validated_and_backward_compatible():
    base = dict(VALID[0].payload)
    base["worker_policy"] = {"allow_control": True, "allow_terminal_view": True, "allow_create": False,
                             "allow_root": ["/srv/work"], "allow_power": False, "allow_restore": False}
    decoded = decode(Message("hello", "w-1", 0, base).encode())
    assert decoded.payload["worker_policy"] == base["worker_policy"]
    base["worker_policy"] = {"allow_create": "yes", "allow_root": []}
    with pytest.raises(ProtocolError, match="worker_policy"):
        decode(Message("hello", "w-1", 0, base).encode())


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xff", "invalid-message"),
        ("[]", "invalid-message"),
        (json.dumps({**_message("ping", {}), "extra": 1}), "invalid-message"),
        (json.dumps(_message("ping", {}, v=2)), "unsupported-version"),
        (json.dumps(_message("teleport", {})), "unsupported-message"),
        (json.dumps(_message("ping", {}, id="../x")), "invalid-message"),
        (json.dumps(_message("ping", {}, generation=-1)), "invalid-message"),
        (json.dumps(_message("ping", {}, generation=True)), "invalid-message"),
        (json.dumps(_message("ping", [])), "invalid-message"),
        (json.dumps(_message("ping", {"x": 1})), "invalid-message"),
        (json.dumps(_message("events", {"after": 5, "through": 9, "events": [{"id": 5}]})), "invalid-message"),
        (json.dumps(_message("events", {"after": 5, "through": 9, "events": [{"id": 7}, {"id": 6}]})), "invalid-message"),
        (json.dumps(_message("events", {"after": 5, "through": 6, "events": [{"id": 7}]})), "invalid-message"),
        (json.dumps(_message("events", {"after": 5, "through": 4, "events": []})), "invalid-message"),
        (json.dumps(_message("events", {"after": 0, "through": 1, "events": "x"})), "invalid-message"),
        (json.dumps(_message("events", {"after": 0, "through": 1, "events": [1]})), "invalid-message"),
        (json.dumps(_message("events", {"after": 0, "through": 1, "events": [{"id": 1}], "missions": {}})), "invalid-message"),
        (json.dumps(_message("events", {"after": 0, "through": 1, "events": [{"id": 1}], "mission_cursor": 2})), "invalid-message"),
        (json.dumps(_message("events.nack", {"expected_after": "1"})), "invalid-message"),
        (json.dumps(_message("welcome", {"generation": 0, "protocol_version": 1, "event_cursor": 0})), "invalid-message"),
        (json.dumps(_message("welcome", {"generation": 1, "protocol_version": 9, "event_cursor": 0})), "unsupported-version"),
        (json.dumps(_message("command.ack", {"request_id": "r", "status": "done"})), "invalid-message"),
        (json.dumps(_message("command", {**CommandRequest("x").to_json(), "schema_version": 9})), "unsupported-version"),
        (json.dumps(_message("hello", {**VALID[0].payload, "missions": {}})), "invalid-message"),
        (json.dumps(_message("hello", {**VALID[0].payload, "host_name": "bad\nhost"})), "invalid-message"),
        (json.dumps(_message("error", {"code": 1, "message": "m"})), "invalid-message"),
        ("x" * (proto.MAX_MESSAGE_BYTES + 1), "message-too-large"),
    ],
)
def test_malformed_messages_are_rejected_with_codes(raw, code):
    with pytest.raises(ProtocolError) as caught:
        decode(raw)
    assert caught.value.code == code


def test_encode_refuses_oversized_messages():
    with pytest.raises(ProtocolError, match="exceeds"):
        Message("error", "w", 1, {"code": "x", "message": "y" * proto.MAX_MESSAGE_BYTES}).encode()


def test_event_batches_are_bounded():
    events = [{"id": number} for number in range(1, proto.MAX_EVENTS_PER_BATCH + 2)]
    with pytest.raises(ProtocolError):
        decode(json.dumps(_message("events", {"after": 0, "through": len(events), "events": events})))


@pytest.mark.parametrize("seed", range(8))
def test_seeded_mutations_never_escape_protocol_error(seed):
    """Byte-level and structural mutations either decode or raise ProtocolError, nothing else."""
    rng = random.Random(seed)
    for _ in range(400):
        text = bytearray(rng.choice(VALID).encode().encode())
        for _ in range(rng.randint(1, 4)):
            position = rng.randrange(len(text))
            choice = rng.random()
            if choice < 0.4:
                text[position] = rng.randrange(256)
            elif choice < 0.7:
                del text[position]
            else:
                text[position:position] = rng.choice([b"{", b"}", b'"', b",", b"[]", b"null", b"-1", b"1e999"])
        try:
            decode(bytes(text))
        except ProtocolError:
            pass


def test_deep_nesting_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        decode("[" * 200_000 + "]" * 200_000)


def test_an_over_long_integer_is_a_protocol_error():
    text = '{"v":1,"type":"ping","id":"a","worker_id":"w","generation":' + "9" * 5000 + ',"payload":{}}'
    with pytest.raises(ProtocolError):
        decode(text)


# Version negotiation. Protocol versions are whole numbers (there is no minor version), so a peer one
# release older or newer is one that also offers a neighbouring number. It is accepted when the lists
# overlap and refused, with both lists named, when they do not.


def _hello(versions) -> str:
    return Message("hello", "w-1", 0, {**VALID[0].payload, "protocol_versions": versions}).encode()


@pytest.mark.parametrize(("offered", "selected"), [([1], 1), ([0, 1], 1), ([1, 2], 1), ([2, 1, 3], 1)])
def test_a_peer_that_also_speaks_a_neighbouring_version_is_accepted(offered, selected):
    message = decode(_hello(offered))
    assert message.payload["protocol_versions"] == offered
    assert proto.negotiate_version(offered) == selected


@pytest.mark.parametrize("offered", [[2], [0], [2, 3], [99]])
def test_a_peer_with_no_common_version_is_refused_naming_both_lists(offered):
    with pytest.raises(ProtocolError) as caught:
        decode(_hello(offered))
    assert caught.value.code == "unsupported-version"
    assert f"peer offers {sorted(set(offered))}" in str(caught.value) and "this build supports [1]" in str(caught.value)
    assert caught.value.details == {"supported": [1], "offered": sorted(set(offered))}


@pytest.mark.parametrize("offered", [[], "1", 1, None, ["1"], [True], [1.0], [-1], {"1": 1}])
def test_a_malformed_version_list_is_an_invalid_message(offered):
    with pytest.raises(ProtocolError) as caught:
        decode(_hello(offered))
    assert caught.value.code == "invalid-message"


def test_the_highest_common_version_wins_when_this_build_speaks_several(monkeypatch):
    monkeypatch.setattr(proto, "SUPPORTED_VERSIONS", (1, 2))
    assert proto.negotiate_version([1]) == 1  # an older peer
    assert proto.negotiate_version([2, 3]) == 2  # a newer peer
    assert proto.negotiate_version([1, 2, 3]) == 2
    with pytest.raises(ProtocolError, match=r"peer offers \[3\], this build supports \[1, 2\]"):
        proto.negotiate_version([3])
    assert decode(_hello([2])).payload["protocol_versions"] == [2]
