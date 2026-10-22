"""Batch 7 acceptance/integration command regressions."""

from __future__ import annotations

import json
from pathlib import Path

from pumpwizard_runner import cli
from pumpwizard_runner.forward_campaigns import START_CONFIRMATION


def result(capsys) -> object:
    return json.loads(capsys.readouterr().out)


def test_observation_operator_commands_prepare_start_export_and_stop(
    tmp_path: Path, capsys,
):
    data_dir = tmp_path / "data"
    common = ["--data-dir", str(data_dir)]

    assert cli.main([
        *common, "observation-prepare-24h",
        "--idempotency-key", "acceptance-prepare",
    ]) == 0
    preset = result(capsys)
    assert preset["network_reads"] == 0
    assert preset["dry_run"]["scheduled_intervals"] == 96
    assert preset["dry_run"]["cost"]["fully_costed_profitability_allowed"] is False

    assert cli.main([*common, "observation-presets"]) == 0
    assert result(capsys)[0]["identity_sha256"] == preset["identity_sha256"]

    assert cli.main([
        *common, "observation-start", preset["id"],
        "--idempotency-key", "acceptance-start",
        "--confirm", START_CONFIRMATION,
    ]) == 0
    campaign = result(capsys)
    assert campaign["preset_id"] == preset["id"]
    assert campaign["authority"] == {
        "wallet_allocation": False,
        "signer": False,
        "orders": False,
        "submission": False,
        "provider_purchase": False,
    }

    assert cli.main([*common, "observation-summary", campaign["id"]]) == 0
    summary = result(capsys)
    assert summary["resources"]["logical_provider_attempts"] == 0
    assert summary["profitability"]["fully_costed_allowed"] is False

    assert cli.main([*common, "observation-export", campaign["id"]]) == 0
    exported = result(capsys)
    assert exported["summary"]["campaign_id"] == campaign["id"]
    assert exported["summary"]["manifest_sha256"] == summary["manifest_sha256"]
    assert len(exported["summary_sha256"]) == 64

    assert cli.main([
        *common, "campaign-command", campaign["id"], "stop",
        "--idempotency-key", "acceptance-stop",
        "--confirm", campaign["id"],
    ]) == 0
    assert result(capsys)["state"] == "stopped"
