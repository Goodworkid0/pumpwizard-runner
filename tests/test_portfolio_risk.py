"""Portfolio gates exercised through real unsigned builders and synthetic receipts."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from pumpwizard_runner import portfolio_risk as portfolio
from pumpwizard_runner import wallet_risk
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.transfers import TransferError
from test_manual_trades import MINT, buy
from test_wallet_risk import configure, sell, setup as _setup


@pytest.fixture
def setup(tmp_path):
    yield from _setup.__wrapped__(tmp_path)


def test_unrealized_loss_blocks_buy_while_paused_full_exit_remains_available(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    configure(setup, portfolio_drawdown_lamports=20_000_000)
    with pytest.raises(TransferError, match="Portfolio drawdown limit"):
        service.preview(wallet.address, MINT, "buy", "0.1")
    result = portfolio.current(journal, rpc.genesis(), wallet.address)
    # 0.1 SOL gross * .95, less fee and .02 SOL conservative account allowance.
    assert result["net_liquidation"] == 74_995_000
    assert result["unrealized_pnl"] == -27_049_280
    assert result["drawdown"] == 27_049_280
    assert len(rpc.sent) == 1
    configure(setup, entries_paused=True, portfolio_drawdown_lamports=20_000_000)
    sell(setup, p["id"])
    assert journal.position(p["id"])["status"] == "closed"


def test_same_mint_lots_use_one_aggregate_quote_and_do_not_change_financial_journal(setup):
    wallet, rpc, journal, _, _ = setup
    buy(setup)
    buy(setup)
    before = list(journal.db.iterdump())
    result = portfolio.refresh(journal, rpc, wallet.address)
    assert result["status"] == "ready"
    assert len(result["quotes"]) == 1
    assert result["quotes"][0]["quantity"] == 2_000_000_000
    assert result["net_liquidation"] == 169_995_000
    assert list(journal.db.iterdump()) == before
    assert len(rpc.sent) == 2


def test_portfolio_peak_survives_restart_policy_changes_and_deposits(setup):
    wallet, rpc, journal, _, directory = setup
    buy(setup)
    effects = rpc.effects
    rpc.effects = (
        lambda amount, buying: effects(amount, buying)
        if buying
        else (200_000_000 - rpc.fee, -amount)
    )
    first = portfolio.refresh(journal, rpc, wallet.address)
    assert first["peak"] == 67_950_720
    assert first["drawdown"] == 0
    rpc.effects = effects
    rpc.balance += 1_000_000_000  # External deposit is not trading profit.
    reopened = TradeJournal(directory)
    try:
        configure(setup)
        second = portfolio.refresh(reopened, rpc, wallet.address)
        assert second["peak"] == first["peak"]
        assert second["drawdown"] == 95_000_000
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "failure", ["missing_tokens", "bad_simulation", "unsupported_migration", "expired_block"]
)
def test_unavailable_valuation_replaces_old_success_and_preserves_exits(
    setup, failure, monkeypatch
):
    wallet, rpc, journal, service, _ = setup
    position, _ = buy(setup)
    configure(setup)
    assert portfolio.refresh(journal, rpc, wallet.address)["status"] == "ready"
    tokens = rpc.tokens
    if failure == "missing_tokens":
        rpc.tokens = 0
    elif failure == "bad_simulation":
        rpc.bad_sim = True
    elif failure == "unsupported_migration":
        read = portfolio.read_state
        monkeypatch.setattr(
            portfolio,
            "read_state",
            lambda *args: (_ for _ in ()).throw(TransferError("Unsupported PumpSwap pool mode")),
        )
    else:
        rpc.expire = True
    assert portfolio.refresh(journal, rpc, wallet.address)["status"] == "blocked"
    with pytest.raises(TransferError, match="Portfolio valuation blocked"):
        service.preview(wallet.address, MINT, "buy", "0.1")
    rpc.tokens, rpc.bad_sim, rpc.graduated, rpc.expire = tokens, False, False, False
    if failure == "unsupported_migration":
        monkeypatch.setattr(portfolio, "read_state", read)
    # A blocked/stale portfolio never gates the sell path.
    assert service.preview(wallet.address, MINT, "sell", "500", position_id=position["id"])


def test_fresh_valuation_required_again_at_submission(setup):
    wallet, rpc, journal, service, _ = setup
    buy(setup)
    configure(setup, portfolio_drawdown_lamports=40_000_000)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    effects = rpc.effects
    rpc.effects = (
        lambda amount, buying: effects(amount, buying)
        if buying
        else (40_000_000 - rpc.fee, -amount)
    )
    signed = []
    with pytest.raises(TransferError, match="Portfolio drawdown limit"):
        service.submit(p["id"], SimpleNamespace(address=wallet.address, sign=signed.append))
    assert signed == [] and len(rpc.sent) == 1


def test_stale_future_and_changed_basis_block_at_final_wallet_check(setup, monkeypatch):
    wallet, rpc, journal, service, _ = setup
    configure(setup)
    preview = service.preview(wallet.address, MINT, "buy", "0.1")
    report = portfolio.current(journal, rpc.genesis(), wallet.address)
    with monkeypatch.context() as m:
        m.setattr(portfolio.time, "time", lambda: report["started_at"] + 31)
        with pytest.raises(TransferError, match="stale"):
            journal.check_agent_reservations(preview, preview["state"])
    with monkeypatch.context() as m:
        m.setattr(portfolio.time, "time", lambda: report["started_at"] - 1)
        with pytest.raises(TransferError, match="stale"):
            portfolio.current(journal, rpc.genesis(), wallet.address)
    configure(setup, portfolio_drawdown_lamports=1)
    with pytest.raises(TransferError, match="Portfolio changed"):
        journal.check_agent_reservations(preview, preview["state"])


def test_slow_collection_does_not_raise_peak(setup, monkeypatch):
    wallet, rpc, journal, _, _ = setup
    buy(setup)
    previous = portfolio.refresh(journal, rpc, wallet.address)
    original = portfolio._quote
    now = portfolio.time.time()
    monkeypatch.setattr(portfolio.time, "time", lambda: now)

    def slow(*args):
        nonlocal now
        result = original(*args)
        now += 31
        return result

    monkeypatch.setattr(portfolio, "_quote", slow)
    result = portfolio.refresh(journal, rpc, wallet.address)
    assert result["status"] == "blocked" and "expired" in result["detail"]
    assert result["peak"] == previous["peak"]


def test_readonly_journal_and_network_isolation(setup):
    wallet, rpc, journal, _, directory = setup
    buy(setup)
    reader = TradeJournal.__new__(TradeJournal)
    reader.db = sqlite3.connect(
        (directory / "wallet-transfers.sqlite").as_uri() + "?mode=ro", uri=True
    )
    reader.db.row_factory = sqlite3.Row
    try:
        assert portfolio.refresh(reader, rpc, wallet.address)["status"] == "ready"
        assert portfolio.saved(reader, "other-network", wallet.address) is None
        assert portfolio.saved(reader, rpc.genesis(), "other-wallet") is None
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM trades")
    finally:
        reader.close()


def test_legacy_policy_stays_readable_without_inventing_a_new_limit(setup):
    wallet, rpc, journal, _, _ = setup
    values = configure(setup)
    del values["portfolio_drawdown_lamports"]
    with journal.db:
        journal.db.execute("UPDATE wallet_risk_policies SET payload=?", (json.dumps(values),))
    assert wallet_risk.status(journal, rpc.genesis(), wallet.address)["policy"] == values
    with pytest.raises(TransferError, match="every wallet risk limit"):
        wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, values)


@pytest.mark.parametrize("token2022", [False, True])
def test_portfolio_values_supported_migration_exits(tmp_path, token2022):
    from test_pump_swap_route import setup as swap_setup
    from test_token2022_route import setup as token_setup

    fixture = (token_setup if token2022 else swap_setup).__wrapped__(tmp_path)
    bank = next(fixture)
    try:
        wallet, rpc, journal, _, _ = bank
        buy(bank)
        rpc.graduated = True
        result = portfolio.refresh(journal, rpc, wallet.address)
        assert result["status"] == "ready"
        assert "pumpswap" in result["quotes"][0]["route"]
        assert result["net_liquidation"] > 0
    finally:
        next(fixture, None)


def test_wallet_revision_race_during_collection_rejects_entire_snapshot(setup, monkeypatch):
    wallet, rpc, journal, _, _ = setup
    buy(setup)
    configure(setup)
    quote = portfolio._quote

    def changing(*args):
        result = quote(*args)
        configure(setup, entries_paused=True)
        return result

    monkeypatch.setattr(portfolio, "_quote", changing)
    report = portfolio.refresh(journal, rpc, wallet.address)
    assert report["status"] == "blocked" and "changed during valuation" in report["detail"]


def test_partial_sale_invalidates_snapshot_and_combines_realized_with_remaining_loss(setup):
    wallet, rpc, journal, _, _ = setup
    p, _ = buy(setup)
    configure(setup)
    portfolio.refresh(journal, rpc, wallet.address)
    sell(setup, p["id"], "500")
    with pytest.raises(TransferError, match="Portfolio changed"):
        portfolio.current(journal, rpc.genesis(), wallet.address)
    report = portfolio.refresh(journal, rpc, wallet.address)
    assert report["quotes"][0]["quantity"] == 500_000_000
    assert report["unrealized_pnl"] == -23_527_140
    assert report["total_pnl"] == -24_554_280
    assert report["drawdown"] == 24_554_280


def test_final_lock_rejects_evidence_expiring_during_submission(setup, monkeypatch):
    wallet, rpc, journal, service, _ = setup
    configure(setup)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    reserve = journal.reserve_trade
    signed = []

    def expired(*args, **kwargs):
        report = portfolio.saved(journal, rpc.genesis(), wallet.address)
        monkeypatch.setattr(portfolio.time, "time", lambda: report["started_at"] + 31)
        return reserve(*args, **kwargs)

    monkeypatch.setattr(journal, "reserve_trade", expired)
    with pytest.raises(TransferError, match="valuation stale"):
        service.submit(p["id"], SimpleNamespace(address=wallet.address, sign=signed.append))
    assert not signed and not rpc.sent and not journal.pending(wallet.address, rpc.genesis())
