"""PROV-003 / PROV-004: Kilo and OpenCode session capture and exact resume from the session store.

Both CLIs keep sessions in a SQLite database (``<XDG data home>/<provider>/<provider>.db``, table
``session``). The fake CLI below writes that store the way the real one does; no real CLI runs.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_tools import bridge_sessions as sessions
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from tests.test_bridge_runtime import _provider_config

PROVIDERS = ("kilo", "opencode")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, slug text NOT NULL,"
    " directory text NOT NULL, title text NOT NULL, version text NOT NULL, time_created integer NOT NULL,"
    " time_updated integer NOT NULL, time_archived integer)"
)
FAKE_CLI = '''#!{python}
import sqlite3, sys, time
args = sys.argv[1:]
if args[:1] == ["--create"]:  # --create DB DIRECTORY SESSION_ID [PARENT_ID]: what the real CLI does on its first prompt
    db, directory, sid = args[1:4]
    parent = args[4] if len(args) > 4 else None
    now = time.time_ns() // 1_000_000 + 5
    con = sqlite3.connect(db)
    con.execute({schema!r})
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,NULL)", (sid, "p", parent, "slug", directory, "t", "1", now, now + 5))
    con.commit()
    sys.exit(0)
print("usage: {name} [project]\\n  -s, --session  session id to continue  [string]")
'''


def _cli(bin_dir: Path, provider: str) -> Path:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / provider
    path.write_text(FAKE_CLI.format(python=sys.executable, schema=SCHEMA, name=provider), encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_session(cli: Path, db: Path, directory: Path | str, sid: str, parent: str | None = None) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(cli), "--create", str(db), str(directory), sid, *([parent] if parent else [])], check=True)


def _old_session(db: Path, directory: Path, sid: str) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute(SCHEMA)
    old = time.time_ns() // 1_000_000 - 3_600_000
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,NULL)", (sid, "p", None, "s", str(directory), "t", "1", old, old))
    con.commit()
    con.close()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    return SimpleNamespace(data=tmp_path / "data", cwd=cwd.resolve(), bin=tmp_path / "bin", tmp=tmp_path)


def _db(env, provider: str) -> Path:
    return env.data / provider / f"{provider}.db"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_new_session_is_the_one_created_after_launch_in_this_directory(env, provider) -> None:
    cli = _cli(env.bin, provider)
    db = _db(env, provider)
    other = env.tmp / "other"
    other.mkdir()
    _old_session(db, env.cwd, "ses_olderthanlaunch")  # same directory, created before launch: not ours
    started = time.time_ns()
    _write_session(cli, db, other, "ses_otherdirectory")
    _write_session(cli, db, env.cwd, "ses_subagentchild1", parent="ses_parent")
    assert sessions.discover_new_db_session(provider, cwd=env.cwd, started_ns=started) is None
    _write_session(cli, db, env.cwd, "ses_thisrunsession")
    identity = sessions.discover_new_db_session(provider, cwd=env.cwd, started_ns=started)
    assert identity == sessions.NativeSessionIdentity(provider, "ses_thisrunsession", str(env.cwd), f"{provider}:session.db")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_two_candidate_sessions_are_ambiguous_and_give_no_identity(env, provider) -> None:
    cli = _cli(env.bin, provider)
    started = time.time_ns()
    _write_session(cli, _db(env, provider), env.cwd, "ses_candidate_a1")
    _write_session(cli, _db(env, provider), env.cwd, "ses_candidate_b2")
    assert sessions.discover_new_db_session(provider, cwd=env.cwd, started_ns=started) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_or_unreadable_store_gives_no_identity(env, provider) -> None:
    assert sessions.discover_new_db_session(provider, cwd=env.cwd, started_ns=time.time_ns()) is None
    db = _db(env, provider)
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database")
    assert sessions.discover_new_db_session(provider, cwd=env.cwd, started_ns=time.time_ns()) is None
    assert sessions.provider_session_metadata(provider, "ses_whatever1234", cwd=env.cwd) is None
    assert sessions.discover_new_db_session("codex", cwd=env.cwd, started_ns=time.time_ns()) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_metadata_is_by_exact_id_and_directory(env, provider) -> None:
    cli = _cli(env.bin, provider)
    _write_session(cli, _db(env, provider), env.cwd, "ses_exactlyone01")
    record = sessions.provider_session_metadata(provider, "ses_exactlyone01", cwd=env.cwd)
    assert record is not None and record.identity.session_id == "ses_exactlyone01"
    assert sessions.provider_session_metadata(provider, "ses_exactlyone02", cwd=env.cwd) is None
    assert sessions.provider_session_metadata(provider, "ses_exactlyone01", cwd=env.tmp) is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_resume_argv_selects_the_exact_session_only_for_a_valid_id(env, provider) -> None:
    identity = sessions.NativeSessionIdentity(provider, "ses_exactlyone01", str(env.cwd), f"{provider}:session.db")
    help_text = "  -s, --session  session id to continue"
    assert sessions.resume_argv(identity, executable=provider, help_text=help_text) == [provider, "--session", "ses_exactlyone01"]
    assert sessions.resume_argv(identity, executable=provider, help_text="  -c, --continue  last session") is None
    bad = sessions.NativeSessionIdentity(provider, "-ses_option_like", str(env.cwd), f"{provider}:session.db")
    assert sessions.resume_argv(bad, executable=provider, help_text=help_text) is None
    wrong_source = sessions.NativeSessionIdentity(provider, "ses_exactlyone01", str(env.cwd), "codex:session_meta")
    assert sessions.resume_argv(wrong_source, executable=provider, help_text=help_text) is None


def _hooks(env, store: BridgeStore, mission_id: str) -> BridgeMissionHooks:
    hooks = BridgeMissionHooks(mission_id, handle=SimpleNamespace(session_name="m", socket_path=env.tmp / "sock", provider_pane="%1"),
                               headless=False, native_tui=True, controller_logs=env.tmp / "controller.log")
    hooks.bind(store)
    return hooks


def _new_mission(env):
    store = BridgeStore(env.tmp / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=env.cwd)
    store.set_mission_status(mission["id"], "RUNNING")
    return store, mission, _hooks(env, store, mission["id"])


def _launch(env, store, mission, hooks, provider: str, cli: Path, n: int):
    task = store.add_task(mission["id"], f"prompt {n}")
    config = _provider_config(provider, env.cwd, env.tmp / f"run{n}")
    context = {"task_id": task}
    assert hooks.before_launch(context, {"provider": provider}, config, n)
    argv = config.command_argv_transform([str(cli), "--prompt", f"prompt {n}"])
    return context, config, argv


@pytest.mark.parametrize("provider", PROVIDERS)
def test_session_created_by_the_run_is_recorded_then_resumed_exactly(env, provider) -> None:
    cli = _cli(env.bin, provider)
    store, mission, hooks = _new_mission(env)
    context, config, argv = _launch(env, store, mission, hooks, provider, cli, 1)
    assert argv == [str(cli), "--prompt", "prompt 1"], "a new run is not given an id: the CLI creates its own"
    other = env.tmp / "other"
    other.mkdir()
    _write_session(cli, _db(env, provider), other, "ses_unrelatedrun1")  # another run elsewhere: not ours
    _write_session(cli, _db(env, provider), env.cwd, "ses_createdbyrun1")
    hooks._observe_db_session(context["operation_id"], provider)
    assert store.get_operation(context["operation_id"])["restoration_state"] == "NATIVE_CREATED"
    config.run_dir.mkdir()
    hooks.after_iteration(context, {"provider": provider}, config, 1, 0, 0.1)
    operation = store.get_operation(context["operation_id"])
    assert operation["native_session_id"] == "ses_createdbyrun1" and operation["restoration_state"] == "NATIVE_CREATED"
    saved = store.get_mission(mission["id"])["config"]["native_sessions"][provider]
    assert saved["session_id"] == "ses_createdbyrun1" and saved["owned_by_bridge"] is True and saved["source"] == f"{provider}:session.db"
    store.close()

    # Controller restart: a new hooks instance resumes exactly the recorded id, never "--continue".
    store = BridgeStore(env.tmp / "bridge.sqlite3")
    hooks = _hooks(env, store, mission["id"])
    context2, _config2, resumed = _launch(env, store, mission, hooks, provider, cli, 2)
    assert resumed == [str(cli), "--session", "ses_createdbyrun1", "--prompt", "prompt 2"]
    assert "--continue" not in resumed and "-c" not in resumed
    assert store.get_operation(context2["operation_id"])["restoration_state"] == "RESTORING"
    store.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_ambiguous_sessions_record_nothing_and_next_run_starts_fresh(env, provider) -> None:
    cli = _cli(env.bin, provider)
    store, mission, hooks = _new_mission(env)
    context, config, _argv = _launch(env, store, mission, hooks, provider, cli, 1)
    _write_session(cli, _db(env, provider), env.cwd, "ses_candidate_a1")
    _write_session(cli, _db(env, provider), env.cwd, "ses_candidate_b2")
    hooks._observe_db_session(context["operation_id"], provider)
    config.run_dir.mkdir()
    hooks.after_iteration(context, {"provider": provider}, config, 1, 0, 0.1)
    operation = store.get_operation(context["operation_id"])
    assert operation["native_session_id"] is None and operation["restoration_state"] == "UNAVAILABLE"
    assert "native_sessions" not in store.get_mission(mission["id"])["config"]
    _context2, _config2, fresh = _launch(env, store, mission, hooks, provider, cli, 2)
    assert fresh == [str(cli), "--prompt", "prompt 2"]
    store.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_saved_session_missing_from_the_store_starts_fresh(env, provider) -> None:
    cli = _cli(env.bin, provider)
    store, mission, hooks = _new_mission(env)
    seed = store.begin_operation(store.add_task(mission["id"], "seed"), iteration=1, station=provider)
    store.record_operation_session(seed, provider=provider, restoration_state="NATIVE_CREATED",
                                   session_id="ses_deletedsession", cwd=str(env.cwd), source=f"{provider}:session.db")
    _context, _config, argv = _launch(env, store, mission, hooks, provider, cli, 2)
    assert argv == [str(cli), "--prompt", "prompt 2"]
    store.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_session_created_before_the_last_look_is_found_when_the_run_ends(env, provider) -> None:
    cli = _cli(env.bin, provider)
    store, mission, hooks = _new_mission(env)
    context, config, _argv = _launch(env, store, mission, hooks, provider, cli, 1)
    _write_session(cli, _db(env, provider), env.cwd, "ses_foundatexit1")
    config.run_dir.mkdir()
    hooks.after_iteration(context, {"provider": provider}, config, 1, 0, 0.1)
    assert store.get_operation(context["operation_id"])["native_session_id"] == "ses_foundatexit1"
    store.close()


def test_help_probe_runs_once_per_cli_path_and_version(env, monkeypatch) -> None:
    cli = _cli(env.bin, "kilo")
    sessions._HELP_CACHE.clear()
    calls = []
    real_run = subprocess.run

    def counting(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(sessions.subprocess, "run", counting)
    identity = sessions.NativeSessionIdentity("kilo", "ses_exactlyone01", str(env.cwd), "kilo:session.db")
    for _ in range(3):
        assert sessions.resume_argv(identity, executable=str(cli)) == [str(cli), "--session", "ses_exactlyone01"]
    assert len(calls) == 1 and calls[0][1] == 10 == sessions.HELP_PROBE_TIMEOUT_SECONDS
    # A replaced binary (new mtime/size) is probed again.
    cli.write_text(cli.read_text() + "\n# upgraded\n")
    sessions.resume_argv(identity, executable=str(cli))
    assert len(calls) == 2


def test_failed_help_probe_is_not_cached(env, monkeypatch) -> None:
    cli = _cli(env.bin, "kilo")
    sessions._HELP_CACHE.clear()
    outcomes = [subprocess.TimeoutExpired("kilo", 10), None]
    real_run = subprocess.run

    def flaky(argv, **kwargs):
        if outcomes and (outcome := outcomes.pop(0)) is not None:
            raise outcome
        return real_run(argv, **kwargs)

    monkeypatch.setattr(sessions.subprocess, "run", flaky)
    identity = sessions.NativeSessionIdentity("kilo", "ses_exactlyone01", str(env.cwd), "kilo:session.db")
    assert sessions.resume_argv(identity, executable=str(cli)) is None
    assert sessions.resume_argv(identity, executable=str(cli)) == [str(cli), "--session", "ses_exactlyone01"]
