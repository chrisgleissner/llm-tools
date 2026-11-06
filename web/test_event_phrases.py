"""The browser's checked-in phrase artifact is generated from the production TUI humanizers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB))
from generate_event_phrases import artifact, phrases  # noqa: E402


class EventPhraseContract(unittest.TestCase):
    def test_generated_browser_artifact_matches_every_tui_humanizer(self) -> None:
        generated = WEB / "src/event-phrases.generated.ts"
        self.assertTrue(generated.exists(), "run web/build.mjs to regenerate the phrase artifact")
        self.assertEqual(generated.read_text(encoding="utf-8"), artifact())
        self.assertTrue(phrases(), "the production TUI phrase registry must not be empty")

    def test_browser_humanizer_uses_generated_table_without_a_second_phrase_map(self) -> None:
        source = (WEB / "src/event-humanizer.ts").read_text(encoding="utf-8")
        self.assertIn('from "./event-phrases.generated"', source)
        self.assertIn("TUI_EVENT_PHRASES[kind]", source)
        self.assertNotIn("const WORDS:", source)


if __name__ == "__main__":
    unittest.main()
