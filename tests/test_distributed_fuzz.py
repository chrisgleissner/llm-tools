"""Seeded property tests for remote command validation and worker reconciliation (TEST-008).

Standard library only; every case is reproducible from its seed, as in
``tests/test_bridge_fuzz.py``. The operation registry is iterated dynamically,
so a new operation is covered without editing this file.
"""

from __future__ import annotations

import dataclasses
import json
import random
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandFailure, CommandRequest
from llm_tools.bridge_protocol import Message, decode
from llm_tools.bridge_store import BridgeStore

pytestmark = pytest.mark.fuzz
CASES_PER_OPERATION = 120
RECONCILE_SEEDS = 60
HUGE = [None, True, False, 0, -1, 7, 2**31, 2**63, -(2**63) - 1, 2**200, 1.5, float("nan"), "", "x", "x" * 5000, "\x00", "ünï‮", " ", "-1",
        [], [1], ["a"], [1, "a"], [[]], [None], {}, {"a": 1}, {"nested": [{"deep": {}}]}, "1; DROP TABLE tasks", "%", "../../etc"]


def _valid(rng: random.Random, kind: type, item: type | None) -> Any:
    if kind is bool:
        return rng.random() < 0.5
    if kind is int:
        return rng.choice([0, 1, 2, 3, 5, 100])
    if kind is list:
        pool = {int: [1, 2], str: ["a", "b"]}.get(item, [])
        return [rng.choice(pool) for _ in range(rng.randrange(3))] if pool else []
    return rng.choice(["", "text", "PENDING", "ALL", "accepted", "x" * 40])


def _params(rng: random.Random, spec: commands.OperationSpec) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, param in spec.params.items():
        roll = rng.random()
        if roll < 0.55:
            params[name] = _valid(rng, param.kind, param.item)
        elif roll < 0.85:
            params[name] = rng.choice(HUGE)
        # else: omitted
    if rng.random() < 0.15:
        params[rng.choice(["extra", "mission", "__proto__", "", "task_id "])] = rng.choice(HUGE)
    return params


def _mission_ref(rng: random.Random, mission_id: str) -> str | None:
    return rng.choice([mission_id, mission_id, mission_id[:8], "zz", "", None, "x" * 500, "%", "' OR 1=1 --", mission_id.upper()])


def _request_id(rng: random.Random, index: int) -> str:
    if rng.random() < 0.1:
        return rng.choice(["", "a b", "x" * 200, "../x", "\x00", "é"])
    return f"fuzz-{index}"


def _snapshot(state: Path) -> str:
    with BridgeStore(state / "bridge.sqlite3", readonly=True) as store:
        return "\n".join(store.db.iterdump())


@pytest.fixture
def mission(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setattr(commands, "_usage_collect", lambda *a, **k: {"providers": []}, raising=False)  # never reach a live provider
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    return state, json.loads(capsys.readouterr().out)["mission"]["id"]


@pytest.mark.parametrize("operation", sorted(commands.OPERATIONS))
def test_mutated_parameters_yield_a_structured_result_and_failed_validation_never_mutates(operation, mission):
    state, mission_id = mission
    spec = commands.OPERATIONS[operation]
    problems: list[str] = []
    for seed in range(CASES_PER_OPERATION):
        rng = random.Random(f"{operation}:{seed}")
        request = CommandRequest(operation, _params(rng, spec), mission_id=_mission_ref(rng, mission_id), request_id=_request_id(rng, seed))
        before = _snapshot(state)
        try:
            result = commands.execute(request)
        except BaseException as exc:  # noqa: BLE001 - the property under test
            problems.append(f"seed {seed}: unhandled {type(exc).__name__}: {str(exc)[:120]} for {request.params!r}")
            continue
        if result.outcome not in commands.OUTCOMES:
            problems.append(f"seed {seed}: outcome {result.outcome!r}")
        if result.error is not None and (not result.error.code or not isinstance(result.error.message, str)):
            problems.append(f"seed {seed}: unstructured error {result.error!r}")
        if result.outcome in {"failed", "uncertain"} or (result.error is not None and result.error.code == "internal"):
            problems.append(f"seed {seed}: internal failure {result.outcome}/{result.error.code if result.error else None} for {request.params!r}")
        json.dumps(result.to_json())  # always serialisable for the wire
        if result.outcome == "rejected" and result.result is None and _snapshot(state) != before:
            problems.append(f"seed {seed}: a rejected request changed the database: {request.params!r}")
    assert not problems, f"{operation}: {len(problems)} of {CASES_PER_OPERATION} cases failed (case seed is '{operation}:<seed>'):\n" + "\n".join(problems[:6])


@pytest.mark.parametrize("seed", range(40))
def test_mutated_envelopes_are_parsed_or_refused_with_a_command_failure(seed):
    rng = random.Random(seed)
    envelope: dict[str, Any] = CommandRequest(rng.choice(sorted(commands.OPERATIONS)), {"a": 1}, mission_id="m", request_id="r").to_json()
    for _ in range(rng.randrange(1, 4)):
        key = rng.choice(sorted(envelope) + ["unknown"])
        action = rng.choice(["replace", "drop", "add"])
        if action == "drop":
            envelope.pop(key, None)
        else:
            envelope[key] = rng.choice(HUGE)
    value: Any = envelope if rng.random() < 0.9 else rng.choice(HUGE)
    try:
        parsed = CommandRequest.from_json(value)
    except CommandFailure as failure:
        assert failure.code
    else:
        assert isinstance(parsed.operation, str) and isinstance(parsed.params, dict)


class Link:
    """One scripted coordinator connection; records every message the worker tries to send."""

    def __init__(self, inbox: list[str], drop_results: bool) -> None:
        self.inbox, self.drop_results = inbox, drop_results
        self.attempted: list[Message] = []

    def send_text(self, text: str) -> None:
        message = decode(text)
        self.attempted.append(message)
        if self.drop_results and message.type == "command.result":
            raise ConnectionResetError("partition")

    def receive_text(self, timeout=None):
        if self.inbox:
            return self.inbox.pop(0)
        raise ConnectionResetError("closed")

    def close(self) -> None:
        pass


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp("reconcile-template")
    mp = pytest.MonkeyPatch()
    mp.setenv("LLM_BRIDGE_STATE_DIR", str(root / "state"))
    try:
        assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(root), "--providers", "codex", "--json"]) == 0
        with BridgeStore(root / "state" / "bridge.sqlite3") as store:
            mission_id = store.list_missions()[0]["id"]
    finally:
        mp.undo()
    return root / "state", mission_id


def _reconcile_case(seed: int, template_state: Path, mission_id: str, tmp_path: Path, monkeypatch) -> int:
    """Run one scripted session; returns how many applied results the worker produced."""
    state = tmp_path / f"state-{seed}"
    shutil.copytree(template_state, state)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))  # commands.execute opens the store from the environment
    rng = random.Random(seed)
    ids = [f"req-{index}" for index in range(rng.randrange(1, 5))]
    config = worker.WorkerConfig("wid", "ws", "wss://c:1", "t" * 40, True, rng.choice([0, 0, 3]), False)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(state / "bridge.sqlite3"), software_version="t", save=lambda value: None,
                                   audit=lambda *a, **k: None)

    def command(request_id: str, generation: int) -> str:
        prompt = f"prompt-{request_id}" + ("-changed" if rng.random() < 0.1 else "")
        request = CommandRequest("task.add", {"prompt": prompt}, mission_id=mission_id, request_id=request_id)
        return Message("command", "wid", generation, request.to_json()).encode()

    links: list[Link] = []
    for _ in range(rng.randrange(2, 7)):
        generation = rng.choice([1, 2, 3, 4, 5, 5, 6])  # may repeat or go backwards: a welcome must then be refused
        inbox = [Message("welcome", "c", generation, {"generation": generation, "protocol_version": 1, "event_cursor": 0}).encode()]
        for _ in range(rng.randrange(0, 6)):
            inbox.append(command(rng.choice(ids), generation if rng.random() < 0.85 else generation + rng.choice([-1, 1])))
        links.append(Link(inbox, drop_results=rng.random() < 0.4))
    pending = iter(links)
    stop = threading.Event()

    def connect(cfg):
        link = next(pending, None)
        if link is None:
            stop.set()
            raise ConnectionRefusedError("script finished")
        return link

    monkeypatch.setattr(worker.random, "uniform", lambda low, high: 0.0)  # no real backoff between scripted connections
    worker.run(session, connect, stop=stop, idle=0.001, max_backoff=0.001)

    attempted: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        for message in link.attempted:
            if message.type == "command.result":
                attempted.setdefault(str(message.payload["request_id"]), []).append(message.payload)
    with BridgeStore(state / "bridge.sqlite3") as store:
        tasks = [str(task["prompt"]) for task in store.list_tasks(mission_id)]
        rows = {row[0]: row[1] for row in store.db.execute("SELECT id, COUNT(*) FROM commands GROUP BY id")}
    assert len(tasks) <= 1 + len(ids)
    for request_id in ids:
        effects = [prompt for prompt in tasks if prompt.startswith(f"prompt-{request_id}")]
        assert len(effects) <= 1, f"seed {seed}: {request_id} applied {len(effects)} times: {effects}"
        assert rows.get(request_id, 0) <= 1, f"seed {seed}: {request_id} has several command rows"
        applied = [payload for payload in attempted.get(request_id, []) if payload["outcome"] == "applied"]
        assert len({json.dumps(payload["result"], sort_keys=True) for payload in applied}) <= 1, f"seed {seed}: {request_id} reported different results"
        if applied:
            assert len(effects) == 1, f"seed {seed}: {request_id} reported applied without an effect"
        if effects:
            assert request_id in attempted, f"seed {seed}: {request_id} applied but never answered"
    return sum(len(payloads) for payloads in attempted.values())


def test_random_redelivery_reconnect_and_generation_changes_apply_each_request_id_at_most_once(template, tmp_path, monkeypatch):
    template_state, mission_id = template
    answers = [_reconcile_case(seed, template_state, mission_id, tmp_path, monkeypatch) for seed in range(RECONCILE_SEEDS)]
    # The property must not hold vacuously: many sessions answered the same request more than once.
    assert sum(1 for count in answers if count >= 2) >= RECONCILE_SEEDS // 4, answers
