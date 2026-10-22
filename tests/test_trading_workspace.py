"""Guided setup and recovery use the real adapter with synthetic bank balances."""

import time

import pytest

from pumpwizard_runner import trading_workspace as workspace
from pumpwizard_runner.transfers import GENESIS, TransferError
from test_agent_execution import MINT, setup as _setup


@pytest.fixture
def setup(tmp_path):
    generator = _setup.__wrapped__(tmp_path)
    data = next(generator)
    rpc, journal = data[1], data[2]
    rpc.network = "devnet"
    rpc.genesis = lambda: GENESIS["devnet"]
    with journal.db:
        journal.db.execute("UPDATE agent_reservations SET genesis=?", (GENESIS["devnet"],))
    yield data
    next(generator, None)


def test_save_setup_before_deposit_never_allocates_or_signs(setup):
    wallet, rpc, journal, _, agent, cash, _, directory = setup
    workspace.release_allocation(directory, rpc, agent, wallet.address, cash["id"])
    rpc.balance = 0
    values = dict(network="devnet", allocation="0.200000000", slippage_bps=75)
    workspace.preferences(directory, agent, wallet.address, values)
    assert workspace.preferences(directory, agent, wallet.address)["allocation"] == "0.2"
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["balance"] == 0 and result["free_cash"] == []
    assert (
        next(c["state"] for c in result["checks"] if c["name"] == "Funding") == "Awaiting deposit"
    )
    with pytest.raises(TransferError, match="balance"):
        workspace.allocate(directory, rpc, agent, wallet.address, "0.2")
    assert (
        not rpc.sent and journal.db.execute("SELECT COUNT(*) FROM agent_orders").fetchone()[0] == 0
    )


def test_rpc_failure_keeps_local_history_and_does_not_invent_balance(setup, monkeypatch):
    wallet, rpc, _, _, agent, _, _, directory = setup
    monkeypatch.setattr(rpc, "genesis", lambda: (_ for _ in ()).throw(TransferError("offline")))
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["balance"] is None and result["available"] is None and result["rpc_error"]
    assert len(result["holds"]) == 1 and len(result["candidates"]) == 1
    assert not rpc.sent
    assert "Connection — Unavailable" in workspace.operations_text(result)


def test_wrong_network_never_reports_ready_and_hides_other_network_orders(setup):
    wallet, rpc, _, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    rpc.network = "mainnet-beta"
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["balance"] is None and result["holds"] == [] and result["orders"] == []
    with pytest.raises(TransferError, match="another agent, wallet or network"):
        workspace.order(directory, rpc, agent, wallet.address, p["id"], action="cancel")


def test_review_cancel_restart_and_finalized_position_accounting(setup):
    wallet, rpc, journal, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    state = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert state["free_cash"] == [] and state["orders"][0]["display_status"] == "review"
    workspace.order(directory, rpc, agent, wallet.address, p["id"], action="cancel")
    assert len(workspace.snapshot(directory, rpc, agent, wallet.address)["free_cash"]) == 1
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    workspace.order(directory, rpc, agent, wallet.address, p["id"], action="submit", wallet=wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["positions"][0]["quantity"] == "799000000"
    assert result["orders"][0]["display_status"] == "finalized" and not result["pending"]
    assert "UNRESOLVED WALLET OPERATION" not in workspace.operations_text(result)
    assert journal.db.execute("SELECT COUNT(*) FROM agent_fills").fetchone()[0] == 1
    workspace.snapshot(directory, rpc, agent, wallet.address)
    assert journal.db.execute("SELECT COUNT(*) FROM agent_fills").fetchone()[0] == 1


def test_expired_review_remains_bound_until_cancelled(setup, monkeypatch):
    wallet, rpc, _, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    now = time.time()
    monkeypatch.setattr("pumpwizard_runner.trading_workspace.time.time", lambda: now + 61)
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["orders"][0]["display_status"] == "expired review" and result["free_cash"] == []
    assert "EXPIRED UNSIGNED REVIEW" in workspace.operations_text(result)
    with pytest.raises(TransferError):
        workspace.order(
            directory, rpc, agent, wallet.address, p["id"], action="submit", wallet=wallet
        )
    assert not rpc.sent


def test_cross_agent_cannot_cancel_or_release_another_agents_order(setup):
    wallet, rpc, _, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    with pytest.raises(TransferError, match="another agent"):
        workspace.order(directory, rpc, "other-agent", wallet.address, p["id"], action="cancel")
    with pytest.raises(TransferError, match="belonging to this agent"):
        workspace.release_allocation(directory, rpc, "other-agent", wallet.address, cash["id"])
    assert not rpc.sent


def test_pending_unknown_submission_cannot_be_cancelled_or_released(setup):
    wallet, rpc, _, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    rpc.disconnect = True
    workspace.order(directory, rpc, agent, wallet.address, p["id"], action="submit", wallet=wallet)
    result = workspace.snapshot(directory, rpc, agent, wallet.address)
    assert result["pending"] and result["orders"][0]["display_status"] == "unknown"
    assert "UNRESOLVED WALLET OPERATION" in workspace.operations_text(result)
    assert "original signature" in workspace.operations_text(result)
    assert not result["free_cash"]
    with pytest.raises(TransferError):
        workspace.order(directory, rpc, agent, wallet.address, p["id"], action="cancel")
    with pytest.raises(TransferError):
        workspace.release_allocation(directory, rpc, agent, wallet.address, cash["id"])
    assert len(rpc.sent) == 1


def test_enabling_reviewed_entries_does_not_resume_paused_tracking(setup):
    wallet, rpc, _, library, agent, _, _, directory = setup
    library.set_tracking(agent, False)
    workspace.enable_reviewed_entries(directory, rpc, agent)
    assert library.instance(agent)["entry_mode"] == "manual"
    assert library.instance(agent)["status"] == "paused"
    assert not library.tracking_settings(agent)["enabled"] and not rpc.sent


def test_test_setup_reuses_pinned_agent_and_creates_only_disabled_agent(tmp_path):
    from pumpwizard_runner.library import StrategyLibrary

    library = StrategyLibrary(tmp_path)
    try:
        agent = workspace.test_agent(library)
        assert workspace.test_agent(library) == agent
        assert len(library.instances()) == 1
        assert library.instance(agent)["entry_mode"] == "disabled"
        assert not library.tracking_settings(agent)["enabled"]
        assert not (tmp_path / "wallet-transfers.sqlite").exists()
    finally:
        library.close()


def test_sidebar_reads_committed_positions_without_a_writer_lock(setup):
    wallet, rpc, journal, _, agent, cash, _, directory = setup
    p = workspace.prepare(directory, rpc, agent, wallet.address, cash["id"], MINT, None, 100)
    workspace.order(directory, rpc, agent, wallet.address, p["id"], action="submit", wallet=wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    workspace.snapshot(directory, rpc, agent, wallet.address)
    try:
        journal.db.execute("BEGIN IMMEDIATE")
        journal.db.execute("UPDATE trade_positions SET quantity='1'")
        result = workspace.wallet_positions(directory, wallet.address)
        assert result[0]["quantity"] == "799000000" and result[0]["agent_id"] == agent
    finally:
        journal.db.rollback()


def test_sidebar_before_setup_does_not_create_a_wallet_journal(tmp_path):
    assert workspace.wallet_positions(tmp_path, "public-address") == []
    assert list(tmp_path.iterdir()) == []
