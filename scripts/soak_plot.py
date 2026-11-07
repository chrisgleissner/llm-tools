#!/usr/bin/env python3
"""Draw resident memory over time as a stand-alone SVG (standard library only).

Inputs are the per-process CSV written by ``scripts/soak_sampler.py`` and, optionally, the JSON report of
``scripts/bridge_soak.py``. The figure has up to three panels: mission controllers (one thin line per process,
the mean as a thick line), workers (mean per process) and the coordinator. A shaded band marks the warm-up
window that the soak check ignores.

    python scripts/soak_plot.py --csv rss.csv --report soak-report.json --warmup-seconds 900 --out plot.svg
    python scripts/soak_plot.py --csv rss.csv --group single=55993b89 --group idle=0a1b2c3d --out plot.svg
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

WIDTH, PANEL_H, LEFT, RIGHT, TOP, GAP = 960, 250, 70, 150, 36, 46
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b"]


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="") as handle:
        return [{"t": float(r["t_s"]), "kind": r["kind"], "pid": r["pid"], "mission": r["mission"], "rss": float(r["rss_kib"])} for r in csv.DictReader(handle)]


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    span = max(high - low, 1e-9)
    raw = span / count
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = min((m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw), default=raw)
    first = int(low // step) * step
    ticks, value = [], first
    while value <= high + step:
        if value >= low - 1e-9:
            ticks.append(value)
        value += step
    return ticks


class Panel:
    def __init__(self, title: str, y: float, x_max: float, unit_label: str) -> None:
        self.title, self.y, self.x_max, self.unit_label = title, y, x_max, unit_label
        self.lines: list[tuple[list[tuple[float, float]], str, float, str]] = []  # points, colour, width, label
        self.bands: list[tuple[float, float, str]] = []

    def add(self, points: list[tuple[float, float]], colour: str, width: float, label: str = "") -> None:
        if points:
            self.lines.append((points, colour, width, label))

    def render(self) -> str:
        values = [v for pts, *_ in self.lines for _, v in pts]
        lo, hi = min(values), max(values)
        pad = (hi - lo) * 0.08 or 1.0
        lo, hi = lo - pad, hi + pad
        ticks = nice_ticks(lo, hi)
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
        plot_w = WIDTH - LEFT - RIGHT

        def px(t: float) -> float:
            return LEFT + plot_w * t / self.x_max

        def py(v: float) -> float:
            return self.y + PANEL_H - PANEL_H * (v - lo) / (hi - lo)

        out = [f'<text x="{LEFT}" y="{self.y - 12}" class="title">{self.title}</text>']
        for start, end, label in self.bands:
            out.append(f'<rect x="{px(start):.1f}" y="{self.y}" width="{px(end) - px(start):.1f}" height="{PANEL_H}" class="band"/>')
            out.append(f'<text x="{px(start) + 4:.1f}" y="{self.y + 14}" class="small">{label}</text>')
        for tick in ticks:
            out.append(f'<line x1="{LEFT}" x2="{LEFT + plot_w}" y1="{py(tick):.1f}" y2="{py(tick):.1f}" class="grid"/>')
            out.append(f'<text x="{LEFT - 6}" y="{py(tick) + 4:.1f}" class="axis" text-anchor="end">{tick:g}</text>')
        minutes = int(self.x_max // 60)
        step = max(1, minutes // 8)
        for minute in range(0, minutes + 1, step):
            out.append(f'<line x1="{px(minute * 60):.1f}" x2="{px(minute * 60):.1f}" y1="{self.y + PANEL_H}" y2="{self.y + PANEL_H + 4}" class="tick"/>')
            out.append(f'<text x="{px(minute * 60):.1f}" y="{self.y + PANEL_H + 16}" class="axis" text-anchor="middle">{minute}</text>')
        out.append(f'<text x="{LEFT + plot_w / 2}" y="{self.y + PANEL_H + 30}" class="axis" text-anchor="middle">minutes</text>')
        out.append(f'<text x="14" y="{self.y + PANEL_H / 2}" class="axis" transform="rotate(-90 14 {self.y + PANEL_H / 2})" text-anchor="middle">{self.unit_label}</text>')
        out.append(f'<rect x="{LEFT}" y="{self.y}" width="{plot_w}" height="{PANEL_H}" class="frame"/>')
        legend_y = self.y + 14
        for points, colour, width, label in self.lines:
            path = " ".join(f"{'M' if i == 0 else 'L'}{px(t):.1f},{py(v):.1f}" for i, (t, v) in enumerate(points))
            opacity = 0.35 if width < 1 else 1
            out.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="{width}" stroke-opacity="{opacity}" stroke-linejoin="round"/>')
            if label:
                out.append(f'<line x1="{LEFT + plot_w + 12}" x2="{LEFT + plot_w + 32}" y1="{legend_y - 4}" y2="{legend_y - 4}" stroke="{colour}" stroke-width="{max(width, 2)}"/>')
                out.append(f'<text x="{LEFT + plot_w + 38}" y="{legend_y}" class="small">{label}</text>')
                legend_y += 16
        return "\n".join(out)


def mean_series(series: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    buckets: dict[float, list[float]] = defaultdict(list)
    for points in series:
        for t, v in points:
            buckets[round(t / 30) * 30].append(v)
    return [(t, statistics.mean(v)) for t, v in sorted(buckets.items()) if len(v) >= max(1, len(series) // 2)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="bridge_soak.py JSON report (workers and coordinator panels)")
    parser.add_argument("--group", action="append", default=[], metavar="NAME=MISSION8[,MISSION8]", help="controllers drawn as their own coloured line (by 8-character mission id)")
    parser.add_argument("--soak-offset", type=float, default=0.0, help="seconds the soak started after the CSV began (the CSV clock is the x axis for controllers)")
    parser.add_argument("--warmup-seconds", type=float, default=0.0, help="shade this many seconds after the start of each series")
    parser.add_argument("--title", default="Resident memory over time")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = load_csv(args.csv)
    groups = {name: set(ids.split(",")) for name, ids in (item.split("=", 1) for item in args.group)}
    grouped_missions = set().union(*groups.values()) if groups else set()
    per_pid: dict[str, list[tuple[float, float]]] = defaultdict(list)
    mission_of: dict[str, str] = {}
    for row in rows:
        if row["kind"] == "controller":
            per_pid[row["pid"]].append((row["t"], row["rss"] / 1024.0))
            mission_of[row["pid"]] = row["mission"]
    soak = [pts for pid, pts in per_pid.items() if mission_of[pid] not in grouped_missions]
    t0 = min((pts[0][0] for pts in soak), default=0.0) if soak else 0.0
    t_end = max((t for pts in per_pid.values() for t, _ in pts), default=1.0)
    panels: list[Panel] = []

    controllers = Panel(f"Mission controllers: resident memory per process ({len(soak)} soak controllers)", TOP, t_end, "MiB")
    for pts in soak:
        controllers.add(pts, "#7f7f7f", 0.6)
    if soak:
        controllers.add(mean_series(soak), "#000000", 2.2, "soak mean")
    for index, (name, ids) in enumerate(groups.items()):
        series = [pts for pid, pts in per_pid.items() if mission_of[pid] in ids]
        for pts in series:
            controllers.add(pts, PALETTE[index % len(PALETTE)], 1.8, name)
    if args.warmup_seconds:
        controllers.bands.append((t0, t0 + args.warmup_seconds, "warm-up (ignored by the check)"))
    panels.append(controllers)

    if args.report:
        report = json.loads(args.report.read_text())
        samples = report["samples"]
        offset = t0 if soak else 0.0
        workers = Panel("Workers: resident memory, mean per process", TOP + (PANEL_H + GAP) * 1, t_end, "MiB")
        count = max(1, int(report["config"].get("workers", 10)))
        workers.add([(s["t"] + offset, s["workers"]["rss_kib"] / count / 1024.0) for s in samples], "#1f77b4", 2, "worker mean")
        coordinator = Panel("Coordinator: resident memory", TOP + (PANEL_H + GAP) * 2, t_end, "MiB")
        coordinator.add([(s["t"] + offset, s["coordinator"]["rss_kib"] / 1024.0) for s in samples], "#d62728", 2, "coordinator")
        for panel in (workers, coordinator):
            if args.warmup_seconds:
                panel.bands.append((offset, offset + args.warmup_seconds, "warm-up (ignored by the check)"))
            panels.append(panel)

    height = TOP + (PANEL_H + GAP) * len(panels)
    style = ("text{font-family:system-ui,Segoe UI,Helvetica,Arial,sans-serif;fill:#222}.title{font-size:13px;font-weight:600}"
             ".axis{font-size:11px}.small{font-size:11px}.grid{stroke:#ddd;stroke-width:1}.tick{stroke:#444}"
             ".frame{fill:none;stroke:#444}.band{fill:#f0c040;fill-opacity:.18}")
    body = "\n".join(panel.render() for panel in panels)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" '
           f'aria-label="{args.title}"><title>{args.title}</title><style>{style}</style><rect width="100%" height="100%" fill="#fff"/>\n{body}\n</svg>\n')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
