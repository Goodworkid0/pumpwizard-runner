"""Bounded service signer acceptance; no funded RPC or real wallet is used."""

import base64
import json
import time

import pytest

from pumpwizard_runner import main_wallet_autopilot as autopilot
from pumpwizard_runner import service_acceptance
from pumpwizard_runner import session_allocations, strategy_risk, wallet_risk
from pumpwizard_runner.main_wallet_autopilot import submit_signed_ticket
from pumpwizard_runner.main_wallet_autotrader import prepare_next_ticket
from pumpwizard_runner.pump_route import ROUTE
from pumpwizard_runner.pump_swap_route import ROUTE as SWAP_EXIT_ROUTE
from pumpwizard_runner.reservations import rows as reservation_rows
from pumpwizard_runner.service_acceptance import (
    acceptance_approval_message,
    allowance_binding_sha256,
    verify_acceptance_approval,
)
from pumpwizard_runner.service_signing import ServiceRuntimeManager, ServiceSigningController
from pumpwizard_runner.transfers import GENESIS, TransferError
from test_agent_execution import observe, setup as _agent_setup
from test_main_wallet_autopilot import configure, plan, strategy_limits, wallet_limits
from test_pump_swap_route import SwapRpc
from test_service_wallet_vault import PASSWORD, envelope


def set_service_brief(library, session_id, scope="entries-and-exits"):
    brief = library.session_brief(session_id)
    permissions = {
        **autopilot.requested_permissions(scope, "main-wallet-service"),
        "automation_wallet_required": False,
        "generated_wallet_required": True,
    }
    brief.update(
        schema="pumpwizard.session-brief/v4",
        mode="trade-autopilot",
        session_mode="trade",
        execution_style="autopilot",
        automation_scope=scope,
        profit_management="user-directed" if scope == "entries-only" else "strategy-managed",
        permissions=permissions,
    )
    with library._connection:
        library._connection.execute(
            "UPDATE session_briefs SET schema_name=?,payload_json=? WHERE instance_id=?",
            (
                brief["schema"],
                json.dumps(brief, sort_keys=True, separators=(",", ":")),
                session_id,
            ),
        )


@pytest.fixture
def setup(tmp_path):
    source = _agent_setup.__wrapped__(tmp_path)
    values = next(source)
    wallet, rpc, _, library, agent, _, _, _ = values
    rpc.network = "mainnet-beta"
    rpc.genesis = lambda: GENESIS["mainnet-beta"]
    set_service_brief(library, agent)
    controller = ServiceSigningController(
        tmp_path,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    controller.enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    configure(values)
    yield values, controller
    try:
        controller.lock("Fixture teardown")
    except Exception:
        pass
    next(source, None)


def authorize(setup):
    values, controller = setup
    wallet, _, _, library, agent, _, _, _ = values
    reviewed = controller.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(),
    )
    assert reviewed["ready"]
    assert reviewed["schema"] == "pumpwizard.main-wallet-allowance-review/v2"
    assert reviewed["requested_permissions"]["continues_without_browser"] is True
    return controller.authorize(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        review_id=reviewed["id"],
        confirmation=f"ALLOW SERVICE {agent[:8].upper()}",
    )


def start(setup, allowance=None):
    values, controller = setup
    agent = values[4]
    allowance = allowance or authorize(setup)
    state = controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    assert state["signer_state"] == "unlocked"
    return allowance


def packaged_identity(monkeypatch):
    identity = {
        "schema": "pumpwizard.build-identity/v1",
        "build_id": "fixture-service-acceptance",
        "version": "0.2.0.test",
        "source_checkpoint": "ab" * 32,
        "packaged_at": "2026-09-29T12:00:00+00:00",
        "package_kind": "linux-x86_64-onedir-local-candidate",
    }
    monkeypatch.setattr(service_acceptance, "current_build_identity", lambda: identity)
    return identity


def acceptance_capability(monkeypatch, wallet, allowance, **changes):
    identity = packaged_identity(monkeypatch)
    now = time.time()
    payload = {
        "schema": "pumpwizard.service-acceptance-approval/v1",
        "approval_id": "ac" * 16,
        "build_identity": identity,
        "custody": "main-wallet-service",
        "sender": allowance["sender"],
        "network": allowance["scope"]["network"],
        "genesis": allowance["genesis"],
        "session_id": allowance["session_id"],
        "strategy_digest": allowance["strategy_digest"],
        "allowance_id": allowance["id"],
        "authorization_revision": allowance["authorization_revision"],
        "allowance_binding_sha256": allowance_binding_sha256(allowance),
        "routes": [ROUTE],
        "approved_at": now,
        "expires_at": min(now + 300, allowance["expires_at"]),
        **changes,
    }
    message = acceptance_approval_message(payload)
    signature = base64.b64encode(wallet.sign(message)).decode()
    return payload, verify_acceptance_approval(payload, signature)


def test_service_executes_entry_and_exit_without_browser_and_pause_keeps_exits(setup):
    values, controller = setup
    wallet, rpc, journal, library, agent, _, _, _ = values
    strategy_risk.save_policy(
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        strategy_limits(max_trade_loss_bps=4_000),
        rpc.balance,
    )
    allowance = start(setup)

    # No browser session, heartbeat or client token participates in either cycle.
    first = controller.run_cycle()
    assert first and first["signature"]
    assert len(rpc.sent) == 1
    assert journal.trades(wallet.address)[0]["status"] == "pending"

    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    entry_position = journal.positions(wallet.address)[0]
    assert entry_position["status"] == "open"
    initial_quantity = int(entry_position["initial_quantity"])
    assert initial_quantity == 799_000_000
    entry_trade = journal.trades(wallet.address)[0]
    entry_receipt = entry_trade["receipt"]
    execution_cash_hold = entry_trade["payload"]["agent_order"]["cash_hold"]
    assert {
        key: entry_receipt[key]
        for key in ("fee_lamports", "wallet_delta", "token_delta")
    } == {
        "fee_lamports": 5_000,
        "wallet_delta": -81_944_280,
        "token_delta": 799_000_000,
    }
    assert next(
        row for row in reservation_rows(journal, wallet.address)
        if row["id"] == execution_cash_hold
    )["lamports"] == "118055720"

    paused = controller.pause_entries("Fixture entry pause")
    assert paused["entries_paused"] is True
    assert paused["authorization_status"] == "paused"
    observe(library, agent, time.time(), 320)
    second = controller.run_cycle()
    assert second and second["signature"]
    assert len(rpc.sent) == 2
    assert json.loads(
        journal.db.execute(
            "SELECT granted_permissions FROM main_wallet_allowances WHERE id=?",
            (allowance["id"],),
        ).fetchone()[0]
    )["automatic_entries"] is True

    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    partial_position = journal.positions(wallet.address)[0]
    assert partial_position["status"] == "open"
    assert int(partial_position["quantity"]) == initial_quantity * 6 // 10
    assert partial_position["remaining_cost"] == "49166568"
    assert partial_position["realized_proceeds"] == "31955000"
    assert next(
        row for row in reservation_rows(journal, wallet.address)
        if row["id"] == execution_cash_hold
    )["lamports"] == "150010720"

    observe(library, agent, time.time(), 170)
    third = controller.run_cycle()
    assert third and third["signature"]
    assert len(rpc.sent) == 3
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()

    trades = journal.trades(wallet.address)
    assert len(trades) == 3 and all(row["status"] == "finalized" for row in trades)
    buy = next(row for row in trades if row["payload"]["side"] == "buy")
    sells = sorted(
        (row for row in trades if row["payload"]["side"] == "sell"),
        key=lambda row: int(row["payload"]["amount"]),
    )
    assert buy["receipt"]["token_delta"] == initial_quantity
    assert [row["receipt"]["token_delta"] for row in sells] == [
        -initial_quantity * 4 // 10,
        -initial_quantity * 6 // 10,
    ]
    assert all(
        0 < row["receipt"]["fee_lamports"] <= row["payload"]["fee"]
        for row in trades
    )
    closed = journal.positions(wallet.address)[0]
    assert closed["status"] == "closed" and closed["quantity"] == "0"
    assert closed["remaining_cost"] == "0"
    assert closed["original_cost"] == closed["realized_cost"] == "81944280"
    assert closed["realized_proceeds"] == "79890000"
    holds = reservation_rows(journal, wallet.address)
    assert any(row["position_id"] == closed["id"] and row["token_atoms"] == "0" for row in holds)
    assert next(row for row in holds if row["id"] == execution_cash_hold)["lamports"] == "197945720"
    assert journal.db.execute("SELECT COUNT(*) FROM agent_fills").fetchone()[0] == 3
    allowance_orders = [
        (row["side"], int(row["debit"]))
        for row in journal.db.execute(
            "SELECT side,debit FROM main_wallet_allowance_orders WHERE allowance_id=?",
            (allowance["id"],),
        )
    ]
    assert sorted(allowance_orders) == [
        ("buy", 99_905_000),
        ("sell", 20_005_000),
        ("sell", 20_005_000),
    ]
    usage = controller.allowances(wallet.address, "mainnet-beta")[0]
    assert usage["used_orders"] == 3 and usage["used_lamports"] == 139_915_000
    public = json.dumps(controller.state(), sort_keys=True)
    assert PASSWORD not in public and "runtime_token" not in public and "ciphertext" not in public
    revoked = controller.revoke("Fixture revocation")
    assert revoked["authorization_status"] == "revoked"
    assert revoked["signer_state"] == "locked"
    with pytest.raises(TransferError, match="locked"):
        controller.run_cycle()


def test_pause_cancels_unsigned_buy_review_but_revoke_preserves_submitted_recovery(setup):
    values, controller = setup
    wallet, rpc, journal, library, _, _, _, _ = values
    allowance = start(setup)
    _, runtime, identity = controller._active()
    prepared = prepare_next_ticket(
        library,
        journal,
        rpc,
        controller.manager,
        allowance["id"],
        str(runtime["runtime_token"]),
    )
    ticket = prepared["ticket"]
    assert ticket is not None
    assert journal.db.execute(
        "SELECT status FROM agent_orders WHERE id=?", (ticket["order_id"],)
    ).fetchone()[0] == "review"

    paused = controller.pause_entries("Fixture pause before signing")
    assert paused["authorization_status"] == "paused"
    assert journal.db.execute(
        "SELECT status FROM agent_orders WHERE id=?", (ticket["order_id"],)
    ).fetchone()[0] == "cancelled"
    assert controller.manager.next_ticket(
        allowance["id"], str(runtime["runtime_token"])
    ) is None
    event = journal.db.execute(
        "SELECT kind,detail,payload FROM main_wallet_allowance_events "
        "WHERE allowance_id=? ORDER BY id DESC LIMIT 1",
        (allowance["id"],),
    ).fetchone()
    assert event["kind"] == "unsigned-reviews-cancelled"
    assert event["detail"] == "Fixture pause before signing"
    assert json.loads(event["payload"])["order_ids"] == [ticket["order_id"]]

    resumed = controller.resume_entries({
        "allowance_id": allowance["id"],
        "confirmation": f"RESUME {identity['session_id'][:8].upper()}",
    })
    assert resumed["authorization_status"] == "active"
    rpc.disconnect = True
    submitted = controller.run_cycle()
    assert submitted and submitted["order_id"] != ticket["order_id"]
    assert journal.db.execute(
        "SELECT status FROM agent_orders WHERE id=?", (submitted["order_id"],)
    ).fetchone()[0] == "submitted"
    assert journal.trades(wallet.address)[0]["status"] == "unknown"

    revoked = controller.revoke("Fixture revoke after uncertain submit")
    assert revoked["authorization_status"] == "revoked"
    assert revoked["recovery_required"] is True
    assert journal.db.execute(
        "SELECT status FROM agent_orders WHERE id=?", (submitted["order_id"],)
    ).fetchone()[0] == "submitted"
    rpc.disconnect = False
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    recovered = controller.reconcile()
    assert recovered["status"] == "locked"
    assert journal.trades(wallet.address)[0]["status"] == "finalized"
    assert len(rpc.sent) == 1


def test_restart_requires_fresh_exit_only_authorization_before_final_exit(setup):
    values, controller = setup
    wallet, rpc, journal, library, agent, _, _, directory = values
    strategy_risk.save_policy(
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        strategy_limits(max_trade_loss_bps=4_000),
        rpc.balance,
    )
    first_allowance = start(setup)
    buy = controller.run_cycle()
    assert buy and buy["signature"]
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    position = journal.positions(wallet.address)[0]
    initial_quantity = int(position["initial_quantity"])

    controller.pause_entries("Entry complete; exits only")
    observe(library, agent, time.time(), 320)
    partial = controller.run_cycle()
    assert partial and partial["signature"]
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    assert int(journal.position(position["id"])["quantity"]) == initial_quantity * 6 // 10

    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    assert restarted.allowances(wallet.address, "mainnet-beta")[0]["id"] == first_allowance["id"]
    assert restarted.allowances(wallet.address, "mainnet-beta")[0]["status"] == "locked"
    with pytest.raises(TransferError, match="not authorized"):
        restarted.start(
            allowance_id=first_allowance["id"],
            unlock_secret=PASSWORD,
            confirmation=f"START {agent[:8].upper()}",
        )

    set_service_brief(library, agent, "exits-only")
    cash = int(
        journal.db.execute(
            "SELECT lamports FROM agent_reservations WHERE id=?",
            (partial and journal.trades(wallet.address)[0]["payload"]["agent_order"]["cash_hold"],),
        ).fetchone()[0]
    )
    reviewed = restarted.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(
            budget_lamports=cash,
            max_order_debit_lamports=min(120_000_000, cash),
            max_daily_debit_lamports=min(180_000_000, cash),
        ),
    )
    assert reviewed["ready"]
    replacement = restarted.authorize(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        review_id=reviewed["id"],
        confirmation=f"ALLOW SERVICE {agent[:8].upper()}",
    )
    assert replacement["granted_permissions"]["automatic_entries"] is False
    restarted.start(
        allowance_id=replacement["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    observe(library, agent, time.time(), 170)
    final = restarted.run_cycle()
    assert final and final["signature"] and len(rpc.sent) == 3
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    restarted.reconcile()
    closed = journal.position(position["id"])
    assert closed["status"] == "closed"
    assert closed["quantity"] == closed["remaining_cost"] == "0"
    assert restarted.allowances(wallet.address, "mainnet-beta")[0]["used_orders"] == 1
    restarted.lock("Fixture teardown after restarted lifecycle")


def test_allowance_order_exhaustion_blocks_exit_without_an_extra_broadcast(setup):
    values, controller = setup
    wallet, rpc, journal, library, agent, _, _, _ = values
    strategy_risk.save_policy(
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        strategy_limits(max_trade_loss_bps=4_000),
        rpc.balance,
    )
    reviewed = controller.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(max_orders=1),
    )
    allowance = controller.authorize(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        review_id=reviewed["id"],
        confirmation=f"ALLOW SERVICE {agent[:8].upper()}",
    )
    controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    assert controller.run_cycle()["signature"]
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    observe(library, agent, time.time(), 320)

    with pytest.raises(TransferError, match="total order count is exhausted"):
        controller.run_cycle()
    usage = controller.allowances(wallet.address, "mainnet-beta")[0]
    assert usage["used_orders"] == 1
    assert len(rpc.sent) == 1
    assert journal.positions(wallet.address)[0]["status"] == "open"


def test_runtime_expiry_locks_signer_and_marks_allowance_expired(setup):
    values, controller = setup
    wallet = values[0]
    allowance = start(setup)
    signer, _, _ = controller._active()
    signer.expires_at = time.time() - 1

    expired = controller.state()
    assert expired["signer_state"] == "locked"
    assert expired["authorization_status"] == "expired"
    assert controller.allowances(wallet.address, "mainnet-beta")[0]["id"] == allowance["id"]
    assert controller.allowances(wallet.address, "mainnet-beta")[0]["status"] == "expired"
    with pytest.raises(TransferError, match="locked"):
        controller.run_cycle()


def test_ticket_message_and_signature_mutation_never_reserve_or_broadcast(setup):
    values, controller = setup
    wallet, rpc, journal, library, _, _, _, directory = values
    allowance = start(setup)
    signer, runtime, _ = controller._active()
    prepared = prepare_next_ticket(
        library,
        journal,
        rpc,
        controller.manager,
        allowance["id"],
        str(runtime["runtime_token"]),
    )
    ticket = prepared["ticket"]
    changed = {**ticket, "message_sha256": "00" * 32}
    with pytest.raises(Exception, match="message changed"):
        signer.sign_ticket(changed)

    signed = bytearray(base64.b64decode(signer.sign_ticket(ticket), validate=True))
    signed[1] ^= 1
    with pytest.raises(TransferError, match="exact authorized ticket"):
        submit_signed_ticket(
            library,
            rpc,
            directory,
            controller.manager,
            allowance_id=allowance["id"],
            runtime_token=str(runtime["runtime_token"]),
            ticket_id=ticket["id"],
            signed_transaction=base64.b64encode(signed).decode(),
        )
    assert rpc.sent == []
    assert journal.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert controller.allowances(wallet.address, "mainnet-beta")[0]["used_orders"] == 0


def test_custody_modes_cannot_activate_each_others_allowances(setup):
    values, controller = setup
    wallet, rpc, journal, _, agent, _, _, _ = values
    allowance = authorize(setup)
    with pytest.raises(TransferError, match="service-signing runtime"):
        autopilot.RuntimeManager(allow_synthetic=True).activate(
            journal,
            allowance["id"],
            sender=wallet.address,
            genesis=rpc.genesis(),
            wallet_kind="local",
        )
    controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    assert controller.state()["custody_mode"] == "main-wallet-service"


def test_production_activation_is_blocked_and_wrong_unlock_never_starts(setup):
    values, controller = setup
    wallet, rpc, _, library, agent, _, _, directory = values
    allowance = authorize(setup)
    rejected_secret = "wrong service password"
    with pytest.raises(Exception, match="password") as rejected:
        controller.start(
            allowance_id=allowance["id"],
            unlock_secret=rejected_secret,
            confirmation=f"START {agent[:8].upper()}",
        )
    assert rejected_secret not in str(rejected.value)
    assert rejected_secret not in json.dumps(controller.state(), sort_keys=True)
    assert all(
        rejected_secret.encode() not in path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    )
    assert controller.state()["signer_state"] == "locked"

    blocked = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(),
        rpc_factory=lambda _network: rpc,
    )
    # Constructing a new service controller is restart recovery: the durable
    # authorization is locked before any password can recreate signing access.
    assert blocked.allowances(wallet.address, "mainnet-beta")[0]["status"] == "locked"
    reviewed = blocked.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(),
    )
    replacement = blocked.authorize(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        review_id=reviewed["id"],
        confirmation=f"ALLOW SERVICE {agent[:8].upper()}",
    )
    with pytest.raises(TransferError, match="remains blocked"):
        blocked.start(
            allowance_id=replacement["id"],
            unlock_secret=PASSWORD,
            confirmation=f"START {agent[:8].upper()}",
        )
    assert blocked.state()["production_activation"] == "blocked"
    assert blocked.state()["activation_capability"] == {
        "available": False,
        "mode": "blocked",
        "blockers": [
            "Live main-wallet service signing has no funded lifecycle acceptance",
            "This build exposes only an injected synthetic service runtime",
        ],
    }
    assert library.instance(agent)["entry_mode"] != "automatic"


def test_restart_locks_active_signer_and_requires_fresh_authorization(setup):
    values, controller = setup
    wallet, rpc, _, _, agent, _, _, directory = values
    allowance = start(setup)
    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    rows = restarted.allowances(wallet.address, "mainnet-beta")
    assert rows[0]["id"] == allowance["id"] and rows[0]["status"] == "locked"
    with pytest.raises(TransferError, match="not authorized"):
        restarted.start(
            allowance_id=allowance["id"],
            unlock_secret=PASSWORD,
            confirmation=f"START {agent[:8].upper()}",
        )
    result = controller.run_cycle()
    assert not rpc.sent
    assert not result or "signature" not in result


def test_lost_response_reconciles_while_restart_is_locked_before_fresh_authorization(setup):
    values, controller = setup
    wallet, rpc, journal, library, agent, _, _, directory = values
    old_allowance = start(setup)
    rpc.disconnect = True
    submitted = controller.run_cycle()
    assert submitted and submitted["signature"]
    assert len(rpc.sent) == 1
    assert journal.trades(wallet.address)[0]["status"] == "unknown"
    duplicate = controller.run_cycle()
    assert duplicate and duplicate["ticket"] is None
    assert "unresolved" in duplicate["detail"]
    assert len(rpc.sent) == 1

    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    assert restarted.state()["signer_state"] == "locked"
    assert restarted.state()["recovery_required"] is True
    assert restarted.allowances(wallet.address, "mainnet-beta")[0]["status"] == "locked"
    rpc.disconnect = False
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    recovery = restarted.reconcile()
    assert recovery["status"] == "locked"
    assert len(rpc.sent) == 1
    assert journal.trades(wallet.address)[0]["status"] == "finalized"
    assert journal.positions(wallet.address)[0]["status"] == "open"
    assert restarted.allowances(wallet.address, "mainnet-beta")[0]["used_orders"] == 1

    with pytest.raises(TransferError, match="not authorized"):
        restarted.start(
            allowance_id=old_allowance["id"],
            unlock_secret=PASSWORD,
            confirmation=f"START {agent[:8].upper()}",
        )
    probe = restarted.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(),
    )
    current_cash = int(probe["cash_lamports"])
    reviewed = restarted.review(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        plan=plan(
            budget_lamports=current_cash,
            max_order_debit_lamports=100_000_000,
            max_daily_debit_lamports=100_000_000,
        ),
    )
    assert reviewed["ready"], (
        reviewed["preflight_blockers"],
        current_cash,
        reviewed.get("budget_lamports"),
        reviewed.get("cash_lamports"),
    )
    replacement = restarted.authorize(
        library,
        sender=wallet.address,
        network="mainnet-beta",
        session_id=agent,
        review_id=reviewed["id"],
        confirmation=f"ALLOW SERVICE {agent[:8].upper()}",
    )
    restarted.start(
        allowance_id=replacement["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    assert restarted.state()["authorization_status"] == "active"
    assert len(rpc.sent) == 1


def test_unresolved_submission_after_restart_keeps_cash_and_original_identity(setup):
    values, controller = setup
    wallet, rpc, journal, _, _, _, _, directory = values
    allowance = start(setup)
    rpc.disconnect = True
    submitted = controller.run_cycle()
    signature = submitted["signature"]
    submitted_trade = journal.trades(wallet.address)[0]
    execution_cash_hold = submitted_trade["payload"]["agent_order"]["cash_hold"]
    starting_cash = next(
        row for row in reservation_rows(journal, wallet.address)
        if row["id"] == execution_cash_hold
    )["lamports"]
    assert submitted_trade["status"] == "unknown"

    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    rpc.disconnect = False
    rpc.expire = True
    rpc.status = None
    first = restarted.reconcile()
    second = restarted.reconcile()

    trade = journal.trades(wallet.address)[0]
    assert first["status"] == second["status"] == "locked"
    assert trade["signature"] == signature and trade["status"] == "unknown"
    assert journal.pending(wallet.address, rpc.genesis())
    assert len(rpc.sent) == 1
    assert next(
        row for row in reservation_rows(journal, wallet.address)
        if row["id"] == execution_cash_hold
    )["lamports"] == starting_cash
    usage = restarted.allowances(wallet.address, "mainnet-beta")[0]
    assert usage["id"] == allowance["id"]
    assert usage["status"] == "locked" and usage["used_orders"] == 1
    with pytest.raises(TransferError, match="locked"):
        restarted.run_cycle()


def test_fee_only_failure_reconciles_after_restart_without_restoring_authority(setup):
    values, controller = setup
    wallet, rpc, journal, _, _, _, _, directory = values
    allowance = start(setup)
    rpc.disconnect = True
    submitted = controller.run_cycle()
    assert submitted and len(rpc.sent) == 1
    execution_cash_hold = journal.trades(wallet.address)[0]["payload"]["agent_order"]["cash_hold"]

    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda _network: rpc,
    )
    failure = {"InstructionError": [0, "Custom"]}
    rpc.disconnect = False
    rpc.status = {"confirmationStatus": "finalized", "err": failure}
    rpc.receipt["meta"]["err"] = failure
    rpc.receipt["meta"]["postBalances"][0] = (
        rpc.receipt["meta"]["preBalances"][0] - rpc.fee
    )
    recovery = restarted.reconcile()

    trade = journal.trades(wallet.address)[0]
    assert recovery["status"] == "locked"
    assert trade["status"] == "failed"
    assert trade["receipt"]["fee_lamports"] == 5_000
    assert trade["receipt"]["wallet_delta"] == -5_000
    assert trade["receipt"]["token_delta"] == 0
    assert not journal.pending(wallet.address, rpc.genesis())
    assert not journal.positions(wallet.address)
    assert next(
        row for row in reservation_rows(journal, wallet.address)
        if row["id"] == execution_cash_hold
    )["lamports"] == "199995000"
    usage = restarted.allowances(wallet.address, "mainnet-beta")[0]
    assert usage["id"] == allowance["id"]
    assert usage["status"] == "locked" and usage["used_orders"] == 1
    assert len(rpc.sent) == 1


def test_wallet_signed_acceptance_capability_runs_one_exact_audited_allowance(
    setup, monkeypatch
):
    values, controller = setup
    wallet, rpc, journal, _, agent, _, _, _ = values
    configure(values)
    cash = strategy_risk._cash_holds(
        journal, agent, rpc.genesis(), wallet.address,
    )[0]
    workspace_id = "shared-service-acceptance-workspace"
    owner_id = f"workspace:{workspace_id}"
    session_allocations.install(journal)
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
        journal,
        agent,
        rpc.genesis(),
        wallet.address,
        strategy_limits(max_trade_loss_bps=4_000),
        rpc.balance,
        workspace_id=workspace_id,
    )
    allowance = authorize(setup)
    packaged_identity(monkeypatch)
    controller.manager = ServiceRuntimeManager()
    assert controller.state()['production_activation'] == 'wallet-approval-required'
    with pytest.raises(TransferError, match='remains blocked'):
        controller.start(allowance_id=allowance['id'], unlock_secret=PASSWORD,
                         confirmation=f"START {agent[:8].upper()}")
    assert controller.state()['signer_state'] == 'locked'
    request = controller.acceptance_request(
        allowance_id=allowance["id"],
        routes=[ROUTE],
        duration_seconds=300,
    )
    payload = request["payload"]
    signature = base64.b64encode(
        wallet.sign(base64.b64decode(request["message"], validate=True))
    ).decode()
    available = controller.install_acceptance(payload, signature)

    assert available["production_activation"] == "wallet-approved"
    assert available["acceptance_activation"] == "available"
    assert available["activation_capability"]["mode"] == "acceptance"
    assert available["activation_capability"]["approval"]["build_id"] == (
        "fixture-service-acceptance"
    )

    state = controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    assert state["acceptance_activation"] == "active"
    assert state["production_activation"] == "active"
    submitted = controller.run_cycle()
    assert submitted and submitted["signature"]
    assert submitted["detail"] == "Submitted one exact reviewed acceptance transaction"
    assert len(rpc.sent) == 1
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    initial_quantity = int(journal.positions(wallet.address)[0]["initial_quantity"])

    controller.pause_entries("Acceptance entry complete")
    observe(values[3], agent, time.time(), 320)
    partial = controller.run_cycle()
    assert partial and partial["signature"] and len(rpc.sent) == 2
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    assert int(journal.positions(wallet.address)[0]["quantity"]) == initial_quantity * 6 // 10

    observe(values[3], agent, time.time(), 170)
    final = controller.run_cycle()
    assert final and final["signature"] and len(rpc.sent) == 3
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    assert journal.positions(wallet.address)[0]["status"] == "closed"
    assert journal.db.execute(
        "SELECT COUNT(*) FROM main_wallet_allowance_orders WHERE allowance_id=?",
        (allowance["id"],),
    ).fetchone()[0] == 3

    audit = journal.db.execute(
        "SELECT * FROM service_acceptance_approvals WHERE approval_id=?",
        (payload["approval_id"],),
    ).fetchone()
    assert audit is not None and audit["allowance_id"] == allowance["id"]
    assert json.loads(audit["payload"]) == payload
    assert audit["message_sha256"] == available["activation_capability"]["approval"][
        "message_sha256"
    ]
    event = journal.db.execute(
        "SELECT kind,payload FROM main_wallet_allowance_events "
        "WHERE allowance_id=? ORDER BY id DESC LIMIT 1",
        (allowance["id"],),
    ).fetchone()
    assert event["kind"] == "acceptance-activated"
    assert json.loads(event["payload"])["approval_id"] == payload["approval_id"]
    evidence = controller.acceptance_evidence(payload["approval_id"])
    assert evidence["schema"] == "pumpwizard.service-acceptance-evidence/v1"
    assert evidence["status"] == "reconciled"
    assert evidence["recovery_required"] is False
    assert evidence["allowance"]["binding_matches_approval"] is True
    assert evidence["allowance"]["used_orders"] == 3
    assert evidence["allowance"]["used_lamports"] == 139_915_000
    assert [order["side"] for order in evidence["orders"]] == ["buy", "sell", "sell"]
    assert all(order["route"] == ROUTE for order in evidence["orders"])
    assert all(order["status"] == "finalized" for order in evidence["orders"])
    assert all(order["receipt"] for order in evidence["orders"])
    encoded_evidence = json.dumps(evidence, sort_keys=True)
    assert PASSWORD not in encoded_evidence
    assert signature not in encoded_evidence
    with journal.db:
        journal.db.execute(
            "UPDATE service_acceptance_approvals SET signature=? WHERE approval_id=?",
            (base64.b64encode(bytes(64)).decode(), payload["approval_id"]),
        )
    with pytest.raises(TransferError, match="signature or message digest"):
        controller.acceptance_evidence(payload["approval_id"])


def test_wallet_signed_acceptance_runs_direct_pumpswap_buy_and_exits(
    setup, monkeypatch
):
    values, controller = setup
    wallet, original_rpc, journal, library, agent, _, _, _ = values
    swap_rpc = SwapRpc(wallet)
    swap_rpc.network = "mainnet-beta"
    swap_rpc.genesis = lambda: GENESIS["mainnet-beta"]
    swap_rpc.balance = original_rpc.balance
    swap_rpc.bank["curve"] = original_rpc.bank["curve"]
    swap_rpc.pool_base_tokens = 800_000_000_000_000
    swap_rpc.pool_quote_tokens = 120_000_000_000
    swap_rpc.graduated = True
    controller.rpc_factory = lambda _network: swap_rpc
    strategy_risk.save_policy(
        journal,
        agent,
        swap_rpc.genesis(),
        wallet.address,
        strategy_limits(max_trade_loss_bps=4_000),
        swap_rpc.balance,
    )
    allowance = authorize(setup)
    packaged_identity(monkeypatch)
    controller.manager = ServiceRuntimeManager()
    request = controller.acceptance_request(
        allowance_id=allowance["id"],
        routes=[SWAP_EXIT_ROUTE],
        duration_seconds=300,
    )
    signature = base64.b64encode(
        wallet.sign(base64.b64decode(request["message"], validate=True))
    ).decode()
    controller.install_acceptance(request["payload"], signature)
    controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )

    entry = controller.run_cycle()
    assert entry and entry.get("signature") and len(swap_rpc.sent) == 1, entry
    swap_rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    position = journal.positions(wallet.address)[0]
    initial_quantity = int(position["initial_quantity"])
    assert position["status"] == "open" and initial_quantity > 0
    entry_trade = journal.trades(wallet.address)[0]
    assert entry_trade["payload"]["route"] == SWAP_EXIT_ROUTE
    assert entry_trade["receipt"]["route"] == SWAP_EXIT_ROUTE

    controller.pause_entries("Direct PumpSwap entry complete")
    observe(library, agent, time.time(), 320)
    partial = controller.run_cycle()
    assert partial and partial["signature"] and len(swap_rpc.sent) == 2
    swap_rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    assert int(journal.position(position["id"])["quantity"]) == initial_quantity * 6 // 10

    observe(library, agent, time.time(), 170)
    final = controller.run_cycle()
    assert final and final["signature"] and len(swap_rpc.sent) == 3
    swap_rpc.status = {"confirmationStatus": "finalized", "err": None}
    controller.reconcile()
    assert journal.position(position["id"])["status"] == "closed"
    evidence = controller.acceptance_evidence(request["payload"]["approval_id"])
    assert evidence["status"] == "reconciled"
    assert [order["side"] for order in evidence["orders"]] == ["buy", "sell", "sell"]
    assert all(order["route"] == SWAP_EXIT_ROUTE for order in evidence["orders"])
    assert all(order["status"] == "finalized" for order in evidence["orders"])


def test_acceptance_request_cannot_self_certify_or_bypass_pre_unlock_binding(
    setup, monkeypatch
):
    values, controller = setup
    wallet, _, _, _, agent, _, _, _ = values
    allowance = authorize(setup)
    payload, _ = acceptance_capability(
        monkeypatch,
        wallet,
        allowance,
        allowance_id="ff" * 16,
    )
    message = acceptance_approval_message(payload)
    capability = verify_acceptance_approval(
        payload, base64.b64encode(wallet.sign(message)).decode()
    )

    with pytest.raises(TypeError, match="verified capability"):
        ServiceRuntimeManager(acceptance=payload)
    manager = ServiceRuntimeManager()
    manager.install_acceptance(capability)
    controller.manager = manager
    with pytest.raises(TransferError, match="exact allowance"):
        controller.start(
            allowance_id=allowance["id"],
            unlock_secret="this wrong password must not be evaluated",
            confirmation=f"START {agent[:8].upper()}",
        )
    assert controller.state()["signer_state"] == "locked"


def test_acceptance_state_marks_unresolved_original_signature_for_recovery(
    setup, monkeypatch
):
    values, controller = setup
    wallet, rpc, _, _, agent, _, _, directory = values
    allowance = authorize(setup)
    payload, capability = acceptance_capability(monkeypatch, wallet, allowance)
    manager = ServiceRuntimeManager()
    manager.install_acceptance(capability)
    controller.manager = manager
    controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )
    rpc.disconnect = True
    submitted = controller.run_cycle()
    assert submitted and submitted["signature"]
    assert controller.state()["recovery_required"] is True
    evidence = controller.acceptance_evidence(payload["approval_id"])
    assert evidence["status"] == "recovery-required"
    assert evidence["orders"][0]["status"] == "unknown"
    assert evidence["orders"][0]["signature"] == submitted["signature"]

    restarted = ServiceSigningController(
        directory,
        manager=ServiceRuntimeManager(),
        rpc_factory=lambda _network: rpc,
    )
    assert restarted.state()["signer_state"] == "locked"
    assert restarted.state()["recovery_required"] is True
    persisted = restarted.acceptance_evidence(payload["approval_id"])
    assert persisted["status"] == "recovery-required"
    assert persisted["allowance"]["status"] == "locked"
    with pytest.raises(TransferError, match="fresh authorized allowance"):
        restarted.install_acceptance(capability.payload, capability.signature)

    failure = {"InstructionError": [0, "Custom"]}
    rpc.disconnect = False
    rpc.status = {"confirmationStatus": "finalized", "err": failure}
    rpc.receipt["meta"]["err"] = failure
    rpc.receipt["meta"]["postBalances"][0] = (
        rpc.receipt["meta"]["preBalances"][0] - rpc.fee
    )
    restarted.reconcile()
    settled = restarted.acceptance_evidence(payload["approval_id"])
    assert settled["status"] == "reconciled"
    assert settled["recovery_required"] is False
    assert settled["orders"][0]["status"] == "failed"
    assert settled["orders"][0]["receipt"]["fee_lamports"] == 5_000
    assert settled["orders"][0]["receipt"]["token_delta"] == 0


def test_acceptance_route_scope_blocks_an_otherwise_authorized_order(setup, monkeypatch):
    values, controller = setup
    wallet, rpc, _, _, agent, _, _, _ = values
    allowance = authorize(setup)
    _, capability = acceptance_capability(
        monkeypatch,
        wallet,
        allowance,
        routes=[SWAP_EXIT_ROUTE],
    )
    manager = ServiceRuntimeManager()
    manager.install_acceptance(capability)
    controller.manager = manager
    controller.start(
        allowance_id=allowance["id"],
        unlock_secret=PASSWORD,
        confirmation=f"START {agent[:8].upper()}",
    )

    result = controller.run_cycle()
    assert result and result["ticket"] is None
    assert "outside the acceptance approval" in result["detail"]
    assert not rpc.sent


def test_acceptance_policy_change_invalidates_before_service_password_unlock(
    setup, monkeypatch
):
    values, controller = setup
    wallet, rpc, journal, _, agent, _, _, _ = values
    allowance = authorize(setup)
    _, capability = acceptance_capability(monkeypatch, wallet, allowance)
    manager = ServiceRuntimeManager()
    manager.install_acceptance(capability)
    controller.manager = manager
    wallet_risk.save_policy(
        journal,
        rpc.genesis(),
        wallet.address,
        wallet_limits(reserve_lamports=110_000_000),
    )

    with pytest.raises(TransferError, match="policy revision changed"):
        controller.start(
            allowance_id=allowance["id"],
            unlock_secret="wrong password must not be evaluated",
            confirmation=f"START {agent[:8].upper()}",
        )
    assert controller.state()["signer_state"] == "locked"


def test_entries_only_service_keeps_exits_manual_on_start_pause_and_resume(setup):
    values, controller = setup
    library, agent = values[3:5]
    set_service_brief(library, agent, "entries-only")
    allowance = start(setup)
    assert allowance["granted_permissions"]["automatic_entries"] is True
    assert allowance["granted_permissions"]["automatic_protective_exits"] is False
    assert library.instance(agent)["entry_mode"] == "automatic"
    assert library.instance(agent)["exit_mode"] == "manual"
    result = controller.run_cycle()
    assert result and result["signature"]
    assert len(values[1].sent) == 1
    identity = {"allowance_id": allowance["id"], "session_id": agent}
    controller._set_execution_modes(identity, active=True, paused=True)
    assert library.instance(agent)["entry_mode"] == "disabled"
    assert library.instance(agent)["exit_mode"] == "manual"
    controller._set_execution_modes(identity, active=True, paused=False)
    assert library.instance(agent)["entry_mode"] == "automatic"
    assert library.instance(agent)["exit_mode"] == "manual"
