"""Provider capability matrix, evidence files and CLI version drift (PROV-002, PROV-011, DOCS-003).

The matrix comes from the registry (``bridge_providers``) and the live-evidence files of PROV-010.

Evidence file ``llm_tools/provider_evidence/<provider>.json`` (schema version 1)::

    {"schema_version": 1, "provider": "kilo", "cli_version": "7.3.45", "date": "2026-11-12",
     "capabilities": {"launch": true, "reply": true, "stop": true, "exact_resume": true, "context_recall": true}}

A capability value is ``true`` (the probe observed it), ``false`` (the probe observed that it fails) or
absent (not probed). Status per provider and capability:

* ``unsupported``: the registry says Bridge has no implementation, or the evidence says it failed.
* ``supported``: implemented, and either observed in the evidence (probe capabilities) or covered by the
  registry's fixtures (the capabilities that need no live run).
* ``unverified``: implemented, but a probe capability has no evidence.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from . import bridge_providers

SCHEMA_VERSION = 1
VERSION_TIMEOUT_SECONDS = 2
EVIDENCE_DIR = Path(__file__).resolve().parent / "provider_evidence"  # packaged with llm_tools (pyproject package-data)
DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "llm-bridge" / "provider-capabilities.md"
PROBE_CAPABILITIES = ("launch", "reply", "stop", "exact_resume", "context_recall")
REGISTRY_CAPABILITIES = ("finish", "limit_notice", "question_detection", "task_notice", "prompt_outside_argv")
CAPABILITIES = PROBE_CAPABILITIES + REGISTRY_CAPABILITIES
SUPPORTED, UNSUPPORTED, UNVERIFIED = "supported", "unsupported", "unverified"
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def designed(spec: bridge_providers.ProviderSpec, capability: str) -> bool:
    """Whether Bridge implements ``capability`` for ``spec``, from the registry alone."""
    if capability in {"launch", "reply", "stop"}:
        return spec.native_supported
    if capability in {"exact_resume", "context_recall"}:
        return bool(spec.resume_syntax) and spec.native_supported
    if capability == "finish":
        return spec.exit_command is not None
    if capability == "limit_notice":
        return spec.limit_notice_pattern is not None
    if capability == "question_detection":
        return spec.question_detector != bridge_providers.QUESTION_NONE
    if capability == "task_notice":
        return spec.notice_delivery
    if capability == "prompt_outside_argv":
        return spec.prompt_channel != bridge_providers.PROMPT_ARGV
    raise KeyError(capability)


def load_evidence(provider: str, directory: Path | None = None) -> dict[str, Any] | None:
    """The evidence record of ``provider``, or ``None`` when the file is missing or does not follow the schema."""
    path = (directory or EVIDENCE_DIR) / f"{provider}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("provider") != provider:
        return None
    if not isinstance(data.get("cli_version"), str) or not isinstance(data.get("date"), str) or not isinstance(data.get("capabilities"), dict):
        return None
    return data


def status(spec: bridge_providers.ProviderSpec, capability: str, evidence: Mapping[str, Any] | None) -> str:
    if not designed(spec, capability):
        return UNSUPPORTED
    if capability not in PROBE_CAPABILITIES:
        return SUPPORTED
    observed = (evidence or {}).get("capabilities", {}).get(capability)
    if observed is True:
        return SUPPORTED
    return UNSUPPORTED if observed is False else UNVERIFIED


def parse_version(text: str) -> tuple[int, int] | None:
    match = _VERSION.search(text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def installed_version(provider: str, run: Callable[..., Any] = subprocess.run, *, path: str | None = None) -> str | None:
    """The provider CLI's version string from ``<cli> --version`` (read-only, bounded), or ``None``."""
    spec = bridge_providers.get(provider)
    if spec is None:
        return None
    resolved = shutil.which(spec.executable, path=path)
    if resolved is None:
        return None
    try:
        result = run([resolved, spec.version_flag], check=False, capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search((result.stdout or "") + " " + (result.stderr or ""))
    return match.group(0) if match else None


def version_drift(installed: str | None, evidence: Mapping[str, Any] | None) -> str | None:
    """A warning when the installed major.minor is newer than the evidence's, else ``None``."""
    have = parse_version(installed or "")
    seen = parse_version((evidence or {}).get("cli_version", ""))
    if have is None or seen is None or have <= seen:
        return None
    return (f"installed version {installed} is newer than the evidence version {evidence['cli_version']} "  # type: ignore[index]
            f"({evidence['date']}) · capabilities are unverified for it; run scripts/bridge_provider_probe.py")  # type: ignore[index]


def provider_version_record(provider: str) -> dict[str, str]:
    """The ``{"provider", "cli_version"}`` pair to store with a run (PROV-011); the version is ``unknown`` when unreadable."""
    return {"provider": provider, "cli_version": installed_version(provider) or "unknown"}


def capabilities_report(directory: Path | None = None, *, installed: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Per provider: capability statuses, evidence date and CLI version, installed version and drift warnings."""
    lookup = installed if installed is not None else installed_version
    providers = []
    for name, spec in bridge_providers.REGISTRY.items():
        evidence = load_evidence(name, directory)
        version = lookup(name)
        warnings = [w for w in (version_drift(version, evidence),) if w]
        providers.append({
            "provider": name,
            "capabilities": {cap: status(spec, cap, evidence) for cap in CAPABILITIES},
            "evidence": {"date": evidence["date"], "cli_version": evidence["cli_version"]} if evidence else None,
            "installed_version": version,
            "warnings": warnings,
        })
    return {"schema_version": SCHEMA_VERSION, "capabilities": list(CAPABILITIES), "providers": providers}


def format_capabilities(report: Mapping[str, Any]) -> str:
    columns = report["capabilities"]
    lines = ["Provider capabilities (supported / unsupported / unverified)", ""]
    for entry in report["providers"]:
        evidence = entry["evidence"]
        stamp = f"evidence {evidence['date']} at CLI {evidence['cli_version']}" if evidence else "no evidence file"
        lines.append(f"{entry['provider']} · installed {entry['installed_version'] or '-'} · {stamp}")
        for cap in columns:
            lines.append(f"  {cap:<20}{entry['capabilities'][cap]}")
        lines.extend(f"  warning: {warning}" for warning in entry["warnings"])
    return "\n".join(lines)


def render_markdown(directory: Path | None = None) -> str:
    """The text of ``docs/llm-bridge/provider-capabilities.md`` (DOCS-003), from the registry and evidence files only."""
    head = ["| Provider | " + " | ".join(CAPABILITIES) + " | Evidence |", "|" + " --- |" * (len(CAPABILITIES) + 2)]
    rows = []
    notes = []
    for name, spec in bridge_providers.REGISTRY.items():
        evidence = load_evidence(name, directory)
        cells = [status(spec, cap, evidence) for cap in CAPABILITIES]
        stamp = f"{evidence['date']} (CLI {evidence['cli_version']})" if evidence else "none"
        rows.append(f"| {spec.display_name} | " + " | ".join(cells) + f" | {stamp} |")
        if spec.prompt_channel_note:
            notes.append(f"- {spec.display_name}: prompt channel `{spec.prompt_channel}`; {spec.prompt_channel_note}")
        if spec.native_unavailable:
            notes.append(f"- {spec.display_name}: no native mission. {spec.native_unavailable}")
    return "\n".join([
        "# Provider capabilities",
        "",
        "Generated from `llm_tools/bridge_providers.py` and `llm_tools/provider_evidence/*.json` by",
        "`python -m llm_tools.bridge_capabilities --write-doc`; `tests/test_bridge_capabilities.py` fails when this file differs.",
        "Do not edit it by hand. Earlier hand-written discovery notes are in `provider-capabilities-history.md`.",
        "",
        "`unverified` means Bridge implements the capability but no evidence file records it for a live CLI.",
        "",
        *head, *rows, "", "Notes", "", *notes, "",
    ])


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--write-doc"]:
        DOC_PATH.write_text(render_markdown(), encoding="utf-8")
        return 0
    print("usage: python -m llm_tools.bridge_capabilities --write-doc", file=sys.stderr)
    return 2


def run_doctor_capabilities(as_json: bool) -> str:
    """The text ``llm-bridge doctor --capabilities`` prints (JSON when ``as_json``)."""
    report = capabilities_report()
    return json.dumps(report, indent=2, sort_keys=True) if as_json else format_capabilities(report)


if __name__ == "__main__":
    raise SystemExit(main())
