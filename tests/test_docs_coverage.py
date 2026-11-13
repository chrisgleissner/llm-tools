"""Documentation coverage: the docs must cover what the code produces.

Nothing here keeps a hand-written list. Error codes come from the Python source, commands come from
the real argparse tree, and flags come from the hand-written parsers of the other tools. Adding an
error code, a command or a flag without documenting it fails these tests and names what is missing.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_help

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "llm-bridge"
TROUBLESHOOTING = DOCS / "troubleshooting.md"
INTERNAL_COMMANDS = {"console", "_controller", "_station-gc"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def produced_error_codes() -> dict[str, set[str]]:
    """Every error code the Bridge source can produce, with the places that produce it."""
    found: dict[str, set[str]] = {}

    def add(code: str | None, path: Path, node: ast.AST) -> None:
        if code:
            found.setdefault(code, set()).add(f"{path.name}:{getattr(node, 'lineno', 0)}")

    for path in sorted((ROOT / "llm_tools").glob("bridge*.py")):
        for node in ast.walk(ast.parse(_read(path))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                is_self_error = name == "error" and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self"
                if (name in {"CommandFailure", "CommandError"} or is_self_error) and node.args:
                    add(_string(node.args[0]), path, node)
                if name in {"_fail", "error_text"} and len(node.args) > 1:
                    add(_string(node.args[1]), path, node)
                if name == "get" and len(node.args) == 2 and _string(node.args[0]) == "code":
                    add(_string(node.args[1]), path, node)
                for keyword in node.keywords:
                    if keyword.arg == "code":
                        add(_string(keyword.value), path, node)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if _string(key) == "code":
                        add(_string(value), path, node)
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
                code, status = node.value.elts
                if isinstance(status, ast.Name) and status.id.startswith("EXIT_"):
                    add(_string(code), path, node)
    return found


def github_slug(heading: str) -> str:
    """The anchor GitHub generates for a Markdown heading."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    in_fence = False
    result: set[str] = set()
    for line in _read(path).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and (match := re.match(r"#{1,6}\s+(.+)", line)):
            result.add(github_slug(match.group(1)))
    return result


def public_commands() -> list[str]:
    """Full names of the public leaf commands in the real parser, one per parser (aliases are skipped)."""
    names: list[str] = []
    seen: set[int] = set()

    def walk(parser: argparse.ArgumentParser, prefix: list[str]) -> None:
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                if name in INTERNAL_COMMANDS or name.startswith("_") or id(child) in seen:
                    continue
                seen.add(id(child))
                has_children = any(isinstance(item, argparse._SubParsersAction) for item in child._actions)
                if has_children:
                    walk(child, prefix + [name])
                else:
                    names.append(" ".join(prefix + [name]))

    walk(bridge._build_parser(), [])
    return names


def code_text(markdown: str) -> str:
    """The code in a Markdown page: fenced blocks and inline backtick spans."""
    pieces: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            pieces.append(line)
        else:
            pieces.extend(re.findall(r"`([^`\n]+)`", line))
    return "\n".join(pieces)


def test_the_error_code_collector_finds_the_documented_codes() -> None:
    codes = set(produced_error_codes())
    assert {"usage", "wrong-state", "mission-not-found", "conflict", "schema-too-new", "failed"} <= codes


def test_every_error_code_has_a_troubleshooting_anchor() -> None:
    assert TROUBLESHOOTING.is_file(), f"{TROUBLESHOOTING.relative_to(ROOT)} does not exist"
    present = anchors(TROUBLESHOOTING)
    missing = {code: sorted(places) for code, places in produced_error_codes().items() if code not in present}
    assert not missing, (
        "error codes without a heading in docs/llm-bridge/troubleshooting.md "
        f"(add a '### <code>' entry for each): {missing}"
    )


def test_error_references_point_at_the_troubleshooting_page() -> None:
    reference = bridge_help.troubleshooting_reference("wrong-state")
    assert reference.endswith("/docs/llm-bridge/troubleshooting.md#wrong-state")
    assert reference.startswith("https://github.com/chrisgleissner/llm-tools/blob/main/")
    assert (ROOT / "README.md").read_text(encoding="utf-8").count("github.com/chrisgleissner/llm-tools") > 0


def test_every_public_bridge_command_is_documented() -> None:
    commands = public_commands()
    assert len(commands) > 40, "the parser walk found too few commands"
    documented = code_text(_read(DOCS / "operations.md") + "\n" + (_read(DOCS / "guide.md") if (DOCS / "guide.md").is_file() else ""))
    missing = [
        name
        for name in commands
        if not re.search(rf"(?<![\w-])llm-bridge(?: --target \S+)? {re.escape(name)}(?![\w-])", documented)
    ]
    assert not missing, (
        "public commands not documented in docs/llm-bridge/operations.md or guide.md "
        f"(write `llm-bridge <command>` in backticks or a code block): {missing}"
    )


def parser_flags(module: str) -> set[str]:
    """The long options a tool's hand-written ``parse_args`` accepts."""
    tree = ast.parse(_read(ROOT / "llm_tools" / f"{module}.py"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_args")
    flags = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"--[a-z][a-z0-9-]*", node.value)
    }
    return flags - {"--help"}


def user_pages() -> str:
    """The README and every docs page it links to."""
    readme = _read(ROOT / "README.md")
    pages = [ROOT / name for name in dict.fromkeys(re.findall(r"\(\./(docs/[\w./-]+\.md)", readme))]
    return readme + "\n" + "\n".join(_read(page) for page in pages if page.is_file())


@pytest.mark.parametrize("module", ["usage", "scheduler", "ralph_robin", "sleep_soak"])
def test_every_public_flag_of_the_other_tools_is_documented(module: str) -> None:
    flags = parser_flags(module)
    assert flags, f"no flags found in llm_tools/{module}.py"
    text = user_pages()
    missing = sorted(flag for flag in flags if not re.search(re.escape(flag) + r"(?![\w-])", text))
    assert not missing, f"flags of llm_tools/{module}.py that README.md and the pages it links to never mention: {missing}"


# --------------------------------------------------------------------------- coordinator error codes (PRD-006)

COORDINATOR_TROUBLESHOOTING = ROOT / "docs" / "distributed" / "troubleshooting.md"
_RUST_CODE = re.compile(r'\b(?:err|perr)\(\s*(?:StatusCode::[A-Z_]+,\s*)?"([a-z][a-z0-9-]*)"|"code"\s*:\s*"([a-z][a-z0-9-]*)"')


def coordinator_error_codes() -> dict[str, set[str]]:
    """Every error code the Rust coordinator raises through err()/perr() or a literal `"code"` field."""
    found: dict[str, set[str]] = {}
    for path in sorted(p for p in (ROOT / "coordinator" / "src").glob("*.rs") if p.name != "config_check.rs"):  # config_check reports a field named code, not an API error
        for match in _RUST_CODE.finditer(_read(path)):
            code = match.group(1) or match.group(2)
            found.setdefault(code, set()).add(path.name)
    return found


def test_the_coordinator_error_collector_finds_known_codes() -> None:
    assert {"unauthorized", "rate-limited", "worker-offline", "invalid-cursor"} <= set(coordinator_error_codes())


def test_every_coordinator_error_code_has_a_troubleshooting_anchor() -> None:
    present = anchors(COORDINATOR_TROUBLESHOOTING)
    missing = {code: sorted(files) for code, files in coordinator_error_codes().items() if code not in present}
    assert not missing, f"coordinator error codes without a '### <code>' heading in docs/distributed/troubleshooting.md: {missing}"
