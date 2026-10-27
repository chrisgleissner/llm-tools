#!/usr/bin/env python3
"""Explicit bounded real-model smoke through Bridge; never prints provider payloads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from llm_tools.bridge_store import BridgeStore, MissionLock  # noqa: E402


def probe(provider: str, seconds: int) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix="lb-live-"))
    work = root / "work"
    work.mkdir()
    state = root / "state"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    env = os.environ.copy()
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH", ""))))
    env.pop("LLM_SCHEDULER_USAGE_JSON", None)
    result: dict[str, object] = {"provider": provider, "initial_prompt_budget": 1,
        "model_execution": "NOT VERIFIED", "native_restore": "NOT VERIFIED"}
    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "llm_tools.bridge", *args],
                              env=env, text=True, capture_output=True, timeout=20)
    prompt = ("Reply only with the uppercase word bridge, an underscore, the uppercase "
              "word smoke, an underscore, and the uppercase word ok. Do not use tools "
              "or read or change any files. This is a bounded integration check.")
    created = cli("mission", "create", "--prompt", prompt, "--cwd", str(work),
                  "--providers", provider, "--headless", "--json")
    if created.returncode:
        result["reason"] = "mission-create-failed"
        shutil.rmtree(root)
        return result
    mid = json.loads(created.stdout)["mission"]["id"]
    started = time.monotonic()
    try:
        launch = cli("start", "--mission", mid, "--headless")
        result["controller_started"] = launch.returncode == 0
        while launch.returncode == 0 and time.monotonic() - started < seconds:
            outputs = list(state.glob("runtime/*/ralph-logs/**/attempt-*.out"))
            if any("BRIDGE_SMOKE_OK" in output.read_text(errors="replace") for output in outputs):
                result["model_execution"] = "PASS: expected reply through Bridge scheduler"
                break
            with BridgeStore(state / "bridge.sqlite3") as store:
                tasks = store.list_tasks(mid)
                result["task_state"] = tasks[0]["status"] if tasks else "unknown"
                if result["task_state"] in {"VERIFYING", "FAILED", "BLOCKED"}:
                    break
            time.sleep(.25)
    finally:
        cli("stop", "--mission", mid, "--force")
        lock = MissionLock(state / "locks", mid)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                lock.acquire()
                lock.release()
                result["cleanup"] = "PASS"
                shutil.rmtree(root)
                break
            except BlockingIOError:
                time.sleep(.1)
        else:
            result["cleanup"] = "NOT VERIFIED: retained state for live controller"
            result["retained_state"] = str(root)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=["codex", "claude", "copilot"])
    parser.add_argument("--model-call-budget", required=True, type=int, choices=[1],
                        help="Opt in for one initial prompt; internal provider requests are not counted")
    parser.add_argument("--seconds", type=int, default=35)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 45:
        parser.error("--seconds must be between 5 and 45")
    print(json.dumps(probe(args.provider, args.seconds), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
