"""The wait loop for a Bridge-owned pane checks the station with a socket connect, not a tmux process per second."""

from __future__ import annotations

from pathlib import Path

from llm_tools import common, scheduler


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _run(tmp_path: Path, monkeypatch, *, ticks_until_status: int, accepts: bool = True, session_alive: bool = True,
         pane_dead_after: int | None = None):
    monkeypatch.setattr(common, "have_cmd", lambda _name: True)
    monkeypatch.setattr(common, "unix_socket_accepts", lambda _path, timeout=0.2: accepts)
    monkeypatch.delenv("LLM_SCHEDULER_TMUX_TIMEOUT", raising=False)
    commands: list[str] = []
    state = {"ticks": 0}

    def fake_tmux(argv, **_kwargs):
        command = argv[3] if len(argv) > 2 and argv[1] == "-S" else argv[1]
        commands.append(command)
        if command == "has-session":
            # the launch check (the first) always finds the session; later ones follow `session_alive`
            return _Completed(0 if session_alive or commands.count("has-session") == 1 else 1)
        if command == "display-message":
            if len(commands) == 2:  # launch: pane state and start command in one reply
                return _Completed(stdout="1\tsleep")
            return _Completed(stdout="1" if pane_dead_after is not None and state["ticks"] >= pane_dead_after else "0")
        return _Completed()

    def fake_sleep(seconds):
        if seconds >= 1:
            state["ticks"] += 1
            if state["ticks"] >= ticks_until_status:
                status.write_text("0", encoding="utf-8")

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)
    logs = common.setup_run_logs(tmp_path / "logs", "codex")
    output = logs.run_dir / "attempt.out"
    status = logs.run_dir / "attempt.status"
    cfg = scheduler.SchedulerConfig(
        provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target="bridge:station",
        tmux_socket=str(tmp_path / "bridge.sock"), tmux_pane="%1", native_tui=True,
    )
    result = scheduler.run_tmux(cfg, logs, ["true"], output, status)
    return result, commands, state["ticks"]


def test_a_running_provider_costs_no_tmux_process_per_second(tmp_path: Path, monkeypatch) -> None:
    result, commands, ticks = _run(tmp_path, monkeypatch, ticks_until_status=60)

    assert result == 0 and ticks == 60
    wait_phase = commands[commands.index("pipe-pane") + 1:]  # after the launch sequence
    assert "has-session" not in wait_phase
    assert wait_phase.count("display-message") == 60 // scheduler.TMUX_PANE_CHECK_SECONDS


def test_launch_reads_the_pane_state_and_start_command_in_one_process(tmp_path: Path, monkeypatch) -> None:
    _result, commands, _ticks = _run(tmp_path, monkeypatch, ticks_until_status=1)

    assert commands[:3] == ["has-session", "display-message", "respawn-pane"]


def test_an_idle_placeholder_that_has_not_exited_yet_is_waited_for_not_reported_as_a_live_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda _name: True)
    monkeypatch.setattr(common, "unix_socket_accepts", lambda _path, timeout=0.2: True)
    replies = iter(["0\texec /bin/sh -c 'exit 0'", "0\texec /bin/sh -c 'exit 0'", "1\texec /bin/sh -c 'exit 0'"])
    respawned: list[int] = []

    def fake_tmux(argv, **_kwargs):
        command = argv[3] if len(argv) > 2 and argv[1] == "-S" else argv[1]
        if command == "display-message" and argv[-1].startswith("#{pane_dead}\t"):
            return _Completed(stdout=next(replies))
        if command == "respawn-pane":
            respawned.append(1)
            status.write_text("0", encoding="utf-8")
        return _Completed()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    logs = common.setup_run_logs(tmp_path / "logs", "codex")
    status = logs.run_dir / "attempt.status"
    cfg = scheduler.SchedulerConfig(
        provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target="bridge:station",
        tmux_socket=str(tmp_path / "bridge.sock"), tmux_pane="%1", native_tui=True,
    )

    assert scheduler.run_tmux(cfg, logs, ["true"], logs.run_dir / "attempt.out", status) == 0
    assert respawned == [1]


def test_a_vanished_server_is_confirmed_with_has_session_and_ends_the_wait(tmp_path: Path, monkeypatch) -> None:
    result, commands, ticks = _run(tmp_path, monkeypatch, ticks_until_status=100, accepts=False, session_alive=False)

    assert result == 143 and ticks <= 1
    assert commands.count("has-session") == 2  # once at launch, once to confirm the missing server


def test_a_refused_connection_with_a_live_session_does_not_end_the_wait(tmp_path: Path, monkeypatch) -> None:
    result, _commands, ticks = _run(tmp_path, monkeypatch, ticks_until_status=5, accepts=False, session_alive=True)

    assert result == 0 and ticks == 5


def test_a_provider_killed_before_writing_its_status_is_reported_within_the_check_interval(tmp_path: Path, monkeypatch) -> None:
    result, _commands, ticks = _run(tmp_path, monkeypatch, ticks_until_status=1000, pane_dead_after=3)

    assert result == 124
    assert ticks <= scheduler.TMUX_PANE_CHECK_SECONDS
