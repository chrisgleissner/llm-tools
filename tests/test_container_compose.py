"""Static checks of the coordinator compose files (docs/distributed/install.md, Low-write host).

The files are YAML, but the project does not depend on a YAML parser, so these are text checks
of the lines that carry the defaults. `docker compose config` was used to confirm the merged
result when the files were written; CI's container jobs start the default file.
"""

from __future__ import annotations

from pathlib import Path

CONTAINER = Path(__file__).resolve().parents[1] / "container"


def test_default_compose_keeps_rotated_json_logs_and_a_30_second_health_check() -> None:
    text = (CONTAINER / "compose.yaml").read_text(encoding="utf-8")
    assert "      driver: json-file\n" in text
    assert 'max-size: "1m"' in text and 'max-file: "3"' in text
    assert "interval: ${BRIDGE_HEALTHCHECK_INTERVAL:-30s}" in text
    assert 'test: ["CMD", "/usr/local/bin/bridge-coordinator", "health"]' in text
    dockerfile = (CONTAINER / "Dockerfile").read_text(encoding="utf-8")
    assert "--interval=30s --timeout=5s --start-period=20s --retries=3" in dockerfile
    assert "timeout: 5s" in text and "start_period: 20s" in text and "retries: 3" in text


def test_low_write_override_replaces_the_whole_logging_block_and_slows_health_checks() -> None:
    text = (CONTAINER / "compose.low-write.yaml").read_text(encoding="utf-8")
    # !override drops the json-file size options, which journald, syslog and none reject.
    assert "    logging: !override\n      driver: ${BRIDGE_LOG_DRIVER:-journald}\n" in text
    assert "interval: ${BRIDGE_HEALTHCHECK_INTERVAL:-5m}" in text
    assert "max-size" not in text.split("services:", 1)[1]
