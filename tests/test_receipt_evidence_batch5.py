from __future__ import annotations

import json

import pytest

from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.receipt_evidence import (
    ReceiptEvidenceError,
    compare_receipt_expectations,
    normalize_retained_receipts,
    read_trade_journal_receipts,
)


def trade(
    ident: str,
    signature: str,
    status: str,
    *,
    receipt=None,
    agent_id: str | None = None,
):
    payload = {
        "network": "devnet",
        "side": "buy",
        "mint": "mint-a",
        "decimals": 6,
    }
    if agent_id is not None:
        payload["agent_order"] = {"agent_id": agent_id}
    return {
        "id": ident,
        "genesis": "devnet-genesis",
        "sender": "wallet",
        "signature": signature,
        "status": status,
        "created_at": 10,
        "payload": payload,
        "detail": "fixture",
        "receipt": receipt,
    }


def success(signature="sig-success"):
    return {
        "err": None,
        "fee_lamports": 5_000,
        "wallet_delta": -105_000,
        "token_delta": 2_000_000,
        "block_time": 20,
        "signature": signature,
    }


def failure(signature="sig-failure"):
    return {
        "err": {"InstructionError": [2, "Custom"]},
        "fee_lamports": 5_000,
        "wallet_delta": -5_000,
        "token_delta": 0,
        "block_time": 21,
        "signature": signature,
    }


def test_receipt_classes_deduplication_and_strategy_ownership_are_explicit():
    finalized = trade(
        "trade-success", "sig-success", "finalized", receipt=success(), agent_id="agent-1"
    )
    report = normalize_retained_receipts(
        [
            finalized,
            finalized,
            trade("trade-failure", "sig-failure", "failed", receipt=failure()),
            trade("trade-pending", "sig-pending", "confirmed"),
            trade("trade-unknown", "sig-unknown", "unknown"),
        ],
        as_of=30,
        source_label="deterministic-synthetic-fixture",
        agent_fill_ids=["trade-success"],
    )
    assert report["counts"] == {
        "pending": 1,
        "finalized_success": 1,
        "fee_only_failure": 1,
        "unresolved": 1,
        "deduplicated_rows": 1,
        "retained": 4,
    }
    saved = next(row for row in report["receipts"] if row["journal_record_id"] == "trade-success")
    assert saved["amounts"] == {
        "fee_lamports": 5_000,
        "cash_delta_lamports": -105_000,
        "token_delta_atoms": 2_000_000,
    }
    assert saved["ownership"] == {
        "kind": "strategy_instance",
        "agent_id": "agent-1",
        "evidence": "retained authorized trade payload",
        "agent_fill_link_retained": True,
    }
    assert saved["evidence_class"] == "recorded_execution"
    assert saved["paper_fill"] is False


def test_conflicting_duplicate_and_non_fee_only_failure_fail_closed():
    left = trade("one", "same-signature", "finalized", receipt=success("same-signature"))
    changed = trade("two", "same-signature", "finalized", receipt=success("same-signature"))
    with pytest.raises(ReceiptEvidenceError, match="conflicting evidence"):
        normalize_retained_receipts([left, changed], as_of=30)

    bad = failure()
    bad["wallet_delta"] = -6_000
    report = normalize_retained_receipts(
        [trade("bad", "sig-failure", "failed", receipt=bad)], as_of=30
    )
    assert report["receipts"][0]["classification"] == "unresolved"
    assert report["counts"]["fee_only_failure"] == 0


def test_read_only_adapter_uses_existing_journal_without_reconciliation(tmp_path):
    journal = TradeJournal(tmp_path)
    payload = json.dumps({
        "network": "devnet",
        "side": "buy",
        "mint": "mint-a",
        "decimals": 6,
        "agent_order": {"agent_id": "agent-1"},
    })
    receipt = json.dumps(success())
    with journal.db:
        journal.db.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "trade-1", "devnet-genesis", "wallet", "sig-success", "finalized",
                10, payload, "fixture", receipt,
            ),
        )
        journal.db.execute(
            "INSERT INTO agent_fills VALUES (?,?,?,?)",
            ("trade-1", "agent-1", "trade-1", receipt),
        )
        journal.db.execute(
            "INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "withdrawal-1", "devnet", "devnet-genesis", "wallet", "recipient",
                "100000", "5000", "sig-withdrawal", 10, 11, "finalized", "fixture",
            ),
        )
    journal.close()
    path = tmp_path / "wallet-transfers.sqlite"
    before = path.stat().st_mtime_ns

    report = read_trade_journal_receipts(
        tmp_path,
        as_of=30,
        sender="wallet",
        source_label="deterministic-synthetic-fixture",
    )

    assert path.stat().st_mtime_ns == before
    assert report["source"]["read_only"] is True
    assert report["source"]["arbitrary_wallet_history_queried"] is False
    assert {row["record_kind"] for row in report["receipts"]} == {"trade", "withdrawal"}
    withdrawal = next(row for row in report["receipts"] if row["record_kind"] == "withdrawal")
    assert withdrawal["classification"] == "finalized_success"
    assert withdrawal["amounts"]["cash_delta_lamports"] == -105_000
    assert withdrawal["receipt_status"] == "finality-status-with-submission-validated-fee"


def test_expectation_comparison_never_relabels_paper_as_execution():
    receipts = normalize_retained_receipts(
        [trade("one", "sig-success", "finalized", receipt=success())], as_of=30
    )
    receipt_id = receipts["receipts"][0]["id"]
    comparison = compare_receipt_expectations(receipts, [{
        "id": "paper-expectation",
        "evidence_class": "observed_paper",
        "receipt_id": receipt_id,
        "cash_delta_lamports": -105_000,
        "token_delta_atoms": 2_000_000,
        "fee_lamports": 5_000,
    }])
    assert comparison["matched"] == 1
    assert comparison["comparisons"][0]["status"] == "matched"
    assert comparison["comparisons"][0]["evidence_class"] == "observed_paper"
    assert comparison["paper_fills_reclassified_as_execution"] is False
