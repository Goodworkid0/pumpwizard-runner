"""Synthetic Automation Wallet acceptance; no private RPC or funded wallet is used."""

import base64
import json
import struct
import time

import pytest

from pumpwizard_runner import web_automation
from pumpwizard_runner.agent_execution import AgentTradeService
from pumpwizard_runner.automation_runtime import (
    AutomationController,
    AutomationSupervisor,
    authorization_rows,
)
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.wallet_vault import WalletVault
from test_agent_execution import MINT, setup as _agent_setup
from test_transfers import Rpc as TransferRpc


def plan(identity, agent, **changes):
    values = dict(
        **identity,
        sessions=[agent],
        budget_sol="0.1",
        max_order_sol="0.06",
        daily_buy_sol="0.06",
        daily_loss_sol="0.005",
        reserve_sol="0.01",
        portfolio_drawdown_sol="0.01",
        max_buys_per_day=1,
        max_trades_per_day=3,
        max_positions=1,
        slippage_bps=100,
        duration_minutes=15,
        daily_loss_bps=1000,
        trade_loss_bps=10000,
        priority_fee_lamports=0,
    )
    values.update(changes)
    return values


def observe_fx(library, agent, at, y, quote_usd=200, real=50):
    library.append_observations(agent, [MarketQuote.from_record(dict(
        mint=MINT, symbol="TEST", observed_at=at, source_at=at, fx_at=at,
        route="pump_bonding_curve_sol", curve_address="Curve", x=1e6, y=y,
        real_x=5e5, real_y=real, supply=1e6, quote_usd=quote_usd,
        market_cap_usd=y * quote_usd, decimals=6,
    )).as_record()])


@pytest.fixture
def setup(tmp_path):
    generator = _agent_setup.__wrapped__(tmp_path)
    main, rpc, journal, library, agent, _, _, _ = next(generator)
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    identity = {"sender": main.address, "network": "mainnet-beta"}
    created = web_automation.command(
        library, "create", dict(**identity, password="automation wallet password")
    )
    automation = WalletVault(tmp_path / "automation-wallet").unlock(
        "automation wallet password"
    )
    assert automation.address == created["address"]
    rpc.wallet = automation

    # The pinned $10 strategy is 0.05 SOL at this fresh synthetic FX quote.
    now = time.time()
    observe_fx(library, agent, now, 115)
    library.record_decisions(agent, [dict(
        at=now, kind="entry_recommended", mint=MINT,
        reason="strategy_entry_signal", payload={},
    )])
    controller = AutomationController(
        tmp_path, rpc_factory=lambda _network: rpc, readiness=lambda: [], interval=300
    )
    yield main, automation, rpc, journal, library, agent, identity, controller
    controller.shutdown()
    next(generator, None)


def authorize(setup):
    _, automation, _, _, library, agent, identity, controller = setup
    web_automation.command(library, "plan", plan(identity, agent), controller=controller)
    result = web_automation.command(library, "review", identity, controller=controller)
    review = result["review"]
    assert review["ready"] and review["can_authorize"]
    assert review["required_entry_debit_lamports"] == 50_000_000
    assert review["exit_headroom_lamports"] == 20_100_000
    result = web_automation.command(
        library,
        "authorize",
        dict(**identity, review_id=review["id"], password="automation wallet password"),
        controller=controller,
    )
    ident = result["authorization_id"]
    row = next(item for item in result["authorizations"] if item["id"] == ident)
    assert row["sender"] == automation.address and row["status"] == "authorized"
    encoded = json.dumps(row)
    assert "password" not in encoded and "seed" not in encoded and "ciphertext" not in encoded
    return ident, row


def test_preflight_rejects_old_small_defaults_with_exact_shortfalls(setup):
    _, automation, rpc, journal, library, agent, identity, controller = setup
    web_automation.command(
        library,
        "plan",
        plan(
            identity, agent, budget_sol="0.05", max_order_sol="0.01",
            daily_buy_sol="0.02", max_trades_per_day=2,
        ),
        controller=controller,
    )
    review = web_automation.command(
        library, "review", identity, controller=controller
    )["review"]
    assert not review["ready"] and not review["can_authorize"]
    assert any("Maximum order is short by 40000000 lamports" in reason for reason in review["preflight_blockers"])
    assert any("Total budget is short by 20100000 lamports" in reason for reason in review["preflight_blockers"])
    with pytest.raises(TransferError, match="Preflight blocked"):
        web_automation.command(
            library,
            "authorize",
            dict(**identity, review_id=review["id"], password="automation wallet password"),
            controller=controller,
        )
    assert not authorization_rows(journal, automation.address, rpc.genesis())
    assert not rpc.sent


def test_production_acceptance_gate_blocks_unlock_after_valid_review(setup):
    _, _, _, _, library, agent, identity, controller = setup
    gated = AutomationController(
        library.path.parent,
        rpc_factory=controller.rpc_factory,
        readiness=lambda: ["funded lifecycle evidence pending"],
    )
    web_automation.command(library, "plan", plan(identity, agent), controller=gated)
    review = web_automation.command(library, "review", identity, controller=gated)["review"]
    assert review["ready"] and not review["can_authorize"]
    with pytest.raises(TransferError, match="funded lifecycle evidence pending"):
        web_automation.command(
            library,
            "authorize",
            dict(**identity, review_id=review["id"], password="automation wallet password"),
            controller=gated,
        )
    assert not gated.manager._leases


def test_bounded_buy_partial_sell_full_sell_and_non_replenishing_usage(setup):
    _, automation, rpc, journal, library, agent, _, controller = setup
    ident, row = authorize(setup)
    cash = row["scope"]["strategies"][agent]["cash_hold"]
    service = AgentTradeService(library, rpc, journal)

    buy = service.prepare(agent, automation.address, MINT, cash)
    controller.manager.submit(service, ident, buy["id"])
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(automation.address)

    effects = rpc.effects
    rpc.effects = lambda amount, buying: (
        effects(amount, buying) if buying else (amount // 5 - rpc.fee, -amount)
    )
    observe_fx(library, agent, time.time(), 160)
    partial = service.prepare(agent, automation.address, MINT, cash, position_id=buy["id"])
    assert partial["amount"] < int(journal.position(buy["id"])["quantity"]), partial["agent_order"]["reason"]
    controller.manager.submit(service, ident, partial["id"])
    service.manual.reconcile(automation.address)

    rpc.effects = lambda amount, buying: (
        effects(amount, buying) if buying else (amount // 8 - rpc.fee, -amount)
    )
    observe_fx(library, agent, time.time(), 85)
    final = service.prepare(agent, automation.address, MINT, cash, position_id=buy["id"])
    controller.manager.submit(service, ident, final["id"])
    service.manual.reconcile(automation.address)

    current = authorization_rows(journal, automation.address, rpc.genesis())[0]
    assert current["used_orders"] == 3
    assert current["used_lamports"] == 89_915_000
    assert journal.position(buy["id"])["status"] == "closed"
    assert len(rpc.sent) == 3


def test_two_strategies_share_one_non_replenishing_cap(setup):
    _, automation, rpc, journal, library, first, identity, controller = setup
    second = library.create_instance(
        library.instance(first)["strategy_digest"], label="Second", entry_mode="manual"
    )
    now = time.time()
    for selected in (first, second):
        fx = 400 if selected == first else 401
        for index, y in enumerate((37, 38, 39, 40)):
            observe_fx(library, selected, now - 30 + index * 10, y, quote_usd=fx, real=40 + index)
        library.record_decisions(selected, [dict(
            at=now, kind="entry_recommended", mint=MINT,
            reason="strategy_entry_signal", payload={},
        )])
    raw = bytearray(base64.b64decode(rpc.bank["curve"]["data"][0]))
    struct.pack_into("<Q", raw, 16, 60_000_000_000)
    rpc.bank["curve"]["data"][0] = base64.b64encode(raw).decode()
    scoped = plan(
        identity, first, sessions=[first, second], max_order_sol="0.03", daily_buy_sol="0.05",
        portfolio_drawdown_sol="0.1", max_buys_per_day=2,
        max_trades_per_day=4, max_positions=2,
    )
    web_automation.command(library, "plan", scoped, controller=controller)
    review = web_automation.command(library, "review", identity, controller=controller)["review"]
    assert review["ready"] and len(review["strategies"]) == 2
    result = web_automation.command(
        library, "authorize",
        dict(**identity, review_id=review["id"], password="automation wallet password"),
        controller=controller,
    )
    ident = result["authorization_id"]
    auth = next(row for row in result["authorizations"] if row["id"] == ident)
    service = AgentTradeService(library, rpc, journal)
    rpc.rent = 0
    first_hold = auth["scope"]["strategies"][first]["cash_hold"]
    first_buy = service.prepare(first, automation.address, MINT, first_hold)
    controller.manager.submit(service, ident, first_buy["id"])
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(automation.address)

    second_hold = auth["scope"]["strategies"][second]["cash_hold"]
    second_buy = service.prepare(second, automation.address, MINT, second_hold)
    controller.manager.submit(service, ident, second_buy["id"])
    usage = authorization_rows(journal, automation.address, rpc.genesis())[0]
    assert usage["used_orders"] == 2
    assert usage["used_lamports"] == 49_747_655
    assert usage["scope"]["budget_lamports"] - usage["used_lamports"] == 50_252_345
    assert len(rpc.sent) == 2


def test_stop_and_restart_drop_the_signer_without_broadcast(setup):
    main, automation, rpc, journal, library, agent, identity, controller = setup
    ident, row = authorize(setup)
    cash = row["scope"]["strategies"][agent]["cash_hold"]
    service = AgentTradeService(library, rpc, journal)
    preview = service.prepare(agent, automation.address, MINT, cash)
    controller.stop(main.address, automation.address)
    with pytest.raises(TransferError, match="locked"):
        controller.manager.submit(service, ident, preview["id"])
    assert not rpc.sent and not journal.db.execute(
        "SELECT 1 FROM automation_authorization_orders"
    ).fetchone()

    # A new process also converts an apparently active durable row to locked.
    with journal.db:
        journal.db.execute(
            "UPDATE automation_authorizations SET status='authorized' WHERE id=?", (ident,)
        )
    replacement = AutomationController(
        library.path.parent, rpc_factory=controller.rpc_factory, readiness=lambda: []
    )
    state = web_automation.state(
        library.path.parent, identity["sender"], identity["network"],
        library=library, controller=replacement,
    )
    assert state["locked"] and state["authorizations"][0]["status"] == "locked"
    replacement.shutdown()


def test_supervisor_prepares_signs_and_broadcasts_through_shared_services(setup):
    _, automation, rpc, journal, _, _, identity, controller = setup
    ident, _ = authorize(setup)
    with journal.db:
        journal.db.execute(
            "UPDATE automation_authorizations SET status='running' WHERE id=?", (ident,)
        )
    supervisor = AutomationSupervisor(controller, ident, identity["network"], 300)
    report = supervisor.cycle()
    assert report["status"] == "submitted" and report["side"] == "buy"
    assert len(rpc.sent) == 1
    current = authorization_rows(journal, automation.address, rpc.genesis())[0]
    assert current["used_orders"] == 1
    trade = journal.trades(automation.address)[0]
    assert trade["id"] == report["order_id"] and trade["signature"] == report["signature"]


def test_stopped_managed_position_can_be_released_and_password_sold(setup):
    _, automation, rpc, journal, library, agent, identity, controller = setup
    ident, row = authorize(setup)
    cash = row["scope"]["strategies"][agent]["cash_hold"]
    service = AgentTradeService(library, rpc, journal)
    buy = service.prepare(agent, automation.address, MINT, cash)
    controller.manager.submit(service, ident, buy["id"])
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(automation.address)

    released = web_automation.command(
        library, "recovery-release-position",
        dict(**identity, position_id=buy["id"], acknowledge="stop-and-release-automation-management"),
        controller=controller,
    )
    assert released["recovery_release"]["released"]
    assert not next(
        row for row in journal.db.execute(
            "SELECT * FROM agent_reservations WHERE position_id=?", (buy["id"],)
        )
    )["status"] == "held"
    review = web_automation.command(
        library, "recovery-sell-review",
        dict(**identity, position_id=buy["id"], slippage_bps=100),
        controller=controller,
    )["recovery_preview"]
    assert review["side"] == "sell" and "unsigned_transaction" not in review
    result = web_automation.command(
        library, "recovery-sell-submit",
        dict(**identity, review_id=review["id"], password="automation wallet password"),
        controller=controller,
    )
    assert result["signature"] and len(rpc.sent) == 2
    web_automation.command(library, "recovery-reconcile", identity, controller=controller)
    assert journal.position(buy["id"])["status"] == "closed"


def test_recovery_withdrawal_requires_exact_review_and_password(tmp_path):
    owner = WalletVault(tmp_path / "owner").create("funding owner password")
    library_dir = tmp_path / "runner"
    from pumpwizard_runner.library import StrategyLibrary

    library = StrategyLibrary(library_dir)
    identity = {"sender": owner.address, "network": "mainnet-beta"}
    web_automation.command(
        library, "create", dict(**identity, password="automation wallet password")
    )
    automation = WalletVault(library_dir / "automation-wallet").unlock(
        "automation wallet password"
    )
    rpc = TransferRpc()
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    controller = AutomationController(library_dir, rpc_factory=lambda _network: rpc, readiness=lambda: [])
    try:
        review = web_automation.command(
            library, "recovery-withdraw-review", dict(**identity, amount="0.01"),
            controller=controller,
        )["recovery_preview"]
        assert review["sender"] == automation.address
        assert review["recipient"] == owner.address
        with pytest.raises(TransferError, match="password"):
            web_automation.command(
                library, "recovery-withdraw-submit",
                dict(**identity, review_id=review["id"], password="wrong password value"),
                controller=controller,
            )
        result = web_automation.command(
            library, "recovery-withdraw-submit",
            dict(**identity, review_id=review["id"], password="automation wallet password"),
            controller=controller,
        )
        assert result["signature"] and len(rpc.sent) == 1
        with pytest.raises(TransferError):
            web_automation.command(
                library, "recovery-withdraw-submit",
                dict(**identity, review_id=review["id"], password="automation wallet password"),
                controller=controller,
            )
    finally:
        controller.shutdown()
        library.close()
