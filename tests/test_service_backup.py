"""Offline service backup/restore acceptance with generated fixture custody."""

import copy
import json
from pathlib import Path
import stat

import pytest

from pumpwizard_runner import cli
from pumpwizard_runner.background_service import (
    BackgroundServiceEngine,
    BackgroundServiceStore,
    ServiceOwnershipError,
)
from pumpwizard_runner.evidence_alerts import EvidenceAlertStore
from pumpwizard_runner.forward_campaigns import (
    REQUEST_SCHEMA,
    START_CONFIRMATION,
    ForwardCampaignStore,
)
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.service_backup import BACKUP_SCHEMA, create_backup, restore_backup
from pumpwizard_runner.service_lifecycle import TOKEN_FILE, access_token
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.service_signing import ServiceSigningController
from pumpwizard_runner.service_wallet_vault import ServiceWalletVault
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import GENESIS
from test_service_wallet_vault import PASSWORD, envelope, fixture_wallet


def campaign_request() -> dict[str, object]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "minimum_samples": 2,
        "minimum_price_changes": 1,
        "minimum_growth_fraction": 0.01,
        "quote_max_age_seconds": 120,
        "failed_attempt_probability": 0,
    })
    package = {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": "backup-arm", "label": "Backup arm"},
        "configuration": configuration,
    }
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": "backup-restore-campaign",
        "supersedes_campaign_id": None,
        "source": {
            "provider": "FixtureSource", "minimum_cap": 1_000,
            "maximum_cap": 50_000, "priority_mints": [],
            "limit": 24, "cadence_seconds": 5,
        },
        "arms": [{
            "id": "arm-a", "source_session_id": "backup-session",
            "strategy_sha256": parse_package(package).digest,
            "strategy_package": package, "simulated_capital_usd": 100,
        }],
        "assumptions": {
            "cost_model": "strategy-declared round trip",
            "execution_model": "paper replay v3; no funded fills",
        },
        "coverage": {
            "max_source_age_seconds": 120, "minimum_independent_tokens": 1,
            "minimum_observations": 10, "maximum_gap_seconds": 60,
        },
        "duration_seconds": 600,
        "quotas": {
            "max_requests": 5, "max_active_runtime_seconds": 600,
            "max_storage_bytes": 10_000_000, "max_concurrency": 1,
        },
        "provider_cost": {
            "currency": "USD", "marginal_cost_per_request": None,
            "fixed_subscription_cost": None, "price_basis": "unknown",
        },
    }


def prepared_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    library = StrategyLibrary(source)
    library.close()
    store = BackgroundServiceStore(source)
    started = store.start_instance(port=8791)
    store.configure_job(enabled=True, interval_seconds=30)
    store.stop_instance(started["instance_id"], "Fixture backup boundary")
    store.close()
    recorder = ServiceEvidenceRecorder(source)
    recorder.close()

    wallet = fixture_wallet(root, "browser-wallet")
    ServiceWalletVault(source).enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    journal = TradeJournal(source)
    with journal.db:
        journal.db.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "fixture-trade",
                GENESIS["mainnet-beta"],
                wallet.address,
                "fixture-finalized-signature",
                "finalized",
                1.0,
                "{}",
                "fixture finalized receipt",
                "{}",
            ),
        )
    journal.close()
    access_token(source, create=True)
    (source / "background-service.log").write_text("fixture log must not be copied\n")
    return source, wallet.address


def test_offline_backup_restores_jobs_receipts_and_vault_ciphertext_locked(tmp_path: Path):
    source, address = prepared_source(tmp_path)
    (source / "solanamarket-wallet.json").write_text("encrypted browser wallet fixture\n")
    (source / "solanamarket-agent-settings.json").write_text("provider settings fixture\n")
    credentials = source / "ai-credentials"
    credentials.mkdir()
    (credentials / "fixture.json").write_text("plaintext-secret-must-not-be-copied\n")
    automation_wallet = source / "automation-wallet"
    automation_wallet.mkdir()
    (automation_wallet / "solanamarket-wallet.json").write_text(
        "encrypted legacy automation wallet fixture\n"
    )
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"

    created = create_backup(source, backup)
    assert created["schema"] == BACKUP_SCHEMA
    assert not (backup / TOKEN_FILE).exists()
    assert not (backup / "background-service.log").exists()
    assert not (backup / "background-service.lock").exists()
    assert not (backup / "solanamarket-wallet.json").exists()
    assert not (backup / "solanamarket-agent-settings.json").exists()
    assert not (backup / "ai-credentials").exists()
    assert not (backup / "automation-wallet").exists()
    manifest_text = (backup / "manifest.json").read_text()
    assert access_token(source, create=False) not in manifest_text
    assert str(source) not in manifest_text
    assert "plaintext-secret-must-not-be-copied" not in manifest_text
    assert created["excluded"] == [
        "background-service-access-token",
        "background-service.lock",
        "background-service.log",
        "solanamarket-wallet.json",
        "automation-wallet/",
        "ai-credentials/",
        "solanamarket-agent-settings.json",
    ]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in backup.rglob("*") if path.is_file()
    )

    result = restore_backup(backup, restored)
    assert result["restored"] is True
    assert result["signing_state"] == "locked-on-start"
    assert not (restored / TOKEN_FILE).exists()
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in restored.rglob("*") if path.is_file()
    )
    restored_store = BackgroundServiceStore(restored)
    try:
        assert restored_store.job()["enabled"] is True
        assert restored_store.job()["interval_seconds"] == 30
    finally:
        restored_store.close()
    journal = TradeJournal(restored)
    try:
        row = journal.db.execute(
            "SELECT status,signature,detail FROM trades WHERE id='fixture-trade'"
        ).fetchone()
        assert tuple(row) == (
            "finalized", "fixture-finalized-signature", "fixture finalized receipt"
        )
    finally:
        journal.close()
    original_vault = source / "service-wallets" / f"{address}.json"
    restored_vault = restored / "service-wallets" / f"{address}.json"
    assert restored_vault.read_bytes() == original_vault.read_bytes()
    assert ServiceSigningController(restored).state()["signer_state"] == "locked"


def test_backup_restores_campaign_provider_and_alert_evidence_paused(tmp_path: Path):
    source, _ = prepared_source(tmp_path)
    recorder = ServiceEvidenceRecorder(source)
    campaigns = ForwardCampaignStore(source)
    alerts = EvidenceAlertStore(source)
    try:
        started = campaigns.start(
            campaign_request(), idempotency_key="backup-start",
            confirmation=START_CONFIRMATION,
        )
        scan_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            limit=24, campaign_consumers={started["id"]: 1},
        )
        failed = recorder.fail_scan(scan_id, TimeoutError("fixture provider timeout"))
        campaigns.record_scan(
            [started["id"]], success=False,
            finished_at=float(failed["received_at"]),
            next_retry_at=float(failed["next_retry_at"]),
            detail="fixture provider timeout",
        )
        campaign = campaigns.campaign(started["id"], recorder)
        alert_input = {
            **campaign,
            "service": {
                "status": "disconnected",
                "repair": "Restart the local service and explicitly resume.",
            },
        }
        alerts.evaluate(
            alert_input, observation_id="backup-alert-snapshot",
            evaluated_at=float(failed["received_at"]) + 1,
        )
    finally:
        alerts.close()
        campaigns.close()
        recorder.close()

    backup = tmp_path / "campaign-backup"
    restored = tmp_path / "campaign-restored"
    create_backup(source, backup)
    restore_backup(backup, restored)

    restored_recorder = ServiceEvidenceRecorder(restored)
    restored_campaigns = ForwardCampaignStore(restored)
    restored_alerts = EvidenceAlertStore(restored)
    try:
        before_recovery = restored_campaigns.campaign(started["id"], restored_recorder)
        assert before_recovery["state"] == "degraded"
        assert before_recovery["usage"]["requests"] == 1
        assert before_recovery["usage"]["timeout_events"] == 1
        inbox = restored_alerts.inbox(started["id"])
        assert any(row["kind"] == "provider-outage" for row in inbox["alerts"])

        assert restored_campaigns.recover_running() == 1
        recovered = restored_campaigns.campaign(started["id"], restored_recorder)
        assert recovered["state"] == "paused"
        assert recovered["blocker"]["code"] == "service_restart"
        assert restored_campaigns.scan_demands() == []
        resumed = restored_campaigns.command(
            started["id"], action="resume", idempotency_key="restore-resume",
        )
        assert resumed["state"] == "running"
    finally:
        restored_alerts.close()
        restored_campaigns.close()
        restored_recorder.close()

    assert ServiceSigningController(restored).state()["signer_state"] == "locked"


def test_restore_rejects_tampering_and_existing_destination(tmp_path: Path):
    source, address = prepared_source(tmp_path)
    backup = tmp_path / "backup"
    create_backup(source, backup)
    vault = backup / "service-wallets" / f"{address}.json"
    record = json.loads(vault.read_text())
    record["vault_id"] = "34" * 16
    vault.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="integrity verification"):
        restore_backup(backup, tmp_path / "tampered-restore")

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="must not already exist"):
        restore_backup(backup, existing)


def test_copy_only_rollback_keeps_older_and_newest_service_stores_separate(tmp_path: Path):
    source, address = prepared_source(tmp_path)
    # Install the current service-signing tables before the first snapshot; a
    # rollback copy must carry every newest store without restoring an unlock.
    ServiceSigningController(source)
    older_backup = tmp_path / "older-backup"
    create_backup(source, older_backup)

    journal = TradeJournal(source)
    try:
        with journal.db:
            journal.db.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "fixture-trade-newest",
                    GENESIS["mainnet-beta"],
                    address,
                    "fixture-finalized-signature-newest",
                    "finalized",
                    2.0,
                    "{}",
                    "newest finalized receipt",
                    "{}",
                ),
            )
    finally:
        journal.close()
    newest_backup = tmp_path / "newest-backup"
    create_backup(source, newest_backup)

    older_restore = tmp_path / "older-rollback-copy"
    newest_restore = tmp_path / "newest-rollback-copy"
    restore_backup(older_backup, older_restore)
    restore_backup(newest_backup, newest_restore)

    def trade_count(directory: Path) -> int:
        restored = TradeJournal(directory)
        try:
            assert restored.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='service_acceptance_approvals'"
            ).fetchone() is not None
            return restored.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        finally:
            restored.close()

    assert trade_count(older_restore) == 1
    assert trade_count(newest_restore) == 2
    assert trade_count(source) == 2
    assert ServiceSigningController(older_restore).state()["signer_state"] == "locked"
    assert ServiceSigningController(newest_restore).state()["signer_state"] == "locked"


def test_backup_refuses_a_data_directory_owned_by_a_running_service(tmp_path: Path):
    engine = BackgroundServiceEngine(tmp_path / "source")
    engine.start()
    try:
        with pytest.raises(ServiceOwnershipError, match="owns this data directory"):
            create_backup(tmp_path / "source", tmp_path / "backup")
    finally:
        engine.stop()


def test_cli_backup_and_restore_rehearsal_uses_new_locked_directory(
    tmp_path: Path, capsys
):
    source, _ = prepared_source(tmp_path)
    backup = tmp_path / "cli-backup"
    restored = tmp_path / "cli-restored"

    assert cli.main([
        "--data-dir", str(source), "service", "backup",
        "--destination", str(backup),
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["destination"] == str(backup)
    assert created["restore_policy"] == "new-directory-only; service starts locked"

    assert cli.main([
        "--data-dir", str(restored), "service", "restore",
        "--backup", str(backup),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["destination"] == str(restored)
    assert result["signing_state"] == "locked-on-start"
    assert ServiceSigningController(restored).state()["signer_state"] == "locked"
