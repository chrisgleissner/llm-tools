#!/usr/bin/env python3
"""Fail clearly when a ZAP scan did not produce a report or found a medium+ alert."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPORTS = ("zap-api-report.json", "zap-console-report.json")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_zap_reports.py REPORT_DIR")
    report_dir = Path(sys.argv[1])
    paths = [report_dir / name for name in REPORTS]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing ZAP JSON report(s): " + ", ".join(missing))

    findings: list[tuple[str, str, str, str]] = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        for site in report.get("site", []):
            for alert in site.get("alerts", []):
                risk = int(alert.get("riskcode", 0))
                if risk >= 2:
                    findings.append(
                        (
                            path.name,
                            alert.get("riskdesc", str(risk)),
                            alert.get("alert", "unknown"),
                            alert.get("url", ""),
                        )
                    )
    for finding in findings:
        print("medium-or-higher ZAP alert:", " | ".join(finding))
    if findings:
        raise SystemExit(f"{len(findings)} ZAP alert(s) at medium severity or above")
    print("No ZAP alerts at medium severity or above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
