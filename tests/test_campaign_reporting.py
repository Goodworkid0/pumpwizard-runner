from __future__ import annotations

import json

import pytest

from pumpwizard_runner.campaign_performance import build_campaign_report
from pumpwizard_runner.evidence_alerts import EvidenceAlertStore
from pumpwizard_runner.forward_campaigns import START_CONFIRMATION, ForwardCampaignStore
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.receipt_evidence import normalize_receipts, retained_receipts
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.transfers import GENESIS

from test_forward_campaigns import Clock, quote, request


def _observed_campaign(tmp_path, clock):
    with ForwardCampaignStore(tmp_path, clock=clock) as store:
        campaign = store.start(
            request(), idempotency_key="report-campaign", confirmation=START_CONFIRMATION,
        )
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    for index, price in enumerate((1.0, 1.1, 1.2)):
        at = clock.value + index
        run_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            priority=["MintCampaign"], limit=24,
            requested_at=at, started_at=at,
            campaign_consumers={campaign["id"]: 2},
        )
        recorder.complete_scan(run_id, [quote(at, price=price)], received_at=at + 0.1)
    clock.value += 10
    worker = PaperResearchWorker(tmp_path, recorder, clock=clock)
    try:
        worker.run_once()
    finally:
        worker.close()
    return campaign, recorder


def test_campaign_report_uses_matched_evidence_and_append_only_snapshots(tmp_path):
    clock = Clock(10_000)
    campaign, recorder = _observed_campaign(tmp_path, clock)
    try:
        with ForwardCampaignStore(tmp_path, clock=clock) as store:
            first = build_campaign_report(
                store, recorder, campaign["id"], created_at=10_020,
            )
            repeated = build_campaign_report(
                store, recorder, campaign["id"], created_at=10_020,
            )
            assert repeated == first
            assert first["matched_coverage"]["identical_source_evidence"] is True
            assert first["matched_coverage"]["matched_evidence_count"] == 3
            assert first["ranking"]["comparable"] is True
            assert {row["id"] for row in first["benchmarks"]} == {"cash", "holding"}
            holding = next(row for row in first["benchmarks"] if row["id"] == "holding")
            assert holding["last_price_quote_units_per_token"] > \
                holding["first_price_quote_units_per_token"]
            assert first["provider_cost"]["cost_status"] == "unknown_or_partial"
            assert first["claims"]["fully_costed"] is False
            assert first["readiness"]["state"] == "exploratory"
            second = build_campaign_report(
                store, recorder, campaign["id"], previous_report_id=first["id"],
                created_at=10_021,
            )
            assert second["id"] != first["id"]
            assert second["previous_report_id"] == first["id"]
            assert [row["id"] for row in store.reports(campaign["id"])] == [
                second["id"], first["id"],
            ]
    finally:
        recorder.close()


def test_uneven_arm_uptime_withholds_ranking(tmp_path):
    clock = Clock(20_000)
    campaign, recorder = _observed_campaign(tmp_path, clock)
    try:
        with ForwardCampaignStore(tmp_path, clock=clock) as store:
            losing_arm = campaign["arms"][1]["paper_run_id"]
            with store.db:
                store.db.execute(
                    """DELETE FROM paper_observations WHERE run_id=? AND evidence_id=(
                           SELECT evidence_id FROM paper_observations WHERE run_id=?
                           ORDER BY first_received_at DESC LIMIT 1
                       )""",
                    (losing_arm, losing_arm),
                )
            report = build_campaign_report(
                store, recorder, campaign["id"], created_at=20_020,
            )
            assert report["matched_coverage"]["identical_source_evidence"] is False
            assert report["ranking"]["comparable"] is False
            assert report["ranking"]["ordered_ids"] == []
            assert "arm_coverage_mismatch" in report["readiness"]["reasons"]
    finally:
        recorder.close()


def test_local_alert_acknowledgment_resolution_and_recurrence(tmp_path):
    clock = Clock(30_000)
    campaign = request(max_requests=1)
    with ForwardCampaignStore(tmp_path, clock=clock) as store:
        started = store.start(
            campaign, idempotency_key="alert-campaign", confirmation=START_CONFIRMATION,
        )
        store.record_scan([started["id"]], success=True, finished_at=30_001)
        view = store.campaign(started["id"])
    alerts = EvidenceAlertStore(tmp_path, clock=clock)
    try:
        assert alerts.evaluate(view, observation_id="quota-1")["opened"] == 1
        first = alerts.alerts(campaign_id=started["id"])[0]
        assert first["condition_key"] == "provider:quota:request_quota"
        acknowledged = alerts.acknowledge(first["id"])
        assert acknowledged["acknowledged_at"] == 30_000
        clock.value = 30_010
        assert alerts.evaluate(view, observation_id="quota-2")["updated"] == 1
        assert alerts.alerts(campaign_id=started["id"])[0]["state"] == "open"

        cleared = {**view, "state": "paused", "blocker": None}
        assert alerts.evaluate(cleared, observation_id="cleared-1")["resolved"] == 1
        clock.value = 30_020
        assert alerts.evaluate(view, observation_id="quota-3")["opened"] == 1
        rows = alerts.alerts(campaign_id=started["id"])
        assert {row["generation"] for row in rows} == {1, 2}
        assert rows[0]["acknowledged_at"] is None
    finally:
        alerts.close()


def test_retained_receipts_are_read_only_deduplicated_and_classified(tmp_path):
    journal = TradeJournal(tmp_path)
    try:
        payload = {
            "sender": "Sender", "mint": "Mint", "side": "buy", "route": "curve",
            "agent_order": False,
        }
        receipt = {
            "err": None, "fee_lamports": 5_000, "wallet_delta": -1_000_000,
            "token_delta": 100, "signature": "sig-success",
        }
        with journal.db:
            journal.db.execute(
                "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
                ("trade-success", GENESIS["devnet"], "Sender", "sig-success",
                 "finalized", 1.0, json.dumps(payload), "done", json.dumps(receipt)),
            )
            journal.db.execute(
                "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
                ("trade-pending", GENESIS["devnet"], "Sender", "sig-pending",
                 "pending", 2.0, json.dumps(payload), "waiting", None),
            )
            journal.db.execute(
                """INSERT INTO transfers(
                       id,network,genesis,sender,recipient,lamports,fee,signature,
                       last_valid_height,created_at,status,detail
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("withdrawal", "devnet", GENESIS["devnet"], "Sender", "Recipient",
                 "100", "5", "sig-withdrawal", 99, 3.0, "finalized", "done"),
            )
    finally:
        journal.close()
    evidence = retained_receipts(tmp_path, sender="Sender")
    assert evidence["read_only"] is True and evidence["rpc_queried"] is False
    assert evidence["counts"]["finalized_success"] == 2
    assert evidence["counts"]["pending"] == 1
    withdrawal = next(row for row in evidence["records"] if row["kind"] == "withdrawal")
    assert withdrawal["return_treatment"] == "excluded_from_strategy_return"
    assert all(row["paper_fill"] is False for row in evidence["records"])

    fixture = evidence["records"][0]
    assert normalize_receipts([fixture, fixture]) == [fixture]
    conflict = {**fixture, "state": "unresolved"}
    with pytest.raises(ValueError, match="conflicting"):
        normalize_receipts([fixture, conflict])
