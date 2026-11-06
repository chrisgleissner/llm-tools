"""Offline LAN operation (TEST-012): coordinator, worker and remote CLI need nothing beyond loopback.

``test_round_trip_in_a_network_namespace_without_a_route`` runs the round trip
inside ``bwrap --unshare-net`` (only a loopback interface, no route out). It is
skipped on hosts where unprivileged namespaces are not available.
``test_round_trip_opens_only_loopback_sockets`` runs everywhere: it samples
the sockets of the coordinator and of this process during the round trip and
fails on any non-loopback endpoint or UDP socket (for example a DNS query).
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled
from tests.distributed_harness import login_remote, requires_coordinator, running_system

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_coordinator
INNER = "BRIDGE_OFFLINE_INNER"


def _round_trip(system, capsys) -> None:
    login_remote(system.coordinator, system.tmp, capsys)
    assert main(["--target", "ws", "mission", "list", "--json"]) == 0
    assert system.mission_id in capsys.readouterr().out
    assert main(["--target", "ws", "status", "--mission", system.mission_id[:8]]) == 0
    assert system.mission_id[:8] in capsys.readouterr().out
    assert main(["--target", "ws", "task", "pause", "--mission", system.mission_id, "--task", "1", "--json"]) == 0
    capsys.readouterr()
    with BridgeStore(system.state / "bridge.sqlite3") as store:
        assert store.list_tasks(system.mission_id)[0]["status"] == "PAUSED"


def _socket_inodes(pid: int) -> set[str]:
    inodes: set[str] = set()
    try:
        for entry in os.scandir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(entry.path)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def _address(hex_value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    raw = bytes.fromhex(hex_value)
    if len(raw) == 4:
        return ipaddress.IPv4Address(raw[::-1])
    words = [raw[index:index + 4][::-1] for index in range(0, 16, 4)]
    return ipaddress.IPv6Address(b"".join(words))


def _is_loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _own_pids() -> list[int]:
    """This process and its direct children (the coordinator), so the sampler can start before they do."""
    pids = [os.getpid()]
    for task in Path("/proc/self/task").iterdir():
        try:
            pids.extend(int(pid) for pid in (task / "children").read_text().split())
        except OSError:
            pass
    return pids


def _endpoints(pids: list[int]) -> list[tuple[str, str, str]]:
    """``(protocol, local, remote)`` of every IP socket the processes hold."""
    inodes = set().union(*(_socket_inodes(pid) for pid in pids))
    found = []
    for table in ("tcp", "tcp6", "udp", "udp6"):
        try:
            lines = Path(f"/proc/net/{table}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if fields[9] in inodes:
                local, remote = (field.split(":")[0] for field in fields[1:3])
                found.append((table, str(_address(local)), str(_address(remote))))
    return found


def _violations(endpoints: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    bad = []
    for table, local, remote in endpoints:
        local_ip, remote_ip = ipaddress.ip_address(local), ipaddress.ip_address(remote)
        unconnected = remote_ip.is_unspecified  # a listener or unconnected socket has no peer
        if table.startswith("udp") and not (_is_loopback(local_ip) and (unconnected or _is_loopback(remote_ip))):
            bad.append((table, local, remote))
        elif not _is_loopback(local_ip) or not (unconnected or _is_loopback(remote_ip)):
            bad.append((table, local, remote))
    return bad


def test_round_trip_opens_only_loopback_sockets(tmp_path, monkeypatch, capsys):
    seen: set[tuple[str, str, str]] = set()
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            seen.update(_endpoints(_own_pids()))
            stop.wait(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()  # before the coordinator and the worker start, so their first connections are sampled too
    try:
        with running_system(tmp_path, monkeypatch, capsys, allow_control=True) as system:
            _round_trip(system, capsys)
    finally:
        stop.set()
        sampler.join(scaled(5))
    assert any(table.startswith("tcp") and remote != "0.0.0.0" for table, _, remote in seen), f"the sampler saw no connections: {sorted(seen)}"
    assert _violations(sorted(seen)) == []


def _namespace_available() -> bool:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    return subprocess.run([bwrap, "--unshare-net", "--dev-bind", "/", "/", "true"], capture_output=True, timeout=scaled(30)).returncode == 0


@pytest.mark.skipif(os.environ.get(INNER) != "1", reason="runs inside the network namespace started by the test below")
def test_inner_round_trip(tmp_path, monkeypatch, capsys):
    interfaces = [line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]]
    assert interfaces == ["lo"], f"not offline: {interfaces}"
    with pytest.raises(OSError) as refused:
        socket.create_connection(("192.0.2.1", 9), timeout=2)  # TEST-NET-1: nothing may route it
    assert refused.value.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}, refused.value
    with running_system(tmp_path, monkeypatch, capsys, allow_control=True) as system:
        _round_trip(system, capsys)


def test_round_trip_in_a_network_namespace_without_a_route():
    if os.environ.get(INNER) == "1":
        pytest.skip("already inside the namespace")
    if not _namespace_available():
        pytest.skip("no unprivileged network namespace (bwrap --unshare-net is unavailable on this host)")
    env = {**os.environ, INNER: "1", "BRIDGE_REQUIRE_COORDINATOR": "1", "PYTHONPATH": str(ROOT)}
    command = ["bwrap", "--unshare-net", "--dev-bind", "/", "/", sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
               "tests/test_distributed_offline.py::test_inner_round_trip"]
    proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=scaled(180))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "PASSED tests/test_distributed_offline.py::test_inner_round_trip" in proc.stdout, proc.stdout
