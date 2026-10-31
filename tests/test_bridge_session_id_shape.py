"""Opaque session IDs become provider argv, so one must never look like an option."""
from __future__ import annotations

import pytest

from llm_tools.bridge_sessions import _CAPABILITIES


@pytest.mark.parametrize("provider", ["kilo", "opencode"])
@pytest.mark.parametrize("value", ["--output=json", "-rf-session", "-12345678", "_leading12", "a" * 129, "short", "ses_01234567 89", "ses_0123456789\n"])
def test_opaque_session_id_rejects_options_and_bad_shapes(provider: str, value: str) -> None:
    assert _CAPABILITIES[provider].id_pattern.fullmatch(value) is None


@pytest.mark.parametrize("provider", ["kilo", "opencode"])
@pytest.mark.parametrize("value", ["ses_0123456789abcdef", "ABCDEFGH", "a1234567", "9-8-7-6-5-4", "a" * 128])
def test_opaque_session_id_accepts_ordinary_ids(provider: str, value: str) -> None:
    assert _CAPABILITIES[provider].id_pattern.fullmatch(value) is not None
