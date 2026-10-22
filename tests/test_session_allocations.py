import json
import sqlite3
import threading
import time

import pytest

from pumpwizard_runner import main_wallet_autopilot, session_allocations, strategy_risk
from pumpwizard_runner.agent_execution import SCHEMA as AGENT_SCHEMA
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.reservations import release
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.wallet_vault import WalletVault


def limits(**changes):
    return {
        **{
            "allocation_lamports": 70_000_000,
            "buys_enabled": True,
            "sells_enabled": True,
            "min_trade_lamports": 1,
            "max_trade_lamports": 50_000_000,
            "max_daily_buy_lamports": 60_000_000,
            "max_trades_per_day": 4,
            "max_daily_loss_lamports": 10_000_000,
            "max_daily_loss_bps": 2_000,
            "max_trade_loss_bps": 2_000,
            "max_open_positions": 2,
        },
        **changes,
    }


@pytest.fixture
def allocation(tmp_path):
    wallet = WalletVault(tmp_path / "wallet").create("fixture wallet password")
    journal = TradeJournal(tmp_path / "journal")
    journal.db.executescript(AGENT_SCHEMA)
    session_allocations.install(journal)
    current = session_allocations.reserve_workspace(
        journal,
        "workspace-one",
        GENESIS["mainnet-beta"],
        wallet.address,
        100_000_000,
        100_000_000,
    )
    yield wallet, journal, current
    journal.close()


def test_two_strategies_share_one_hold_and_removal_never_releases_the_other(allocation):
    wallet, journal, current = allocation
    for agent in ("strategy-one", "strategy-two"):
        saved = strategy_risk.save_policy(
            journal,
            agent,
            GENESIS["mainnet-beta"],
            wallet.address,
            limits(),
            100_000_000,
            workspace_id=current["workspace_id"],
        )
        assert saved["hold_id"] == current["cash_hold"]
    assert journal.db.execute(
        "SELECT COUNT(*) FROM agent_reservations WHERE status='held' AND position_id IS NULL"
    ).fetchone()[0] == 1
    assert session_allocations.unbind_strategy(journal, "strategy-one") is True
    assert session_allocations.binding(
        journal, "strategy-two", GENESIS["mainnet-beta"], wallet.address,
    )["cash_hold"] == current["cash_hold"]
    with pytest.raises(TransferError, match="Remove this session's strategies"):
        session_allocations.release_workspace(journal, current["workspace_id"])
    assert journal.db.execute(
        "SELECT status FROM agent_reservations WHERE id=?", (current["cash_hold"],),
    ).fetchone()[0] == "held"


def test_concurrent_workspace_consumers_cannot_oversubscribe_wallet(tmp_path):
    wallet = WalletVault(tmp_path / "wallet").create("fixture wallet password")
    directory = tmp_path / "journal"
    initialized = TradeJournal(directory)
    session_allocations.install(initialized)
    database = initialized.db.execute("PRAGMA database_list").fetchone()[2]
    initialized.close()
    ready = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def consume(workspace_id: str) -> None:
        class ConcurrentJournal:
            def __init__(self):
                self.db = sqlite3.connect(database, timeout=10)
                self.db.row_factory = sqlite3.Row

            def pending(self, sender, genesis):
                return self.db.execute(
                    "SELECT 1 FROM wallet_intents WHERE sender=? AND genesis=?",
                    (sender, genesis),
                ).fetchone() is not None

            def close(self):
                self.db.close()

        journal = ConcurrentJournal()
        try:
            ready.wait()
            try:
                session_allocations.reserve_workspace(
                    journal,
                    workspace_id,
                    GENESIS["mainnet-beta"],
                    wallet.address,
                    70_000_000,
                    100_000_000,
                )
                results.append((workspace_id, "held"))
            except TransferError as exc:
                results.append((workspace_id, str(exc)))
        finally:
            journal.close()

    threads = [threading.Thread(target=consume, args=(f"workspace-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert sum(result == "held" for _, result in results) == 1
    journal = TradeJournal(directory)
    try:
        assert journal.db.execute(
            "SELECT COALESCE(SUM(CAST(lamports AS INTEGER)),0) FROM agent_reservations "
            "WHERE status='held' AND position_id IS NULL"
        ).fetchone()[0] == 70_000_000
    finally:
        journal.close()


def test_managed_position_requires_explicit_handoff_before_strategy_unbind(allocation):
    wallet, journal, current = allocation
    agent = "strategy-managed"
    strategy_risk.save_policy(
        journal,
        agent,
        GENESIS["mainnet-beta"],
        wallet.address,
        limits(max_open_positions=1),
        100_000_000,
        workspace_id=current["workspace_id"],
    )
    now = time.time()
    position_id = "position-one"
    token_hold = "token-hold-one"
    with journal.db:
        journal.db.execute(
            "INSERT INTO trade_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                position_id, GENESIS["mainnet-beta"], wallet.address, "fixture-mint", now,
                "10", "10", "100", "100", "0", "0", "open", "fixture-signature",
            ),
        )
        journal.db.execute(
            "INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                token_hold, agent, GENESIS["mainnet-beta"], wallet.address, "0",
                position_id, "fixture-mint", "10", now, None, "held",
            ),
        )
        journal.db.execute(
            "INSERT INTO agent_positions VALUES (?,?,?,?,?,?,?)",
            (position_id, agent, token_hold, 100.0, "[]", 1.0, now),
        )
    with pytest.raises(TransferError, match="explicitly hand back"):
        session_allocations.unbind_strategy(journal, agent)
    managed_summary = session_allocations.summary(journal, current["workspace_id"])
    managed_metrics = strategy_risk.metrics(
        journal, agent, GENESIS["mainnet-beta"], wallet.address,
    )
    assert managed_summary["committed_lamports"] == "100"
    assert managed_summary["attributed_open_cost_lamports"] == "100"
    assert managed_metrics["open_positions"] == 1
    assert managed_metrics["attributed_open_positions"] == 1

    release(journal, token_hold)
    handed_back_summary = session_allocations.summary(journal, current["workspace_id"])
    handed_back_metrics = strategy_risk.metrics(
        journal, agent, GENESIS["mainnet-beta"], wallet.address,
    )
    assert handed_back_summary["available_lamports"] == "100000000"
    assert handed_back_summary["committed_lamports"] == "0"
    assert handed_back_summary["attributed_open_cost_lamports"] == "100"
    assert handed_back_metrics["open_positions"] == 0
    assert handed_back_metrics["open_cost_lamports"] == 0
    assert handed_back_metrics["attributed_open_positions"] == 1
    assert handed_back_metrics["attributed_open_cost_lamports"] == 100
    assert "Strategy open-position limit reached" not in strategy_risk.status(
        journal, agent, GENESIS["mainnet-beta"], wallet.address,
    )["entry_blocks"]

    assert session_allocations.unbind_strategy(journal, agent) is True
    assert session_allocations.release_workspace(journal, current["workspace_id"]) == 100_000_000


def test_active_allowance_prevents_workspace_cash_release(allocation):
    wallet, journal, current = allocation
    main_wallet_autopilot.install(journal)
    scope = json.dumps({"cash_hold": current["cash_hold"]})
    with journal.db:
        journal.db.execute(
            "INSERT INTO main_wallet_allowances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "11" * 16, "22" * 16, "strategy-one", GENESIS["mainnet-beta"],
                wallet.address, "33" * 32, 1, 1, "{}", "{}", scope,
                time.time(), time.time() + 60, "authorized", 0, "fixture",
            ),
        )
    with pytest.raises(TransferError, match="Revoke this session's main-wallet allowance"):
        session_allocations.release_workspace(journal, current["workspace_id"])
    with journal.db:
        journal.db.execute(
            "UPDATE main_wallet_allowances SET status='revoked' WHERE id=?", ("11" * 16,),
        )
    assert session_allocations.release_workspace(journal, current["workspace_id"]) == 100_000_000
