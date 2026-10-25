# LLM Bridge verification matrix

Temporary HOME/XDG/config/runtime, injected capacity and fake providers isolate
CI from credentials/developer state. Install tmux explicitly in CI; terminal
tests cannot silently skip there. Live probes separate and bounded/opt-in.

| Contract / scenarios | Observable assertion |
| --- | --- |
| Mission create/CLI/JSON (A,D,S) | Verbatim multiline prompt, stable ids, second process sees state, ledger identifies unfinished work |
| State machine | Every allowed/rejected transition; event and state atomic; zero exit alone never COMPLETED |
| Queue/dependencies | Cycles/foreign mission refs rejected; deterministic priority/order ties; concurrent submissions retained; proposals not auto-scheduled |
| Steering (E) | Revision persisted while running, amendment delivered next boundary, old evidence cannot complete new revision |
| SQLite/crash (N) | Kill before/after commit/spawn; reopen sees committed old/new state; incomplete effect UNCERTAIN/BLOCKED, never silently repeated |
| Controller ownership | Competing controllers yield one owner; stale/reused PID not killed; worktree guard prevents implicit writers |
| Schema/private files | Migration rollback, newer schema rejection; DB/WAL/log directory permissions |
| Verification | Command timeout/failure/artifact digest and revision recorded; explicit approval evidence; bounded no-progress recovery |
| Capacity/handoff (F,G,Q) | Existing Ralph selection/model context/runtime blocks used; task evidence survives; legacy global rotation state unchanged |
| Modes/stations (B,O) | Fake process observes production argv/isatty; viewed != executing; interactive writer waits safe boundary; no screen-text inference |
| Native fidelity (B,C,O) | Private tmux + real PTY fake verifies cursor/ANSI/alternate screen/input/paste/SGR mouse/wheel/focus/prefix bytes at child |
| Resize/cleanup (O) | 80x24,120x40,large/dynamic; child dimensions/SIGWINCH correct; exit/crash restores terminal/mouse/termios |
| Stress | 1MiB/s fake stream and 10k tasks/events while input dispatch target <250ms; partial UTF-8/escape chunks; record actual CPU/RSS/fds |
| Detach/attach (H) | UI exits, same owned controller/native pane alive, reattach state consistent; observer quit cannot kill mission |
| Stop/resume (I) | Prevent new launch; checkpoint or honest STOPPING timeout; force uncertainty recorded before owned group termination |
| Reboot/reconstruct (J,L,M,N) | No prior tmux/provider process needed; retain tasks/config/evidence/station history and reconcile uncertainty |
| Native restore (K) | Captured verified id + documented argv; identity mismatch RECONSTRUCTED, no global last-session assumption |
| Machine sleep (G) | Existing fake power backend; stop/pause/resume never suspend; all-exhausted path owned by Ralph and commands interrupt waits |
| Long run (P) | Thousands iterations/handoffs/failures; stable operation ids, bounded resources/failure policy, actual storage growth reported |
| Real providers (R) | Per version/capability launch/render/input/mouse/resize/resume/cleanup; unavailable NOT VERIFIED; installation alone never pass |
| Legacy tools (Q) | Existing regression suite and branch coverage >=85%, existing config/JSON/default contracts preserved |

Mandatory review gates: persistence/lifecycle, native terminal transport,
orchestration/restoration, final integrated implementation. Reviewer records
severity/location/reproduction/impact/fix/verification; critical/high block
completion. Every provider starts NOT VERIFIED until observed runtime evidence
exists. No tests or benchmarks were executed during planning.
