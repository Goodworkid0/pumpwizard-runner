import pytest

from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.strategy_creator import draft_package


def test_local_draft_uses_the_existing_handoff_contract_and_paper_rules():
    package = draft_package('My breakout', 'my-breakout', 'breakout', 20_000, 50_000, 25, 250)
    strategy = parse_package(package)
    assert strategy.schema == 'pumpwizard.strategy-handoff/v1'
    assert strategy.compatibility == 'monitoring-ready'
    assert strategy.package['configuration']['mode'] == 'paper_only'
    assert strategy.package['configuration']['targets'][0]['market_cap_usd'] == 60_000


@pytest.mark.parametrize(('goal', 'entry_style', 'max_hold_seconds', 'stop_loss_fraction'), (
    ('liquidity-first', 'liquidity', 259_200, -.08),
    ('swing-trader', 'higher-low', 259_200, -.15),
))
def test_preference_templates_resolve_to_existing_verifiable_fields(
        goal, entry_style, max_hold_seconds, stop_loss_fraction):
    package = draft_package(
        f'{goal} research', f'{goal}-research', entry_style,
        20_000, 50_000, 10, 500, goal,
    )
    strategy = parse_package(package)
    config = strategy.package['configuration']
    assert strategy.compatibility == 'monitoring-ready'
    assert config['entry_style'] == entry_style
    assert config['max_hold_seconds'] == max_hold_seconds
    assert config['stop_loss_fraction'] == stop_loss_fraction
    assert len(config['targets']) == 3


@pytest.mark.parametrize('values', ((50_000, 20_000, 25, 250), (20_000, 50_000, 0, 250),
                                    (20_000, 50_000, 250, 25)))
def test_local_draft_rejects_invalid_paper_boundaries(values):
    with pytest.raises(ValueError):
        draft_package('My strategy', 'my-strategy', 'momentum', *values)
