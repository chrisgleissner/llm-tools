"""The host-level `llm-bridge-coordinator reset-password` wrapper (PRD-036).

The wrapper is a POSIX shell script. These tests run it with a fake `docker`, `sudo`, `id` and
`bridge-coordinator` first on PATH. The fakes log their command line and never touch a real
Docker daemon, sudo or coordinator. The project follows `tests/test_container_compose.py`: text
and script checks, no containers.
"""

from __future__ import annotations

import os
import pty
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "container" / "llm-bridge-coordinator"
INSTALL_CLI = ROOT / "container" / "install-cli.sh"

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >>"$FAKE_LOG"
if [ "$1" = info ]; then
    [ "${FAKE_DOCKER_REACHABLE:-0}" = 1 ] || [ -n "${FAKE_VIA_SUDO:-}" ]
    exit $?
fi
exit 0
"""
FAKE_SUDO = """#!/bin/sh
echo "sudo $*" >>"$FAKE_LOG"
FAKE_VIA_SUDO=1 exec "$@"
"""
FAKE_ID = """#!/bin/sh
case "$1" in
    -u) echo "${FAKE_UID:-1000}" ;;
    -un) echo "${FAKE_USER:-operator}" ;;
    *) echo "uid=${FAKE_UID:-1000}" ;;
esac
"""
FAKE_COORDINATOR = """#!/bin/sh
echo "bridge-coordinator $*" >>"$FAKE_LOG"
"""


def _script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Host:
    """A temporary host: fake commands on PATH, a command log and a place to run the wrapper."""

    def __init__(self, tmp_path: Path, *, docker: bool = True, sudo: bool = True, coordinator: bool = False) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True)
        self.log = tmp_path / "commands.log"
        self.log.write_text("", encoding="utf-8")
        self.config = tmp_path / "etc"
        self.config.mkdir(parents=True)
        _script(self.bin / "id", FAKE_ID)
        if docker:
            _script(self.bin / "docker", FAKE_DOCKER)
        if sudo:
            _script(self.bin / "sudo", FAKE_SUDO)
        if coordinator:
            _script(self.bin / "bridge-coordinator", FAKE_COORDINATOR)
        # Basic tools only, so a real docker, sudo or bridge-coordinator on this machine is never found.
        for tool in ("sh", "sed", "dirname", "readlink", "printf", "cat", "env"):
            found = shutil.which(tool)
            assert found, tool
            (self.bin / tool).symlink_to(found)

    def place_wrapper(self, folder: Path, *, compose: bool) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "llm-bridge-coordinator"
        shutil.copy(WRAPPER, target)
        if compose:
            (folder / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        return target

    def run(self, command: list[str], *, stdin=subprocess.DEVNULL, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": str(self.bin),
            "HOME": str(self.tmp),
            "FAKE_LOG": str(self.log),
            "LLM_BRIDGE_COORDINATOR_CONFIG_DIR": str(self.config),
            **env,
        }
        return subprocess.run(
            ["/bin/sh", *command],
            stdin=stdin,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )

    def commands(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_docker_group_user_runs_exec_directly_in_the_compose_folder_next_to_the_script(host: Host) -> None:
    project = host.tmp / "opt" / "llm-bridge"
    wrapper = host.place_wrapper(project, compose=True)
    result = host.run([str(wrapper), "reset-password", "--temporary"], FAKE_DOCKER_REACHABLE="1")
    assert result.returncode == 0, result.stderr
    assert host.commands() == [
        "docker info",
        f"docker compose --project-directory {project} -f {project}/compose.yaml exec -T "
        "coordinator bridge-coordinator admin reset-password --temporary",
    ]
    assert "sudo" not in result.stderr and "docker group" not in result.stderr


def test_user_outside_the_docker_group_goes_through_sudo_and_is_told_why(host: Host) -> None:
    project = host.tmp / "opt" / "llm-bridge"
    wrapper = host.place_wrapper(project, compose=True)
    result = host.run([str(wrapper), "reset-password", "--keep-api-tokens", "--username", "admin"])
    assert result.returncode == 0, result.stderr
    assert "(not in the docker group); using sudo" in result.stderr
    assert "operator" in result.stderr
    assert host.commands() == [
        "docker info",
        f"sudo docker compose --project-directory {project} -f {project}/compose.yaml exec -T "
        "coordinator bridge-coordinator admin reset-password --keep-api-tokens --username admin",
        # The fake sudo runs the fake docker, which logs the same line again.
        f"docker compose --project-directory {project} -f {project}/compose.yaml exec -T "
        "coordinator bridge-coordinator admin reset-password --keep-api-tokens --username admin",
    ]


def test_without_docker_access_and_without_sudo_the_wrapper_says_what_to_do(tmp_path: Path) -> None:
    host = Host(tmp_path, sudo=False)
    wrapper = host.place_wrapper(tmp_path / "p", compose=True)
    result = host.run([str(wrapper), "reset-password"])
    assert result.returncode == 1
    assert "sudo is not installed" in result.stderr and "docker group" in result.stderr
    assert host.commands() == ["docker info"], "no compose command may run"


def test_recorded_path_wins_and_works_from_a_copy_in_another_folder(host: Host) -> None:
    unreadable_project = host.tmp / "opt" / "llm-bridge"  # not created: the user could not read it either
    (host.config / "compose-dir").write_text(f"{unreadable_project}\n", encoding="utf-8")
    wrapper = host.place_wrapper(host.tmp / "usr" / "local" / "bin", compose=False)
    result = host.run([str(wrapper), "reset-password"], FAKE_DOCKER_REACHABLE="1")
    assert result.returncode == 0, result.stderr
    assert host.commands()[-1] == (
        f"docker compose --project-directory {unreadable_project} -f {unreadable_project}/compose.yaml exec -T "
        "coordinator bridge-coordinator admin reset-password"
    )


def test_a_symlink_to_the_script_finds_the_compose_folder_of_the_real_script(host: Host) -> None:
    project = host.tmp / "opt" / "llm-bridge"
    wrapper = host.place_wrapper(project, compose=True)
    link_dir = host.tmp / "links"
    link_dir.mkdir()
    link = link_dir / "llm-bridge-coordinator"
    link.symlink_to(wrapper)
    result = host.run([str(link), "reset-password"], FAKE_DOCKER_REACHABLE="1")
    assert result.returncode == 0, result.stderr
    assert f"--project-directory {project} " in host.commands()[-1]


def test_environment_variable_selects_the_compose_folder(host: Host) -> None:
    wrapper = host.place_wrapper(host.tmp / "bin2", compose=False)
    (host.config / "compose-dir").write_text("/recorded\n", encoding="utf-8")
    result = host.run([str(wrapper), "reset-password"], FAKE_DOCKER_REACHABLE="1", LLM_BRIDGE_COORDINATOR_COMPOSE_DIR="/chosen")
    assert result.returncode == 0, result.stderr
    assert "--project-directory /chosen -f /chosen/compose.yaml" in host.commands()[-1]


def test_a_terminal_keeps_the_tty_so_the_prompt_works(host: Host) -> None:
    project = host.tmp / "p"
    wrapper = host.place_wrapper(project, compose=True)
    master, slave = pty.openpty()
    try:
        result = host.run([str(wrapper), "reset-password"], stdin=slave, FAKE_DOCKER_REACHABLE="1")
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    exec_line = host.commands()[-1]
    assert " exec coordinator bridge-coordinator admin reset-password" in exec_line and " -T " not in exec_line


def test_native_install_uses_sudo_with_the_binary_and_passes_options_through(tmp_path: Path) -> None:
    host = Host(tmp_path, docker=False, coordinator=True)
    wrapper = host.place_wrapper(tmp_path / "usr-local-bin", compose=False)
    result = host.run([str(wrapper), "reset-password", "--temporary"])
    assert result.returncode == 0, result.stderr
    assert "needs root; using sudo" in result.stderr
    binary = host.bin / "bridge-coordinator"
    assert host.commands() == [
        f"sudo {binary} admin reset-password --temporary",
        "bridge-coordinator admin reset-password --temporary",
    ]
    assert "docker" not in "\n".join(host.commands())


def test_native_install_as_root_or_as_the_service_user_needs_no_sudo(tmp_path: Path) -> None:
    host = Host(tmp_path, docker=False, coordinator=True)
    wrapper = host.place_wrapper(tmp_path / "w", compose=False)
    for env in ({"FAKE_UID": "0", "FAKE_USER": "root"}, {"FAKE_UID": "998", "FAKE_USER": "bridge"}):
        host.log.write_text("", encoding="utf-8")
        result = host.run([str(wrapper), "reset-password"], **env)
        assert result.returncode == 0, result.stderr
        assert host.commands() == ["bridge-coordinator admin reset-password"], env


def test_neither_install_found_gives_a_clear_error(tmp_path: Path) -> None:
    host = Host(tmp_path, docker=False)
    wrapper = host.place_wrapper(tmp_path / "w", compose=False)
    result = host.run([str(wrapper), "reset-password"])
    assert result.returncode == 1
    assert "cannot find the coordinator" in result.stderr and "install-cli.sh" in result.stderr
    assert host.commands() == []


def test_help_and_unknown_commands_never_run_docker_or_sudo(host: Host) -> None:
    wrapper = host.place_wrapper(host.tmp / "p", compose=True)
    result = host.run([str(wrapper), "reset-password", "--help"])
    assert result.returncode == 0
    assert "exactly one administrator account" in result.stdout
    result = host.run([str(wrapper), "--help"])
    assert result.returncode == 0 and "reset-password" in result.stdout
    result = host.run([str(wrapper), "bogus"])
    assert result.returncode == 2 and "unknown command bogus" in result.stderr
    assert host.commands() == []


def test_install_cli_copies_the_wrapper_and_records_the_compose_folder(tmp_path: Path) -> None:
    prefix = tmp_path / "root"
    project = tmp_path / "opt" / "llm-bridge"
    project.mkdir(parents=True)
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    result = subprocess.run(
        ["/bin/sh", str(INSTALL_CLI), "--compose-dir", str(project), "--prefix", str(prefix)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = prefix / "usr/local/bin/llm-bridge-coordinator"
    assert installed.read_bytes() == WRAPPER.read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o755
    record = prefix / "etc/llm-bridge-coordinator/compose-dir"
    assert record.read_text(encoding="utf-8") == f"{project}\n"
    assert record.stat().st_mode & 0o777 == 0o644

    # The installed copy, run from the world-readable location, finds the project by the record.
    host = Host(tmp_path / "host", docker=True)
    result = host.run([str(installed), "reset-password"], FAKE_DOCKER_REACHABLE="1", LLM_BRIDGE_COORDINATOR_CONFIG_DIR=str(prefix / "etc/llm-bridge-coordinator"))
    assert result.returncode == 0, result.stderr
    assert f"--project-directory {project} " in host.commands()[-1]


def test_install_cli_refuses_a_folder_without_compose_yaml(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", str(INSTALL_CLI), "--compose-dir", str(tmp_path), "--prefix", str(tmp_path / "root")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and "compose.yaml not found" in result.stderr


def test_native_installer_installs_the_wrapper_next_to_the_binary(tmp_path: Path) -> None:
    binary = tmp_path / "bridge-coordinator"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    prefix = tmp_path / "stage"
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "deploy" / "install.sh"), "--binary", str(binary), "--prefix", str(prefix)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = prefix / "usr/local/bin/llm-bridge-coordinator"
    assert installed.read_bytes() == WRAPPER.read_bytes() and installed.stat().st_mode & 0o111
    assert (prefix / "usr/local/bin/bridge-coordinator").exists()
