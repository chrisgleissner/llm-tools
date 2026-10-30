"""Rewrites of other tools' config files are atomic and keep the original file mode."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from llm_tools import common


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _refresh(cred: Path, monkeypatch: pytest.MonkeyPatch) -> str | None:
    monkeypatch.setattr(common, "urlopen", lambda _req, timeout=0: _Resp(b'{"access_token":"new-token","expires_in":3600}'))
    data = json.loads(cred.read_text(encoding="utf-8"))
    return common._refresh_claude_oauth_access_token(cred, data, {"LLM_USAGE_LIVE_FETCH_RETRIES": "0"})


def _credentials(tmp_path: Path, mode: int) -> Path:
    cred = tmp_path / ".claude" / ".credentials.json"
    cred.parent.mkdir()
    cred.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r", "accessToken": "old"}}), encoding="utf-8")
    cred.chmod(mode)
    return cred


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_credential_rewrite_keeps_the_original_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int) -> None:
    cred = _credentials(tmp_path, mode)
    assert _refresh(cred, monkeypatch) == "new-token"
    assert stat.S_IMODE(cred.stat().st_mode) == mode
    assert json.loads(cred.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new-token"
    assert [p.name for p in cred.parent.iterdir()] == [".credentials.json"], "no temporary file is left behind"


def test_credential_rewrite_goes_through_a_replace_so_a_failure_keeps_the_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = _credentials(tmp_path, 0o600)
    before = cred.read_text(encoding="utf-8")
    monkeypatch.setattr(common.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    assert _refresh(cred, monkeypatch) == "new-token"  # the token is still returned for this read
    assert cred.read_text(encoding="utf-8") == before, "the original file is untouched"
    assert [p.name for p in cred.parent.iterdir()] == [".credentials.json"]


def test_credential_symlink_is_kept_and_its_target_updated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = _credentials(tmp_path, 0o600)
    link_dir = tmp_path / "dotfiles"
    link_dir.mkdir()
    link = link_dir / "credentials.json"
    link.symlink_to(real)
    assert _refresh(link, monkeypatch) == "new-token"
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new-token"


def test_copilot_settings_update_keeps_the_original_mode(tmp_path: Path) -> None:
    config = tmp_path / "copilot"
    config.mkdir()
    settings = config / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    settings.chmod(0o600)
    common.ensure_copilot_footer_settings({"COPILOT_HOME": str(config), "HOME": str(tmp_path)})
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["footer"] == {"showQuota": True, "showAiUsed": True} and data["theme"] == "dark"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert [p.name for p in config.iterdir()] == ["settings.json"]


def test_atomic_write_creates_new_files_private_and_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(common.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    target = tmp_path / "new.json"
    common.atomic_write_text(target, "{}\n")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert synced, "file contents are fsynced before the rename"
