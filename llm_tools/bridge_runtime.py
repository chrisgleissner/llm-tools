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
from collections.abc import Mapping
from typing import Any, Callable

from . import common
from .common import LAUNCH_REFUSED_STATUS, python_module_argv, rooted_module_argv, scrubbed_environment
from .bridge_store import BridgeStore, MissionLock, TransitionError, default_state_dir, store_path
from .bridge_states import (
    ACTIVE_OPERATION_STATUSES,
    CONTROLLED_MISSION_STATUSES, ENDED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, HALTED_MISSION_STATUSES,
    OPERATOR_DECLINED_EXIT_CODE, STATION_CLOSING_MISSION_STATUSES,
)
from .bridge_refresh import CapacityRefreshWorker
from .bridge_progress import repository_checkpoint
from .bridge_sessions import (
    NativeSessionIdentity,
    boot_id,
    child_pids,
    confirm_restored,
    codex_owned_argv,
    discover_owned_codex_session,
    new_session_argv,
    provider_session_metadata,
    resume_argv,
)
from .terminal import DeadStationError, TerminalHandle, TmuxTerminalBackend


def __getattr__(name: str) -> Any:
    # ``ralph_robin`` (and the scheduler behind it) is only needed by the controller; importing it here would
    # slow every command that merely locates a station socket. It stays readable as ``bridge_runtime.ralph_robin``.
    if name == "ralph_robin":
        from . import ralph_robin

        globals()[name] = ralph_robin
        return ralph_robin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


NATIVE_COMPLETION_NOTE = (
    "When the task is complete, summarise what you did and ask the operator to exit this CLI "
    "(for example /exit) so Bridge can verify the work."
)
_CHECKPOINT_LIMIT = 12000
_EVENT_LINE_LIMIT = 300


def _compact(value: Any, limit: int = _EVENT_LINE_LIMIT) -> str:
    """One bounded line for a scalar or nested value."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_acceptance(acceptance: Any) -> str:
    """Acceptance criteria as a numbered list, or ``none recorded``."""
    items = acceptance if isinstance(acceptance, (list, tuple)) else ([acceptance] if acceptance else [])
    lines = [f"{number}. {_compact(item, 1000)}" for number, item in enumerate(items, 1) if item not in (None, "")]
    return "\n".join(lines) or "none recorded"


def _event_summary(data: Any) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            others = {key: value for key, value in data.items() if key != "summary" and value not in (None, "")}
            extra = ", ".join(f"{key}={_compact(value, 80)}" for key, value in others.items())
            return _compact(data["summary"]) + (f" ({extra})" if extra else "")
        return ", ".join(f"{key}={_compact(value, 80)}" for key, value in data.items() if value not in (None, "")) or "no details"
    return _compact(data) if data not in (None, "") else "no details"


def _format_checkpoint(checkpoint: dict[str, Any]) -> str:
    """Readable, deterministic, bounded text for the durable checkpoint."""
    lines = [
        f"restoration: {checkpoint.get('restoration')}",
        f"task revision: {checkpoint.get('task_revision')}",
    ]
    evidence = checkpoint.get("evidence")
    if isinstance(evidence, (list, tuple)):
        evidence_lines = [_compact(item) for item in evidence if item not in (None, "")]
    else:
        evidence_lines = [_compact(evidence)] if evidence not in (None, "", {}, []) else []
    if evidence_lines:
        lines.append("evidence:")
        lines.extend(f"- {line}" for line in evidence_lines)
    else:
        lines.append("evidence: none recorded")
    operations = checkpoint.get("operations") or []
    lines.append("operations (newest first):" if operations else "operations: none")
    for operation in operations:
        fields = (("station", "station"), ("route", "route_id"), ("model", "model"), ("status", "status"), ("exit", "exit_code"), ("logs", "logs_path"))
        detail = ", ".join(f"{label} {_compact(operation[key], 200)}" for label, key in fields if operation.get(key) not in (None, ""))
        lines.append(f"- operation {operation.get('id')}: {detail or 'no details'}")
    events = checkpoint.get("recent_events") or []
    lines.append("recent events (oldest first):" if events else "recent events: none")
    for event in events:
        lines.append(f"- {event.get('kind')}: {_event_summary(event.get('data'))} (event {event.get('id')}, {event.get('at')})")
    return "\n".join(lines)[:_CHECKPOINT_LIMIT]


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
        self._codex_observe_schedule: dict[str, tuple[int, float]] = {}  # operation -> (backoff step or -1 once found, next look)
        # The (providers, routes) pair the running Ralph loop currently uses.
        self.applied_rotation: tuple[str, str] | None = None
        self._waiting_message = ""
        self._hold_message = ""
        self._hold_message_tried = 0.0

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
        data = {
            "target_epoch": target_epoch,
            "all_rate_limited": projection["all_rate_limited"],
            "reason": projection["decision"].get("reason", projection["rotation_reason"]),
            "decisions": projection["decisions"],
        }
        self.store.record_runtime_event(self.mission_id, "provider-wait", data)
        self._show_waiting_message(data)

    def _show_waiting_message(self, wait_data: dict[str, Any]) -> None:
        """Tell the operator why an idle native provider pane stays empty.

        Only a pane with nothing in it is written to; a live provider, or the
        output of a previous provider, is never replaced. The scheduler
        respawns the pane when a provider launches, which removes the message.
        """
        if self.handle is None or not self.native_tui:
            return
        from .bridge_monitor import waiting_from_wait, waiting_message

        now = time.time()
        message = waiting_message(waiting_from_wait(wait_data, now), now)
        if message == self._waiting_message:
            return
        try:
            if TmuxTerminalBackend().show_provider_message(self.handle, message):
                self._waiting_message = message
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass  # the message is a courtesy; the wait itself must continue

    def _show_review_hold(self, hold: dict[str, Any]) -> None:
        """Tell the operator, in the provider pane, which task waits for their decision."""
        if self.handle is None or not self.native_tui:
            return
        message = f"Task {hold['task_ordinal']} is waiting for your review. Ctrl-] then B, then /approve or /reject."
        now = time.monotonic()
        if message == self._hold_message or now - self._hold_message_tried < 5.0:
            return
        self._hold_message_tried = now
        try:
            # Only an empty provider pane takes the placeholder text; a pane that
            # still holds the finished provider's output is left alone (see
            # ``_publish_review_hint`` for its footer).
            if TmuxTerminalBackend().show_provider_message(self.handle, message):
                self._hold_message = message
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass  # a courtesy message; the hold itself must continue

    def _publish_review_hint(self, mission: dict[str, Any], task_id: int) -> None:
        """Prepare the footer tmux prints in the provider pane when this run exits cleanly.

        tmux formats that footer once, when the pane's process dies, so the text
        has to be in place before the provider is launched. A clean exit moves
        the task to review, so the hint names it; it is left out when a
        configured verifier may complete the task, or when the mission starts
        the next task without waiting.
        """
        if self.handle is None or not self.native_tui:
            return
        config = mission["config"]
        text = ""
        if not self.store.auto_continue_default(config) and not (config.get("verifier") or {}).get("argv"):
            ordinal = self.store.get_task(task_id)["ordinal"]
            text = f"Task {ordinal} is waiting for your review. Ctrl-] then B, then /approve or /reject."
        try:
            TmuxTerminalBackend().set_review_hint(self.handle, text)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass  # a courtesy footer; the launch must continue

    def rotation_update(self) -> tuple[str, str] | None:
        """The saved rotation when it differs from the one the loop is using."""
        config = self.store.get_mission(self.mission_id)["config"]
        saved = (str(config.get("providers", "")), str(config.get("routes", "")))
        if self.applied_rotation is None or saved == self.applied_rotation:
            return None
        return saved

    def rotation_applied(self, rotation: tuple[str, str], error: str) -> None:
        """Record whether the loop switched to a changed rotation."""
        self.applied_rotation = rotation
        kind = "rotation-rejected" if error else "rotation-applied"
        self.store.record_runtime_event(self.mission_id, kind, {"providers": rotation[0], "routes": rotation[1], "error": error})

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

            hold = self.store.sync_review_hold_events(self.mission_id)
            if hold is not None:
                # A native operator is at the station: do not start the next
                # task (and its paid run) until they decide on the reviewed one.
                self._show_review_hold(hold)
                time.sleep(0.5)
                continue
            self._hold_message = ""

            tasks = self.store.ready_tasks(self.mission_id, limit=1)
            if tasks:
                task = tasks[0]
                current = self.store.begin_iteration(int(task["id"]))
                self._iteration += 1
                history = self.store.recent_task_history(self.mission_id, int(current["id"]))
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
                rejection = self.store.latest_rejection_reason(self.mission_id, int(current["id"]))
                rejection_text = (
                    f"Previous attempt was rejected by the operator: {rejection}\n"
                    "Address this feedback in this attempt before finishing.\n\n"
                    if rejection
                    else ""
                )
                prompt = (
                    "LLM Bridge mission context. Provider session continuity is RECONSTRUCTED from durable state; "
                    "this is not an exact native conversation restore.\n\n"
                    f"Mission objective:\n{mission['objective']}\n\n"
                    f"Task {current['ordinal']} ({current['title']}), revision {current['revision']}:\n{current['prompt']}\n\n"
                    f"Acceptance criteria:\n{_format_acceptance(current['acceptance'])}\n\n"
                    f"{rejection_text}"
                    f"Durable checkpoint (latest bounded history):\n{_format_checkpoint(checkpoint)}\n\n"
                    "Continue from this state. Do not claim work from an earlier provider session unless supported by the evidence above."
                )
                if self.native_tui and not self.headless:
                    prompt += f" {NATIVE_COMPLETION_NOTE}"
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
            self._publish_review_hint(mission, int(context["task_id"]))
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
        if not self.native_tui or getattr(scheduler_config, "command_template", ""):  # type: ignore[arg-type]  # mypy picks the bool overload of getattr in a boolean context
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
        if mission["status"] in HALTED_MISSION_STATUSES | ENDED_MISSION_STATUSES:
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
        self._codex_observe_schedule.pop(operation_id, None)

    # Seconds between looks for a Codex session while none has been found: the
    # session file is opened within the first moments of a run, so the search
    # starts fast and slows to a rarely-repeated check.
    CODEX_OBSERVE_BACKOFF = (1.0, 2.0, 5.0, 15.0)

    def _observe_owned_codex_session(self, operation_id: str, *, force: bool = False) -> None:
        if operation_id not in self._codex_owned_operations:
            return
        now = time.monotonic()
        step, next_at = self._codex_observe_schedule.get(operation_id, (0, 0.0))
        if step < 0 or (not force and now < next_at):
            return  # found already, or not yet due
        backoff = self.CODEX_OBSERVE_BACKOFF
        self._codex_observe_schedule[operation_id] = (min(step + 1, len(backoff) - 1), now + backoff[min(step, len(backoff) - 1)])
        operation = self.store.get_operation(operation_id)
        if operation.get("restoration_state") in {"NATIVE_CREATED", "RESTORED_NATIVE"}:
            self._codex_observe_schedule[operation_id] = (-1, 0.0)
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
        self._codex_observe_schedule[operation_id] = (-1, 0.0)  # found: stop looking

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
        """A scheduler wait stops at any mission boundary or when this task is paused or cancelled."""
        if self.wait_interrupted():
            return True
        return self.store.get_task(task_id)["status"] in {"PAUSED", "CANCELLED"}

    def _record_process(self, operation_id: str, process_pid: int) -> None:
        token = _process_start_token(process_pid)
        if not token:
            # A short provider may have exited before /proc can be read. The
            # operation will be resolved by after_iteration; a crash in this
            # small interval remains uncertain and cannot be force-signalled.
            return
        self.store.record_operation_process(operation_id, process_pid=process_pid, process_start=token)
        if operation_id in self._codex_owned_operations:
            self._observe_owned_codex_session(operation_id, force=True)
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
        if operation["status"] not in ACTIVE_OPERATION_STATUSES:
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
            if status == OPERATOR_DECLINED_EXIT_CODE:
                provider_name = str(selection.get("provider") or scheduler_config.provider)
                self.store.record_runtime_event(self.mission_id, "provider-declined", {
                    "operation_id": operation_id,
                    "task_id": int(task["id"]),
                    "provider": provider_name,
                    "reason": "folder-trust-declined",
                    "next_action": "operator",
                })
            elif status == LAUNCH_REFUSED_STATUS:
                reason = ""
                if logs_path:
                    try:
                        reason = Path(logs_path).read_text(encoding="utf-8", errors="replace").strip()[:500]
                    except OSError:
                        pass
                self.store.record_runtime_event(self.mission_id, "task-launch-refused", {
                    "task_id": int(task["id"]),
                    "operation_id": operation_id,
                    "provider": str(selection.get("provider") or ""),
                    "reason": reason or "the provider command could not be launched",
                })
            elif status != 0:
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


def _signal_process_group(process_group: int, sig: int) -> None:
    """Send ``sig`` to a process group, ignoring a group that has already exited."""
    try:
        os.killpg(process_group, sig)
    except ProcessLookupError:
        pass


def _kill_process_group(proc: subprocess.Popen[bytes], grace: float = 0.5) -> None:
    """Stop a process started in its own session: SIGTERM, then SIGKILL after ``grace`` seconds, then reap it."""
    _signal_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc.pid, signal.SIGKILL)
        proc.wait()


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
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=scrubbed_environment(os.environ, None))
    assert proc.stdout is not None
    digest = hashlib.sha256()
    captured = bytearray()
    output_size = 0
    written = 0
    timed_out = False
    try:
        if on_started is not None:
            process_start = _process_start_token(proc.pid)
            if process_start:
                on_started(proc.pid, process_start)
            elif proc.poll() is None:
                raise RuntimeError("could not verify verifier process identity after launch")
            # else: the verifier already exited (and was reaped by poll), so
            # /proc has no identity to record. That is a normal fast exit, the
            # same tolerance _record_process has for short providers; its
            # output and exit code are collected below.
        with log_path.open("wb") as log:
            os.chmod(log_path, 0o600)
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    _kill_process_group(proc)
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
                    _signal_process_group(proc.pid, signal.SIGKILL)
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
            _kill_process_group(proc)
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
    from .bridge_verifier_guard import changed_verifier_files

    changed = changed_verifier_files(verifier, str(mission["cwd"]))
    if changed:
        # The provider can edit these files, so a pass from them proves nothing:
        # do not run them and leave the task for the operator to approve.
        outcome = {
            "status": "VERIFYING",
            "passed": False,
            "reason": "verifier-modified",
            "detail": "verifier files changed since the mission was created: " + ", ".join(changed[:10]) + "; operator approval required",
            "changed_files": changed[:50],
            "revision": int(task["revision"]),
        }
        store.record_verification_attempt(task_id, outcome)
        return outcome
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
            # A verifier that exited before its identity could be read ran to
            # completion (exit code >= 0); one killed because its identity
            # could not be verified did not.
            status="EXITED" if managed_started or exit_code >= 0 else "FAILED",
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
    outcome: dict[str, Any] = {  # type: ignore[no-redef]  # the name is already bound earlier in this function
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


_RUN_USER_ROOT = Path("/run/user")
_TMP_ROOT = Path("/tmp")


def _socket_candidate_dirs(uid: int, env: dict[str, str] | None) -> tuple[list[Path], list[Path]]:
    """Return (lookup, creation) socket directories, most specific first."""
    source = os.environ if env is None else env
    xdg_runtime = source.get("XDG_RUNTIME_DIR", "")
    primary = [Path(xdg_runtime) / "llm-bridge"] if xdg_runtime else []
    tmp = _tmp_fallback_dirs(uid)
    last_resort = [_cache_runtime_dir(source)]
    creation = [*primary, *tmp, *last_resort]
    # A second login (sudo -iu, some SSH/cron setups) may lack or differ in
    # XDG_RUNTIME_DIR; the per-user runtime directory is still discoverable.
    lookup = [*primary, _RUN_USER_ROOT / str(uid) / "llm-bridge", *tmp, *last_resort]
    return list(dict.fromkeys(lookup)), creation


def _cache_runtime_dir(env: Mapping[str, str]) -> Path:
    """Private per-user directory used when every shared ``/tmp`` name is taken by someone else."""
    base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".cache")
    return Path(base) / "llm-tools" / "runtime"


def _tmp_fallback_dirs(uid: int) -> list[Path]:
    """Short ``/tmp`` socket directories, tried in order.

    ``/tmp`` is shared, so another user can pre-create ``/tmp/lb-<uid>`` (or
    replace it with a symlink). A directory that is not a real, private
    directory owned by this user is skipped and the next numbered name is used;
    the names are fixed so later commands find the same station.
    """
    return [_TMP_ROOT / f"lb-{uid}", *(_TMP_ROOT / f"lb-{uid}-{index}" for index in range(1, 5))]


def _trusted_tmp_directory(directory: Path, uid: int) -> bool:
    """Create ``directory`` (mode 0700) if absent; accept it only if it is ours and private."""
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return False
    return _private_owned_directory(directory, uid)


def _short_enough(socket: Path) -> bool:
    # Keep below the conservative 100-byte sockaddr_un limit, including the
    # sibling config filename used by the terminal backend.
    return len(os.fsencode(str(socket.with_name(socket.name + ".conf")))) < 96


def _private_owned_directory(directory: Path, uid: int) -> bool:
    try:
        info = directory.lstat()  # a symlink is never accepted, even to a directory we own
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077


def mission_socket_path(mission_id: str, *, env: dict[str, str] | None = None) -> Path:
    """Return a short, private tmux socket path stable across controller restarts.

    Unix-domain sockets have a small platform-dependent path limit. Durable
    Bridge state can live below a long XDG state path, so tmux sockets live in
    a separate user-private runtime directory and use a hash of the mission id.

    An already existing socket for this mission is found in any candidate
    directory (``$XDG_RUNTIME_DIR/llm-bridge``, ``/run/user/<uid>/llm-bridge``,
    ``/tmp/lb-<uid>``) so a login with a different environment still reaches a
    running station. Only when none exists is a creation directory chosen.
    """
    uid = os.getuid()
    lookup, creation = _socket_candidate_dirs(uid, env)
    digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:20]
    for directory in lookup:
        socket = directory / f"m-{digest}.sock"
        if _short_enough(socket) and _private_owned_directory(directory, uid) and socket.exists():
            return socket
    for directory in creation:
        socket = directory / f"m-{digest}.sock"
        if not _short_enough(socket):
            continue
        if directory.parent == _TMP_ROOT:
            if _trusted_tmp_directory(directory, uid):
                return socket
            continue
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.stat()
        except OSError:
            continue
        if info.st_uid != uid or info.st_mode & 0o077:
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
        boot = boot_id()
        return {
            "pid": int(process_pid),
            "state": tail[0],
            "ppid": int(tail[1]),
            "pgid": int(tail[2]),
            "start": f"{boot}:{tail[19]}" if boot else "",
        }
    except (OSError, ValueError, IndexError):
        return None


def _current_owned_tree(root_pid: int, root_start: str) -> dict[int, dict[str, Any]]:
    """``_owned_process_tree`` for repeated polling: follows child lists instead of scanning all of ``/proc``.

    Falls back to the scan when the kernel does not provide child lists or the
    root is not a live process. A root whose start token changed is refused,
    exactly as the scan refuses it.
    """
    root = _proc_process_info(root_pid)
    if root is None or child_pids(root_pid) is None:
        return _owned_process_tree(root_pid, root_start)
    if root["start"] != root_start:
        raise PermissionError("provider PID no longer matches its persisted process identity")
    owned: dict[int, dict[str, Any]] = {root_pid: root}
    pending = [root_pid]
    while pending:
        children = child_pids(pending.pop())
        if children is None:
            return _owned_process_tree(root_pid, root_start)
        for child_pid in children:
            if child_pid in owned:
                continue
            info = _proc_process_info(child_pid)
            if info and info["start"]:
                owned[child_pid] = info
                pending.append(child_pid)
    return owned


def _process_group_absent(process_group: int) -> bool:
    """True when no process, not even an unreaped zombie, belongs to the group (one syscall)."""
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


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
    if process_pid < 2 or not process_start:
        raise PermissionError("refusing to signal a process whose persisted identity does not match")
    current = _process_start_token(process_pid)
    if current != process_start:
        if not current and not _process_alive_but_unverifiable(process_pid):
            return  # already exited (no /proc entry, or an unreaped zombie): nothing to signal
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
        owned_now = _current_owned_tree(process_pid, process_start)
        owned.update(owned_now)
        _signal_owned_process_tree(owned_now, signal.SIGTERM)
        if (_process_group_absent(process_pid) or not _live_process_group_members(process_pid)) and not _owned_process_records_alive(owned):
            return
        time.sleep(0.05)
    owned_now = _current_owned_tree(process_pid, process_start)
    owned.update(owned_now)
    _signal_owned_process_tree(owned, signal.SIGKILL)
    if (_process_group_absent(process_pid) or not _live_process_group_members(process_pid)) and not _owned_process_records_alive(owned):
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        owned_now = _current_owned_tree(process_pid, process_start)
        owned.update(owned_now)
        _signal_owned_process_tree(owned, signal.SIGKILL)
        if (_process_group_absent(process_pid) or not _live_process_group_members(process_pid)) and not _owned_process_records_alive(owned):
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




def _clean_line(text: Any, limit: int = 300) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _last_logged_error(log_path: Path) -> str:
    """The last ``error:`` line in the tail of the controller log, if the log has one yet."""
    try:
        sys.stderr.flush()
        time.sleep(0.2)  # the log sink drains the controller's stderr pipe asynchronously
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    errors = [line.strip() for line in lines if line.strip().startswith("error:")]
    return errors[-1][len("error:"):].strip() if errors else ""


def controller_failure_reason(exc: BaseException, log_path: Path) -> str:
    """One line saying why the controller failed."""
    if isinstance(exc, SystemExit):
        logged = _last_logged_error(log_path)
        return _clean_line(logged or f"ralph-robin exited with status {exc.code}; see {log_path}")
    text = str(exc).strip()
    return _clean_line(text if text else type(exc).__name__)


def record_controller_failure(store: BridgeStore, mission_id: str, reason: str) -> None:
    """Persist why the controller failed so status and the station can show it."""
    try:
        store.record_runtime_event(mission_id, "controller-failed", {"reason": _clean_line(reason), "controller_pid": os.getpid()})
    except (ValueError, TransitionError, OSError):
        pass  # recording the reason must not mask the failure itself


def rotation_problem(mission: dict[str, Any], providers: str, routes: str) -> str | None:
    """Why this rotation can never launch under the mission's frozen config, or ``None``."""
    from . import bridge_rotation

    conf = bridge_rotation.parse_frozen(mission["config"].get("frozen_tool_config"))
    return bridge_rotation.check_rotation(
        bridge_rotation.frozen_enabled(conf), conf, providers, routes, config_label="", explicit=False,
    )


def station_detach_argv(mission_id: str) -> list[str]:
    """Command the station's tmux server runs whenever a client detaches."""
    package_root = str(Path(__file__).resolve().parent.parent)
    return [
        "env",
        f"LLM_BRIDGE_STATE_DIR={default_state_dir()}",
        "PYTHONPATH=" + os.pathsep.join(filter(None, (package_root, os.environ.get("PYTHONPATH", "")))),
        *python_module_argv("llm_tools.bridge", "_station-gc", "--mission", mission_id, "--exclude-client", "{client}"),
    ]


def close_finished_station(
    store: BridgeStore,
    backend: TmuxTerminalBackend,
    handle: TerminalHandle,
    mission_id: str,
    *,
    exclude_clients: tuple[str, ...] = (),
) -> str:
    """Close the station of a finished mission once nobody is using it.

    Returns ``closed``, ``not-finished``, ``provider-alive`` (a native provider
    process still runs in its pane) or ``clients-attached``. A station that is
    kept is closed later, when its last client detaches (see
    ``station_detach_argv``).
    """
    mission = store.get_mission(mission_id)
    if mission["status"] not in STATION_CLOSING_MISSION_STATUSES:
        return "not-finished"
    if mission["config"].get("native_tui") and backend.pane_runs_provider(handle):
        return "provider-alive"
    if backend.attached_clients(handle, exclude=exclude_clients):
        return "clients-attached"
    backend.stop(handle)
    store.record_runtime_event(mission_id, "station-stopped", {"mission_status": mission["status"], "source": "finished-mission"})
    return "closed"


def gc_finished_station(mission_id: str, exclude_clients: tuple[str, ...] = ()) -> str:
    """Close a finished mission's idle station from outside its controller."""
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return "controller-active"
    try:
        with BridgeStore(store_path()) as store:
            if store.get_mission(mission_id)["status"] not in STATION_CLOSING_MISSION_STATUSES:
                return "not-finished"
            socket = mission_socket_path(mission_id)
            if not socket.exists():
                return "no-station"
            backend = TmuxTerminalBackend()
            try:
                handle = backend.open_existing(socket, _session_name(mission_id))
            except DeadStationError:
                return "no-station"
            return close_finished_station(store, backend, handle, mission_id, exclude_clients=exclude_clients)
    finally:
        lock.release()


# Liveness probes in other processes briefly hold the lock; wait them out
# before concluding that another controller owns the mission.
_LOCK_STARTUP_WAIT_SECONDS = 0.5


def _process_controller_lock(mission_id: str) -> MissionLock:
    return MissionLock(default_state_dir() / "locks", mission_id)


def _open_existing(backend: TmuxTerminalBackend, runtime: Path, mission_id: str) -> TerminalHandle | None:
    socket = mission_socket_path(mission_id)
    if not socket.exists():
        return None
    try:
        return backend.open_existing(socket_path=socket, session_name=_session_name(mission_id), bridge_rows=6)
    except DeadStationError:
        # The previous station is gone and its dead socket has been removed.
        return None


def station_server_environment(config: dict[str, Any]) -> dict[str, str]:
    """Environment of the station's tmux server, and so of every pane it starts.

    Credentials of providers this mission cannot use are removed, and so is the
    Bridge package root from PYTHONPATH. Credentials of providers the mission may
    rotate to stay: the launch script of each run removes the ones that belong
    to the other providers.
    """
    from . import bridge_rotation

    enabled = bridge_rotation.frozen_enabled(bridge_rotation.parse_frozen(config.get("frozen_tool_config"))).names
    env = common.without_package_root(os.environ)
    doomed = set(common.credential_variables_unused_by(env, enabled))
    return {name: value for name, value in env.items() if name not in doomed}


def ralph_argv(mission: dict[str, Any], runtime: Path, *, providers: str, routes: str) -> list[str]:
    """The ralph-robin arguments for one mission controller.

    Provider workspace-trust dialogs are acknowledged only for a mission created
    with ``--trust-folder``; otherwise a run that stops at one is aborted.
    """
    config = mission.get("config") or {}
    argv = [
        "--prompt", mission["objective"],
        "--cwd", mission["cwd"],
        "--providers", providers,
        "--max-iterations", "0",
        "--max-duration", "0",
        "--min-iteration-seconds", "0",
        "--log-dir", str(runtime / "ralph-logs"),
        "--state-file", str(runtime / "ralph-state.json"),
        "--auto-confirm" if config.get("trust_folder") else "--no-auto-confirm",
    ]
    if routes:
        argv.extend(["--routes", routes])
    command_template = str(config.get("command_template", ""))
    if command_template:
        argv.extend(["--command-template", command_template])
    return argv


def run_controller(mission_id: str, *, headless: bool = False, native_tui: bool = False, providers: str | None = None, routes: str | None = None, startup_token: str = "") -> int:
    """Own one mission until it is completed, stopped, or requires recovery."""
    lock = _process_controller_lock(mission_id)
    try:
        lock.acquire(timeout=_LOCK_STARTUP_WAIT_SECONDS)
    except BlockingIOError:
        return 2
    runtime = _runtime_directory(mission_id)
    ready = _ready_file(runtime)
    try:
        ready.unlink()
    except FileNotFoundError:
        pass

    with lock, BridgeStore(store_path()) as store:
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
        routes = str(mission["config"].get("routes", "")) if routes is None else str(routes)
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
        store.release_declined_tasks(mission_id)
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
        elif mission["status"] in FINISHED_MISSION_STATUSES:
            _write_controller_ready(ready, startup_token, mission["status"])
            return 0

        # A start that names a different rotation changes the mission's saved
        # rotation; record it so the Activity view shows the change.
        store.set_rotation(mission_id, providers=providers, routes=routes, source="start")
        mission = store.set_mission_config_values(
            mission_id,
            {"headless": headless, "native_tui": native_tui},
            event_kind="controller-config",
        )
        prior_headless = bool(mission["config"].get("headless", False))

        _install_frozen_tool_config(mission, runtime)
        problem = rotation_problem(mission, providers, routes)
        if problem:
            record_controller_failure(store, mission_id, problem)
            store.set_mission_status(mission_id, "FAILED")
            print(f"controller failure: {problem}", file=sys.stderr, flush=True)
            return 2
        backend: TmuxTerminalBackend | None = None
        handle: TerminalHandle | None = None
        if not headless:
            if not shutil.which("tmux"):
                record_controller_failure(store, mission_id, "tmux is required for native mode; retry with --headless")
                store.set_mission_status(mission_id, "FAILED")
                raise RuntimeError("tmux is required for native mode; retry with --headless")
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is None:
                controller_log = runtime / "controller.log"
                controller_log.touch(mode=0o600, exist_ok=True)
                os.chmod(controller_log, 0o600)
                bridge_argv = rooted_module_argv("llm_tools.bridge", "console", "--mission", mission_id)
                handle = backend.start(
                    provider_argv=_provider_station_argv(native_tui, controller_log),
                    bridge_argv=bridge_argv,
                    picker_argv=rooted_module_argv("llm_tools.bridge", "missions", "--pick", "--from", mission_id),
                    cwd=mission["cwd"],
                    socket_path=mission_socket_path(mission_id),
                    session_name=_session_name(mission_id),
                    size=(120, 40),
                    server_env=station_server_environment(mission["config"]),
                )

        if backend is not None and handle is not None:
            try:
                backend.set_detach_command(handle, station_detach_argv(mission_id))
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass  # without the hook a finished station is closed by `llm-bridge stop`
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
            hooks.applied_rotation = (providers, routes)
            refresh_worker = CapacityRefreshWorker(
                mission_id,
                store_path(),
                interval_seconds=int(mission["config"].get("capacity_refresh_seconds", 60)),
            )
            refresh_worker.start()
            argv = ralph_argv(mission, runtime, providers=providers, routes=routes)
            from . import ralph_robin

            return ralph_robin.main(argv, bridge_hooks=hooks)
        except BaseException as exc:
            store.reconcile_interrupted_operations(mission_id)
            current = store.get_mission(mission_id)
            if current["status"] in CONTROLLED_MISSION_STATUSES:
                record_controller_failure(store, mission_id, controller_failure_reason(exc, runtime / "controller.log"))
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
                record_controller_failure(store, mission_id, "the controller stopped while the mission was still running")
                store.set_mission_status(mission_id, "FAILED")
                current = store.get_mission(mission_id)
            if backend is not None and handle is not None and current["status"] in {"STOPPED", "CANCELLED"}:
                backend.stop(handle)
            elif backend is not None and handle is not None and current["status"] in {"COMPLETED", "FAILED"}:
                # Close the finished mission's station unless someone is still
                # attached; the client-detached hook closes it later.
                try:
                    close_finished_station(store, backend, handle, mission_id)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass


def _recorded_failure(mission_id: str) -> str:
    """`: <reason>` from the latest controller-failed event, or an empty string."""
    try:
        with BridgeStore(store_path()) as store:
            row = store.db.execute(
                "SELECT data_json FROM events WHERE mission_id=? AND kind='controller-failed' ORDER BY id DESC LIMIT 1", (mission_id,)
            ).fetchone()
        reason = json.loads(row[0]).get("reason") if row else None
    except (OSError, ValueError, TypeError):
        return ""
    return f": {reason}" if reason else ""


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
    with BridgeStore(store_path()) as store:
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
        lock.acquire(timeout=_LOCK_STARTUP_WAIT_SECONDS)
    except BlockingIOError:
        # A controller that is already running is holding a task after the operator
        # declined the provider's trust prompt: starting again releases it.
        with BridgeStore(store_path()) as store:
            released = store.release_declined_tasks(mission_id)
        if released:
            label = ", ".join(str(ordinal) for ordinal in released)
            print(f"mission {mission_id[:8]} · released task {label} · the provider starts again", file=sys.stderr)
            return 0
        if not headless and sys.stdin.isatty() and sys.stdout.isatty():
            runtime = _runtime_directory(mission_id)
            backend = TmuxTerminalBackend()
            handle = _open_existing(backend, runtime, mission_id)
            if handle is not None:
                return backend.attach(handle)
        print(f"mission {mission_id[:8]} already has a controller; use: llm-bridge attach --mission {mission_id[:8]} or llm-bridge status --mission {mission_id[:8]}", file=sys.stderr)
        return 2
    lock.release()

    with BridgeStore(store_path()) as store:
        mission = store.get_mission(mission_id)
        if providers:
            providers_value = providers
        else:
            providers_value = str(mission["config"].get("providers", "claude,codex,opencode"))
        if routes is not None:
            routes_value = routes  # an empty string is an explicit "no routes" for this start
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
    env.pop("LLM_BRIDGE_MANAGED", None)  # a controller is the operator, never a managed provider
    env["LLM_BRIDGE_STATE_DIR"] = str(default_state_dir())
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH", ""))))
    command = python_module_argv("llm_tools.bridge", "_controller", "--mission", mission_id, "--providers", providers_value, "--startup-token", startup_token)
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
            raise RuntimeError(f"mission controller exited with status {proc.returncode}{_recorded_failure(mission_id)}; inspect {log_path}")
        time.sleep(0.05)
    else:
        raise RuntimeError(f"mission controller did not become ready; inspect {log_path}")

    if headless or not (sys.stdin.isatty() and sys.stdout.isatty()):
        if headless:
            print(f"mission {mission_id[:8]} running without a terminal station · inspect with: llm-bridge status --mission {mission_id[:8]} · controller log: {log_path}")
        else:
            print(f"mission {mission_id[:8]} running · attach with: llm-bridge attach --mission {mission_id[:8]} · when the provider finishes, llm-bridge status --mission {mission_id[:8]} shows how to approve · controller log: {log_path}")
        return 0
    backend = TmuxTerminalBackend()
    handle = _open_existing(backend, runtime, mission_id)
    if handle is None:
        with BridgeStore(store_path()) as store:
            final = store.get_mission(mission_id)["status"]
        if final in STATION_CLOSING_MISSION_STATUSES:
            print(f"mission {mission_id[:8]} {final.lower()}; its station closed on its own · details: llm-bridge status --mission {mission_id[:8]}")
            return 0
        raise RuntimeError(f"mission is running but its terminal station is unavailable; inspect {log_path}")
    return backend.attach(handle)
