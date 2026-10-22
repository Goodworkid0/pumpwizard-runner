"""Synthetic main-wallet Autopilot allowance tests; no funded RPC is used."""

import base64
import json
import time

import pytest

from pumpwizard_runner import main_wallet_autopilot as autopilot
from pumpwizard_runner import strategy_risk, wallet_risk
from pumpwizard_runner.agent_execution import cancel
from pumpwizard_runner.main_wallet_autotrader import _prepare_candidate
from pumpwizard_runner.reservations import reserve
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.web_main_wallet_autopilot import MainWalletAutopilotController
from test_agent_execution import MINT, observe, prepare_buy, setup as _agent_setup


@pytest.fixture
def setup(tmp_path):
    source = _agent_setup.__wrapped__(tmp_path)
    values = next(source)
    set_autopilot_brief(values[3], values[4])
    yield values
    next(source, None)


def set_autopilot_brief(library, session_id, scope="entries-and-exits"):
    brief = library.session_brief(session_id)
    brief.update(
        schema="pumpwizard.session-brief/v3",
        mode="trade-autopilot",
        session_mode="trade",
        execution_style="autopilot",
        automation_scope=scope,
        profit_management="user-directed" if scope == "entries-only" else "strategy-managed",
        permissions={
            "prepare_orders": True,
            "automatic_entries": scope in ("entries-only", "entries-and-exits"),
            "automatic_protective_exits": scope != "entries-only",
            "automatic_profit_exits": scope != "entries-only",
            "winner_handoff": False,
            "user_signature_each_order": False,
            "automation_wallet_required": False,
            "generated_wallet_required": True,
            "activation_required": True,
            "custody": "main-wallet-browser",
        },
    )
    with library._connection:
        library._connection.execute(
            "UPDATE session_briefs SET schema_name=?,payload_json=? WHERE instance_id=?",
            (brief["schema"], json.dumps(brief, sort_keys=True, separators=(",", ":")), session_id),
        )


def strategy_limits(**changes):
    return {
        **{
            "allocation_lamports": 200_000_000,
            "buys_enabled": True,
            "sells_enabled": True,
            "min_trade_lamports": 1,
            "max_trade_lamports": 150_000_000,
            "max_daily_buy_lamports": 180_000_000,
            "max_trades_per_day": 4,
            "max_daily_loss_lamports": 40_000_000,
            "max_daily_loss_bps": 2_000,
            "max_trade_loss_bps": 2_000,
            "max_open_positions": 2,
        },
        **changes,
    }


def wallet_limits(**changes):
    return {
        **{
            "max_buy_lamports": 200_000_000,
            "max_exposure_lamports": 500_000_000,
            "reserve_lamports": 100_000_000,
            "daily_loss_lamports": 100_000_000,
            "drawdown_lamports": 100_000_000,
            "portfolio_drawdown_lamports": 500_000_000,
            "entries_paused": False,
        },
        **changes,
    }


def plan(**changes):
    return {
        **{
            "duration_seconds": 900,
            "budget_lamports": 200_000_000,
            "max_order_debit_lamports": 120_000_000,
            "max_daily_debit_lamports": 180_000_000,
            "max_orders": 4,
            "max_trades_per_day": 4,
            "max_open_positions": 2,
            "slippage_bps": 100,
        },
        **changes,
    }


def configure(setup):
    wallet, rpc, journal, _, agent, _, _, _ = setup
    wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, wallet_limits())
    return strategy_risk.save_policy(
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        strategy_limits(),
        rpc.balance,
    )


def reviewed(setup, **changes):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    configure(setup)
    return autopilot.review(
        library,
        journal,
        session_id=agent,
        sender=wallet.address,
        network=rpc.network,
        genesis=rpc.genesis(),
        wallet_kind="local",
        plan=plan(**changes),
    )


def authorized(setup, **changes):
    _, _, journal, library, _, _, _, _ = setup
    review = reviewed(setup, **changes)
    assert review["ready"] and not review["can_activate"]
    return autopilot.authorize(library, journal, review["id"])


def activate(setup, allowance=None):
    wallet, rpc, journal, _, _, _, _, _ = setup
    allowance = allowance or authorized(setup)
    manager = autopilot.RuntimeManager(allow_synthetic=True)
    runtime = manager.activate(
        journal,
        allowance["id"],
        sender=wallet.address,
        genesis=rpc.genesis(),
        wallet_kind="local",
    )
    return manager, runtime, allowance


def test_review_derives_permissions_and_reuses_one_existing_cash_reservation(setup):
    _, _, journal, library, agent, cash, _, _ = setup
    review = reviewed(setup)
    assert review["strategy_digest"] == library.instance(agent)["strategy_digest"]
    assert review["cash_hold"] == cash["id"]
    assert review["requested_permissions"] == {
        "prepare_orders": True,
        "automatic_entries": True,
        "automatic_protective_exits": True,
        "automatic_profit_exits": True,
        "winner_handoff": False,
        "user_signature_each_order": False,
        "custody": "main-wallet-browser",
        "activation_required": True,
    }
    assert review["valuation_rule"].startswith("fresh wallet-risk")
    assert journal.db.execute(
        "SELECT COUNT(*) FROM agent_reservations WHERE status='held'"
    ).fetchone()[0] == 1

    allowance = autopilot.authorize(library, journal, review["id"])
    assert allowance["status"] == "authorized"
    assert allowance["granted_permissions"]["synthetic_signing"] is True
    assert allowance["granted_permissions"]["live_submission"] is False
    assert allowance["scope"]["policy_revision"] == review["policy_revision"]


def test_extension_and_immutable_exit_only_scope_never_grant_entries(setup):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    configure(setup)
    extension = autopilot.review(
        library,
        journal,
        session_id=agent,
        sender=wallet.address,
        network=rpc.network,
        genesis=rpc.genesis(),
        wallet_kind="extension",
        plan=plan(),
    )
    assert not extension["ready"]
    assert "Extension wallets" in extension["preflight_blockers"][0]
    with pytest.raises(TransferError, match="preflight is blocked"):
        autopilot.authorize(library, journal, extension["id"])

    set_autopilot_brief(library, agent, "exits-only")
    exit_only = autopilot.review(
        library,
        journal,
        session_id=agent,
        sender=wallet.address,
        network=rpc.network,
        genesis=rpc.genesis(),
        wallet_kind="local",
        plan=plan(),
    )
    assert exit_only["ready"]
    assert exit_only["automation_scope"] == "exits-only"
    assert exit_only["requested_permissions"]["automatic_entries"] is False
    with pytest.raises(TransferError, match="Supply every"):
        autopilot.review(
            library,
            journal,
            session_id=agent,
            sender=wallet.address,
            network=rpc.network,
            genesis=rpc.genesis(),
            wallet_kind="local",
            plan=plan(automatic_entries=True),
        )


def test_live_activation_is_not_request_bypassable_and_runtime_needs_heartbeat(setup):
    wallet, rpc, journal, _, _, _, _, _ = setup
    allowance = authorized(setup)
    with pytest.raises(TransferError, match="Live main-wallet Autopilot activation remains blocked"):
        autopilot.RuntimeManager().activate(
            journal,
            allowance["id"],
            sender=wallet.address,
            genesis=rpc.genesis(),
            wallet_kind="local",
        )
    manager, runtime, _ = activate(setup, allowance)
    assert manager.available(allowance["id"])
    beat = manager.heartbeat(allowance["id"], runtime["runtime_token"])
    assert beat["authorization_revision"] == allowance["authorization_revision"]
    manager.lock(journal, allowance["id"])
    assert not manager.available(allowance["id"])
    assert autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]["status"] == "locked"


def test_exit_only_activation_never_prepares_or_resumes_entries(setup):
    wallet, rpc, journal, library, agent, _, _, directory = setup
    set_autopilot_brief(library, agent, "exits-only")
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    configure(setup)
    controller = MainWalletAutopilotController(
        directory,
        manager=autopilot.RuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    reviewed_scope = controller.review(
        library,
        sender=wallet.address,
        network=rpc.network,
        session_id=agent,
        wallet_kind="local",
        plan=plan(),
    )
    allowance = controller.authorize(
        library,
        sender=wallet.address,
        network=rpc.network,
        session_id=agent,
        review_id=reviewed_scope["id"],
        confirmation=f"ALLOW {agent[:8].upper()}",
    )
    runtime = controller.activate(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        wallet_kind="local",
    )
    assert library.instance(agent)["entry_mode"] == "manual"
    assert library.instance(agent)["exit_mode"] == "automatic"
    result = controller.next_ticket(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        runtime_token=runtime["runtime_token"],
    )
    assert result["ticket"] is None and "automatic entries are off" in result["detail"]
    assert not journal.db.execute("SELECT 1 FROM agent_orders").fetchone()
    with pytest.raises(TransferError, match="exit-only"):
        controller.pause_entries(
            library,
            sender=wallet.address,
            network=rpc.network,
            allowance_id=allowance["id"],
            runtime_token=runtime["runtime_token"],
            paused=False,
        )


def test_two_sessions_bind_distinct_existing_holds_without_reserving_twice(setup):
    wallet, rpc, journal, library, first, first_cash, _, _ = setup
    wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, wallet_limits())
    strategy_risk.save_policy(
        journal, first, rpc.genesis(), wallet.address, strategy_limits(), rpc.balance
    )
    second = library.create_instance(
        library.instance(first)["strategy_digest"],
        label="Second",
        entry_mode="manual",
    )
    set_autopilot_brief(library, second)
    second_cash = reserve(
        journal, rpc, second, wallet.address, lamports=200_000_000
    )
    strategy_risk.save_policy(
        journal, second, rpc.genesis(), wallet.address, strategy_limits(), rpc.balance
    )
    reviews = [
        autopilot.review(
            library,
            journal,
            session_id=session_id,
            sender=wallet.address,
            network=rpc.network,
            genesis=rpc.genesis(),
            wallet_kind="local",
            plan=plan(),
        )
        for session_id in (first, second)
    ]
    allowances = [autopilot.authorize(library, journal, item["id"]) for item in reviews]
    assert {item["scope"]["cash_hold"] for item in allowances} == {
        first_cash["id"], second_cash["id"],
    }
    assert journal.db.execute(
        "SELECT COUNT(*) FROM agent_reservations WHERE status='held' AND position_id IS NULL"
    ).fetchone()[0] == 2
    assert sum(item["scope"]["budget_lamports"] for item in allowances) == 400_000_000


def test_policy_revision_and_strategy_digest_changes_invalidate_runtime_scope(setup):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    manager, runtime, allowance = activate(setup)
    preview = prepare_buy(setup)
    wallet_risk.save_policy(
        journal,
        rpc.genesis(),
        wallet.address,
        wallet_limits(reserve_lamports=110_000_000),
    )
    with pytest.raises(TransferError, match="policy revision changed"):
        manager.issue_ticket(
            library,
            journal,
            allowance["id"],
            runtime["runtime_token"],
            preview,
        )
    assert not journal.db.execute("SELECT 1 FROM main_wallet_allowance_orders").fetchone()

    # Historical allowance identity remains exact; changing a library instance
    # never rewrites the durable digest stored above.
    assert allowance["strategy_digest"] != "changed"
    journal.db.execute("UPDATE main_wallet_allowances SET strategy_digest='changed' WHERE id=?", (
        allowance["id"],
    ))
    with pytest.raises(TransferError, match="strategy digest changed"):
        manager.issue_ticket(
            library,
            journal,
            allowance["id"],
            runtime["runtime_token"],
            preview,
        )


@pytest.mark.parametrize("invalidation", ["policy", "expiry", "revocation"])
def test_rejected_new_ticket_cancels_only_its_unsigned_review(setup, invalidation):
    wallet, rpc, journal, library, agent, cash, service, _ = setup
    manager, runtime, allowance = activate(setup)
    if invalidation == "policy":
        wallet_risk.save_policy(
            journal,
            rpc.genesis(),
            wallet.address,
            wallet_limits(max_buy_lamports=190_000_000),
        )
    elif invalidation == "expiry":
        with journal.db:
            journal.db.execute(
                "UPDATE main_wallet_allowances SET expires_at=? WHERE id=?",
                (time.time() - 1, allowance["id"]),
            )
    else:
        manager.lock(journal, allowance["id"], revoked=True)

    ticket, reasons = _prepare_candidate(
        manager,
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        service,
        agent,
        wallet.address,
        cash["id"],
        100,
        [{"mint": MINT, "position_id": None}],
    )
    assert ticket is None and reasons
    assert not journal.db.execute(
        "SELECT 1 FROM agent_orders WHERE cash_hold=? AND status='review'", (cash["id"],)
    ).fetchone()
    cancelled = journal.db.execute(
        "SELECT id FROM agent_orders WHERE cash_hold=? AND status='cancelled'", (cash["id"],)
    ).fetchall()
    assert len(cancelled) == 1
    assert not journal.db.execute("SELECT 1 FROM main_wallet_allowance_orders").fetchone()

    # The rejected ticket did not strand the shared reservation.  A fresh
    # unsigned review can claim it, independently of renewing the allowance.
    replacement = service.prepare(agent, wallet.address, MINT, cash["id"])
    assert replacement["id"] != cancelled[0]["id"]
    cancel(journal, replacement["id"])


def test_rejected_strategy_review_does_not_block_shared_hold_exit(setup):
    wallet, rpc, journal, library, first, cash, service, _ = setup
    manager, runtime, allowance = activate(setup)
    workspace_id = "shared-autopilot-regression"
    owner_id = f"workspace:{workspace_id}"
    with journal.db:
        journal.db.execute(
            "UPDATE agent_reservations SET agent_id=? WHERE id=?", (owner_id, cash["id"]),
        )
        journal.db.execute(
            "INSERT INTO session_workspace_allocations VALUES (?,?,?,?,?,?,?,NULL,'held')",
            (
                workspace_id, owner_id, rpc.genesis(), wallet.address, cash["id"],
                cash["lamports"], time.time(),
            ),
        )
    strategy_risk.save_policy(
        journal, first, rpc.genesis(), wallet.address, strategy_limits(), rpc.balance,
        workspace_id=workspace_id,
    )
    second = library.create_instance(
        library.instance(first)["strategy_digest"], label="Shared exit", entry_mode="manual",
    )
    strategy_risk.save_policy(
        journal, second, rpc.genesis(), wallet.address, strategy_limits(), rpc.balance,
        workspace_id=workspace_id,
    )
    now = time.time()
    for index in range(4):
        observe(library, second, now - 30 + index * 10, 110 + index, 40 + index)
    library.record_decisions(second, [{
        "at": now, "kind": "entry_recommended", "mint": MINT,
        "reason": "strategy_entry_signal", "payload": {},
    }])

    bought = service.prepare(second, wallet.address, MINT, cash["id"])
    service.submit(bought["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(wallet.address)
    observe(library, second, time.time(), 320)

    rejected, reasons = _prepare_candidate(
        manager, library, journal, allowance["id"], runtime["runtime_token"],
        service, first, wallet.address, cash["id"], 100,
        [{"mint": MINT, "position_id": None}],
    )
    assert rejected is None and reasons
    assert not journal.db.execute(
        "SELECT 1 FROM agent_orders WHERE cash_hold=? AND status='review'", (cash["id"],)
    ).fetchone()

    eligible_exit = service.prepare(
        second, wallet.address, MINT, cash["id"], position_id=bought["id"],
    )
    assert eligible_exit["side"] == "sell"
    cancel(journal, eligible_exit["id"])


def test_exact_ticket_reserves_fee_inclusive_usage_atomically_and_rejects_tampering(setup):
    wallet, _, journal, library, _, _, _, _ = setup
    manager, runtime, allowance = activate(setup)
    preview = prepare_buy(setup)
    ticket = manager.issue_ticket(
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        preview,
    )
    message = base64.b64decode(preview["message"])

    with pytest.raises(RuntimeError, match="rollback"):
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            manager.reserve_order(
                library,
                journal,
                allowance["id"],
                runtime["runtime_token"],
                ticket["id"],
                preview,
                message,
            )
            raise RuntimeError("rollback")
    assert not journal.db.execute("SELECT 1 FROM main_wallet_allowance_orders").fetchone()

    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(TransferError, match="different from the ticket"):
            manager.reserve_order(
                library,
                journal,
                allowance["id"],
                runtime["runtime_token"],
                ticket["id"],
                preview,
                message + b"tampered",
            )
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        manager.reserve_order(
            library,
            journal,
            allowance["id"],
            runtime["runtime_token"],
            ticket["id"],
            preview,
            message,
        )
    usage = journal.db.execute("SELECT * FROM main_wallet_allowance_orders").fetchone()
    assert int(usage["debit"]) == preview["amount"] + preview["fee"] + autopilot.MAX_ACCOUNT_FUNDING

    raw = bytearray(base64.b64decode(ticket["unsigned_transaction"]))
    raw[1:65] = wallet.sign(bytes(raw[65:]))
    signed = base64.b64encode(raw).decode()
    assert manager.validate_signed_ticket(
        allowance["id"], runtime["runtime_token"], ticket["id"], signed
    )
    raw[-1] ^= 1
    with pytest.raises(TransferError, match="exact authorized ticket"):
        manager.validate_signed_ticket(
            allowance["id"],
            runtime["runtime_token"],
            ticket["id"],
            base64.b64encode(raw).decode(),
        )


def test_entry_pause_keeps_ticketed_exits_eligible_but_revocation_does_not(setup):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    manager, runtime, allowance = activate(setup)
    buy = prepare_buy(setup)
    ticket = manager.issue_ticket(
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        buy,
    )
    manager.pause_entries(journal, allowance["id"], runtime["runtime_token"], True)
    assert manager.next_ticket(allowance["id"], runtime["runtime_token"]) is None
    with pytest.raises(TransferError, match="missing"):
        manager.validate_signed_ticket(
            allowance["id"], runtime["runtime_token"], ticket["id"], "ignored",
        )
    with pytest.raises(TransferError, match="entries are paused"):
        manager.issue_ticket(
            library,
            journal,
            allowance["id"],
            runtime["runtime_token"],
            buy,
        )
    # The pause is distinct from signer loss; permission and runtime remain.
    current = autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]
    assert current["status"] == "entry-paused" and manager.available(allowance["id"])
    manager.lock(journal, allowance["id"], revoked=True)
    assert autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]["status"] == "revoked"
    with pytest.raises(TransferError, match="locked"):
        manager.heartbeat(allowance["id"], runtime["runtime_token"])


def test_signed_ticket_uses_normal_submission_and_keeps_unknown_signature_usage(setup):
    wallet, rpc, journal, library, _, _, service, directory = setup
    manager, runtime, allowance = activate(setup)
    preview = prepare_buy(setup)
    ticket = manager.issue_ticket(
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        preview,
    )
    raw = bytearray(base64.b64decode(ticket["unsigned_transaction"]))
    raw[1:65] = wallet.sign(bytes(raw[65:]))
    rpc.disconnect = True
    signature = autopilot.submit_signed_ticket(
        library,
        rpc,
        directory,
        manager,
        allowance_id=allowance["id"],
        runtime_token=runtime["runtime_token"],
        ticket_id=ticket["id"],
        signed_transaction=base64.b64encode(raw).decode(),
    )
    trade = journal.trades(wallet.address)[0]
    assert trade["signature"] == signature and trade["status"] == "unknown"
    usage = autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]
    assert usage["used_orders"] == 1

    rpc.disconnect = False
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(wallet.address)
    assert journal.trades(wallet.address)[0]["status"] == "finalized"
    assert autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]["used_orders"] == 1
    assert autopilot.policy_revision(
        journal, allowance["session_id"], rpc.genesis(), wallet.address
    ) == allowance["policy_revision"]


@pytest.mark.parametrize("scope", ["entries-and-exits", "entries-only"])
def test_browser_controller_runs_synthetic_review_ticket_and_signed_submission(setup, scope):
    wallet, rpc, journal, library, agent, _, _, directory = setup
    set_autopilot_brief(library, agent, scope)
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    configure(setup)
    controller = MainWalletAutopilotController(
        directory,
        manager=autopilot.RuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    reviewed_scope = controller.review(
        library,
        sender=wallet.address,
        network=rpc.network,
        session_id=agent,
        wallet_kind="local",
        plan=plan(),
    )
    with pytest.raises(TransferError, match="Type ALLOW"):
        controller.authorize(
            library,
            sender=wallet.address,
            network=rpc.network,
            session_id=agent,
            review_id=reviewed_scope["id"],
            confirmation="ALLOW WRONG",
        )
    allowance = controller.authorize(
        library,
        sender=wallet.address,
        network=rpc.network,
        session_id=agent,
        review_id=reviewed_scope["id"],
        confirmation=f"ALLOW {agent[:8].upper()}",
    )
    runtime = controller.activate(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        wallet_kind="local",
    )
    set_modes = library.instance(agent)
    assert set_modes["entry_mode"] == "automatic"
    assert set_modes["exit_mode"] == ("manual" if scope == "entries-only" else "automatic")
    next_order = controller.next_ticket(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        runtime_token=runtime["runtime_token"],
    )
    ticket = next_order["ticket"]
    assert ticket is not None and "entry" in next_order["detail"].lower()
    raw = bytearray(base64.b64decode(ticket["unsigned_transaction"]))
    raw[1:65] = wallet.sign(bytes(raw[65:]))
    signature = controller.submit_signed(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        runtime_token=runtime["runtime_token"],
        ticket_id=ticket["id"],
        signed_transaction=base64.b64encode(raw).decode(),
    )
    assert journal.trades(wallet.address)[0]["signature"] == signature
    assert controller.next_ticket(
        library,
        sender=wallet.address,
        network=rpc.network,
        allowance_id=allowance["id"],
        runtime_token=runtime["runtime_token"],
    )["ticket"] is None
    assert controller.status(wallet.address, rpc.network, agent)["allowances"][0][
        "used_orders"
    ] == 1


def test_durable_entry_pause_discards_a_cached_buy_ticket_without_locking_exit_runtime(setup):
    from pumpwizard_runner.main_wallet_autotrader import prepare_next_ticket
    _, rpc, journal, library, _, _, _, _ = setup
    manager, runtime, allowance = activate(setup)
    preview = prepare_buy(setup)
    manager.issue_ticket(library, journal, allowance['id'], runtime['runtime_token'], preview)
    with journal.db:
        journal.db.execute("UPDATE main_wallet_allowances SET status='entry-paused',entries_paused=1 WHERE id=?", (allowance['id'],))
        journal.db.execute("UPDATE agent_orders SET status='cancelled' WHERE id=?", (preview['id'],))
    result = prepare_next_ticket(library, journal, rpc, manager, allowance['id'], runtime['runtime_token'])
    assert result['ticket'] is None
    assert manager.next_ticket(allowance['id'], runtime['runtime_token']) is None
    assert manager.heartbeat(allowance['id'], runtime['runtime_token'])
    assert journal.db.execute('SELECT status FROM main_wallet_allowances WHERE id=?', (allowance['id'],)).fetchone()[0] == 'entry-paused'


def test_restart_and_expiry_remove_runtime_without_releasing_usage(setup):
    wallet, rpc, journal, library, _, _, _, _ = setup
    manager, runtime, allowance = activate(setup)
    preview = prepare_buy(setup)
    ticket = manager.issue_ticket(
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        preview,
    )
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        manager.reserve_order(
            library,
            journal,
            allowance["id"],
            runtime["runtime_token"],
            ticket["id"],
            preview,
            base64.b64decode(preview["message"]),
        )
    manager.recover_restart(journal)
    row = autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0]
    assert row["status"] == "locked" and row["used_orders"] == 1

    cancel(journal, preview["id"])
    replacement = authorized(setup)
    manager, runtime, _ = activate(setup, replacement)
    with journal.db:
        journal.db.execute(
            "UPDATE main_wallet_allowances SET expires_at=? WHERE id=?",
            (time.time() - 1, replacement["id"]),
        )
    preview = prepare_buy(setup)
    with pytest.raises(TransferError, match="expired"):
        manager.issue_ticket(
            library,
            journal,
            replacement["id"],
            runtime["runtime_token"],
            preview,
        )


def test_review_requires_order_and_daily_caps_that_can_fund_conservative_exits(setup):
    too_small_order = reviewed(
        setup,
        max_order_debit_lamports=autopilot.EXIT_DEBIT - 1,
    )
    assert not too_small_order["ready"]
    assert any(
        "Maximum order debit must cover" in blocker
        for blocker in too_small_order["preflight_blockers"]
    )

    _, _, journal, library, _, _, _, _ = setup
    viable_order = autopilot.review(
        library,
        journal,
        session_id=too_small_order["session_id"],
        sender=too_small_order["sender"],
        network=too_small_order["network"],
        genesis=too_small_order["genesis"],
        wallet_kind="local",
        plan=plan(max_order_debit_lamports=autopilot.EXIT_DEBIT),
    )
    assert viable_order["ready"]

    too_small_day = autopilot.review(
        library,
        journal,
        session_id=too_small_order["session_id"],
        sender=too_small_order["sender"],
        network=too_small_order["network"],
        genesis=too_small_order["genesis"],
        wallet_kind="local",
        plan=plan(
            max_open_positions=2,
            max_daily_debit_lamports=2 * autopilot.EXIT_DEBIT - 1,
        ),
    )
    assert not too_small_day["ready"]
    assert any(
        "Daily debit must cover conservative exits" in blocker
        for blocker in too_small_day["preflight_blockers"]
    )


@pytest.mark.parametrize(("remaining", "passes"), [(-1, False), (0, True)])
def test_daily_debit_boundary_retains_exit_headroom_atomically(setup, remaining, passes):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    configure(setup)
    preview = prepare_buy(setup)
    debit = preview["amount"] + preview["fee"] + autopilot.MAX_ACCOUNT_FUNDING
    daily_limit = debit + autopilot.EXIT_DEBIT + remaining
    review = autopilot.review(
        library,
        journal,
        session_id=agent,
        sender=wallet.address,
        network=rpc.network,
        genesis=rpc.genesis(),
        wallet_kind="local",
        plan=plan(
            max_open_positions=1,
            max_daily_debit_lamports=daily_limit,
        ),
    )
    assert review["ready"]
    allowance = autopilot.authorize(library, journal, review["id"])
    manager, runtime, _ = activate(setup, allowance)
    ticket = manager.issue_ticket(
        library,
        journal,
        allowance["id"],
        runtime["runtime_token"],
        preview,
    )
    if passes:
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            manager.reserve_order(
                library,
                journal,
                allowance["id"],
                runtime["runtime_token"],
                ticket["id"],
                preview,
                base64.b64decode(preview["message"]),
            )
        assert autopilot.allowance_rows(journal, wallet.address, rpc.genesis())[0][
            "used_orders"
        ] == 1
    else:
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(TransferError, match="retain a conservative exit debit"):
                manager.reserve_order(
                    library,
                    journal,
                    allowance["id"],
                    runtime["runtime_token"],
                    ticket["id"],
                    preview,
                    base64.b64decode(preview["message"]),
                )
        assert not journal.db.execute(
            "SELECT 1 FROM main_wallet_allowance_orders"
        ).fetchone()


def test_entries_only_rejects_exit_ticket_and_never_scans_exits(setup, monkeypatch):
    from pumpwizard_runner import main_wallet_autotrader

    _, _, journal, library, agent, _, _, _ = setup
    set_autopilot_brief(library, agent, "entries-only")
    manager, runtime, allowance = activate(setup)
    assert allowance["granted_permissions"]["automatic_entries"] is True
    assert allowance["granted_permissions"]["automatic_protective_exits"] is False
    assert allowance["granted_permissions"]["automatic_profit_exits"] is False
    preview = prepare_buy(setup)
    with pytest.raises(TransferError, match="does not grant automatic exits"):
        manager.issue_ticket(library, journal, allowance["id"], runtime["runtime_token"],
                             {**preview, "side": "sell"})
    cancel(journal, preview["id"])
    def unexpected(*args):
        pytest.fail("Entry-only automation must never inspect automatic exit candidates")
    monkeypatch.setattr(main_wallet_autotrader, "_exit_candidates", unexpected)
    manager.pause_entries(journal, allowance["id"], runtime["runtime_token"], True)
    result = main_wallet_autotrader.prepare_next_ticket(
        library, journal, setup[1], manager, allowance["id"], runtime["runtime_token"])
    assert result["ticket"] is None
