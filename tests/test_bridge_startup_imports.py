"""Short Bridge commands must not import the heavy modules they do not use.

Each case runs ``llm_tools.bridge.main`` in a fresh interpreter and lists the modules it loaded. The test
checks which modules are absent, never how long the command took.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_DRIVER = """
import contextlib, io, json, sys
baseline = set(sys.modules)
from llm_tools import bridge
sink = io.StringIO()
with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
    try:
        status = bridge.main(json.loads(sys.argv[1]))
    except SystemExit as exc:
        status = exc.code
sys.stdout.write(json.dumps({"status": status, "loaded": sorted(set(sys.modules) - baseline)}))
"""

# Modules a command that only reads the mission store must not load.
_NEVER = {
    "llm_tools.common",
    "llm_tools.config",
    "llm_tools.capacity",
    "llm_tools.bridge_theme",
    "llm_tools.terminal",
    "llm_tools.bridge_runtime",
    "llm_tools.bridge_switcher",
    "llm_tools.bridge_tui",
    "llm_tools.bridge_launcher",
    "llm_tools.ralph_robin",
    "llm_tools.scheduler",
    "curses",
    "importlib.metadata",
    "zipfile",
    "tempfile",
    "urllib.parse",
    "difflib",
}
_NO_STORE_ENGINE = {"sqlite3"}
_NO_PARSER = {"argparse", "llm_tools.bridge_help"}


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    directory = tmp_path_factory.mktemp("startup-state")
    directory.chmod(0o700)
    work = tmp_path_factory.mktemp("startup-work")
    env = {
        **os.environ,
        "LLM_BRIDGE_STATE_DIR": str(directory),
        "PYTHONPATH": str(ROOT),
        "LLM_TOOLS_NO_INHIBIT": "1",
        "STARTUP_TEST_WORK": str(work),
    }
    result = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", "mission", "create", "--prompt", "startup probe", "--providers", "claude", "--headless", "--cwd", str(work), "--json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)["mission"]["name"]
    return env


def _modules(env: dict[str, str], *arguments: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, json.dumps(list(arguments))],
        cwd=env["STARTUP_TEST_WORK"], env=env, capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert report["status"] in (0, None), (arguments, report)
    return set(report["loaded"])


def test_version_loads_neither_parser_nor_store_engine(state: dict[str, str]) -> None:
    loaded = _modules(state, "--version")
    assert not (_NEVER | _NO_STORE_ENGINE | _NO_PARSER) & loaded


@pytest.mark.parametrize(
    "arguments",
    [
        ("status",),
        ("status", "--json"),
        ("task", "list"),
        ("task", "list", "--json"),
        ("events",),
        ("events", "--json"),
    ],
    ids=lambda arguments: " ".join(arguments),
)
def test_store_reading_commands_skip_the_heavy_modules(state: dict[str, str], arguments: tuple[str, ...]) -> None:
    loaded = _modules(state, *arguments)
    assert not _NEVER & loaded
    # Only the requested command's parser is built, so the help module is not loaded either.
    assert "llm_tools.bridge_help" not in loaded


def test_help_builds_the_full_parser(state: dict[str, str]) -> None:
    loaded = _modules(state, "task", "list", "-h")
    assert {"argparse", "llm_tools.bridge_help"} <= loaded
    assert not {"llm_tools.common", "llm_tools.config", "llm_tools.ralph_robin", "curses"} & loaded


def test_mission_list_does_not_load_the_provider_launcher(state: dict[str, str]) -> None:
    loaded = _modules(state, "mission", "list", "--json")
    assert not {"llm_tools.ralph_robin", "llm_tools.scheduler", "llm_tools.bridge_tui", "llm_tools.bridge_launcher", "llm_tools.config"} & loaded


def test_single_command_parser_matches_the_full_parser(state: dict[str, str]) -> None:
    from llm_tools import bridge

    for arguments in (
        ["task", "list", "--limit", "5", "--json"],
        ["mission", "create", "--prompt", "x", "--providers", "claude,codex"],
        ["mission", "colour", "3"],
        ["mission", "color", "auto"],
        ["events", "--limit", "7"],
        ["rotation", "--set", "claude"],
    ):
        assert vars(bridge._parse_arguments(arguments)) == vars(bridge._build_parser().parse_args(arguments))


def test_usage_errors_come_from_the_full_parser(capsys: pytest.CaptureFixture[str]) -> None:
    from llm_tools import bridge

    with pytest.raises(SystemExit) as raised:
        bridge._parse_arguments(["task", "bogus"])
    assert raised.value.code == 2
    assert "invalid choice: 'bogus'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        bridge._parse_arguments(["nonsense"])
    assert raised.value.code == 2


def test_lazy_module_names_stay_readable_on_the_bridge_module() -> None:
    from llm_tools import bridge

    assert bridge.subprocess.run and bridge.bridge_theme.prepare and bridge.toolconfig.load_config
    assert bridge.TerminalAction("m", None, "pick").action == "pick"
    assert bridge._ConsoleParser.__name__ == "_ConsoleParser"
    with pytest.raises(AttributeError):
        bridge.no_such_name  # noqa: B018
