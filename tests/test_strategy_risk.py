"""Per-strategy allocations and limits use synthetic wallet state only."""

import pytest

from pumpwizard_runner import strategy_risk
from pumpwizard_runner.transfers import TransferError
from test_agent_execution import setup as _agent_setup, prepare_buy


@pytest.fixture
def setup(tmp_path):
    yield from _agent_setup.__wrapped__(tmp_path)


def limits(**changes):
    values = {
        "allocation_lamports": 200_000_000,
        "buys_enabled": True,
        "sells_enabled": True,
        "min_trade_lamports": 10_000_000,
        "max_trade_lamports": 150_000_000,
        "max_daily_buy_lamports": 200_000_000,
        "max_trades_per_day": 5,
        "max_daily_loss_lamports": 50_000_000,
        "max_daily_loss_bps": 1_000,
        "max_trade_loss_bps": 2_000,
        "max_open_positions": 3,
    }
    return dict(values, **changes)


def test_policy_reuses_agent_cash_hold_and_reports_main_metrics(setup):
    wallet, rpc, journal, _, agent, cash, _, _ = setup
    saved = strategy_risk.save_policy(
        journal, agent, rpc.genesis(), wallet.address, limits(allocation_lamports=250_000_000),
        balance=3_000_000_000,
    )
    assert saved["hold_id"] == cash["id"]
    current = strategy_risk.status(journal, agent, rpc.genesis(), wallet.address)
    assert current["policy"]["max_trade_loss_bps"] == 2_000
    assert current["metrics"]["allocated_lamports"] == 250_000_000
    assert current["metrics"]["daily_trades"] == 0


def test_strategy_buy_limits_are_checked_during_review(setup):
    wallet, rpc, journal, _, agent, _, _, _ = setup
    strategy_risk.save_policy(
        journal, agent, rpc.genesis(), wallet.address, limits(max_trade_lamports=50_000_000),
        balance=3_000_000_000,
    )
    with pytest.raises(TransferError, match="strategy minimum/maximum"):
        prepare_buy(setup)
    assert not journal.db.execute("SELECT 1 FROM agent_orders").fetchone()


def test_disabled_buys_block_strategy_orders_but_manual_exit_path_is_separate(setup):
    wallet, rpc, journal, _, agent, _, _, _ = setup
    strategy_risk.save_policy(
        journal, agent, rpc.genesis(), wallet.address, limits(buys_enabled=False),
        balance=3_000_000_000,
    )
    with pytest.raises(TransferError, match="buys are disabled"):
        prepare_buy(setup)


def test_allocations_cannot_exceed_main_wallet_balance(setup):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    other = library.create_instance(library.instance(agent)["strategy_digest"], entry_mode="manual")
    strategy_risk.save_policy(
        journal, agent, rpc.genesis(), wallet.address, limits(allocation_lamports=1_900_000_000),
        balance=3_000_000_000,
    )
    with pytest.raises(TransferError, match="exceed the main wallet"):
        strategy_risk.save_policy(
            journal, other, rpc.genesis(), wallet.address,
            limits(allocation_lamports=1_200_000_000), balance=3_000_000_000,
        )


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"min_trade_lamports": 160_000_000}, "Minimum trade"),
        ({"max_trade_lamports": 210_000_000}, "Maximum trade"),
        ({"max_daily_buy_lamports": 210_000_000}, "Daily buy cap"),
        ({"max_daily_loss_bps": 0}, "percentage limits"),
        ({"max_trades_per_day": 101}, "count limits"),
    ],
)
def test_policy_validation_rejects_unsafe_or_inconsistent_values(changes, message):
    with pytest.raises(TransferError, match=message):
        strategy_risk.validate(limits(**changes))


def test_browser_policy_converts_exact_sol_and_percent_values():
    parsed = strategy_risk.parse_browser_policy({
        "allocation_sol": "0.2", "buys_enabled": True, "sells_enabled": True,
        "min_trade_sol": "0.005", "max_trade_sol": "0.02",
        "max_daily_buy_sol": "0.05", "max_trades_per_day": 5,
        "max_daily_loss_sol": "0.01", "max_daily_loss_percent": "7.5",
        "max_trade_loss_percent": "12.25", "max_open_positions": 3,
    })
    assert parsed["allocation_lamports"] == 200_000_000
    assert parsed["max_daily_loss_bps"] == 750
    assert parsed["max_trade_loss_bps"] == 1_225
