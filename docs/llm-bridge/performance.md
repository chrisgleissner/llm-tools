# Performance evidence

Measured locally on 2026-10-25 using
`python scripts/bridge_store_benchmark.py --tasks 10000`, against persistence
checkpoint `219b5f0`. No provider calls. Raw results are in
`store-benchmark-results.json`.

| Measurement | Observed |
| --- | --- |
| 10,000 durable task submissions | 4.062488 s wall time |
| List 10,000 tasks | 0.097032 s |
| Commit one pause command after those tasks | 0.000373 s |
| CPU for complete benchmark | 4.176313 s |
| Peak process RSS (Linux ru_maxrss) | 35,644 KiB |
| SQLite files including WAL while open | 8,048,264 bytes |
| Database after connection close | 3,887,104 bytes |
| SQLite integrity check | ok |

This is a store measurement, not a TUI responsiveness, streaming throughput,
long-duration memory, or multi-day reliability result. Those acceptance checks
remain unverified until separately recorded. The architecture's provisional
input latency target is 250 ms while a fake provider streams 1 MiB/s. Do not
claim this target is met from database timing alone.

## Integrated streaming and iteration measurements

The actual curses command pane under a 10,000-task queue committed eight
operator changes in 24.9–29.9 ms while the fake provider emitted 3,260,416 bytes
in 3.146 s. This measured candidate met the local 250 ms target; the CI guard is
750 ms to allow shared-runner variability. See `stream-stress-results.json` and
`tests/test_bridge_stress.py`. A rerun after switching REPL dispatch to the
shared CLI parser passed in 7.82 s; final frozen-head measurements are required.

A deterministic 1,000-task/1,009-provider-attempt simulation completed in
24.01 s, with 360,410 bytes retained tracemalloc growth, a 1,597,349-byte traced
peak and a 3,944,448-byte database. Its file-descriptor guard passed. See
`long-run-results.json` and `tests/test_bridge_long_run.py`. These are measured
stress/simulation results, not proof of weeks of unattended real-model work.
