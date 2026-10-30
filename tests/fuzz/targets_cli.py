"""Fuzz targets for command lines, the CLI parser and the durable store."""

from __future__ import annotations

import contextlib
import io
import json
import random
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from llm_tools import bridge, bridge_format, bridge_tui
from llm_tools.bridge_store import TASK_STATES, MISSION_STATES, BridgeStore, TransitionError

from . import generators as gen
from .harness import FuzzFailure, Patcher, Sandbox, db_fingerprint, note

CI_CASES: dict[str, int] = {}


def _cli_case(cases: int) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def register(function: Callable[..., None]) -> Callable[..., None]:
        CI_CASES[function.__name__] = cases
        return function

    return register


# --------------------------------------------------------------------- fixtures

def make_mission(sb: Sandbox, rng: random.Random, *, tasks: int | None = None, verifying: bool = True) -> str:
    """A fresh database with one mission whose tasks sit in assorted states; returns the mission id."""
    sb.fresh_state()
    count = rng.randint(1, 5) if tasks is None else tasks
    with BridgeStore(sb.database()) as store:
        mission = store.create_mission("Fuzz mission", "objective for fuzzing", cwd=sb.work, initial_prompt="first task",
                                       config={"native_tui": rng.random() < 0.5, "providers": "codex"})
        mid = str(mission["id"])
        ids = [int(mission["initial_task_id"])]
        for number in range(2, count + 1):
            ids.append(store.add_task(mid, f"task number {number}", priority=rng.randint(-2, 3), acceptance=["done"] if rng.random() < 0.3 else ()))
        if verifying and count > 1 and rng.random() < 0.8:
            store.set_mission_status(mid, "RUNNING")
            op = store.begin_operation(ids[0], iteration=1, station="codex")
            store.mark_operation_running(op, process_pid=None, route_id="codex", model="gpt")
            store.finish_operation(op, exit_code=0)  # task 1 now awaits verification
        if rng.random() < 0.3 and count > 2:
            store.set_task_status(ids[-1], "PAUSED")
    return mid


@contextlib.contextmanager
def guarded_main_side_effects(patch: Patcher) -> Any:
    """Make every route that would start a controller, station, UI or process raise instead."""
    def refuse(name: str) -> Callable[..., Any]:
        def inner(*_a: Any, **_k: Any) -> Any:
            raise FuzzFailure(f"the fuzzed command reached {name}")
        return inner

    from llm_tools import bridge_launcher, bridge_runtime, bridge_switcher

    patch.setattr(bridge, "start_mission", refuse("start_mission"))
    patch.setattr(bridge, "_stop_terminal_mission_station", lambda mission_id: {"station": "none"})
    patch.setattr(bridge_runtime, "run_controller", refuse("run_controller"))
    patch.setattr(bridge_tui, "run_control", refuse("run_control"))
    patch.setattr(bridge_tui, "run_console", refuse("run_console"))
    patch.setattr(bridge_launcher, "run_launcher", refuse("run_launcher"))
    patch.setattr(bridge_switcher, "run_picker", refuse("run_picker"))
    yield


class _Spawns:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []


def record_spawns(patch: Patcher, *, forbid: bool) -> _Spawns:
    """Record (and optionally forbid) subprocess creation; the provider stubs on PATH are inert anyway."""
    spawns = _Spawns()
    original = subprocess.Popen.__init__

    def init(self: subprocess.Popen[Any], args: Any, *a: Any, **k: Any) -> None:
        argv = [str(item) for item in args] if isinstance(args, (list, tuple)) else [str(args)]
        spawns.argv.append(argv)
        if forbid:
            raise FuzzFailure(f"a read-only fuzz case spawned a process: {argv[:4]}")
        original(self, args, *a, **k)

    patch.setattr(subprocess.Popen, "__init__", init)
    return spawns


STATE_TABLES = ("missions", "tasks", "task_dependencies", "operations")  # a rejected command is still logged in `commands` and `events`
READ_ONLY_VERBS: list[list[str]] = [["status"], ["events"], ["task", "list"], ["task", "show"], ["proposal", "list"], ["mission", "list"], ["missions"], ["rotation"], ["theme"], ["help"], ["doctor"]]
SAFE_MUTATING_VERBS: list[list[str]] = [
    ["task", "add"], ["task", "amend"], ["task", "priority"], ["task", "reorder"], ["task", "dependencies"], ["task", "pause"], ["task", "resume"], ["task", "cancel"], ["task", "retry"],
    ["task", "verify"], ["task", "reject"], ["proposal", "accept"], ["proposal", "reject"], ["proposal", "create"], ["mission", "create"], ["mission", "set"], ["mission", "colour"],
    ["rotation", "--set", "codex"], ["steer"], ["station"], ["pause"], ["cancel"], ["continue"],
]

_FLAG_POOL = ["--json", "--task", "--limit", "--offset", "--after", "--all", "--status", "--include-config", "--theme", "--colors", "--color", "--mission", "--help", "-h", "--task-id", "--value",
              "--add", "--evidence", "--approve", "--reason", "--prompt", "--title", "--priority", "--order", "--depends-on", "--acceptance", "--kind", "--route", "--set", "--providers", "--routes",
              "--auto-continue", "--cwd", "--name", "--operation", "--pick", "--"]


def argv_text(rng: random.Random) -> str:
    """Text as it can arrive in argv: valid Unicode, or bytes that were not UTF-8 (surrogate escapes)."""
    if rng.random() < 0.1:
        return "".join(chr(0xDC00 + rng.randint(0x80, 0xFF)) for _ in range(rng.randint(1, 4))) + gen.plain_text(rng, 5, bounded=True)
    return gen.valid_text(rng, 15, bounded=True).replace("\x00", "")


def chaotic_args(rng: random.Random, base: list[str]) -> list[str]:
    args = list(base)
    for _ in range(rng.randint(0, 6)):
        kind = rng.choice(["flag", "flagnum", "flagtext", "num", "text", "dup"])
        if kind == "flag":
            args.append(rng.choice(_FLAG_POOL))
        elif kind == "flagnum":
            args += [rng.choice(["--task", "--limit", "--offset", "--after", "--value", "--add", "--priority", "--order", "--task-id", "--colors"]), rng.choice(gen._NUMBERS)]
        elif kind == "flagtext":
            args += [rng.choice(_FLAG_POOL), argv_text(rng)]
        elif kind == "num":
            args.append(rng.choice(gen._NUMBERS))
        elif kind == "text":
            args.append(argv_text(rng))
        elif args:
            args.append(args[-1])
    return [arg for arg in args if "\x00" not in arg]


def run_main(argv: list[str]) -> tuple[int, str, str]:
    """In-process ``bridge.main``: (exit status, stdout, stderr); only SystemExit is an acceptable escape."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = bridge.main(argv)
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return int(status or 0), out.getvalue(), err.getvalue()


# ------------------------------------------------------------------------ the CLI

@_cli_case(40)
def target_cli_readonly(rng: random.Random, sb: Sandbox) -> None:
    """Read-only commands with chaotic arguments never traceback, never spawn work and never change the database."""
    mid = make_mission(sb, rng)
    verb = rng.choice(READ_ONLY_VERBS)
    args = chaotic_args(rng, verb)
    if rng.random() < 0.6 and verb[0] not in {"missions", "theme", "mission", "help"}:
        args += ["--mission", mid[:rng.choice([4, 8, 32])]]
    note("argv", args)
    before = db_fingerprint(sb.database())
    with Patcher() as patch:
        with guarded_main_side_effects(patch):
            spawns = record_spawns(patch, forbid=verb[0] != "doctor")
            status, out, err = run_main(args)
    if db_fingerprint(sb.database()) != before:
        raise FuzzFailure(f"read-only command changed the database: {args}")
    if "Traceback (most recent call last)" in out + err:
        raise FuzzFailure(f"traceback in output of {args}: {(out + err)[-400:]}")
    if status != 0 and not (out + err).strip():
        raise FuzzFailure(f"{args} failed with status {status} and no message")
    del spawns


@_cli_case(60)
def target_cli_mutating(rng: random.Random, sb: Sandbox) -> None:
    """State-changing commands with chaotic arguments exit cleanly with a message; invalid input exits non-zero."""
    mid = make_mission(sb, rng)
    verb = rng.choice(SAFE_MUTATING_VERBS)
    args = chaotic_args(rng, verb)
    if rng.random() < 0.8:
        args += ["--mission", mid[:8]]
    note("argv", args)
    with Patcher() as patch:
        with guarded_main_side_effects(patch):
            record_spawns(patch, forbid=True)
            status, out, err = run_main(args)
    text = out + err
    if "Traceback (most recent call last)" in text:
        raise FuzzFailure(f"traceback in output of {args}: {text[-400:]}")
    if status != 0 and not text.strip():
        raise FuzzFailure(f"{args} failed with status {status} and no message")
    with BridgeStore(sb.database(), readonly=True) as store:
        check_store(store, mid)


@_cli_case(40)
def target_cli_invalid(rng: random.Random, sb: Sandbox) -> None:
    """Commands that are certainly invalid fail with a clean message and a non-zero status."""
    mid = make_mission(sb, rng)
    templates: list[list[str]] = [
        ["frobnicate"], ["task"], ["task", "frobnicate"], ["task", "show"], ["task", "show", "--task", "abc"], ["task", "show", "--task", "99999"], ["task", "priority", "--task", "1"],
        ["task", "add"], ["task", "add", "--prompt", ""], ["task", "reject", "--task", "1"], ["mission", "colour", "99"], ["mission", "colour", "banana"], ["events", "--limit", "-5"],
        ["events", "--limit", "99999999"], ["task", "list", "--limit", "x"], ["steer", "--kind", "bogus"], ["steer", "--kind", "replace"], ["rotation", "--set", "no-such-provider"],
        ["station"], ["task", "dependencies", "--task", "1", "--add", "1"], ["proposal", "accept", "--task", "1"], ["--bogus-flag"], ["status", "--mission", "zzzzzzzz-not-a-mission"],
    ]
    args = list(rng.choice(templates))
    if rng.random() < 0.5 and "--mission" not in args:
        args += ["--mission", mid[:8]]
    note("argv", args)
    before = db_fingerprint(sb.database(), STATE_TABLES)
    with Patcher() as patch:
        with guarded_main_side_effects(patch):
            record_spawns(patch, forbid=True)
            status, out, err = run_main(args)
    text = out + err
    if "Traceback (most recent call last)" in text:
        raise FuzzFailure(f"traceback in output of {args}: {text[-400:]}")
    if status == 0 and not (args[0] == "task" and args[1:2] == ["list"]):
        raise FuzzFailure(f"invalid command {args} exited 0: {text[:300]!r}")
    if not text.strip():
        raise FuzzFailure(f"invalid command {args} gave no message")
    if db_fingerprint(sb.database(), STATE_TABLES) != before:
        raise FuzzFailure(f"invalid command changed missions, tasks or operations: {args}")


# --------------------------------------------------------------------- console commands

class _FakeRun:
    """Stands in for the subprocess that executes a console command."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(item) for item in argv])
        roll = self.rng.random()
        if roll < 0.7:
            return subprocess.CompletedProcess(argv, 0, '{"status": "APPLIED", "result": {}}', "")
        if roll < 0.85:
            return subprocess.CompletedProcess(argv, 2, "", "llm-bridge: refused")
        return subprocess.CompletedProcess(argv, 0, gen.chaotic_text(self.rng, 60, bounded=True), "")


READ_ONLY_CONSOLE = ["status", "events", "tasks", "orders", "task list", "task show", "proposals", "proposal list", "theme", "doctor", "rotation", "help", "?", "missions", "quit", "exit"]


@_cli_case(80)
def target_console_commands(rng: random.Random, sb: Sandbox) -> None:
    """dispatch_console_command on chaotic lines: always a clean result, never a traceback; read-only lines never touch the database."""
    mid = make_mission(sb, rng)
    line = gen.command_line(rng)
    read_only = rng.random() < 0.3
    if read_only:
        line = rng.choice(READ_ONLY_CONSOLE) + " " + line
    note("line", line)
    fake = _FakeRun(rng)
    before = db_fingerprint(sb.database())
    with Patcher() as patch:
        patch.setattr(bridge.subprocess, "run", fake)
        record_spawns(patch, forbid=True)
        result = bridge.dispatch_console_command(mid, line)
    if isinstance(result, str):
        if "Traceback (most recent call last)" in result:
            raise FuzzFailure(f"traceback text returned for {line!r}: {result[-300:]}")
    elif type(result).__name__ not in {"TerminalAction", "ControlViewAction"}:
        raise FuzzFailure(f"unexpected result type {type(result).__name__} for {line!r}")
    if read_only and not line.strip().startswith(("quit", "exit")):
        words = line.split()
        if words and words[0] in {"status", "events", "tasks", "orders", "proposals", "theme", "doctor", "help", "?", "missions"} and db_fingerprint(sb.database()) != before:
            raise FuzzFailure(f"read-only console line changed the database: {line!r}")
    # A command the console runs in a subprocess must be a real bridge invocation, never a shell string.
    for call in fake.calls:
        if call[:3] and call[1:3] != ["-m", "llm_tools.bridge"] and not call[0].endswith(("python", "python3", sys.executable)):
            raise FuzzFailure(f"console command executed an unexpected program: {call[:4]}")


@_cli_case(30)
def target_console_commands_invalid(rng: random.Random, sb: Sandbox) -> None:
    """Certainly-invalid console lines answer with an error and run nothing."""
    mid = make_mission(sb, rng)
    bad = rng.choice([
        "frobnicate", "frobnicate --json", "task", "task frobnicate", "/frobnicate", "approve abc", "approve 1 2", "reject", "verify abc", "priority 1", "priority a b", "amend 1", "dependencies 1",
        "add", "add a b c", "steer", "steer bogus", "task show", "task show --task x", "mission colour", "console", "_controller --mission " + mid, "'unbalanced", '"unbalanced', "task add --prompt 'x",
    ])
    if rng.random() < 0.3:
        bad = "/" + bad.lstrip("/")
    note("line", bad)
    fake = _FakeRun(rng)
    with Patcher() as patch:
        patch.setattr(bridge.subprocess, "run", fake)
        record_spawns(patch, forbid=True)
        result = bridge.dispatch_console_command(mid, bad)
    text = result if isinstance(result, str) else ""
    if not isinstance(result, str) or not (text.lower().startswith("error") or "unknown" in text.lower() or "usage" in text.lower() or "required" in text.lower() or "not " in text.lower()):
        raise FuzzFailure(f"invalid line {bad!r} was not rejected clearly: {result!r}")
    if fake.calls:
        raise FuzzFailure(f"invalid line {bad!r} still ran a subprocess: {fake.calls[0][:5]}")


@_cli_case(8)
def target_console_commands_real(rng: random.Random, sb: Sandbox) -> None:
    """Read-only console lines through the real subprocess path: never a traceback."""
    mid = make_mission(sb, rng)
    for _ in range(3):
        line = rng.choice(["status", "events", "tasks", "task list", "proposals", "theme", "rotation", "task show --task 1"]) + " " + gen.command_line(rng, words=3)
        note("line", line)
        before = db_fingerprint(sb.database())
        result = bridge.dispatch_console_command(mid, line)
        text = result if isinstance(result, str) else ""
        if "Traceback (most recent call last)" in text:
            raise FuzzFailure(f"traceback from {line!r}: {text[-500:]}")
        words = line.split()
        if words[0] in {"status", "events", "tasks", "proposals", "theme", "rotation"} and db_fingerprint(sb.database()) != before:
            raise FuzzFailure(f"read-only line changed the database: {line!r}")


# ----------------------------------------------------------------------- the store

MISSION_EDGES = {
    "CREATED": {"RUNNING", "CANCELLED"}, "RUNNING": {"PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"}, "PAUSED": {"RUNNING", "STOPPING", "CANCELLED"},
    "STOPPING": {"STOPPED", "RUNNING", "FAILED", "CANCELLED"}, "STOPPED": {"RUNNING", "CANCELLED"}, "COMPLETED": set(), "FAILED": {"RUNNING", "CANCELLED"}, "CANCELLED": set(),
}
TASK_EDGES = {
    "QUEUED": {"READY", "PAUSED", "BLOCKED", "CANCELLED"}, "READY": {"QUEUED", "RUNNING", "PAUSED", "BLOCKED", "CANCELLED"}, "RUNNING": {"PAUSED", "BLOCKED", "CANCELLED"},
    "PAUSED": {"QUEUED", "READY", "CANCELLED"}, "BLOCKED": {"QUEUED", "READY", "CANCELLED"}, "VERIFYING": {"FAILED", "CANCELLED"}, "COMPLETED": set(), "FAILED": {"QUEUED", "CANCELLED"},
    "CANCELLED": set(),
}
DOCUMENTED_ERRORS = (TransitionError, ValueError, KeyError)


def check_store(store: BridgeStore, mission_id: str) -> None:
    """Structural invariants of one mission's rows."""
    db = store.db
    tasks = [dict(row) for row in db.execute("SELECT * FROM tasks WHERE mission_id=? ORDER BY id", (mission_id,))]
    for task in tasks:
        if task["status"] not in TASK_STATES:
            raise FuzzFailure(f"task {task['id']} has status {task['status']!r}")
    mission = store.get_mission(mission_id)
    if mission["status"] not in MISSION_STATES:
        raise FuzzFailure(f"mission has status {mission['status']!r}")
    ordinals = [task["ordinal"] for task in tasks]
    if len(set(ordinals)) != len(ordinals):
        raise FuzzFailure(f"duplicate ordinals {ordinals}")
    if ordinals and sorted(ordinals) != list(range(1, len(ordinals) + 1)):
        raise FuzzFailure(f"ordinals are not 1..n: {sorted(ordinals)}")
    # counters match a recount
    counters = store.task_counters(mission_id)
    effective = [task for task in tasks if task["origin"] == "operator" or task["proposal_status"] == "ACCEPTED"]
    recount: dict[str, int] = {}
    for task in effective:
        recount[task["status"]] = recount.get(task["status"], 0) + 1
    if counters["status"] != recount:
        raise FuzzFailure(f"status counters {counters['status']} != recount {recount}")
    if counters["total"] != len(tasks):
        raise FuzzFailure(f"total counter {counters['total']} != {len(tasks)} tasks")
    proposals: dict[str, int] = {}
    for task in tasks:
        if task["origin"] == "agent-proposal":
            proposals[task["proposal_status"]] = proposals.get(task["proposal_status"], 0) + 1
    if counters["proposal"] != proposals:
        raise FuzzFailure(f"proposal counters {counters['proposal']} != {proposals}")
    verified = sum(len(json.loads(task["acceptance_json"])) for task in effective if task["status"] == "COMPLETED")
    total = sum(len(json.loads(task["acceptance_json"])) for task in effective)
    if (counters["criteria_total"], counters["criteria_verified"]) != (total, verified):
        raise FuzzFailure(f"criteria counters {(counters['criteria_total'], counters['criteria_verified'])} != {(total, verified)}")
    # dependency graph: same mission, acyclic
    by_id = {task["id"]: task for task in tasks}
    edges: dict[int, list[int]] = {task["id"]: [] for task in tasks}
    for row in db.execute("SELECT task_id, depends_on FROM task_dependencies"):
        if row[0] in by_id:
            if row[1] not in by_id:
                raise FuzzFailure(f"task {row[0]} depends on a task of another mission")
            edges[row[0]].append(row[1])
    state: dict[int, int] = {}

    def visit(node: int) -> None:
        state[node] = 1
        for nxt in edges[node]:
            if state.get(nxt) == 1:
                raise FuzzFailure("dependency cycle")
            if nxt not in state:
                visit(nxt)
        state[node] = 2

    for node in edges:
        if node not in state:
            visit(node)
    event_ids = [row[0] for row in db.execute("SELECT id FROM events WHERE mission_id=? ORDER BY id", (mission_id,))]
    if event_ids != sorted(event_ids) or len(set(event_ids)) != len(event_ids):
        raise FuzzFailure("event ids are not strictly increasing")
    stamps = [row[0] for row in db.execute("SELECT created_at FROM events WHERE mission_id=? ORDER BY id", (mission_id,))]
    if stamps != sorted(stamps):
        raise FuzzFailure("events are not in chronological order")


def _snapshot_of(store: BridgeStore) -> tuple[Any, ...]:
    return (
        [dict(row) for row in store.db.execute("SELECT * FROM missions")],
        [dict(row) for row in store.db.execute("SELECT * FROM tasks ORDER BY id")],
        [tuple(row) for row in store.db.execute("SELECT * FROM task_dependencies ORDER BY 1,2")],
        store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    )


@_cli_case(60)
def target_store_machine(rng: random.Random, sb: Sandbox) -> None:
    """Random store operation sequences against a reference model: legal transitions apply, illegal ones raise a documented error and change nothing."""
    sb.fresh_state()
    with BridgeStore(sb.database()) as store:
        mission = store.create_mission(gen.plain_text(rng, 20).strip() or "m", gen.valid_text(rng, 30) or "objective", cwd=sb.work, initial_prompt=gen.valid_text(rng, 20) or "first")
        mid = str(mission["id"])
        model_status: dict[int, str] = {int(mission["initial_task_id"]): "QUEUED"}
        model_mission = "CREATED"
        ids = list(model_status)
        operations: dict[str, int] = {}
        for step in range(rng.randint(10, 60)):
            kind = rng.choice(["add", "amend", "priority", "reorder", "dep", "task_status", "task_status", "mission_status", "begin", "finish", "verify", "reject", "ready", "cancel_all"])
            target = rng.choice(ids + [999999]) if rng.random() > 0.05 else rng.choice([0, -1])
            before = _snapshot_of(store)
            events_before = before[3]
            expected_legal: bool | None = None  # None: let the store decide
            apply: Callable[[], Any]
            if kind == "add":
                text = gen.valid_text(rng, 15, bounded=True)
                deps = [rng.choice(ids)] if ids and rng.random() < 0.3 else []
                note("op", ("add", text, deps))
                expected_legal = bool(text) and model_mission != "CANCELLED"
                apply = lambda: store.add_task(mid, text, priority=rng.randint(-5, 5), depends_on=deps)  # noqa: E731
            elif kind == "amend":
                note("op", ("amend", target))
                expected_legal = model_status.get(target) not in {None, "COMPLETED", "FAILED", "CANCELLED"}
                apply = lambda: store.amend_task(target, prompt=gen.valid_text(rng, 10, bounded=True) or "p")  # noqa: E731
            elif kind == "priority":
                note("op", ("priority", target))
                expected_legal = model_status.get(target) not in {None, "COMPLETED", "CANCELLED"}
                apply = lambda: store.set_task_priority(target, rng.randint(-10, 10))  # noqa: E731
            elif kind == "reorder":
                note("op", ("reorder", target))
                expected_legal = model_status.get(target) not in {None, "COMPLETED", "CANCELLED"}
                apply = lambda: store.reorder_task(target, rng.randint(-10, 100))  # noqa: E731
            elif kind == "dep":
                other = rng.choice(ids + [999999])
                note("op", ("dep", target, other))
                apply = lambda: store.add_dependency(mid, target, other)  # noqa: E731
            elif kind == "task_status":
                wanted = rng.choice(sorted(TASK_STATES) + ["bogus"])
                note("op", ("task_status", target, wanted))
                old = model_status.get(target)
                if wanted == "bogus":
                    expected_legal = False
                elif old is None:
                    expected_legal = False
                else:
                    expected_legal = wanted == old or wanted in TASK_EDGES[old]
                    if wanted == "READY" and old != "READY" and not store._dependencies_complete(target):
                        expected_legal = False  # a task cannot become ready while a prerequisite is incomplete
                apply = lambda: store.set_task_status(target, wanted)  # noqa: E731
            elif kind == "mission_status":
                wanted = rng.choice(sorted(MISSION_STATES) + ["bogus"])
                note("op", ("mission_status", wanted))
                if wanted == "bogus":
                    expected_legal = False
                elif wanted != model_mission and wanted not in MISSION_EDGES[model_mission]:
                    expected_legal = False
                apply = lambda: store.set_mission_status(mid, wanted)  # noqa: E731
            elif kind == "begin":
                note("op", ("begin", target))
                apply = lambda: operations.__setitem__(store.begin_operation(target, iteration=rng.choice([0, 1, 2]), station=rng.choice(["codex", ""])), target)  # noqa: E731
            elif kind == "finish":
                if operations:
                    op = rng.choice(list(operations))
                else:
                    op = "no-such-operation"
                note("op", ("finish", op[:8]))
                code = rng.choice([0, 0, 1, 75, 143])
                def apply() -> Any:  # noqa: E306
                    if store.db.execute("SELECT status FROM operations WHERE id=?", (op,)).fetchone() and store.get_operation(op)["status"] == "PREPARED":
                        store.mark_operation_running(op, route_id="codex")
                    return store.finish_operation(op, exit_code=code)
            elif kind == "verify":
                note("op", ("verify", target))
                evidence = rng.choice(["", "  ", gen.valid_text(rng, 10, bounded=True)])
                approve = rng.random() < 0.5
                apply = lambda: store.verify_task(target, evidence=evidence, approved=approve, record_only=rng.random() < 0.3)  # noqa: E731
            elif kind == "reject":
                note("op", ("reject", target))
                reason = rng.choice(["", " ", gen.valid_text(rng, 10, bounded=True), "x" * 2500])
                apply = lambda: store.reject_task(target, reason)  # noqa: E731
            elif kind == "ready":
                note("op", ("ready",))
                apply = lambda: store.ready_tasks(mid)  # noqa: E731
            else:
                note("op", ("cancel_all",))
                def apply() -> Any:  # noqa: E306
                    store.set_mission_status(mid, "CANCELLED")
                    for task_id in ids:
                        if store.get_task(task_id)["status"] not in {"COMPLETED", "CANCELLED"}:
                            store.set_task_status(task_id, "CANCELLED")
            try:
                apply()
                raised: BaseException | None = None
            except DOCUMENTED_ERRORS as exc:
                raised = exc
            except Exception as exc:  # noqa: BLE001
                raise FuzzFailure(f"{kind} raised undocumented {type(exc).__name__}: {exc}") from exc
            after = _snapshot_of(store)
            if raised is not None:
                if after != before:
                    raise FuzzFailure(f"{kind} raised {type(raised).__name__} ({raised}) but changed stored state")
            elif expected_legal is False:
                raise FuzzFailure(f"{kind} on {target} should have been refused but applied")
            if raised is not None and expected_legal is True:
                raise FuzzFailure(f"{kind} on {target} should have applied but raised {type(raised).__name__}: {raised}")
            if raised is None and kind not in {"ready"} and after[3] <= events_before:
                raise FuzzFailure(f"{kind} applied but appended no event")
            # refresh the model from the store, checking it only moved along documented edges
            for row in after[1]:
                tid, new = int(row["id"]), str(row["status"])
                old = model_status.get(tid)
                if old is not None and new != old and new not in TASK_EDGES[old] and kind not in {"begin", "finish", "verify", "reject", "ready", "add", "amend", "dep", "cancel_all"}:
                    raise FuzzFailure(f"task {tid} moved {old} -> {new} outside the documented transitions via {kind}")
                model_status[tid] = new
                if tid not in ids:
                    ids.append(tid)
            model_mission = str(after[0][0]["status"])
            check_store(store, mid)
    # the human views of the final state must render too
    snapshot = bridge.status_snapshot(mid)
    bridge_format.format_status(snapshot)
    bridge_format.format_task_list(snapshot["tasks"])
    for task in snapshot["tasks"][:5]:
        bridge_format.format_task_detail(task)
    bounded = bridge_tui._bounded_snapshot(snapshot)
    bridge_format.format_events(snapshot["events"])
    del bounded


@_cli_case(20)
def target_store_text(rng: random.Random, sb: Sandbox) -> None:
    """Chaotic prompts, titles and evidence survive the store and every projection and view that shows them."""
    from .targets_ui import Audit, build_console, draw_state, screen_patches
    from .harness import fake_curses

    sb.fresh_state()
    with BridgeStore(sb.database()) as store:
        name = gen.plain_text(rng, 30).strip() or "m"
        mission = store.create_mission(name, gen.valid_text(rng, 60) or "o", cwd=sb.work, initial_prompt=gen.valid_text(rng, 60) or "first")
        mid = str(mission["id"])
        for _ in range(rng.randint(0, 6)):
            prompt = gen.valid_text(rng, 40) or "p"
            note("prompt", prompt)
            task = store.add_task(mid, prompt, title=gen.plain_text(rng, 30), acceptance=[gen.plain_text(rng, 20)])
            if rng.random() < 0.3:
                store.amend_task(task, prompt=gen.valid_text(rng, 30) or "q")
        first = store.list_tasks(mid)[0]
        if rng.random() < 0.6:
            store.set_mission_status(mid, "RUNNING")
            op = store.begin_operation(int(first["id"]), iteration=1, station="codex")
            store.mark_operation_running(op, route_id="codex", model=gen.plain_text(rng, 10))
            store.finish_operation(op, exit_code=0)
            store.verify_task(int(first["id"]), evidence=gen.valid_text(rng, 30).strip() or "e", record_only=True)
    snapshot = bridge.console_snapshot(mid)
    for task in snapshot["tasks"]:
        for field in ("title", "prompt", "original_prompt"):
            if task[field] is None:
                raise FuzzFailure(f"task {field} lost")
    bridge_format.format_status(snapshot)
    bridge_format.format_task_list(snapshot["tasks"])
    bridge_format.format_events(snapshot["events"])
    audit = Audit(rng)
    with Patcher() as patch:
        with fake_curses(patch):
            screen_patches(patch)
            state, _holder = build_console(rng, audit, full=True, snapshot=snapshot)
            for index in range(len(bridge_tui.VIEWS)):
                state.view_index = index
                for height, width in ((24, 80), gen.terminal_size(rng), (40, 140)):
                    draw_state(state, height, width)
    # the CLI prints them too
    for argv in (["status", "--mission", mid], ["task", "list", "--mission", mid], ["task", "show", "--task", "1", "--mission", mid], ["events", "--mission", mid]):
        status, out, err = run_main(argv)
        if status != 0 or "Traceback" in out + err:
            raise FuzzFailure(f"{argv} failed after chaotic text: status {status}: {(out + err)[-300:]}")
        status, out, err = run_main([*argv, "--json"])
        if status != 0:
            raise FuzzFailure(f"{argv} --json failed: {(out + err)[-300:]}")
        json.loads(out)
