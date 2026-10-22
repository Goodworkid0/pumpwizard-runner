import pytest

from pumpwizard_runner.holding_valuation import (
    holding_valuation,
    retained_holding_valuation,
    wallet_asset_valuation,
    wallet_position_valuation,
)


@pytest.mark.parametrize(("observed_at", "expected"), [
    (None, "missing_time"),
    ("not-a-time", "invalid_time"),
    (879, "stale"),
    (1_001, "future"),
    (880, "fresh"),
])
def test_mark_time_states_control_current_value(observed_at, expected):
    value = holding_valuation(
        quantity="2", quantity_unit="token-units",
        basis="1", basis_currency="USD",
        mark_price="3", mark_currency="USD",
        mark_observed_at=observed_at, mark_source="fixture", now=1_000,
        purchase_attribution="user_manual", purchase_owner_id=None,
        current_management="user",
        manager_id=None, fees_state="unknown",
        realized_proceeds=None, realized_currency="USD",
    )
    assert value["mark"]["freshness"] == expected
    assert value["current_value"]["amount"] == ("6" if expected == "fresh" else None)
    assert value["executable_liquidation"] == {
        "state": "unavailable",
        "amount": None,
        "currency": "USD",
        "reason": "A fresh reviewed sell quote and its costs are required.",
    }


def test_tiny_wallet_quantity_uses_decimal_precision_without_rounding_to_zero():
    value = wallet_asset_valuation(
        amount_atoms="1", decimals=18, price_usd="0.000001",
        source_slot=10, current_slot=11, mark_source="fixture", now=1_000,
    )
    assert value["quantity"]["amount"] == "0.000000000000000001"
    assert value["current_value"]["amount"] == "0.000000000000000000000001"
    assert value["basis"]["state"] == "unknown"
    assert value["external_flows"]["strategy_profit_treatment"] == "excluded"


def test_session_purchase_handed_back_keeps_purchase_and_management_distinct():
    value = wallet_position_valuation({
        "quantity": "500000", "decimals": 6,
        "remaining_cost": "500000000", "entry_fx": "100",
        "original_cost": "1000000000",
        "realized_proceeds": "750000000", "transaction_fees": "6000",
        "agent_id": "session-7", "managed": False,
    }, price_usd="2", observed_at=995, mark_source="fixture", now=1_000)
    assert value["ownership"] == {
        "purchase_attribution": "session",
        "purchase_owner_id": "session-7",
        "current_management": "user",
        "manager_id": None,
    }
    assert value["quantity"]["amount"] == "0.5"
    assert value["basis"]["amount"] == "50"
    assert value["basis"]["native_amount"] == "500000000"
    assert value["basis"]["native_currency"] == "lamports"
    assert value["current_value"]["amount"] == "1"
    assert value["unrealized_result"]["amount"] == "-49"
    assert value["fees"] == {
        "state": "retained_lamports", "amount": "6000",
        "currency": "lamports", "exit_costs_included": False,
    }


def test_manual_position_without_entry_fx_does_not_invent_usd_basis():
    value = wallet_position_valuation({
        "quantity": "250000", "decimals": 6,
        "remaining_cost": "9000000", "entry_fx": None,
        "realized_proceeds": "0", "transaction_fees": None,
        "agent_id": None, "managed": False,
    }, price_usd="4", observed_at=995, mark_source="fixture", now=1_000)
    assert value["ownership"]["purchase_attribution"] == "user_manual"
    assert value["ownership"]["current_management"] == "user"
    assert value["basis"] == {
        "state": "unknown", "amount": None, "currency": "USD",
        "source": "missing_entry_fx",
        "native_state": "known", "native_amount": "9000000",
        "native_currency": "lamports",
    }
    assert value["current_value"]["amount"] == "1"
    assert value["unrealized_result"]["state"] == "unknown"
    assert "unknown_cost_basis" in value["blockers"]


def test_retained_manual_holding_is_external_purchase_currently_user_managed():
    value = retained_holding_valuation({
        "quantity": "0.00000001", "remaining_cost_usd": "0.50",
        "realized_proceeds_usd": "0", "management_mode": "manual",
        "instance_id": "session-1",
    }, {"price_usd": "2", "observed_at": 995}, now=1_000)
    assert value["quantity"]["amount"] == "0.00000001"
    assert value["ownership"] == {
        "purchase_attribution": "user_reported_external_fill",
        "purchase_owner_id": None,
        "current_management": "user",
        "manager_id": None,
    }
    assert value["current_value"]["state"] == "estimated"
    assert value["realized_proceeds"]["profit_treatment"] == (
        "not_profit_without_matched_basis_and_costs"
    )


def test_mixed_purchase_and_management_states_remain_independent():
    session_managed = wallet_position_valuation({
        "quantity": "1", "decimals": 0, "remaining_cost": "1",
        "entry_fx": "1", "realized_proceeds": "0", "transaction_fees": "0",
        "agent_id": "session-active", "managed": True,
    }, price_usd="1", observed_at=10, mark_source="fixture", now=11)
    user_manual = wallet_position_valuation({
        "quantity": "1", "decimals": 0, "remaining_cost": "1",
        "entry_fx": None, "realized_proceeds": "0", "transaction_fees": "0",
        "agent_id": None, "managed": False,
    }, price_usd="1", observed_at=10, mark_source="fixture", now=11)
    assert session_managed["ownership"] == {
        "purchase_attribution": "session",
        "purchase_owner_id": "session-active",
        "current_management": "session",
        "manager_id": "session-active",
    }
    assert user_manual["ownership"] == {
        "purchase_attribution": "user_manual",
        "purchase_owner_id": None,
        "current_management": "user",
        "manager_id": None,
    }


@pytest.mark.parametrize(("source_slot", "current_slot", "expected"), [
    (1, 6_002, "stale"),
    (6_002, 1, "future"),
    ("invalid", 6_002, "invalid_time"),
])
def test_every_supplied_freshness_signal_must_be_valid_and_fresh(
    source_slot, current_slot, expected,
):
    value = wallet_position_valuation({
        "quantity": "1", "decimals": 0, "remaining_cost": "1",
        "entry_fx": "1", "realized_proceeds": "0", "transaction_fees": "0",
        "agent_id": None, "managed": False,
    }, price_usd="2", observed_at=999, source_slot=source_slot,
       current_slot=current_slot, mark_source="fixture", now=1_000)
    assert value["mark"]["freshness"] == expected
    assert value["mark"]["age_seconds"] == 1
    assert value["current_value"]["state"] == "unknown"
