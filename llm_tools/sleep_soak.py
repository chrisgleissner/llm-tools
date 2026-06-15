"""llm-sleep-soak — repeatedly suspend and wake the machine to prove that the
sleep/resume path ralph-robin depends on is actually reliable on THIS hardware.

ralph-robin waits out provider rate-limit windows by suspending the whole
machine with an RTC wake. That is only safe if the box reliably comes back from
suspend; a single wedged resume (a classic NVIDIA/USB failure mode) leaves it
powered but dead. This tool exercises exactly the production suspend/wake path
(:func:`common.suspend_with_wake`) in a tight loop, measures the wake drift of
every cycle, scrapes the kernel log for resume errors, and -- crucially --
writes a durable per-cycle ledger so that if a cycle wedges the machine and it
is hard-reset, the failure is still visible on the next boot.

It is a real-hardware test: it genuinely suspends the machine, so it cannot run
in CI. The decision/accounting logic is unit-tested with the suspend primitive
stubbed; ``LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1`` runs the whole loop in simulation
without sleeping the host.

Portability: the suspend backend is feature-detected (systemd today). On a host
with no usable backend every cycle reports ``no-suspend`` instead of sleeping,
so the tool degrades cleanly rather than pretending.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from . import common

APP_NAME = "llm-sleep-soak"

USAGE = """Usage: llm-sleep-soak [options]
  Repeatedly suspend and wake the machine, verifying every cycle resumes on time.

Options:
  -n, --cycles N            Number of suspend/wake cycles (default 20).
  -p, --period SECONDS      Time asleep per cycle, e.g. 90s, 2m (default 120s).
  -g, --gap SECONDS         Awake time between cycles (default 15s).
  -l, --min-lead SECONDS    Minimum lead before arming a wake (default 30s).
  -W, --watchdog            Arm a hardware watchdog across each suspend.
      --json                Emit a machine-readable JSON summary.
  -h, --help                Show this help.

Exit status is 0 only when every cycle resumed reliably and no earlier cycle was
left unfinished by a prior wedged resume.
"""


def parse_seconds(text: str, default: int) -> int | None:
    s = text.strip().lower()
    if not s:
        return default
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    mult = 1
    if s[-1] in units:
        mult = units[s[-1]]
        s = s[:-1]
    try:
        value = float(s)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value * mult)


@dataclass
class SoakConfig:
    cycles: int = 20
    period: int = 120
    gap: int = 15
    min_lead: int = 30
    watchdog: bool = False
    as_json: bool = False


def parse_args(argv: list[str]) -> SoakConfig:
    cfg = SoakConfig()
    i = 0

    def value(flag: str) -> str:
        nonlocal i
        if i + 1 >= len(argv):
            common.err(f"{flag} requires a value")
            raise SystemExit(2)
        i += 1
        return argv[i]

    def seconds(flag: str) -> int:
        parsed = parse_seconds(value(flag), -1)
        if parsed is None or parsed < 0:
            common.err(f"{flag} must be a non-negative duration like 90s, 2m, or seconds")
            raise SystemExit(2)
        return parsed

    while i < len(argv):
        arg = argv[i]
        if arg in ("-n", "--cycles"):
            raw = value(arg)
            if not raw.isdigit() or int(raw) <= 0:
                common.err("--cycles must be a positive integer")
                raise SystemExit(2)
            cfg.cycles = int(raw)
        elif arg in ("-p", "--period"):
            cfg.period = seconds(arg)
        elif arg in ("-g", "--gap"):
            cfg.gap = seconds(arg)
        elif arg in ("-l", "--min-lead"):
            cfg.min_lead = seconds(arg)
        elif arg == "--json":
            cfg.as_json = True
        elif arg in ("-W", "--watchdog"):
            cfg.watchdog = True
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        else:
            common.err(f"unknown option: {arg}")
            print(USAGE, end="", file=sys.stderr)
            raise SystemExit(2)
        i += 1
    return cfg


_RESUME_ERROR_PATTERNS = (
    "error in resume",
    "failed to resume",
    "resume failed",
    "pm: resume devices",
    "hung_task",
    "watchdog: bug",
    "gpu has fallen off the bus",
    "xhc error in resume",
)


def scrape_resume_errors(since_epoch: int, env: dict[str, str] | None = None) -> list[str]:
    """Best-effort kernel-log resume errors since ``since_epoch`` (Linux/journald).

    Purely advisory: a host without ``journalctl`` simply reports nothing.
    """
    env = env or os.environ
    if not common.have_cmd("journalctl"):
        return []
    try:
        import subprocess

        proc = subprocess.run(
            ["journalctl", "-k", "-b", "0", "--since", f"@{int(since_epoch)}", "--no-pager", "-o", "cat"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False, timeout=15,
        )
    except Exception:
        return []
    hits: list[str] = []
    for line in proc.stdout.splitlines():
        low = line.lower()
        if any(pat in low for pat in _RESUME_ERROR_PATTERNS):
            hits.append(line.strip())
    return hits[:10]


@dataclass
class CycleResult:
    index: int
    target_epoch: int
    suspended: bool
    woke_epoch: int | None
    drift_seconds: int | None
    reliable: bool
    reason: str
    resume_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # A simulated/awake cycle is "ok" (nothing to fail); a real cycle is ok
        # only when it resumed within tolerance and logged no resume errors.
        if not self.suspended:
            return self.reason in ("simulated",)
        return self.reliable and not self.resume_errors


def run_cycle(cfg: SoakConfig, logs: common.RunLogs, index: int, env: dict[str, str]) -> CycleResult:
    target = common.now_epoch(env) + cfg.period
    started = common.now_epoch(env)
    outcome = common.suspend_with_wake(
        target,
        who="llm-sleep-soak",
        logs=logs,
        min_lead=cfg.min_lead,
        watchdog=cfg.watchdog,
        cycle_id=f"soak-{index}-{int(time.time() * 1000)}",
        env=env,
    )
    errors = scrape_resume_errors(started, env) if outcome.suspended else []
    return CycleResult(
        index=index,
        target_epoch=target,
        suspended=outcome.suspended,
        woke_epoch=outcome.woke_epoch,
        drift_seconds=outcome.drift_seconds,
        reliable=outcome.reliable,
        reason=outcome.reason,
        resume_errors=errors,
    )


def summarize(results: list[CycleResult], prior_incomplete: int) -> dict[str, Any]:
    suspended = [r for r in results if r.suspended]
    unreliable = [r for r in results if r.suspended and not r.reliable]
    with_errors = [r for r in results if r.resume_errors]
    drifts = [r.drift_seconds for r in suspended if r.drift_seconds is not None]
    return {
        "cycles": len(results),
        "suspended": len(suspended),
        "ok": sum(1 for r in results if r.ok),
        "unreliable": len(unreliable),
        "with_resume_errors": len(with_errors),
        "prior_incomplete": prior_incomplete,
        "max_abs_drift": max((abs(d) for d in drifts), default=0),
        "all_reliable": not unreliable and not with_errors and prior_incomplete == 0,
    }


def main(argv: list[str] | None = None) -> int:
    common.migrate_legacy_cache_dirs()
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    env = os.environ
    logs = common.setup_run_logs(common.soak_log_dir(env), "sleep-soak")
    common.log_event(logs, "start", {"cycles": cfg.cycles, "period": cfg.period, "gap": cfg.gap, "min_lead": cfg.min_lead, "watchdog": cfg.watchdog, "backend": common.power_backend(env)})

    prior = common.incomplete_suspend_cycles(common.suspend_ledger_path(env))
    if prior:
        print(f"warning: {len(prior)} earlier suspend cycle(s) never recorded a resume — a prior wake may have wedged the machine.", file=sys.stderr)
        common.log_event(logs, "prior_incomplete", {"count": len(prior)})

    if common.power_backend(env) == "none" and env.get("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "0") != "1":
        print("error: no usable suspend backend (need systemd: systemctl + systemd-run). Nothing to soak.", file=sys.stderr)
        common.log_event(logs, "final", {"status": "no-backend"})
        return 2

    # Hold an idle inhibitor so the desktop's own auto-suspend cannot interleave
    # with the soak's deliberate cycles.
    inhibitor = common.IdleSuspendInhibitor("llm-sleep-soak", "sleep/wake soak test in progress", env)
    inhibitor.acquire()

    results: list[CycleResult] = []
    try:
        for index in range(1, cfg.cycles + 1):
            print(f"cycle {index}/{cfg.cycles}: suspending for {cfg.period}s ...", file=sys.stderr)
            result = run_cycle(cfg, logs, index, env)
            results.append(result)
            common.log_event(logs, "cycle", {
                "index": index, "suspended": result.suspended, "drift_seconds": result.drift_seconds,
                "reliable": result.reliable, "reason": result.reason, "resume_errors": result.resume_errors,
            })
            print(_format_cycle(result), file=sys.stderr)
            if not result.suspended and result.reason in ("missing-backend", "arm-failed"):
                print(f"  aborting soak: cannot suspend ({result.reason})", file=sys.stderr)
                break
            if index < cfg.cycles and cfg.gap:
                time.sleep(cfg.gap)
    finally:
        inhibitor.release()

    summary = summarize(results, len(prior))
    common.log_event(logs, "final", {"status": "ok" if summary["all_reliable"] else "unreliable", **summary})
    if cfg.as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(_format_summary(summary, logs))
    return 0 if summary["all_reliable"] else 1


def _format_cycle(result: CycleResult) -> str:
    if not result.suspended:
        return f"  cycle {result.index}: did not suspend ({result.reason})"
    drift = f"{result.drift_seconds:+d}s" if result.drift_seconds is not None else "?"
    verdict = "OK" if result.ok else "UNRELIABLE"
    extra = f" — {len(result.resume_errors)} resume error(s)" if result.resume_errors else ""
    return f"  cycle {result.index}: woke {drift} from target [{verdict}]{extra}"


def _format_summary(summary: dict[str, Any], logs: common.RunLogs) -> str:
    verdict = "PASS" if summary["all_reliable"] else "FAIL"
    return (
        f"sleep-soak {verdict}: {summary['ok']}/{summary['cycles']} cycles ok, "
        f"{summary['unreliable']} unreliable, {summary['with_resume_errors']} with resume errors, "
        f"max drift {summary['max_abs_drift']}s, prior-incomplete {summary['prior_incomplete']}\n"
        f"logs: {logs.run_dir}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
