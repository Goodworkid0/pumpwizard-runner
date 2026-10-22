"""Bound offline evaluation evidence without changing agents, wallets or replay runs."""

import copy
import json
from pathlib import Path
import socket

import pytest

from pumpwizard_runner import evaluation
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG, PortfolioEngine, PortfolioJournal
from pumpwizard_runner.strategies import content_hash, parse_package


def package():
    return {"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": copy.deepcopy(CONFIG)}


def test_replay_matches_existing_engine_and_exports_reproducible_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("Offline replay used network"))
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package(package())
        library.import_strategy(strategy)
        agent = library.create_instance(strategy.digest)
        before = list(library._connection.iterdump())
        recording = evaluation.example_recording()
        engine = PortfolioEngine(CONFIG)
        expected_events = []
        for batch in recording["batches"]:
            expected_events.extend(engine.step(batch))
        result = evaluation.evaluate(tmp_path, strategy.package, recording)
        assert result["checkpoint"] == engine.checkpoint()
        assert result["events"] == expected_events
        assert result["identity"]["package_sha256"] == strategy.digest
        assert result["identity"]["rules_sha256"] == content_hash(CONFIG)
        assert result["input_sha256"] == content_hash(recording)
        assert result["summary"]["open_positions"] == result["summary"]["pending_orders"] == 0
        assert result["summary"]["event_counts"]["fill_partial_exit"] == 1
        assert not result["funded_acceptance"] and not result["submission_enabled"]
        exported = json.loads(json.dumps(result))
        again = evaluation.evaluate(tmp_path, exported["strategy_package"], exported["recording"])
        assert again == result
        assert evaluation.reports(tmp_path, strategy.digest) == [result]
        assert list(library._connection.iterdump()) == before
        assert library.instance(agent)["entry_mode"] == "disabled"
        assert not list(tmp_path.glob("*wallet*")) and not (tmp_path / "strategy-replays.sqlite").exists()
    finally:
        library.close()


@pytest.mark.parametrize("change", ["configuration", "trial", "empty", "clock", "duplicate", "schema", "evidence", "quotes"])
def test_invalid_recording_or_unsupported_strategy_never_publishes_report(tmp_path, change):
    value, recording = package(), evaluation.example_recording()
    if change == "configuration":
        recording["configuration"]["order_usd"] += 1
    elif change == "trial":
        value["trial"] = {"version": 3}
    elif change == "empty":
        recording["batches"] = []
    elif change == "clock":
        recording["batches"][-1]["at"] = float("inf")
    elif change == "duplicate":
        recording["batches"][-1] = recording["batches"][-2]
    elif change == "schema":
        recording["schema"] = "unknown/v1"
    elif change == "evidence":
        del recording["batches"][-1]["evidence"]
    else:
        recording["batches"][-1]["quotes"] = [None]
    with pytest.raises(ValueError):
        evaluation.evaluate(tmp_path, value, recording)
    assert not (tmp_path / "local-evaluations.sqlite").exists()


def test_cancel_during_replay_leaves_existing_reports_and_cli_run_untouched(tmp_path):
    recording = evaluation.example_recording()
    original = evaluation.evaluate(tmp_path, package(), recording)
    journal = PortfolioJournal(tmp_path)
    try:
        journal.advance("cli-existing", recording["batches"][0], CONFIG)
        before = list(journal.db.iterdump())
        count = 0

        def cancelled():
            nonlocal count
            count += 1
            return count == 5

        with pytest.raises(ValueError, match="cancelled"):
            evaluation.evaluate(tmp_path, package(), recording, cancelled=cancelled)
        assert evaluation.reports(tmp_path, original["identity"]["package_sha256"]) == [original]
        assert list(journal.db.iterdump()) == before
    finally:
        journal.close()


def test_identity_and_history_do_not_mix_packages_or_recordings(tmp_path):
    recording = evaluation.example_recording()
    first = evaluation.evaluate(tmp_path, package(), recording)
    renamed = package()
    renamed["strategy"]["label"] = "Separate imported package"
    second = evaluation.evaluate(tmp_path, renamed, recording)
    recording["batches"] = recording["batches"][:5]
    third = evaluation.evaluate(tmp_path, package(), recording)
    assert len({r["id"] for r in (first, second, third)}) == 3
    assert len(evaluation.reports(tmp_path, first["identity"]["package_sha256"])) == 2
    assert evaluation.reports(tmp_path, second["identity"]["package_sha256"]) == [second]


def test_bundled_example_matches_existing_recording():
    assert evaluation.example_recording() == json.loads(
        (Path(__file__).parents[1] / "examples/core-frequency-replay.json").read_text())


def test_history_before_first_evaluation_does_not_create_database(tmp_path):
    assert evaluation.reports(tmp_path, "missing") == []
    assert list(tmp_path.iterdir()) == []


def test_evaluation_cli_uses_imported_agent_without_enabling_it(tmp_path, capsys):
    from pumpwizard_runner.cli import main

    library = StrategyLibrary(tmp_path)
    strategy = parse_package(package())
    library.import_strategy(strategy)
    agent = library.create_instance(strategy.digest)
    library.close()
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(evaluation.example_recording()))
    assert main(["--data-dir", str(tmp_path), "evaluate-recording", agent, str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["identity"]["package_sha256"] == strategy.digest
    assert not result["submission_enabled"]
