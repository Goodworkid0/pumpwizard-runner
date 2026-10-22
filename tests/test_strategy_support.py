"""Capability labels must agree with the execution boundary, never with marketing metadata."""

import copy
import json

import pytest

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import content_hash, parse_package
from pumpwizard_runner.strategy_support import import_comparison, reviewed_eligible, support_text
from pumpwizard_runner.trading_workspace import supported


def package():
    return {"schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": copy.deepcopy(CONFIG)}


@pytest.mark.parametrize("change", ["rules", "strict", "id", "trial", "nested_trial"])
def test_near_matches_and_overlays_never_claim_execution_support(change):
    value = package()
    if change == "rules":
        value["configuration"]["order_usd"] += 1
    elif change == "strict":
        value["configuration"]["entry_policy"] = "strict"
    elif change == "id":
        value["strategy"]["id"] = "another-strategy"
    elif change == "trial":
        value["trial"] = {"version": 3}
    else:
        value["strategy"]["trial"] = {"version": 2}
    value["evidence"] = {"funded_acceptance": True, "execution_supported": True}
    strategy = parse_package(value)
    assert not reviewed_eligible(strategy)
    assert not supported({"package": value})
    assert "Reviewed execution: unavailable" in support_text(strategy)
    assert "Funded agent lifecycle acceptance: pending" in support_text(strategy)


def test_identity_diff_preserves_prior_configuration_and_agent(tmp_path):
    library = StrategyLibrary(tmp_path)
    try:
        old = parse_package(package())
        assert reviewed_eligible(old)
        library.import_strategy(old)
        agent = library.create_instance(old.digest)
        assert "already imported" in import_comparison(library, old)
        renamed = package()
        renamed["strategy"]["label"] = "New label"
        new = parse_package(renamed)
        assert new.digest != old.digest and reviewed_eligible(new)
        assert content_hash(new.effective_rules) == content_hash(old.effective_rules)
        assert "Rules unchanged" in import_comparison(library, new)
        renamed["configuration"]["order_usd"] += 1
        changed = parse_package(renamed)
        assert "Changed rule fields: order_usd" in import_comparison(library, changed)
        library.import_strategy(changed)
        assert library.instance(agent)["package"] == old.package
        assert len(library.instances()) == 1
        assert not library.tracking_settings(agent)["enabled"]
    finally:
        library.close()


def test_overlay_rejected_by_order_session_and_worker_before_financial_changes(tmp_path, monkeypatch):
    from test_agent_execution import MINT, setup
    from pumpwizard_runner.execution_worker import WorkerConfig, candidates
    from pumpwizard_runner.signing_sessions import SessionManager
    from pumpwizard_runner.transfers import TransferError

    fixture = setup.__wrapped__(tmp_path)
    wallet, rpc, journal, library, agent, cash, service, _ = next(fixture)
    try:
        instance = library.instance(agent)
        instance["package"]["trial"] = {"version": 3}
        monkeypatch.setattr(library, "instance", lambda _: instance)
        before = list(journal.db.iterdump())
        with pytest.raises(TransferError, match="pinned core-frequency"):
            service.prepare(agent, wallet.address, MINT, cash["id"])
        with pytest.raises(TransferError, match="pinned core-frequency"):
            SessionManager().authorize(service, wallet, agent, cash["id"],
                                       budget_lamports=100_000_000, duration_seconds=60,
                                       max_orders=1, slippage_bps=100, reviewed_limits={})
        with pytest.raises(TransferError, match="pinned core-frequency"):
            candidates(service, WorkerConfig(agent, wallet.address, rpc.network), rpc.genesis(), {})
        assert before == list(journal.db.iterdump()) and not rpc.sent
        assert json.loads(json.dumps(instance["package"]))["trial"] == {"version": 3}
    finally:
        next(fixture, None)
