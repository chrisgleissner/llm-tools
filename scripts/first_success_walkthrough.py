#!/usr/bin/env python3
"""Timed first-success walkthrough against a throw-away coordinator and fake provider.

Use --coordinator-bin PATH (or BRIDGE_COORDINATOR_BIN) for a local coordinator,
--coordinator-image NAME for an already-present Docker image, and optionally
--wheel PATH to install llm-bridge from one wheel into a private virtualenv.
--coordinator-only checks init, serve, token, health and the API description without a
worker. Full walkthroughs run on Linux and macOS; Windows uses --coordinator-only
because workers are not supported there (DECISIONS.md D8).

    python scripts/first_success_walkthrough.py [--coordinator-bin PATH | --coordinator-image NAME]
        [--wheel PATH] [--coordinator-only] [--budget SECONDS] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dist_harness as h  # noqa: E402

DEFAULT_BUDGET_SECONDS = 300.0


def wheel_file(path: str | Path) -> Path:
    """Resolve a wheel file or a directory containing exactly one wheel."""
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        wheels = sorted(candidate.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"expected exactly one .whl in {candidate}, found {len(wheels)}")
        return wheels[0].resolve()
    if candidate.is_file() and candidate.suffix == ".whl":
        return candidate.resolve()
    raise ValueError(f"not a .whl file or wheel directory: {candidate}")


class ContainerCoordinator:
    """Coordinator lifecycle backed by a preloaded Docker image."""

    def __init__(self, image: str, base: Path) -> None:
        self.image = image
        self.base = base
        self.port = h.free_port()
        self.api_token = ""
        self.volume = f"lb-first-success-{os.getpid()}-{self.port}"
        self.name = f"lb-first-success-{os.getpid()}-{self.port}"
        self._api: h.ApiClient | None = None
        self._ca_file: Path | None = None

    def _docker(self, *args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], check=True, capture_output=capture, text=True, timeout=60)

    def init(self) -> None:
        self._docker("volume", "create", self.volume)
        self._docker("run", "--rm", "-v", f"{self.volume}:/data", self.image, "init", "--hostname", "localhost")

    def start(self) -> None:
        self._docker("run", "-d", "--name", self.name, "-p", f"127.0.0.1:{self.port}:8443",
                     "-e", f"BRIDGE_COORDINATOR_PUBLIC_PORT={self.port}", "-e",
                     "BRIDGE_COORDINATOR_PUBLIC_NAMES=localhost", "-v", f"{self.volume}:/data", self.image)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.api.healthy():
                return
            time.sleep(0.2)
        logs = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True, timeout=30)
        tail = "\n".join((logs.stdout + logs.stderr).splitlines()[-20:])
        raise RuntimeError(f"container coordinator did not become healthy within 60s; last logs:\n{tail}")

    def run(self, *args: str) -> str:
        return self._docker("exec", self.name, "/usr/local/bin/bridge-coordinator", *args).stdout.strip()

    def add_worker(self, name: str) -> str:
        return self.run("worker", "add", name)

    @property
    def ca_file(self) -> Path:
        if self._ca_file is None:
            target = self.base / "ca.pem"
            self._docker("cp", f"{self.name}:/data/ca.pem", str(target))
            self._ca_file = target
        return self._ca_file

    @property
    def api(self) -> h.ApiClient:
        if self._api is None or self._api.token != (self.api_token or "none"):
            self._api = h.ApiClient(self.port, self.ca_file, self.api_token or "none")
        return self._api

    def stop(self) -> None:
        for args in (("rm", "-f", self.name), ("volume", "rm", "-f", self.volume)):
            try:
                subprocess.run(["docker", *args], check=False, capture_output=True, text=True, timeout=30)
            except Exception:
                pass


class Walkthrough:
    def __init__(self, binary: Path | None, base: Path, budget: float, *, image: str | None = None,
                 wheel: Path | None = None, coordinator_only: bool = False) -> None:
        self.binary = binary
        self.base = base
        self.budget = budget
        self.image = image
        self.wheel = wheel
        self.coordinator_only = coordinator_only
        self.steps: list[dict[str, Any]] = []
        self.coordinator = ContainerCoordinator(image, base / "coordinator") if image else h.Coordinator(binary, base / "coordinator")
        bridge_command: list[str] | None = None
        if wheel is not None:
            scripts = base / "venv" / ("Scripts" if os.name == "nt" else "bin")
            bridge_command = [str(scripts / ("llm-bridge.exe" if os.name == "nt" else "llm-bridge"))]
        self.node = h.WorkerNode("first-success", base / "worker", speed=0.0,
                                 mix=h.ProviderMix(output=1, question=0, ratelimit=0, crash=0), bridge_command=bridge_command)
        self.mission_id = ""

    def step(self, name: str, action: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            result = action()
        except Exception:
            self.steps.append({"step": name, "seconds": time.monotonic() - started, "ok": False})
            raise
        self.steps.append({"step": name, "seconds": time.monotonic() - started, "ok": True})
        return result

    def wait(self, what: str, predicate: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise RuntimeError(f"timed out after {timeout:g}s waiting for {what}")

    def worker_online(self) -> bool:
        return any(item.get("name") == self.node.name and item.get("state") == "online" for item in self.coordinator.api.workers())

    def mission_events(self) -> bool:
        return any(str(item.get("mission_id") or item.get("mission") or "") == self.mission_id or self.mission_id in json.dumps(item)
                   for item in self.coordinator.api.events_after(self.node.name, 0))

    def install_wheel(self) -> None:
        assert self.wheel is not None
        env_dir = self.base / "venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check", str(self.wheel)],
                       check=True, capture_output=True, text=True, timeout=300)

    def run(self) -> dict[str, Any]:
        coordinator, node = self.coordinator, self.node
        error = ""
        try:
            if self.image:
                self.step("image", lambda: subprocess.run(["docker", "image", "inspect", self.image], check=True,
                                                            capture_output=True, text=True, timeout=30))
            if self.wheel:
                self.step("install-wheel", self.install_wheel)
            self.step("init", coordinator.init)
            self.step("serve", coordinator.start)
            coordinator.api_token = self.step("token", lambda: coordinator.run("token", "create", "first-success", "--expires-days", "1", "--scope", "admin"))
            if self.coordinator_only:
                self.step("health", lambda: self._health(coordinator))
                self.step("api", lambda: self._api_description(coordinator))
            else:
                self.step("enroll", lambda: node.enroll(coordinator))
                self.step("connect", lambda: (node.start(), self.wait("the worker to connect", self.worker_online, 60)))

                def mission() -> None:
                    self.mission_id = node.create_mission("first-success", prompt="first success", run_provider=True)
                    node.start_mission(self.mission_id)

                self.step("mission", mission)
                self.step("visible", lambda: self.wait("mission events in the coordinator", self.mission_events, 120))
        except Exception as exc:  # noqa: BLE001 - the report names the failed step
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.cleanup()
        total = sum(item["seconds"] for item in self.steps)
        ok = not error and total <= self.budget
        if not error and not ok:
            error = f"total {total:.1f}s exceeds the {self.budget:g}s budget"
        return {"ok": ok, "total_seconds": round(total, 2), "budget_seconds": self.budget, "error": error,
                "mode": "coordinator-only" if self.coordinator_only else "full",
                "steps": [{**item, "seconds": round(item["seconds"], 2)} for item in self.steps]}

    @staticmethod
    def _health(coordinator: Any) -> None:
        coordinator.run("health")

    @staticmethod
    def _api_description(coordinator: Any) -> None:
        status, _ = coordinator.api.request("GET", "/api/v1/openapi.json")
        if status != 200:
            raise RuntimeError(f"coordinator API description returned HTTP {status}, expected 200")

    def cleanup(self) -> None:
        for action in (self.node.stop_missions, self.node.stop, self.coordinator.stop):
            try:
                action()
            except Exception:  # noqa: BLE001 - best effort
                pass


def render(report: dict[str, Any]) -> str:
    lines = [f"{item['step']:<14}{item['seconds']:>8.2f}s  {'ok' if item['ok'] else 'FAILED'}" for item in report["steps"]]
    lines.append(f"{'total':<14}{report['total_seconds']:>8.2f}s (budget {report['budget_seconds']:g}s)")
    lines.append("PASS" if report["ok"] else f"FAIL: {report['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coordinator-bin", help="path of bridge-coordinator (default: BRIDGE_COORDINATOR_BIN, then coordinator/target)")
    parser.add_argument("--coordinator-image", help="preloaded Docker image containing bridge-coordinator")
    parser.add_argument("--wheel", type=wheel_file, help="llm-tools wheel file or directory containing exactly one .whl")
    parser.add_argument("--coordinator-only", action="store_true", help="check the coordinator without starting a worker")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS, help="maximum total seconds (default 300)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    binary = None if args.coordinator_image else h.find_coordinator_binary(args.coordinator_bin)
    if binary is None and not args.coordinator_image:
        print("error: no bridge-coordinator binary found; build it with `cargo build --release` in coordinator/ or pass --coordinator-bin (or use --coordinator-image)", file=sys.stderr)
        return 2
    base = h.short_temp_dir("lb-first-success-")
    try:
        report = Walkthrough(binary, base, args.budget, image=args.coordinator_image, wheel=args.wheel,
                             coordinator_only=args.coordinator_only).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
