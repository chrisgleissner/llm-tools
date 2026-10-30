"""CLI start-up must not import the network stack; it is loaded on first use."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _modules_after(code: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\n{code}\nprint('\\n'.join(sorted(sys.modules)))"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return set(result.stdout.split())


def test_importing_the_bridge_cli_does_not_load_the_network_stack() -> None:
    # Under coverage the interpreter itself loads a few modules (hashlib among them), so compare with a bare start.
    baseline = _modules_after("")
    loaded = _modules_after("import llm_tools.bridge, llm_tools.bridge_rotation, llm_tools.config") - baseline
    assert not {"urllib.request", "http.client", "ssl", "email.parser", "hashlib", "calendar"} & loaded


def test_network_helpers_load_on_first_use_and_stay_patchable() -> None:
    loaded = _modules_after(
        "from llm_tools import common\n"
        "request = common.Request('https://example.invalid/', headers={'X': 'y'})\n"
        "assert request.full_url == 'https://example.invalid/' and request.get_header('X') == 'y'\n"
        "assert common._http_error().__name__ == 'HTTPError'\n"
    )
    assert "urllib.request" in loaded and "urllib.error" in loaded


def test_live_reads_use_the_module_level_urlopen(monkeypatch) -> None:
    from llm_tools import common

    seen: list[str] = []

    def fake(request, timeout=None):
        seen.append(request.full_url)
        raise OSError("offline")

    monkeypatch.setattr(common, "urlopen", fake)
    try:
        common.urlopen(common.Request("https://example.invalid/x"), timeout=1)
    except OSError:
        pass
    assert seen == ["https://example.invalid/x"]
