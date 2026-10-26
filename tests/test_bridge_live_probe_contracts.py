"""The live scripts' evidence oracles run in CI; paid CLI probes never do."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_oracle_rejects_prompt_echo_and_substring():
    smoke = load('bridge_provider_smoke')
    assert not smoke.response_seen('Reply only BRIDGE_SMOKE_OK please', 'BRIDGE_SMOKE_OK')
    assert not smoke.response_seen('BRIDGE_SMOKE_OK_BUT_FAILED', 'BRIDGE_SMOKE_OK')
    assert smoke.response_seen('• BRIDGE_SMOKE_OK\n', 'BRIDGE_SMOKE_OK')
    assert smoke.response_seen('BRIDGE_SMOKE_OK\n', 'BRIDGE_SMOKE_OK')
    # A resumed screen containing old conversation text is insufficient.
    assert not smoke.response_seen('• BRIDGE_SMOKE_OK\n', 'BRIDGE_SMOKE_OK_RESUMED')
    assert smoke.response_seen('• BRIDGE_SMOKE_OK_RESUMED', 'BRIDGE_SMOKE_OK_RESUMED')


def test_mouse_oracle_requires_specific_native_effect():
    smoke = load('bridge_native_mouse_probe')
    assert smoke.mouse_result('Select Model', 'Select Model').startswith('NOT VERIFIED')
    assert smoke.mouse_result('loading', 'Select Reasoning Effort').startswith('NOT VERIFIED')
    assert smoke.mouse_result('Select Model', 'Select Reasoning Effort').startswith('PASS')


def test_native_mission_oracle_rejects_echo_and_uncertain_probe_keeps_metadata():
    import shutil
    smoke = load('bridge_native_mission_smoke')
    assert not smoke.answer_seen('Reply only BRIDGE_NATIVE_OK', 'BRIDGE_NATIVE_OK')
    assert not smoke.answer_seen('• BRIDGE_NATIVE_OK', 'BRIDGE_NATIVE_OK_RESUMED')
    assert smoke.answer_seen('• BRIDGE_NATIVE_OK_RESUMED', 'BRIDGE_NATIVE_OK_RESUMED')
    failed = {'cleanup':'FAILED'}
    with smoke.probe_workspace(failed) as root:
        (root/'ownership').write_text('fixture start token')
    try:
        assert root.exists()
        assert failed['retained_state'] == str(root)
        assert (root/'ownership').read_text() == 'fixture start token'
        assert (root/'probe-result.json').exists()
    finally:
        shutil.rmtree(root)
    passed = {'cleanup':'PASS'}
    with smoke.probe_workspace(passed) as root:
        (root/'ownership').write_text('fixture')
    assert not root.exists()
