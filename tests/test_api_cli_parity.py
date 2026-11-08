"""API-015 contract: every Bridge CLI leaf, option and coordinator route is accounted for."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from llm_tools.bridge import _build_parser

ROOT = Path(__file__).resolve().parents[1]
API_DOC = ROOT / "docs/distributed/api.md"
COORDINATOR_DOC = ROOT / "docs/distributed/coordinator.md"
HTTP_RS = ROOT / "coordinator/src/http.rs"

# Intentional local-only commands are named explicitly in the docs. The remaining
# entries are likewise present so a new CLI leaf cannot silently escape review.
COMMANDS = {
    "help", "view", "mission create", "mission list", "mission set", "mission settings", "mission delete", "mission colour", "mission color",
    "status", "screen", "agents", "suspend-until-credits", "task add", "task list", "task show",
    "task amend", "task priority", "task reorder", "task dependencies", "task pause", "task resume",
    "task cancel", "task retry", "task verify", "task approve", "task output", "task diff", "task reject", "proposal create", "proposal list",
    "proposal accept", "proposal reject", "events", "start", "pause", "resume", "continue", "stop",
    "cancel", "attach", "detach", "missions", "gc", "backup", "restore", "worker enroll",
    "worker status", "worker run", "worker audit", "worker install-service", "worker uninstall-service", "worker rotate-token",
    "worker unenroll", "worker list", "remote login", "remote status", "remote logout", "finish", "doctor",
    "theme", "console", "control", "usage", "station", "rotation", "steer", "_station-gc", "_controller",
}


def _leaves(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> list[str]:
    choices = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not choices:
        return [" ".join(prefix)]
    return [leaf for action in choices for name, child in action.choices.items() for leaf in _leaves(child, (*prefix, name))]


def _options(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        found.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                found.update(_options(child))
    return found


def _rust_routes() -> set[tuple[str, str]]:
    source = HTTP_RS.read_text()
    block = source.split("pub const ROUTES:", 1)[1].split("];", 1)[0]
    routes: set[tuple[str, str]] = set()
    for path, methods in re.findall(r'\("([^\"]+)"\s*,\s*&\[([^\]]+)\]', block):
        routes.update((method, path) for method in re.findall(r'"([A-Z]+)"', methods))
    return routes


def _parity_rows(section: str, header: str) -> list[tuple[str, str]]:
    table = section.split(header, 1)[1]
    return re.findall(r"^\| `([^`]+)` \| ([^|]*) \|$", table, re.MULTILINE)


def _decision_routes(rows: list[tuple[str, str]]) -> set[tuple[str, str]]:
    return {
        (method, path)
        for _name, decision in rows
        for method, path in re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/v1/[^ ;,`.)]+)", decision)
    }


def _assert_decisions_are_present(rows: list[tuple[str, str]], kind: str) -> None:
    missing = [name for name, decision in rows if not decision.strip() or decision.strip() in {"-", "TBD", "TODO", "N/A"}]
    assert not missing, f"every CLI {kind} needs a parity decision; blank or placeholder decisions: {missing}"
    unclassified = [
        name
        for name, decision in rows
        if not any(
            marker in decision.lower()
            for marker in (
                "typed", "resource", "rest", "operation", "maps to", "mapping", "exclusion", "local", "alias", "short form",
                "body", "field", "internal", "never remote", "client-side",
            )
        )
    ]
    assert not unclassified, f"every CLI {kind} decision must identify a mapping or exclusion: {unclassified}"


def test_parity_decision_requires_mapping_or_exclusion_language() -> None:
    _assert_decisions_are_present([("mission list", "Typed operation `mission.list` maps to GET /api/v1/missions.")], "leaf")
    _assert_decisions_are_present([("view", "Exclusion: opens a local artifact; no REST operation.")], "leaf")
    try:
        _assert_decisions_are_present([("new command", "Covered by the API.")], "leaf")
    except AssertionError as exc:
        assert "new command" in str(exc)
    else:
        raise AssertionError("an unclassified parity statement must fail")


def test_cli_commands_and_options_are_listed_in_parity_table() -> None:
    doc = API_DOC.read_text()
    section = doc.split("## API-015 parity table", 1)[1].split("Coordinator router declarations", 1)[0]
    actual_commands = set(_leaves(_build_parser()))
    assert actual_commands == COMMANDS, f"update API-015 command inventory; missing={actual_commands-COMMANDS}, stale={COMMANDS-actual_commands}"
    command_table = section.split("| CLI option |", 1)[0]
    command_rows = re.findall(r"^\| `([^`]+)` \| ([^|]*) \|$", command_table, re.MULTILINE)
    documented_commands = {name for name, _decision in command_rows}
    assert len(documented_commands) == len(command_rows), "each CLI leaf must have exactly one parity decision"
    assert documented_commands == actual_commands, (
        f"update API-015 command decisions; missing={actual_commands-documented_commands}, "
        f"stale={documented_commands-actual_commands}"
    )
    _assert_decisions_are_present(command_rows, "leaf")

    option_rows = _parity_rows(section, "| CLI option |")
    actual_options = _options(_build_parser())
    documented_options = {name for name, _decision in option_rows}
    assert len(documented_options) == len(option_rows), "each CLI option must have exactly one parity decision"
    assert documented_options == actual_options, (
        f"update API-015 option decisions; missing={actual_options-documented_options}, "
        f"stale={documented_options-actual_options}"
    )
    _assert_decisions_are_present(option_rows, "option")

    actual_routes = _rust_routes()
    referenced_routes = _decision_routes([*command_rows, *option_rows])
    assert referenced_routes <= actual_routes, (
        "API-015 decisions name routes absent from http::ROUTES; "
        f"missing={referenced_routes-actual_routes}. Exclusions without a route are valid."
    )


def test_coordinator_routes_are_listed_in_parity_table() -> None:
    doc = API_DOC.read_text()
    route_table = doc.split("Coordinator router declarations", 1)[1].split("The regression test", 1)[0]
    documented = set(re.findall(r"`((?:GET|POST|PUT|PATCH|DELETE) /[^`]+)`", route_table))
    actual = _rust_routes()
    documented_routes = {(item.split(" ", 1)[0], item.split(" ", 1)[1]) for item in documented}
    assert len(documented_routes) == len(documented), "each coordinator route must be documented once"
    assert documented_routes == actual, f"update API-015 route table; missing={actual-documented_routes}, stale={documented_routes-actual}"


def test_api_reference_covers_auth_concurrency_pagination_errors_and_versioning() -> None:
    coordinator = COORDINATOR_DOC.read_text()
    reference = API_DOC.read_text()
    for term in (
        "Authorization: Bearer",
        "terminal:view",
        "terminal:write",
        "Idempotency-Key",
        "If-Match",
        "412 Precondition Failed",
        "202 Accepted",
        "next_cursor",
        "application/problem+json",
        "Retry-After",
        "breaking change requires `/api/v2`",
        "Deprecation",
        "Sunset",
    ):
        assert term in coordinator, f"coordinator API contract must document {term}"
    assert '"$COORDINATOR/api/v1/missions?status=RUNNING&limit=100"' in reference
    assert '"$COORDINATOR/api/v1/missions/$MISSION/tasks/$TASK/verify?worker=$WORKER"' in reference
    assert '"$COORDINATOR/api/v1/missions/$MISSION/tasks?worker=$WORKER"' in reference
