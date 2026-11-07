#!/usr/bin/env python3
"""Fail clearly when a ZAP scan did not produce a report or found a medium+ alert."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


REPORTS = ("zap-api-report.json", "zap-console-report.json")


def accepted_terminal_style_alert(report_name: str, alert: dict[str, object]) -> bool:
    """Allow only the reviewed xterm.js inline-style CSP alert, with its exact evidence."""
    if (
        report_name != "zap-console-report.json"
        or str(alert.get("pluginid")) != "10055"
        or alert.get("alert") != "CSP: style-src unsafe-inline"
    ):
        return False
    instances = alert.get("instances")
    if not isinstance(instances, list) or not instances:
        return False
    for instance in instances:
        if not isinstance(instance, dict):
            return False
        evidence = str(instance.get("evidence", ""))
        if (
            instance.get("param") != "content-security-policy"
            or urlsplit(str(instance.get("uri", ""))).path not in {"/", "/robots.txt", "/sitemap.xml"}
            or "script-src 'self'" not in evidence
            or "style-src 'self' 'unsafe-inline'" not in evidence
            or "default-src 'self'" not in evidence
        ):
            return False
    return True


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_zap_reports.py REPORT_DIR")
    report_dir = Path(sys.argv[1])
    paths = [report_dir / name for name in REPORTS]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing ZAP JSON report(s): " + ", ".join(missing))

    findings: list[tuple[str, str, str, str]] = []
    accepted = 0
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        for site in report.get("site", []):
            for alert in site.get("alerts", []):
                risk = int(alert.get("riskcode", 0))
                if risk >= 2:
                    if accepted_terminal_style_alert(path.name, alert):
                        accepted += 1
                        continue
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
    if accepted:
        print(f"Accepted {accepted} documented xterm.js inline-style CSP alert group(s)")
    print("No ZAP alerts at medium severity or above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
