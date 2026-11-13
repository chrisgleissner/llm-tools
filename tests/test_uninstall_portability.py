"""Uninstall documentation, service removal and backup portability (PRD-005).

The uninstall guide `docs/llm-bridge/uninstall.md` lists the files, directories, units and sockets
that llm-tools creates. These tests keep that list in step with the code: every path the code
computes must appear in the guide, in its shell form.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_power, bridge_remote, bridge_runtime, bridge_store, bridge_worker as worker, common, config, routes, usage_service
from llm_tools.bridge import main

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "llm-bridge" / "uninstall.md"


# --------------------------------------------------------------------------- the guide lists every path the code uses


def shell_form(path: Path, env: dict[str, str]) -> str:
    """The path as the guide writes it: variables instead of this test's directories."""
    text = str(path)
    for key, form in (("XDG_CACHE_HOME", "${XDG_CACHE_HOME:-$HOME/.cache}"), ("XDG_STATE_HOME", "${XDG_STATE_HOME:-$HOME/.local/state}"),
                      ("XDG_CONFIG_HOME", "${XDG_CONFIG_HOME:-$HOME/.config}"), ("XDG_RUNTIME_DIR", "${XDG_RUNTIME_DIR}")):
        if text.startswith(env[key] + "/") or text == env[key]:
            return form + text[len(env[key]):]
    if text.startswith(env["HOME"] + "/"):
        return "$HOME" + text[len(env["HOME"]):]
    return text


@pytest.fixture
def sentinel_env(tmp_path, monkeypatch) -> dict[str, str]:
    env = {key: str(tmp_path / name) for key, name in (("HOME", "h"), ("XDG_CACHE_HOME", "c"), ("XDG_STATE_HOME", "s"),
                                                       ("XDG_CONFIG_HOME", "f"), ("XDG_RUNTIME_DIR", "r"))}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("LLM_BRIDGE_STATE_DIR", "LLM_TOOLS_CONFIG", "LLM_TOOLS_LOCAL_BLOCK_DIR", "LLM_TOOLS_RTC_WAKEALARM"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "r").mkdir(mode=0o700)
    return {**env, "PATH": os.environ.get("PATH", "")}


def code_paths(env: dict[str, str]) -> dict[str, Path]:
    """Every location the code names, keyed by what it is."""
    state = bridge_store.default_state_dir(env)
    return {
        "state directory": state,
        "mission database": bridge_store.store_path(env),
        "worker enrolment directory": worker.worker_dir(env),
        "worker enrolment file": worker.worker_dir(env) / worker.WORKER_FILE,
        "worker CA file": worker.worker_dir(env) / worker.CA_FILE,
        "worker audit log": worker.worker_dir(env) / worker.AUDIT_FILE,
        "remote directory": bridge_remote.remote_dir(env),
        "remote file": bridge_remote.remote_dir(env) / bridge_remote.REMOTE_FILE,
        "remote CA file": bridge_remote.remote_dir(env) / bridge_remote.CA_FILE,
        "drafts": state / "drafts",
        "locks": state / "locks",
        "mission runtime": state / "runtime",
        "cache root": common.cache_root(env),
        "usage cache": common.usage_cache_dir(env),
        "usage service directory": usage_service.service_dir(env),
        "usage service latest": usage_service.latest_path(env),
        "usage service history": usage_service.history_path(env),
        "scheduler logs": common.scheduler_log_dir(env),
        "ralph-robin logs": common.ralph_log_dir(env),
        "ralph-robin state": common.ralph_state_file(env),
        "sleep-soak logs": common.soak_log_dir(env),
        "suspend ledger": common.suspend_ledger_path(env),
        "shared usage snapshots": common.shared_snapshot_dir(env),
        "route blocks": routes.local_block_dir(env),
        "capacity wake planner": common.cache_root(env) / "llm-bridge" / "capacity-wakes",
        "usage service socket directory": usage_service.runtime_dir(env),
        "usage service socket": usage_service.socket_path(env),
        "usage service pid file": usage_service.pid_path(env),
        "usage service systemd unit": usage_service.systemd_unit_path(env),
        "usage service launchd agent": usage_service.launchd_plist_path(env),
        "worker systemd unit": worker.service_unit_path(env),
        "configuration file": config.config_path(env),
        "RTC wake alarm": common.rtc_wakealarm_path(env),
    }


def test_uninstall_guide_names_every_path_the_code_uses(sentinel_env):
    guide = GUIDE.read_text(encoding="utf-8")
    missing = {what: shell_form(path, sentinel_env) for what, path in code_paths(sentinel_env).items()
               if shell_form(path, sentinel_env) not in guide}
    assert not missing, f"docs/llm-bridge/uninstall.md does not mention: {missing}"


def test_uninstall_guide_names_the_tmux_socket_directories_and_transient_units(sentinel_env, tmp_path):
    guide = GUIDE.read_text(encoding="utf-8")
    # The socket directory depends on path length and what exists in /tmp; the candidate list is fixed.
    lookup, creation = bridge_runtime._socket_candidate_dirs(os.getuid(), sentinel_env)
    for directory in creation:
        if directory == bridge_runtime._cache_runtime_dir(sentinel_env):
            continue  # inside the cache root, checked below
        shown = re.sub(r"^/tmp/lb-\d+", "/tmp/lb-$UID", shell_form(directory, sentinel_env))
        assert shown in guide or shown in {"/tmp/lb-$UID-2", "/tmp/lb-$UID-3"}, shown  # the guide writes "-1 to -4"
    assert len([d for d in creation if str(d).startswith("/tmp/lb-")]) == 5  # lb-UID and lb-UID-1 .. -4, as the guide says
    assert "${XDG_RUNTIME_DIR}/llm-bridge" in guide and "/tmp/lb-$UID" in guide
    assert str(bridge_runtime._cache_runtime_dir(sentinel_env).relative_to(sentinel_env["XDG_CACHE_HOME"])) in guide
    assert bridge_power._WAKE_UNIT in guide
    for prefix in ("llm-scheduler-wake-", "llm-scheduler-resume-", "ralph-robin-wake-", "llm-sleep-soak-wake-"):
        assert prefix in guide, prefix
    assert common.rtc_wakealarm_path({}).as_posix() in guide
    for unit in (usage_service.SERVICE_NAME, worker.SERVICE_NAME):
        assert f"{unit}.service" in guide
    assert usage_service.SERVICE_LABEL in guide


def test_uninstall_guide_covers_every_install_method_and_is_linked(sentinel_env):
    guide = GUIDE.read_text(encoding="utf-8")
    for command in ("pipx uninstall llm-tools", "python3 -m pip uninstall llm-tools", "llm-bridge worker uninstall-service",
                    "llm-usage --service-uninstall", "llm-bridge worker unenroll", "llm-bridge remote logout", "llm-bridge backup --output"):
        assert command in guide, command
    for entry in ("llm-usage", "llm-scheduler", "ralph-robin", "llm-sleep-soak", "llm-bridge"):
        assert f"`{entry}`" in guide
    for page in ("README.md", "docs/llm-bridge/operations.md"):
        assert "uninstall.md" in (ROOT / page).read_text(encoding="utf-8"), page


# --------------------------------------------------------------------------- service removal with a fake systemctl


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    """A `systemctl` on PATH that records its arguments; the real user manager is never reached."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    script = bin_dir / "systemctl"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def service_env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    values = {"HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_RUNTIME_DIR": str(tmp_path / "run"),
              "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": os.environ["PATH"]}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    return values


def test_worker_uninstall_service_removes_the_unit_and_disables_it(tmp_path, monkeypatch, fake_systemctl, capsys):
    env = service_env(tmp_path, monkeypatch)
    worker.enroll("ws", "wss://c:1", "t" * 40, ca_pem=None, allow_control=False)
    unit, _ = worker.install_service()
    assert unit == Path(env["HOME"]) / ".config" / "systemd" / "user" / "llm-bridge-worker.service" and unit.is_file()
    assert main(["worker", "uninstall-service"]) == 0
    assert "Worker service removed." in capsys.readouterr().out
    assert not unit.exists()
    calls = fake_systemctl.read_text(encoding="utf-8").splitlines()
    assert "--user disable --now llm-bridge-worker.service" in calls and calls[-1] == "--user daemon-reload"
    assert main(["worker", "uninstall-service"]) == 0  # a second run has nothing left to remove
    assert "No worker service is installed." in capsys.readouterr().out
    # The enrolment stays until `worker unenroll`: the guide lists it as data the user may keep.
    assert worker.load_config().name == "ws"


def test_usage_service_uninstall_removes_the_unit_and_keeps_the_data(tmp_path, monkeypatch, fake_systemctl):
    env = service_env(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert usage_service.install_service(60, env) == 0
    unit = usage_service.systemd_unit_path(env)
    assert unit.is_file() and "llm-usage background sampler" in unit.read_text(encoding="utf-8")
    usage_service._atomic_json_write(usage_service.latest_path(env), {"ok": True})
    assert usage_service.uninstall_service(env) == 0
    assert not unit.exists()
    calls = fake_systemctl.read_text(encoding="utf-8").splitlines()
    assert "--user disable --now llm-usage.service" in calls and calls[-1] == "--user daemon-reload"
    assert usage_service.latest_path(env).exists()  # data is the user's to keep or delete (see the guide)
    assert usage_service.uninstall_service(env) == 0  # idempotent


def test_usage_service_uninstall_removes_the_launchd_agent(tmp_path, monkeypatch):
    env = service_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "launchctl-bin"
    bin_dir.mkdir()
    (bin_dir / "launchctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "launchctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert usage_service.install_service(60, {**env, "PATH": os.environ["PATH"]}) == 0
    plist = usage_service.launchd_plist_path(env)
    assert plist.is_file()
    assert usage_service.uninstall_service(env) == 0
    assert not plist.exists()


def test_service_removal_never_touches_the_real_user_manager(tmp_path, monkeypatch, fake_systemctl):
    """Guards the two tests above: the fake is first on PATH, so the real systemctl is never what ran."""
    service_env(tmp_path, monkeypatch)
    import shutil

    assert Path(shutil.which("systemctl") or "").parent == tmp_path / "fake-bin"
    assert subprocess.run(["systemctl", "--user", "status", "x"], check=False).returncode == 0
    assert fake_systemctl.read_text(encoding="utf-8").strip() == "--user status x"


# --------------------------------------------------------------------------- backups restore on another host


def dump(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table except the sequence counters, which restore sets on purpose."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            width = len(db.execute(f'PRAGMA table_info("{table}")').fetchall())
            order = ", ".join(str(index) for index in range(1, width + 1))
            result[table] = [tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
        return result


def populate(project: Path, capsys) -> str:
    assert main(["mission", "create", "--prompt", "First task\n", "--cwd", str(project), "--providers", "codex", "--json"]) == 0
    mission = json.loads(capsys.readouterr().out)["mission"]["id"]
    for text in ("Second task", "Third task"):
        assert main(["task", "add", "--mission", mission, "--prompt", text, "--json"]) == 0
        capsys.readouterr()
    assert main(["mission", "set", "--mission", mission, "--auto-continue", "off", "--json"]) == 0
    capsys.readouterr()
    return mission


def test_backup_restores_on_another_host_with_identical_data(tmp_path, monkeypatch, capsys):
    source_host, target_host = tmp_path / "source-host", tmp_path / "target-host"
    project = source_host / "work" / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(source_host / "home"))
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(source_host / "state"))
    mission = populate(project, capsys)
    backup = tmp_path / "carried.sqlite3"
    assert main(["backup", "--output", str(backup), "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["missions"] == 1 and facts["last_event_id"] > 0
    original = dump(source_host / "state" / "bridge.sqlite3")
    assert dump(backup) == original

    # "Another host": a different home and state directory, and no trace of the first one.
    monkeypatch.setenv("HOME", str(target_host / "home"))
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(target_host / "state"))
    assert main(["restore", "--input", str(backup), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["missions"] == 1 and result["previous_database"] is None
    restored = target_host / "state" / "bridge.sqlite3"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600 and stat.S_IMODE(restored.parent.stat().st_mode) == 0o700
    assert dump(restored) == original
    assert len(original["tasks"]) == 3 and len(original["events"]) >= 3

    # The restored database opens with the same missions, tasks and events through the normal commands.
    assert main(["task", "list", "--mission", mission, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    texts = [task["original_prompt"] for task in (listed["tasks"] if isinstance(listed, dict) else listed)]
    assert texts == ["First task\n", "Second task", "Third task"]
    assert main(["events", "--mission", mission, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)


def text_values(path: Path) -> list[tuple[str, str, str]]:
    """(table, column, value) of every text or blob value in the database."""
    found: list[tuple[str, str, str]] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            for row in db.execute(f'SELECT * FROM "{table}"'):
                for column, value in zip(columns, row):
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", "replace")
                    if isinstance(value, str):
                        found.append((table, column, value))
    return found


def test_backup_holds_no_path_of_the_source_host_except_mission_folders(tmp_path, monkeypatch, capsys):
    source_host = tmp_path / "source-host"
    project = source_host / "work" / "project"
    project.mkdir(parents=True)
    state = source_host / "state"
    monkeypatch.setenv("HOME", str(source_host / "home"))
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(source_host / "run"))
    populate(project, capsys)
    backup = tmp_path / "carried.sqlite3"
    assert main(["backup", "--output", str(backup)]) == 0
    capsys.readouterr()
    markers = [str(source_host), str(state), str(source_host / "home"), str(source_host / "run")]
    # The folder a mission works in is the one place a path belongs: `missions.cwd`, the identity derived from it
    # (`missions.repo_identity`) and the `cwd` of the mission-created event. The folder itself is removed before checking.
    folder_columns = {("missions", "cwd"), ("missions", "repo_identity"), ("events", "data_json")}
    offenders = [(table, column) for table, column, value in text_values(backup)
                 if any(marker in (value.replace(str(project), "<folder>") if (table, column) in folder_columns else value) for marker in markers)]
    assert not offenders, f"source paths outside the mission folder: {sorted(set(offenders))}"
    assert any(table == "missions" and column == "cwd" and value == str(project) for table, column, value in text_values(backup))
    raw = backup.read_bytes()
    for marker in (str(state), str(source_host / "home"), str(source_host / "run")):
        assert marker.encode() not in raw, marker  # nothing else in the file, including free pages


def test_backup_file_format_does_not_depend_on_the_architecture(tmp_path, monkeypatch, capsys):
    """The file is a single self-contained SQLite file in the portable format (see the guide)."""
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    populate(tmp_path, capsys)
    backup = tmp_path / "b.sqlite3"
    assert main(["backup", "--output", str(backup)]) == 0
    header = backup.read_bytes()[:100]
    assert header[:16] == b"SQLite format 3\x00"
    assert header[18] == header[19] == 1  # rollback-journal format: no -wal file is needed to read it
    assert int.from_bytes(header[56:60], "big") == 1  # text is UTF-8, not the native UTF-16 byte order
    assert not Path(str(backup) + "-wal").exists() and not Path(str(backup) + "-shm").exists()
    guide = GUIDE.read_text(encoding="utf-8")
    assert "https://www.sqlite.org/onefile.html" in guide and "https://www.sqlite.org/lts.html" in guide


# --------------------------------------------------------------------------- coordinator install methods (PRD-005)

COORDINATOR_INSTALL = ROOT / "docs" / "distributed" / "install.md"
COORDINATOR_UNINSTALL = ROOT / "docs" / "distributed" / "uninstall.md"
# Each documented coordinator install method (a heading of install.md) and the heading that removes it.
COORDINATOR_METHODS = {
    "Container deployment (recommended)": "Container deployment",
    "Windows": "Windows",
    "Native install with systemd (alternative)": "Native install with systemd",
}


def _headings(path: Path) -> set[str]:
    return {line[3:].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("## ")}


def test_every_coordinator_install_method_has_an_uninstall_section():
    install_headings = _headings(COORDINATOR_INSTALL)
    uninstall_headings = _headings(COORDINATOR_UNINSTALL)
    for install_heading, uninstall_heading in COORDINATOR_METHODS.items():
        assert install_heading in install_headings, f"install.md lost the method {install_heading!r}; update COORDINATOR_METHODS"
        assert uninstall_heading in uninstall_headings, f"uninstall.md has no section for {install_heading!r}"
    assert "uninstall.md" in COORDINATOR_INSTALL.read_text(encoding="utf-8")


def test_coordinator_uninstall_names_what_the_installers_create():
    guide = COORDINATOR_UNINSTALL.read_text(encoding="utf-8")
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    for path in ("/usr/local/bin/bridge-coordinator", "/usr/local/bin/llm-bridge-coordinator", "/usr/share/bridge-coordinator",
                 "/var/lib/bridge-coordinator", "/etc/systemd/system/bridge-coordinator.service"):
        assert path in guide, path
        assert Path(path).name in script, path  # the installer creates it (names appear in deploy/install.sh)
    assert "user=bridge" in script and "userdel bridge" in guide
    compose = (ROOT / "container" / "compose.yaml").read_text(encoding="utf-8")
    volumes = re.findall(r"^  ([a-z-]+):$", compose[compose.index("\nvolumes:"):], re.M)
    assert "bridge-data" in volumes and "bridge-data" in guide
    assert "--profile tailscale down --volumes" in guide and "profiles: [\"tailscale\"]" in compose
    assert 'bridge-coordinator backup' in guide and "unenroll" in guide


def test_systemd_uninstall_matches_the_unit_file():
    unit = (ROOT / "deploy" / "bridge-coordinator.service").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "distributed" / "uninstall.md").read_text(encoding="utf-8")
    values = dict(re.findall(r"^(User|ExecStart|Environment|StateDirectory)=(.*)$", unit, re.M))
    binary = values["ExecStart"].split()[0]
    data_dir = re.search(r"(?:^|\s)BRIDGE_COORDINATOR_DATA_DIR=([^\s]+)", values["Environment"])[1]
    state_directory = values["StateDirectory"]
    assert binary in guide
    assert data_dir in guide
    assert f"bridge-coordinator.service" in guide
    assert f"userdel {values['User']}" in guide
    assert state_directory in guide


def test_staged_install_creates_only_paths_the_uninstall_guide_names(tmp_path):
    fake = tmp_path / "fake-coordinator"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    webdir = tmp_path / "web"
    webdir.mkdir()
    (webdir / "index.html").write_text("ok", encoding="utf-8")
    stage = tmp_path / "stage"
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "install.sh"), "--binary", str(fake), "--web-dir", str(webdir), "--prefix", str(stage)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    guide = (ROOT / "docs" / "distributed" / "uninstall.md").read_text(encoding="utf-8")
    unnamed = []
    for created in stage.rglob("*"):
        if not created.is_file():
            continue
        relative = created.relative_to(stage)
        path = "/" + relative.as_posix()
        ancestors = ["/" + parent.as_posix() for parent in relative.parents if parent.as_posix() != "."]
        if not any(candidate in guide for candidate in [path, *ancestors]):
            unnamed.append(path)
    assert not unnamed, f"uninstall.md does not name staged install paths: {unnamed}"


def test_container_uninstall_matches_the_compose_file():
    compose = (ROOT / "container" / "compose.yaml").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "distributed" / "uninstall.md").read_text(encoding="utf-8")
    volumes_block = compose.split("\nvolumes:\n", 1)[1]
    volume_names = set(re.findall(r"^  ([\w-]+):$", volumes_block, re.M))
    service_blocks = re.split(r"^  ([\w-]+):\s*$", compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0], flags=re.M)
    services = {service_blocks[i]: service_blocks[i + 1] for i in range(1, len(service_blocks), 2)}
    tailscale_services = [body for body in services.values() if re.search(r"^    profiles:\s*\[.*tailscale", body, re.M)]
    tailscale_volumes = {volume for body in tailscale_services for volume in re.findall(r"^\s+- ([\w-]+):", body, re.M)} & volume_names
    default_volumes = (set(re.findall(r"^\s+- ([\w-]+):", services["coordinator"], re.M)) & volume_names) - tailscale_volumes
    assert "bridge-data" in default_volumes
    missing = sorted(name for name in default_volumes if name not in guide)
    assert not missing, f"uninstall.md omits default coordinator volumes: {missing}"
    assert "--volumes" in guide
    tailscale_text = guide.split("If you started the Tailscale profile", 1)[-1].split("## ", 1)[0]
    missing_tailscale = sorted(name for name in tailscale_volumes if name not in tailscale_text and "--volumes" not in guide)
    assert not missing_tailscale, f"uninstall.md omits Tailscale volumes: {missing_tailscale}"


def test_windows_uninstall_matches_the_install_guide():
    install = (ROOT / "docs" / "distributed" / "install.md").read_text(encoding="utf-8")
    uninstall = (ROOT / "docs" / "distributed" / "uninstall.md").read_text(encoding="utf-8")
    install_windows = re.search(r"^## Windows\s*$([\s\S]*?)(?=^## |\Z)", install, re.M)[1]
    uninstall_windows = re.search(r"^## Windows\s*$([\s\S]*?)(?=^## |\Z)", uninstall, re.M)[1]
    data_dir = re.search(r"--data-dir\s+([^\s`]+)", install_windows)[1]
    assert data_dir in uninstall_windows
