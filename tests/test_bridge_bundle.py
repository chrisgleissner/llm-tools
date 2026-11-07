"""``llm-bridge doctor --bundle``: a redacted diagnostic archive (PRD-012)."""

from __future__ import annotations

import json
import stat
import tarfile
from pathlib import Path

import pytest

from llm_tools import bridge_bundle
from llm_tools.bridge import main
from llm_tools.bridge_bundle import redact

PROMPT_MARKER = "UNIQUE-PROMPT-MARKER-7f3a9c"
MISSION_NAME_WORDS = "zebra-quartz-mission"
SECRETS = {
    "config token": "cfg-secret-value-1234567890abcdef",
    "worker token": "W0rkerTok3n_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "bearer": "bearerSecretValue987654321",
    "email": "alice.example@example.org",
    "lan ip": "192.168.1.23",
    "host": "bridge.home.arpa",
    "ipv6": "fe80::1c2d:3e4f",
}


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[Path, str]:
    state = tmp_path / "state"
    home = tmp_path / "home"
    (home / ".config" / "llm-tools").mkdir(parents=True)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    (home / ".config" / "llm-tools" / "config.toml").write_text(
        f'[usage]\nproviders = ["claude", "codex"]\n\n# token = "{SECRETS["config token"]}"\n'
        f'# contact {SECRETS["email"]} at {SECRETS["lan ip"]}\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    prompt = f"Fix the parser. {PROMPT_MARKER}\nSecond line of the prompt that is long enough to match."
    assert main(["mission", "create", "--name", MISSION_NAME_WORDS, "--prompt", prompt, "--cwd", str(project), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    worker = state / "worker"
    worker.mkdir(parents=True, mode=0o700)
    (worker / "worker.json").write_text(json.dumps({
        "worker_id": "w1", "name": "mickey", "coordinator_url": f"wss://{SECRETS['host']}:8443", "token": SECRETS["worker token"],
        "allow_control": False, "max_generation": 0, "allow_terminal_view": False}), encoding="utf-8")
    runtime = state / "runtime" / f"mission-{mission_id}"
    (runtime / "ralph-logs" / "run-1").mkdir(parents=True)
    (runtime / "controller.log").write_text(
        f"started for {home}/project\nAuthorization: Bearer {SECRETS['bearer']}\nowner {SECRETS['email']} on {SECRETS['lan ip']}\n"
        f"coordinator https://{SECRETS['host']}:8443 via {SECRETS['ipv6']}\n", encoding="utf-8")
    (runtime / "ralph-logs" / "run-1" / "attempt-1.out").write_text(
        f"> {PROMPT_MARKER}\nworking on it\nSecond line of the prompt that is long enough to match.\n", encoding="utf-8")
    (runtime / "ralph-logs" / "run-1" / "prompt.txt").write_text(f"{PROMPT_MARKER}\n", encoding="utf-8")
    return state, mission_id


def test_doctor_bundle_contains_no_secrets_prompts_or_private_addresses(tmp_path, monkeypatch, capsys):
    state, mission_id = _seed(tmp_path, monkeypatch, capsys)
    output = tmp_path / "bundle.tar.gz"
    main(["doctor", "--bundle", str(output)])
    stdout = capsys.readouterr().out
    assert str(output) in stdout and "Removed:" in stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with tarfile.open(output) as archive:
        members = {member.name: archive.extractfile(member).read().decode("utf-8") for member in archive.getmembers() if member.isfile()}  # type: ignore[union-attr]
    assert {"versions.json", "doctor.json", "config/llm-tools.toml", "config/worker.json", "config/remote.json",
            "store-summary.json", "health.json"} <= set(members)
    assert any(name.endswith("controller.log") for name in members)
    assert any(name.endswith("attempt-1.out") for name in members)
    assert not any("prompt.txt" in name for name in members)
    everything = "\n".join(members.values())
    for label, secret in SECRETS.items():
        assert secret not in everything, label
    assert PROMPT_MARKER not in everything
    assert MISSION_NAME_WORDS not in everything
    assert str(tmp_path / "home") not in everything
    summary = json.loads(members["store-summary.json"])
    assert summary["missions"] == 1 and summary["tasks"] == 1 and summary["mission_list"][0]["id"] == mission_id
    assert json.loads(members["health.json"])["controllers"][0]["mission"] == mission_id
    worker = json.loads(members["config/worker.json"])
    assert worker["enrolled"] is True and worker["token_present"] is True and "token" not in worker
    assert "providers" in members["config/llm-tools.toml"]  # ordinary configuration survives


def test_doctor_bundle_never_overwrites_and_defaults_to_a_timestamped_file_in_the_current_folder(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, monkeypatch, capsys)
    existing = tmp_path / "taken.tar.gz"
    existing.write_bytes(b"keep")
    assert main(["doctor", "--bundle", str(existing)]) == 2
    assert "already exists" in capsys.readouterr().err and existing.read_bytes() == b"keep"
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    assert main(["doctor", "--bundle", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    created = list(work.glob("llm-bridge-bundle-*.tar.gz"))
    assert len(created) == 1 and result["path"] == str(created[0])
    assert stat.S_IMODE(created[0].stat().st_mode) == 0o600


REDACTION_CASES = [
    ("Authorization: Bearer abcDEF123456789xyz", "abcDEF123456789xyz"),
    ("curl -H 'Authorization: Bearer abcDEF123456789xyz'", "abcDEF123456789xyz"),
    ("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("token github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz", "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", "eyJhbGciOiJIUzI1NiJ9"),
    ("sha " + "a1b2c3d4" * 8, "a1b2c3d4a1b2c3d4"),
    ("blob Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFiQ2RFZkdoSWo", "Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MEFiQ2RFZkdoSWo"),
    ('api_key = "hunter2hunter2"', "hunter2hunter2"),
    ('{"password": "p@ss w0rd"}', "p@ss w0rd"),
    ("LLM_USAGE_ZAI_API_KEY=plainvalue123", "plainvalue123"),
    ("run --token plainvalue123 now", "plainvalue123"),
    ("mail bob@corp.example.com now", "bob@corp.example.com"),
    ("ip 10.1.2.3 and 172.16.5.4 and 172.31.255.1 and 192.168.0.9 and 127.0.0.1", "10.1.2.3"),
    ("ip 172.20.1.1", "172.20.1.1"),
    ("ip 192.168.1.23.", "192.168.1.23"),
    ("v6 fc00::12 fd12:3456::1 fe80::1ff:fe23:4567:890a ::1", "fd12:3456::1"),
    ("v6 fe80::1%eth0", "fe80::1"),
    ("host nas.home.arpa and printer.lan and mac-mini.local:8080", "nas.home.arpa"),
    ("host printer.lan", "printer.lan"),
    ("host mac-mini.local:8080", "mac-mini.local"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----", "MIIEvQIBADANBgkq"),
]


@pytest.mark.parametrize(("text", "secret"), REDACTION_CASES)
def test_redact_removes_the_secret(text, secret):
    cleaned = redact(text, home="/home/someone")
    assert secret not in cleaned
    assert redact(cleaned, home="/home/someone") == cleaned  # idempotent


KEPT_CASES = [
    "2026-11-06T10:11:12Z started",
    "public address 8.8.8.8 and 2001:4860:4860::8888",
    "mission 0123456789abcdef0123456789abcdef is RUNNING",
    "path ~/.local/share/x and settings.local.json and std::string",
    "max retries 3, status COMPLETED, schema 10",
    "a_long_identifier_with_only_lowercase_letters_and_underscores_in_it",
    "at ::",
    '{"token_present": true, "max_generation": 0}',
]


@pytest.mark.parametrize("text", KEPT_CASES)
def test_redact_keeps_ordinary_diagnostic_text(text):
    assert redact(text, home="/home/someone") == text


def test_redact_replaces_the_home_folder_and_literals():
    assert redact("open /home/someone/dev/x and /home/someone2/y", home="/home/someone") == "open ~/dev/x and /home/someone2/y"
    literals = bridge_bundle.literal_patterns(["Fix the parser please now\nshort\nand then the second line of text"])
    cleaned = redact("echo: Fix the parser please now\nshort\nand then the second line of text; again: and then the second line of text", literals=literals)
    assert "parser" not in cleaned and "second line" not in cleaned
