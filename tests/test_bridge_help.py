"""Packaged help describes the actual grammar without opening mission state."""
from pathlib import Path

from llm_tools.bridge import _build_parser, main
from llm_tools.bridge_help import COMMANDS, TOPICS, topic_text


def test_all_documented_examples_parse_and_command_help_has_context() -> None:
    import shlex
    parser = _build_parser()
    for path, (_, example) in COMMANDS.items():
        parser.parse_args(shlex.split(example))
        text = topic_text(path)
        assert "Example:" in text, path
        assert "provider work" in text or "paid capacity" in text, path
        assert "==SUPPRESS==" not in text
        assert "_controller" not in text


def test_help_topics_and_root_create_no_state(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "must-not-exist"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    for topic in TOPICS:
        assert main(["help", topic]) == 0
        assert topic_text(topic) in capsys.readouterr().out
    assert not state.exists()
    text = _build_parser().format_help()
    assert "_controller" not in text and "==SUPPRESS==" not in text
    assert len(text.splitlines()) <= 25


def test_help_help_uses_public_parser(capsys) -> None:
    import pytest
    with pytest.raises(SystemExit) as result:
        main(["help", "--help"])
    assert result.value.code == 0
    assert "topic" in capsys.readouterr().out
