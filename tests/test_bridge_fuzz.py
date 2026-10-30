"""Seeded property tests for LLM Bridge's chaotic-input handling.

Each target in ``tests/fuzz/targets*.py`` runs a fixed range of seeds, so a
failure is reproducible: ``python scripts/bridge_fuzz.py --case TARGET:SEED``.
The long-running variant is ``scripts/bridge_fuzz.py``. Skip these in the fast
inner loop with ``pytest -m "not fuzz"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fuzz.harness import Sandbox
from tests.fuzz.targets import TARGETS, run_seeds

pytestmark = pytest.mark.fuzz


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_fuzz_target(name: str, tmp_path: Path) -> None:
    target = TARGETS[name]
    with Sandbox(tmp_path) as sandbox:
        ran, failures = run_seeds(target, range(target.cases), sandbox)
    assert ran == target.cases
    report = "\n".join(f"{failure.summary()}\n    reproduce: python scripts/bridge_fuzz.py --case {failure.target}:{failure.seed}" for failure in failures[:5])
    assert not failures, f"{len(failures)} of {ran} {name} cases failed:\n{report}"
