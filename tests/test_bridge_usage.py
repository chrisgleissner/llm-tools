"""Embedded usage reads never block Bridge input or spawn llm-usage."""
import threading
import time
from llm_tools.bridge_usage import EmbeddedUsage


def wait(predicate):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate()


def test_slow_usage_single_inflight_cache_and_failure_preserves_prior_result():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def collect():
        calls.append(1); entered.set(); release.wait(2)
        return {'text':'full dashboard\n','rows':[{'provider':'Codex'}],'observed_at':'now'}
    view = EmbeddedUsage(collector=collect, interval=5)
    started = time.monotonic()
    first = view.snapshot(now=0)
    assert time.monotonic()-started < .1
    assert entered.wait(1)
    assert first['status'] == 'refreshing'
    for _ in range(20): view.snapshot(now=1)
    assert len(calls) == 1
    release.set()
    wait(lambda: view.snapshot(now=1)['status'] == 'ready')
    assert view.snapshot(now=4)['text'] == 'full dashboard\n'
    assert len(calls) == 1
    def fail(): raise RuntimeError('secret-looking payload must not be displayed')
    view.collector = fail
    view.snapshot(now=6)
    wait(lambda: view.snapshot(now=7)['status'] == 'unavailable')
    stale = view.snapshot(now=7)
    assert stale['text'] == 'full dashboard\n'
    assert stale['error'] == 'RuntimeError'
    assert stale['age_seconds'] == 7


def test_configuration_exit_and_invalid_dashboard_are_visible_failures():
    import pytest
    for failure in [lambda: (_ for _ in ()).throw(SystemExit(2)), lambda: {'text':None,'rows':[]}]:
        view = EmbeddedUsage(collector=failure, interval=5)
        view.snapshot(now=0)
        wait(lambda: view.snapshot(now=1)['status']=='unavailable')
        assert view.snapshot(now=1)['error'] in {'SystemExit','ValueError'}
    with pytest.raises(ValueError): EmbeddedUsage(interval=1)


def test_usage_collector_reuses_public_api_without_subprocess(monkeypatch):
    from llm_tools import usage
    from llm_tools.bridge_usage import collect_usage
    observed = []
    def snapshot(cfg):
        observed.append(cfg)
        return {'text':'full output','rows':[]}
    monkeypatch.setattr(usage,'dashboard_snapshot',snapshot)
    assert collect_usage() == {'text':'full output','rows':[]}
    assert observed[0].show_remaining_time and not observed[0].show_source  # the cache-path column made rows ~170 cells wide
    assert observed[0].show_copilot_credits
    assert not observed[0].progress_enabled and not observed[0].color_enabled
