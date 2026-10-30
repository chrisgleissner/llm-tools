"""Home screen: model-pinned routes in the picker, stale/live controllers, first-run text."""
from __future__ import annotations

import curses
from datetime import timedelta

import pytest

from llm_tools import bridge, bridge_launcher as launcher, bridge_tui as tui
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_launcher import Launcher
from llm_tools.bridge_store import MissionLock

from tests import test_bridge_launcher_home as home

Grid, mission = home.Grid, home.mission


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)


ROUTES_TOML = """
[routes.claude-sonnet]
provider = "claude"
model = "claude-sonnet-5-5"

[routes.codex-fast]
provider = "codex"

[routes.kilo-glm]
provider = "kilo"
model = "zai/glm-5.2"

[routes.claude]
provider = "claude"
model = "claude-opus-4"
"""


@pytest.fixture
def route_config(tmp_path, monkeypatch):
    from llm_tools import config as toolconfig
    path = tmp_path / "config.toml"
    path.write_text(ROUTES_TOML, encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}" if name in {"codex", "claude"} else None)
    toolconfig._cache.clear()
    yield toolconfig.load_config()
    toolconfig._cache.clear()


def routed(tmp_path, **extra):
    routes = (launcher.RouteChoice("claude-sonnet", "claude", "claude-sonnet-5-5"), launcher.RouteChoice("codex-fast", "codex"))
    return Launcher(str(tmp_path), creating=True, focus=1, installed=("codex", "claude"), routes=routes,
                    providers="codex", **extra)


# --- route discovery and defaults -------------------------------------------

def test_detect_routes_keeps_installed_launch_clis_and_skips_provider_names(route_config):
    found = launcher.detect_routes(route_config)
    assert [(r.route_id, r.provider, r.model) for r in found] == [
        ("claude-sonnet", "claude", "claude-sonnet-5-5"), ("codex-fast", "codex", None)]
    assert found[0].detail == "claude · claude-sonnet-5-5" and found[1].detail == "codex"
    assert launcher.detect_routes({}) == () and launcher.detect_routes({"routes": "oops"}) == ()


def test_default_selection_prefers_configured_routes(route_config):
    routes = launcher.detect_routes(route_config)
    config = {"ralph": {"routes": ["claude-sonnet", "codex", "ghost"], "providers": ["claude"]}}
    assert launcher.default_providers(("codex", "claude"), config, routes) == "claude-sonnet,codex"
    ghost = {"ralph": {"routes": ["ghost"], "providers": ["claude"]}}
    assert launcher.default_providers(("codex", "claude"), ghost, routes) == "claude"


# --- picker layout and interaction ------------------------------------------

def test_routes_render_as_a_vertical_checklist_with_fields_below(tmp_path):
    state = routed(tmp_path)
    grid = Grid(size=(34, 120))
    launcher._draw(grid, state)
    assert grid.row(7).startswith("> Providers: rotation: codex")
    assert "(copilot, kilo, opencode, minimax not installed)" in grid.row(7)
    assert grid.row(8) == "    ▸[1] codex"
    assert grid.row(9).startswith("     [ ] claude") and "claude-sonnet" not in grid.row(9)
    assert grid.row(10).split() == ["[", "]", "claude-sonnet", "claude", "·", "claude-sonnet-5-5"]
    assert grid.row(11).split() == ["[", "]", "codex-fast", "codex"]
    assert grid.row(10).index("claude ·") == grid.row(11).rindex("codex")  # details share one column
    assert grid.row(13).startswith("  Folder:") and grid.row(15).startswith("  Mode:")
    assert grid.row(31) == "↑/↓ choose · Space toggle · Tab next field · numbers show rotation order"
    assert state.vertical is True


def test_vertical_picker_keys_walk_toggle_and_leave_at_edges(tmp_path):
    state = routed(tmp_path)
    state.key(curses.KEY_DOWN)
    state.key(" ")
    state.key(curses.KEY_DOWN)
    state.key(" ")
    assert state.focus == 1 and state.providers == "codex,claude,claude-sonnet"
    state.key(curses.KEY_DOWN)
    state.key(" ")
    assert state.providers == "codex,claude,claude-sonnet,codex-fast"
    state.key(curses.KEY_DOWN)  # past the last item the arrows stay in the checklist
    assert state.focus == 1 and state.provider_cursor == 3
    state.provider_cursor = 0
    state.key(curses.KEY_UP)  # and so they do above the first item
    assert state.focus == 1 and state.provider_cursor == 0
    state.provider_cursor = 3
    state.key("\t")  # Tab leaves the field
    assert state.focus == 2 and state.provider_cursor == 3
    state.key(curses.KEY_BTAB)  # Shift-Tab enters it on the first item, not the last
    assert state.focus == 1 and state.provider_cursor == 0
    state.key(curses.KEY_BTAB)
    assert state.focus == 0


def test_vertical_picker_scrolls_in_short_terminals(tmp_path):
    routes = tuple(launcher.RouteChoice(f"route-{i}", "codex") for i in range(12))
    state = Launcher(str(tmp_path), creating=True, focus=1, installed=("codex", "claude"), routes=routes, provider_cursor=13)
    grid = Grid(size=(24, 80))
    launcher._draw(grid, state)
    layout = state.form_layout(24, 80)
    assert layout.vertical and len(layout.items) < 14 and layout.items[-1][0] == 13
    assert layout.ys[5] <= 20  # every field still fits above the hint row
    assert "of 14" in grid.row(layout.ys[1])
    assert "route-8" in grid.text() and "route-0" not in grid.text()
    assert grid.row(21).startswith("↑/↓ choose")


def test_long_single_line_picker_falls_back_to_a_checklist(tmp_path):
    state = Launcher(str(tmp_path), creating=True, installed=launcher.PROVIDER_ORDER)
    assert state.picker_vertical(120) is False
    assert state.picker_vertical(80) is True
    grid = Grid(size=(24, 80))
    launcher._draw(grid, state)
    assert state.vertical is True and "[ ] claude" in grid.text()


def test_clicking_a_checklist_item_toggles_it(tmp_path):
    state = routed(tmp_path)
    state.width = 120
    state.click(10, height=34, toggle=True)  # claude-sonnet row
    assert state.providers == "codex,claude-sonnet" and state.provider_cursor == 1  # selected rows move up; the cursor follows
    state.click(9, height=34, toggle=False)  # a bare press only moves the cursor
    assert state.providers == "codex,claude-sonnet"
    state.click(9, height=34, activate=True)
    assert state.providers == "codex"
    state.click(7, height=34)  # the Providers label focuses the field
    assert state.focus == 1
    state.click(13, height=34)
    assert state.focus == 2
    state.objective = "o"
    assert state.click(17, height=34) == "create"


# --- creating the mission ---------------------------------------------------

def test_create_args_use_routes_only_when_a_route_is_selected(tmp_path):
    state = routed(tmp_path, objective="o")
    state.providers = "codex,claude-sonnet"
    assert state.create_args() == ["--routes", "codex,claude-sonnet", "--providers", "codex"]
    state.providers = "claude-sonnet"
    assert state.create_args() == ["--routes", "claude-sonnet"]
    state.providers = "claude,codex"
    assert state.create_args() == ["--providers", "claude,codex", "--routes="]


def test_validate_accepts_routes_and_reports_missing_launch_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/bin/{name}" if name == "codex" else None)
    state = routed(tmp_path, objective="o")
    state.providers = "codex,codex-fast"
    state.validate()
    state.providers = "claude-sonnet"
    with pytest.raises(ValueError, match="claude-sonnet needs claude, which is not installed"):
        state.validate()
    state.providers = "ghost"
    with pytest.raises(ValueError, match="Unknown provider ghost; choose from .*claude-sonnet"):
        state.validate()


def test_running_launcher_creates_a_route_mission(tmp_path, monkeypatch, route_config):
    home.prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_load_config", lambda: route_config)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex", "claude"))
    monkeypatch.setattr(launcher, "provider_installed", lambda name: name in {"codex", "claude"})
    # Providers: Down twice moves to claude-sonnet, Space selects it; Tab x3 reaches "Save mission only"; o opens it.
    keys = ["n", *"Route me", "\t", curses.KEY_DOWN, curses.KEY_DOWN, " ", "\t", "\t", "\t", "\n", "o"]
    home.Recorder(keys, monkeypatch)
    calls: list[list[str]] = []

    def dispatch(args):
        calls.append(args)
        return 0 if args[0] == "control" else bridge.main(args)

    assert launcher.run_launcher(dispatch) == 0
    assert calls[0][calls[0].index("--routes") + 1] == "codex,claude-sonnet"
    with bridge.open_store() as store:
        config = store.list_missions()[0]["config"]
    assert config["routes"] == "codex,claude-sonnet" and config["providers"] == "codex"


def test_bare_provider_selection_clears_a_configured_default_rotation(tmp_path, monkeypatch):
    config = tmp_path / "default.toml"
    config.write_text('[ralph]\nroutes = ["claude-sonnet"]\n[routes.claude-sonnet]\nprovider = "claude"\n', encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    status, _calls, _ = home.run_with(lambda args: 0, tmp_path, monkeypatch, home.start_keys(["\x04"]))
    assert status == 0
    with bridge.open_store() as store:
        created = store.list_missions()[0]["config"]
    assert created["routes"] == "" and created["providers"] == "codex"


# --- stale and live controllers ---------------------------------------------

def test_controller_state_is_probed_once_per_interval_and_only_for_live_statuses():
    calls = []
    state = Launcher("/w", probe=lambda ident: calls.append(ident) or ident == "alive")
    running, stale, done = mission(ident="alive"), mission(ident="dead"), mission(status="COMPLETED", ident="done")
    assert [state.controller_state(m) for m in (running, stale, done)] == ["live", "stale", None]
    state.controller_state(running)
    state.controller_state(stale)
    assert calls == ["alive", "dead"]
    state.probe_ttl = 0
    state.controller_state(running)
    assert calls == ["alive", "dead", "alive"]
    state.probe = lambda ident: None
    assert state.controller_state(stale) is None

    def boom(ident):
        raise RuntimeError("boom")
    state.probe = boom
    assert state.controller_state(stale) is None


def test_stale_running_mission_is_marked_and_explained(tmp_path):
    items = [mission("Dead run", "RUNNING", "/work/a", timedelta(hours=5), "dddddddd1111"),
             mission("Live run", "RUNNING", "/work/a", timedelta(minutes=1), "eeeeeeee2222"),
             mission("Finished", "COMPLETED", "/work/a", timedelta(days=1), "ffffffff3333")]
    state = Launcher(str(tmp_path), missions=items, selected=1, installed=("codex",),
                     probe=lambda ident: ident.startswith("eeee"))
    grid = Grid()
    launcher._draw(grid, state)
    assert "RUNNING   stale" in grid.row(6) and "Dead run" in grid.row(6) and "5h ago" in grid.row(6)
    assert "stale" not in grid.row(7) and "stale" not in grid.row(8)
    assert grid.row(32) == "controller not running · Enter opens controls, then type resume"
    state.selected = 2
    grid = Grid()
    launcher._draw(grid, state)
    assert grid.row(32) == "Enter opens controls · controller is running"
    # Columns stay aligned whether or not the marker column is present.
    cells = {cell_width(grid.row(y)[:grid.row(y).index(" ago")]) for y in (6, 7, 8)}
    assert len(cells) == 1


def test_unknown_liveness_is_not_reported_as_stale(tmp_path):
    state = Launcher(str(tmp_path), missions=[mission()], installed=("codex",), selected=1, probe=lambda ident: None)
    grid = Grid()
    launcher._draw(grid, state)
    assert "stale" not in grid.text() and grid.row(32) == "Enter opens controls"


def test_status_roles_for_live_and_stale_rows_use_ok_and_warn(tmp_path, monkeypatch):
    roles: dict[str, str] = {}
    monkeypatch.setattr(tui, "role_attr", lambda role: roles.setdefault(role, len(roles)) or 0)

    class Recording(Grid):
        def addnstr(self, y, x, text, count, *attrs):
            seen[(y, text.strip())] = attrs[0] if attrs else 0
            super().addnstr(y, x, text, count, *attrs)

    seen: dict[tuple[int, str], int] = {}
    here = str(tmp_path)
    items = [mission("a", "RUNNING", here, ident="alive111"), mission("b", "RUNNING", here, ident="dead2222")]
    state = Launcher(here, missions=items, installed=("codex",), probe=lambda ident: ident.startswith("alive"))
    launcher._draw(Recording(), state)
    assert seen[(6, "● RUNNING")] == roles["state_running"]
    assert seen[(7, "● RUNNING")] == roles["warn"] and seen[(7, "stale")] == roles["warn"]


def test_detail_line_shows_route_rotation():
    item = mission(providers="codex,claude")
    assert launcher.mission_detail(item, 80).endswith(" · 01234567 · codex → claude")
    item["config"]["routes"] = "claude-sonnet,codex"
    assert launcher.mission_detail(item, 80).endswith(" · claude-sonnet → codex")


def test_controller_running_sees_a_real_lock_without_keeping_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(mode=0o700)
    assert launcher.controller_running("nolock") is False  # never had a controller; nothing is created
    assert not (tmp_path / "state" / "locks").exists()
    assert launcher.controller_running("bad/id") is None
    holder = MissionLock(tmp_path / "state" / "locks", "mine")
    holder.acquire()
    holder.release()
    assert launcher.controller_running("mine") is False
    holder.acquire()
    try:
        assert launcher.controller_running("mine") is True
    finally:
        holder.release()
    assert launcher.controller_running("mine") is False  # the probe released its own lock


# --- first run --------------------------------------------------------------

def test_empty_state_wraps_on_narrow_terminals(tmp_path):
    grid = Grid(size=(24, 70))
    launcher._draw(grid, Launcher(str(tmp_path), installed=("codex",)))
    assert grid.row(7).startswith("A mission is a goal plus ordered tasks")
    assert "N to create one" in grid.row(8)
