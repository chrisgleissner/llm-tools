"""Row, table and detail builders of the console views, mixed into the console state."""

from __future__ import annotations

import textwrap
from . import bridge_format
from .bridge_format import plain_reason
from .bridge_glyphs import localize_cells
from .bridge_wording import NEXT_NOTHING, finished_next
from .bridge_event_text import _SEP, _age, _decision_owner, _dict, _int_or_none, _known, _relative_epoch, _short_id, _span, _text, restoration_word
from .bridge_draw import Segment, _segments_text
from .bridge_console_commands import _BRIDGE_LABEL, _MAX_DETAIL_ITEMS, _SESSION_COLUMNS, _SYSTEM_LABEL, _cut_text, _evidence_text, _items, _last_run_line, task_number
from .bridge_console_style import _inline_segments, _state_segment, _state_text, _status_role
from .bridge_console_text import _PALETTE_NOTE, _USAGE_HEADER, _active_owner, _activity_entries, _activity_role, _activity_row_segments, _capacity_summaries, _clock_epoch, _controller_line, _count_phrase, _finished_operation_text, _fit_usage_segments, _idle_phrase, _local_clock, _mission, _needs_you_rows, _order_list_segments, _provider_header_segments, _provider_state, _recent_operation_segments, _station_label, _task_label, _task_numbers, _usage_capacity_lines, _usage_disagreements, _usage_rows_for, _usage_window_lines, _window_summary, _with_square
from typing import TYPE_CHECKING, Any


class _ViewRows:
    """The row text and segments of each control view.

    Mixed into ``bridge_tui._ConsoleState``: the methods read the state's fields through ``self``.
    """

    if TYPE_CHECKING:
        selected: int

        def __getattr__(self, name: str) -> Any:
            """Tell the type checker that the mixed-in state supplies the fields these methods read."""
            ...

    def rows(self) -> list[str]:
        view = self.view
        if view == "Usage":
            return self._usage_rows()
        if view == "Overview":
            return self._bridge_rows()
        if view == "Tasks":
            return self._order_rows()
        if view == "Providers":
            return self._station_rows()
        if view == "Health":
            return self._system_rows()
        if view == "Activity":
            return self._log_rows()
        if view == "Sessions":
            return self._session_rows() or [_station_label(station) for station in _items(self.snapshot.get("stations"))] or ["No session activity recorded."]
        if view == "Agents":
            return self._agent_rows()
        if view == "Actions":
            return self._palette_rows()
        return []

    def row_segments(self) -> list[list[Segment]] | None:
        """Per-row coloured segments for the views that style elements, aligned with `rows()`."""
        view = self.view
        if view == "Usage":
            return self._usage_segments()
        if view == "Tasks" and not self.task_detail:
            return self._order_list()[0] or None
        if view == "Providers":
            return self._provider_segments()[0]
        if view == "Sessions":
            return self._session_cells()[1] or None
        if view == "Actions":
            return self._palette_segments()
        if view == "Activity":
            rows, roles = self._activity_layout()
            return [_activity_row_segments(row, role) for row, role in zip(rows, roles)]
        return None

    def row_roles(self) -> list[str] | None:
        """Semantic role per row for views that style rows individually."""
        if self.view == "Providers":
            return self._provider_layout()[2]
        if self.view == "Activity":
            return self._activity_layout()[1]
        return None

    def _usage_segments(self) -> list[list[Segment]]:
        """Render llm-usage's ANSI cell colors through Rich and Bridge's palette."""
        from .bridge_documents import render_ansi_segments

        usage = _dict(self.snapshot.get("usage"))
        limited = "all_text" in usage  # the default text is the mission's rotation; `a` shows every provider
        everything = limited and self.usage_all
        text = str(usage.get("all_text" if everything else "text") or "Loading full llm-usage dashboard...")
        styled = [[(localize_cells(part), role) for part, role in line] for line in render_ansi_segments(text)]
        lines = [_segments_text(line) for line in styled]
        parts = ["Usage", str(usage.get("status") or "refreshing")]
        age = _int_or_none(usage.get("age_seconds"))
        parts.append(f"updated {_age(age)} ago" if age is not None else "first sample pending")
        if usage.get("error"):
            parts.append(f"last refresh failed: {_text(usage.get('error'), 60)}")
        if limited:
            parts.append("all providers (a: rotation)" if everything else "rotation (a: all)")
        parts.append("live llm-usage read")  # the source, so a difference from the controller's observation is explicable
        status = [(_SEP.join(parts), _status_role(_SEP.join(parts)))]
        differences = [[(localize_cells(line), "warn")] for line in _usage_disagreements(self.snapshot)]
        table_start = next((index for index, line in enumerate(lines) if _USAGE_HEADER.match(line)), None)
        if table_start is None:
            return [status, *differences, *styled]
        table = _fit_usage_segments(styled[table_start:], self.content_width)
        while table and not _segments_text(table[-1]).strip():
            table.pop()
        if table:
            table[0] = [(_segments_text(table[0]), "heading")]
        if len(table) > 1 and _segments_text(table[1]).strip() and set(_segments_text(table[1]).strip()) <= {"─", "-", " "}:
            table[1] = [(_segments_text(table[1]), "dim")]
        result = [status, *differences, *table]
        legend = [line for line in styled[1:table_start] if _segments_text(line).strip()]
        if legend:
            result.extend([[], *(legend if self.usage_legend else [[("l shows the legend", "dim")]])])
        return result

    def _usage_rows(self) -> list[str]:
        return [_segments_text(line) for line in self._usage_segments()]

    def _bridge_rows(self) -> list[str]:
        data = self.snapshot
        tasks = _items(data.get("tasks"))
        current_task = _dict(data.get("current_task"))

        def row(label: str, value: str) -> str:
            return f"{label:<{_BRIDGE_LABEL}}{value}"

        if str(current_task.get("status") or "").upper() in {"READY", "QUEUED"}:
            current_task = {}  # waiting to start: it is the Next task below, and nothing runs now
        next_task = next((task for task in tasks if task.get("status") in {"READY", "QUEUED"}), None)
        order_counts = _count_phrase(_dict(data.get("task_counts")))
        proposals = _dict(data.get("proposal_counts"))
        proposal_total = sum(count for count in proposals.values() if isinstance(count, (int, float)))
        orders = order_counts or (f"{len(tasks)} visible" if tasks else "none yet — Ctrl-E adds a task")
        if proposal_total:
            orders += f"{_SEP}{int(proposal_total)} proposal{'s' if proposal_total != 1 else ''}"
        rows = [
            row("Controller", _controller_line(data)),
            row("Now", ("task " + _task_label(current_task) + (_SEP + _idle_phrase(data) if _idle_phrase(data) else "")) if current_task else "idle"),
            row("Next", "task " + _task_label(next_task) if next_task else finished_next(str(_mission(data).get("status") or "").upper(), "console") or NEXT_NOTHING),
            row("Tasks", orders),
        ]
        policy = _dict(_dict(data.get("mission")).get("execution_policy"))
        policy_text = bridge_format.LEGACY_POLICY_TEXT if policy.get("legacy") else f"teams {'on' if policy.get('teams_enabled') else 'off'} · token spend {policy.get('token_spend', 'medium')}"
        rows.append(row("Policy", policy_text))
        budget_data = _dict(data.get("budget"))
        if budget_data.get("limits"):
            from .bridge_budget import summary

            reached = " · limit reached" if budget_data.get("reached") else ""
            rows.append(row("Budget", _text(summary(_dict(budget_data["limits"]), _dict(budget_data.get("used"))), 200) + reached))
        needs = _needs_you_rows(data)
        for index, text in enumerate(needs):
            rows.append(row("Needs you" if index == 0 else "", text))
        queued = data.get("pending_command_count") or 0
        if isinstance(queued, int) and queued:
            rows.append(row("Queue", f"{queued} command{'s' if queued != 1 else ''} waiting for the controller"))
        criteria = _dict(data.get("criteria"))
        total = _int_or_none(criteria.get("total"))
        if total:
            rows.append(row("Criteria", f"{_int_or_none(criteria.get('verified')) or 0}/{total} verified"))
        for index, text in enumerate(_capacity_summaries(data)):
            rows.append(row("Capacity" if index == 0 else "", _with_square(text)))
        return rows

    def _order_list(self) -> tuple[list[list[Segment]], list[str]]:
        """Segments and text of the Tasks list; computed once per drawn frame."""
        cache = self._draw_cache
        if cache is not None and "orders" in cache:
            return cache["orders"]  # type: ignore[return-value]
        segments = _order_list_segments(_items(self.snapshot.get("tasks")))
        result = (segments, [_segments_text(row) for row in segments])
        if cache is not None:
            cache["orders"] = result
        return result

    def _order_rows(self) -> list[str]:
        if not self.task_detail:
            return self._order_list()[1] or ["No tasks recorded."]
        task = self.selected_task()
        if task is None:
            return ["No task selected."]
        dependencies = task.get("depends_on", [])
        acceptance = task.get("acceptance", [])
        evidence = task.get("evidence", [])
        pending = _dict(task.get("pending"))
        amendments = task.get("amendments")
        return [
            f"Task {_text(task_number(task) if task_number(task) is not None else '?', 32)}: {_cut_text(task.get('title') or 'Untitled', 100)}",
            f"Status {_text(task.get('status', 'unknown'), 24)} | priority {_text(task.get('priority', '?'), 16)} | revision {_text(task.get('revision', '?'), 16)}",
            " | ".join(part for part in (
                f"Origin {_text(task.get('origin') or 'operator', 24)}",
                f"proposal {_text(task['proposal_status'], 32)}" if _known(task.get("proposal_status")) else "",
                f"proposed by {_text(task['proposed_by_operation'], 32)}" if _known(task.get("proposed_by_operation")) else "",
            ) if part),
            *([line] if (line := _last_run_line(self.snapshot, task)) else []),
            "Dependencies: " + (", ".join(_text(item, 32) for item in dependencies) if isinstance(dependencies, list) and dependencies else "none"),
            "Acceptance criteria:",
            *([f"  {index + 1}. {_cut_text(item, 240)}" for index, item in enumerate(acceptance[:_MAX_DETAIL_ITEMS])] if isinstance(acceptance, list) and acceptance else ["  none recorded"]),
            "Verification evidence:",
            *([f"  {index + 1}. {_evidence_text(item)}" for index, item in enumerate(evidence[:_MAX_DETAIL_ITEMS])] if isinstance(evidence, list) and evidence else ["  none recorded"]),
            "Pending amendment: " + (_cut_text(bridge_format.amendment_text(pending), 400) if pending else "none"),
            "Amendment history:",
            *([
                f"  revision {_text(item.get('revision', '?'), 16)} {_text(item.get('status', 'unknown'), 20).lower()}: {_cut_text(bridge_format.amendment_text(item.get('changes', {})), 240)}"
                for item in amendments[:5] if isinstance(item, dict)
            ] if isinstance(amendments, list) and amendments else ["  none recorded"]),
            "Prompt preview (v opens full prompt): " + _cut_text(task.get("prompt") or "", 800),
        ]

    def _station_rows(self) -> list[str]:
        return self._provider_layout()[0]

    def _provider_layout(self) -> tuple[list[str], list[int], list[str]]:
        """Rows of the Providers view, the first row of each selectable block, and a role per row."""
        segments, starts, roles = self._provider_segments()
        return [_segments_text(row) for row in segments], starts, roles

    def _provider_segments(self) -> tuple[list[list[Segment]], list[int], list[str]]:
        """Segments of each Providers row, the first row of each selectable block, and a role per row.

        Each configured provider or route is one block: a header (name, model,
        live state), one line per capacity window with remaining share, pace and
        reset, then its most recent operations.
        """

        data = self.snapshot
        stations = _items(data.get("stations"))
        operations = _items(data.get("operations"))
        if not stations:
            observed = {
                (_text(item.get("station") or item.get("provider") or "unknown", 60),
                 _text(item.get("model"), 60) if _known(item.get("model")) else "default model",
                 _text(item.get("status") or "unknown", 24))
                for item in operations
            }
            fallback = [f"{station}  {model}  {status}" for station, model, status in sorted(observed)] or ["No providers recorded."]
            return [[(row, "info")] for row in fallback], [], ["info"] * len(fallback)
        context = _dict(data.get("station_context"))
        viewed = _text(context.get("viewed_station") or "unknown", 80)
        last_launched = _text(context.get("last_launched_station") or "unknown", 80)
        executing = context.get("executing_operation")
        summary = [
            f"Viewing {viewed}" if _known(viewed) else "",
            f"last launched {last_launched}" if _known(last_launched) else "",
            f"operation {_short_id(executing)} {_text(context.get('operation_status') or '', 20)}".rstrip() if _known(executing) else "",
        ]
        header = _SEP.join(part for part in summary if part) or "No provider launched yet"
        details = [
            f"mode {_text(context.get('mode'), 20)}" if _known(context.get("mode")) else "",
            f"liveness {_text(context.get('provider_liveness'), 20)}" if _known(context.get("provider_liveness")) else "",
            f"effort {_text(context.get('observed_effort'), 20)}" if _known(context.get("observed_effort")) else "",
        ]
        detail_line = _SEP.join(part for part in details if part) or "Enter marks the highlighted provider as viewed"
        rows: list[list[Segment]] = [_inline_segments(header, "muted"), _inline_segments(detail_line, "muted")]
        roles: list[str] = ["dim", "dim"]
        starts: list[int] = []
        now = _clock_epoch()
        selection = _dict(_dict(data.get("capacity")).get("selection"))
        decisions = {_decision_owner(row): row for row in _items(selection.get("decisions"))}
        active = _active_owner(data)
        current = _dict(data.get("current_task"))
        elapsed = _dict(data.get("execution")).get("operation_elapsed_seconds")
        for item in stations:
            name = _text(item.get("name") or item.get("provider") or item.get("id") or "Provider", 80)
            decision = decisions.get(name) or decisions.get(_text(item.get("provider") or "", 80))
            usage_rows = _usage_rows_for(data, _text(item.get("provider") or name, 60), _text(item.get("model"), 60) if _known(item.get("model")) else "")
            state, role = _provider_state(name, active, current, elapsed, decision, now, usage_rows)
            model = f"model {_text(item.get('model'), 50)}" if _known(item.get("model")) else "default model"
            if _known(item.get("observed_model")) and item.get("observed_model") != item.get("model"):
                model += f" (live {_text(item['observed_model'], 50)})"
            flags = [flag for flag, shown in (("[viewed]", name == viewed), ("[historical]", item.get("status") == "historical")) if shown]
            starts.append(len(rows))
            rows.append(_provider_header_segments(name, model, state, role, flags))
            roles.append(role)
            windows = _items(decision.get("windows")) if decision else []
            lines = [_window_summary(window, now) for window in windows] or _usage_window_lines(usage_rows, now) or [
                "capacity: no observation yet" if decision is None else "capacity unavailable"]
            for line in lines:
                rows.append([("  ", "info"), *_inline_segments(line, "info")])
                roles.append("info")
            recent = [op for op in operations if name in {str(op.get("station") or ""), str(op.get("route_id") or "")}][-3:]
            for op in reversed(recent):
                age = _relative_epoch(op.get("created_at"), now)
                rows.append(_recent_operation_segments(op, age))
                roles.append("dim")
            if not recent:
                rows.append([("  recent: no operations yet", "dim")])
                roles.append("dim")
        return rows, starts, roles

    def _session_rows(self) -> list[str]:
        return self._session_table()[1]

    def _session_table(self) -> tuple[str | None, list[str]]:
        header, rows = self._session_cells()
        return header, [_segments_text(row) for row in rows]

    def _session_cells(self) -> tuple[str | None, list[list[Segment]]]:
        operations = _items(self.snapshot.get("operations"))
        if not operations:
            return None, []

        def cell(value: object, limit: int) -> str:
            return _text(value, limit) if _known(value) else "-"

        table = [
            (
                _short_id(item.get("id") or item.get("operation_id")),
                cell(item.get("task_ordinal") if _known(item.get("task_ordinal")) else item.get("task_id"), 12),
                cell(item.get("route_id") or item.get("route") or item.get("station") or item.get("provider"), 24),
                cell(item.get("model"), 28),
                _state_text(cell(item.get("status"), 20)),
                _short_id(item.get("native_session_id")),
                restoration_word(item.get("restoration_state")),
            )
            for item in operations
        ]
        widths = [
            max(len(title), *(len(line[index]) for line in table))
            for index, title in enumerate(_SESSION_COLUMNS)
        ]
        header = "  ".join(title.ljust(widths[index]) for index, title in enumerate(_SESSION_COLUMNS)).rstrip()
        roles = ["muted", "muted", "info", "muted", "", "muted", "muted"]
        rows: list[list[Segment]] = []
        for line in table:
            row: list[Segment] = []
            for index, value in enumerate(line):
                role = roles[index] or _state_segment(value.split()[-1])[1]
                row.append((value.ljust(widths[index]) if index < len(line) - 1 else value, role))
                if index < len(line) - 1:
                    row.append(("  ", "info"))
            rows.append(row)
        return header, rows

    def _agent_rows(self) -> list[str]:
        from .bridge_agents import format_agent_map, ordered_agents

        agent_map = _dict(self.snapshot.get("agent_map"))
        if self.agent_inspector:
            ordered = ordered_agents(agent_map)
            if not ordered:
                return ["No agent selected.", "Esc returns to the map."]
            item, _depth = ordered[min(max(0, self.selected), len(ordered) - 1)]
            model = item.get("model") if item.get("model_source") == "observed" else item.get("configured_model")
            effort = item.get("reasoning_effort") if item.get("effort_source") == "observed" else item.get("configured_effort")
            model_source = item.get("model_source") if item.get("model_source") == "observed" else ("configured" if _known(item.get("configured_model")) else "unknown")
            effort_source = item.get("effort_source") if item.get("effort_source") == "observed" else ("configured" if _known(item.get("configured_effort")) else "unknown")
            capability = _dict(item.get("capabilities"))
            role = item.get("role") if item.get("role_source") == "observed" else None
            activity_age = item.get("activity_age_seconds")
            activity_age_text = f"age {int(activity_age)}s" if isinstance(activity_age, (int, float)) and not isinstance(activity_age, bool) else ""
            lifecycle_age = item.get("lifecycle_age_seconds")
            lifecycle_age_text = f"age {int(lifecycle_age)}s" if isinstance(lifecycle_age, (int, float)) and not isinstance(lifecycle_age, bool) else ""
            mission = item.get("mission_name") if _known(item.get("mission_name")) else item.get("mission_id")
            # A field the provider did not report is left out of the list and named once on the last line.
            fields: list[tuple[str, str]] = [
                ("role", f"{_text(role, 80)} ({_text(item.get('role_source') or 'observed', 20)})" if _known(role) else ""),
                ("mission", _text(mission, 100) if _known(mission) else ""),
                ("age", _span(item.get("age_seconds"), 2) if _known(item.get("age_seconds")) else ""),
                ("model", f"{_text(model, 100)} ({_text(model_source, 20)})" if _known(model) else ""),
                ("reasoning effort", f"{_text(effort, 40)} ({_text(effort_source, 20)})" if _known(effort) else ""),
                ("activity", _text(item.get("activity"), 180) if _known(item.get("activity")) else ""),
                ("last observed", _text(item.get("last_activity_at"), 80) if _known(item.get("last_activity_at")) else ""),
                ("native status", _text(item.get("native_status"), 40) if _known(item.get("native_status")) else ""),
                ("activity freshness", _SEP.join(part for part in (_text(item["activity_freshness"], 24) if _known(item.get("activity_freshness")) else "", activity_age_text) if part)),
                ("lifecycle freshness", _SEP.join(part for part in (_text(item["lifecycle_freshness"], 24) if _known(item.get("lifecycle_freshness")) else "", lifecycle_age_text) if part)),
            ]
            shown = {label: value for label, value in fields if value}
            missing = [label for label, value in fields if not value]
            identity = f"identity: {_text(item.get('identity_source') or 'provider metadata', 60)}"
            rows = [f"{_text(item.get('name'), 100)} · {_text(item.get('provider'), 40)} · {_text(item.get('status_label') or item.get('status'), 30)}"]
            for label in ("role", "mission", "age", "model", "reasoning effort", "activity", "last observed", "native status"):
                if label in shown:
                    rows.append(f"{label}: {shown[label]}")
            rows.append(f"parent: {_text(item.get('parent_id') or 'none', 100)}")
            rows.append(f"freshness: {_text(item['freshness'], 40)} · {identity}" if _known(item.get("freshness")) else identity)
            for label in ("activity freshness", "lifecycle freshness"):
                if label in shown:
                    rows.append(f"{label}: {shown[label]}")
            rows.append(f"navigation: {_text(capability.get('reason') or 'inspect only', 160)}")
            if {"activity freshness", "lifecycle freshness"} <= set(missing):
                missing = [label for label in missing if "freshness" not in label] + ["freshness"]
            if missing:  # one statement, wrapped so a narrow terminal does not cut it
                rows.extend(textwrap.wrap("Not reported by the provider: " + ", ".join(missing), 70, subsequent_indent="  "))
            rows.append("Esc returns to the map.")
            return rows
        rows = format_agent_map(agent_map)
        rows.append("↑↓ select · Enter inspect/switch · i inspect · /agents refresh")
        return rows

    def _system_rows(self) -> list[str]:
        data = self.snapshot
        out: list[str] = []
        internals: list[str] = []  # implementation facts for diagnosing Bridge itself; shown on the last row

        def add(label: str, value: str) -> None:
            if value:
                out.append(f"{label:<{_SYSTEM_LABEL}}{value}")

        mode = _dict(data.get("station_mode"))
        if mode:
            actual = mode.get("actual_mode")
            add("Mode", _SEP.join(part for part in (
                f"configured {_text(mode['configured_mode'], 24)}" if _known(mode.get("configured_mode")) else "",
                f"actual {_text(actual, 24)}" if _known(actual) else "actual unverified",
            ) if part))
        lifecycle = _dict(data.get("lifecycle"))
        if lifecycle:
            add("Lifecycle", _SEP.join(part for part in (
                _text(lifecycle.get("status"), 20) if _known(lifecycle.get("status")) else "",
                f"controller {_text(lifecycle['controller'], 24)}"
                + (f" (verified {_relative_epoch(lifecycle.get('controller_event_at'))})" if _known(lifecycle.get("controller_event_at")) and _relative_epoch(lifecycle.get("controller_event_at")) else "")
                if _known(lifecycle.get("controller")) else "",
            ) if part))
            heartbeat = _dict(lifecycle.get("heartbeat"))
            if heartbeat:
                age = _int_or_none(heartbeat.get("age_seconds"))
                lock = heartbeat.get("owner_lock_held")
                if str(lock).lower() in {"true", "false"}:
                    internals.append("owner lock held" if str(lock).lower() == "true" else "owner lock not held")
                add("Controller heartbeat", _SEP.join(part for part in (
                    _text(heartbeat["reported_state"], 20).lower() if _known(heartbeat.get("reported_state")) else "",
                    f"{_age(age)} ago" if age is not None else "",
                    f"mission wants {_text(heartbeat['desired_mission_status'], 20)}" if _known(heartbeat.get("desired_mission_status")) else "",
                    f"liveness {_text(heartbeat['current_liveness'], 20)}" if _known(heartbeat.get("current_liveness")) else "",
                ) if part) or "none recorded")
        add("Routes", _text(data["routes"], 220) if _known(data.get("routes")) else "")
        controller = data.get("controller")
        add("Controller", _text(controller, 220) if _known(controller) else "")
        execution = data.get("execution")
        if isinstance(execution, dict):
            elapsed = _int_or_none(execution.get("operation_elapsed_seconds"))
            verified = _int_or_none(execution.get("seconds_since_verified_progress"))
            finished = _finished_operation_text(data)
            add("Execution", _SEP.join(part for part in (
                f"iteration {_text(execution['iteration'], 8)}" if _known(execution.get("iteration")) else "",
                finished or (f"operation running {_span(elapsed)}" if elapsed is not None else ""),
                f"last verified progress {_age(verified)} ago" if verified is not None else "no verified progress yet",
            ) if part))
        elif _known(execution):
            add("Execution", _text(execution, 220))
        health = data.get("health")
        if isinstance(health, dict):
            failed = _int_or_none(health.get("consecutive_failed_operations")) or 0
            limit = _int_or_none(health.get("max_provider_failures"))
            warning = health.get("stall_warning")
            add("Health", _SEP.join(part for part in (
                _text(health["status"], 24) if _known(health.get("status")) else "",
                f"{failed} consecutive failed operation{'s' if failed != 1 else ''}" + (f" (limit {limit})" if limit is not None else "") if failed else "",
                _text(warning, 200) if _known(warning) else "",
            ) if part) or "no problems reported")
        elif _known(health):
            add("Health", _text(health, 220))
        persistence = _dict(data.get("persistence"))
        internals.extend(part for part in (
            f"sqlite schema {_text(persistence['schema_version'], 8)}" if _known(persistence.get("schema_version")) else "",
            f"{_text(persistence['journal_mode'], 12)} journal" if _known(persistence.get("journal_mode")) else "",
            _text(persistence["database"], 300) if _known(persistence.get("database")) else "",
        ) if part)
        criteria = _dict(data.get("criteria"))
        if criteria and _int_or_none(criteria.get("total")):
            add("Criteria", f"{_int_or_none(criteria.get('verified')) or 0}/{_int_or_none(criteria.get('total'))} verified")
        progress = _dict(data.get("last_meaningful_progress"))
        if progress:
            when = _relative_epoch(progress.get("at"))
            add("Last progress", _SEP.join(part for part in (
                _text(progress["kind"], 40) if _known(progress.get("kind")) else "",
                f"{_local_clock(progress.get('at'))} ({when})" if when else "",
            ) if part))
        suspend = _dict(data.get("suspend"))
        if suspend and (_known(suspend.get("status")) or _known(suspend.get("target_epoch"))):
            until = _relative_epoch(suspend.get("target_epoch"))
            add("Suspend", _SEP.join(part for part in (
                _text(suspend["status"], 40) if _known(suspend.get("status")) else "",
                f"until {until[3:] if until and until.startswith('in ') else until}" if until else "",
                _text(suspend["reason"], 80) if _known(suspend.get("reason")) else "",
            ) if part))
        capacity = _dict(data.get("capacity"))
        summaries = _capacity_summaries(data)
        fallback = [] if summaries else _usage_capacity_lines(data)
        if fallback and not any(_dict(capacity.get("selection")).get(key) for key in ("route", "provider")):
            # Nothing has run yet: show what llm-usage reports instead of "no capacity observation".
            age = _int_or_none(_dict(data.get("usage")).get("age_seconds"))
            add("Capacity", "from llm-usage" + (f", updated {_age(age)} ago" if age is not None else ""))
            for line in fallback:
                out.append(f"{'':<{_SYSTEM_LABEL}}{line}")
        elif capacity:
            selection = _dict(capacity.get("selection"))
            owner = selection.get("route") or selection.get("provider")
            age = _int_or_none(capacity.get("source_age_seconds"))
            header = _SEP.join(part for part in (
                f"{_text(capacity['source'], 40)}" + (f" ({_age(age)} old)" if age is not None else "") if _known(capacity.get("source")) else "",
                f"selected {_text(owner, 40)}" + (f" / {_text(selection['selected_model'], 40)}" if _known(selection.get("selected_model")) else "") if _known(owner) else "",
                _text(plain_reason(selection["rotation_reason"]), 60) if _known(selection.get("rotation_reason")) else "",
                "all routes rate-limited" if str(selection.get("all_rate_limited")).lower() == "true" else "",
            ) if part)
            wait = _dict(capacity.get("wait"))
            if wait:
                until = _relative_epoch(wait.get("target_epoch"))
                header = _SEP.join(part for part in (header, f"waiting {until}" if until else "waiting", _text(wait["reason"], 60) if _known(wait.get("reason")) else "") if part)
            add("Capacity", header or "no capacity observation")
            for line in summaries:
                out.append(f"{'':<{_SYSTEM_LABEL}}{_with_square(line)}")
        add("Internals", _SEP.join(internals))
        return out or ["System and capacity status is not available in this snapshot."]

    def _log_rows(self) -> list[str]:
        return self._activity_layout()[0]

    def _activity_layout(self) -> tuple[list[str], list[str]]:
        """Rows of the Activity view and a role per row (plumbing is dim, problems are errors)."""
        events = _items(self.snapshot.get("events"))
        if not events:
            return ["No recent mission events."], ["dim"]
        entries = _activity_entries(events, _task_numbers(self.snapshot))
        shown = [entry for entry in entries if not (self.hide_plumbing and entry.plumbing)]
        if not shown:
            return [f"Only controller bookkeeping on this page ({len(entries)} rows); Ctrl-T shows it."], ["dim"]
        rows = [f"{entry.clock}  {entry.text}".rstrip() for entry in shown]
        roles = ["dim" if entry.plumbing else _activity_role(entry.text) for entry in shown]
        return rows, roles

    def _palette_rows(self) -> list[str]:
        return [_segments_text(row) for row in self._palette_segments()]

    def _palette_segments(self) -> list[list[Segment]]:
        """Action rows: the name in default text, its typed command in `key`, the effect `muted`."""
        actions = self.palette_actions()
        self.selected = min(max(0, self.selected), max(0, len(actions) - 1))
        if not actions:
            return [[(_PALETTE_NOTE, "info")]]
        label_width = max(len(action.label) for action in actions)
        typed_width = max(len(action.typed) for action in actions)
        rows: list[list[Segment]] = []
        for action in actions:
            unavailable = self.action_unavailable(action)
            if unavailable:  # disabled items recede
                text = f"{action.label:<{label_width}}  {action.typed:<{typed_width}}  {action.effect}"
                rows.append([(text + f"{_SEP}unavailable: {unavailable}", "dim")])
                continue
            rows.append([
                (f"{action.label:<{label_width}}", "info"), ("  ", "info"),
                (f"{action.typed:<{typed_width}}", "key"), ("  ", "info"), (action.effect, "muted"),
            ])
        return rows
