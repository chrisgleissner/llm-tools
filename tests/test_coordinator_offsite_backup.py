"""DOG-024: the off-device backup wrapper fails loudly and keeps partial state recoverable.

``scripts/coordinator_offsite_backup.py`` is run with a fake ``ssh`` (a shell script that runs the remote
command on this machine) and a real SQLite file. The tests cover the cases that lost the archive on the
Pi: the device backup fails, the transfer breaks half way and is resumed, a corrupt file arrives, and the
compressor fails.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "coordinator_offsite_backup.py"
spec = importlib.util.spec_from_file_location("coordinator_offsite_backup", SCRIPT)
assert spec and spec.loader
offsite = importlib.util.module_from_spec(spec)
sys.modules["coordinator_offsite_backup"] = offsite
spec.loader.exec_module(offsite)

FAKE_SSH = """#!/bin/bash
# Fake ssh: ignore options and the host, run the command locally. FAKE_SSH_CUT=N makes a `tail -c` fetch
# end after N bytes with a failure status, once (the marker file records that it happened).
while [[ "$1" == -o ]]; do shift 2; done
shift
cmd="$1"
if [[ -n "$FAKE_SSH_CUT" && "$cmd" == tail\\ -c* && ! -e "$FAKE_SSH_MARK" ]]; then
  touch "$FAKE_SSH_MARK"
  bash -c "$cmd" | head -c "$FAKE_SSH_CUT"
  exit 255
fi
exec bash -c "$cmd"
"""


def make_database(path: Path, rows: int = 4000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO t(body) VALUES(?)", (("event number %d " % i * 8,) for i in range(rows)))
    conn.commit()
    conn.close()


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    device = tmp_path / "device"
    device.mkdir()
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_SSH_MARK", str(tmp_path / "cut-done"))
    source = tmp_path / "source.sqlite3"
    make_database(source)
    return {"device": device, "ssh": ssh, "dest": tmp_path / "archives", "source": source}


def run(rig: dict[str, object], backup_command: str, *extra: str) -> int:
    device = rig["device"]
    return offsite.main(
        [
            "--host", "pi",
            "--ssh-command", str(rig["ssh"]),
            "--backup-command", backup_command,
            "--remote-file", f"{device}/backup-{{stamp}}.sqlite3",
            "--dest", str(rig["dest"]),
            "--backup-timeout", "30",
            *extra,
        ]
    )


def copy_command(rig: dict[str, object]) -> str:
    return f"cp {rig['source']} {rig['device']}/backup-{{stamp}}.sqlite3"


def archives(rig: dict[str, object]) -> list[Path]:
    return sorted(Path(str(rig["dest"])).iterdir())


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_successful_run_produces_a_verified_zstd_archive(rig: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(rig, copy_command(rig), "--remove-remote") == 0
    (archive,) = archives(rig)
    assert archive.name.endswith(".sqlite3.zst")
    assert not list(Path(str(rig["device"])).iterdir()), "the device copy is removed after a verified fetch"
    restored = Path(str(rig["dest"])).parent / "restored.sqlite3"
    subprocess.run(["zstd", "-q", "-d", str(archive), "-o", str(restored)], check=True)
    assert sqlite3.connect(restored).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4000
    assert oct(os.stat(rig["dest"]).st_mode & 0o777) == "0o700"
    assert "stage 4" in capsys.readouterr().err


def test_a_failed_device_backup_exits_2_and_fetches_nothing(rig: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(rig, "echo 'no space left' >&2; exit 1") == offsite.EXIT_BACKUP
    err = capsys.readouterr().err
    assert "FAILED" in err and "nothing was fetched" in err
    assert archives(rig) == []


def test_a_slow_device_backup_is_stopped_at_the_limit(rig: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    device = rig["device"]
    code = offsite.main(
        ["--host", "pi", "--ssh-command", str(rig["ssh"]), "--backup-command", "sleep 20",
         "--remote-file", f"{device}/x", "--dest", str(rig["dest"]), "--backup-timeout", "0.5"]
    )
    assert code == offsite.EXIT_BACKUP
    assert "did not finish within" in capsys.readouterr().err


def test_an_interrupted_transfer_keeps_the_partial_file_and_resumes(
    rig: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_SSH_CUT", "20000")
    code = run(rig, copy_command(rig), "--no-compress", "--fetch-attempts", "1")
    err = capsys.readouterr().err
    assert code == offsite.EXIT_FETCH, err
    (partial,) = archives(rig)
    assert partial.name.endswith(".fetch") and partial.stat().st_size == 20000
    assert "--resume" in err and "kept" in err
    stamp = partial.name.split("coordinator-")[1].split(".sqlite3")[0]
    # The fault is gone (the marker file exists); the resumed run continues from byte 20000.
    assert run(rig, copy_command(rig), "--no-compress", "--resume", stamp) == 0
    (done,) = archives(rig)
    assert done.name == f"coordinator-{stamp}.sqlite3"
    assert sqlite3.connect(done).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4000


def test_a_transfer_retry_inside_one_run_completes(rig: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SSH_CUT", "30000")
    assert run(rig, copy_command(rig), "--no-compress", "--fetch-attempts", "3") == 0
    (done,) = archives(rig)
    assert done.stat().st_size == Path(str(rig["source"])).stat().st_size


def test_a_corrupt_copy_is_rejected_and_kept(rig: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
    device = rig["device"]
    corrupt = f"head -c 40000 {rig['source']} > {device}/backup-{{stamp}}.sqlite3; printf 'garbage garbage' >> {device}/backup-{{stamp}}.sqlite3"
    assert run(rig, corrupt, "--no-compress") == offsite.EXIT_VERIFY
    (rejected,) = archives(rig)
    assert rejected.name.endswith(".rejected")
    assert "integrity_check failed" in capsys.readouterr().err


def test_a_failing_compressor_keeps_the_verified_copy(
    rig: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "zstd"
    fake.write_text("#!/bin/sh\necho 'cannot allocate memory' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert run(rig, copy_command(rig)) == offsite.EXIT_COMPRESS
    names = [p.name for p in archives(rig)]
    assert any(n.endswith(".sqlite3.fetch") for n in names), names
    err = capsys.readouterr().err
    assert "kept" in err and "to continue" in err


def test_old_archives_are_pruned_only_after_a_complete_run(rig: dict[str, object]) -> None:
    dest = Path(str(rig["dest"]))
    dest.mkdir()
    for day in ("20260101", "20260102", "20260103"):
        (dest / f"coordinator-{day}T000000Z.sqlite3.zst").write_bytes(b"old")
    assert run(rig, "exit 1", "--keep", "2") == offsite.EXIT_BACKUP
    assert len(archives(rig)) == 3, "a failed run deletes nothing"
    assert run(rig, copy_command(rig), "--keep", "2", "--no-compress") == 0
    assert len(archives(rig)) == 2
