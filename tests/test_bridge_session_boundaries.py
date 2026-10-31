"""Malformed metadata must never become a restored native conversation."""
import json
import pytest
from llm_tools import bridge_sessions as sessions

SID = '12345678-1234-4234-8234-123456789abc'


@pytest.mark.parametrize('provider',['claude','copilot'])
@pytest.mark.parametrize('kind',[[],{},None,42])
def test_untrusted_metadata_type_never_raises_or_binds(tmp_path, provider, kind):
    record = {'type':kind,'sessionId':SID,'cwd':str(tmp_path)}
    assert sessions.parse_session_metadata(provider,record,cwd=tmp_path) is None


def test_sidechain_and_nonboolean_sidechain_are_not_main_native_sessions(tmp_path):
    for sidechain in [True,'true',{},0]:
        record = {'type':'user','sessionId':SID,'cwd':str(tmp_path),'isSidechain':sidechain}
        assert sessions.parse_session_metadata('claude',record,cwd=tmp_path) is None


@pytest.mark.parametrize('provider',['claude','copilot','codex'])
def test_metadata_for_removed_working_directory_is_unavailable(tmp_path,provider):
    assert sessions.provider_session_metadata(provider,SID,cwd=tmp_path/'deleted') is None


def test_explicit_codex_identity_survives_many_newer_session_directories(tmp_path,monkeypatch):
    home = tmp_path/'codex'
    monkeypatch.setenv('CODEX_HOME',str(home))
    for index in range(45): (home/'sessions'/f'day-{index:03}').mkdir(parents=True)
    transcript = home/'sessions'/'day-000'/f'rollout-{SID}.jsonl'
    transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':SID,'cwd':str(tmp_path)}})+'\n')
    record = sessions.provider_session_metadata('codex',SID,cwd=tmp_path)
    assert record is not None
    assert record.path == transcript


def test_capability_queries_fail_closed_for_missing_or_failing_cli(tmp_path,monkeypatch):
    import subprocess
    monkeypatch.setattr(sessions.shutil,'which',lambda _:'fake-cli')
    observed = []
    def unavailable(argv,**kwargs):
        observed.append((argv,kwargs['timeout']))
        raise subprocess.TimeoutExpired(argv,3)
    monkeypatch.setattr(sessions.subprocess,'run',unavailable)
    identity = sessions.NativeSessionIdentity('codex',SID,str(tmp_path),'codex:session_meta')
    assert sessions.resume_argv(identity) is None
    assert sessions.codex_owned_argv(['codex','prompt']) is None
    assert sessions.new_session_argv('claude',['claude','prompt'],cwd=tmp_path) is None
    assert observed and all(timeout==3 for _,timeout in observed)
    assert sessions._installed_help('unsupported',None) is None
    monkeypatch.setattr(sessions.shutil,'which',lambda _:None)
    assert sessions.resume_argv(identity) is None
    assert sessions.new_session_argv('claude',['claude'],cwd=tmp_path,help_text='--session-id UUID') is None
    assert sessions.codex_owned_argv(['codex'],help_text='--no-daemon') is None


@pytest.mark.parametrize('code,help_text',[(1,'--resume UUID'),(0,'--resume-other UUID'),(0,'unrelated')])
def test_resume_rejects_nonzero_or_unadvertised_capability(tmp_path,monkeypatch,code,help_text):
    import subprocess
    monkeypatch.setattr(sessions.shutil,'which',lambda _:'claude')
    monkeypatch.setattr(sessions.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a[0],code,help_text,''))
    identity = sessions.NativeSessionIdentity('claude',SID,str(tmp_path),'claude:transcript')
    assert sessions.resume_argv(identity) is None


def test_invalid_native_launches_are_not_rewritten(tmp_path,monkeypatch):
    monkeypatch.setattr(sessions.shutil,'which',lambda name:name)
    missing = tmp_path/'removed'
    file = tmp_path/'not-directory'
    file.write_text('file')
    for cwd in (missing,file):
        identity = sessions.NativeSessionIdentity('claude',SID,str(cwd),'claude:transcript')
        assert sessions.resume_argv(identity,help_text='--resume ID') is None
        assert sessions.new_session_argv('claude',['claude'],cwd=cwd,help_text='--session-id UUID') is None
    for argv in ([],['wrapper'],['claude','--resume',SID],['claude','--session-id',SID],['claude','--session',SID]):
        assert sessions.new_session_argv('claude',argv,cwd=tmp_path,help_text='--session-id UUID') is None
    assert sessions.new_session_argv('claude',['claude'],cwd=tmp_path,help_text='--other') is None
    assert sessions.new_session_argv('claude',['claude'],cwd=tmp_path,help_text='--session-id UUID',session_id='../escape') is None
    assert sessions.codex_owned_argv([]) is None
    assert sessions.codex_owned_argv(['wrapper']) is None
    assert sessions.codex_owned_argv(['codex'],help_text='--no-daemonize') is None
    argv = ['codex','--no-daemon','--model','configured','prompt']
    assert sessions.codex_owned_argv(argv,help_text='--no-daemon') == argv
    assert sessions.resume_argv(sessions.NativeSessionIdentity('unknown',SID,str(tmp_path),'unknown'),help_text='resume') is None


def _claude_fixture(tmp_path,monkeypatch):
    import re
    cwd = tmp_path/'work.with_underscores'
    cwd.mkdir()
    root = tmp_path/'claude-config'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(root))
    path = root/'projects'/re.sub(r'[^a-zA-Z0-9]','-',str(cwd.resolve()))/(SID+'.jsonl')
    path.parent.mkdir(parents=True)
    valid = json.dumps({'type':'user','sessionId':SID,'cwd':str(cwd),'isSidechain':False}).encode()+b'\n'
    return cwd,path,valid


@pytest.mark.parametrize('prefix',[b'not json\n',b'\xff\n',b'[]\n',b'null\n'])
def test_native_reader_recovers_from_malformed_unrelated_records(tmp_path,monkeypatch,prefix):
    cwd,path,valid = _claude_fixture(tmp_path,monkeypatch)
    path.write_bytes(prefix+valid)
    record = sessions.provider_session_metadata('claude',SID,cwd=cwd)
    assert record is not None
    assert record.identity.session_id == SID


@pytest.mark.parametrize('content',[b'x'*65537+b'\n',b'{}\n'*256])
def test_native_reader_does_not_scan_past_size_or_record_budget(tmp_path,monkeypatch,content):
    cwd,path,valid = _claude_fixture(tmp_path,monkeypatch)
    path.write_bytes(content+valid)
    assert sessions.provider_session_metadata('claude',SID,cwd=cwd) is None


def test_native_reader_rejects_symlink_directory_and_unknown_provider(tmp_path,monkeypatch):
    cwd,path,valid = _claude_fixture(tmp_path,monkeypatch)
    outside = tmp_path/'unrelated.jsonl'
    outside.write_bytes(valid)
    path.symlink_to(outside)
    assert sessions.provider_session_metadata('claude',SID,cwd=cwd) is None
    path.unlink(); path.mkdir()
    assert sessions.provider_session_metadata('claude',SID,cwd=cwd) is None
    assert sessions.provider_session_metadata('unknown',SID,cwd=cwd) is None
    assert sessions.provider_session_metadata('claude','../escape',cwd=cwd) is None


def test_codex_record_scan_rejects_untrusted_headers_and_respects_freshness(tmp_path):
    root = tmp_path/'sessions'
    root.mkdir()
    path = root/f'rollout-{SID}.jsonl'
    valid = json.dumps({'type':'session_meta','payload':{'id':SID,'cwd':str(tmp_path)}})+'\n'
    for content in ['x'*65537,'not-json','[]',json.dumps({'type':'session_meta','payload':[]})]:
        path.write_text(content)
        assert sessions.codex_session_records(root,cwd=tmp_path) == []
    path.write_text(valid)
    original = path.stat().st_mtime_ns
    assert sessions.codex_session_records(root,cwd=tmp_path,modified_after_ns=original+1) == []
    assert sessions.codex_session_records(root,cwd=tmp_path,baseline={str(path):original}) == []
    assert len(sessions.codex_session_records(root,cwd=tmp_path)) == 1
    link = tmp_path/'symlink-root'
    link.symlink_to(root,target_is_directory=True)
    assert sessions.codex_session_records(link,cwd=tmp_path) == []
    assert sessions.codex_session_records(path,cwd=tmp_path) == []
    assert sessions.codex_session_records(root,cwd=tmp_path/'removed') == []
