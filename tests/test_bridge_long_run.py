"""Deterministic thousand-iteration state/rotation simulation, not live proof."""
import gc
import json
import os
import time
import tracemalloc

from llm_tools import ralph_robin
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def test_thousand_iterations_keep_tasks_evidence_and_resources_bounded(tmp_path, monkeypatch):
    ready = {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps({"codex": ready, "claude": ready}))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("soak simulation", "deterministic task sequence", cwd=tmp_path)
    mid = mission["id"]
    for index in range(1000):
        store.add_task(mid, f"independent task {index}")
    store.set_mission_status(mid, "RUNNING")
    class SimulatedOperator(BridgeMissionHooks):
        def after_iteration(self, context, selection, cfg, iteration, status, seconds):
            super().after_iteration(context, selection, cfg, iteration, status, seconds)
            if status == 0:
                store.verify_task(context["task_id"], approved=True)
    hooks = SimulatedOperator(mid, handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    calls = 0
    def executor(cfg):
        nonlocal calls
        calls += 1
        return 75 if calls % 101 == 0 else 0
    monkeypatch.setattr(ralph_robin, "run_scheduler_inline", executor)
    before_fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    started = time.monotonic()
    try:
        status = ralph_robin.main(["--prompt", "initial", "--providers", "codex,claude",
            "--cwd", str(tmp_path), "--log-dir", str(tmp_path / "logs"),
            "--state-file", str(tmp_path / "rotation.json"), "--max-duration", "0",
            "--min-iteration-seconds", "0"], bridge_hooks=hooks)
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert status == 0 and calls == 1009
    assert store.get_mission(mid)["status"] == "COMPLETED"
    assert store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=? AND status='COMPLETED'", (mid,)).fetchone()[0] == 1000
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=?", (mid,)).fetchone()[0] == 1009
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=? AND status!='EXITED'", (mid,)).fetchone()[0] == 0
    assert current - baseline < 8 * 1024 * 1024
    if before_fds is not None:
        assert len(os.listdir("/proc/self/fd")) <= before_fds + 3
    (tmp_path / "long-run-results.json").write_text(json.dumps({"successful_tasks": 1000,
        "provider_attempts": calls, "elapsed_seconds": time.monotonic()-started,
        "traced_current_growth_bytes": current-baseline, "traced_peak_bytes": peak,
        "database_bytes": store.path.stat().st_size}, indent=2))
    store.close()
