from pathlib import Path

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder


def quote(at: float = 100, *, source_at: float = 90) -> MarketQuote:
    return MarketQuote(
        "Mint", "TEST", at, "curve", "Pool", 15_000, 1, .01,
        1_000, 10, source="fixture-provider", quote_usd=100,
        source_at=source_at,
    )


def begin(recorder: ServiceEvidenceRecorder) -> str:
    return recorder.begin_scan(
        minimum_cap=10_000, maximum_cap=20_000, source="FixtureSource",
        priority={"Mint"}, limit=24,
    )


def provider_diagnostics(*, status: str = "partial") -> dict[str, object]:
    return {
        "schema": "pumpwizard.provider-diagnostics/v1",
        "provider": "CompositePumpMarketSource",
        "started_at": 190.0,
        "finished_at": 199.0,
        "status": status,
        "bounds": {
            "maximum_http_requests": 6,
            "maximum_elapsed_seconds": 60,
            "maximum_response_bytes": 12_000_000,
            "maximum_concurrency": 2,
        },
        "stages": {
            "fx_requests": 1,
            "discovery_requests": 2,
            "listing_rows": 4,
            "unique_listings": 3,
            "eligible_candidates": 2,
            "selected_records": 2,
            "selected_detail_requests": 0,
            "returned_details": 0,
            "normalized_quotes": 1,
            "http_requests": 3,
            "usable_discovery_responses": 2,
            "response_bytes": 2_048,
        },
        "rejections": {"invalid_identity_or_units": 1},
        "provenance": {
            "provider_hosts": ["frontend-api-v3.pump.fun"],
            "endpoint_classes": ["sol_usd_reference", "curve_discovery_listing"],
        },
        "readiness": {
            "ready": True,
            "routes": ["curve"],
            "reasons": ["selected_records_rejected"],
            "first_received_at": 195.0,
            "earliest_source_at": 190.0,
            "latest_source_at": 190.0,
            "maximum_source_age_seconds": 9.0,
        },
    }


def test_provider_diagnostics_preserve_known_reads_and_unknown_wire_bytes(tmp_path: Path):
    clock = [200.0]
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: clock[0])
    try:
        run_id = recorder.begin_scan(
            minimum_cap=10_000, maximum_cap=20_000, source="FixtureSource",
            priority={"Mint"}, limit=24,
            campaign_consumers={"campaign-test": 1},
        )
        diagnostics = provider_diagnostics(status="healthy")
        diagnostics["stages"]["response_bytes"] = None
        recorder.complete_scan(run_id, [quote()], diagnostics=diagnostics)
        usage = recorder.provider_usage("campaign-test")
        assert usage["http_reads"] == 3
        assert usage["unknown_provider_response_byte_requests"] == 1
        record = recorder.provider_history()["records"][0]
        assert record["diagnostics"]["stages"]["http_requests"] == 3
        assert record["provider_response_bytes"] is None
    finally:
        recorder.close()


def test_records_event_source_and_first_receipt_without_duplicate_raw_rows(tmp_path: Path):
    clock = [200.0]
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: clock[0])
    try:
        first = begin(recorder)
        completed = recorder.complete_scan(first, [quote(), quote()])
        assert completed["status"] == "complete"
        assert completed["quote_count"] == 1
        assert completed["earliest_event_at"] == 100
        assert completed["earliest_source_at"] == 90

        clock[0] = 220
        second = begin(recorder)
        recorder.complete_scan(second, [quote()])
        evidence = recorder.raw_evidence()
        assert len(evidence) == 1
        assert evidence[0]["observed_at"] == 100
        assert evidence[0]["source_at"] == 90
        assert evidence[0]["first_received_at"] == 200
        assert len(recorder.coverage()) == 2
    finally:
        recorder.close()


def test_provider_failures_and_crash_recovery_create_visible_gaps_with_backoff(tmp_path: Path):
    clock = [200.0]
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: clock[0])
    try:
        first = begin(recorder)
        failed = recorder.fail_scan(first, "https://provider.test/path?api-key=secret")
        assert failed["status"] == "gap"
        assert failed["gap_count"] == 1
        assert failed["gap_reason"].endswith("?[redacted]")
        assert failed["next_retry_at"] == 205

        clock[0] = 210
        second = begin(recorder)
        failed_again = recorder.fail_scan(second, RuntimeError("offline"))
        assert failed_again["consecutive_failures"] == 2
        assert failed_again["next_retry_at"] == 220

        clock[0] = 230
        abandoned = begin(recorder)
        assert recorder.recover_incomplete() == 1
        assert recorder.run(abandoned)["gap_reason"].startswith("Service stopped")
    finally:
        recorder.close()


def test_provider_diagnostics_are_validated_retained_and_exposed_read_only(tmp_path: Path):
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: 200.0)
    try:
        run_id = begin(recorder)
        completed = recorder.complete_scan(
            run_id, [quote()], diagnostics=provider_diagnostics(),
        )
        assert completed["provider_diagnostics"]["status"] == "partial"
        assert completed["telemetry"]["provider_response_bytes"] == 2_048
        assert completed["telemetry"]["excluded_rows"] == 1
        history = recorder.provider_history()["records"][0]
        assert history["diagnostics"]["stages"]["selected_detail_requests"] == 0
        assert history["provider_response_bytes"] == 2_048

        invalid = provider_diagnostics()
        invalid["rejections"] = {"secret_provider_reason": 1}
        collecting = begin(recorder)
        with pytest.raises(ValueError, match="rejection count"):
            recorder.complete_scan(collecting, [], diagnostics=invalid)
        assert recorder.run(collecting)["status"] == "collecting"
    finally:
        recorder.close()


def test_schema_rejects_newer_state_and_compaction_is_bounded(tmp_path: Path):
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: 10_000.0)
    try:
        run_id = begin(recorder)
        recorder.complete_scan(run_id, [quote(100)])
        # Retention is based on truthful first receipt, not an older event time.
        assert recorder.compact(now=20_000, retention_seconds=3_600,
                                max_evidence=100, max_runs=100) == {
            "runs_removed": 1, "evidence_removed": 1,
        }
        with recorder.db:
            recorder.db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    finally:
        recorder.close()
    with pytest.raises(ValueError, match="newer PumpWizard"):
        ServiceEvidenceRecorder(tmp_path)
