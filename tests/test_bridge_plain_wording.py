"""Plain wording in the console and CLI: no internals, enum names, JSON or empty placeholders (CLIUX-020)."""

from __future__ import annotations

import pytest

from llm_tools import bridge_event_text, bridge_format, bridge_tui
from tests.test_bridge_tui_experience import event, make_state


def _health_snapshot() -> dict[str, object]:
    return {
        "mission": {"id": "M1", "name": "Fix", "status": "RUNNING"},
        "lifecycle": {"status": "RUNNING", "controller": "healthy",
                      "heartbeat": {"reported_state": "ACTIVE", "age_seconds": 3, "owner_lock_held": True, "desired_mission_status": "RUNNING"}},
        "persistence": {"schema_version": 10, "journal_mode": "wal", "database": "/state/bridge.sqlite3"},
        "health": {"status": "ok"},
    }


def test_health_keeps_schema_journal_and_owner_lock_under_one_internals_line() -> None:
    rows = make_state(_health_snapshot(), view="Health").rows()
    text = "\n".join(rows)
    assert not any(row.startswith("Persistence") for row in rows)
    heartbeat = next(row for row in rows if row.startswith("Controller heartbeat"))
    assert "owner lock" not in heartbeat and "active" in heartbeat and "mission wants RUNNING" in heartbeat
    internals = [row for row in rows if row.startswith("Internals")]
    assert len(internals) == 1 and rows.index(internals[0]) > rows.index(heartbeat)
    assert "owner lock held" in internals[0] and "sqlite schema 10" in internals[0] and "wal journal" in internals[0]
    assert text.count("owner lock") == 1


@pytest.mark.parametrize(("stored", "words"), [
    ("NATIVE_CREATED", "resumable"), ("RESTORED_NATIVE", "resumed"), ("UNAVAILABLE", "cannot resume"),
    ("RECONSTRUCTED", "rebuilt"), ("RESTORING", "restoring"),
])
def test_sessions_view_and_activity_use_plain_words_for_the_restoration_state(stored: str, words: str) -> None:
    operations = [{"id": "0123456789abcdef", "task_id": 1, "station": "claude", "model": "m", "status": "EXITED", "native_session_id": "abc", "restoration_state": stored}]
    rows = make_state({"operations": operations}, view="Sessions").rows()
    assert rows[0].endswith(words) and stored not in rows[0]
    phrase = bridge_event_text._humanize_event(event("native-session-restoration", {"provider": "claude", "restoration_state": stored}))[0]
    assert phrase == f"Session {words} · claude"


def test_an_unknown_restoration_state_is_shown_as_stored() -> None:
    assert bridge_event_text.restoration_word("SOMETHING_NEW") == "SOMETHING_NEW"
    assert bridge_event_text.restoration_word(None) == "-"


def _inspector_text(item: dict[str, object]) -> str:
    snapshot = bridge_tui._bounded_snapshot({"agent_map": {"observed_at": 200, "agents": [item]}})
    state = bridge_tui._ConsoleState("M", lambda *_args: "", lambda _id: {}, full=True, snapshot=snapshot)
    state.view_index = bridge_tui.VIEWS.index("Agents")
    state.agent_inspector = True
    return "\n".join(state.rows())


def test_agent_inspector_omits_fields_the_provider_did_not_report() -> None:
    text = _inspector_text({"id": "a", "kind": "native-child", "name": "Halley", "provider": "codex", "status": "completed", "model": "gpt-6", "model_source": "observed", "age_seconds": 60})
    assert "unknown" not in text.lower().replace("not reported by the provider", "") and "None" not in text
    lines = [line for line in text.splitlines() if line.startswith("Not reported by the provider")]
    assert len(lines) == 1
    block = text[text.index("Not reported by the provider"):text.index("Esc returns")]  # one statement, wrapped over rows
    for field in ("role", "mission", "last observed", "native status", "freshness"):
        assert field in block
        assert not any(line.startswith(f"{field}:") for line in text.splitlines())
    assert "model: gpt-6 (observed)" in text


def test_agent_inspector_without_gaps_has_no_not_reported_line() -> None:
    text = _inspector_text({
        "id": "a", "kind": "native-child", "name": "Halley", "provider": "codex", "status": "completed", "role": "reviewer", "role_source": "observed",
        "mission_name": "Fix", "model": "m", "model_source": "observed", "reasoning_effort": "high", "effort_source": "observed", "age_seconds": 5,
        "last_activity_at": "2033-05-18T03:00:00+00:00", "native_status": "idle", "activity": "x", "freshness": "fresh",
        "activity_freshness": "fresh", "activity_age_seconds": 1, "lifecycle_freshness": "fresh", "lifecycle_age_seconds": 2,
    })
    assert "Not reported" not in text


def _task_detail(task: dict[str, object]) -> str:
    state = make_state({"tasks": [{"id": 1, "ordinal": 1, "title": "t", "prompt": "p", "status": "READY", "priority": 0, "revision": 2, **task}]}, view="Tasks")
    state.task_detail = True
    return "\n".join(state.rows())


def test_task_inspector_shows_amendments_as_text_and_omits_unknown_origin_fields() -> None:
    text = _task_detail({
        "pending": {"title": "New title", "priority": 3, "prompt": "Write parser tests incl. edge cases"},
        "amendments": [{"revision": 2, "status": "APPLIED", "changes": {"prompt": "Write parser tests incl. edge cases"}}],
        "proposal_status": "NONE", "proposed_by_operation": None,
    })
    assert "{" not in text and '"' not in text
    assert "Pending amendment: " in text and "prompt: Write parser tests incl. edge cases" in text
    assert "revision 2 applied" in text.lower()
    assert "proposed by" not in text and "proposal NONE" not in text and "None" not in text
    assert "Origin operator" in text


def test_task_inspector_names_who_proposed_a_proposal() -> None:
    text = _task_detail({"origin": "agent-proposal", "proposal_status": "PENDING", "proposed_by_operation": "op-9"})
    assert "proposal PENDING" in text and "proposed by op-9" in text


def test_amendment_text_names_each_changed_field() -> None:
    assert bridge_format.amendment_text({"priority": 3, "acceptance": ["a", "b"], "title": "T"}) == "title: T; priority: 3; acceptance: 2 criteria"
    assert bridge_format.amendment_text({"prompt": "x" * 200}).endswith("…")
    assert bridge_format.amendment_text({}) == "no changes"


def test_legacy_policy_is_described_in_plain_language_on_both_surfaces() -> None:
    rows = make_state({"mission": {"status": "CREATED", "execution_policy": {"legacy": True}}}).rows()
    policy = next(row for row in rows if row.startswith("Policy"))
    assert "legacy" not in policy and "unchanged" not in policy
    assert bridge_format.LEGACY_POLICY_TEXT in policy
    assert "teams" in bridge_format.LEGACY_POLICY_TEXT and "token spend" in bridge_format.LEGACY_POLICY_TEXT
