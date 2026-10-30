"""First run of ``llm-usage``: parallel reads, early table, and no slow work that has a faster source."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from llm_tools import common, usage
from llm_tools.capacity import CapacityKind
from llm_tools.providers import read_kilo
from tests.conftest import write_exe

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[78]")


def _fake_snapshot(name: str) -> object:
    if name == "codex":  # the Codex reader returns a plain dict
        return {"provider": "codex", "available": False, "reason": "fake", "source": "codex fake"}
    return usage.unavailable_snapshot(name, f"{name} fake", reason="fake")


def _readers(delays: dict[str, float], started: list[str] | None = None) -> dict[str, tuple[object, object]]:
    """Fake provider readers that sleep, then report an unavailable snapshot."""
    readers: dict[str, tuple[object, object]] = {}
    for name in usage.ALL_PROVIDERS:
        def reader(name: str = name) -> object:
            if started is not None:
                started.append(name)
            time.sleep(delays.get(name, 0.0))
            return _fake_snapshot(name)

        readers[name] = (reader, lambda name=name: usage.unavailable_snapshot(name, "fallback"))
    return readers


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> usage.Config:
    monkeypatch.setenv("LLM_USAGE_NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    config = usage.Config()
    config.show_remaining_time = False
    config.progress_enabled = False
    return config


def test_providers_are_read_in_parallel(monkeypatch: pytest.MonkeyPatch, cfg: usage.Config) -> None:
    started: list[str] = []
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({name: 0.4 for name in usage.ALL_PROVIDERS}, started))
    cfg.provider_parallelism = 8

    began = time.monotonic()
    data = usage.read_all_provider_data(cfg)
    elapsed = time.monotonic() - began

    assert set(data) >= set(usage.ALL_PROVIDERS)
    assert len(started) == len(usage.ALL_PROVIDERS)
    # Serial reads would take len(ALL_PROVIDERS) * 0.4 s.
    assert elapsed < 0.4 * len(usage.ALL_PROVIDERS) / 2


def test_parallelism_of_one_reads_serially(monkeypatch: pytest.MonkeyPatch, cfg: usage.Config) -> None:
    running = 0
    peak = 0
    lock = threading.Lock()

    def reader(name: str) -> object:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return _fake_snapshot(name)

    monkeypatch.setattr(
        usage,
        "_provider_readers",
        lambda: {name: ((lambda name=name: reader(name)), lambda name=name: usage.unavailable_snapshot(name, "x")) for name in usage.ALL_PROVIDERS},
    )
    cfg.provider_parallelism = 1

    usage.read_all_provider_data(cfg)

    assert peak == 1


def test_hidden_providers_are_not_read(monkeypatch: pytest.MonkeyPatch, cfg: usage.Config) -> None:
    started: list[str] = []
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({}, started))
    cfg.visible_providers = frozenset({"claude", "codex"})

    data = usage.read_all_provider_data(cfg)

    assert sorted(started) == ["claude", "codex"]
    assert data["copilot"].reason == "hidden"


def test_a_failing_reader_does_not_block_the_others(monkeypatch: pytest.MonkeyPatch, cfg: usage.Config) -> None:
    readers = _readers({})

    def broken() -> object:
        raise RuntimeError("boom")

    readers["copilot"] = (broken, lambda: usage.unavailable_snapshot("copilot", "copilot cli", reason="reader-error"))
    monkeypatch.setattr(usage, "_provider_readers", lambda: readers)

    data = usage.read_all_provider_data(cfg)

    assert data["copilot"].reason == "reader-error"
    assert data["claude"].reason == "fake"
    assert data["codex"]["reason"] == "fake"


def test_pending_providers_render_as_reading_without_ready_or_total(cfg: usage.Config) -> None:
    provider_data = {name: _fake_snapshot(name) for name in usage.ALL_PROVIDERS}
    pending = {"copilot", "opencode"}

    rows, _ = usage._build_usage_rows(cfg, {k: v for k, v in provider_data.items() if k not in pending}, pending)

    reading = [row for row in rows if row.pending]
    assert sorted(row.provider for row in reading) == ["Copilot", "OpenCode"]
    assert all(row.left_text == usage.READING_TEXT for row in reading)
    assert not any(row.provider == "Total" for row in rows)
    values = usage.row_values(cfg, reading[0], "Copilot", "yes")
    assert values["Remaining"] == usage.READING_TEXT
    assert values["Ready"] == values["Guidance"] == values["Resets in"] == ""


class _Tty:
    """Stdout stand-in that reports being a terminal."""

    def __init__(self, stream: object) -> None:
        self._stream = stream

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def test_interactive_one_shot_draws_early_then_redraws_in_place(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({"copilot": 0.8}))
    monkeypatch.setattr(usage, "_terminal_rows", lambda: 80)
    monkeypatch.setattr(usage.sys, "stdout", _Tty(usage.sys.stdout))
    cfg.first_table_wait = 0.2

    began = time.monotonic()
    usage.render_once(cfg)
    total = time.monotonic() - began

    out = ANSI.sub("", capsys.readouterr().out)
    assert total >= 0.8
    assert out.count("reading…") == 1
    assert out.count("Provider") == 2  # the early table and the complete one


def test_redraw_moves_up_over_exactly_the_early_table(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config
) -> None:
    import io

    class TtyBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    buffer = TtyBuffer()
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({"copilot": 0.6}))
    monkeypatch.setattr(usage, "_terminal_rows", lambda: 80)
    monkeypatch.setattr(usage.sys, "stdout", buffer)
    cfg.first_table_wait = 0.1

    usage.render_once(cfg)

    text = buffer.getvalue()
    moves = re.findall(r"\x1b\[(\d+)A\r\x1b\[J", text)
    assert len(moves) == 1
    early, _, final = text.partition(f"\x1b[{moves[0]}A\r\x1b[J")
    assert early.count("\n") == int(moves[0])
    assert "reading…" in early
    assert "reading…" not in final
    assert "Total" not in early


def test_early_table_is_skipped_when_it_would_not_fit_the_screen(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({"copilot": 0.5}))
    monkeypatch.setattr(usage, "_terminal_rows", lambda: 10)
    monkeypatch.setattr(usage.sys, "stdout", _Tty(usage.sys.stdout))
    cfg.first_table_wait = 0.1

    usage.render_once(cfg)

    out = capsys.readouterr().out
    assert "reading…" not in out
    assert out.count("Provider") == 1
    assert not re.search(r"\x1b\[\d+A", out)


def test_fast_providers_draw_one_table_with_no_redraw(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({}))
    monkeypatch.setattr(usage.sys, "stdout", _Tty(usage.sys.stdout))
    cfg.first_table_wait = 5.0

    usage.render_once(cfg)

    out = capsys.readouterr().out
    assert "reading…" not in out
    assert out.count("Provider") == 1


def test_json_and_pipes_wait_for_every_provider(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(usage, "_provider_readers", lambda: _readers({"copilot": 0.4}))
    cfg.first_table_wait = 0.05

    # Not a terminal (the test's captured stdout): the table appears once, complete.
    usage.render_once(cfg)
    out = capsys.readouterr().out
    assert "reading…" not in out and out.count("Provider") == 1

    cfg.json_output = True
    monkeypatch.setattr(usage.sys, "stdout", _Tty(usage.sys.stdout))
    usage.render_once(cfg)
    payload = json.loads(capsys.readouterr().out)
    assert payload["copilot"]["reason"] == "fake"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", usage.FIRST_TABLE_WAIT_DEFAULT), ("0", 0.0), ("1.5", 1.5), ("-3", 0.0), ("soon", usage.FIRST_TABLE_WAIT_DEFAULT)],
)
def test_first_table_wait_setting(raw: str, expected: float) -> None:
    assert usage.first_table_wait_seconds({"LLM_USAGE_FIRST_TABLE_WAIT": raw}) == expected


def test_interactive_one_shot_does_not_start_an_ephemeral_sampler(
    monkeypatch: pytest.MonkeyPatch, cfg: usage.Config
) -> None:
    requested: list[bool] = []
    monkeypatch.setattr(usage, "_provider_data_via_service", lambda config, *, start_ephemeral=True: requested.append(start_ephemeral))

    real_stdout = usage.sys.stdout
    monkeypatch.setattr(usage.sys, "stdout", _Tty(real_stdout))
    assert usage.render_once_via_service(cfg) is False
    monkeypatch.setattr(usage.sys, "stdout", real_stdout)
    cfg.json_output = True
    assert usage.render_once_via_service(cfg) is False

    assert requested == [False, True]


def test_copilot_asks_the_quota_endpoint_before_driving_the_cli(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    marker = tmp_path / "cli-ran"
    write_exe(fake_bin / "copilot", f"#!/bin/sh\ntouch {marker}\nsleep 30\n")
    payload = {"quota_snapshots": {"premium_interactions": {"percent_remaining": 40.0, "entitlement": 300}}}
    live = env | {
        "LLM_USAGE_COPILOT_QUOTA_JSON": json.dumps(payload),
        "LLM_USAGE_DISABLE_COPILOT_MONTHLY": "0",
    }

    began = time.monotonic()
    result = common.read_copilot_live(live)

    assert time.monotonic() - began < 5
    assert not marker.exists()
    assert result["monthly"] == {"used": 60.0, "remaining": 40.0}
    assert result["source"] == "copilot api"
    assert result["capture_status"] == "skipped"


def test_copilot_still_drives_the_cli_when_the_quota_endpoint_has_no_answer(env: dict[str, str], fake_bin: Path) -> None:
    write_exe(fake_bin / "copilot", "#!/bin/sh\necho 'Plan: 25% used'\n")
    live = env | {"LLM_USAGE_COPILOT_TIMEOUT": "5", "LLM_USAGE_COPILOT_CAPTURE_CMD": "echo 'Plan: 25% used'"}

    # A capture helper means a test fixture, so the quota endpoint is not consulted first.
    result = common.read_copilot_live(live)

    assert result["monthly"]["used"] == 25.0
    assert result["capture_status"] == "ok"


def test_copilot_real_cli_falls_back_to_the_capture_without_quota_data(
    env: dict[str, str], fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_exe(fake_bin / "copilot", "#!/bin/sh\nexit 0\n")
    captured: list[str] = []
    monkeypatch.setattr(common, "capture_copilot_screen", lambda e=None: (captured.append("capture"), ("ok", "Plan: 10% used"))[1])

    result = common.read_copilot_live(env)  # monthly reader disabled by the fixture: no REST answer

    assert captured == ["capture"]
    assert result["monthly"]["used"] == 10.0


def _kilo_database(home: str, now: int, cost: float) -> None:
    directory = Path(home) / ".local" / "share" / "kilo"
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "kilo.db")
    conn.execute("create table session (time_created integer not null, cost real not null)")
    conn.execute("insert into session (time_created, cost) values (?, ?)", ((now - 3600) * 1000, cost))
    conn.commit()
    conn.close()


def test_kilo_reads_spend_from_its_database_without_starting_the_cli(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    marker = tmp_path / "kilo-ran"
    write_exe(fake_bin / "kilo", f"#!/bin/sh\ntouch {marker}\nsleep 30\n")
    now = 1783158240  # 2026-07-04 09:44:00 UTC
    env["LLM_USAGE_NOW_EPOCH"] = str(now)
    _kilo_database(env["HOME"], now, 4.25)

    began = time.monotonic()
    snap = read_kilo(env)

    assert time.monotonic() - began < 5
    assert not marker.exists()
    spend = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert spend and spend[0].remaining_amount == pytest.approx(4.25)
    assert spend[0].currency == "$"
    assert spend[0].extras.get("spent") is True
    assert snap.source == "kilo db"


def test_kilo_with_a_configured_balance_still_asks_the_cli(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    marker = tmp_path / "kilo-ran"
    write_exe(fake_bin / "kilo", f"#!/bin/sh\ntouch {marker}\necho '│Total Cost                  $9.00│'\n")
    now = 1783158240
    env["LLM_USAGE_NOW_EPOCH"] = str(now)
    env["LLM_USAGE_KILO_BALANCE"] = "12.50"
    _kilo_database(env["HOME"], now, 4.25)

    snap = read_kilo(env)

    assert marker.exists()
    assert any(s.remaining_amount == pytest.approx(12.5) for s in snap.scopes)


def test_kilo_without_a_database_still_asks_the_cli(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    marker = tmp_path / "kilo-ran"
    write_exe(fake_bin / "kilo", f"#!/bin/sh\ntouch {marker}\necho '│Total Cost                  $9.00│'\n")

    snap = read_kilo(env)

    assert marker.exists()
    assert snap.source == "kilo stats"
