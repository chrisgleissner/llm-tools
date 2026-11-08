#!/usr/bin/env python3
"""Copy a coordinator database backup off the device in stages that can each be resumed (DOG-024).

The stages, in order:

1. ``--backup-command`` runs on the device through ssh and must create ``--remote-file`` and exit 0
   only when the file is complete (``bridge-coordinator backup`` does: it writes a hidden partial file
   and links it to its final name after ``integrity_check``). Nothing is fetched from a partial file.
2. The file is fetched over ssh into ``<dest>/.<name>.fetch`` and resumed from its current length when
   the transfer is interrupted, then compared with the SHA-256 the device reports.
3. ``PRAGMA integrity_check`` runs on the fetched copy here, not on the device.
4. The copy is compressed with zstd (gzip, with a warning, when zstd is not installed) into a hidden
   ``.partial`` file that is renamed only when the compressor succeeded, and optionally encrypted with
   ``gpg --symmetric``.
5. Only after all of that: the remote file is removed (``--remove-remote``) and archives beyond
   ``--keep`` are deleted.

Every failure prints what went wrong, which files were kept and the command that continues, and exits
with a code of its own. No stage ever deletes a partial result: a rerun with the same arguments picks
up where the previous run stopped.

Exit codes: 0 done, 2 device backup failed, 3 transfer failed (partial kept, rerun resumes),
4 fetched file failed verification (kept as ``*.rejected``), 5 compression or encryption failed,
6 invalid arguments or environment.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

EXIT_BACKUP, EXIT_FETCH, EXIT_VERIFY, EXIT_COMPRESS, EXIT_USAGE = 2, 3, 4, 5, 6
CHUNK = 1024 * 1024


class Stage(Exception):
    """A stage failed; ``code`` is the process exit status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def say(message: str) -> None:
    print(f"offsite-backup: {message}", file=sys.stderr, flush=True)


def ssh_argv(args: argparse.Namespace, remote_command: str) -> list[str]:
    argv = shlex.split(args.ssh_command) + [
        "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
    ]
    argv += [args.host, remote_command]
    return argv


def run_remote(args: argparse.Namespace, remote_command: str, *, timeout: float | None, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ssh_argv(args, remote_command), input=stdin, capture_output=True, timeout=timeout, check=False
    )


def stage_backup(args: argparse.Namespace, stamp: str) -> str:
    remote_file = args.remote_file.replace("{stamp}", stamp)
    command = args.backup_command.replace("{stamp}", stamp).replace("{remote_file}", remote_file)
    say(f"stage 1: running the backup on {args.host} (limit {args.backup_timeout:.0f} s)")
    try:
        done = run_remote(args, command, timeout=args.backup_timeout)
    except subprocess.TimeoutExpired:
        raise Stage(
            EXIT_BACKUP,
            f"the backup on {args.host} did not finish within {args.backup_timeout:.0f} s. Nothing was fetched. "
            f"A hidden partial file may remain next to {remote_file} on the device; the coordinator removes it when "
            "the backup fails, and a later backup does not reuse it. Check the load on the device before retrying.",
        ) from None
    sys.stderr.buffer.write(done.stderr[-4000:])
    if done.returncode != 0:
        raise Stage(
            EXIT_BACKUP,
            f"the backup command exited with status {done.returncode} on {args.host}; nothing was fetched. "
            f"Output tail: {done.stdout[-500:].decode(errors='replace').strip()!r}",
        )
    return remote_file


def remote_size_and_hash(args: argparse.Namespace, remote_file: str) -> tuple[int, str]:
    q = shlex.quote(remote_file)
    done = run_remote(args, f"stat -c %s {q} && sha256sum {q}", timeout=args.step_timeout)
    if done.returncode != 0:
        raise Stage(EXIT_FETCH, f"cannot read {remote_file} on {args.host}: {done.stderr.decode(errors='replace').strip()}")
    lines = done.stdout.decode().split("\n")
    return int(lines[0]), lines[1].split()[0]


def stage_fetch(args: argparse.Namespace, remote_file: str, fetch: Path) -> None:
    size, digest = remote_size_and_hash(args, remote_file)
    have = fetch.stat().st_size if fetch.exists() else 0
    if have > size:
        # Not a prefix of this file: keep it aside rather than overwrite what may be a different backup.
        aside = fetch.with_name(fetch.name + f".stale-{int(time.time())}")
        fetch.rename(aside)
        say(f"{fetch.name} is longer than the remote file; kept as {aside.name}")
        have = 0
    say(f"stage 2: fetching {remote_file} ({size / 1048576:.1f} MiB), resuming at {have / 1048576:.1f} MiB")
    attempts = 0
    while have < size:
        attempts += 1
        proc = subprocess.Popen(
            ssh_argv(args, f"tail -c +{have + 1} {shlex.quote(remote_file)}"), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert proc.stdout is not None
        with fetch.open("ab") as out:
            while True:
                block = proc.stdout.read(CHUNK)
                if not block:
                    break
                out.write(block)
                out.flush()
                os.fsync(out.fileno())
                have += len(block)
        status = proc.wait()
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        if have < size and (status == 0 or attempts >= args.fetch_attempts):
            raise Stage(
                EXIT_FETCH,
                f"transfer stopped at {have} of {size} bytes (ssh status {status}). The partial file {fetch} is kept; "
                f"run the command again with --resume to continue. {err}",
            )
        if have < size:
            say(f"transfer interrupted at {have} of {size} bytes (ssh status {status}); retrying ({attempts}/{args.fetch_attempts})")
    h = hashlib.sha256()
    with fetch.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    if h.hexdigest() != digest:
        rejected = fetch.with_name(fetch.name.removesuffix(".fetch") + ".rejected")
        fetch.rename(rejected)
        raise Stage(EXIT_VERIFY, f"the fetched file does not match the SHA-256 reported by the device; kept as {rejected}")


def stage_verify(fetch: Path) -> None:
    say("stage 3: integrity_check on the fetched copy")
    try:
        conn = sqlite3.connect(f"file:{fetch}?mode=ro", uri=True)
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        verdict = f"{type(exc).__name__}: {exc}"
    if verdict != "ok":
        rejected = fetch.with_name(fetch.name.removesuffix(".fetch") + ".rejected")
        fetch.rename(rejected)
        raise Stage(EXIT_VERIFY, f"integrity_check failed on the fetched copy ({verdict}); kept as {rejected}")


def stage_compress(args: argparse.Namespace, fetch: Path, final_plain: Path) -> Path:
    if args.no_compress:
        fetch.rename(final_plain)
        return final_plain
    if shutil.which("zstd"):
        suffix, argv = ".zst", ["zstd", "-q", f"-{args.zstd_level}", "-T1", "--no-progress", "-f"]
    else:
        say("WARNING: zstd is not installed; compressing with gzip instead")
        suffix, argv = ".gz", ["gzip", "-6", "-c"]
    final = final_plain.with_name(final_plain.name + suffix)
    partial = final.with_name("." + final.name + ".partial")
    say(f"stage 4: compressing to {final.name}")
    with partial.open("wb") as out:
        if argv[0] == "zstd":
            done = subprocess.run(argv + ["-c", str(fetch)], stdout=out, stderr=subprocess.PIPE, check=False)
        else:
            with fetch.open("rb") as src:
                done = subprocess.run(argv, stdin=src, stdout=out, stderr=subprocess.PIPE, check=False)
        out.flush()
        os.fsync(out.fileno())
    if done.returncode != 0:
        raise Stage(
            EXIT_COMPRESS,
            f"{argv[0]} failed ({done.stderr.decode(errors='replace').strip()}). The verified copy {fetch} and the "
            f"partial {partial} are kept; run the command again to compress once more.",
        )
    os.replace(partial, final)
    return final


def stage_encrypt(args: argparse.Namespace, archive: Path) -> Path:
    if not args.gpg_passphrase_file:
        return archive
    final = archive.with_name(archive.name + ".gpg")
    partial = final.with_name("." + final.name + ".partial")
    say(f"stage 5: encrypting to {final.name}")
    done = subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-file", args.gpg_passphrase_file,
         "--symmetric", "--cipher-algo", "AES256", "--output", str(partial), str(archive)],
        capture_output=True, check=False,
    )
    if done.returncode != 0:
        raise Stage(EXIT_COMPRESS, f"gpg failed ({done.stderr.decode(errors='replace').strip()}); the unencrypted archive {archive} is kept")
    os.replace(partial, final)
    archive.unlink()
    return final


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="ssh destination of the device, for example chris@pi")
    p.add_argument("--ssh-command", default="ssh", help="ssh program and fixed options (default: ssh)")
    p.add_argument("--backup-command", required=True,
                   help="command run on the device; {stamp} and {remote_file} are replaced. It must create {remote_file}")
    p.add_argument("--remote-file", required=True, help="path of the finished backup on the device; {stamp} is replaced")
    p.add_argument("--dest", required=True, type=Path, help="directory for the archives (created, mode 0700)")
    p.add_argument("--resume", metavar="STAMP",
                   help="continue the run with this stamp: the backup already exists on the device, so stage 1 is skipped "
                        "and the fetch continues from the length of the partial file")
    p.add_argument("--backup-timeout", type=float, default=1800.0, help="seconds the device backup may run (default 1800)")
    p.add_argument("--step-timeout", type=float, default=60.0, help="seconds for a short remote command (default 60)")
    p.add_argument("--fetch-attempts", type=int, default=5)
    p.add_argument("--zstd-level", type=int, default=9)
    p.add_argument("--no-compress", action="store_true")
    p.add_argument("--gpg-passphrase-file", help="encrypt the archive symmetrically with this passphrase file")
    p.add_argument("--remove-remote", action="store_true", help="delete the backup on the device after a complete, verified fetch")
    p.add_argument("--keep", type=int, default=30, help="archives to keep in --dest (default 30; 0 keeps all)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.keep < 0 or args.fetch_attempts < 1:
        say("--keep must be 0 or more and --fetch-attempts at least 1")
        return EXIT_USAGE
    args.dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.dest, 0o700)
    stamp = args.resume or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"coordinator-{stamp}.sqlite3"
    fetch = args.dest / f".{name}.fetch"
    plain = args.dest / name
    resume = f"{shlex.join([sys.argv[0]] + (argv if argv is not None else sys.argv[1:]))}" + ("" if args.resume else f" --resume {stamp}")
    started = time.monotonic()
    backed_up = bool(args.resume)
    try:
        remote_file = args.remote_file.replace("{stamp}", stamp)
        if args.resume:
            say(f"stage 1 skipped: resuming run {stamp}")
        else:
            stage_backup(args, stamp)
            backed_up = True
        stage_fetch(args, remote_file, fetch)
        stage_verify(fetch)
        archive = stage_compress(args, fetch, plain)
        archive = stage_encrypt(args, archive)
        if fetch.exists():
            fetch.unlink()
        if args.remove_remote:
            done = run_remote(args, f"rm -f -- {shlex.quote(remote_file)}", timeout=args.step_timeout)
            if done.returncode != 0:
                say(f"WARNING: could not remove {remote_file} on the device (status {done.returncode})")
    except Stage as stage:
        say(f"FAILED: {stage}")
        if backed_up and stage.code in (EXIT_FETCH, EXIT_COMPRESS):
            say(f"to continue: {resume}")
        return stage.code
    except subprocess.TimeoutExpired as exc:
        say(f"FAILED: {exc.cmd[-1] if exc.cmd else 'command'} timed out")
        return EXIT_FETCH
    if args.keep:
        archives = sorted(p for p in args.dest.glob("coordinator-*.sqlite3*") if not p.name.startswith("."))
        for old in archives[: max(0, len(archives) - args.keep)]:
            old.unlink()
    say(f"done in {time.monotonic() - started:.1f} s: {archive} ({archive.stat().st_size / 1048576:.1f} MiB)")
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
