"""Expected values captured from PumpWizard, never from the local implementation."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from pumpwizard_runner.decision import DecisionEngine, MarketQuote, PositionView
from pumpwizard_runner.strategies import parse_package


REFERENCE = json.loads((Path(__file__).parent/"fixtures/momentum-reference.json").read_text())


def engine():
    return DecisionEngine(parse_package({"schema":"pumpwizard.strategy-handoff/v1",
        "strategy":{"id":"core-frequency","label":"Core frequency"},
        "configuration":REFERENCE["configuration"]}))


@pytest.mark.parametrize("case", REFERENCE["entries"], ids=lambda case: case["name"])
def test_momentum_reference_entry_screens_and_history_survive_restart(case):
    local=engine()
    recorded=[]
    for frame in case["frames"]:
        q=MarketQuote.from_record(frame["quote"])
        local.evaluate([q],frame["at"],evidence={q.mint:frame["evidence"]})
        assert local.entry_reason(q,frame["evidence"],frame["at"]) == frame["reason"]
        # Compare the complete rolling window, not just the final recommendation.
        history=[{"at":p.observed_at,"price":p.price_native,"real_sol":p.liquidity_native,"fx":p.quote_usd}
                 for p in local.histories.get(q.mint,[])]
        assert history == frame["history"]
        if local.quote_usable(q,frame["at"]):
            recorded.append(q.as_record())
        restarted=engine()
        restarted.restore(recorded)
        restarted.restore_checkpoint(local.checkpoint())
        assert restarted.histories == local.histories
        assert restarted.entry_reason(q,frame["evidence"],frame["at"]) == frame["reason"]
        local=restarted


@pytest.mark.parametrize("case", REFERENCE["exits"], ids=lambda case: case["name"])
def test_momentum_reference_exit_screens(case):
    local=engine()
    p=case["position"]
    position=PositionView("test",p["mint"],p["symbol"],p["entry_at"],p["initial_quantity"],p["quantity"],
        p["remaining_cost_usd"],p["cost_usd"],sold_stages=tuple(p["sold_stages"]),
        peak_price_usd=p["peak_price_usd"],last_usable_at=p["last_usable_at"],
        entry_pool=p["entry_pool"],entry_supply=p["entry_supply"])
    quote=MarketQuote.from_record(case["quote"]) if case["quote"] else None
    assert local.ladder_sell_value(quote,position) == pytest.approx(case["net_sell_usd"]) if case["net_sell_usd"] is not None else local.ladder_sell_value(quote,position) is None
    result=local._exit_decision(position,quote,case["evidence"],case["at"])
    expected=case["expected"]
    if expected is None:
        assert result is None
    else:
        assert result.reason == ("holding_limit" if expected["reason"] == "ten_day_limit" else expected["reason"])
        assert result.payload.get("stages",[]) == expected["stages"]
        if expected["stages"]:
            assert result.payload["fraction_of_original"] == sum(REFERENCE["configuration"]["targets"][i]["initial_fraction"] for i in expected["stages"])
    # No route context means no invented fee-adjusted sell valuation.
    if quote:
        assert local.ladder_sell_value(quote,replace(position,entry_pool="")) is None
