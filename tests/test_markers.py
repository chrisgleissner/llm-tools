"""The marker manifest and the shared timing helpers in conftest."""

from __future__ import annotations

import json

import pytest

from tests import conftest
from tests.conftest import MARKERS_FILE, RESOURCE_MARKERS, ROOT, scaled, timeout_scale, wait_for


def test_manifest_lists_only_existing_files_and_known_markers() -> None:
    manifest = json.loads(MARKERS_FILE.read_text(encoding="utf-8"))
    assert manifest, "tests/markers.json is empty; regenerate it with: python -m pytest --record-markers"
    for path, names in manifest.items():
        assert (ROOT / path).is_file(), f"{path} is listed in tests/markers.json but no longer exists"
        for name, kinds in names.items():
            assert kinds and set(kinds) <= set(RESOURCE_MARKERS), (path, name, kinds)


def test_marker_key_strips_parametrisation() -> None:
    assert conftest._marker_key("tests/test_a.py::test_b[x-1]") == "tests/test_a.py::test_b"
    assert conftest._marker_key("tests/test_a.py::Case::test_b") == "tests/test_a.py::Case::test_b"


def test_timeout_scale_defaults_and_rejects_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_TIMEOUT_SCALE", raising=False)
    assert timeout_scale() == 1.0 and scaled(2) == 2
    monkeypatch.setenv("TEST_TIMEOUT_SCALE", "2.5")
    assert scaled(2) == 5
    for bad in ("0", "-1", "fast", ""):
        monkeypatch.setenv("TEST_TIMEOUT_SCALE", bad)
        assert timeout_scale() == 1.0


def test_wait_for_returns_value_raises_with_message_and_scales_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = iter([0, 0, "ready"])
    assert wait_for(lambda: next(calls), timeout=2, interval=0.001) == "ready"
    assert wait_for(lambda: False, timeout=0.02, interval=0.001) is False
    with pytest.raises(AssertionError, match="never true"):
        wait_for(lambda: False, timeout=0.02, interval=0.001, message="never true")
    monkeypatch.setenv("TEST_TIMEOUT_SCALE", "50")
    attempts = []
    assert wait_for(lambda: attempts.append(1) or len(attempts) > 30, timeout=0.01, interval=0.001)
