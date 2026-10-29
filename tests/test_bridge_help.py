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


def test_console_start_feedback_reads_durable_state_and_keeps_log_path_secondary(tmp_path, monkeypatch):
    import json
    import subprocess
    from llm_tools import bridge
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    with bridge.open_store() as store:
        mission=store.create_mission('Start','Objective',cwd=tmp_path)
        store.set_mission_status(mission['id'],'RUNNING')
    monkeypatch.setattr(bridge.subprocess,'run',lambda argv,**kwargs: subprocess.CompletedProcess(argv,0,'mission running; controller log: private/path',''))
    result=json.loads(bridge.dispatch_console_command(mission['id'],'start'))
    assert result['status']=='APPLIED'
    assert result['result']['detail']=='Running · use attach to open the provider'
    assert 'private/path' not in result['result']['detail']
    monkeypatch.setattr(bridge.subprocess,'run',lambda argv,**kwargs: subprocess.CompletedProcess(argv,2,'','station unavailable'))
    assert bridge.dispatch_console_command(mission['id'],'start')=='error: station unavailable'
