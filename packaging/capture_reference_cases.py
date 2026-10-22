"""Capture reference decision screens from a supplied PumpWizard checkout.

Only the reference implementation computes expected results. No providers,
wallets, live ledgers, or simulated fills are opened by this script.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys


def capture(root):
    sys.path.insert(0, str(root))
    from paper.ladder import LadderRunner

    cfg = next(x["config"] for x in json.loads((root/"long_hold_experiments.json").read_text())["experiments"]
               if x["id"] == "core-frequency")
    cfg = dict(cfg, entry_style="momentum")

    def reference():
        runner = LadderRunner.__new__(LadderRunner)
        runner.cfg = copy.deepcopy(cfg)
        runner.state = {"histories": {}, "observations": 0}
        runner.event = lambda *args: None
        return runner

    def quote(at, y=110, real=40, fx=100):
        return {"mint": "TestMint", "symbol": "TEST", "route": "pump_bonding_curve_sol",
                "curve_address": "TestCurve", "observed_at": at, "source_at": at, "fx_at": at,
                "x": 1_000_000_000, "y": y, "real_x": 500_000_000, "real_y": real,
                "supply": 1_000_000_000, "quote_usd": fx, "market_cap_usd": y*fx,
                "decimals": 6, "complete": False, "mayhem": False}

    entries = []
    series = {
        "growing": [quote(1000+i*10, 110+i, 40+i) for i in range(7)],
        "window_rollover": [quote(1000+i*10, y, 40+i) for i,y in enumerate((110,114,118,122,120,118,116,114))],
        "flat": [quote(1000+i*10) for i in range(5)],
        "fx_only": [quote(1000+i*10, fx=100+i) for i in range(5)],
        "no_reserve_growth": [quote(1000+i*10,110+i) for i in range(5)],
        "gap_reset": [quote(at,110+i,40+i) for i,at in enumerate((1000,1010,1020,1100,1110,1120,1130))],
        "low_liquidity": [quote(1000+i*10,110+i,2+i*.01) for i in range(5)],
    }
    for name, quotes in series.items():
        runner = reference()
        frames = []
        for q in quotes:
            at=q["observed_at"]
            market=runner.market(q,at)
            runner.update_histories({q["mint"]:q},{q["mint"]:market},at)
            frames.append({"at":at,"quote":q,"evidence":{},
                "reason":runner.entry_reason(q["mint"],market,{},at),
                "history":copy.deepcopy(runner.state["histories"].get(q["mint"],[]))})
        entries.append({"name":name,"frames":frames})
    runner=reference()
    warm=[quote(1000+i*10,110+i,40+i) for i in range(4)]
    for q in warm:
        runner.update_histories({q["mint"]:q},{q["mint"]:runner.market(q,q["observed_at"])},q["observed_at"])
    for name,q,at,evidence in (
        ("recorded_peak",warm[-1],1030,{"peak_cap":50000}),
        ("outside_band",quote(1040,300),1040,{}),
        ("stale",warm[-1],1070,{}),
        ("stale_fx",dict(quote(1040),fx_at=1),1040,{}),
    ):
        m=runner.market(q,at)
        frames=copy.deepcopy(entries[0]["frames"][:4])
        runner.update_histories({q["mint"]:q},{q["mint"]:m},at)
        frames.append({"at":at,"quote":q,"evidence":evidence,
            "reason":runner.entry_reason(q["mint"],m,evidence,at),
            "history":copy.deepcopy(runner.state["histories"].get(q["mint"],[]))})
        entries.append({"name":name,"frames":frames})
        runner=reference()
        for previous in warm:
            runner.update_histories({previous["mint"]:previous},{previous["mint"]:runner.market(previous,previous["observed_at"])},previous["observed_at"])

    runner=reference()
    entry=quote(1000)
    p=runner.buy_quote(runner.market(entry,1000))
    p.update(mint="TestMint",symbol="TEST",entry_at=1000,last_usable_at=1000,
             peak_price_usd=entry["y"]/entry["x"]*100,proceeds_usd=0,sold_stages=[],status="open")
    exits=[]
    scenarios=[("hold",quote(1010),{},{}), ("fee_adjusted_stop",quote(1010,84),{},{}),
               ("stop",quote(1010,60),{},{}), ("target",quote(1010,320),{},{}),
               ("multiple_targets",quote(1010,650,500),{},{}),
               ("trail",quote(1010,170),{"sold_stages":[0],"peak_price_usd":.0003},{}),
               ("ownership",quote(1010),{}, {"ownership":{"mint":"TestMint","status":"blocked"}}),
               ("wrong_mint_ownership",quote(1010),{}, {"ownership":{"mint":"Other","status":"blocked"}}),
               ("missing_quote",None,{},{}),
               ("wrong_pool",dict(quote(1700),curve_address="OtherCurve"),{},{}),
               ("holding_limit",quote(1000+cfg["max_hold_seconds"]),{}, {})]
    # Independent exit screens with a supplied partially filled portfolio. These
    # do not claim parity for the reference's virtual fill scheduler itself.
    partial = {"quantity": p["initial_quantity"] * .6,
               "remaining_cost_usd": p["cost_usd"] * .6, "sold_stages": [0]}
    scenarios.extend([
        ("partial_remaining_hold", quote(1010, 170), partial, {}),
        ("partial_remaining_stop", quote(1010, 60), partial, {}),
        ("partial_remaining_targets", quote(1010, 650, 500), partial, {}),
        ("partial_remaining_trail", quote(1010, 170), dict(partial, peak_price_usd=.0003), {}),
    ])
    for name,q,changes,evidence in scenarios:
        position=dict(copy.deepcopy(p),**changes)
        original=copy.deepcopy(position)
        at=q["observed_at"] if q else 1700
        m=runner.market(q,at)
        runner.exit_position("TestMint",position,m,evidence,at)
        exits.append({"name":name,"at":at,"quote":q,"position":original,"evidence":evidence,
                      "expected":position.get("exit_order"),
                      "net_sell_usd":(runner.sell_quote(m,original) or {}).get("proceeds_usd")})
    return {"schema":"pumpwizard.reference-decisions/v1", "scope":"momentum curve decision screens; no fills or scheduling parity",
            "source_hashes":{n:hashlib.sha256((root/"paper"/n).read_bytes()).hexdigest() for n in ("ladder.py","model.py")},
            "configuration":cfg,"entries":entries,"exits":exits}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--pumpwizard-root",type=Path,required=True)
    args=parser.parse_args()
    target=Path(__file__).parents[1]/"tests/fixtures/momentum-reference.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(capture(args.pumpwizard_root),indent=2)+"\n")
    print(target)
