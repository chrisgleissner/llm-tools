"""Generate browser event base phrases from the Bridge TUI's production humanizer registry."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from llm_tools import bridge_tui  # noqa: E402


def phrases() -> dict[str, str]:
    result: dict[str, str] = {}
    for kind in sorted(bridge_tui._HUMANIZERS):
        phrase, _plumbing = bridge_tui._humanize_event({"kind": kind, "data": {}, "task_id": None})
        result[kind] = phrase
    return result


def spelled_out(kind: str) -> str:
    """The phrase the browser derives for a kind that has no table entry: `task-added` becomes `Task added`.

    Keep in step with the fallback in src/event-humanizer.ts.
    """
    words = re.sub(r"[-_.]", " ", kind)
    return words[:1].upper() + words[1:]


def artifact() -> str:
    # A kind whose phrase is its own name spelled out needs no entry: the browser derives it the same way.
    table = {kind: phrase for kind, phrase in phrases().items() if phrase != spelled_out(kind)}
    # One string of `kind<TAB>phrase` lines: the browser splits it once, which is smaller than a quoted object literal.
    lines = "\n".join(f"{kind}\t{phrase}" for kind, phrase in sorted(table.items()))
    if "`" in lines or "${" in lines or "\\" in lines:
        raise ValueError("a phrase contains a character that needs escaping in a template literal")
    return (
        "// Generated from llm_tools.bridge_tui._HUMANIZERS by generate_event_phrases.py. Do not edit.\n"
        "// Kinds whose phrase is the kind spelled out as words (task-added: Task added) are omitted; src/event-humanizer.ts derives them.\n"
        "export const TUI_EVENT_PHRASES: Readonly<Record<string, string>> = Object.fromEntries(\n"
        f"  `{lines}`.split(\"\\n\").map((line) => line.split(\"\\t\") as [string, string]),\n"
        ");\n"
    )


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "src/event-phrases.generated.ts"
    output.write_text(artifact(), encoding="utf-8")
    print(f"generated {output}")
