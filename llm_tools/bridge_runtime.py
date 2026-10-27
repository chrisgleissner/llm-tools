"""Bridge mission controller and the thin hooks into Ralph Robin."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import select
import signal
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import ralph_robin
from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir, utc_now
from .bridge_refresh import CapacityRefreshWorker
from .bridge_progress import repository_checkpoint
from .bridge_sessions import (
    NativeSessionIdentity,
    confirm_restored,
    codex_owned_argv,
    discover_owned_codex_session,
    new_session_argv,
    provider_session_metadata,
    resume_argv,
)
from .terminal import TerminalHandle, TmuxTerminalBackend


class BridgeMissionHooks:
    """Translate durable Bridge task boundaries into the existing Ralph loop."""

    def __init__(
        self,
        mission_id: str,
        *,
        handle: TerminalHandle | None,
        headless: bool,
        native_tui: bool = False,
        max_provider_failures: int = 8,
        controller_logs: Path,
    ) -> None:
        self.mission_id = mission_id
        self.handle = handle
        self.headless = headless
        self.native_tui = native_tui
        self.max_provider_failures = max(1, int(max_provider_failures))
        self.controller_logs = controller_logs
        self._store: BridgeStore | None = None
        self._iteration = 0
        self._last_selected_station = ""
        self._session_restore_expectations: dict[str, NativeSessionIdentity] = {}
        self._session_baselines: dict[str, dict[str, int]] = {}
        self._session_started_ns: dict[str, int] = {}
        self._session_candidates: dict[str, NativeSessionIdentity] = {}
        self._session_providers: dict[str, str] = {}
        self._session_cwds: dict[str, str] = {}
        self._codex_owned_operations: set[str] = set()

    def bind(self, store: BridgeStore) -> None:
        self._store = store

    @staticmethod
    def _selection_projection(selection: dict[str, Any]) -> dict[str, Any]:
        from .bridge_refresh import _safe_decision

        return _safe_decision(selection)

    def on_selection(self, selection: dict[str, Any]) -> None:
        projection = self._selection_projection(selection)
        station = projection["route"] or projection["provider"]
        if self._last_selected_station and station and station != self._last_selected_station:
            self.store.record_runtime_event(self.mission_id, "station-handoff", {
                "from": self._last_selected_station,
                "to": station,
                "provider": projection["provider"],
                "route": projection["route"],
                "reason": projection["rotation_reason"],
            })
        self.store.record_runtime_event(self.mission_id, "provider-selection", projection)
        if station:
            self._last_selected_station = station

    def on_wait(self, selection: dict[str, Any], target_epoch: int | None) -> None:
        projection = self._selection_projection(selection)
        self.store.record_runtime_event(self.mission_id, "provider-wait", {
            "target_epoch": target_epoch,
            "all_rate_limited": projection["all_rate_limited"],
            "reason": projection["decision"].get("reason", projection["rotation_reason"]),
            "decisions": projection["decisions"],
        })

    @property
    def store(self) -> BridgeStore:
        if self._store is None:
            raise RuntimeError("Bridge hooks are not bound to a store")
        return self._store

    def before_iteration(self) -> dict[str, Any]:
        while True:
            self._apply_pending_boundary_requests()
            mission = self.store.get_mission(self.mission_id)
            status = mission["status"]
            if status in {"STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"}:
                return {"action": "stop", "reason": f"mission {status.lower()}", "exit_code": 0}
            if status == "PAUSED":
                time.sleep(0.5)
                continue

            tasks = self.store.ready_tasks(self.mission_id)
            if tasks:
                task = tasks[0]
                current = self.store.begin_iteration(int(task["id"]))
                self._iteration += 1
                history = self.store.db.execute(
                    "SELECT id,kind,data_json,created_at FROM events WHERE mission_id=? AND (task_id=? OR operation_id IN (SELECT id FROM operations WHERE task_id=?)) ORDER BY id DESC LIMIT 20",
                    (self.mission_id, current["id"], current["id"]),
                ).fetchall()
                operations = self.store.db.execute(
                    "SELECT id,station,route_id,model,status,exit_code,logs_path FROM operations INDEXED BY operations_task_history WHERE task_id=? ORDER BY rowid DESC LIMIT 10",
                    (current["id"],),
                ).fetchall()
                checkpoint = {
                    "restoration": "RECONSTRUCTED",
                    "task_revision": int(current["revision"]),
                    "evidence": current["evidence"],
                    "operations": [dict(row) for row in operations],
                    "recent_events": [
                        {"id": int(row["id"]), "kind": row["kind"], "data": json.loads(row["data_json"]), "at": row["created_at"]}
                        for row in reversed(history)
                    ],
                }
                prompt = (
                    "LLM Bridge mission context. Provider session continuity is RECONSTRUCTED from durable state; "
                    "this is not an exact native conversation restore.\n\n"
                    f"Mission objective:\n{mission['objective']}\n\n"
                    f"Task {current['id']} ({current['title']}), revision {current['revision']}:\n{current['prompt']}\n\n"
                    f"Acceptance criteria:\n{json.dumps(current['acceptance'], ensure_ascii=False)}\n\n"
                    f"Durable checkpoint (latest bounded history):\n{json.dumps(checkpoint, ensure_ascii=False, default=str)[:12000]}\n\n"
                    "Continue from this state. Do not claim work from an earlier provider session unless supported by the evidence above."
                )
                return {
                    "action": "run",
                    "task_id": int(current["id"]),
                    "revision": int(current["revision"]),
                    "prompt": prompt,
                    "task_title": str(current["title"]),
                    "iteration": self._iteration,
                }

            counts = {
                row["status"]: int(row["n"])
                for row in self.store.db.execute(
                    "SELECT status,COUNT(*) AS n FROM tasks WHERE mission_id=? "
                    "AND (origin!='agent-proposal' OR proposal_status='ACCEPTED') GROUP BY status",
                    (self.mission_id,),
                )
            }
            total = sum(counts.values())
            if total and counts.get("COMPLETED", 0) == total:
                self.store.set_mission_status(self.mission_id, "COMPLETED")
                return {"action": "stop", "reason": "all tasks verified", "exit_code": 0}
            time.sleep(0.5)

    def _apply_pending_boundary_requests(self) -> None:
        mission = self.store.get_mission(self.mission_id)
        config = mission["config"]
        requests = [int(value) for value in config.get("verification_requests", [])]
        remaining: list[int] = []
        for task_id in requests:
            task = self.store.get_task(task_id)
            if task["status"] == "VERIFYING":
                run_configured_verifier(self.store, self.mission_id, task_id, runtime=self.controller_logs.parent)
                self.store.record_runtime_event(self.mission_id, "steering-verify-applied", {"task_id": task_id})
            else:
                remaining.append(task_id)
        if requests != remaining:
            self.store.set_mission_config_value(self.mission_id, "verification_requests", remaining, event_kind="steering-verify-drained")

        mission = self.store.get_mission(self.mission_id)
        config = mission["config"]
        stop_after = config.get("stop_after_task")
        if stop_after is not None and self.store.get_task(int(stop_after))["status"] == "COMPLETED":
            self.store.set_mission_config_value(self.mission_id, "stop_after_task", None, event_kind="steering-stop-task-applied")
            if mission["status"] == "RUNNING":
                self.store.set_mission_status(self.mission_id, "STOPPING")
            return
        if config.get("pause_after_iteration"):
            self.store.set_mission_config_value(self.mission_id, "pause_after_iteration", False, event_kind="steering-pause-applied")
            if mission["status"] == "RUNNING":
                self.store.set_mission_status(self.mission_id, "PAUSED")

    def apply_handoff_preference(self, selection: dict[str, Any]) -> dict[str, Any]:
        mission = self.store.get_mission(self.mission_id)
        preferred = str(mission["config"].get("handoff_preference", ""))
        if not preferred:
            return selection
        decisions = selection.get("decisions")
        if not isinstance(decisions, list):
            return selection
        for index, decision in enumerate(decisions):
            if not isinstance(decision, dict) or decision.get("usable") is not True:
                continue
            candidate = str(decision.get("route") or decision.get("provider") or "")
            if candidate != preferred:
                continue
            updated = dict(selection)
            updated.update({
                "index": index,
                "route": str(decision.get("route") or ""),
                "provider": str(decision.get("provider") or preferred),
                "rotation_reason": "operator-handoff",
                "all_rate_limited": False,
                "decision": decision,
            })
            self.store.set_mission_config_value(self.mission_id, "handoff_preference", "", event_kind="steering-handoff-consumed")
            self.store.record_runtime_event(self.mission_id, "provider-handoff-selected", {"station": preferred, "index": index})
            return updated
        return selection

    def before_launch(self, context: dict[str, Any], selection: dict[str, Any], scheduler_config: Any, iteration: int) -> bool:
        mission = self.store.get_mission(self.mission_id)
        if mission["status"] != "RUNNING":
            return False
        provider = str(selection.get("provider") or scheduler_config.provider)
        route_id = str(selection.get("route") or "")
        try:
            operation_id = self.store.begin_operation(int(context["task_id"]), iteration=iteration, station=route_id or provider)
        except TransitionError:
            # Mission pause/stop and operation intent serialize in SQLite. If
            # the command won that race, simply reject this launch boundary.
            task_state = self.store.get_task(int(context["task_id"]))["status"]
            if self.store.get_mission(self.mission_id)["status"] != "RUNNING" or task_state in {"PAUSED", "CANCELLED"}:
                return False
            raise
        pane_id = ""
        if self.handle is not None and self.native_tui:
            scheduler_config.exec_mode = "tmux"
            scheduler_config.tmux_target = f"{self.handle.session_name}:station"
            scheduler_config.tmux_socket = str(self.handle.socket_path)
            scheduler_config.tmux_pane = self.handle.provider_pane
            scheduler_config.native_tui = True
            scheduler_config.headless = False
            pane_id = self.handle.provider_pane
        else:
            scheduler_config.exec_mode = "fresh"
            scheduler_config.headless = True
            scheduler_config.native_tui = False
        try:
            self.store.mark_operation_running(
                operation_id,
                pane_id=pane_id,
                route_id=route_id,
                model=str(scheduler_config.model or ""),
            )
        except TransitionError:
            task_state = self.store.get_task(int(context["task_id"]))["status"]
            mission_state = self.store.get_mission(self.mission_id)["status"]
            if mission_state != "RUNNING" or task_state in {"PAUSED", "CANCELLED"}:
                self.store.abort_prepared_operation(operation_id, reason="operator state change won before provider launch")
                return False
            raise
        context["operation_id"] = operation_id
        context["selected_route"] = route_id
        context["selected_provider"] = provider
        task_id = int(context["task_id"])
        task_revision = int(context.get("revision") or self.store.get_task(task_id)["revision"])
        controls = {
            "status": f"llm-bridge status --mission {self.mission_id}",
            "pause": f"llm-bridge pause --mission {self.mission_id}",
            "stop": f"llm-bridge stop --mission {self.mission_id}",
            "propose_follow_up": (
                f"llm-bridge proposal create --mission {self.mission_id} "
                f"--operation {operation_id} --file PATH"
            ),
        }
        identity = {
            "managed": True,
            "mission_id": self.mission_id,
            "task_id": task_id,
            "task_revision": task_revision,
            "operation_id": operation_id,
            "provider": provider,
            "route": route_id or None,
        }
        scheduler_config.env_overrides = {
            **getattr(scheduler_config, "env_overrides", {}),
            "LLM_BRIDGE_MANAGED": "1",
            "LLM_BRIDGE_MISSION_ID": self.mission_id,
            "LLM_BRIDGE_TASK_ID": str(task_id),
            "LLM_BRIDGE_TASK_REVISION": str(task_revision),
            "LLM_BRIDGE_OPERATION_ID": operation_id,
            "LLM_BRIDGE_PROVIDER": provider,
            "LLM_BRIDGE_ROUTE": route_id,
            "LLM_BRIDGE_CONTROL": "llm-bridge",
            "LLM_BRIDGE_CONTROLS": json.dumps(controls, ensure_ascii=True, sort_keys=True),
        }
        prior_prompt = str(getattr(scheduler_config, "prompt_text", ""))
        scheduler_config.prompt_text = (
            "Managed execution context (LLM Bridge): this provider process is launched and supervised by LLM Bridge; "
            "do not describe it as a direct or unmanaged invocation. These identifiers name the durable operation.\n"
            f"{json.dumps(identity, ensure_ascii=True, sort_keys=True)}\n"
            "Operator controls are available through the inherited LLM_BRIDGE_* environment variables. "
            "To suggest additional work, run `llm-bridge proposal create` with this mission and operation ID. "
            "Proposals require operator acceptance and are never scheduled automatically.\n\n"
            + prior_prompt
        )
        self._session_providers[operation_id] = provider
        try:
            self._session_cwds[operation_id] = str(Path(getattr(scheduler_config, "cwd", mission["cwd"])).expanduser().resolve(strict=True))
        except (OSError, RuntimeError, ValueError, TypeError):
            self._session_cwds[operation_id] = str(getattr(scheduler_config, "cwd", mission["cwd"]))
        checkpoint = repository_checkpoint(self._session_cwds[operation_id])
        context["repository_before"] = checkpoint.get("fingerprint")
        self.store.record_runtime_event(self.mission_id, "repository-checkpoint", {
            "operation_id": operation_id,
            "task_id": task_id,
            "revision": task_revision,
            "checkpoint": checkpoint,
        })
        log_config = self.store.get_mission(self.mission_id)["config"]
        scheduler_config.max_output_bytes = int(log_config.get("log_max_bytes", 4 * 1024 * 1024))
        scheduler_config.max_output_rotations = int(log_config.get("log_rotations", 3))
        scheduler_config.process_started = lambda process_pid: self._record_process(operation_id, process_pid)
        scheduler_config.process_observer = lambda: self._observe_owned_codex_session(operation_id)
        scheduler_config.should_interrupt_wait = lambda: self._should_interrupt_wait(int(context["task_id"]))
        scheduler_config.command_argv_transform = self._native_session_argv_transform(operation_id, provider, scheduler_config)
        return True

    def _native_session_argv_transform(self, operation_id: str, provider: str, scheduler_config: Any):
        if not self.native_tui or getattr(scheduler_config, "command_template", ""):
            return None

        def transform(argv: list[str]) -> list[str]:
            if not argv:
                return argv
            if provider == "codex":
                owned_argv = codex_owned_argv(argv)
                if owned_argv is not None:
                    argv = owned_argv
                    self._codex_owned_operations.add(operation_id)
                    self._session_started_ns[operation_id] = time.time_ns()
            cwd = self._session_cwds.get(
                operation_id,
                str(Path(getattr(scheduler_config, "cwd", Path.cwd())).expanduser().resolve()),
            )
            mission = self.store.get_mission(self.mission_id)
            sessions = mission["config"].get("native_sessions", {})
            raw_identity = sessions.get(provider) if isinstance(sessions, dict) else None
            expected: NativeSessionIdentity | None = None
            if isinstance(raw_identity, dict) and raw_identity.get("owned_by_bridge") is True:
                try:
                    expected = NativeSessionIdentity(
                        provider=str(raw_identity["provider"]),
                        session_id=str(raw_identity["session_id"]),
                        cwd=str(raw_identity["cwd"]),
                        source=str(raw_identity["source"]),
                    )
                except (KeyError, TypeError, ValueError):
                    expected = None
            if expected is not None:
                resume = resume_argv(expected, executable=argv[0]) if expected.provider == provider and expected.cwd == cwd else None
                if resume:
                    self._session_restore_expectations[operation_id] = expected
                    current = provider_session_metadata(provider, expected.session_id, cwd=cwd)
                    self._session_baselines[operation_id] = {str(current.path): current.modified_ns} if current else {}
                    self._session_started_ns[operation_id] = time.time_ns()
                    self.store.record_operation_session(
                        operation_id,
                        provider=provider,
                        restoration_state="RESTORING",
                        session_id=expected.session_id,
                        cwd=expected.cwd,
                        source=expected.source,
                    )
                    if provider == "codex":
                        # Codex treats -C as a global option; retain it before
                        # the exact resume subcommand and saved session UUID.
                        return [argv[0], *argv[1:-1], *resume[1:], argv[-1]]
                    return [*resume, *argv[1:]]
            if provider not in {"claude", "copilot"} or Path(argv[0]).name != provider:
                # New Codex IDs are captured only from this operation's owned
                # writable process descriptors, never from recency scans.
                return argv
            candidate = new_session_argv(provider, argv, cwd=cwd)
            if candidate is None:
                return argv
            transformed, session_id = candidate
            self._session_candidates[operation_id] = NativeSessionIdentity(
                provider,
                session_id,
                cwd,
                "claude:system.init" if provider == "claude" else "copilot:session.start",
            )
            current = provider_session_metadata(provider, session_id, cwd=cwd)
            self._session_baselines[operation_id] = {str(current.path): current.modified_ns} if current else {}
            self._session_started_ns[operation_id] = time.time_ns()
            return transformed

        return transform

    def wait_interrupted(self) -> bool:
        """Return whether an operator mission boundary should interrupt waits."""
        mission = self.store.get_mission(self.mission_id)
        if mission["status"] in {"PAUSED", "STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"}:
            return True
        config = mission["config"]
        if config.get("pause_after_iteration"):
            return True
        stop_after = config.get("stop_after_task")
        return stop_after is not None and self.store.get_task(int(stop_after))["status"] == "COMPLETED"

    def _record_session_outcome(self, operation_id: str, provider: str, cwd: str) -> None:
        expected = self._session_restore_expectations.pop(operation_id, None)
        candidate = self._session_candidates.pop(operation_id, None)
        baseline = self._session_baselines.pop(operation_id, {})
        started_ns = self._session_started_ns.pop(operation_id, 0)
        identity: NativeSessionIdentity | None = None
        restoration_state = "UNAVAILABLE"
        target = expected or candidate
        if target is not None and started_ns:
            observed = provider_session_metadata(target.provider, target.session_id, cwd=cwd)
            prior_mtime = baseline.get(str(observed.path), 0) if observed is not None else 0
            if observed is not None and observed.modified_ns >= started_ns and observed.modified_ns > prior_mtime:
                if expected is not None and confirm_restored(expected, observed.identity):
                    identity = observed.identity
                    restoration_state = "RESTORED_NATIVE"
                elif candidate is not None and confirm_restored(candidate, observed.identity):
                    identity = observed.identity
                    restoration_state = "NATIVE_CREATED"
                else:
                    restoration_state = "RECONSTRUCTED"
            elif expected is not None:
                restoration_state = "RECONSTRUCTED"
        self.store.record_operation_session(
            operation_id,
            provider=provider,
            restoration_state=restoration_state,
            session_id=identity.session_id if identity else None,
            cwd=identity.cwd if identity else None,
            source=identity.source if identity else None,
        )
        self._session_providers.pop(operation_id, None)
        self._session_cwds.pop(operation_id, None)
        self._codex_owned_operations.discard(operation_id)

    def _observe_owned_codex_session(self, operation_id: str) -> None:
        if operation_id not in self._codex_owned_operations:
            return
        operation = self.store.get_operation(operation_id)
        if operation.get("restoration_state") in {"NATIVE_CREATED", "RESTORED_NATIVE"}:
            return
        pid = int(operation.get("process_pid") or 0)
        token = str(operation.get("process_start") or "")
        cwd = self._session_cwds.get(operation_id, "")
        identity = discover_owned_codex_session(pid, token, cwd=cwd)
        if identity is None:
            return
        expected = self._session_restore_expectations.get(operation_id)
        if expected is not None and not confirm_restored(expected, identity):
            return
        if expected is None:
            self._session_candidates[operation_id] = identity
        self.store.record_operation_session(operation_id, provider="codex",
            restoration_state="RESTORED_NATIVE" if expected else "NATIVE_CREATED",
            session_id=identity.session_id, cwd=identity.cwd, source=identity.source)

    def _capture_session_after_launch(self, operation_id: str) -> None:
        """Wait briefly for exact provider-owned metadata from the live pane."""
        provider = self._session_providers.get(operation_id, "")
        cwd = self._session_cwds.get(operation_id, "")
        started_ns = self._session_started_ns.get(operation_id, 0)
        expected = self._session_restore_expectations.get(operation_id)
        candidate = self._session_candidates.get(operation_id)
        target = expected or candidate
        if target is None or not cwd or not started_ns:
            return
        baseline = self._session_baselines.get(operation_id, {})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            identity: NativeSessionIdentity | None = None
            restoration_state = ""
            observed = provider_session_metadata(target.provider, target.session_id, cwd=cwd)
            prior_mtime = baseline.get(str(observed.path), 0) if observed is not None else 0
            if observed is not None and observed.modified_ns >= started_ns and observed.modified_ns > prior_mtime:
                if expected is not None and confirm_restored(expected, observed.identity):
                    identity = observed.identity
                    restoration_state = "RESTORED_NATIVE"
                elif candidate is not None and confirm_restored(candidate, observed.identity):
                    identity = observed.identity
                    restoration_state = "NATIVE_CREATED"
            if identity is not None:
                self.store.record_operation_session(
                    operation_id,
                    provider=provider,
                    restoration_state=restoration_state,
                    session_id=identity.session_id,
                    cwd=identity.cwd,
                    source=identity.source,
                )
                return
            if self.wait_interrupted():
                return
            time.sleep(0.25)
        self.store.record_operation_session(operation_id, provider=provider, restoration_state="UNAVAILABLE")

    def _should_interrupt_wait(self, task_id: int) -> bool:
        mission = self.store.get_mission(self.mission_id)
        if mission["status"] in {"PAUSED", "STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"}:
            return True
        if mission["config"].get("pause_after_iteration"):
            return True
        stop_after = mission["config"].get("stop_after_task")
        if stop_after is not None and self.store.get_task(int(stop_after))["status"] == "COMPLETED":
            return True
        task = self.store.get_task(task_id)
        return task["status"] in {"PAUSED", "CANCELLED"}

    def _record_process(self, operation_id: str, process_pid: int) -> None:
        token = _process_start_token(process_pid)
        if not token:
            # A short provider may have exited before /proc can be read. The
            # operation will be resolved by after_iteration; a crash in this
            # small interval remains uncertain and cannot be force-signalled.
            return
        self.store.record_operation_process(operation_id, process_pid=process_pid, process_start=token)
        if operation_id in self._codex_owned_operations:
            self._observe_owned_codex_session(operation_id)
        else:
            self._capture_session_after_launch(operation_id)

    def after_iteration(
        self,
        context: dict[str, Any],
        selection: dict[str, Any],
        scheduler_config: Any,
        iteration: int,
        status: int,
        seconds: float,
    ) -> None:
        operation_id = context.get("operation_id")
        if not isinstance(operation_id, str):
            return
        try:
            operation = self.store.get_operation(operation_id)
        except KeyError:
            return
        if operation["status"] not in {"PREPARED", "RUNNING", "INTENT"}:
            return
        run_dir = scheduler_config.run_dir
        output_files = sorted(run_dir.glob("attempt-*.out")) if run_dir else []
        logs_path = str(output_files[-1]) if output_files else None
        try:
            self._record_session_outcome(
                operation_id,
                str(selection.get("provider") or scheduler_config.provider),
                str(Path(scheduler_config.cwd).expanduser().resolve(strict=True)),
            )
            task = self.store.finish_operation(operation_id, exit_code=int(status), logs_path=logs_path)
            checkpoint = repository_checkpoint(getattr(scheduler_config, "cwd", self.store.get_mission(self.mission_id)["cwd"]))
            self.store.record_runtime_event(self.mission_id, "operation-progress", {
                "operation_id": operation_id,
                "task_id": int(task["id"]),
                "revision": int(task["revision"]),
                "before": context.get("repository_before"),
                "after": checkpoint.get("fingerprint"),
                "checkpoint_reason": checkpoint.get("reason"),
                "station": str(selection.get("route") or selection.get("provider") or scheduler_config.provider),
                "exit_code": int(status),
                "output_sha256": _bounded_output_digest(output_files[-1]) if output_files else None,
            })
            if status != 0:
                continued = self.store.continue_task_after_provider_failure(
                    operation_id,
                    max_failures=self.max_provider_failures,
                )
                if continued is not None:
                    self.store.record_runtime_event(self.mission_id, "task-provider-continuation", {
                        "task_id": int(task["id"]),
                        "operation_id": operation_id,
                        "provider": str(selection.get("provider") or ""),
                        "exit_code": int(status),
                        "next_action": "reselect-provider",
                    })
            verify_requests = self.store.get_mission(self.mission_id)["config"].get("verification_requests", [])
            if status == 0 and task["status"] == "VERIFYING" and int(task["id"]) not in verify_requests:
                run_configured_verifier(self.store, self.mission_id, int(task["id"]), runtime=self.controller_logs.parent)
            self._apply_pending_boundary_requests()
            mission_config = self.store.get_mission(self.mission_id)["config"]
            log_root = getattr(scheduler_config, "log_dir", None)
            if log_root:
                _prune_old_run_logs(
                    self.store,
                    self.mission_id,
                    Path(log_root),
                    retain=int(mission_config.get("log_retained_runs", 20)),
                )
        except TransitionError:
            # A force stop may have durably reconciled this operation before
            # the scheduler noticed that its owned tmux server disappeared.
            return


def _private_verifier_dir(runtime: Path) -> Path:
    directory = runtime / "verifier"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"verifier directory must be private: {directory}")
    return directory


def _prune_old_run_logs(store: BridgeStore, mission_id: str, log_root: Path, *, retain: int) -> None:
    """Retain a bounded number of scheduler run directories with audit digests."""
    if retain < 1 or not log_root.is_dir():
        return
    directories = [path for path in log_root.iterdir() if path.is_dir() and not path.is_symlink()]
    directories.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for directory in directories[retain:]:
        try:
            canonical = directory.resolve(strict=True)
            canonical.relative_to(log_root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        for operation in store.operation_logs_in_directory(mission_id, canonical):
            logs_path = Path(str(operation["logs_path"]))
            try:
                logs_path.resolve(strict=False).relative_to(canonical)
            except ValueError:
                continue
            try:
                data = logs_path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                size = len(data)
            except OSError:
                digest, size = "unavailable", 0
            store.record_pruned_operation_log(mission_id, str(logs_path), size_bytes=size, sha256=digest)
        shutil.rmtree(canonical, ignore_errors=True)


def _bounded_output_digest(path: Path, *, max_bytes: int = 1_048_576) -> str | None:
    """Digest a bounded log snapshot without following links or trusting races."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            if before.st_size > max_bytes:
                stream.seek(before.st_size - max_bytes)
                data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    data = data[-max_bytes:]
                digest = hashlib.sha256(f"tail:{before.st_size}:".encode("ascii"))
            else:
                data = stream.read(max_bytes + 1)
                digest = hashlib.sha256()
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ):
            return None
        digest.update(data)
        return digest.hexdigest()
    except OSError:
        return None


def _artifact_records(cwd: Path, artifacts: Any) -> list[dict[str, str]]:
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("verifier artifacts must be a list of at most 100 paths")
    result: list[dict[str, str]] = []
    for item in artifacts:
        if not isinstance(item, str) or not item:
            raise ValueError("verifier artifact paths must be non-empty strings")
        path = (cwd / item).resolve(strict=True) if not Path(item).is_absolute() else Path(item).resolve(strict=True)
        try:
            relative = path.relative_to(cwd.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"verifier artifact is outside the mission working directory: {item}") from exc
        if not path.is_file():
            raise ValueError(f"verifier artifact is not a regular file: {item}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise ValueError(f"verifier artifact exceeds 50 MiB: {item}")
                digest.update(chunk)
        result.append({"path": relative.as_posix(), "sha256": digest.hexdigest(), "size": str(size)})
    return result


def _run_bounded_verifier(
    argv: list[str],
    cwd: str,
    timeout: float,
    log_path: Path,
    *,
    on_started: Callable[[int, str], None] | None = None,
    on_finished: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Drain verifier output to a capped private log without unbounded RAM/disk."""
    log_limit = 10 * 1024 * 1024
    result_limit = 1024 * 1024
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert proc.stdout is not None
    digest = hashlib.sha256()
    captured = bytearray()
    output_size = 0
    written = 0
    timed_out = False
    try:
        if on_started is not None:
            process_start = _process_start_token(proc.pid)
            if not process_start:
                raise RuntimeError("could not verify verifier process identity after launch")
            on_started(proc.pid, process_start)
        with log_path.open("wb") as log:
            os.chmod(log_path, 0o600)
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                readable, _, _ = select.select([proc.stdout], [], [], min(0.05, max(0, remaining)))
                if readable:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if chunk:
                        digest.update(chunk)
                        output_size += len(chunk)
                        if len(captured) <= result_limit:
                            captured.extend(chunk[: max(0, result_limit + 1 - len(captured))])
                        if written < log_limit:
                            allowed = min(len(chunk), log_limit - written)
                            log.write(chunk[:allowed])
                            written += allowed
                        continue
                    break
                if proc.poll() is not None and time.monotonic() - started >= timeout:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
            if output_size > written:
                log.write(b"\n[verifier output truncated at 10 MiB]\n")
            log.flush()
            os.fsync(log.fileno())
        exit_code = proc.wait()
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output": bytes(captured),
            "output_size": output_size,
            "output_sha256": digest.hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "output_truncated": output_size > result_limit,
        }
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        if on_finished is not None and proc.returncode is not None:
            on_finished(int(proc.returncode))


def run_configured_verifier(
    store: BridgeStore,
    mission_id: str,
    task_id: int,
    *,
    runtime: Path,
    timeout_override: float | None = None,
) -> dict[str, Any]:
    """Run the mission's explicit argv verifier and bind evidence to a revision."""
    mission = store.get_mission(mission_id)
    task = store.get_task(task_id)
    if task["mission_id"] != mission_id:
        raise TransitionError("configured verification task belongs to another mission")
    if task["status"] != "VERIFYING":
        return {"status": task["status"], "reason": "task-no-longer-verifying", "revision": int(task["revision"])}
    verifier = mission["config"].get("verifier")
    if not isinstance(verifier, dict) or not isinstance(verifier.get("argv"), list) or not verifier["argv"]:
        return {"status": "VERIFYING", "reason": "no-configured-verifier"}
    argv_template = verifier["argv"]
    if not all(isinstance(arg, str) and "\x00" not in arg for arg in argv_template):
        raise ValueError("verifier argv must contain only strings without NUL bytes")
    timeout = float(timeout_override if timeout_override is not None else verifier.get("timeout_seconds", 300))
    if timeout <= 0 or timeout > 3600:
        raise ValueError("verifier timeout must be between 0 and 3600 seconds")
    directory = _private_verifier_dir(runtime)
    attempt_id = secrets.token_hex(8)
    attempt_stem = f"{task_id}-r{task['revision']}-a{attempt_id}"
    context_path = directory / f"{attempt_stem}.json"
    context = {
        "mission_id": mission_id,
        "objective": mission["objective"],
        "cwd": mission["cwd"],
        "task_id": task_id,
        "title": task["title"],
        "revision": task["revision"],
        "prompt": task["prompt"],
        "acceptance": task["acceptance"],
    }
    with context_path.open("w", encoding="utf-8") as stream:
        os.chmod(context_path, 0o600)
        json.dump(context, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    substitutions = {
        "{context}": str(context_path),
        "{mission_id}": mission_id,
        "{task_id}": str(task_id),
        "{revision}": str(task["revision"]),
        "{cwd}": str(mission["cwd"]),
    }
    argv = [next((value + arg[len(key):] for key, value in substitutions.items() if arg.startswith(key)), arg) for arg in argv_template]
    log_path = directory / f"{attempt_stem}.log"
    managed_process_id = store.prepare_managed_process(mission_id, task_id, kind="verifier")
    managed_started = False
    managed_finished = False

    def mark_started(pid: int, process_start: str) -> None:
        nonlocal managed_started
        store.mark_managed_process_running(managed_process_id, process_pid=pid, process_start=process_start)
        managed_started = True

    def mark_finished(exit_code: int) -> None:
        nonlocal managed_finished
        store.finish_managed_process(
            managed_process_id,
            exit_code=exit_code,
            status="EXITED" if managed_started else "FAILED",
        )
        managed_finished = True

    try:
        run_result = _run_bounded_verifier(
            argv,
            str(mission["cwd"]),
            timeout,
            log_path,
            on_started=mark_started,
            on_finished=mark_finished,
        )
        output = run_result["output"].decode("utf-8", errors="replace")
        exit_code = int(run_result["exit_code"])
        reason = "timeout" if run_result["timed_out"] else "output-too-large" if run_result["output_truncated"] else "nonzero-exit" if exit_code else "invalid-result"
    except OSError as exc:
        log_path.touch(mode=0o600, exist_ok=True)
        os.chmod(log_path, 0o600)
        run_result = {"exit_code": None, "timed_out": False, "output": b"", "output_size": 0, "output_sha256": hashlib.sha256(b"").hexdigest(), "elapsed_seconds": 0.0, "output_truncated": False}
        output = ""
        exit_code = None
        reason = f"launch-failed: {exc}"
    finally:
        if not managed_finished:
            store.finish_managed_process(
                managed_process_id,
                exit_code=None,
                status="UNCERTAIN" if managed_started else "FAILED",
            )
        # The context contains the mission and task prompts. It is only an
        # invocation input; the immutable result log and digest are retained.
        try:
            context_path.unlink()
        except FileNotFoundError:
            pass
    outcome: dict[str, Any] = {
        "status": "VERIFYING",
        "passed": False,
        "reason": reason,
        "exit_code": exit_code,
        "elapsed_seconds": run_result["elapsed_seconds"],
        "logs_path": str(log_path),
        "revision": int(task["revision"]),
        "output_sha256": run_result["output_sha256"],
        "output_size": run_result["output_size"],
    }
    if exit_code == 0 and not run_result["output_truncated"]:
        try:
            try:
                payload = json.loads(output) if output.strip() else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if payload.get("passed") is False:
                raise ValueError("verifier reported passed: false")
            summary = str(payload.get("summary") or output.strip() or "verifier exited successfully")[:2000]
            artifacts = _artifact_records(Path(mission["cwd"]), payload.get("artifacts", []))
            evidence_payload = {
                "mission_id": mission_id,
                "task_id": task_id,
                "revision": int(task["revision"]),
                "summary": summary,
                "artifacts": artifacts,
                "output_sha256": run_result["output_sha256"],
            }
            digest = hashlib.sha256(json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            outcome.update(status="COMPLETED", passed=True, reason="passed", summary=summary, artifacts=artifacts, digest=digest)
            try:
                store.verify_task(
                    task_id,
                    evidence=f"Configured verifier passed: {summary}",
                    source="verifier",
                    metadata={"digest": digest, "artifacts": artifacts, "logs_path": str(log_path)},
                )
            except TransitionError:
                current = store.get_task(task_id)
                outcome.update(status=current["status"], passed=False, reason="task-changed-during-verification")
            return outcome
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            outcome["reason"] = f"invalid-result: {exc}"
    store.record_verification_attempt(task_id, outcome)
    return outcome


def _runtime_directory(mission_id: str) -> Path:
    directory = default_state_dir() / "runtime" / f"mission-{mission_id}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"Bridge runtime directory must be private: {directory}")
    return directory


def mission_socket_path(mission_id: str, *, env: dict[str, str] | None = None) -> Path:
    """Return a short, private tmux socket path stable across controller restarts.

    Unix-domain sockets have a small platform-dependent path limit. Durable
    Bridge state can live below a long XDG state path, so tmux sockets live in
    a separate user-private runtime directory and use a hash of the mission id.
    """
    uid = os.getuid()
    xdg_runtime = (os.environ if env is None else env).get("XDG_RUNTIME_DIR", "")
    candidates = [Path(xdg_runtime) / "llm-bridge"] if xdg_runtime else []
    candidates.append(Path("/tmp") / f"lb-{uid}")
    digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:20]
    for directory in candidates:
        socket = directory / f"m-{digest}.sock"
        # Keep below the conservative 100-byte sockaddr_un limit, including
        # the sibling config filename used by the terminal backend.
        if len(os.fsencode(str(socket.with_name(socket.name + ".conf")))) >= 96:
            continue
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != uid or info.st_mode & 0o077:
            if directory.parent == Path("/tmp"):
                raise PermissionError(f"Bridge runtime socket directory must be private: {directory}")
            continue
        return socket
    raise OSError("could not create a short private runtime directory for the tmux socket")


def _session_name(mission_id: str) -> str:
    return f"mission-{mission_id}"


def _process_start_token(process_pid: int) -> str:
    info = _proc_process_info(int(process_pid))
    return info["start"] if info and info["state"] != "Z" else ""


def _proc_process_info(process_pid: int) -> dict[str, Any] | None:
    try:
        raw = Path(f"/proc/{int(process_pid)}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if len(tail) <= 19:
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return {
            "pid": int(process_pid),
            "state": tail[0],
            "ppid": int(tail[1]),
            "pgid": int(tail[2]),
            "start": f"{boot_id}:{tail[19]}" if boot_id else "",
        }
    except (OSError, ValueError, IndexError):
        return None


def _owned_process_tree(root_pid: int, root_start: str) -> dict[int, dict[str, Any]]:
    """Snapshot a process and its current descendants with /proc start tokens."""
    try:
        entries = os.scandir("/proc")
    except OSError as exc:
        raise RuntimeError("cannot inspect /proc to verify owned provider descendants") from exc
    processes: dict[int, dict[str, Any]] = {}
    with entries:
        for entry in entries:
            if entry.name.isdigit():
                info = _proc_process_info(int(entry.name))
                if info and info["start"]:
                    processes[int(entry.name)] = info
    root = processes.get(root_pid)
    if root and root["start"] != root_start:
        raise PermissionError("provider PID no longer matches its persisted process identity")
    if root is None:
        return {}
    children: dict[int, list[int]] = {}
    for pid, info in processes.items():
        children.setdefault(int(info["ppid"]), []).append(pid)
    owned: dict[int, dict[str, Any]] = {root_pid: root}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child_pid in children.get(parent, []):
            if child_pid in owned:
                continue
            owned[child_pid] = processes[child_pid]
            pending.append(child_pid)
    return owned


def _signal_owned_process_tree(processes: dict[int, dict[str, Any]], sig: int) -> None:
    """Signal verified process groups where possible, otherwise exact PIDs."""
    group_leaders = {
        int(info["pgid"]): (int(pid), str(info["start"]))
        for pid, info in processes.items()
        if int(info["pgid"]) == int(pid)
    }
    signalled: set[int] = set()
    for process_group, (leader_pid, leader_start) in group_leaders.items():
        if _process_start_token(leader_pid) != leader_start:
            continue
        try:
            os.killpg(process_group, sig)
            signalled.add(process_group)
        except ProcessLookupError:
            pass
    for pid, info in processes.items():
        if int(info["pgid"]) in signalled:
            continue
        if _process_start_token(int(pid)) != str(info["start"]):
            continue
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            pass


def terminate_managed_processes(store: BridgeStore, mission_id: str) -> None:
    """Terminate persisted verifier groups before controller stop or restart."""
    for process in store.unresolved_managed_processes(mission_id):
        process = store.get_managed_process(str(process["id"]))
        if process["status"] not in {"STARTING", "RUNNING"}:
            continue
        pid = process.get("process_pid")
        expected = str(process.get("process_start") or "")
        if not pid or not expected:
            raise RuntimeError(
                f"managed {process['kind']} process {process['id']} has no verified identity; refusing stop/recovery"
            )
        process_pid = int(pid)
        current = _process_start_token(process_pid)
        status = "UNCERTAIN"
        if current == expected:
            try:
                _terminate_owned_process_group(process_pid, expected)
                status = "TERMINATED"
            except PermissionError:
                # It may have exited between the preflight and signal check.
                # A live process with changed identity remains a hard refusal.
                after = _process_start_token(process_pid)
                if after:
                    raise
        elif not current and _process_alive_but_unverifiable(process_pid):
            raise PermissionError(f"managed process pid {process_pid} is alive but its identity cannot be verified")
        try:
            store.finish_managed_process(process["id"], exit_code=None, status=status)
        except TransitionError:
            # The verifier may have reported its exit while teardown was
            # waiting for the process group. Preserve that committed result.
            latest = store.get_managed_process(process["id"])
            if latest["status"] in {"STARTING", "RUNNING"}:
                raise


def _process_alive_but_unverifiable(process_pid: int) -> bool:
    """Distinguish a vanished process from one whose /proc identity is unreadable."""
    try:
        raw = Path(f"/proc/{process_pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(process_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    tail = raw[raw.rfind(")") + 2:].split()
    return bool(tail and tail[0] != "Z")


def _terminate_owned_process_group(process_pid: int, process_start: str, *, timeout: float = 2.0) -> None:
    if process_pid < 2 or not process_start or _process_start_token(process_pid) != process_start:
        raise PermissionError("refusing to signal a process whose persisted identity does not match")
    try:
        if os.getpgid(process_pid) != process_pid:
            raise PermissionError("owned provider process is not a process-group leader")
    except ProcessLookupError:
        return
    owned = _owned_process_tree(process_pid, process_start)
    if not owned:
        return
    _signal_owned_process_tree(owned, signal.SIGTERM)
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        current = _owned_process_tree(process_pid, process_start)
        owned.update(current)
        _signal_owned_process_tree(current, signal.SIGTERM)
        if not _live_process_group_members(process_pid) and not _owned_process_records_alive(owned):
            return
        time.sleep(0.05)
    current = _owned_process_tree(process_pid, process_start)
    owned.update(current)
    _signal_owned_process_tree(owned, signal.SIGKILL)
    if not _live_process_group_members(process_pid) and not _owned_process_records_alive(owned):
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        current = _owned_process_tree(process_pid, process_start)
        owned.update(current)
        _signal_owned_process_tree(owned, signal.SIGKILL)
        if not _live_process_group_members(process_pid) and not _owned_process_records_alive(owned):
            return
        time.sleep(0.05)
    raise TimeoutError(f"owned process tree rooted at {process_pid} did not exit after SIGKILL")


def _owned_process_records_alive(processes: dict[int, dict[str, Any]]) -> bool:
    for pid, info in processes.items():
        current = _proc_process_info(int(pid))
        if current and current["state"] != "Z" and current["start"] == info["start"]:
            return True
    return False


def _live_process_group_members(process_group: int) -> list[int]:
    """List non-zombie members while the group ID is still allocated."""
    members: list[int] = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        # If procfs cannot establish that the group is gone, fail closed and
        # let the bounded termination path report its timeout.
        return [process_group]
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = Path(entry.path, "stat").read_text(encoding="ascii")
                fields = raw[raw.rfind(")") + 2:].split()
                if len(fields) > 2 and fields[0] != "Z" and int(fields[2]) == process_group:
                    members.append(int(entry.name))
            except (OSError, ValueError):
                continue
    return members


def _ready_file(runtime: Path) -> Path:
    return runtime / "controller-ready.json"


def _write_controller_ready(ready: Path, startup_token: str, status: str, *, tmux: bool = False) -> None:
    payload = {
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()),
        "startup_token": startup_token,
        "status": status,
        "tmux": bool(tmux),
    }
    temporary = ready.with_name(f"{ready.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ready)


def _install_frozen_tool_config(mission: dict[str, Any], runtime: Path) -> None:
    path = runtime / "config.toml"
    content = str(mission["config"].get("frozen_tool_config", ""))
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.environ["LLM_TOOLS_CONFIG"] = str(path)


def _provider_station_argv(native_tui: bool, controller_log: Path) -> list[str]:
    if native_tui:
        # Native mode respawns this retained pane only after its prior CLI exits.
        return ["/bin/sh", "-c", "exit 0"]
    # Autonomous mode presents the real headless activity stream in the
    # provider-sized pane while the separate Bridge pane remains interactive.
    return ["tail", "-n", "50", "-F", str(controller_log)]


def _process_controller_lock(mission_id: str) -> MissionLock:
    return MissionLock(default_state_dir() / "locks", mission_id)


def _open_existing(backend: TmuxTerminalBackend, runtime: Path, mission_id: str) -> TerminalHandle | None:
    socket = mission_socket_path(mission_id)
    if not socket.exists():
        return None
    return backend.open_existing(socket_path=socket, session_name=_session_name(mission_id), bridge_rows=6)


def run_controller(mission_id: str, *, headless: bool = False, native_tui: bool = False, providers: str = "", routes: str = "", startup_token: str = "") -> int:
    """Own one mission until it is completed, stopped, or requires recovery."""
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return 2
    runtime = _runtime_directory(mission_id)
    ready = _ready_file(runtime)
    try:
        ready.unlink()
    except FileNotFoundError:
        pass

    with lock, BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        # Recovery must use the mode that owned the previous station. A new
        # explicit mode takes effect only after old owned resources are cleaned
        # up successfully, then becomes durable before new provider work.
        prior_headless = bool(mission["config"].get("headless", False))
        prior_native_tui = bool(mission["config"].get("native_tui", False))
        if headless and native_tui:
            raise ValueError("--headless and --native cannot be combined")
        if headless:
            effective_headless, effective_native_tui = True, False
        elif native_tui:
            effective_headless, effective_native_tui = False, True
        else:
            effective_headless, effective_native_tui = prior_headless, prior_native_tui
        headless, native_tui = effective_headless, effective_native_tui
        providers = str(providers or mission["config"].get("providers", "claude,codex,opencode"))
        routes = str(routes or mission["config"].get("routes", ""))
        interrupted = [
            dict(row)
            for row in store.db.execute(
                "SELECT * FROM operations WHERE mission_id=? AND status IN ('INTENT','PREPARED','RUNNING')",
                (mission_id,),
            )
        ]
        interrupted_managed = store.unresolved_managed_processes(mission_id)
        # Ownership is established before recovery. An unresolved operation is
        # uncertain; its task is blocked and is never replayed automatically.
        store.reconcile_interrupted_operations(mission_id)
        if interrupted_managed:
            terminate_managed_processes(store, mission_id)
        # A controller crash does not kill a native tmux pane or a detached
        # headless process group. Resolve those exact owned identities before
        # creating a replacement controller/station, after first making the
        # durable operation UNCERTAIN so recovery cannot replay it.
        prior_handle: TerminalHandle | None = None
        if not prior_headless and shutil.which("tmux"):
            prior_handle = _open_existing(TmuxTerminalBackend(), runtime, mission_id)
            if prior_handle is not None:
                TmuxTerminalBackend().stop(prior_handle)
        terminated_pids: set[int] = set()
        for operation in interrupted:
            process_pid = operation.get("process_pid")
            process_start = operation.get("process_start")
            if process_pid and process_start and int(process_pid) not in terminated_pids:
                current_identity = _process_start_token(int(process_pid))
                if current_identity and current_identity != str(process_start):
                    raise PermissionError(
                        f"persisted provider pid {process_pid} now belongs to a different process; refusing recovery cleanup"
                    )
                if current_identity:
                    _terminate_owned_process_group(int(process_pid), str(process_start))
                terminated_pids.add(int(process_pid))
            elif operation["status"] == "RUNNING" and (prior_headless or prior_handle is None):
                raise RuntimeError(
                    f"cannot prove identity of surviving provider operation {operation['id']}; refusing to start replacement work"
                )
        for command in store.pending_commands(mission_id):
            store.finish_command(command["id"], status="REJECTED", result={"error": "controller restarted; queued command was not replayed"})
        mission = store.get_mission(mission_id)
        if mission["status"] in {"CREATED", "PAUSED", "STOPPED", "FAILED"}:
            store.set_mission_status(mission_id, "RUNNING")
        elif mission["status"] == "STOPPING":
            # The previous controller may have exited before finalizing its
            # transition. Its station cleanup was attempted above; only now
            # is STOPPED truthful.
            store.set_mission_status(mission_id, "STOPPED")
            _write_controller_ready(ready, startup_token, "STOPPED")
            return 0
        elif mission["status"] in {"COMPLETED", "CANCELLED"}:
            _write_controller_ready(ready, startup_token, mission["status"])
            return 0

        mission = store.set_mission_config_values(
            mission_id,
            {"headless": headless, "native_tui": native_tui, "providers": providers, "routes": routes},
            event_kind="controller-config",
        )
        prior_headless = bool(mission["config"].get("headless", False))

        _install_frozen_tool_config(mission, runtime)
        backend: TmuxTerminalBackend | None = None
        handle: TerminalHandle | None = None
        if not headless:
            if not shutil.which("tmux"):
                store.set_mission_status(mission_id, "FAILED")
                raise RuntimeError("tmux is required for native mode; retry with --headless")
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is None:
                controller_log = runtime / "controller.log"
                controller_log.touch(mode=0o600, exist_ok=True)
                os.chmod(controller_log, 0o600)
                bridge_argv = [sys.executable, "-m", "llm_tools.bridge", "console", "--mission", mission_id]
                handle = backend.start(
                    provider_argv=_provider_station_argv(native_tui, controller_log),
                    bridge_argv=bridge_argv,
                    cwd=mission["cwd"],
                    socket_path=mission_socket_path(mission_id),
                    session_name=_session_name(mission_id),
                    size=(120, 40),
                )

        _write_controller_ready(ready, startup_token, "RUNNING", tmux=bool(handle))
        actual_mode = "native" if native_tui else "headless" if headless else "autonomous"
        store.record_runtime_event(mission_id, "controller-mode", {
            "verified": True,
            "actual_mode": actual_mode,
            "controller_pid": os.getpid(),
        })
        refresh_worker: CapacityRefreshWorker | None = None
        try:
            configured_routes = routes.split(",") if routes else providers.split(",")
            handoff_budget = max(2, len([item for item in configured_routes if item.strip()]) * 2)
            hooks = BridgeMissionHooks(mission_id, handle=handle, headless=headless, native_tui=native_tui, max_provider_failures=handoff_budget, controller_logs=runtime / "controller.log")
            hooks.bind(store)
            refresh_worker = CapacityRefreshWorker(
                mission_id,
                default_state_dir() / "bridge.sqlite3",
                interval_seconds=int(mission["config"].get("capacity_refresh_seconds", 60)),
            )
            refresh_worker.start()
            argv = [
                "--prompt", mission["objective"],
                "--cwd", mission["cwd"],
                "--providers", providers,
                "--max-iterations", "0",
                "--max-duration", "0",
                "--min-iteration-seconds", "0",
                "--log-dir", str(runtime / "ralph-logs"),
                "--state-file", str(runtime / "ralph-state.json"),
            ]
            if routes:
                argv.extend(["--routes", routes])
            command_template = str(mission["config"].get("command_template", ""))
            if command_template:
                argv.extend(["--command-template", command_template])
            return ralph_robin.main(argv, bridge_hooks=hooks)
        except BaseException as exc:
            store.reconcile_interrupted_operations(mission_id)
            current = store.get_mission(mission_id)
            if current["status"] in {"RUNNING", "PAUSED", "STOPPING"}:
                store.set_mission_status(mission_id, "FAILED")
            print(f"controller failure: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return int(exc.code) if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
        finally:
            if refresh_worker is not None:
                refresh_worker.stop()
            current = store.get_mission(mission_id)
            # Confirm the managed terminal station is gone before claiming the
            # stop boundary. A cleanup error deliberately leaves STOPPING and
            # keeps the socket/config coordinates available for retry.
            if backend is not None and handle is not None and current["status"] in {"STOPPING", "CANCELLED"}:
                backend.stop(handle)
                handle = None
            if current["status"] == "STOPPING":
                store.set_mission_status(mission_id, "STOPPED")
                current = store.get_mission(mission_id)
            if current["status"] == "RUNNING":
                store.record_runtime_event(mission_id, "controller-exited-unexpectedly", {
                    "reason": "controller returned while the mission remained RUNNING",
                })
                store.set_mission_status(mission_id, "FAILED")
                current = store.get_mission(mission_id)
            if backend is not None and handle is not None and current["status"] in {"STOPPED", "CANCELLED"}:
                backend.stop(handle)


def launch_controller(
    mission_id: str,
    *,
    headless: bool | None = None,
    providers: str | None = None,
    routes: str | None = None,
    native_tui: bool | None = None,
) -> int:
    """Start a detached mission owner, then attach the terminal if interactive."""
    if native_tui is True and headless is True:
        raise ValueError("--headless and --native cannot be combined")
    with BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        saved_headless = bool(mission["config"].get("headless", False))
        saved_native = bool(mission["config"].get("native_tui", False))
    if headless is None and native_tui is None:
        headless, native_tui = saved_headless, saved_native
    elif headless is True:
        headless, native_tui = True, False
    elif native_tui is True:
        headless, native_tui = False, True
    else:
        headless, native_tui = bool(headless), bool(native_tui)
    if native_tui and headless:
        raise ValueError("--headless and --native cannot be combined")
    if not headless and not shutil.which("tmux"):
        raise RuntimeError("tmux is required for native mode; retry with --headless")
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        if not headless and sys.stdin.isatty() and sys.stdout.isatty():
            runtime = _runtime_directory(mission_id)
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is not None:
                return backend.attach(handle)
        print(f"mission {mission_id} already has a controller; use attach or status", file=sys.stderr)
        return 2
    lock.release()

    with BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        if providers:
            providers_value = providers
        else:
            providers_value = str(mission["config"].get("providers", "claude,codex,opencode"))
        if routes:
            routes_value = routes
        else:
            routes_value = str(mission["config"].get("routes", ""))
    runtime = _runtime_directory(mission_id)
    log_path = runtime / "controller.log"
    ready = _ready_file(runtime)
    try:
        ready.unlink()
    except FileNotFoundError:
        pass
    startup_token = secrets.token_hex(16)
    env = os.environ.copy()
    env["LLM_BRIDGE_STATE_DIR"] = str(default_state_dir())
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH", ""))))
    command = [sys.executable, "-m", "llm_tools.bridge", "_controller", "--mission", mission_id, "--providers", providers_value, "--startup-token", startup_token]
    if routes_value:
        command += ["--routes", routes_value]
    if headless:
        command.append("--headless")
    if native_tui:
        command.append("--native")
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        proc = subprocess.Popen(command, cwd=mission["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.is_file():
            try:
                status = json.loads(ready.read_text(encoding="utf-8"))
                if int(status.get("pid", -1)) == proc.pid and status.get("startup_token") == startup_token and status.get("status") in {"RUNNING", "COMPLETED", "STOPPED", "CANCELLED"}:
                    break
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"mission controller exited with status {proc.returncode}; inspect {log_path}")
        time.sleep(0.05)
    else:
        raise RuntimeError(f"mission controller did not become ready; inspect {log_path}")

    if headless or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"mission {mission_id} running; controller log: {log_path}")
        return 0
    backend = TmuxTerminalBackend()
    handle = _open_existing(backend, runtime, mission_id)
    if handle is None:
        raise RuntimeError(f"mission is running but its terminal station is unavailable; inspect {log_path}")
    return backend.attach(handle)
