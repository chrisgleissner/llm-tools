from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_zap_reports.py"


def test_missing_zap_report_fails_with_a_clear_path(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Missing ZAP JSON report(s)" in result.stderr
    assert str(tmp_path / "zap-api-report.json") in result.stderr


def test_zap_alert_gate_accepts_no_medium_or_higher_alerts(tmp_path: Path) -> None:
    clean = {"site": [{"alerts": [{"riskcode": "1", "riskdesc": "Low", "alert": "info"}]}]}
    for name in ("zap-api-report.json", "zap-console-report.json"):
        (tmp_path / name).write_text(json.dumps(clean), encoding="utf-8")
    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "No ZAP alerts at medium severity or above" in result.stdout


def test_zap_alert_gate_fails_on_medium_alerts(tmp_path: Path) -> None:
    clean = {"site": [{"alerts": []}]}
    medium = {"site": [{"alerts": [{"riskcode": "2", "riskdesc": "Medium", "alert": "issue"}]}]}
    (tmp_path / "zap-api-report.json").write_text(json.dumps(medium), encoding="utf-8")
    (tmp_path / "zap-console-report.json").write_text(json.dumps(clean), encoding="utf-8")
    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "medium-or-higher ZAP alert" in result.stdout


def test_zap_alert_gate_accepts_only_reviewed_xterm_style_csp_alert(tmp_path: Path) -> None:
    clean = {"site": [{"alerts": []}]}
    accepted = {
        "pluginid": "10055",
        "riskcode": "2",
        "riskdesc": "Medium (High)",
        "alert": "CSP: style-src unsafe-inline",
        "instances": [
            {
                "uri": "https://localhost/",
                "param": "content-security-policy",
                "evidence": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
            }
        ],
    }
    (tmp_path / "zap-api-report.json").write_text(json.dumps(clean), encoding="utf-8")
    (tmp_path / "zap-console-report.json").write_text(json.dumps({"site": [{"alerts": [accepted]}]}), encoding="utf-8")

    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Accepted 1 documented xterm.js inline-style CSP alert group(s)" in result.stdout


def test_zap_alert_gate_does_not_accept_inline_script_csp(tmp_path: Path) -> None:
    clean = {"site": [{"alerts": []}]}
    unsafe = {
        "pluginid": "10055",
        "riskcode": "2",
        "riskdesc": "Medium (High)",
        "alert": "CSP: style-src unsafe-inline",
        "instances": [
            {
                "uri": "https://localhost/",
                "param": "content-security-policy",
                "evidence": "default-src 'self'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
            }
        ],
    }
    (tmp_path / "zap-api-report.json").write_text(json.dumps(clean), encoding="utf-8")
    (tmp_path / "zap-console-report.json").write_text(json.dumps({"site": [{"alerts": [unsafe]}]}), encoding="utf-8")

    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "medium-or-higher ZAP alert" in result.stdout


def test_zap_alert_gate_does_not_accept_the_console_csp_alert_on_other_paths(tmp_path: Path) -> None:
    clean = {"site": [{"alerts": []}]}
    csp_alert = {
        "pluginid": "10055",
        "riskcode": "2",
        "riskdesc": "Medium (High)",
        "alert": "CSP: style-src unsafe-inline",
        "instances": [
            {
                "uri": "https://localhost/api/docs",
                "param": "content-security-policy",
                "evidence": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
            }
        ],
    }
    (tmp_path / "zap-api-report.json").write_text(json.dumps(clean), encoding="utf-8")
    (tmp_path / "zap-console-report.json").write_text(json.dumps({"site": [{"alerts": [csp_alert]}]}), encoding="utf-8")

    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "medium-or-higher ZAP alert" in result.stdout
