"""Registry of fuzz targets, shared by the pytest suite and ``scripts/bridge_fuzz.py``."""

from __future__ import annotations

import json
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import targets_cli, targets_pure, targets_ui
from .harness import Failure, Sandbox, run_case

TargetFunction = Callable[[random.Random, Sandbox], None]


@dataclass(frozen=True)
class Target:
    name: str
    function: TargetFunction
    cases: int  # fixed-seed cases the CI test runs (seeds 0..cases-1)
    case_timeout: float = 20.0
    summary: str = ""


def _target(function: TargetFunction, cases: int, *, timeout: float = 20.0) -> Target:
    name = function.__name__.removeprefix("target_")
    return Target(name, function, cases, timeout, (function.__doc__ or "").strip().splitlines()[0] if function.__doc__ else "")


_ALL = [
    _target(targets_pure.target_input_primitives, 400),
    _target(targets_pure.target_bracketed_paste, 250),
    _target(targets_pure.target_text_buffer, 250),
    _target(targets_pure.target_documents, 80),
    _target(targets_pure.target_markdown, 30),
    _target(targets_pure.target_theme_config, 150),
    _target(targets_pure.target_theme_curses, 60),
    _target(targets_pure.target_event_summaries, 80),
    _target(targets_pure.target_regex_time, 600),
    _target(targets_pure.target_text_helpers, 600),
    *[_target(getattr(targets_ui, name), cases, timeout=30.0) for name, cases in targets_ui.CI_CASES.items()],
    *[_target(getattr(targets_cli, name), cases, timeout=60.0) for name, cases in targets_cli.CI_CASES.items()],
]

TARGETS: dict[str, Target] = {target.name: target for target in _ALL}


def run_seeds(target: Target, seeds: range | list[int], sandbox: Sandbox, *, stop_after: float | None = None) -> tuple[int, list[Failure]]:
    """Run ``seeds`` against ``target``; return (cases run, failures)."""
    failures: list[Failure] = []
    ran = 0
    started = time.monotonic()
    for seed in seeds:
        failure = run_case(target.name, target.function, seed, sandbox, timeout=target.case_timeout)
        ran += 1
        if failure is not None:
            failures.append(failure)
        if stop_after is not None and time.monotonic() - started >= stop_after:
            break
    return ran, failures


def fresh_sandbox_root(prefix: str = "bridge-fuzz-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def save_failure(failure: Failure, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{failure.target}-seed{failure.seed}.json"
    payload = failure.as_dict()
    payload["replay"] = f"python scripts/bridge_fuzz.py --replay {path}"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=repr) + "\n", encoding="utf-8")
    return path
