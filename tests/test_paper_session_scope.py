from __future__ import annotations

import copy
import json

import pytest

from pumpwizard_runner.forward_campaigns import ForwardCampaignStore
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.paper_research import PaperResearchError, PaperResearchStore, START_CONFIRMATION
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.web_api import command, payload


def session(library, *, style=None, legacy=False):
    selected = parse_package({
        'schema': 'pumpwizard.strategy-handoff/v1',
        'strategy': {'id': 'scope-fixture', 'label': 'Scope fixture'},
        'configuration': copy.deepcopy(CONFIG),
    })
    library.import_strategy(selected)
    brief = dict(goal='collect-evidence', intent='Check monitoring session scope.',
                 paper_budget_usd=500, max_session_loss_usd=100,
                 review_after_seconds=86400)
    if style:
        brief.update(session_mode='trade', execution_style=style,
                     profit_management='user-directed' if style == 'interactive' else 'strategy-managed')
    ident = library.create_instance(selected.digest, brief=brief)
    if legacy:
        saved = library.session_brief(ident)
        saved.pop('session_mode')
        if not style:
            saved['mode'] = 'observe-only'
        with library._connection:
            library._connection.execute('UPDATE session_briefs SET payload_json=? WHERE instance_id=?',
                                        (json.dumps(saved), ident))
    return ident, selected


def start_request(ident, selected, *, legacy=False):
    body = dict(source_session_id=ident, strategy_sha256=selected.digest,
                idempotency_key='scope-start', confirmation=START_CONFIRMATION)
    if legacy:
        body.update(simulated_capital_usd=500, start_at=1000)
    else:
        body.update(schema='pumpwizard.paper-start-request/v1', starting_capital_usd=500)
    return body


def paper_command(library, run, action):
    return command(library, f"/api/v1/research/paper-runs/{run['id']}/{action}", {
        'schema': 'pumpwizard.paper-command/v1', 'run_id': run['id'],
        'command': action, 'idempotency_key': f'scope-{action}',
    })['paper_run']


@pytest.mark.parametrize('style', ['interactive', 'autopilot'])
@pytest.mark.parametrize('legacy', [False, True])
def test_trading_sources_reject_paper_start_and_resume_preserving_old_history(tmp_path, style, legacy):
    library = StrategyLibrary(tmp_path)
    try:
        ident, selected = session(library, style=style, legacy=legacy)
        saved_brief = library.session_brief(ident)
        for path in (f'/api/v1/sessions/{ident}', f'/api/v1/research/sessions/{ident}'):
            assert payload(library, path, {})['paper_research_allowed'] is False
        assert payload(library, '/api/v1/sessions', {})['rows'][0]['paper_research_allowed'] is False
        for old_request in (False, True):
            with pytest.raises(PaperResearchError, match='only in monitoring sessions') as rejected:
                command(library, '/api/v1/research/paper-runs', start_request(ident, selected, legacy=old_request))
            assert rejected.value.code == 'monitoring_session_required'
        paper = PaperResearchStore(tmp_path)
        try:
            assert paper.runs() == []
            # A pre-upgrade run stays readable and can be paused/stopped, but cannot resume.
            run = paper.start(source_session_id=ident, strategy_package=library.instance(ident)['package'],
                              strategy_sha256=selected.digest, simulated_capital_usd=500,
                              start_at=1000, idempotency_key='old-run', confirmation=START_CONFIRMATION)
            assert paper_command(library, run, 'pause')['state'] == 'paused'
            history = paper.history(run['id'])
            with pytest.raises(PaperResearchError, match='only in monitoring sessions'):
                paper_command(library, run, 'resume')
            assert paper.history(run['id']) == history
            assert paper_command(library, run, 'stop')['state'] == 'stopped'
            assert len(paper.runs()) == 1
        finally:
            paper.close()
        assert library.session_brief(ident) == saved_brief
    finally:
        library.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_monitoring_sources_retain_paper_start_and_resume(tmp_path, legacy):
    library = StrategyLibrary(tmp_path)
    try:
        ident, selected = session(library, legacy=legacy)
        assert payload(library, f'/api/v1/research/sessions/{ident}', {})['paper_research_allowed'] is True
        run = command(library, '/api/v1/research/paper-runs', start_request(ident, selected))['paper_run']
        assert run['state'] == 'running'
        assert paper_command(library, run, 'pause')['state'] == 'paused'
        assert paper_command(library, run, 'resume')['state'] == 'running'
    finally:
        library.close()


def test_owning_trading_session_overrides_an_older_monitor_brief(tmp_path):
    library = StrategyLibrary(tmp_path)
    try:
        ident, selected = session(library, style='interactive')
        workspace = library.create_session_workspace('trade', allocation_lamports='10000000',
                                                     wallet_sender='fixture-wallet', wallet_network='devnet')
        library.assign_instance_to_workspace(workspace['id'], ident)
        # Model an older/mismatched brief without modifying its actual funded workspace.
        brief = library.session_brief(ident)
        brief.update(session_mode='monitoring', mode='observe-only')
        with library._connection:
            library._connection.execute('UPDATE session_briefs SET payload_json=? WHERE instance_id=?',
                                        (json.dumps(brief), ident))
        with pytest.raises(PaperResearchError, match='only in monitoring sessions'):
            command(library, '/api/v1/research/paper-runs', start_request(ident, selected))
        assert library.session_workspace(workspace['id'])['allocation_lamports'] == '10000000'
    finally:
        library.close()


def test_campaign_start_and_resume_reject_trading_arms(tmp_path):
    from test_forward_campaigns import request

    library = StrategyLibrary(tmp_path)
    campaigns = ForwardCampaignStore(tmp_path)
    try:
        ident, selected = session(library, style='autopilot')
        manifest = request()
        manifest['source']['provider'] = 'CompositePumpMarketSource'
        manifest['arms'] = [dict(id='trade-arm', source_session_id=ident,
                                 strategy_sha256=selected.digest, simulated_capital_usd=500)]
        with pytest.raises(PaperResearchError, match='only in monitoring sessions'):
            command(library, '/api/v1/research/campaigns', {
                'schema': 'pumpwizard.forward-campaign-start/v1', 'campaign': manifest,
                'confirmation': 'START FORWARD CAMPAIGN', 'idempotency_key': 'scope-campaign',
            })
        # Preserve an older campaign and permit cleanup while blocking new collection.
        manifest['arms'][0]['strategy_package'] = library.instance(ident)['package']
        old = campaigns.start(manifest, confirmation='START FORWARD CAMPAIGN', idempotency_key='old-campaign')
        campaigns.command(old['id'], action='pause', idempotency_key='old-pause')
        with pytest.raises(PaperResearchError, match='only in monitoring sessions'):
            command(library, f"/api/v1/research/campaigns/{old['id']}/resume", {
                'schema': 'pumpwizard.forward-campaign-command/v1', 'campaign_id': old['id'],
                'command': 'resume', 'idempotency_key': 'old-resume',
            })
        assert campaigns.campaign(old['id'])['state'] == 'paused'
    finally:
        campaigns.close()
        library.close()
