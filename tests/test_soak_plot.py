"""The memory plot and sampler of the soak harness (scripts/soak_plot.py, scripts/soak_sampler.py)."""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import soak_plot  # noqa: E402
import soak_sampler  # noqa: E402


def _csv(path: Path) -> None:
    lines = ["t_s,kind,pid,mission,rss_kib,anon_kib,threads,fds"]
    for index in range(20):
        for pid, mission in ((101, "aaaaaaaa"), (102, "bbbbbbbb"), (103, "cccccccc")):
            lines.append(f"{index * 30},controller,{pid},{mission},{36000 + index * 50 + pid},20000,3,15")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_plot_is_well_formed_svg_with_a_warmup_band_and_a_named_group(tmp_path) -> None:
    csv_path, out = tmp_path / "rss.csv", tmp_path / "plot.svg"
    _csv(csv_path)
    report = tmp_path / "report.json"
    samples = [{"t": index * 30.0, "workers": {"rss_kib": 300_000 + index}, "coordinator": {"rss_kib": 20_000 + index}} for index in range(20)]
    report.write_text(json.dumps({"samples": samples, "config": {"workers": 10}}), encoding="utf-8")
    assert soak_plot.main(["--csv", str(csv_path), "--report", str(report), "--group", "single=cccccccc", "--warmup-seconds", "200", "--out", str(out)]) == 0
    root = ET.fromstring(out.read_text(encoding="utf-8"))
    text = " ".join(root.itertext())
    assert "warm-up (ignored by the check)" in text and "single" in text and "soak mean" in text
    assert text.count("Mission controllers") == 1 and "Workers" in text and "Coordinator" in text


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="the soak sampler reads process state from /proc (Linux only)")
def test_sampler_finds_no_processes_for_an_unused_runtime_prefix_and_reads_stats_for_this_one() -> None:
    assert soak_sampler.sample_once(("/nonexistent-prefix-",), 0.0) == []
    rss, anon, threads, fds = soak_sampler.process_stats(os.getpid())
    assert rss > 0 and threads >= 1 and fds >= 3
    assert soak_sampler.kind_of(b"python\0-m\0llm_tools.bridge\0_controller\0") == "controller"
    assert soak_sampler.kind_of(b"python\0llm-bridge\0worker\0run\0") == "worker"
    assert soak_sampler.kind_of(b"bash\0") is None
