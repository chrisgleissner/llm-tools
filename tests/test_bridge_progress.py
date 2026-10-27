"""Conservative repository activity and convergence evidence contracts."""
from pathlib import Path
import subprocess
from llm_tools.bridge_progress import repository_checkpoint, convergence_signals


def test_checkpoint_detects_same_size_edits_reversal_and_untracked_files(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    path = tmp_path/'source.py'
    path.write_text('first\n')
    first = repository_checkpoint(tmp_path)
    path.write_text('other\n')
    second = repository_checkpoint(tmp_path)
    path.write_text('first\n')
    assert first['fingerprint'] != second['fingerprint']
    assert repository_checkpoint(tmp_path)['fingerprint'] == first['fingerprint']
    (tmp_path/'new.md').write_text('new work')
    assert repository_checkpoint(tmp_path)['fingerprint'] != first['fingerprint']
    path.unlink()
    assert repository_checkpoint(tmp_path)['files'] == 1


def test_checkpoint_does_not_follow_symlinks_or_claim_partial_scan_is_complete(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    outside = tmp_path.parent/'outside-secret'
    outside.write_text('secret is never read')
    (tmp_path/'link').symlink_to(outside)
    first = repository_checkpoint(tmp_path)
    outside.write_text('changed outside content')
    assert repository_checkpoint(tmp_path)['fingerprint'] == first['fingerprint']
    assert repository_checkpoint(tmp_path, max_files=0)['fingerprint'] is None
    assert repository_checkpoint(tmp_path, max_bytes=1)['fingerprint'] is None
    assert repository_checkpoint(tmp_path/'missing')['fingerprint'] is None


def test_checkpoint_skips_git_processes_for_plain_directories(tmp_path, monkeypatch):
    from llm_tools import bridge_progress

    monkeypatch.setattr(bridge_progress, '_git', lambda *_args: (_ for _ in ()).throw(AssertionError('git should not start')))
    result = repository_checkpoint(tmp_path)
    assert result['fingerprint'] is None
    assert result['reason'] == 'not a Git worktree'


def test_convergence_requires_repeated_evidence_not_elapsed_build_time():
    assert convergence_signals([])['warnings'] == []
    observations = [{'before':'a','after':'a','exit_code':1,'output_sha256':'same','station':station}
                    for station in ['codex','claude','codex']]
    signals = convergence_signals(observations)
    assert signals['unchanged_operations'] == 3
    assert signals['identical_failures'] == 3
    assert signals['provider_cycles_without_change'] is True
    assert signals['warnings']
    assert convergence_signals([{'before':None,'after':None,'exit_code':0,'seconds':86400}])['warnings'] == []
    reversals = [{'before':'a','after':'b'},{'before':'b','after':'a'},
                 {'before':'a','after':'b'},{'before':'b','after':'a'}]
    assert convergence_signals(reversals)['repeated_reversal'] is True


def test_concurrent_file_edit_and_git_timeout_report_unknown(tmp_path, monkeypatch):
    from llm_tools import bridge_progress as progress
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    path = tmp_path/'file'
    path.write_text('original')
    inode = path.stat().st_ino
    original_fdopen = progress.os.fdopen
    def concurrent_edit(fd, *args, **kwargs):
        stream = original_fdopen(fd,*args,**kwargs)
        if progress.os.fstat(fd).st_ino == inode:
            path.write_text('concurrent replacement contents')
        return stream
    monkeypatch.setattr(progress.os,'fdopen',concurrent_edit)
    assert progress.repository_checkpoint(tmp_path)['fingerprint'] is None
    monkeypatch.setattr(progress.os,'fdopen',original_fdopen)
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired('git',2)
    monkeypatch.setattr(progress.subprocess,'run',timeout)
    assert progress.repository_checkpoint(tmp_path)['fingerprint'] is None


def test_deleted_tracked_file_and_large_file_budget_are_observable(tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    path = tmp_path/'tracked'
    path.write_text('content')
    subprocess.run(['git','-C',str(tmp_path),'add','tracked'],check=True)
    before = repository_checkpoint(tmp_path)
    path.unlink()
    after = repository_checkpoint(tmp_path)
    assert before['fingerprint'] != after['fingerprint']
    assert after['files'] == 0
    (tmp_path/'large').write_bytes(b'x'*65540)
    assert repository_checkpoint(tmp_path,max_bytes=65536)['fingerprint'] is None
