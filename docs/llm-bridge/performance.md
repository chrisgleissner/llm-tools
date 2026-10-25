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
