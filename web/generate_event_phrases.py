"""Generate browser event base phrases from the Bridge TUI's production humanizer registry."""
from __future__ import annotations

import json
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


def artifact() -> str:
    entries = json.dumps(phrases(), ensure_ascii=False, indent=2, sort_keys=True)
    return "// Generated from llm_tools.bridge_tui._HUMANIZERS by generate_event_phrases.py. Do not edit.\n" + f"export const TUI_EVENT_PHRASES: Readonly<Record<string, string>> = {entries};\n"


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "src/event-phrases.generated.ts"
    output.write_text(artifact(), encoding="utf-8")
    print(f"generated {output}")
