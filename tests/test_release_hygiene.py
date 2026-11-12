"""Repository-level release hygiene checks (PRD-024, PRD-025, PRD-029, FIND-017, FIND-019, LB-002)."""

from __future__ import annotations

import ast
import builtins
import re
import sys
import tomllib
from pathlib import Path

import pytest

from llm_tools import bridge_documents

ROOT = Path(__file__).resolve().parents[1]


def test_security_policy_names_reporting_channel_and_response_times() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "/security/advisories/new" in text
    for heading in ("## Reporting a vulnerability", "## Supported versions", "## Dependencies and advisories"):
        assert heading in text
    assert re.search(r"Acknowledge the report \| \d+ working days", text)


def test_dependabot_covers_every_shipped_ecosystem() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    entries = re.findall(r"package-ecosystem: (\S+)\n\s+directory: (\S+)", text)
    assert set(entries) == {("cargo", "/coordinator"), ("npm", "/web"), ("github-actions", "/"), ("pip", "/")}
    for directory in ("coordinator/Cargo.lock", "web/package-lock.json", "pyproject.toml"):
        assert (ROOT / directory).exists()


def test_audit_workflow_runs_weekly_for_all_lock_files() -> None:
    text = (ROOT / ".github" / "workflows" / "audit.yml").read_text(encoding="utf-8")
    assert "schedule:" in text and "cron:" in text
    for command in ("cargo audit", "npm audit", "pip-audit"):
        assert command in text


PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION: str = PYPROJECT["project"]["version"]


def _dependency_name(spec: str) -> str:
    return re.split(r"[<>=!~\[;\s]", spec, maxsplit=1)[0].lower()


def test_rich_is_the_only_runtime_dependency() -> None:
    dependencies = PYPROJECT["project"].get("dependencies", [])
    assert [_dependency_name(item) for item in dependencies] == ["rich"], dependencies


def _top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Imports that execute when the module loads, including those in top-level if/try blocks."""
    found: list[tuple[int, str]] = []

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                found.extend((node.lineno, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.append((node.lineno, node.module))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)
            elif isinstance(node, ast.ClassDef):
                visit(node.body)

    visit(tree.body)
    return found


def test_rich_is_never_imported_at_module_top_level() -> None:
    offenders = []
    for path in sorted((ROOT / "llm_tools").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in _top_level_imports(tree):
            if name == "rich" or name.startswith("rich."):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {name}")
    assert not offenders, offenders


def test_top_level_scan_detects_a_top_level_rich_import() -> None:
    tree = ast.parse("import os\ntry:\n    from rich.text import Text\nexcept ImportError:\n    pass\n\ndef f():\n    import rich\n")
    assert _top_level_imports(tree) == [(1, "os"), (3, "rich.text")]


def test_markdown_viewer_falls_back_to_plain_source_without_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == "rich" or name.startswith("rich.")]:
        monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def refuse_rich(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "rich" or name.startswith("rich."):
            raise ImportError("No module named 'rich'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_rich)
    rows = bridge_documents.render_markdown_segments("# Title\n\nsome *text*", 40)
    texts = ["".join(text for text, _role in row) for row in rows]
    assert texts[0] == "[plain source · Markdown renderer unavailable]"
    assert texts[1:] == ["# Title", "", "some *text*"]


# FIND-019: pinned versions.

PIN = re.compile(r"(?:\.git(?:<[^>]*>)*@|--branch\s+)(\d+\.\d+\.\d+)")


def _pins(path: Path) -> list[str]:
    return PIN.findall(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", ["docs/site/index.html", "README.md"])
def test_pinned_install_versions_equal_pyproject(relative: str) -> None:
    pins = _pins(ROOT / relative)
    assert all(pin == VERSION for pin in pins), (relative, pins, VERSION)


def test_the_website_pins_the_current_version() -> None:
    assert len(_pins(ROOT / "docs/site/index.html")) >= 4


def test_release_workflow_checks_pins() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "tests/test_release_hygiene.py" in workflow


# PRD-029: changelog.


def test_changelog_has_every_release_and_an_unreleased_section() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", text, flags=re.M)
    assert headings[0] == "Unreleased"
    assert {"1.4.0", "1.3.0", "1.2.1", "1.2.0", "1.1.0", "1.0.0"} <= set(headings)
    assert VERSION in headings
    unreleased = text.split("## [Unreleased]")[1].split("\n## [")[0]
    assert len(re.findall(r"^- ", unreleased, flags=re.M)) <= 15
    assert "semantic versioning" in text and "pyproject.toml" in text
    assert not re.search(r"[\w.-]+@[A-Za-z][\w-]*\.[A-Za-z]+|\b192\.168\.|\b10\.\d+\.\d+\.\d+\b|\bpi2\b", text)
    assert "(./CHANGELOG.md)" in (ROOT / "README.md").read_text(encoding="utf-8")


# LB-002: platform claims.

PLATFORM_WORDS = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}


def _ci_platforms() -> set[str]:
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    found: set[str] = set()
    for match in re.finditer(r"^\s*(?:-\s*)?(?:name|runs-on|os):\s*(.+)$|\{[^}]*\bos:\s*([\w.-]+)", workflow, flags=re.M):
        value = (match.group(1) or match.group(2) or "").lower()
        if "ubuntu" in value or "linux" in value:
            found.add("linux")
        if "macos" in value:
            found.add("macos")
        if "windows" in value:
            found.add("windows")
    return found


def test_readme_platform_badge_names_only_platforms_ci_runs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    badge = re.search(r"img\.shields\.io/badge/platform-([^)\-]+(?:%20[^)\-]+)*)-", readme)
    assert badge, "README has no platform badge"
    claimed = {part.strip().lower() for part in re.split(r"%20%7C%20", badge.group(1))}
    assert claimed <= set(PLATFORM_WORDS), claimed
    assert claimed <= _ci_platforms(), (claimed, _ci_platforms())


def test_suspend_and_wake_are_documented_as_linux_with_systemd_only() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"[Ss]uspend and wake[^.]*Linux with systemd only", readme)
    first_run = (ROOT / "docs/llm-bridge/first-run.md").read_text(encoding="utf-8")
    assert "Platform: Linux only" in first_run and "suspend and wake need Linux with systemd" in first_run


def test_usage_ansi_renderer_falls_back_to_plain_text_without_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == "rich" or name.startswith("rich.")]:
        monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def refuse_rich(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "rich" or name.startswith("rich."):
            raise ImportError("No module named 'rich'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_rich)
    rows = bridge_documents.render_ansi_segments("\x1b[32mclaude\x1b[0m   ready\n\nend")
    assert rows == ((("claude   ready", "info"),), (), (("end", "info"),))
