"""Golden-screen regression harness for LLM Bridge.

Each case renders one screen through the production draw code onto a recording fake window
(``tests/fakes.py``) and compares the result with a checked-in golden file in
``tests/golden/screens/``.  A golden file holds the text of every row and, per row, the list
of ``[column, text, role]`` runs, so a refactor that is meant to leave the UI unchanged
proves it by changing no golden file.

Cases are the product of

* screens   - the eight console views, the console overlays (help, approve confirmation,
              response pager, document viewer, composer), the home screen (list, new-mission
              form with the provider checklist, saved confirmation) and the mission picker;
* sizes     - 80x24, 100x30 and 140x40 (columns x rows);
* glyphs    - UTF-8 and ASCII (the C locale, via the same codeset switch Bridge itself reads).

The role -> style mapping of the ``dark`` and ``mono`` themes is checked once for every role
in ``_themes.json``; it does not depend on the screen, so it is not repeated per case.

Determinism: the store is seeded with a fixed clock, fixed mission and operation ids; wall
clock, time zone, HOME and the state and working directories are pinned (see ``Env``); tmp
paths are rewritten to fixed ones in the projected snapshot before the console sees it; no
tmux, no sleeping, no live provider.  Run with ``UPDATE_GOLDEN=1`` after an intended UI change
and review the diff of ``tests/golden/screens/``.
"""

from __future__ import annotations

import copy
import curses
import datetime as dt
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from llm_tools import (
    bridge_documents,
    bridge_format,
    bridge_glyphs,
    bridge_launcher,
    bridge_monitor,
    bridge_store,
    bridge_switcher,
    bridge_theme,
    bridge_tui,
)
from llm_tools.bridge_store import BridgeStore

from . import golden_screens
from .fakes import FakeScreen, RoleAttrs, ScriptExhausted

NOW = 2_000_000_000  # 2033-05-18T03:33:20Z
MISSION_ID = "9a3c51e07d2b4f68a1c0e5b7d3f24861"
FIXED_HOME = "/home/golden"
FIXED_CWD = f"{FIXED_HOME}/projects/release"
FIXED_STATE = f"{FIXED_HOME}/.local/state/llm-tools/llm-bridge"
SIZES = ((24, 80), (30, 100), (40, 140))  # (rows, columns)
GLYPH_MODES = ("utf8", "ascii")
CODESETS = {"utf8": "UTF-8", "ascii": "ANSI_X3.4-1968"}
THEMES = ("dark", "mono")


# --- deterministic environment ---------------------------------------------------------------------


class _FixedTime:
    """Stands in for the ``time`` module inside Bridge modules: ``time()`` is the fixed epoch."""

    def time(self) -> float:
        return float(NOW)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _Uuids:
    """Counter-based replacement for the ``uuid`` module used by the store (ids are shown in the UI)."""

    def __init__(self) -> None:
        self.count = 0

    def uuid4(self) -> uuid.UUID:
        self.count += 1
        return uuid.UUID(int=((0x5EED0000 + self.count) << 96) | 0x4F68A1C0E5B7D3F2)

    def __getattr__(self, name: str) -> Any:
        return getattr(uuid, name)


@pytest.fixture(scope="module")
def seeded_snapshot(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The projection of a seeded mission, with tmp paths replaced by fixed ones.  Built once per module."""
    root = tmp_path_factory.mktemp("golden-seed")
    state_dir = root / "state"
    state_dir.mkdir(mode=0o700)
    os.chmod(state_dir, 0o700)
    cwd = root / "release"
    cwd.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        _pin_clock(patch)
        patch.setenv("LLM_BRIDGE_STATE_DIR", str(state_dir))
        with BridgeStore(state_dir / "bridge.sqlite3") as store:
            _seed(store, cwd)
            snapshot = bridge_monitor.project_snapshot(store, MISSION_ID, task_limit=200)
        # The console adds these to its projection (see bridge.status_snapshot).
        snapshot["lifecycle"]["controller_running"] = True
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    text = text.replace(str(cwd), FIXED_CWD).replace(str(state_dir), FIXED_STATE).replace(str(root), FIXED_HOME)
    assert str(root) not in text
    return json.loads(text)


def _pin_clock(patch: pytest.MonkeyPatch) -> None:
    """Fixed store timestamps (one second apart), fixed ids, fixed ``time.time()`` in the projections."""
    clock = {"epoch": NOW - 1500}

    def utc_now() -> str:
        clock["epoch"] += 5
        return dt.datetime.fromtimestamp(clock["epoch"], dt.timezone.utc).isoformat(timespec="seconds")

    patch.setattr(bridge_store, "utc_now", utc_now)
    patch.setattr(bridge_store, "uuid", _Uuids())
    for module in (bridge_monitor, bridge_format, bridge_launcher, bridge_switcher, bridge_tui):
        patch.setattr(module, "time", _FixedTime(), raising=False)


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


def _run_operation(store: BridgeStore, task_id: int, station: str, model: str, exit_code: int | None) -> str:
    """Prepare and run an operation; finish it with ``exit_code`` unless that is ``None`` (left running)."""
    operation = store.begin_operation(task_id, iteration=1, station=station)
    store.mark_operation_running(operation, pane_id="%3", process_pid=4242, process_start="boot-1", route_id=station, model=model)
    if exit_code is not None:
        store.finish_operation(operation, exit_code=exit_code, logs_path=f"{FIXED_STATE}/logs/{operation[:8]}")
    return operation


def _seed(store: BridgeStore, cwd: Path) -> None:
    mission = store.create_mission(
        "Release 2.0 hardening",
        "Harden the 2.0 release: licences, flaky tests, CI matrix, migration guide and notes.",
        cwd=cwd,
        mission_id=MISSION_ID,
        config={"providers": "codex,claude", "viewed_station": "codex"},
    )
    mid = mission["id"]
    licences = store.add_task(mid, "# Audit dependency licences\n\nList every dependency and its licence.", title="Audit dependency licences")
    flaky = store.add_task(mid, "# Fix the flaky login test\n\nThe test fails one run in ten.", title="Fix the flaky login test", priority=2)
    guide = store.add_task(
        mid,
        "# Write the migration guide\n\nExplain how to move from 1.x to 2.0.\n\n- Config keys that were renamed\n- Removed commands\n"
        "- A worked upgrade example\n\n```sh\nllm-bridge migrate --dry-run\n```\n\nKeep it under two pages.",
        title="Write the migration guide",
        acceptance=["Guide covers every renamed key", "Example runs on a clean checkout"],
    )
    matrix = store.add_task(mid, "# Update the CI matrix\n\nAdd Python 3.13 and drop 3.9.", title="Update the CI matrix", priority=1)
    notes = store.add_task(mid, "# Publish the release notes\n\nSummarise the changes since 1.9.", title="Publish the release notes", priority=1, depends_on=[matrix])
    keys = store.add_task(mid, "# Rotate the signing keys", title="Rotate the signing keys")
    legacy = store.add_task(mid, "# Remove the legacy exporter", title="Remove the legacy exporter")
    store.add_task(mid, "Reconcile the changelog.", title="Reconcile the changelog with the tracker for every closed milestone since the 1.4 branch")
    store.add_task(mid, "Translate the README.", title="文档更新: translate the README")
    store.set_mission_status(mid, "RUNNING")

    op = _run_operation(store, licences, "codex", "gpt-5", 0)
    store.verify_task(licences, evidence="Licence report attached; no GPL findings.", approved=True)
    store.record_operation_session(op, restoration_state="NATIVE_CREATED", provider="codex", session_id="sess-0001", cwd=str(cwd), source="provider-file")
    _run_operation(store, flaky, "claude", "sonnet-4", 1)
    store.record_runtime_event(mid, "provider-handoff", {"exit_code": 1, "failure_count": 1, "max_failures": 3})
    op_guide = _run_operation(store, guide, "codex", "gpt-5", 0)
    _run_operation(store, keys, "claude", "sonnet-4", 75)
    store.set_task_status(legacy, "CANCELLED")
    store.propose_task(mid, "Add a smoke test for the installer.", source_operation_id=op_guide, title="Add an installer smoke test")
    _run_operation(store, matrix, "codex", "gpt-5", None)
    store.amend_task(matrix, prompt="# Update the CI matrix\n\nAdd Python 3.13, drop 3.9 and keep 3.10 as the floor.")

    store.set_rotation(mid, providers="codex,claude", routes="", source="operator")
    store.record_runtime_event(mid, "provider-wait", {"target_epoch": NOW + 1500, "reason": "claude 5h window exhausted"})
    store.record_runtime_event(mid, "provider-selection", {
        "provider": "codex", "route": "codex", "selected_model": "gpt-5", "rotation_reason": "preferred",
        "observed_at": _iso(NOW - 12),
        "decision": {"usable": True},
        "decisions": [
            {"route": "codex", "provider": "codex", "selected_model": "gpt-5", "windows": [
                {"name": "5h", "kind": "reset_window", "remaining_percent": 62.0, "reset_epoch": NOW + 3 * 3600 + 10 * 60},
                {"name": "weekly", "kind": "reset_window", "remaining_percent": 31.0, "reset_epoch": NOW + 4 * 86400},
            ]},
            {"route": "claude", "provider": "claude", "selected_model": "sonnet-4", "windows": [
                {"name": "5h", "kind": "reset_window", "remaining_percent": 8.0, "reset_epoch": NOW + 1500},
                {"name": "weekly", "kind": "reset_window", "remaining_percent": 55.0, "reset_epoch": NOW + 5 * 86400},
            ]},
        ],
    })
    store.record_runtime_event(mid, "controller-heartbeat", {
        "pid": 4242, "controller_state": "ACTIVE", "owner_lock_held": True,
        "desired_mission_status": "RUNNING", "observed_at": _iso(NOW - 2),
    })


_USAGE_WIDTHS = (8, 5, 7, 15, 19, 14, 10)


def _usage_line(*cells: str) -> str:
    return "   ".join(cell.ljust(width) for cell, width in zip(cells, _USAGE_WIDTHS)).rstrip()


USAGE_TEXT = "\n".join([
    "LLM Usage · 03:33",
    "",
    "Bars: quota rows █ available · ░ spent",
    "Guidance: 5h rows forecast runout; weekly rows show pace.",
    "",
    _usage_line("Provider", "Ready", "Scope", "Remaining", "Guidance", "Remaining Time", "Resets in"),
    _usage_line(*("─" * width for width in _USAGE_WIDTHS)),
    _usage_line("Codex", "yes", "5h", " 62% ██████░░░░", "✓ on pace", "3h 10m", "3h 10m"),
    _usage_line("", "", "weekly", " 31% ███░░░░░░░", "↓ conserve", "2d 4h", "4d 0h"),
    _usage_line("Claude", "no", "5h", "  8% █░░░░░░░░░", "! empty in 12m", "12m", "25m"),
    _usage_line("", "", "weekly", " 55% █████░░░░░", "✓ on pace", "3d 1h", "5d 0h"),
]) + "\n"


def _usage_block() -> dict[str, Any]:
    return {"status": "ready", "age_seconds": 12, "error": "", "text": USAGE_TEXT, "rows": [
        {"provider": "codex", "model": "gpt-5", "scope": "5h", "remaining_percent": 62.0},
        {"provider": "claude", "model": "sonnet-4", "scope": "5h", "remaining_percent": 8.0},
    ]}


# --- per-case context ------------------------------------------------------------------------------


@dataclass
class Case:
    """Everything a screen builder needs for one (size, glyph mode) combination."""

    rows: int
    columns: int
    glyphs: str
    patch: pytest.MonkeyPatch
    root: Path
    seed: dict[str, Any]

    @property
    def unicode(self) -> bool:
        return self.glyphs == "utf8"

    def screen(self, keys: tuple[object, ...] = ()) -> FakeScreen:
        screen = FakeScreen(self.rows, self.columns, keys=keys)
        screen.install(self.patch, wrapper=True)
        return screen

    def console_state(self, view: str = "Overview") -> bridge_tui._ConsoleState:
        state = bridge_tui._ConsoleState(MISSION_ID, lambda *_: "ok", lambda *_a, **_k: {}, full=True)
        snapshot = copy.deepcopy(self.seed)
        snapshot["usage"] = _usage_block()
        state.snapshot = bridge_tui._bounded_snapshot(snapshot)
        state.wall_clock = lambda: float(NOW)
        state.status_clock = lambda: 0.0
        state.choose_view(bridge_tui.VIEWS.index(view))
        return state


def _draw_console(screen: FakeScreen, state: bridge_tui._ConsoleState) -> FakeScreen:
    """The draw dispatch of the console loop: response overlay, else document, else the full view."""
    if state.response_overlay:
        bridge_tui._draw_response_overlay(screen, state)
    elif state.document_viewer is not None:
        bridge_tui._draw_document(screen, state)
    else:
        bridge_tui._draw_full(screen, state)
    return screen


@pytest.fixture(autouse=True)
def _deterministic(tmp_path: Path) -> Iterator[None]:
    """Pin everything a screen can depend on: clock, time zone, HOME, terminal env, state directory."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_USAGE_NOW_EPOCH", str(NOW))
        patch.setenv("TZ", "UTC")
        time.tzset()
        patch.setenv("HOME", FIXED_HOME)
        for name in ("TMUX", "TMUX_PANE", "NO_COLOR", "LLM_USAGE_NO_COLOR", "LLM_BRIDGE_THEME", "VISUAL", "EDITOR"):
            patch.delenv(name, raising=False)
        patch.setenv("TERM", "xterm-256color")
        patch.setenv("ESCDELAY", "25")  # run_picker does os.environ.setdefault on it
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)
        os.chmod(state_dir, 0o700)
        patch.setenv("LLM_BRIDGE_STATE_DIR", str(state_dir))
        for module in (bridge_monitor, bridge_format, bridge_launcher, bridge_switcher, bridge_tui):
            patch.setattr(module, "time", _FixedTime(), raising=False)
        patch.setattr(bridge_tui, "LAST_RECOVERY_DRAFT", None, raising=False)  # compose_document assigns it
        yield
    time.tzset()  # the environment is restored now


def _set_glyphs(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(bridge_glyphs, "_locale_codeset", lambda: CODESETS[mode])
    bridge_glyphs.ascii_mode.cache_clear()


# --- console screens -------------------------------------------------------------------------------


def _console_view(view: str, keys: tuple[str, ...] = ()) -> Callable[[Case], FakeScreen]:
    def build(case: Case) -> FakeScreen:
        state = case.console_state(view)
        for key in keys:
            state.handle_key(key)
        return _draw_console(case.screen(), state)

    return build


def _overlay_help(case: Case) -> FakeScreen:
    state = case.console_state("Tasks")
    state.handle_key("?")
    return _draw_console(case.screen(), state)


def _overlay_approve(case: Case) -> FakeScreen:
    state = case.console_state("Tasks")
    assert state._confirm_or_dispatch("approve 3")
    assert state.confirm_command is not None
    return _draw_console(case.screen(), state)


def _overlay_pager(case: Case) -> FakeScreen:
    state = case.console_state("Tasks")
    state.output.extend(
        [f"Task 3 · Write the migration guide · revision 2 · line group {group}" for group in range(1, 9)]
        + ["", "A long line that has to wrap inside the response pager so that the wrapped continuation rows are part of the golden file: " + "word " * 30]
        + [f"evidence {index}: {'ok ' * (index % 7 + 2)}" for index in range(1, 40)]
    )
    state.response_overlay = True
    state.response_scroll = 6
    return _draw_console(case.screen(), state)


DOCUMENT_TEXT = """# Task 3 prompt (revision 2)

Explain how to move from **1.x** to `2.0`.

## Renamed keys

| old | new |
|-----|-----|
| `providers` | `rotation` |

- Config keys that were renamed
- Removed commands
  - `llm-bridge legacy-export`
- A worked upgrade example

```sh
llm-bridge migrate --dry-run
```

> Keep it under two pages: """ + "readers skim, so lead with the commands. " * 6 + "\n\n" + "\n".join(f"Paragraph {index}: the quick brown fox jumps over the lazy dog." for index in range(1, 30)) + "\n"


def _stub_markdown(source: str, width: int, wrap: bool = True) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Deterministic stand-in for the Rich renderer (its output differs between Rich versions).

    Headings are bold (``brand``), code and quotes are ``secondary``, the rest is ``info``;
    lines are wrapped to ``width`` the way the viewer wraps rendered text.
    """
    import textwrap

    rows: list[tuple[tuple[str, str], ...]] = []
    for line in source.split("\n"):
        role = "brand" if line.startswith("#") else "secondary" if line.startswith((">", "    ", "```", "|")) else "info"
        text = line.lstrip("# ") if role == "brand" else line
        pieces = textwrap.wrap(text, max(20, width), drop_whitespace=False) if wrap and text else [text]
        rows.extend(((piece, role),) for piece in pieces or [""])
    return tuple(rows)


def _overlay_document(rendered: bool) -> Callable[[Case], FakeScreen]:
    def build(case: Case) -> FakeScreen:
        case.patch.setattr(bridge_documents, "render_markdown_segments", _stub_markdown)
        state = case.console_state("Tasks")
        state.document_viewer = bridge_documents.DocumentViewer("Task 3 r2 prompt", DOCUMENT_TEXT, rendered=rendered)
        state.document_viewer.top = 2
        return _draw_console(case.screen(), state)

    return build


def _overlay_composer(case: Case) -> FakeScreen:
    initial = "# Add retry logic\n\nAcceptance:\n- tests pass\n- no new dependencies\n"
    typed = (curses.KEY_UP, curses.KEY_END, " ", "(", "d", "r", "a", "f", "t", ")", "\n", "H", "é", "文")
    screen = case.screen(keys=typed)
    try:
        bridge_tui.compose_document(initial, title="Add task prompt", window=screen)
    except ScriptExhausted:
        pass
    for thread in threading.enumerate():
        if thread.name == "llm-bridge-composer-checkpoint":
            thread.join(5)
    return screen


# --- home screen and mission picker ----------------------------------------------------------------


def _home_missions() -> list[dict[str, Any]]:
    def mission(index: int, name: str, status: str, folder: str, age: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "id": f"{index:08x}7d2b4f68a1c0e5b7d3f2{index:04x}", "name": name, "status": status, "cwd": f"{FIXED_HOME}/{folder}",
            "updated_at": _iso(NOW - age), "created_at": _iso(NOW - age - 600), "objective": name, "config": config or {"providers": "codex,claude"},
        }

    return [
        mission(0xA1, "Release 2.0 hardening", "RUNNING", "projects/release", 40),
        mission(0xB2, "Port the exporter to the new API", "PAUSED", "projects/exporter", 3 * 3600),
        mission(0xC3, "Investigate the memory leak in the sampler service", "FAILED", "work/sampler", 26 * 3600),
        mission(0xD4, "Docs pass", "CREATED", "projects/docs", 9 * 60, {"providers": "claude"}),
        mission(0xE5, "Dependency bumps", "COMPLETED", "projects/release", 5 * 86400),
        mission(0xF6, "Prototype the tmux station", "CANCELLED", "play/station", 40 * 86400, {"routes": "kilo-zai,codex"}),
    ]


def _launcher(case: Case, **changes: Any) -> bridge_launcher.Launcher:
    missions = _home_missions()
    ordered = bridge_launcher.order_missions(missions, f"{FIXED_HOME}/projects/release")
    stats = {
        missions[0]["id"]: bridge_launcher.MissionStats(3, 9, "codex", 3),
        missions[1]["id"]: bridge_launcher.MissionStats(1, 4, "claude"),
        missions[2]["id"]: bridge_launcher.MissionStats(0, 2, "codex"),
        missions[3]["id"]: bridge_launcher.MissionStats(0, 0),
        missions[4]["id"]: bridge_launcher.MissionStats(6, 6, "claude"),
        missions[5]["id"]: bridge_launcher.MissionStats(0, 1, "kilo-zai"),
    }
    live = {missions[0]["id"]: True, missions[1]["id"]: False, missions[2]["id"]: False}
    state = bridge_launcher.Launcher(
        cwd=f"{FIXED_HOME}/projects/release",
        missions=ordered,
        stats=stats,
        installed=("codex", "claude", "copilot", "kilo", "opencode"),
        routes=(bridge_launcher.RouteChoice("kilo-zai", "kilo", "zai/glm-5.2"), bridge_launcher.RouteChoice("codex-spark", "codex", "gpt-5-spark")),
        probe=lambda mission_id: live.get(mission_id),
    )
    state.theme_hint = ""
    for name, value in changes.items():
        setattr(state, name, value)
    return state


def _draw_launcher(case: Case, state: bridge_launcher.Launcher) -> FakeScreen:
    screen = case.screen()
    bridge_launcher._draw(screen, state)
    return screen


def _home_list(case: Case) -> FakeScreen:
    return _draw_launcher(case, _launcher(case, selected=2))


def _home_form(case: Case) -> FakeScreen:
    state = _launcher(
        case,
        creating=True,
        objective="# Ship the 2.1 patch\n\nFix the exporter crash.\nCheck the migration guide once more.",
        providers="codex,kilo-zai",
        directory=f"{FIXED_HOME}/projects/release",
        native=True,
        provider_cursor=1,
    )
    state.cursors = [len(state.objective), 0, len(state.directory)]
    state.focus = 1  # row 1 is the (blank) providers row of the form
    return _draw_launcher(case, state)


def _home_saved(case: Case) -> FakeScreen:
    missions = _home_missions()
    saved = bridge_launcher.order_missions(missions, f"{FIXED_HOME}/projects/release")[2]
    state = _launcher(case, selected=3)  # "Docs pass", a freshly saved mission without a controller note
    state.notice = f"Saved {str(saved['id'])[:8]} {bridge_glyphs.glyph('sep')} S start {bridge_glyphs.glyph('sep')} o controls"
    state.notice_id = str(state.view()[state.selected - 1]["id"])
    return _draw_launcher(case, state)


def _picker_entries() -> list[bridge_switcher.MissionEntry]:
    def entry(index: int, name: str, status: str, folder: str, provider: str, **changes: Any) -> bridge_switcher.MissionEntry:
        base = dict(
            mission_id=f"{index:08x}7d2b4f68a1c0e5b7d3f2{index:04x}", name=name, status=status, cwd=f"{FIXED_HOME}/{folder}",
            provider=provider, rotation="codex,claude", updated_at=_iso(NOW - 60),
        )
        base.update(changes)
        return bridge_switcher.MissionEntry(**base)

    return [
        entry(0xA1, "Release 2.0 hardening", "RUNNING", "projects/release", "codex", task_id=4, task_ordinal=4, task_status="RUNNING", task_title="Update the CI matrix", controller=True, station=True),
        entry(0xB2, "Port the exporter to the new API", "RUNNING", "projects/exporter", "claude", task_id=9, task_ordinal=2, task_status="VERIFYING", task_title="Check parity", controller=True, review_ordinal=2),
        entry(0xC3, "Investigate the memory leak in the sampler service", "RUNNING", "work/sampler", "codex", task_id=11, task_ordinal=1, task_status="RUNNING", task_title="Reproduce"),
        entry(0xD4, "Docs pass", "PAUSED", "projects/docs", "claude", controller=True),
        entry(0xE5, "Dependency bumps", "COMPLETED", "projects/release", "claude", station=True),
        entry(0xF6, "Prototype the tmux station", "CANCELLED", "play/station", "-", rotation=""),
        entry(0x17, "Quarterly report", "CREATED", "docs/report", "-"),
        entry(0x28, "Fix the flaky login test", "FAILED", "projects/release", "codex"),
    ]


def _mission_picker(case: Case) -> FakeScreen:
    screen = case.screen(keys=("KEY_DOWN", "KEY_DOWN"))
    entries = _picker_entries()
    case.patch.setattr(bridge_switcher, "enable_mouse", lambda: None)
    try:
        bridge_switcher.run_picker(entries, current=entries[0].mission_id, unicode=case.unicode)
    except ScriptExhausted:
        pass
    return screen


# --- case table ------------------------------------------------------------------------------------

SCREENS: dict[str, Callable[[Case], FakeScreen]] = {
    "console-overview": _console_view("Overview"),
    "console-tasks": _console_view("Tasks", ("KEY_DOWN",) * 4),  # list plus the preview of task 3
    "console-providers": _console_view("Providers"),
    "console-activity": _console_view("Activity"),
    "console-sessions": _console_view("Sessions"),
    "console-health": _console_view("Health"),
    "console-actions": _console_view("Actions"),
    "console-usage": _console_view("Usage"),
    "overlay-help": _overlay_help,
    "overlay-approve-confirm": _overlay_approve,
    "overlay-response-pager": _overlay_pager,
    "overlay-document-source": _overlay_document(False),
    "overlay-document-rendered": _overlay_document(True),
    "overlay-composer": _overlay_composer,
    "home-list": _home_list,
    "home-form": _home_form,
    "home-saved": _home_saved,
    "mission-picker": _mission_picker,
}


def case_name(screen: str, rows: int, columns: int, glyphs: str) -> str:
    return f"{screen}.{columns}x{rows}.{glyphs}"


CASES = [(screen, rows, columns, glyphs) for screen in SCREENS for rows, columns in SIZES for glyphs in GLYPH_MODES]


@pytest.mark.parametrize("screen,rows,columns,glyphs", CASES, ids=[case_name(*case) for case in CASES])
def test_golden_screen(screen: str, rows: int, columns: int, glyphs: str, seeded_snapshot: dict[str, Any],
                       monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_glyphs(monkeypatch, glyphs)
    case = Case(rows, columns, glyphs, monkeypatch, tmp_path, seeded_snapshot)
    window = SCREENS[screen](case)
    name = case_name(screen, rows, columns, glyphs)
    frame = window.frame()
    leaked = [row for row in frame.rows if str(tmp_path) in row or "/tmp/" in row]
    assert not leaked, f"a temporary path reached the screen (normalise it in the seed): {leaked[0]}"
    assert any(row for row in frame.rows), "nothing was drawn"
    message = golden_screens.check(name, frame)
    if message is not None:
        pytest.fail(message + f"\n\nIf the change is intended, review it and run:  {golden_screens.UPDATE_ENV}=1 python -m pytest tests/test_bridge_golden_screens.py", pytrace=False)


def test_every_golden_file_belongs_to_a_case() -> None:
    """A renamed or removed case must not leave its golden file behind."""
    expected = {case_name(*case) for case in CASES} | {"_themes"}
    stale = golden_screens.stale_goldens(expected)
    if golden_screens.updating():
        golden_screens.remove_stale(stale)
        return
    assert not stale, "stale golden files (delete them or run with UPDATE_GOLDEN=1): " + ", ".join(path.name for path in stale)


# --- themes ----------------------------------------------------------------------------------------


def _theme_table(monkeypatch: pytest.MonkeyPatch, theme: str) -> dict[str, str]:
    """Role -> appearance, through the real ``initialize_colors`` / ``role_attr`` on a stub curses."""
    owner = __import__(bridge_tui.initialize_colors.__module__, fromlist=["x"])
    pairs: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: pairs.__setitem__(pair, (fg, bg)))
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 512, raising=False)
    for name in ("_ROLE_ATTRS", "_ROLE_PAIRS", "_CURRENT_THEME"):  # restored when the test ends
        monkeypatch.setattr(owner, name, getattr(owner, name))
    assert bridge_tui.initialize_colors({"TERM": "xterm-256color", "LLM_BRIDGE_THEME": theme}, is_tty=True)
    table: dict[str, str] = {}
    flags = ((curses.A_BOLD, "bold"), (curses.A_DIM, "dim"), (curses.A_UNDERLINE, "underline"), (curses.A_REVERSE, "reverse"))
    for role in (*bridge_theme.ROLES, "secondary", "info.quiet", "state_failed.quiet"):
        attr = bridge_tui.role_attr(role)
        pair = (attr >> 8) & 0x1FFF
        fg, bg = pairs.get(pair, (None, None))
        words = [] if pair == 0 else [f"fg={'default' if fg == -1 else fg}", f"bg={'default' if bg == -1 else bg}"]
        words += [label for flag, label in flags if attr & flag]
        table[role] = " ".join(words) or "plain"
    return table


def test_theme_role_styles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The role -> style table of the ``dark`` and ``mono`` themes (what turns a recorded role into colour)."""
    record = {theme: _theme_table(monkeypatch, theme) for theme in THEMES}
    path = golden_screens.GOLDEN_DIR / "_themes.json"
    lines = ["{"]
    for index, (theme, table) in enumerate(record.items()):
        lines.append(f"  {json.dumps(theme)}: {{")
        items = list(table.items())
        lines.extend(f"    {json.dumps(role)}: {json.dumps(look)}{',' if i < len(items) - 1 else ''}" for i, (role, look) in enumerate(items))
        lines.append("  }" + ("," if index < len(record) - 1 else ""))
    lines.append("}")
    text = "\n".join(lines) + "\n"
    if golden_screens.updating():
        golden_screens.GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no {path.name}; run {golden_screens.UPDATE_ENV}=1"
    stored = json.loads(path.read_text(encoding="utf-8"))
    changes = [
        f"{theme}.{role}: {stored.get(theme, {}).get(role)!r} -> {look!r}"
        for theme, table in record.items() for role, look in table.items() if stored.get(theme, {}).get(role) != look
    ] + [f"{theme}.{role}: removed" for theme, table in stored.items() for role in table if role not in record.get(theme, {})]
    assert not changes, "theme styles changed:\n" + "\n".join(changes) + f"\n\nIf intended, run {golden_screens.UPDATE_ENV}=1"


def test_role_decoder_round_trips() -> None:
    """The fake window's attribute decoding is exact for roles, quiet roles and extra flags."""
    roles = RoleAttrs()
    for role, attr in roles.table.items():
        assert roles.decode(attr) == (role, ())
        assert roles.decode(attr & ~curses.A_BOLD) == (role + ".quiet", ())
        assert roles.decode(attr | curses.A_UNDERLINE) == (role, ("underline",))
    assert roles.decode(0) == ("none", ())
