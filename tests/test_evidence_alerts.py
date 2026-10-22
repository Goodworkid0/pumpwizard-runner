from __future__ import annotations

import json

import pytest

from pumpwizard_runner.evidence_alerts import (
    ALERT_SCHEMA,
    EXPORT_SCHEMA,
    INBOX_SCHEMA,
    EvidenceAlertError,
    EvidenceAlertStore,
)


def campaign(**overrides):
    value = {
        "id": "campaign-1",
        "state": "running",
        "updated_at": 100.0,
        "blocker": None,
        "service": {"status": "healthy", "repair": None},
        "coverage": {
            "max_source_age_seconds": 30,
            "minimum_observations": 10,
        },
        "usage": {"maximum_source_age_seconds": 2},
        "arms": [{
            "id": "arm-a",
            "paper_run_id": "paper-a",
            "state": "running",
            "counts": {"observations": 2},
            "fault": None,
        }],
    }
    value.update(overrides)
    return value


def problem_campaign():
    return campaign(
        state="capped",
        blocker={
            "code": "request_quota",
            "message": "The request ceiling was reached.",
            "repair": "Start a deliberate new version with reviewed limits.",
        },
        usage={"maximum_source_age_seconds": 75},
        arms=[{
            "id": "arm-a",
            "paper_run_id": "paper-a",
            "state": "interrupted",
            "counts": {"observations": 10},
            "fault": {
                "message": "The evaluator rejected one retained row.",
                "repair": "Inspect the durable paper fault.",
            },
        }],
    )


def mismatch():
    return {
        "id": "receipt-gap-1",
        "reason": "Recorded fee does not match retained cash movement.",
        "repair": "Inspect the source-linked receipt.",
    }


def test_campaign_conditions_have_source_links_and_required_kinds(tmp_path):
    with EvidenceAlertStore(tmp_path) as store:
        result = store.evaluate(
            problem_campaign(),
            accounting_mismatches=[mismatch()],
            observation_id="snapshot-1",
            evaluated_at=200.0,
        )
        assert result == {"opened": 5, "updated": 0, "resolved": 0, "duplicate": False}
        inbox = store.inbox("campaign-1")
    assert inbox["schema"] == INBOX_SCHEMA
    assert {row["kind"] for row in inbox["alerts"]} == {
        "provider-quota-exhausted",
        "stale-source-data",
        "arm-faulted",
        "accounting-mismatch",
        "evidence-milestone",
    }
    assert all(row["schema"] == ALERT_SCHEMA for row in inbox["alerts"])
    assert all(row["source"]["href"].startswith("/") for row in inbox["alerts"])
    assert all(row["local_only"] and not row["external_delivery"] for row in inbox["alerts"])


def test_duplicate_observation_and_acknowledgment_do_not_change_condition(tmp_path):
    with EvidenceAlertStore(tmp_path) as store:
        store.evaluate(problem_campaign(), observation_id="same", evaluated_at=200.0)
        alert = next(
            row for row in store.alerts(campaign_id="campaign-1")
            if row["kind"] == "provider-quota-exhausted"
        )
        acknowledged = store.acknowledge(alert["id"], acknowledged_at=210.0)
        assert acknowledged["state"] == "open"
        assert acknowledged["acknowledged_at"] == 210.0
        assert acknowledged["acknowledgment_changes_condition"] is False

        duplicate = store.evaluate(
            problem_campaign(), observation_id="same", evaluated_at=220.0
        )
        assert duplicate["duplicate"] is True
        unchanged = store.get(alert["id"])
        assert unchanged is not None
        assert unchanged["occurrence_count"] == 1
        assert unchanged["last_occurred_at"] == 200.0
        assert unchanged["acknowledged_at"] == 210.0


def test_implicit_observation_identity_ignores_clock_derived_read_progress(tmp_path):
    first = problem_campaign()
    first["progress"] = {"active_seconds": 10.0, "wall_seconds": 12.0}
    second = problem_campaign()
    second["progress"] = {"active_seconds": 10.25, "wall_seconds": 12.25}
    with EvidenceAlertStore(tmp_path) as store:
        assert store.evaluate(first, evaluated_at=200.0)["duplicate"] is False
        assert store.evaluate(second, evaluated_at=201.0)["duplicate"] is True
        quota = next(
            row for row in store.alerts(campaign_id="campaign-1")
            if row["kind"] == "provider-quota-exhausted"
        )
        assert quota["occurrence_count"] == 1

        changed = problem_campaign()
        changed["updated_at"] = 101.0
        assert store.evaluate(changed, evaluated_at=202.0)["updated"] == 4


def test_resolution_then_recurrence_opens_a_new_generation(tmp_path):
    store = EvidenceAlertStore(tmp_path)
    store.evaluate(problem_campaign(), observation_id="problem-1", evaluated_at=200.0)
    first = next(
        row for row in store.alerts(campaign_id="campaign-1")
        if row["kind"] == "provider-quota-exhausted"
    )
    store.acknowledge(first["id"], acknowledged_at=210.0)

    store.evaluate(campaign(), observation_id="healthy", evaluated_at=300.0)
    resolved = store.get(first["id"])
    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["resolved_at"] == 300.0
    assert resolved["last_occurred_at"] == 200.0

    store.evaluate(problem_campaign(), observation_id="problem-2", evaluated_at=400.0)
    recurred = next(
        row for row in store.alerts(campaign_id="campaign-1", state="open")
        if row["kind"] == "provider-quota-exhausted"
    )
    assert recurred["id"] != first["id"]
    assert recurred["occurrence_generation"] == 2
    assert recurred["recurrence_count"] == 1
    assert recurred["acknowledged_at"] is None
    store.close()

    with EvidenceAlertStore(tmp_path) as reopened:
        persisted = reopened.get(recurred["id"])
        assert persisted is not None
        assert persisted["occurrence_generation"] == 2


def test_provider_disconnect_is_not_rendered_as_healthy(tmp_path):
    disconnected = campaign(service={
        "status": "disconnected",
        "repair": "Reconnect the local service and explicitly resume.",
    })
    with EvidenceAlertStore(tmp_path) as store:
        store.evaluate(disconnected, observation_id="disconnected", evaluated_at=100.0)
        alert = store.alerts(campaign_id="campaign-1")[0]
    assert alert["kind"] == "provider-outage"
    assert alert["severity"] == "critical"
    assert "explicitly resume" in alert["repair"]


@pytest.mark.parametrize("code", ["provider_failure", "provider_mismatch"])
def test_campaign_provider_blockers_use_provider_outage_alert_kind(tmp_path, code):
    blocked = campaign(
        state="degraded",
        blocker={
            "code": code,
            "message": "The configured campaign provider is unavailable.",
            "repair": "Inspect provider configuration and bounded retry state.",
        },
    )
    with EvidenceAlertStore(tmp_path) as store:
        store.evaluate(blocked, observation_id=code, evaluated_at=100.0)
        alert = store.alerts(campaign_id="campaign-1")[0]
    assert alert["kind"] == "provider-outage"
    assert alert["source"]["type"] == "provider"


def test_bounded_store_replaces_resolved_but_never_discards_active(tmp_path):
    two_conditions = campaign(
        usage={"maximum_source_age_seconds": 75},
        service={"status": "disconnected", "repair": "Reconnect."},
    )
    with EvidenceAlertStore(tmp_path, maximum_alerts=2) as store:
        store.evaluate(two_conditions, observation_id="two", evaluated_at=100.0)
        assert store.inbox("campaign-1")["counts"]["active"] == 2
        milestone = campaign(arms=[{
            "id": "arm-a", "paper_run_id": "paper-a", "state": "running",
            "counts": {"observations": 10}, "fault": None,
        }])
        store.evaluate(milestone, observation_id="milestone", evaluated_at=200.0)
        current = store.inbox("campaign-1")
        assert current["counts"] == {
            "returned": 2, "total": 2, "active": 1, "unacknowledged_active": 1,
        }
        assert any(row["kind"] == "evidence-milestone" for row in current["alerts"])

    with EvidenceAlertStore(tmp_path / "full", maximum_alerts=1) as store:
        with pytest.raises(EvidenceAlertError, match="full of active"):
            store.evaluate(two_conditions, observation_id="too-many", evaluated_at=100.0)
        assert store.inbox("campaign-1")["counts"]["total"] == 0


def test_reordered_or_conflicting_observations_cannot_rewrite_newer_state(tmp_path):
    with EvidenceAlertStore(tmp_path) as store:
        store.evaluate(problem_campaign(), observation_id="newer", evaluated_at=300.0)
        with pytest.raises(EvidenceAlertError, match="older than"):
            store.evaluate(campaign(), observation_id="older", evaluated_at=200.0)
        assert store.inbox("campaign-1")["counts"]["active"] == 4

        with pytest.raises(EvidenceAlertError, match="reused with different"):
            store.evaluate(campaign(), observation_id="newer", evaluated_at=400.0)


def test_export_is_deterministic_local_only_json(tmp_path):
    with EvidenceAlertStore(tmp_path) as store:
        store.evaluate(problem_campaign(), observation_id="snapshot", evaluated_at=200.0)
        first = store.export("campaign-1")
        second = store.export("campaign-1")
    assert first == second
    assert first["schema"] == EXPORT_SCHEMA
    assert first["local_only"] is True
    assert first["external_delivery"] is False
    assert first["id"] == first["reproducibility_sha256"]
    json.dumps(first, allow_nan=False)


def test_input_and_page_bounds_are_enforced(tmp_path):
    with EvidenceAlertStore(tmp_path) as store:
        with pytest.raises(EvidenceAlertError, match="100-row bound"):
            store.evaluate(
                campaign(),
                accounting_mismatches=[{"id": f"gap-{index}"} for index in range(101)],
            )
        with pytest.raises(EvidenceAlertError, match="between 1 and 500"):
            store.alerts(limit=501)
