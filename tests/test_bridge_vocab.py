"""Contract tests for the Bridge shared vocabulary (``bridge_vocab``) and the modules that build on it."""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

from llm_tools import bridge_draw, bridge_states, bridge_theme, bridge_tui, bridge_vocab
from llm_tools.bridge_vocab import ColorRole, EventKind, MissionStatus, OperationStatus, TaskStatus, ViewName


def test_members_behave_as_the_plain_strings_they_replace() -> None:
    assert MissionStatus.RUNNING == "RUNNING" and hash(MissionStatus.RUNNING) == hash("RUNNING")
    assert str(TaskStatus.VERIFYING) == "VERIFYING" and f"{OperationStatus.EXITED}" == "EXITED"
    assert {"RUNNING"} == {MissionStatus.RUNNING}
    assert json.dumps({"status": MissionStatus.PAUSED}) == '{"status": "PAUSED"}'
    connection = sqlite3.connect(":memory:")
    assert connection.execute("SELECT ?", (MissionStatus.STOPPED,)).fetchone() == ("STOPPED",)


def test_status_sets_are_built_from_the_enums_as_plain_strings() -> None:
    assert bridge_states.MISSION_STATES == {str(item) for item in MissionStatus}
    assert bridge_states.TASK_STATES == {str(item) for item in TaskStatus}
    assert bridge_states.OPERATION_STATES == {str(item) for item in OperationStatus}
    for name in dir(bridge_states):
        value = getattr(bridge_states, name)
        if isinstance(value, frozenset):
            assert all(type(item) is str for item in value), name
        if isinstance(value, dict) and name.endswith("_TRANSITIONS"):
            assert all(type(key) is str and all(type(item) is str for item in targets) for key, targets in value.items()), name


def test_view_names_match_the_console_views_and_every_alias_resolves() -> None:
    assert bridge_draw.VIEWS == tuple(str(view) for view in ViewName) == bridge_tui.VIEWS
    assert set(bridge_draw._VIEW_ALIASES.values()) <= set(bridge_draw.VIEWS)
    assert set(bridge_tui._VIEW_COMMANDS.values()) <= set(bridge_draw.VIEWS)
    assert bridge_tui._SELECTABLE_VIEWS <= set(bridge_draw.VIEWS)
    for view in ViewName:
        assert bridge_draw.normalize_view(str(view).lower()) == view


def test_colour_roles_match_the_theme_table() -> None:
    assert bridge_theme.ROLES == tuple(str(role) for role in ColorRole)
    assert list(bridge_theme.ROLE_DESCRIPTIONS) == list(bridge_theme.ROLES)
    assert bridge_theme.MISSION_ROLES == tuple(str(role) for role in ColorRole if str(role).startswith("mission_"))


def test_event_kinds_are_unique_kebab_case_and_the_enum_names_follow_the_values() -> None:
    values = [str(kind) for kind in EventKind]
    assert len(values) == len(set(values))
    for kind in EventKind:
        assert kind.name == str(kind).upper().replace("-", "_")
        assert str(kind) == str(kind).lower()


def test_vocab_module_imports_only_enum() -> None:
    tree = ast.parse(Path(bridge_vocab.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert imported <= {"enum", "__future__"}
