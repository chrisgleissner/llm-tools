"""Tests for the Kilo Code CLI provider adapter."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import common, scheduler
from llm_tools.capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_KILO,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
)
from llm_tools.providers import (
    kilo,
    kilo_cli,
    kilo_command_argv,
    kilo_currency,
    kilo_min_balance,
    kilo_mode,
    kilo_monthly_reset_epoch,
    read_kilo,
)


# --- Env-var reader -----------------------------------------------------------


def test_kilo_mode_defaults_to_gateway() -> None:
    assert kilo_mode({}) == "gateway"


def test_kilo_mode_accepts_known_values() -> None:
    for mode in ("gateway", "budget", "byok", "local", "ungated"):
        assert kilo_mode({"LLM_USAGE_KILO_MODE": mode}) == mode


def test_kilo_mode_falls_back_for_unknown() -> None:
    assert kilo_mode({"LLM_USAGE_KILO_MODE": "weird"}) == "gateway"


def test_kilo_min_balance_default_and_override() -> None:
    assert kilo_min_balance({}) == 1.0
    assert kilo_min_balance({"LLM_USAGE_KILO_MIN_BALANCE": "5"}) == 5.0
    assert kilo_min_balance({"LLM_USAGE_KILO_MIN_BALANCE": "bad"}) == 1.0


def test_kilo_currency_returns_env_value() -> None:
    assert kilo_currency({}) is None
    assert kilo_currency({"LLM_USAGE_KILO_CURRENCY": "GBP"}) == "GBP"


def test_kilo_monthly_reset_epoch_advances_to_next_month(env: dict[str, str]) -> None:
    # env fixture has no LLM_USAGE_NOW_EPOCH override; the helper uses
    # now_epoch which honours the override.
    env["LLM_USAGE_NOW_EPOCH"] = "1000"  # 1970-01-01 00:16:40 UTC
    epoch = kilo_monthly_reset_epoch(env)
    assert epoch > 1000
    # 1st of the *next* month at 00:00 local time.
    assert epoch - 1000 < 32 * 86400


# --- read_kilo: env-var path ---------------------------------------------------


def test_read_kilo_inconclusive_without_data(env: dict[str, str]) -> None:
    # No mode, no env vars, no kilo binary: should report inconclusive-usage
    # in the gateway default mode. The conftest env PATH includes a host
    # kilo install, so we must mask it to keep the test deterministic.
    env["PATH"] = "/var/empty"
    snap = read_kilo(env)
    assert snap.provider == PROVIDER_KILO
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"
    assert any(s.kind == CapacityKind.UNKNOWN for s in snap.scopes)


def test_read_kilo_balance_above_minimum(env: dict[str, str]) -> None:
    env.update(
        {
            "LLM_USAGE_KILO_BALANCE": "12.50",
            "LLM_USAGE_KILO_CURRENCY": "GBP",
            "LLM_USAGE_KILO_MIN_BALANCE": "1",
            "PATH": env["PATH"],  # keep PATH intact
        }
    )
    snap = read_kilo(env)
    assert snap.available is True
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].name == SCOPE_BALANCE
    assert balances[0].remaining_amount == 12.5
    assert balances[0].currency == "GBP"


def test_read_kilo_balance_below_minimum(env: dict[str, str]) -> None:
    env.update({"LLM_USAGE_KILO_BALANCE": "0.5", "LLM_USAGE_KILO_MIN_BALANCE": "1"})
    snap = read_kilo(env)
    # Available in gateway mode (we have a known data scope) but the
    # decision logic should mark the balance as insufficient.
    assert snap.available is True
    assert any(
        s.kind == CapacityKind.BALANCE and s.remaining_amount == 0.5 for s in snap.scopes
    )


def test_read_kilo_budget_scope(env: dict[str, str]) -> None:
    env.update(
        {
            "LLM_USAGE_KILO_MONTHLY_BUDGET": "100",
            "LLM_USAGE_KILO_MONTHLY_SPENT": "38",
            "LLM_USAGE_KILO_CURRENCY": "USD",
        }
    )
    snap = read_kilo(env)
    budgets = [s for s in snap.scopes if s.kind == CapacityKind.BUDGET]
    assert budgets
    assert budgets[0].name == SCOPE_BUDGET
    assert budgets[0].total_amount == 100.0
    assert budgets[0].remaining_amount == 62.0
    assert budgets[0].remaining_percent == pytest.approx(62.0)
    assert budgets[0].reset_epoch is not None and budgets[0].reset_epoch > 0


def test_read_kilo_balance_and_budget_both_visible(env: dict[str, str]) -> None:
    env.update(
        {
            "LLM_USAGE_KILO_BALANCE": "5",
            "LLM_USAGE_KILO_MONTHLY_BUDGET": "20",
            "LLM_USAGE_KILO_MONTHLY_SPENT": "5",
        }
    )
    snap = read_kilo(env)
    kinds = {s.kind for s in snap.scopes}
    assert CapacityKind.BALANCE in kinds
    assert CapacityKind.BUDGET in kinds


def test_read_kilo_byok_mode_ungated(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/usr/bin/env python3\nprint('mock')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_KILO_MODE"] = "byok"
    snap = read_kilo(env)
    assert snap.available is True
    scopes = snap.scopes
    assert any(s.kind == CapacityKind.UNGATED for s in scopes)
    label = next(s.label for s in scopes if s.kind == CapacityKind.UNGATED)
    assert label == "byok"


def test_read_kilo_local_mode_label(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_KILO_MODE"] = "local"
    snap = read_kilo(env)
    assert snap.available is True
    label = next(s.label for s in snap.scopes if s.kind == CapacityKind.UNGATED)
    assert label == "local"


def test_read_kilo_ungated_mode_label(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_KILO_MODE"] = "ungated"
    snap = read_kilo(env)
    assert snap.available is True
    label = next(s.label for s in snap.scopes if s.kind == CapacityKind.UNGATED)
    assert label == "unmetered"


def test_read_kilo_missing_cli_ungated_reports_missing_cli(env: dict[str, str], monkeypatch) -> None:
    env["LLM_USAGE_KILO_MODE"] = "byok"
    # Override the env's PATH with an empty one; the conftest env fixture
    # includes a host kilo on PATH which would otherwise be found.
    env["PATH"] = "/var/empty"
    monkeypatch.setenv("PATH", "/var/empty")
    snap = read_kilo(env)
    assert snap.available is False
    assert snap.reason == "missing-cli"


# --- Kilo CLI stats output -----------------------------------------------------


def test_read_kilo_parses_stats_text(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('balance: 7.50 GBP')\n"
        "print('monthly_budget: 100')\n"
        "print('monthly_spent: 30')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_kilo(env)
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].remaining_amount == 7.5
    assert balances[0].currency == "GBP"
    budgets = [s for s in snap.scopes if s.kind == CapacityKind.BUDGET]
    assert budgets
    assert budgets[0].total_amount == 100.0
    assert budgets[0].remaining_amount == 70.0


def test_read_kilo_parses_stats_tui(env: dict[str, str], fake_bin: Path) -> None:
    """The default ``kilo stats`` output is a TUI box layout; the reader
    must parse ``│Key  Value│`` rows and surface a BALANCE scope with
    ``extras={'spent': True}`` when no env-var balance is configured."""
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('┌────┐')\n"
        "print('│Total Cost                  $7.50│')\n"
        "print('│Avg Cost/Day                $1.50│')\n"
        "print('│Sessions                       10│')\n"
        "print('│Days                            5│')\n"
        "print('└────┘')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_kilo(env)
    assert snap.available is True
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances, f"expected a BALANCE scope from the TUI cost row, got {snap.scopes}"
    assert balances[0].remaining_amount == 7.5
    assert balances[0].currency == "$"
    assert balances[0].extras.get("spent") is True
    # Source must mention the kilo stats feed so --show-source surfaces it.
    assert any("kilo stats" in s.source for s in snap.scopes)
    # The CLI spend row is a month-to-date figure, so it must carry a
    # bounded ``reset_epoch`` (next calendar month start) -- otherwise the
    # cross-provider Total row cannot tell MTD spend apart from a
    # lifetime/cumulative total.
    assert balances[0].reset_epoch is not None
    assert balances[0].reset_epoch > common.now_epoch(env)


def test_read_kilo_stats_query_is_bounded_to_mtd(env: dict[str, str], fake_bin: Path, tmp_path: Path) -> None:
    """``kilo stats`` defaults to all-time, which would surface a
    long-running install's lifetime cost as if it were this month's bill.
    The reader must always pass ``--days N`` (N = days since the start of
    the current calendar month) so the parsed cost is month-to-date."""
    log_path = tmp_path / "argv.txt"
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        f"import sys\n"
        f"open({str(log_path)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        "print('│Total Cost                  $13.15│')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    # Pin the clock to 2026-07-03 (day 3 of the month) so the expected
    # days argument is deterministic regardless of when tests run.
    env["LLM_USAGE_NOW_EPOCH"] = "1783074000"  # 2026-07-03 UTC
    snap = read_kilo(env)
    assert snap.available is True
    assert log_path.exists(), "kilo stats was never invoked"
    argv = log_path.read_text().split()
    assert "--days" in argv, f"kilo stats not bounded to MTD: argv={argv}"
    days_idx = argv.index("--days")
    assert int(argv[days_idx + 1]) == 3, argv
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances and balances[0].remaining_amount == pytest.approx(13.15)


def test_read_kilo_exact_mtd_cost_uses_db_not_rolling_days(
    env: dict[str, str], fake_bin: Path
) -> None:
    """The spend row must reflect the exact current-month total.

    ``kilo stats --days N`` is a rolling last-N-days window. On July 4,
    2026, ``--days 4`` includes part of June 30 and can overstate July
    month-to-date spend. The reader therefore replaces the parsed
    ``Total Cost`` with an exact sum from Kilo's local SQLite DB.
    """
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('│Total Cost                  $37.33│')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_NOW_EPOCH"] = "1783158240"  # 2026-07-04 09:44:00 UTC

    kilo_dir = Path(env["HOME"]) / ".local" / "share" / "kilo"
    kilo_dir.mkdir(parents=True, exist_ok=True)
    db_path = kilo_dir / "kilo.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table session (time_created integer not null, cost real not null)")
    # June 30 cost that should NOT leak into July MTD.
    conn.execute(
        "insert into session (time_created, cost) values (?, ?)",
        (1782777600 * 1000, 4.93),  # 2026-06-30 00:00:00 UTC
    )
    # July 2-4 costs that SHOULD be summed.
    conn.executemany(
        "insert into session (time_created, cost) values (?, ?)",
        [
            (1782950400 * 1000, 11.33),  # 2026-07-02 00:00:00 UTC
            (1783036800 * 1000, 5.52),   # 2026-07-03 00:00:00 UTC
            (1783123200 * 1000, 15.55),  # 2026-07-04 00:00:00 UTC
        ],
    )
    conn.commit()
    conn.close()

    snap = read_kilo(env)
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].remaining_amount == pytest.approx(32.40)
    assert balances[0].extras.get("spent") is True
    assert "kilo db" in balances[0].source


def test_read_kilo_exact_mtd_cost_warns_and_falls_back_on_schema_mismatch(
    env: dict[str, str], fake_bin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('│Total Cost                  $37.33│')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    kilo._KILO_DB_SCHEMA_WARNINGS_EMITTED.clear()

    kilo_dir = Path(env["HOME"]) / ".local" / "share" / "kilo"
    kilo_dir.mkdir(parents=True, exist_ok=True)
    db_path = kilo_dir / "kilo.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table session (created_at integer not null, amount real not null)")
    conn.commit()
    conn.close()

    snap = read_kilo(env)
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].remaining_amount == pytest.approx(37.33)
    assert "kilo db" not in balances[0].source
    captured = capsys.readouterr()
    assert "warning: kilo db exact-MTD cost disabled" in captured.err
    assert "time_created" in captured.err
    assert "cost" in captured.err


def test_read_kilo_source_omits_db_when_exact_cost_is_not_rendered(
    env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('│Total Cost                  $37.33│')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_KILO_BALANCE"] = "12.50"
    env["LLM_USAGE_KILO_CURRENCY"] = "GBP"

    kilo_dir = Path(env["HOME"]) / ".local" / "share" / "kilo"
    kilo_dir.mkdir(parents=True, exist_ok=True)
    db_path = kilo_dir / "kilo.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table session (time_created integer not null, cost real not null)")
    conn.execute(
        "insert into session (time_created, cost) values (?, ?)",
        (1783123200 * 1000, 15.55),
    )
    conn.commit()
    conn.close()

    snap = read_kilo(env)
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE]
    assert balances
    assert balances[0].remaining_amount == pytest.approx(12.5)
    assert snap.source == "kilo stats + env"
    assert all("kilo db" not in scope.source for scope in snap.scopes)


def test_read_kilo_spend_row_has_monthly_reset(env: dict[str, str], fake_bin: Path) -> None:
    """The MTD spend scope must tag itself with the next calendar month
    start so ``budget_total_row`` recognises it as a bounded cycle and
    includes it in the cross-provider Total row."""
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "print('│Total Cost                  $13.15│')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    env["LLM_USAGE_NOW_EPOCH"] = "1783074000"  # 2026-07-03
    snap = read_kilo(env)
    balances = [s for s in snap.scopes if s.kind == CapacityKind.BALANCE and s.extras.get("spent")]
    assert balances
    assert balances[0].reset_epoch is not None
    import datetime as _dt
    reset_dt = _dt.datetime.fromtimestamp(balances[0].reset_epoch, tz=_dt.timezone.utc)
    assert (reset_dt.year, reset_dt.month, reset_dt.day) == (2026, 8, 1)
    assert balances[0].extras.get("period") == "mtd"


def test_read_kilo_spend_disappears_when_stats_cli_missing_cli(
    env: dict[str, str], fake_bin: Path, monkeypatch
) -> None:
    """With PATH cleared and no env-var fallback, the reader reports
    ``inconclusive-usage`` rather than fabricating a spend figure -- a
    conservative fallback that keeps the row out of the cross-provider
    monthly total instead of inventing one."""
    monkeypatch.setenv("PATH", "/var/empty")
    env["PATH"] = "/var/empty"
    snap = read_kilo(env)
    assert snap.available is False
    assert snap.reason == "inconclusive-usage"
    assert not any(s.extras.get("spent") for s in snap.scopes)


def test_read_kilo_parses_stats_json(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'balance': 9.0, 'currency': 'USD', 'budget': 50, 'spent': 12}))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["PATH"] = str(fake_bin)
    snap = read_kilo(env)
    balance = next(s for s in snap.scopes if s.kind == CapacityKind.BALANCE)
    assert balance.remaining_amount == 9.0
    assert balance.currency == "USD"
    budget = next(s for s in snap.scopes if s.kind == CapacityKind.BUDGET)
    assert budget.total_amount == 50.0
    assert budget.remaining_amount == 38.0


# --- Kilo command construction -------------------------------------------------


def test_kilo_command_argv_attached() -> None:
    argv = kilo_command_argv(cfg_attached=True, cwd="/tmp/work", prompt="hello world")
    assert argv == ["kilo", "run", "hello world"]


def test_kilo_command_argv_headless() -> None:
    argv = kilo_command_argv(cfg_attached=False, cwd="/tmp/work", prompt="hello world")
    assert argv == ["kilo", "run", "--dir", "/tmp/work", "hello world"]


# --- Scheduler: Kilo end-to-end ------------------------------------------------


def test_scheduler_accepts_kilo_provider(env: dict[str, str]) -> None:
    # Use --dry-run so the scheduler resolves the kilo decision without
    # looping on the inconclusive-usage wait gate. The check is that kilo
    # is accepted as a provider and the decision event is recorded.
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "kilo",
            "--prompt",
            "x",
            "--scope",
            "byok",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env | {"LLM_USAGE_KILO_MODE": "byok"},
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions


def test_scheduler_rejects_kilo_invalid_scope(env: dict[str, str]) -> None:
    bad = run_cmd(
        ["./llm-scheduler", "--provider", "kilo", "--prompt", "x", "--scope", "5h"],
        env,
    )
    assert bad.returncode == 2
    assert "not valid for kilo" in bad.stderr


def test_scheduler_kilo_balance_below_minimum_blocks(env: dict[str, str]) -> None:
    # No kilo binary on PATH, balance below the minimum → decision should
    # block with insufficient-balance. Use --dry-run so the scheduler
    # resolves and exits without actually launching the provider or
    # looping the wait gate.
    env["LLM_USAGE_KILO_BALANCE"] = "0.1"
    env["LLM_USAGE_KILO_MIN_BALANCE"] = "5"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "kilo",
            "--prompt",
            "x",
            "--scope",
            "balance",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decisions = [e for e in events if e["type"] == "usage_decision"]
    assert decisions
    assert decisions[0]["data"]["usable"] is False
    assert decisions[0]["data"]["reason"] == "insufficient-balance"


def test_scheduler_kilo_byok_launches_with_attached_command(env: dict[str, str], fake_bin: Path) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/usr/bin/env python3\nprint('hi from kilo')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["LLM_USAGE_KILO_MODE"] = "byok"
    env["LLM_SCHEDULER_HEADLESS"] = "1"  # avoid attached path complexity
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "kilo",
            "--prompt",
            "x",
            "--scope",
            "byok",
            "--command-template",
            "kilo run --auto {prompt}",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "hi from kilo" in result.stdout


def test_scheduler_dry_run_kilo_balance_event(env: dict[str, str]) -> None:
    env["LLM_USAGE_KILO_BALANCE"] = "42"
    env["LLM_USAGE_KILO_CURRENCY"] = "GBP"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "kilo",
            "--prompt",
            "x",
            "--scope",
            "balance",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decision_events = [e for e in events if e["type"] == "usage_decision"]
    assert decision_events, events
    assert decision_events[0]["data"]["usable"] is True
    windows = decision_events[0]["data"]["windows"]
    assert any(w["name"] == "balance" and w["kind"] == "balance" for w in windows)


def test_scheduler_dry_run_kilo_budget_pacing(env: dict[str, str]) -> None:
    env["LLM_USAGE_KILO_MONTHLY_BUDGET"] = "100"
    env["LLM_USAGE_KILO_MONTHLY_SPENT"] = "50"
    result = run_cmd(
        [
            "./llm-scheduler",
            "--provider",
            "kilo",
            "--prompt",
            "x",
            "--scope",
            "budget",
            "--command-template",
            "true",
            "--dry-run",
            "--log-dir",
            str(Path(env["HOME"]) / "logs"),
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    run_dir = Path(result.stdout.strip().split()[-1])
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    decision_events = [e for e in events if e["type"] == "usage_decision"]
    assert decision_events[0]["data"]["usable"] is True
    windows = decision_events[0]["data"]["windows"]
    budget = next(w for w in windows if w["name"] == "budget")
    assert budget["remaining"] == 50.0
    assert budget["kind"] == "budget"


# --- Usage table rendering for Kilo --------------------------------------------


def test_usage_table_renders_kilo_scope(env: dict[str, str], capsys, monkeypatch) -> None:
    from llm_tools import usage

    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")
    monkeypatch.setenv("LLM_USAGE_KILO_BALANCE", "12.40")
    monkeypatch.setenv("LLM_USAGE_KILO_CURRENCY", "GBP")
    monkeypatch.setenv("LLM_USAGE_KILO_MONTHLY_BUDGET", "50")
    monkeypatch.setenv("LLM_USAGE_KILO_MONTHLY_SPENT", "20")

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    # Merge the monkeypatched env into the conftest env so the kilo reader
    # sees both the conftest HOME/CODEX dirs and the test-specific kilo
    # configuration.
    merged = dict(env)
    for key in (
        "LLM_USAGE_KILO_BALANCE",
        "LLM_USAGE_KILO_CURRENCY",
        "LLM_USAGE_KILO_MONTHLY_BUDGET",
        "LLM_USAGE_KILO_MONTHLY_SPENT",
    ):
        value = os.environ.get(key)
        if value is not None:
            merged[key] = value
    snap = read_kilo(merged)
    json_obj = {
        "provider": snap.provider,
        "available": snap.available,
        "reason": snap.reason,
        "source": snap.source,
        "selected_model": snap.selected_model,
        "scopes": [
            {
                "name": s.name,
                "kind": s.kind,
                "remaining_percent": s.remaining_percent,
                "remaining_amount": s.remaining_amount,
                "total_amount": s.total_amount,
                "currency": s.currency,
                "reset_epoch": s.reset_epoch,
                "label": s.label,
                "source": s.source,
            }
            for s in snap.scopes
        ],
    }
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_kilo_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "Kilo" in out
    assert "balance" in out
    assert "budget" in out
    # Currency-prefixed balance.
    assert "GBP12" in out


def test_usage_table_renders_kilo_spent(env: dict[str, str], capsys, monkeypatch) -> None:
    """When the snapshot carries a spent-style BALANCE scope (cost from
    the CLI stats, no env-var balance), the renderer prefixes the value
    with ``spent`` so the table does not pretend the amount is remaining
    capacity."""
    from llm_tools import usage

    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("LLM_USAGE_DISABLE_COPILOT", "1")

    from io import StringIO
    import contextlib

    cfg = usage.Config()
    cfg.color_enabled = False
    cfg.no_header = True
    json_obj = {
        "provider": PROVIDER_KILO,
        "available": True,
        "reason": "",
        "source": "kilo stats",
        "selected_model": None,
        "scopes": [
            {
                "name": SCOPE_BALANCE,
                "kind": CapacityKind.BALANCE,
                "remaining_percent": None,
                "remaining_amount": 7.5,
                "total_amount": None,
                "currency": "$",
                "reset_epoch": None,
                "label": None,
                "source": "kilo stats",
                "extras": {"spent": True},
            }
        ],
    }
    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        usage.print_kilo_rows(cfg, json_obj)
    out = buf.getvalue()
    assert "Kilo" in out
    # Spend rows read "spend  $7.5" (scope label + left-aligned amount), never
    # right-aligned "spent $", and are distinct from a funded "balance" row.
    assert "spend" in out
    assert "$7.5" in out
    assert "balance   $7" not in out
    # A spent-cost row from an available snapshot means the provider is ready.
    assert "yes" in out


# --- Helpers -------------------------------------------------------------------


def run_cmd(args, env):
    from .conftest import run_cmd as _run_cmd
    return _run_cmd(args, env)
