#!/usr/bin/env python3
"""Regenerate the coordinator database fixtures in coordinator/tests/fixtures/schema/.

Each fixture is the database that the coordinator at the commit which introduced schema version N
creates on a first start (``Db::open``), plus a few seed rows written through the tables of the first
schema. The seed uses only columns that exist in schema 1, so the same rows are valid in every version.
The current coordinator must open every fixture, migrate it and keep the seed rows
(``coordinator/tests/migrations.rs``).

Schema 15 was never the target of a commit (14 was followed directly by 16), but each migration step is its
own transaction, so a start that stopped after the 14 to 15 step leaves a database at 15. That state is built
from the schema 14 fixture by running only the 14 to 15 step (``MIGRATE_V15`` in db.rs).

Usage: scripts/make_coordinator_schema_fixtures.py [--only N] [--target-dir DIR]
Needs git, cargo and network access for crates of the old lock files. Nothing under the real
Bridge state or the coordinator data directory is read or written.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coordinator" / "tests" / "fixtures" / "schema"

# schema version -> first commit whose coordinator wrote that version
COMMITS = {
    1: "b8a09a1d", 2: "0f47ea91", 3: "a649ef96", 4: "5a419919", 5: "0e48c648", 6: "9ad0c031",
    7: "c53a6db0", 8: "f4d61cf6", 9: "854b5f79", 10: "00b6689c", 11: "e23df4b2", 12: "2220c0b9",
    13: "5ff9dbd9", 14: "e5ff41e9", 16: "2c7548c6", 17: "3c55db76", 18: "5c78da9a",
}

OPEN_ONLY = """fn main() {
    let path = std::env::args().nth(1).expect("path");
    let db = bridge_coordinator::db::Db::open(std::path::Path::new(&path)).expect("open");
    drop(db);
}
"""

SEED = [
    "INSERT INTO workers(name, token_hash, created_at) VALUES('seed-worker', 'seed-worker-token-hash', 1700000000)",
    "INSERT INTO missions(worker, mission_id, info) VALUES('seed-worker', 'mission-seed-1', '{\"title\":\"seed mission\"}')",
    "INSERT INTO events(worker, id, body) VALUES('seed-worker', 1, '{\"kind\":\"seed\"}')",
    "INSERT INTO events(worker, id, body) VALUES('seed-worker', 2, '{\"kind\":\"seed\"}')",
    "INSERT INTO admin(id, username, password_hash, created_at) VALUES(1, 'seed-admin', 'seed-password-hash', 1700000000)",
    "INSERT INTO api_tokens(name, token_hash, created_at) VALUES('seed-token', 'seed-api-token-hash', 1700000000)",
    "INSERT INTO audit(ts, actor, action, detail, remote) VALUES(1700000000, 'seed-admin', 'seed', 'seed detail', 'seed-remote')",
]


def build(version: int, commit: str, target_dir: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix=f"coordfix-{version}-"))
    tree = work / "tree"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(tree), commit], check=True)
    try:
        examples = tree / "coordinator" / "examples"
        examples.mkdir(exist_ok=True)
        (examples / "open_only.rs").write_text(OPEN_ONLY)
        db = work / "bridge.sqlite3"
        env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir))
        subprocess.run(
            ["nice", "-n", "10", "cargo", "run", "-q", "--example", "open_only", "--", str(db)],
            cwd=tree / "coordinator", env=env, check=True,
        )
        con = sqlite3.connect(db)
        found = con.execute("PRAGMA user_version").fetchone()[0]
        if found != version:
            raise SystemExit(f"commit {commit} wrote schema {found}, expected {version}")
        for stmt in SEED:
            con.execute(stmt)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.close()
        dest = OUT / f"schema-v{version}.sqlite3"
        shutil.copyfile(db, dest)
        return dest
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(tree)], check=False)
        shutil.rmtree(work, ignore_errors=True)


MIGRATE_V15 = "ALTER TABLE api_tokens ADD COLUMN allowed_workers TEXT NOT NULL DEFAULT '[]';"


def derive_v15() -> None:
    """Half-migrated state: the schema 14 fixture after only the 14 to 15 step."""
    dest = OUT / "schema-v15.sqlite3"
    shutil.copyfile(OUT / "schema-v14.sqlite3", dest)
    con = sqlite3.connect(dest)
    con.execute(MIGRATE_V15)
    con.execute("PRAGMA user_version = 15")
    con.commit()
    con.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", type=int)
    p.add_argument("--target-dir", default=str(Path.home() / ".cache" / "coordinator-fixture-target"))
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for version, commit in COMMITS.items():
        if a.only and version != a.only:
            continue
        print(f"schema {version} from {commit}", flush=True)
        build(version, commit, Path(a.target_dir))
    derive_v15()
    fixtures = {str(v): c for v, c in COMMITS.items()}
    fixtures["15"] = "derived from 14 by the 14 to 15 step only"
    manifest = {
        "note": "generated by scripts/make_coordinator_schema_fixtures.py",
        "fixtures": dict(sorted(fixtures.items(), key=lambda kv: int(kv[0]))),
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
