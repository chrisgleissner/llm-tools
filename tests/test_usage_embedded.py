"""The embedded dashboard is the same renderer as standalone llm-usage."""
from io import StringIO
from contextlib import redirect_stdout
from llm_tools import usage


def test_embedded_dashboard_matches_cli_without_redirecting_global_stdout(monkeypatch):
    cfg = usage.parse_args(['--no-service', '--show-source', '--show-remaining-time'])
    cfg.color_enabled = False
    rows = [usage.UsageRow('Codex', '5h', 30, '30%', 2000000000, 'fixture', '38m'),
            usage.UsageRow('Claude', 'weekly', 50, '50%', 2000100000, 'fixture', '2h')]
    monkeypatch.setattr(usage, '_build_usage_rows', lambda cfg, data: (rows, False))
    provider_data = {}
    output = StringIO()
    with redirect_stdout(output):
        usage.render_from_provider_data(cfg, provider_data)
    embedded = usage.dashboard_snapshot(cfg, provider_data=provider_data)
    assert embedded['text'] == output.getvalue()
    assert embedded['rows'][0]['estimated_remaining'] == '38m'
    assert embedded['rows'][1]['scope'] == 'weekly'
    assert embedded['rows'][1]['estimated_remaining'] == '2h'


def test_embedded_missing_estimate_is_explicit_and_opaque_has_no_fake_quota(monkeypatch):
    cfg = usage.parse_args(['--no-service'])
    rows = [usage.UsageRow('opaque', 'subscription', None, 'prepaid $20/mo', None, 'fixture', '-'),
            usage.UsageRow('Codex', 'weekly', 75, '75%', None, 'fixture', '-')]
    monkeypatch.setattr(usage, '_build_usage_rows', lambda cfg, data: (rows, False))
    result = usage.dashboard_snapshot(cfg, provider_data={})
    assert result['rows'][0]['remaining_percent'] is None
    assert result['rows'][0]['estimated_remaining'] is None
    assert result['rows'][1]['estimated_remaining'] is None


def test_embedded_dashboard_never_starts_an_ephemeral_usage_process(monkeypatch):
    from llm_tools import usage_service
    cfg = usage.Config()
    cfg.use_service = True
    starts = []
    def snapshot(*,env,start_ephemeral):
        starts.append(start_ephemeral)
        return None
    monkeypatch.setattr(usage_service,'request_snapshot',snapshot)
    monkeypatch.setattr(usage,'_fetch_provider_data',lambda cfg,**kwargs:{})
    monkeypatch.setattr(usage,'_build_usage_rows',lambda cfg,data:([],False))
    assert 'LLM Usage' in usage.dashboard_snapshot(cfg)['text']
    assert starts == [False]
