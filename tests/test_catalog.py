import copy
import io
import json

import pytest

from pumpwizard_runner.catalog import (
    add_strategy, load_catalog, package_for, period_performance, public_rows, refresh_catalog, select_rows,
)
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.strategies import parse_package


@pytest.fixture
def catalog():
    return load_catalog()


def test_offline_catalog_covers_published_cohorts_and_staged_matrix(catalog):
    rows = catalog["strategies"]
    assert len(rows) == 1111
    assert len({x["id"] for x in rows}) == len(rows)
    assert len([x for x in rows if x.get("report")]) == 211
    assert len([x for x in rows if x.get("trial")]) == 72
    assert len({x["family"] for x in rows}) == 14
    for row in rows:
        parsed = parse_package(package_for(row))
        assert parsed.effective_rules == row["config"]
        assert (parsed.compatibility == "monitoring-ready") == (not row.get("trial"))


def test_untested_is_unknown_and_not_ranked_as_zero_return(catalog):
    untested = select_rows(catalog, outcome="Untested")
    assert len(untested) == 900
    assert all(row["report"] is None for row in untested)
    profitable = select_rows(catalog, outcome="Profitable")
    assert all(row["report"]["pnl_usd"] > 0 for row in profitable)
    v3 = select_rows(catalog, family="persistent", cohort="V3")
    assert len(v3) == 4
    assert select_rows(catalog, query="no such strategy") == []


def test_add_pins_exact_config_and_deduplicates_across_results_and_restart(catalog, tmp_path):
    row = next(x for x in catalog["strategies"] if x["id"] == "early-frequency")
    lib = StrategyLibrary(tmp_path)
    agent_id, created = add_strategy(lib, row)
    assert created and agent_id
    instance = lib.instance(agent_id)
    assert instance["entry_mode"] == "disabled" and instance["exit_mode"] == "manual"
    changed = copy.deepcopy(row)
    changed["report"]["pnl_usd"] += 100
    changed["source_as_of"] += 100
    assert add_strategy(lib, changed) == (agent_id, False)
    lib.close()
    lib = StrategyLibrary(tmp_path)
    assert add_strategy(lib, row) == (agent_id, False)
    stored = lib.strategy(instance["strategy_digest"])["package"]
    assert stored["configuration"] == row["config"]
    # Changed rules are a new immutable local configuration, not a silent edit.
    changed["config"]["minimum_growth_fraction"] += .001
    another, created = add_strategy(lib, changed)
    assert created and another != agent_id
    assert lib.strategy(instance["strategy_digest"])["package"] == stored
    lib.close()


def test_trial_overlay_is_preserved_and_cannot_start_as_parent(catalog, tmp_path):
    lib = StrategyLibrary(tmp_path)
    for row in (x for x in catalog["strategies"] if x.get("trial")):
        assert add_strategy(lib, row) == (None, True)
        parsed = parse_package(package_for(row))
        saved = lib.strategy(parsed.digest)
        assert saved["package"]["trial"] == row["trial"]
        with pytest.raises(ValueError, match="cannot be started"):
            lib.create_instance(parsed.digest)
    assert lib.instances() == []
    lib.close()


def test_unknown_rule_cannot_be_silently_dropped(catalog):
    package = package_for(catalog["strategies"][0])
    package["configuration"]["confirmation_enabled"] = True
    assert parse_package(package).compatibility != "monitoring-ready"


def test_period_sorting_filter_and_roi_use_selected_window(catalog):
    rows = copy.deepcopy(catalog["strategies"][:4])
    for index, row in enumerate(rows):
        row["id"] = f"period-{index}"
        row["report"].update(equity_usd=600, initial_usd=500, pnl_usd=100-index,
                             pnl_1h_usd=[-2, 12, 0, None][index],
                             pnl_8h_usd=[35, -7, 0, None][index],
                             pnl_24h_usd=[1, 2, 3, None][index])
    document = {"strategies": rows}
    assert [r["id"] for r in select_rows(document, period="Last hour")] == ["period-1", "period-2", "period-0", "period-3"]
    assert select_rows(document, period="Last 8 hours")[0]["id"] == "period-0"
    assert select_rows(document, period="Last 24 hours")[0]["id"] == "period-2"
    assert [r["id"] for r in select_rows(document, period="Last hour", outcome="Profitable")] == ["period-1"]
    assert period_performance(rows[1], "Last hour") == (12, 12/588*100)
    assert period_performance(rows[3], "Last hour") == (None, None)
    rows[0]["report"].update(equity_usd=0, pnl_1h_usd=0)
    assert period_performance(rows[0], "Last hour") == (0, None)


def test_new_period_metrics_survive_import_and_legacy_snapshot_is_unknown(catalog):
    document = publication(catalog["strategies"][0])
    report = document["strategies"][0]["report"]
    report.update(pnl_1h_usd=17, pnl_8h_usd=-8)
    row = public_rows(document)[0]
    assert period_performance(row, "Last hour")[0] == 17
    assert period_performance(row, "Last 8 hours")[0] == -8
    del report["pnl_1h_usd"]
    assert period_performance(public_rows(document)[0], "Last hour") == (None, None)


def publication(row, timestamp=None):
    return {"_snapshot": {"source_as_of": timestamp or row["source_as_of"]}, "strategies": [
        {"id": row["id"], "label": row["label"], "family": row["family"], "trial": row["trial"],
         "report": dict(row["report"], config=row["config"])}]}


def test_refresh_keeps_old_row_dates_and_local_configs(catalog, tmp_path, monkeypatch):
    row = catalog["strategies"][0]
    document = publication(row, row["source_as_of"] + 10)
    document["strategies"][0]["report"]["pnl_usd"] += 1
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(document).encode()))
    cache = tmp_path / "catalog.json"
    result = refresh_catalog(catalog, cache)
    assert result["strategies"][0]["source_as_of"] == row["source_as_of"] + 10
    assert result["strategies"][1] == catalog["strategies"][1]
    assert load_catalog(cache) == result
    assert package_for(result["strategies"][0]) == package_for(row)
    before = cache.read_bytes()
    document["_snapshot"]["source_as_of"] -= 20
    with pytest.raises(ValueError, match="older"):
        refresh_catalog(result, cache)
    assert cache.read_bytes() == before


def test_invalid_results_do_not_replace_last_good_cache(catalog, tmp_path, monkeypatch):
    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps(catalog))
    before = cache.read_bytes()
    document = publication(catalog["strategies"][0])
    document["strategies"][0]["report"]["pnl_usd"] = float("nan")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(document).encode()))
    with pytest.raises(ValueError, match="metrics"):
        refresh_catalog(catalog, cache)
    assert cache.read_bytes() == before
    cache.write_text("[]")
    assert load_catalog(cache) == catalog


def test_upgrade_keeps_newest_snapshot_per_strategy(catalog, tmp_path):
    cached = copy.deepcopy(catalog)
    for row in cached["strategies"]:
        if row.get("report"):
            row["source_as_of"] -= 100
            row["report"].pop("pnl_1h_usd", None)
            row["report"].pop("pnl_8h_usd", None)
    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps(cached))
    assert load_catalog(cache) == catalog
    cached["strategies"][0]["source_as_of"] += 200
    cached["strategies"][0]["report"]["pnl_1h_usd"] = 23
    cache.write_text(json.dumps(cached))
    loaded = load_catalog(cache)
    assert loaded["strategies"][0] == cached["strategies"][0]
    assert loaded["strategies"][1:] == catalog["strategies"][1:]


def test_public_snapshot_rejects_duplicate_and_missing_metrics(catalog):
    document = publication(catalog["strategies"][0])
    document["strategies"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        public_rows(document)
    document["strategies"] = document["strategies"][:1]
    del document["strategies"][0]["report"]["pnl_usd"]
    with pytest.raises(ValueError, match="Missing"):
        public_rows(document)
