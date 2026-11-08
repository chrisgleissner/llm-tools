"""One headless mission with the fake provider and no coordinator: sample the controller's resident memory.

The cheap form of the PRD-007 soak for the mission controller (see docs/distributed/soak-and-chaos.md). It creates a
private state directory, queues N tasks, starts the mission and prints the RSS of its controller every SAMPLE seconds
(default 2) for SECONDS seconds, then the least-squares slope after the first quarter. Everything lives in a new
temporary directory; nothing of the operator's Bridge state is read or written.

    python scripts/controller_rss_repro.py . 3000 120                        # instant tasks: growth per task
    SAMPLE=30 LIMIT_EVERY=240 python scripts/controller_rss_repro.py . 600 5400 \
        SOAK_PROVIDER_MIX=80,0,10,10 SOAK_PROVIDER_SPEED=0.1 LLM_TOOLS_LOCAL_BLOCK_BACKOFF=5   # a task about every 20 s

The first argument is the repository root; trailing KEY=VALUE arguments set environment variables of the mission
(the ``SOAK_PROVIDER_*`` variables of ``scripts/bridge_soak_provider.py``). LIMIT_EVERY injects a 20 s capacity limit
that often.
"""
import json, os, subprocess, sys, tempfile, time, shlex, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
wt = Path(sys.argv[1]); N = int(sys.argv[2]); secs = float(sys.argv[3])
extra = dict(a.split("=", 1) for a in sys.argv[4:])
base = Path(tempfile.mkdtemp(prefix="rssrepro-"))
rt = Path(tempfile.mkdtemp(prefix="lbr-")); rt.chmod(0o700)
state = base / "state"; state.mkdir(); state.chmod(0o700)
(base / "home").mkdir(); (base / "cfg/llm-tools").mkdir(parents=True)
(base / "cfg/llm-tools/config.toml").write_text('[ralph]\nretry_delays = "2,4"\n')
usage = base / "usage.json"
def usage_json(limited=False):
    n = int(time.time())
    win = {"remaining": 0, "resets_at": n + 20} if limited else {"remaining": 80, "resets_at": n + 18000}
    return json.dumps({"codex": {"available": True, "five_hour": win, "week": {"remaining": 60, "resets_at": n + 3 * 86400}}})
usage.write_text(usage_json())
env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "TERM": "dumb", "HOME": str(base / "home"), "XDG_CONFIG_HOME": str(base / "cfg"),
       "XDG_RUNTIME_DIR": str(rt), "XDG_STATE_HOME": str(base / "xs"), "XDG_CACHE_HOME": str(base / "cache"),
       "LLM_BRIDGE_STATE_DIR": str(state), "LLM_SCHEDULER_USAGE_JSON_FILE": str(usage), "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1",
       "LLM_TOOLS_NO_INHIBIT": "1", "LLM_USAGE_NO_SERVICE": "1", "LLM_BRIDGE_NO_SIGNIN_CHECK": "1", "LLM_BRIDGE_NO_OSC": "1",
       "SOAK_PROVIDER_SEED": "1", "SOAK_PROVIDER_MIX": "100,0,0,0", "SOAK_PROVIDER_SPEED": "0", "SOAK_PROVIDER_LINES": "4"}
env.update(extra)
def cli(*a):
    r = subprocess.run([sys.executable, str(wt / "llm-bridge"), *a], env=env, capture_output=True, text=True, cwd=base, timeout=120)
    if r.returncode: raise SystemExit(f"{a} failed: {r.stderr}{r.stdout}")
    return r.stdout
folder = base / "proj"; folder.mkdir()
prov = shlex.join([sys.executable, str(wt / "scripts/bridge_soak_provider.py")])
m = json.loads(cli("mission", "create", "--prompt", "soak", "--cwd", str(folder), "--providers", "codex", "--mode", "headless", "--new", "--json", "--verifier", "true", "--command-template", prov))["mission"]["id"]
with ThreadPoolExecutor(8) as ex: list(ex.map(lambda i: cli("task", "add", "--mission", m, "--prompt", f"task {i}"), range(N)))
print("added", N, flush=True)
cli("start", "--mission", m)
LIMIT_EVERY = float(os.environ.get("LIMIT_EVERY", "0"))
def limiter():
    while LIMIT_EVERY:
        time.sleep(LIMIT_EVERY); usage.write_text(usage_json(True)); time.sleep(20); usage.write_text(usage_json())
threading.Thread(target=limiter, daemon=True).start()
def pids():
    out = []
    for e in os.listdir("/proc"):
        if e.isdigit():
            try:
                if b"_controller" in Path(f"/proc/{e}/cmdline").read_bytes() and f"XDG_RUNTIME_DIR={rt}".encode() in Path(f"/proc/{e}/environ").read_bytes().split(b"\0"): out.append(int(e))
            except OSError: pass
    return out
def done():
    c = sqlite3.connect(f"file:{state/'bridge.sqlite3'}?mode=ro", uri=True)
    try: return c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('COMPLETED','REVIEW')").fetchone()[0]
    finally: c.close()
def rss(p):
    for l in Path(f"/proc/{p}/status").read_text().splitlines():
        if l.startswith("VmRSS"): return int(l.split()[1])
print("base", base, "mission", m, flush=True)
t0 = time.time(); rows = []
while time.time() - t0 < secs:
    time.sleep(float(os.environ.get("SAMPLE", "2")))
    ps = pids()
    if not ps: print("controller gone"); break
    d = done(); r = rss(ps[0]); rows.append((time.time() - t0, d, r)); print(f"{rows[-1][0]:6.0f}s tasks={d} rss={r} KiB", flush=True)
def slope(xs, ys):
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n; den = sum((x-mx)**2 for x in xs)
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys))/den if den else float("nan")
if len(rows) > 6:
    h = rows[len(rows)//4:]
    print("slope KiB/task after first quarter:", round(slope([x[1] for x in h], [x[2] for x in h]), 3), " KiB/min:", round(slope([x[0] for x in h], [x[2] for x in h])*60, 2))
cli("stop", "--mission", m)
if os.environ.get("KEEP") != "1":  # the controller has exited after "stop"; KEEP=1 leaves the directories for inspection
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    shutil.rmtree(rt, ignore_errors=True)
